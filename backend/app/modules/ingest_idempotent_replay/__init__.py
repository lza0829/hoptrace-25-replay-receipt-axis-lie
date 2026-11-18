"""幂等装载核心：稳定业务键、装载回执、幂等证明、回执对照核证。

本模块不依赖 HTTP 层。装载服务在同一个 SQLite 事务中调用这里的能力：

1. 用 ``waybill_id + event_ts + location_code + event_code`` 组装稳定业务键；
2. 先查 ``ingest_receipts``（按业务键落库的回执），再查正式时间线；
3. 只有回执和正式点都不存在时才插入正式点，并在同一事务写回执；
4. 每个业务键的写入 / 跳过动作都写入 ``ingest_proofs``，供按批次只读查询。

跳过闸（硬约束，禁止假跳过）：写任何 ``skip`` 证明之前，必须先做
「回执 → 正式点」三方对撞（``evaluate_receipt_vs_formal`` /
``assert_receipt_reconciles``），同时满足：

* 回执指向的正式点真实存在（按回执 ``hop_id`` 查 ``hops``）；
* 该正式点未撤回（``retracted=0``）且仍在正式轴（``channel='primary'``）；
* 回执的业务键四字段与正式点四字段逐字段一致，且回执状态为 ``success``。

任一条件不满足即抛 :class:`IdempotencyError`，装载整笔回滚，绝不写 skip。
对撞通过的每一次跳过都在 ``ingest_receipt_checks`` 留一条 ``matched`` 留痕，
与当条 skip 证明互链（``proof_id``），供只读核证接口复核。

幂等依据只能来自数据库回执，禁止使用进程内 Set。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

KEY_FIELDS = ("waybill_id", "event_ts", "location_code", "event_code")

ACTION_WRITE = "write"
ACTION_SKIP = "skip"
ACTIONS = (ACTION_WRITE, ACTION_SKIP)

SOURCE_INGEST = "ingest"
SOURCE_FORMAL_BACKFILL = "formal_backfill"

VERDICT_MATCHED = "matched"
VERDICT_MISMATCH = "mismatch"
VERDICTS = (VERDICT_MATCHED, VERDICT_MISMATCH)

# 对照失败原因（失败闭，reason 同时进异常信息，便于定位）。
REASON_CHECK_MATCHED = "matched"
REASON_RECEIPT_NOT_SUCCESS = "receipt_not_success"
REASON_FORMAL_MISSING = "formal_point_missing"
REASON_FORMAL_RETRACTED = "formal_point_retracted"
REASON_FORMAL_NOT_PRIMARY = "formal_point_not_primary"
REASON_HOP_KEY_MISMATCH = "hop_id_business_key_mismatch"


class IdempotencyError(RuntimeError):
    """回执与正式线不一致，或业务键无法安全落账时抛出（装载须整体回滚）。"""


def key_fields(hop: Mapping[str, Any]) -> dict[str, str]:
    """取出并规范化业务键字段；缺字段直接失败闭。"""
    fields: dict[str, str] = {}
    for name in KEY_FIELDS:
        raw = hop.get(name)
        if raw is None or str(raw).strip() == "":
            raise IdempotencyError(f"业务键缺少字段：{name}")
        fields[name] = str(raw).strip()
    return fields


def business_key(hop: Mapping[str, Any]) -> str:
    """稳定业务键：四个字段以 ``|`` 连接。"""
    return business_key_from_fields(key_fields(hop))


def business_key_from_fields(fields: Mapping[str, str]) -> str:
    return "|".join(fields[name] for name in KEY_FIELDS)


def find_receipt(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ingest_receipts WHERE business_key=?",
        (key,),
    ).fetchone()


def receipt_matches_fields(receipt: sqlite3.Row, fields: Mapping[str, str]) -> bool:
    return all(receipt[name] == fields[name] for name in KEY_FIELDS)


def insert_receipt(
    conn: sqlite3.Connection,
    *,
    fields: Mapping[str, str],
    hop_id: str,
    batch_id: str,
    run_id: str,
    now: str,
    source: str = SOURCE_INGEST,
) -> sqlite3.Row:
    """写入成功回执，并立即回读校验。调用方须保证正式点已在同一事务中落账。"""
    key = business_key_from_fields(fields)
    conn.execute(
        """
        INSERT INTO ingest_receipts(
          business_key, waybill_id, event_ts, location_code, event_code,
          hop_id, status, source, first_batch_id, first_run_id, created_at
        ) VALUES (?,?,?,?,?,?, 'success', ?, ?, ?, ?)
        """,
        (
            key,
            fields["waybill_id"],
            fields["event_ts"],
            fields["location_code"],
            fields["event_code"],
            hop_id,
            source,
            batch_id,
            run_id,
            now,
        ),
    )
    receipt = find_receipt(conn, key)
    if receipt is None or receipt["hop_id"] != hop_id or receipt["status"] != "success":
        raise IdempotencyError(f"回执写入后回读不一致：{key}")
    return receipt


def load_formal_by_hop_id(conn: sqlite3.Connection, hop_id: str) -> sqlite3.Row | None:
    """按回执指向的 hop_id 取正式轴成员（含撤回 / 旁路状态，供对撞判定）。"""
    return conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, channel, retracted
        FROM hops WHERE id=?
        """,
        (hop_id,),
    ).fetchone()


