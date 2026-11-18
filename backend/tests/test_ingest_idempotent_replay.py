import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "seed_hops.json").write_text(
        (BACKEND.parent / "fixtures" / "seed_hops.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    batch = json.loads(
        (BACKEND.parent / "fixtures" / "ingest_batch.json").read_text(encoding="utf-8")
    )
    (fixtures / "ingest_batch.json").write_text(
        json.dumps(batch, ensure_ascii=False), encoding="utf-8"
    )

    for name, value in {
        "DATA_DIR": str(data_dir),
        "FIXTURES_DIR": str(fixtures),
    }.items():
        monkeypatch.setenv(name, value)
    # 重新加载依赖环境变量的模块（含 app.main，避免复用上一个用例的 app/db）
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        sys.modules.pop(mod, None)
    from app import seed

    seed.init_db()
    from app.main import app

    with TestClient(app) as c:
        yield c


def _formal_count(client, waybill_id):
    data = client.get(f"/api/waybills/{waybill_id}/timeline").json()
    return len(data["hops"])


def test_replay_same_fixture_twice_is_idempotent(client):
    before_1001 = _formal_count(client, "WB-1001")
    before_1002 = _formal_count(client, "WB-1002")
    before_proofs = client.get("/api/status-proofs").json()

    first = client.post("/api/ingest/run").json()
    assert first["ok"] is True
    assert first["written"] == 2
    assert first["skipped"] == 0

    after_1001 = _formal_count(client, "WB-1001")
    after_1002 = _formal_count(client, "WB-1002")
    assert after_1001 == before_1001 + 1
    assert after_1002 == before_1002 + 1
    proofs_after_first = client.get("/api/status-proofs").json()
    assert len(proofs_after_first) == len(before_proofs) + 2

    second = client.post("/api/ingest/run").json()
    assert second["ok"] is True
    assert second["written"] == 0
    assert second["skipped"] == 2
    assert second["idempotent_noop"] is True
    assert second["skip_reasons"] == {"receipt_exists": 2}

    # 正式点数不变，投影也不再追加
    assert _formal_count(client, "WB-1001") == after_1001
    assert _formal_count(client, "WB-1002") == after_1002
    assert len(client.get("/api/status-proofs").json()) == len(proofs_after_first)
    assert second["formal_points_after"] == first["formal_points_after"]
    assert second["receipts_total"] == first["receipts_total"]


def test_replay_proofs_are_queryable_by_batch(client):
    client.post("/api/ingest/run")
    client.post("/api/ingest/run")

    data = client.get("/api/ingest/proofs?batch_id=B-DEMO-01").json()
    assert data["ok"] is True
    assert len(data["runs"]) == 2
    assert data["runs"][0]["written"] == 2
    assert data["runs"][1]["skipped"] == 2
    assert data["summary"] == {"proofs": 4, "writes": 2, "skips": 2}

    writes = [p for p in data["proofs"] if p["action"] == "write"]
    skips = [p for p in data["proofs"] if p["action"] == "skip"]
    assert len(writes) == 2 and len(skips) == 2
    for proof in writes:
        assert proof["hop_id"] == "H-5" or proof["hop_id"] == "H-6"
        assert len(proof["business_key"].split("|")) == 4
    # 跳过证明指向同一条正式点，不产生新点
    assert {s["hop_id"] for s in skips} == {w["hop_id"] for w in writes}

    only_skips = client.get(
        "/api/ingest/proofs?batch_id=B-DEMO-01&action=skip"
    ).json()
    assert only_skips["summary"]["skips"] == 2


def test_receipt_missing_formal_point_fails_closed(client):
    first = client.post("/api/ingest/run").json()
    assert first["ok"] is True

    from app.db import connect

    conn = connect()
    try:
        conn.execute("DELETE FROM hops WHERE id='H-5'")
        conn.commit()
    finally:
        conn.close()

    second = client.post("/api/ingest/run").json()
    assert second["ok"] is False
    assert "不存在" in second["error"]
    # 失败闭：事务回滚，回执和证明都不变
    proofs = client.get("/api/ingest/proofs?batch_id=B-DEMO-01").json()
    assert proofs["summary"]["proofs"] == 2


def test_missing_business_key_field_fails_closed(client):
    bad = {
        "batch_id": "B-BAD",
        "hops": [
            {
                "id": "H-X",
                "waybill_id": "WB-1001",
                "event_ts": "",
                "location_code": "HZ-STATION",
                "event_code": "IN_TRANSIT",
            }
        ],
    }
    result = client.post("/api/ingest/run", json=bad).json()
    assert result["ok"] is False
    assert "event_ts" in result["error"]
    assert client.get("/api/ingest/proofs?batch_id=B-BAD").status_code == 200
    assert client.get("/api/ingest/proofs?batch_id=B-BAD").json()["summary"]["proofs"] == 0


def test_duplicate_key_inside_one_batch_is_skipped(client):
    """同一批次内重复业务键：只落一点，第二个动作记为 skip/duplicate_in_batch。"""
    hop = {
        "id": "H-DUP-A",
        "waybill_id": "WB-1001",
        "event_ts": "2026-11-17T21:00:00Z",
        "location_code": "HZ-STATION",
        "event_code": "DELIVERED",
        "seq": 7,
    }
    duplicate = dict(hop, id="H-DUP-B")
    result = client.post(
        "/api/ingest/run", json={"batch_id": "B-DUP", "hops": [hop, duplicate]}
    ).json()
    assert result["ok"] is True
    assert result["written"] == 1
    assert result["skipped"] == 1
    assert result["skip_reasons"] == {"duplicate_in_batch": 1}

    timeline = client.get("/api/waybills/WB-1001/timeline").json()["hops"]
    ids = [h["id"] for h in timeline]
    assert ids.count("H-DUP-A") == 1
    assert "H-DUP-B" not in ids

    proofs = client.get("/api/ingest/proofs?batch_id=B-DUP").json()["proofs"]
    assert [(p["action"], p["hop_id"]) for p in proofs] == [
        ("write", "H-DUP-A"),
        ("skip", "H-DUP-A"),
    ]


def test_formal_point_without_receipt_is_backfilled_and_skipped(client):
    """正式线已有点、回执缺失：回填回执后跳过，不得再插一点。"""
    from app.db import connect

    conn = connect()
    try:
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) "
            "VALUES ('H-LEGACY','WB-1002','2026-11-17T11:00:00Z','SH-LOCAL','IN_TRANSIT',9,'primary')"
        )
        conn.commit()
    finally:
        conn.close()
    assert _formal_count(client, "WB-1002") == 2

    payload = {
        "batch_id": "B-LEGACY",
        "hops": [
            {
                "id": "H-NEW",
                "waybill_id": "WB-1002",
                "event_ts": "2026-11-17T11:00:00Z",
                "location_code": "SH-LOCAL",
                "event_code": "IN_TRANSIT",
                "seq": 9,
            }
        ],
    }
    result = client.post("/api/ingest/run", json=payload).json()
    assert result["ok"] is True
    assert result["written"] == 0
    assert result["skipped"] == 1
    assert result["skip_reasons"] == {"formal_exists_receipt_backfilled": 1}

    timeline = client.get("/api/waybills/WB-1002/timeline").json()["hops"]
    ids = [h["id"] for h in timeline]
    assert "H-NEW" not in ids
    assert ids.count("H-LEGACY") == 1

    proofs = client.get("/api/ingest/proofs?batch_id=B-LEGACY").json()["proofs"]
    assert proofs[0]["action"] == "skip"
    assert proofs[0]["hop_id"] == "H-LEGACY"
    receipt = client.get(
        "/api/ingest/receipts?business_key=WB-1002|2026-11-17T11:00:00Z|SH-LOCAL|IN_TRANSIT"
    ).json()[0]
    assert receipt["hop_id"] == "H-LEGACY"
    assert receipt["source"] == "formal_backfill"
