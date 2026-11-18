import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status_proj TEXT NOT NULL DEFAULT '未知',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hops (
  id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL REFERENCES waybills(id),
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  channel TEXT NOT NULL DEFAULT 'primary',
  retracted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(waybill_id, event_ts, location_code, event_code)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  cutoff_ts TEXT,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS status_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  last_hop_id TEXT,
  mapped_code TEXT,
  proj_value TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 装载回执：按稳定业务键记录正式线落账结果，是幂等判断的唯一依据。
CREATE TABLE IF NOT EXISTS ingest_receipts (
  business_key TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  hop_id TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'ingest',
  first_batch_id TEXT NOT NULL,
  first_run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(hop_id)
);

CREATE INDEX IF NOT EXISTS idx_ingest_receipts_waybill
  ON ingest_receipts(waybill_id, event_ts);

-- 幂等证明：每个业务键每次装载的写入 / 跳过动作，只追加。
CREATE TABLE IF NOT EXISTS ingest_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  business_key TEXT NOT NULL,
  waybill_id TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('write', 'skip')),
  hop_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingest_proofs_batch
  ON ingest_proofs(batch_id, id);

CREATE INDEX IF NOT EXISTS idx_ingest_proofs_key
  ON ingest_proofs(business_key, id);

-- 每轮装载的汇总：同一批次可回放多轮，attempt 递增。
CREATE TABLE IF NOT EXISTS ingest_runs (
  run_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES ingest_batches(id),
  attempt INTEGER NOT NULL,
  cutoff_ts TEXT,
  input_count INTEGER NOT NULL DEFAULT 0,
  written INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  skip_reasons_json TEXT NOT NULL DEFAULT '{}',
  formal_points_after INTEGER NOT NULL DEFAULT 0,
  receipts_total INTEGER NOT NULL DEFAULT 0,
  report_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(batch_id, attempt)
);
"""

EVENT_STATUS = {
    "PICKED": "已揽收",
    "IN_TRANSIT": "运输中",
    "ARRIVED_HUB": "到站",
    "OUT_FOR_DELIVERY": "派送中",
    "DELIVERED": "已签收",
    "EXCEPTION": "异常",
}


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    n = conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    # 历史正式点补回执：保证「正式点 ⇒ 回执」，避免下次装载重复插入。
    from app.services.ingest import backfill_receipts

    backfill_receipts(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hops.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for wb in data.get("waybills", []):
        conn.execute(
            "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
            (wb["id"], wb["title"], wb.get("status_proj", "未知"), wb["created_at"]),
        )
    for hop in data.get("hops", []):
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) VALUES (?,?,?,?,?,?,?)",
            (
                hop["id"],
                hop["waybill_id"],
                hop["event_ts"],
                hop["location_code"],
                hop["event_code"],
                hop.get("seq"),
                hop.get("channel", "primary"),
            ),
        )
    _refresh_projections(conn)


def _refresh_projections(conn):
    rows = conn.execute(
        """
        SELECT h.waybill_id, h.id, h.event_code
        FROM hops h
        JOIN (
          SELECT waybill_id, MAX(event_ts || '|' || id) AS mk
          FROM hops WHERE retracted=0 AND channel='primary'
          GROUP BY waybill_id
        ) t ON t.waybill_id=h.waybill_id AND (h.event_ts || '|' || h.id)=t.mk
        WHERE h.retracted=0 AND h.channel='primary'
        """
    ).fetchall()
    for r in rows:
        proj = EVENT_STATUS.get(r["event_code"], "未知")
        conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, r["waybill_id"]))
        conn.execute(
            "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,datetime('now'))",
            (r["waybill_id"], r["id"], r["event_code"], proj),
        )


def map_status(event_code: str) -> str:
    return EVENT_STATUS.get(event_code, "未知")