def evaluate_receipt_vs_formal(
    receipt: sqlite3.Row,
    formal: sqlite3.Row | None,
    fields: Mapping[str, str],
) -> dict[str, Any]:
    """回执 → 正式点三方对撞（纯函数，不落库）。

    返回对撞结论：``verdict`` 为 ``matched`` / ``mismatch``，附机器可读
    ``reason`` 与人类可读 ``message``。对撞通过必须同时满足：

    1. 回执状态为 ``success``；
    2. 回执 ``hop_id`` 指向的正式点存在；
    3. 正式点未撤回且在正式轴（``channel='primary'``）；
    4. 正式点四字段与本次业务键（即回执业务键）逐字段一致。
    """
    key = business_key_from_fields(fields)
    detail: dict[str, Any] = {
        "business_key": key,
        "receipt_hop_id": receipt["hop_id"],
        "receipt_status": receipt["status"],
        "formal_hop_id": formal["id"] if formal is not None else None,
        "formal_channel": formal["channel"] if formal is not None else None,
        "formal_retracted": int(formal["retracted"]) if formal is not None else None,
        "key_match": 0,
        "verdict": VERDICT_MISMATCH,
        "reason": REASON_CHECK_MATCHED,
        "message": "",
    }

    def fail(reason: str, message: str) -> dict[str, Any]:
        detail["verdict"] = VERDICT_MISMATCH
        detail["reason"] = reason
        detail["message"] = message
        return detail

    if receipt["status"] != "success":
        return fail(
            REASON_RECEIPT_NOT_SUCCESS,
            f"回执状态不是 success（status={receipt['status']}），禁止跳过：{key}",
        )
    if formal is None:
        return fail(
            REASON_FORMAL_MISSING,
            f"回执指向的正式点 {receipt['hop_id']} 不存在，禁止假跳过：{key}",
        )
    if int(formal["retracted"]) != 0:
        return fail(
            REASON_FORMAL_RETRACTED,
            f"回执指向撤回点 {formal['id']}（retracted=1），禁止跳过：{key}",
        )
    if formal["channel"] != "primary":
        return fail(
            REASON_FORMAL_NOT_PRIMARY,
            f"回执指向的 {formal['id']} 不在正式轴（channel={formal['channel']}），"
            f"禁止跳过：{key}",
        )
    mismatched = [name for name in KEY_FIELDS if formal[name] != fields[name]]
    if formal["id"] != receipt["hop_id"] or mismatched:
        return fail(
            REASON_HOP_KEY_MISMATCH,
            f"回执 hop_id={receipt['hop_id']} 与业务键正式点不一致"
            f"（正式点={formal['id']}，不符字段={mismatched or 'hop_id'}），禁止跳过：{key}",
        )

    detail["key_match"] = 1
    detail["verdict"] = VERDICT_MATCHED
    detail["reason"] = REASON_CHECK_MATCHED
    detail["message"] = f"回执 {receipt['hop_id']} 与正式点三方一致，允许跳过：{key}"
    return detail


def assert_receipt_reconciles(
    receipt: sqlite3.Row,
    formal: sqlite3.Row | None,
    fields: Mapping[str, str],
) -> dict[str, Any]:
    """跳过闸：对撞不通过直接抛 :class:`IdempotencyError`（调用方整笔回滚）。"""
    check = evaluate_receipt_vs_formal(receipt, formal, fields)
    if check["verdict"] != VERDICT_MATCHED:
        raise IdempotencyError(check["message"])
    return check


