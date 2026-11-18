"""装载服务：带落库回执的幂等装载。

幂等策略（二选一中的「装载前查回执」路径）：

* 业务键 = ``waybill_id | event_ts | location_code | event_code``；
* 装载前按业务键查 ``ingest_receipts``：已有成功回执 ⇒ 整键跳过，写 ``skip`` 证明，
  既不插入正式点，也不触碰投影；
* 回执缺失但正式线已有点（历史数据）⇒ 回填回执后跳过，绝不重复插入；
* 两者都不存在 ⇒ 同一事务内插入正式点、写成功回执、写 ``write`` 证明；
* 回执与正式线对不上（点缺失 / 点已撤回 / id 不符）⇒ 抛错整体回滚，失败闭。

幂等状态只来自数据库，不使用进程内去重集合。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import ingest_idempotent_replay as replay
from app.seed import map_status
from app.services import timeline as timeline_svc

REASON_INSERTED = "inserted"
REASON_RECEIPT_EXISTS = "receipt_exists"
REASON_DUPLICATE_IN_BATCH = "duplicate_in_batch"
REASON_FORMAL_BACKFILL = "formal_exists_receipt_backfilled"


def run_batch(payload: dict | None = None) -> dict:
    """跑一批装载。``payload`` 为空时读取本地夹具（同夹具可重复跑）。"""
    if payload is None:
        path = FIXTURES_DIR / "ingest_batch.json"
        if not path.exists():
            return {"ok": False, "error": "缺少 ingest_batch.json"}
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"ok": False, "error": "装载请求必须是 JSON 对象"}
    if not isinstance(payload.get("hops", []), list):
        return {"ok": False, "error": "hops 必须是数组"}

    batch_id = str(payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}")
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])
    now = _now()
    run_id = _new_run_id()

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute(
            "SELECT COUNT(*) AS c FROM ingest_runs WHERE batch_id=?", (batch_id,)
        ).fetchone()["c"] + 1

        written = 0
        skipped = 0
        skip_reasons: dict[str, int] = {}
        written_keys: list[str] = []
        touched_waybills: set[str] = set()

        for hop in hops:
            if not isinstance(hop, dict):
                raise replay.IdempotencyError("hops 中存在非对象成员")
            fields = replay.key_fields(hop)
            key = replay.business_key_from_fields(fields)
            hop_id = str(hop.get("id") or "").strip()
            if not hop_id:
                raise replay.IdempotencyError(f"轨迹点缺少 id：{key}")

            receipt = replay.find_receipt(conn, key)
            if receipt is not None:
                formal = _load_formal_by_key(conn, fields)
                replay.assert_receipt_points_to_formal(receipt, formal)
                reason = (
                    REASON_DUPLICATE_IN_BATCH
                    if receipt["first_run_id"] == run_id
                    else REASON_RECEIPT_EXISTS
                )
                replay.record_proof(
                    conn,
                    batch_id=batch_id,
                    run_id=run_id,
                    fields=fields,
                    action=replay.ACTION_SKIP,
                    hop_id=receipt["hop_id"],
                    reason=reason,
                    now=now,
                )
                skipped += 1
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue

            formal = _load_formal_by_key(conn, fields)
            if formal is not None:
                # 历史正式点没有回执：回填回执再跳过，保证「正式点 ⇒ 回执」。
                if formal["channel"] != "primary" or formal["retracted"]:
                    raise replay.IdempotencyError(
                        f"业务键 {key} 已被不可用点 {formal['id']} 占用"
                        f"（channel={formal['channel']}, retracted={formal['retracted']}）"
                    )
                receipt = replay.insert_receipt(
                    conn,
                    fields=fields,
                    hop_id=formal["id"],
                    batch_id=batch_id,
                    run_id=run_id,
                    now=now,
                    source=replay.SOURCE_FORMAL_BACKFILL,
                )
                replay.assert_receipt_points_to_formal(receipt, formal)
                replay.record_proof(
                    conn,
                    batch_id=batch_id,
                    run_id=run_id,
                    fields=fields,
                    action=replay.ACTION_SKIP,
                    hop_id=formal["id"],
                    reason=REASON_FORMAL_BACKFILL,
                    now=now,
                )
                skipped += 1
                skip_reasons[REASON_FORMAL_BACKFILL] = (
                    skip_reasons.get(REASON_FORMAL_BACKFILL, 0) + 1
                )
                continue

            occupied = conn.execute(
                "SELECT id FROM hops WHERE id=?", (hop_id,)
            ).fetchone()
            if occupied is not None:
                raise replay.IdempotencyError(
                    f"正式点 id {hop_id} 已被其他业务键占用，拒绝覆盖"
                )

            _ensure_waybill(conn, hop)
            conn.execute(
                """
                INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hop_id,
                    fields["waybill_id"],
                    fields["event_ts"],
                    fields["location_code"],
                    fields["event_code"],
                    hop.get("seq"),
                ),
            )
            receipt = replay.insert_receipt(
                conn,
                fields=fields,
                hop_id=hop_id,
                batch_id=batch_id,
                run_id=run_id,
                now=now,
            )
            replay.assert_receipt_points_to_formal(
                receipt, _load_formal_by_key(conn, fields)
            )
            replay.record_proof(
                conn,
                batch_id=batch_id,
                run_id=run_id,
                fields=fields,
                action=replay.ACTION_WRITE,
                hop_id=hop_id,
                reason=REASON_INSERTED,
                now=now,
            )
            written += 1
            written_keys.append(key)
            touched_waybills.add(fields["waybill_id"])

        for waybill_id in sorted(touched_waybills):
            _sync_projection(conn, waybill_id, now)

        formal_after = conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE channel='primary' AND retracted=0"
        ).fetchone()["c"]
        receipts_total = conn.execute(
            "SELECT COUNT(*) AS c FROM ingest_receipts WHERE status='success'"
        ).fetchone()["c"]

        report = {
            "ok": True,
            "batch_id": batch_id,
            "run_id": run_id,
            "attempt": attempt,
            "cutoff_ts": cutoff,
            "input": len(hops),
            "written": written,
            "skipped": skipped,
            "skip_reasons": skip_reasons,
            "written_keys": written_keys,
            "formal_points_after": formal_after,
            "receipts_total": receipts_total,
            "idempotent_noop": len(hops) > 0 and written == 0 and skipped == len(hops),
        }
        _save_run(conn, report, now)
        conn.commit()
        return report
    except Exception as exc:  # 失败闭：任何异常整体回滚
        conn.rollback()
        return {"ok": False, "error": str(exc), "batch_id": batch_id, "run_id": run_id}
    finally:
        conn.close()


