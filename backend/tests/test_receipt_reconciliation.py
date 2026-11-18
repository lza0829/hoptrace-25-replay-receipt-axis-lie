"""回执对照核证：跳过前必须核实「回执 → 正式点」三方对板。

硬约束（任何一条违反都必须整笔失败闭，不得假跳过）：

* 回执指向撤回点，禁止 skipped；
* 回执 hop_id 与业务键正式点不一致，禁止成功；
* 核证页（只读接口）显示的对照结论与实际对照失败不得矛盾。

夹具 ``fixtures/receipt_check_cases.json`` 声明「回执指错点」与「回执正确」
两类场景，由本文件在本地 SQLite 内执行篡改后重放同业务键批次。
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for name in ("seed_hops.json", "ingest_batch.json", "receipt_check_cases.json"):
        (fixtures / name).write_text(
            (ROOT / "fixtures" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("FIXTURES_DIR", str(fixtures))
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        sys.modules.pop(mod, None)
    from app import seed

    seed.init_db()
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def cases():
    return json.loads(
        (ROOT / "fixtures" / "receipt_check_cases.json").read_text(encoding="utf-8")
    )


def _case(cases, case_id):
    return next(c for c in cases["cases"] if c["id"] == case_id)


def _apply_mutations(conn, mutations):
    """在本地 SQLite 内执行夹具声明的篡改（参数化，禁止拼接裸 SQL）。"""
    for m in mutations:
        where = " AND ".join(f"{k}=?" for k in m["where"])
        params = list(m["where"].values())
        if m["op"] == "delete":
            conn.execute(f"DELETE FROM {m['table']} WHERE {where}", params)
        elif m["op"] == "update":
            sets = ", ".join(f"{k}=?" for k in m["set"])
            conn.execute(
                f"UPDATE {m['table']} SET {sets} WHERE {where}",
                list(m["set"].values()) + params,
            )
        else:
            raise AssertionError(f"未知篡改操作：{m['op']}")
    conn.commit()


def _replay_batch(client, cases, batch_id):
    return client.post(
        "/api/ingest/run",
        json={"batch_id": batch_id, "hops": cases["replay_hops"]},
    ).json()


def _seed_success_receipts(client):
    first = client.post("/api/ingest/run").json()
    assert first["ok"] is True
    assert first["written"] == 2
    return first


# ---------- 夹具：「回执指错点」应失败 ----------

@pytest.mark.parametrize(
    "case_id",
    [
        "receipt-points-missing-formal",
        "receipt-points-retracted-formal",
        "receipt-hop-id-key-mismatch",
    ],
)
def test_wrong_receipt_pointer_fails_closed(client, cases, case_id):
    _seed_success_receipts(client)
    case = _case(cases, case_id)

    from app.db import connect

    conn = connect()
    try:
        _apply_mutations(conn, case["mutations"])
    finally:
        conn.close()

    result = _replay_batch(client, cases, case["batch_id"])
    assert result["ok"] is False, case_id
    assert case["expect_error_contains"] in result["error"], case_id

    # 失败闭：该失败批次不得留下任何 skip / write 证明，更没有对照留痕
    proofs = client.get(
        f"/api/ingest/proofs?batch_id={case['batch_id']}"
    ).json()
    assert proofs["summary"]["proofs"] == 0, case_id
    assert proofs["summary"]["skips"] == 0, case_id

    checks = client.get(
        f"/api/ingest/receipt-checks?batch_id={case['batch_id']}"
    ).json()
    assert checks["ok"] is True
    assert checks["summary"] == {"checks": 0, "matched": 0, "mismatch": 0}, case_id

    # 核证页协同：现场只读复核必须同样判定 mismatch，禁止显示跳过成功
    live = client.get(
        "/api/ingest/receipt-checks",
        params={"business_key": case["tampered_business_key"]},
    ).json()
    assert live["ok"] is True
    assert live["live_check"]["verdict"] == "mismatch", case_id
    assert live["live_check"]["live"] is True


def test_retracted_pointer_never_skipped(client, cases):
    """硬约束一：回执指向撤回点，禁止 skipped。"""
    _seed_success_receipts(client)
    case = _case(cases, "receipt-points-retracted-formal")

    from app.db import connect

    conn = connect()
    try:
        _apply_mutations(conn, case["mutations"])
    finally:
        conn.close()

    result = _replay_batch(client, cases, case["batch_id"])
    assert result["ok"] is False
    assert "撤回" in result["error"]

    # 整笔回滚：两个业务键都不得产生 skip，正式点数不变
    proofs = client.get(
        f"/api/ingest/proofs?batch_id={case['batch_id']}"
    ).json()
    assert proofs["summary"]["skips"] == 0
    timeline = client.get("/api/waybills/WB-1001/timeline").json()["hops"]
    assert all(h["id"] != "H-5" for h in timeline)


def test_hop_id_business_key_mismatch_never_succeeds(client, cases):
    """硬约束二：回执 hop_id 与业务键正式点不一致，禁止成功。"""
    _seed_success_receipts(client)
    case = _case(cases, "receipt-hop-id-key-mismatch")

    from app.db import connect

    conn = connect()
    try:
        _apply_mutations(conn, case["mutations"])
        row = conn.execute(
            "SELECT hop_id FROM ingest_receipts WHERE business_key=?",
            (case["tampered_business_key"],),
        ).fetchone()
        assert row["hop_id"] == "H-6"
        # H-6 的业务键与 H-5 回执的业务键不同：典型的 hop_id/业务键不对板
        h6 = conn.execute(
            "SELECT waybill_id, event_ts, location_code, event_code FROM hops WHERE id='H-6'"
        ).fetchone()
        assert h6["waybill_id"] == "WB-1002"
    finally:
        conn.close()

    result = _replay_batch(client, cases, case["batch_id"])
    assert result["ok"] is False
    assert "不一致" in result["error"]
    # 未被篡改的另一条回执也不得被跳过——整笔失败闭
    proofs = client.get(
        f"/api/ingest/proofs?batch_id={case['batch_id']}"
    ).json()
    assert proofs["summary"]["proofs"] == 0


# ---------- 夹具：「回执正确」应跳过 ----------

def test_correct_receipt_skips_with_matched_check(client, cases):
    """回执正确：正式点存在、未撤回、业务键一致 ⇒ 跳过且留 matched 核证。"""
    _seed_success_receipts(client)
    case = _case(cases, "receipt-correct-skips")

    result = _replay_batch(client, cases, case["batch_id"])
    assert result["ok"] is True
    assert result["written"] == 0
    assert result["skipped"] == 2
    assert result["idempotent_noop"] is True
    assert result["skip_reasons"] == {"receipt_exists": 2}

    proofs = client.get(
        f"/api/ingest/proofs?batch_id={case['batch_id']}"
    ).json()
    assert proofs["summary"] == {"proofs": 2, "writes": 0, "skips": 2}
    skip_ids = {p["hop_id"] for p in proofs["proofs"]}
    assert skip_ids == {"H-5", "H-6"}

    checks = client.get(
        f"/api/ingest/receipt-checks?batch_id={case['batch_id']}"
    ).json()
    assert checks["summary"] == {"checks": 2, "matched": 2, "mismatch": 0}
    # 每条对照留痕都与当条 skip 证明互链
    proof_ids = {p["id"] for p in proofs["proofs"]}
    for chk in checks["checks"]:
        assert chk["verdict"] == "matched"
        assert chk["key_match"] == 1
        assert chk["formal_retracted"] == 0
        assert chk["formal_channel"] == "primary"
        assert chk["proof_id"] in proof_ids

    # 现场复核同样是 matched，证明页与核证页一致
    for key in cases["targets"].values():
        live = client.get(
            "/api/ingest/receipt-checks", params={"business_key": key}
        ).json()
        assert live["receipt"] is not None
        assert live["live_check"]["verdict"] == "matched"
        assert live["live_check"]["receipt_hop_id"] == live["receipt"]["hop_id"]


# ---------- 核证只读接口 ----------

def test_receipt_checks_endpoint_filters_and_live_recheck(client, cases):
    _seed_success_receipts(client)
    ok_case = _case(cases, "receipt-correct-skips")
    _replay_batch(client, cases, ok_case["batch_id"])

    # verdict 过滤
    matched = client.get(
        "/api/ingest/receipt-checks", params={"verdict": "matched"}
    ).json()
    assert matched["summary"]["matched"] >= 2
    assert matched["summary"]["mismatch"] == 0

    bad_verdict = client.get(
        "/api/ingest/receipt-checks", params={"verdict": "bogus"}
    )
    assert bad_verdict.status_code == 400

    # 未知业务键：无回执、无现场结论
    unknown = client.get(
        "/api/ingest/receipt-checks",
        params={"business_key": "NOPE|NOPE|NOPE|NOPE"},
    ).json()
    assert unknown["ok"] is True
    assert unknown["receipt"] is None
    assert unknown["live_check"] is None
    assert unknown["checks"] == []

    # 撤回答场复核：留痕历史是 matched，但现场复核必须翻成 mismatch
    from app.db import connect

    conn = connect()
    try:
        conn.execute("UPDATE hops SET retracted=1 WHERE id='H-5'")
        conn.commit()
    finally:
        conn.close()

    h5 = client.get(
        "/api/ingest/receipt-checks",
        params={"business_key": cases["targets"]["H5_key"]},
    ).json()
    assert h5["live_check"]["verdict"] == "mismatch"
    assert h5["live_check"]["reason"] == "formal_point_retracted"
    # 历史留痕不被篡改（只追加），仍全部 matched
    assert all(c["verdict"] == "matched" for c in h5["checks"])


def test_failed_run_rolls_back_checks_and_proofs(client, cases):
    """失败闭原子性：对撞失败时，同批次早先写入的 skip 与对照留痕一并回滚。"""
    _seed_success_receipts(client)
    case = _case(cases, "receipt-points-missing-formal")

    from app.db import connect

    conn = connect()
    try:
        _apply_mutations(conn, case["mutations"])
    finally:
        conn.close()

    result = _replay_batch(client, cases, case["batch_id"])
    assert result["ok"] is False

    # 该失败轮次在 runs / batches 中也不得留痕
    runs = client.get(f"/api/ingest/runs?batch_id={case['batch_id']}").json()
    assert runs == []
    checks = client.get(
        f"/api/ingest/receipt-checks?batch_id={case['batch_id']}"
    ).json()
    assert checks["checks"] == []

    # 原始成功批次的证明与留痕不受影响
    demo = client.get("/api/ingest/proofs?batch_id=B-DEMO-01").json()
    assert demo["summary"]["writes"] == 2
