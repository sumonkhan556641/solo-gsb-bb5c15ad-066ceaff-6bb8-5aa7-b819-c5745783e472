"""原料批号待检锁定与出货资格版本联动测试。

覆盖：
- 按批号锁定/解除并填写原因；当前状态与历次变更查询；
- 未知批号、空原因拒绝；重复提交相同状态不产生新变更；
- 锁定不改变过敏原裁决；直接使用该批号的批次出货资格版本递增；
- 同线后续未直接使用该批号的批次不因残留传递连带锁定；
- 新登记批次按当前锁定状态确定资格；更正用料改变资格时递增版本并联动核验单；
- 核验单逐批快照记录出货资格版本；确认时同时核对裁决版本、出货资格版本与当前锁定状态；
- 已确认单据所含批次资格版本变化后转待复核，重新核验后方可再确认，历次确认快照保留。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def set_lock(client, lot_no, lock_status, reason):
    return client.put(
        f"/api/material-lots/{lot_no}/lock",
        json={"lock_status": lock_status, "reason": reason},
    )


# ---------------------------------------------------------------- 锁定/解除


def test_lock_and_unlock_records_status_and_history(client):
    add_lot(client, "RM-X", "花生酱", ["peanut"])

    resp = set_lock(client, "RM-X", "LOCKED", "复检风险，待检")
    assert resp.status_code == 200
    body = resp.json()
    assert body["lock_status"] == "LOCKED"
    assert body["changed"] is True
    assert body["lock_reason"] == "复检风险，待检"
    assert body["locked_at"] is not None

    # 原料批号详情与列表都带当前锁定状态
    assert client.get("/api/material-lots/RM-X").json()["lock_status"] == "LOCKED"
    assert client.get("/api/material-lots").json()["material_lots"][0]["lock_status"] == "LOCKED"

    # 重复提交相同状态不产生新变更
    resp = set_lock(client, "RM-X", "LOCKED", "再次提交同一状态")
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    assert resp.json()["eligibility_changes"] == []

    # 解除
    resp = set_lock(client, "RM-X", "UNLOCKED", "复检合格")
    assert resp.status_code == 200
    body = resp.json()
    assert body["lock_status"] == "UNLOCKED"
    assert body["changed"] is True
    assert body["locked_at"] is None

    hist = client.get("/api/material-lots/RM-X/lock-history").json()
    assert hist["lock_status"] == "UNLOCKED"
    assert [e["lock_status"] for e in hist["history"]] == ["LOCKED", "UNLOCKED"]
    assert [e["reason"] for e in hist["history"]] == ["复检风险，待检", "复检合格"]


def test_lock_rejects_unknown_lot_and_empty_reason(client):
    resp = set_lock(client, "NOPE", "LOCKED", "待检")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_NOT_FOUND"

    add_lot(client, "RM-X", "原料", [])
    # 空原因 / 纯空白原因
    resp = client.put(
        "/api/material-lots/RM-X/lock", json={"lock_status": "LOCKED", "reason": ""}
    )
    assert resp.status_code == 422  # pydantic min_length
    resp = set_lock(client, "RM-X", "LOCKED", "   ")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"
    # 非法状态
    resp = client.put(
        "/api/material-lots/RM-X/lock", json={"lock_status": "MAYBE", "reason": "x"}
    )
    assert resp.status_code == 422
    # 历史查询未知批号
    assert client.get("/api/material-lots/NOPE/lock-history").status_code == 404


# ---------------------------------------------------------------- 资格版本


def test_locked_lot_blocks_direct_batches_but_not_residual_downstream(client):
    add_lot(client, "RM-X", "花生酱", ["peanut"])
    b1 = add_batch(client, "B1", lots=["RM-X"], declared=["peanut"])       # RELEASE
    b2 = add_batch(client, "B2", lots=[], declared=[], cleaned=["peanut"])  # 未直接使用

    assert b1["shipping_eligibility"]["eligible"] is True
    assert b1["shipping_eligibility"]["version"] == 1

    resp = set_lock(client, "RM-X", "LOCKED", "待检")
    assert resp.status_code == 200
    changed = {c["batch_no"]: c for c in resp.json()["eligibility_changes"]}
    assert list(changed) == ["B1"]
    assert changed["B1"]["version"] == 2 and changed["B1"]["eligible"] is False
    assert changed["B1"]["locked_lots"] == ["RM-X"]

    # 直接使用 B1：过敏原裁决保持 RELEASE、裁决版本不变，但出货资格递增且不可出货
    b1d = client.get("/api/batches/B1").json()
    assert b1d["adjudication"]["version"] == 1
    assert b1d["adjudication"]["disposition"] == "RELEASE"
    assert b1d["shipping_eligibility"]["version"] == 2
    assert b1d["shipping_eligibility"]["eligible"] is False
    assert b1d["shipping_eligibility"]["locked_lots"] == ["RM-X"]
    assert any(r["code"] == "MATERIAL_LOT_LOCKED" for r in b1d["shipping_eligibility"]["reasons"])

    # 同线后续 B2 未直接使用该批号：残留传递不连带，资格仍 v1/可出货
    b2d = client.get("/api/batches/B2").json()
    assert b2d["shipping_eligibility"]["version"] == 1
    assert b2d["shipping_eligibility"]["eligible"] is True
    assert b2d["adjudication"]["version"] == 1  # 过敏原裁决完全不变

    # 逐批出货资格查询：当前 + 历次版本
    elig = client.get("/api/batches/B1/eligibility").json()
    assert elig["current"]["version"] == 2 and elig["current"]["eligible"] is False
    assert [v["version"] for v in elig["history"]] == [1, 2]
    assert [v["trigger"] for v in elig["history"]] == ["REGISTER", "LOT_LOCK"]
    assert client.get("/api/batches/NOPE/eligibility").status_code == 404


def test_new_batch_takes_current_lock_state_at_registration(client):
    add_lot(client, "RM-X", "花生酱", ["peanut"])
    set_lock(client, "RM-X", "LOCKED", "待检")
    # 登记时即按当前锁定状态确定资格：v1 不可出货，即使裁决 RELEASE
    b = add_batch(client, "B1", lots=["RM-X"], declared=["peanut"])
    assert b["adjudication"]["disposition"] == "RELEASE"
    assert b["shipping_eligibility"]["version"] == 1
    assert b["shipping_eligibility"]["eligible"] is False
    assert b["shipping_eligibility"]["locked_lots"] == ["RM-X"]


def test_lock_affects_direct_batches_on_every_line(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "X1", line_id="LX", lots=["RM-M"], declared=["milk"])
    add_batch(client, "Y1", line_id="LY", lots=["RM-M"], declared=["milk"])
    resp = set_lock(client, "RM-M", "LOCKED", "待检")
    changed = sorted(c["batch_no"] for c in resp.json()["eligibility_changes"])
    assert changed == ["X1", "Y1"]


def test_lot_allergen_correction_does_not_bump_eligibility_version(client):
    add_lot(client, "RM-X", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-X"], declared=["peanut"])  # RELEASE
    client.put("/api/material-lots/RM-X", json={"allergens": ["peanut", "sesame"]})
    # 过敏原更正只递增裁决版本，不递增出货资格版本
    b = client.get("/api/batches/B1").json()
    assert b["adjudication"]["version"] == 2
    assert b["shipping_eligibility"]["version"] == 1


def test_correct_batch_materials_bumps_eligibility_when_state_changes(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])  # RELEASE
    set_lock(client, "RM-M", "LOCKED", "待检")
    assert client.get("/api/batches/B1").json()["shipping_eligibility"]["version"] == 2

    # 更正用料移除锁定批号 -> 资格恢复并递增版本
    resp = client.put("/api/batches/B1", json={"material_lots": []})
    assert resp.status_code == 200
    change = resp.json()["eligibility_change"]
    assert change["version"] == 3 and change["eligible"] is True
    assert change["locked_lots"] == []

    # 仅更正声明（用料不变）不产生新资格版本（标志同步、版本不递增）
    resp = client.put("/api/batches/B1", json={"declared_allergens": []})
    assert resp.json()["eligibility_change"] is None
    assert client.get("/api/batches/B1").json()["shipping_eligibility"]["version"] == 3

    # 更正用料重新加入锁定批号 -> 资格再次不可出货并递增
    resp = client.put("/api/batches/B1", json={"material_lots": ["RM-M"]})
    change = resp.json()["eligibility_change"]
    assert change["version"] == 4 and change["eligible"] is False
    assert change["locked_lots"] == ["RM-M"]

    # 在锁定状态下先声明更正使批次 HOLD，再解除批号锁定：
    # 用料资格依据变化（版本递增），但批次仍因 HOLD 不可出货；
    # 之后补全声明恢复 RELEASE 时不再产生资格新版本（依据未变，仅标志同步）。
    client.put("/api/batches/B1", json={"declared_allergens": []})  # HOLD
    resp = set_lock(client, "RM-M", "UNLOCKED", "合格")
    assert resp.json()["eligibility_changes"][0]["version"] == 5
    assert resp.json()["eligibility_changes"][0]["eligible"] is False
    resp = client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    assert resp.json()["eligibility_change"] is None
    se = client.get("/api/batches/B1").json()["shipping_eligibility"]
    assert se["version"] == 5 and se["eligible"] is True


def test_correct_batch_to_unknown_lot_rejected(client):
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    resp = client.put("/api/batches/B1", json={"material_lots": ["NOPE"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_MATERIAL_LOT"


# ---------------------------------------------------------------- 核验单联动


def _create(client, no, batches):
    return client.post(
        "/api/verifications", json={"verification_no": no, "batch_nos": batches}
    )


def test_confirm_blocked_while_locked_even_if_adjudication_release(client):
    add_lot(client, "RM-X", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-X"], declared=["peanut"])  # RELEASE
    _create(client, "V1", ["B1"])

    set_lock(client, "RM-X", "LOCKED", "待检")
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    codes = sorted(d["reason_code"] for d in details)
    assert codes == ["MATERIAL_LOT_LOCKED", "STALE_ELIGIBILITY_SNAPSHOT"]
    locked = next(d for d in details if d["reason_code"] == "MATERIAL_LOT_LOCKED")
    assert locked["locked_lots"] == ["RM-X"]
    stale = next(d for d in details if d["reason_code"] == "STALE_ELIGIBILITY_SNAPSHOT")
    assert stale["snapshot_eligibility_version"] == 1
    assert stale["current_eligibility_version"] == 2
    assert stale["change_reasons"][0]["trigger"] == "LOT_LOCK"
    # 整单不确认、不写确认记录
    doc = client.get("/api/verifications/V1").json()
    assert doc["status"] == "DRAFT" and doc["confirmations"] == []


def test_snapshot_records_eligibility_version_and_confirm_checks_all(client):
    add_lot(client, "RM-X", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-X"], declared=["peanut"])
    body = _create(client, "V1", ["B1"]).json()
    snap = body["batches"][0]["snapshot"]["shipping_eligibility"]
    assert snap == {"version": 1, "eligible": True, "locked_lots": []}
    assert client.post("/api/verifications/V1/confirm").status_code == 200
    confirmed = client.get("/api/verifications/V1").json()
    cb = confirmed["confirmations"][0]["batches"][0]
    assert cb["shipping_eligibility"]["version"] == 1
    assert cb["shipping_eligibility"]["eligible"] is True


def test_confirmed_document_goes_pending_review_on_eligibility_change(client):
    add_lot(client, "RM-X", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-X"], declared=["peanut"])
    _create(client, "V1", ["B1"])
    client.post("/api/verifications/V1/confirm")

    # 锁定：裁决版本不变，仅出货资格版本变化，已确认单仍转待复核
    set_lock(client, "RM-X", "LOCKED", "待检")
    doc = client.get("/api/verifications/V1").json()
    assert doc["status"] == "PENDING_REVIEW"
    entry = doc["batches"][0]
    assert entry["snapshot"]["version"] == entry["current"]["version"] == 1  # 裁决版本未变
    assert entry["snapshot"]["shipping_eligibility"]["version"] == 1
    assert entry["current"]["shipping_eligibility"]["version"] == 2
    assert entry["current"]["shipping_eligibility"]["eligible"] is False
    assert entry["changed"] is True
    assert [r["to_version"] for r in entry["eligibility_change_reasons"]] == [2]
    # 历次确认快照仍保留
    assert doc["confirmations"][0]["batches"][0]["shipping_eligibility"]["version"] == 1

    # 直接确认被拒（锁定 + 快照陈旧）
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    codes = sorted(d["reason_code"] for d in resp.json()["error"]["details"])
    assert codes == ["MATERIAL_LOT_LOCKED", "STALE_ELIGIBILITY_SNAPSHOT"]

    # 解锁 -> 重新核验 -> 再次确认；历史确认继续保留
    set_lock(client, "RM-X", "UNLOCKED", "合格")
    assert client.post("/api/verifications/V1/reverify").json()["status"] == "DRAFT"
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["status"] == "CONFIRMED" and len(doc["confirmations"]) == 2
    assert doc["confirmations"][0]["batches"][0]["shipping_eligibility"]["version"] == 1
    assert doc["confirmations"][1]["batches"][0]["shipping_eligibility"]["version"] == 3


def test_hold_batch_with_locked_lot_returns_all_reasons(client):
    add_lot(client, "RM-X", "花生酱", ["peanut"])
    # B1 未声明 peanut -> HOLD；批号同时锁定
    add_batch(client, "B1", lots=["RM-X"], declared=[])
    set_lock(client, "RM-X", "LOCKED", "待检")
    _create(client, "V1", ["B1"])
    client.post("/api/verifications/V1/reverify")  # 刷新到最新两版本
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    codes = sorted(d["reason_code"] for d in resp.json()["error"]["details"])
    assert codes == ["BATCH_ON_HOLD", "MATERIAL_LOT_LOCKED"]
