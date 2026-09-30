#!/usr/bin/env python3
"""食品换线放行服务 —— 原料批号待检锁定与出货资格联动端到端示例（仅标准库）。

演示流程（独立产线 LLOCK，批次固定，可重复运行）：
  1. 登记原料批号 LK-RM-PEANUT（含 peanut）与批次 LKB001：声明 peanut -> 裁决 RELEASE
     同线后续批次 LKB002 不使用该批号（纯黑巧，清洁 peanut）-> RELEASE，用于演示“不连带”
  2. 按批号锁定 LK-RM-PEANUT（填写原因）：
       - LKB001 直接使用：裁决仍 RELEASE，但出货资格 v1->v2、不可出货
       - LKB002 未直接使用：资格不变（残留传递不连带）
  3. 重复提交相同状态：changed=false，不产生新变更
  4. 查询原料批号当前状态与历次锁定变更；查询批次逐批出货资格版本
  5. 创建核验单：锁定期间确认被拒（STALE_ELIGIBILITY_SNAPSHOT + MATERIAL_LOT_LOCKED）
  6. 解除锁定 -> LKB001 资格 v2->v3；重新核验后确认成功
  7. 已确认核验单再次锁定 -> 自动转 PENDING_REVIEW；解锁、重新核验、二次确认；历史保留
  8. 拒绝场景：未知批号、空原因、非法状态

用法：
    python examples/lot_lock_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/lot_lock_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LLOCK"
LOT = "LK-RM-PEANUT"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> None:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        se = body["shipping_eligibility"]
        print(
            f"  已登记批次 {batch_no} => 裁决 {adj['disposition']}（v{adj['version']}），"
            f"出货资格 {'可出货' if se['eligible'] else '不可出货'}（资格 v{se['version']}）"
        )
    elif status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记批次失败: {body}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))

    section("1. 准备数据：LKB001 直接使用批号；LKB002 同线后续但不直接使用")
    ensure_lot(LOT, "锁定示例花生酱", ["peanut"])
    ensure_batch("LKB001", {
        "batch_no": "LKB001", "line_id": LINE,
        "material_lots": [LOT],
        "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })
    ensure_batch("LKB002", {
        "batch_no": "LKB002", "line_id": LINE,
        "material_lots": [],
        "declared_allergens": [], "cleaned_allergens": ["peanut"],
    })

    section("2. 按批号待检锁定（裁决 RELEASE 不变，直接使用批次资格版本递增）")
    status, body = call("PUT", f"/api/material-lots/{LOT}/lock", {
        "lock_status": "LOCKED", "reason": f"抽检异常待检（{suffix}）",
    })
    show("锁定", status, body)
    assert status == 200 and body["changed"] is True
    assert [c["batch_no"] for c in body["eligibility_changes"]] == ["LKB001"], body
    assert body["eligibility_changes"][0]["eligible"] is False

    status, b1 = call("GET", "/api/batches/LKB001")
    print(f"  LKB001: 裁决 {b1['adjudication']['disposition']} v{b1['adjudication']['version']}，"
          f"出货资格可出货={b1['shipping_eligibility']['eligible']} "
          f"v{b1['shipping_eligibility']['version']}，"
          f"锁定批号={b1['shipping_eligibility']['locked_lots']}")
    assert b1["adjudication"]["disposition"] == "RELEASE"
    assert b1["shipping_eligibility"]["eligible"] is False

    status, b2 = call("GET", "/api/batches/LKB002")
    print(f"  LKB002（未直接使用）: 出货资格可出货={b2['shipping_eligibility']['eligible']} "
          f"v{b2['shipping_eligibility']['version']}（残留传递不连带）")
    assert b2["shipping_eligibility"]["eligible"] is True
    assert b2["shipping_eligibility"]["version"] == 1

    section("3. 重复提交相同状态：不产生新变更")
    status, body = call("PUT", f"/api/material-lots/{LOT}/lock", {
        "lock_status": "LOCKED", "reason": "重复提交相同状态",
    })
    show("再次锁定（预期 changed=false）", status, body)
    assert status == 200 and body["changed"] is False
    assert body["eligibility_changes"] == []

    section("4. 查询批号锁定状态/历次变更 与 逐批出货资格版本")
    status, hist = call("GET", f"/api/material-lots/{LOT}/lock-history")
    show("批号锁定历史", status, hist)
    assert status == 200 and hist["lock_status"] == "LOCKED"
    status, elig = call("GET", "/api/batches/LKB001/eligibility")
    show("LKB001 出货资格（当前 + 历次版本）", status, elig)
    assert [v["version"] for v in elig["history"]] == [1, 2]

    doc_no = f"LK-{suffix}-1"
    section(f"5. 创建核验单 {doc_no}：锁定期间确认应被整单拒绝（逐批原因）")
    status, _ = call("POST", "/api/verifications", {
        "verification_no": doc_no, "batch_nos": ["LKB001"],
    })
    assert status == 201
    status, body = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("确认（预期 422：资格快照陈旧 + 批号锁定）", status, body)
    assert status == 422
    codes = sorted(d["reason_code"] for d in body["error"]["details"])
    assert codes == ["MATERIAL_LOT_LOCKED", "STALE_ELIGIBILITY_SNAPSHOT"], body

    section("6. 解除锁定 -> 资格 v2->v3；重新核验后确认成功")
    status, body = call("PUT", f"/api/material-lots/{LOT}/lock", {
        "lock_status": "UNLOCKED", "reason": "复检合格，解除锁定",
    })
    show("解除锁定", status, body)
    assert status == 200 and body["changed"] is True
    assert body["eligibility_changes"][0]["version"] == 3

    status, body = call("POST", f"/api/verifications/{doc_no}/reverify")
    show("重新核验（快照资格刷新到 v3）", status, body)
    assert status == 200
    assert body["batches"][0]["snapshot"]["shipping_eligibility"]["version"] == 3
    status, body = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("确认（预期 200 CONFIRMED）", status, body)
    assert status == 200 and body["status"] == "CONFIRMED"

    section("7. 已确认单据：再次锁定 -> PENDING_REVIEW；解锁重核 -> 二次确认（历史保留）")
    call("PUT", f"/api/material-lots/{LOT}/lock", {
        "lock_status": "LOCKED", "reason": "供应商复核，再次待检",
    })
    status, doc = call("GET", f"/api/verifications/{doc_no}")
    show(f"{doc_no} 查询（预期 PENDING_REVIEW，裁决版本未变）", status, doc)
    assert doc["status"] == "PENDING_REVIEW"
    assert doc["batches"][0]["changed"] is True
    assert len(doc["confirmations"]) == 1  # 历史确认快照保留

    call("PUT", f"/api/material-lots/{LOT}/lock", {
        "lock_status": "UNLOCKED", "reason": "复核合格",
    })
    call("POST", f"/api/verifications/{doc_no}/reverify")
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    show("二次确认（预期 200，confirmations 保留两次）", status, doc)
    assert status == 200 and len(doc["confirmations"]) == 2
    versions = [c["batches"][0]["shipping_eligibility"]["version"] for c in doc["confirmations"]]
    assert versions == [3, 5], versions

    section("8. 拒绝场景：未知批号 / 空原因 / 非法状态")
    cases = [
        ("未知批号-锁定", "PUT", "/api/material-lots/NO-SUCH-LOT/lock",
         {"lock_status": "LOCKED", "reason": "x"}),
        ("空原因", "PUT", f"/api/material-lots/{LOT}/lock",
         {"lock_status": "LOCKED", "reason": ""}),
        ("非法状态", "PUT", f"/api/material-lots/{LOT}/lock",
         {"lock_status": "MAYBE", "reason": "x"}),
    ]
    for label, method, path, payload in cases:
        status, body = call(method, path, payload)
        code = body.get("error", {}).get("code", "(FastAPI 校验)")
        print(f"[{status}] {label}: {code}")

    section("完成")
    print(f"批号 {LOT} 当前已解锁；核验单 {doc_no} 已二次确认，历次确认快照均可在 GET 详情查看。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