def list_runs(batch_id: str | None = None) -> list[dict]:
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM ingest_runs WHERE batch_id=? ORDER BY attempt ASC",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ingest_runs ORDER BY created_at DESC, attempt DESC LIMIT 100"
            ).fetchall()
        return [_decode_run(row) for row in rows]
    finally:
        conn.close()


def query_proofs(
    batch_id: str | None = None,
    run_id: str | None = None,
    action: str | None = None,
) -> dict:
    """按批次（必填）查询幂等证明，附带该批次各轮的汇总。"""
    if not batch_id:
        return {"ok": False, "error": "必须提供 batch_id"}
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT id, cutoff_ts, created_at FROM ingest_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        proofs = replay.list_proofs(
            conn, batch_id=batch_id, run_id=run_id, action=action
        )
        runs = [
            _decode_run(row)
            for row in conn.execute(
                "SELECT * FROM ingest_runs WHERE batch_id=? ORDER BY attempt ASC",
                (batch_id,),
            ).fetchall()
        ]
        return {
            "ok": True,
            "batch": dict(batch) if batch else None,
            "runs": runs,
            "summary": replay.count_proofs(
                conn, batch_id=batch_id, run_id=run_id, action=action
            ),
            "proofs": proofs,
        }
    finally:
        conn.close()


