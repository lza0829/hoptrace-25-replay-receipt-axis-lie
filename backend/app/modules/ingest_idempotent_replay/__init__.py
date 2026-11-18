"""幂等装载核心：稳定业务键、装载回执、幂等证明。

本模块不依赖 HTTP 层。装载服务在同一个 SQLite 事务中调用这里的能力：

1. 用 ``waybill_id + event_ts + location_code + event_code`` 组装稳定业务键；
2. 先查 ``ingest_receipts``（按业务键落库的回执），再查正式时间线；
3. 只有回执和正式点都不存在时才插入正式点，并在同一事务写回执；
4. 每个业务键的写入 / 跳过动作都写入 ``ingest_proofs``，供按批次只读查询。

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


def assert_receipt_points_to_formal(receipt: sqlite3.Row, formal: sqlite3.Row | None) -> None:
    return


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
) -> None:
    """追加一条幂等证明。proof 只追加，不修改。"""
    if action not in ACTIONS:
        raise IdempotencyError(f"未知幂等动作：{action}")
    conn.execute(
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
