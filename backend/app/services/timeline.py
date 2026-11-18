def list_primary(conn, waybill_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_primary(conn, waybill_id: str) -> dict | None:
    """正式时间线末点：投影只认它。"""
    row = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts DESC, id DESC LIMIT 1
        """,
        (waybill_id,),
    ).fetchone()
    return dict(row) if row else None