def list_receipts(business_key: str | None = None) -> list[dict]:
    conn = connect()
    try:
        return replay.list_receipts(conn, business_key=business_key)
    finally:
        conn.close()


def backfill_receipts(conn) -> int:
    """为历史正式点补回执（幂等），保证「正式点 ⇒ 回执」不变量。"""
    rows = conn.execute(
        """
        SELECT h.id, h.waybill_id, h.event_ts, h.location_code, h.event_code
        FROM hops h
        LEFT JOIN ingest_receipts r
          ON r.waybill_id=h.waybill_id AND r.event_ts=h.event_ts
         AND r.location_code=h.location_code AND r.event_code=h.event_code
        WHERE r.business_key IS NULL AND h.channel='primary' AND h.retracted=0
        ORDER BY h.id
        """
    ).fetchall()
    now = _now()
    filled = 0
    for row in rows:
        fields = {
            "waybill_id": row["waybill_id"],
            "event_ts": row["event_ts"],
            "location_code": row["location_code"],
            "event_code": row["event_code"],
        }
        try:
            replay.insert_receipt(
                conn,
                fields=fields,
                hop_id=row["id"],
                batch_id="SEED",
                run_id="SEED",
                now=now,
                source="seed_backfill",
            )
            filled += 1
        except Exception:
            # 业务键已被别处占用说明数据本身冲突，留给装载时失败闭。
            continue
    return filled


def _load_formal_by_key(conn, fields):
    return conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, channel, retracted
        FROM hops
        WHERE waybill_id=? AND event_ts=? AND location_code=? AND event_code=?
        """,
        (
            fields["waybill_id"],
            fields["event_ts"],
            fields["location_code"],
            fields["event_code"],
        ),
    ).fetchone()


def _ensure_waybill(conn, hop) -> None:
    wid = hop["waybill_id"]
    exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
            (wid, hop.get("waybill_title") or wid, "未知", _now()),
        )


def _sync_projection(conn, waybill_id: str, now: str) -> None:
    """按正式时间线末点重算投影；投影未变化则不再追加状态证明。"""
    row = timeline_svc.latest_primary(conn, waybill_id)
    if row is None:
        return
    proj = map_status(row["event_code"])
    current = conn.execute(
        "SELECT status_proj FROM waybills WHERE id=?", (waybill_id,)
    ).fetchone()
    if current is not None and current["status_proj"] == proj:
        return
    conn.execute(
        "UPDATE waybills SET status_proj=? WHERE id=?", (proj, waybill_id)
    )
    conn.execute(
        """
        INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at)
        VALUES (?,?,?,?,?)
        """,
        (waybill_id, row["id"], row["event_code"], proj, now),
    )


def _save_run(conn, report: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO ingest_batches(id, cutoff_ts, created_at, report_json)
        VALUES (?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          cutoff_ts=excluded.cutoff_ts,
          report_json=excluded.report_json
        """,
        (
            report["batch_id"],
            report["cutoff_ts"],
            now,
            json.dumps(report, ensure_ascii=False),
        ),
    )
    conn.execute(
        """
        INSERT INTO ingest_runs(
          run_id, batch_id, attempt, cutoff_ts, input_count, written, skipped,
          skip_reasons_json, formal_points_after, receipts_total, report_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            report["run_id"],
            report["batch_id"],
            report["attempt"],
            report["cutoff_ts"],
            report["input"],
            report["written"],
            report["skipped"],
            json.dumps(report["skip_reasons"], ensure_ascii=False),
            report["formal_points_after"],
            report["receipts_total"],
            json.dumps(report, ensure_ascii=False),
            now,
        ),
    )


def _decode_run(row) -> dict:
    data = dict(row)
    try:
        data["skip_reasons"] = json.loads(data.pop("skip_reasons_json"))
    except (TypeError, ValueError):
        data["skip_reasons"] = {}
    try:
        data["report"] = json.loads(data.pop("report_json"))
    except (TypeError, ValueError):
        data["report"] = {}
    return data


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"R-{stamp}-{uuid.uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