def record_receipt_check(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    run_id: str,
    check: Mapping[str, Any],
    proof_id: int | None = None,
    now: str,
) -> int:
    """追加一条回执对照核证留痕（只追加），返回留痕 id。"""
    verdict = check["verdict"]
    if verdict not in VERDICTS:
        raise IdempotencyError(f"未知核证结论：{verdict}")
    cur = conn.execute(
        """
        INSERT INTO ingest_receipt_checks(
          batch_id, run_id, business_key, receipt_hop_id, receipt_status,
          formal_hop_id, formal_channel, formal_retracted, key_match,
          verdict, reason, proof_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            run_id,
            check["business_key"],
            check["receipt_hop_id"],
            check["receipt_status"],
            check["formal_hop_id"],
            check["formal_channel"],
            check["formal_retracted"],
            int(check["key_match"]),
            verdict,
            check["reason"],
            proof_id,
            now,
        ),
    )
    return int(cur.lastrowid)


def list_receipt_checks(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    business_key: str | None = None,
    verdict: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """按批次 / 业务键 / 结论查询回执对照核证留痕（只读）。"""
    where: list[str] = []
    params: list[str] = []
    if batch_id:
        where.append("batch_id=?")
        params.append(batch_id)
    if business_key:
        where.append("business_key=?")
        params.append(business_key)
    if verdict:
        if verdict not in VERDICTS:
            raise IdempotencyError(f"未知核证结论：{verdict}")
        where.append("verdict=?")
        params.append(verdict)
    sql = (
        "SELECT id, batch_id, run_id, business_key, receipt_hop_id, receipt_status, "
        "formal_hop_id, formal_channel, formal_retracted, key_match, verdict, "
        "reason, proof_id, created_at FROM ingest_receipt_checks"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(str(max(1, min(int(limit), 5000))))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def receipt_check_summary(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    business_key: str | None = None,
) -> dict[str, int]:
    """全量聚合计数，避免分页截断导致汇总失真。"""
    where: list[str] = []
    params: list[str] = []
    if batch_id:
        where.append("batch_id=?")
        params.append(batch_id)
    if business_key:
        where.append("business_key=?")
        params.append(business_key)
    sql = (
        "SELECT verdict, COUNT(*) AS c FROM ingest_receipt_checks"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY verdict"
    )
    counts = {row["verdict"]: row["c"] for row in conn.execute(sql, params).fetchall()}
    matched = counts.get(VERDICT_MATCHED, 0)
    mismatch = counts.get(VERDICT_MISMATCH, 0)
    return {"checks": matched + mismatch, "matched": matched, "mismatch": mismatch}


def reconcile_receipt(conn: sqlite3.Connection, receipt: sqlite3.Row) -> dict[str, Any]:
    """只读现场复核：按回执现状重新做一次三方对撞，不写任何数据。

    供核证只读接口使用——即便库里尚未留痕（或留痕后状态又被改坏），
    也能给出回执当前指向是否仍然对板的结论。
    """
    fields = {name: receipt[name] for name in KEY_FIELDS}
    formal = load_formal_by_hop_id(conn, receipt["hop_id"])
    check = evaluate_receipt_vs_formal(receipt, formal, fields)
    check["live"] = True
    return check


def record_proof(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    run_id: str,
    fields: Mapping[str, str],
    action: str,
    hop_id: str,
    reason: str,
    now: str,
) -> int:
    """追加一条幂等证明。proof 只追加，不修改；返回证明行 id。"""
    if action not in ACTIONS:
        raise IdempotencyError(f"未知幂等动作：{action}")
    cur = conn.execute(
        """
        INSERT INTO ingest_proofs(
          batch_id, run_id, business_key, waybill_id, event_ts, location_code,
          event_code, action, hop_id, reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            run_id,
            business_key_from_fields(fields),
            fields["waybill_id"],
            fields["event_ts"],
            fields["location_code"],
            fields["event_code"],
            action,
            hop_id,
            reason,
            now,
        ),
    )
    return int(cur.lastrowid)


def list_proofs(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    run_id: str | None = None,
    action: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """按批次 / 轮次 / 动作查询幂等证明（只读）。"""
    where, params = _proof_where(batch_id=batch_id, run_id=run_id, action=action)
    sql = (
        "SELECT id, batch_id, run_id, business_key, waybill_id, event_ts, "
        "location_code, event_code, action, hop_id, reason, created_at "
        "FROM ingest_proofs"
    )
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(str(max(1, min(int(limit), 5000))))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count_proofs(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    run_id: str | None = None,
    action: str | None = None,
) -> dict[str, int]:
    """对全量证明做聚合计数，避免分页截断导致汇总失真。"""
    where, params = _proof_where(batch_id=batch_id, run_id=run_id, action=action)
    sql = (
        "SELECT action, COUNT(*) AS c FROM ingest_proofs"
        + (" WHERE " + where if where else "")
        + " GROUP BY action"
    )
    counts = {row["action"]: row["c"] for row in conn.execute(sql, params).fetchall()}
    writes = counts.get(ACTION_WRITE, 0)
    skips = counts.get(ACTION_SKIP, 0)
    return {"proofs": writes + skips, "writes": writes, "skips": skips}


def _proof_where(
    *,
    batch_id: str | None,
    run_id: str | None,
    action: str | None,
) -> tuple[str, list[str]]:
    where: list[str] = []
    params: list[str] = []
    if batch_id:
        where.append("batch_id=?")
        params.append(batch_id)
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    if action:
        if action not in ACTIONS:
            raise IdempotencyError(f"未知幂等动作：{action}")
        where.append("action=?")
        params.append(action)
    return " AND ".join(where), params


def list_receipts(conn: sqlite3.Connection, business_key: str | None = None) -> list[dict]:
    sql = "SELECT * FROM ingest_receipts"
    params: list[str] = []
    if business_key:
        sql += " WHERE business_key=?"
        params.append(business_key)
    sql += " ORDER BY created_at ASC, business_key ASC LIMIT 500"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]
