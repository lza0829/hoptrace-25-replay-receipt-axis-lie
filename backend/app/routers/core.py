from fastapi import APIRouter, HTTPException

from app.db import connect
from app.modules import ingest_idempotent_replay as replay
from app.services import ingest as ingest_svc
from app.services import timeline as timeline_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "waybills": conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"],
            "hops_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='primary' AND retracted=0"
            ).fetchone()["c"],
            "hops_sidecar": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='sidecar'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
        }
    finally:
        conn.close()


@router.get("/waybills")
def list_waybills():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, status_proj, created_at FROM waybills ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/timeline")
def waybill_timeline(waybill_id: str):
    conn = connect()
    try:
        wb = conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        hops = timeline_svc.list_primary(conn, waybill_id)
        proof = conn.execute(
            "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
            (waybill_id,),
        ).fetchone()
        return {
            "waybill": dict(wb),
            "hops": hops,
            "proof": dict(proof) if proof else None,
        }
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sidecar")
def list_sidecar():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, event_ts, location_code, event_code, channel FROM hops WHERE channel='sidecar' ORDER BY event_ts"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(payload: dict | None = None):
    return ingest_svc.run_batch(payload)


@router.get("/ingest/receipts")
def ingest_receipts(business_key: str | None = None):
    return ingest_svc.list_receipts(business_key)


@router.get("/ingest/runs")
def ingest_runs(batch_id: str | None = None):
    return ingest_svc.list_runs(batch_id)


@router.get("/ingest/proofs")
def ingest_proofs(
    batch_id: str | None = None,
    run_id: str | None = None,
    action: str | None = None,
):
    """幂等证明只读接口：默认按批次查询，返回证明明细与轮次汇总。"""
    try:
        result = ingest_svc.query_proofs(batch_id=batch_id, run_id=run_id, action=action)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except replay.IdempotencyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "查询失败")
    return result


@router.get("/status-proofs")
def status_proofs(waybill_id: str | None = None):
    conn = connect()
    try:
        if waybill_id:
            rows = conn.execute(
                "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC",
                (waybill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM status_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
