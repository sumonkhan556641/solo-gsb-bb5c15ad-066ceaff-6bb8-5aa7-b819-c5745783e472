"""核心业务：批次登记、过敏原传递与清洁裁决、更正重算。

裁决规则（按同一产线的先后顺序 seq 递增）：
  带入 = 上一批“实际可能含有的过敏原”
  残留 = 带入 - 本次清洁确认清除的过敏原        （只清除被确认覆盖的项目）
  本批 = 本批所用原料批号过敏原的并集
  实际 = 残留 ∪ 本批                            （上一批即使被扣留，其实际过敏原仍继续传递）
  未声明 = 实际 - 成品声明过敏原
  未声明非空 => 扣留(HOLD)；否则放行(RELEASE)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import allergens as al
from .db import Store, dumps, loads
from .errors import bad_request, conflict, not_found, unprocessable


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_allergen_list(values, field: str) -> list[str]:
    """去重、保序；校验所有过敏原标识，非法立即报错（外层事务回滚）。"""
    if values is None:
        return []
    if not isinstance(values, list):
        raise bad_request("INVALID_ALLERGEN", f"字段 {field} 必须是过敏原代码数组")
    bad = [v for v in values if not isinstance(v, str) or not al.is_valid(v)]
    if bad:
        raise bad_request(
            "INVALID_ALLERGEN", f"无效的过敏原标识: {', '.join(map(str, bad))}"
        )
    return list(dict.fromkeys(values))


def _normalize_str_list(values, field: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise bad_request("INVALID_PAYLOAD", f"字段 {field} 必须是非空字符串数组")
    return list(dict.fromkeys(v.strip() for v in values))


# ---------------------------------------------------------------- 出货资格

LOT_LOCK_STATUSES = ("LOCKED", "UNLOCKED")

_ELIGIBILITY_TRIGGER_LABELS = {
    "REGISTER": "登记批次时确定出货资格",
    "LOT_LOCK": "原料批号待检锁定",
    "LOT_UNLOCK": "原料批号解除锁定",
    "CORRECT_BATCH": "批次用料更正重定出货资格",
}


def _eligibility_trigger_label(trigger: str) -> str:
    return _ELIGIBILITY_TRIGGER_LABELS.get(trigger, trigger)


def _locked_lots_for_batch(conn, batch_no: str, lot_nos: list[str] | None = None) -> list[str]:
    """该批“直接使用”且当前处于待检锁定的原料批号（残留传递不算直接使用）。"""
    if lot_nos is None:
        rows = conn.execute(
            """SELECT bm.lot_no AS lot_no FROM batch_material bm
               JOIN material_lot ml ON ml.lot_no = bm.lot_no
               WHERE bm.batch_no = ? AND ml.lock_status = 'LOCKED'
               ORDER BY bm.lot_no""",
            (batch_no,),
        ).fetchall()
        return [r["lot_no"] for r in rows]
    if not lot_nos:
        return []
    placeholders = ",".join("?" * len(lot_nos))
    rows = conn.execute(
        f"""SELECT lot_no FROM material_lot
            WHERE lock_status = 'LOCKED' AND lot_no IN ({placeholders})""",
        lot_nos,
    ).fetchall()
    locked = {r["lot_no"] for r in rows}
    return [lot for lot in dict.fromkeys(lot_nos) if lot in locked]


def _eligibility_reasons(eligible: bool, locked_lots: list[str], disposition: str) -> list[dict[str, Any]]:
    """出货资格判定原因（锁定与裁决两条独立理由，可同时存在）。"""
    reasons: list[dict[str, Any]] = []
    if locked_lots:
        reasons.append(
            {
                "code": "MATERIAL_LOT_LOCKED",
                "locked_lots": locked_lots,
                "message": (
                    f"直接使用的原料批号 {', '.join(locked_lots)} 处于待检锁定状态；"
                    "即使过敏原裁决为 RELEASE，也不得通过出货核验。"
                ),
            }
        )
    if disposition != "RELEASE":
        reasons.append(
            {
                "code": "ADJUDICATION_ON_HOLD",
                "message": "过敏原裁决结论为 HOLD（非 RELEASE），不得通过出货核验。",
            }
        )
    if not reasons:
        reasons.append(
            {
                "code": "SHIPMENT_ELIGIBLE",
                "message": "过敏原裁决为 RELEASE，且直接使用的原料批号均未处于待检锁定，具备出货资格。",
            }
        )
    return reasons


def _evaluate_eligibility(
    conn, batch_no: str, lot_nos: list[str] | None = None
) -> tuple[list[str], str, bool, list[dict[str, Any]]]:
    """按当前锁定状态与当前裁决计算出货资格：返回 (锁定批号, 裁决结论, 是否可出货, 原因)。"""
    locked_lots = _locked_lots_for_batch(conn, batch_no, lot_nos)
    row = conn.execute(
        "SELECT disposition FROM adjudication WHERE batch_no = ?", (batch_no,)
    ).fetchone()
    disposition = row["disposition"] if row else "RELEASE"
    eligible = not locked_lots and disposition == "RELEASE"
    return locked_lots, disposition, eligible, _eligibility_reasons(
        eligible, locked_lots, disposition
    )


def _eligibility_block(
    version: int,
    locked_lots: list[str],
    disposition: str,
    updated_at: str,
) -> dict[str, Any]:
    eligible = not locked_lots and disposition == "RELEASE"
    return {
        "version": version,
        "eligible": eligible,
        "locked_lots": locked_lots,
        "reasons": _eligibility_reasons(eligible, locked_lots, disposition),
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------- 裁决计算


def _build_reasons(undeclared: list[str], sources: dict[str, list[str]]) -> list[dict[str, str]]:
    if not undeclared:
        return [{"code": "DECLARED_COMPLETE", "message": "实际可能含有的过敏原均已在成品标签中声明，且清洁核验有效，准予放行。"}]
    reasons = []
    for code in undeclared:
        src = "、".join(sources.get(code, [])) or "来源未知"
        reasons.append(
            {
                "code": "UNDECLARED_ALLERGEN",
                "allergen": code,
                "message": f"实际可能含有未声明过敏原 {al.ALLERGENS[code]}({code})；来源：{src}。",
            }
        )
    reasons.append(
        {
            "code": "HOLD_POLICY",
            "message": "存在未声明过敏原，按换线放行政策扣留本批，不得放行。",
        }
    )
    return reasons


def _adjudicate_batch(b: dict[str, Any], previous_actual: list[str]) -> dict[str, Any]:
    """根据上一批实际过敏原与本批记录，计算本批裁决。"""
    material_allergens = b["material_allergens"]
    declared = b["declared"]
    cleaned = b["cleaned"]

    carried_in = list(previous_actual)
    # 清洁只能清除上一批残留在产线上的过敏原中被本次确认覆盖的项目
    residual = [a for a in carried_in if a not in cleaned]

    actual_set = set(residual) | set(material_allergens)
    actual = [a for a in al.ALLERGENS if a in actual_set]
    undeclared = [a for a in actual if a not in declared]

    sources: dict[str, list[str]] = {}
    for code in material_allergens:
        lots = "、".join(b["allergen_lots"].get(code, []))
        sources.setdefault(code, []).append(
            f"本批原料{('（批号 ' + lots + '）') if lots else ''}引入"
        )
    for code in residual:
        sources.setdefault(code, []).append("上一批残留在共用产线、本次清洁未确认清除")

    disposition = "HOLD" if undeclared else "RELEASE"
    return {
        "carried_in": carried_in,
        "residual": residual,
        "from_materials": list(material_allergens),
        "actual": actual,
        "undeclared": undeclared,
        "disposition": disposition,
        "sources": sources,
        "reasons": _build_reasons(undeclared, sources),
    }


def _load_batches(conn, line_id: str, start_seq: int) -> list[dict[str, Any]]:
    """读取某产线从 start_seq 起的批次，并附上本批原料过敏原与批号来源。"""
    rows = conn.execute(
        "SELECT * FROM batch WHERE line_id = ? AND seq >= ? ORDER BY seq",
        (line_id, start_seq),
    ).fetchall()
    result = []
    for row in rows:
        lot_nos = loads(row["material_lots"])
        lot_rows = conn.execute(
            "SELECT lot_no, allergens FROM material_lot WHERE lot_no IN (%s)"
            % ",".join("?" * len(lot_nos)),
            lot_nos,
        ).fetchall() if lot_nos else []
        lot_allergens: dict[str, list[str]] = {r["lot_no"]: loads(r["allergens"]) for r in lot_rows}

        material_allergens: list[str] = []
        allergen_lots: dict[str, list[str]] = {}
        for lot_no in lot_nos:
            for code in lot_allergens.get(lot_no, []):
                if code not in material_allergens:
                    material_allergens.append(code)
                allergen_lots.setdefault(code, [])
                if lot_no not in allergen_lots[code]:
                    allergen_lots[code].append(lot_no)
        material_allergens = [a for a in al.ALLERGENS if a in material_allergens]

        result.append(
            {
                "batch_no": row["batch_no"],
                "line_id": row["line_id"],
                "seq": row["seq"],
                "material_lots": lot_nos,
                "declared": loads(row["declared"]),
                "cleaned": loads(row["cleaned"]),
                "material_allergens": material_allergens,
                "allergen_lots": allergen_lots,
            }
        )
    return result


def _previous_actual(conn, line_id: str, start_seq: int) -> list[str]:
    """start_seq 之前最近一批的实际过敏原（不存在则为空）。"""
    row = conn.execute(
        """
        SELECT a.actual FROM adjudication a
        JOIN batch b ON b.batch_no = a.batch_no
        WHERE b.line_id = ? AND b.seq < ?
        ORDER BY b.seq DESC LIMIT 1
        """,
        (line_id, start_seq),
    ).fetchone()
    return loads(row["actual"]) if row else []


def recompute_from(
    conn, line_id: str, start_seq: int, trigger: str
) -> list[dict[str, Any]]:
    """从 start_seq 起按产线顺序重算本批及后续批次，落库新裁决版本。

    前置条件：start_seq 之前的批次裁决已是最新（更正入口负责保证）。
    返回被重算批次的裁决摘要列表。
    """
    batches = _load_batches(conn, line_id, start_seq)
    previous_actual = _previous_actual(conn, line_id, start_seq)
    summaries = []

    for b in batches:
        result = _adjudicate_batch(b, previous_actual)
        now = _now()

        row = conn.execute(
            "SELECT version FROM adjudication WHERE batch_no = ?",
            (b["batch_no"],),
        ).fetchone()
        new_version = (row["version"] if row else 0) + 1

        if row:
            conn.execute(
                """UPDATE adjudication SET version=?, actual=?, undeclared=?,
                   disposition=?, reasons=?, sources=?, decided_at=? WHERE batch_no=?""",
                (
                    new_version,
                    dumps(result["actual"]),
                    dumps(result["undeclared"]),
                    result["disposition"],
                    dumps(result["reasons"]),
                    dumps(result["sources"]),
                    now,
                    b["batch_no"],
                ),
            )
        else:
            conn.execute(
                """INSERT INTO adjudication
                   (batch_no, version, actual, undeclared, disposition, reasons, sources, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    b["batch_no"],
                    new_version,
                    dumps(result["actual"]),
                    dumps(result["undeclared"]),
                    result["disposition"],
                    dumps(result["reasons"]),
                    dumps(result["sources"]),
                    now,
                ),
            )

        conn.execute(
            """INSERT INTO adjudication_version
               (batch_no, line_id, seq, version, actual, undeclared, disposition,
                reasons, sources, trigger, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                b["batch_no"],
                line_id,
                b["seq"],
                new_version,
                dumps(result["actual"]),
                dumps(result["undeclared"]),
                result["disposition"],
                dumps(result["reasons"]),
                dumps(result["sources"]),
                trigger,
                now,
            ),
        )

        previous_actual = result["actual"]
        summaries.append(
            {
                "batch_no": b["batch_no"],
                "line_id": line_id,
                "seq": b["seq"],
                "version": new_version,
                "actual": result["actual"],
                "undeclared": result["undeclared"],
                "disposition": result["disposition"],
                "reasons": result["reasons"],
            }
        )

    # 本次重算产生了新裁决版本：引用这些批次的“已确认”核验单立即转待复核
    # （即使结论仍为 RELEASE）。草稿单不转态——其快照本就待重新核验时刷新。
    if summaries:
        _flip_confirmed_verifications(conn, [s["batch_no"] for s in summaries])

    return summaries


def _flip_confirmed_verifications(conn, batch_nos: list[str]) -> int:
    """所含批次（裁决版本或出货资格版本）发生变化的已确认核验单立即转待复核。

    草稿/待复核单据不动（其快照本就待重新核验时刷新）；历次确认快照保留。
    """
    if not batch_nos:
        return 0
    placeholders = ",".join("?" * len(batch_nos))
    cur = conn.execute(
        f"""UPDATE verification SET status = 'PENDING_REVIEW', updated_at = ?
            WHERE status = 'CONFIRMED' AND verification_no IN (
                SELECT DISTINCT verification_no FROM verification_batch
                WHERE batch_no IN ({placeholders})
            )""",
        [_now(), *batch_nos],
    )
    return cur.rowcount


def _sync_eligibility_state(conn, batch_no: str) -> None:
    """无版本同步：声明/清洁更正导致 HOLD↔RELEASE 翻转时，资格表的可出货标志与原因
    随之刷新（不追加历史版本——资格依据即锁定批号集合未变）。"""
    row = conn.execute(
        "SELECT version FROM shipping_eligibility WHERE batch_no = ?", (batch_no,)
    ).fetchone()
    if row is None:
        return
    locked_lots, disposition, eligible, reasons = _evaluate_eligibility(conn, batch_no)
    conn.execute(
        "UPDATE shipping_eligibility SET eligible=?, locked_lots=?, reasons=?, updated_at=? WHERE batch_no=?",
        (1 if eligible else 0, dumps(locked_lots), dumps(reasons), _now(), batch_no),
    )


def _init_eligibility(conn, batch_no: str, trigger: str) -> dict[str, Any]:
    """新登记批次：按当前锁定状态建立出货资格 v1。"""
    locked_lots, disposition, eligible, reasons = _evaluate_eligibility(conn, batch_no)
    now = _now()
    conn.execute(
        """INSERT INTO shipping_eligibility
           (batch_no, version, eligible, locked_lots, reasons, updated_at)
           VALUES (?, 1, ?, ?, ?, ?)""",
        (batch_no, 1 if eligible else 0, dumps(locked_lots), dumps(reasons), now),
    )
    conn.execute(
        """INSERT INTO shipping_eligibility_version
           (batch_no, version, eligible, locked_lots, reasons, trigger, updated_at)
           VALUES (?, 1, ?, ?, ?, ?, ?)""",
        (batch_no, 1 if eligible else 0, dumps(locked_lots), dumps(reasons), trigger, now),
    )
    return _eligibility_block(1, locked_lots, disposition, now)


def _bump_eligibility(
    conn,
    batch_no: str,
    trigger: str,
    *,
    force: bool,
    prev_eligible: bool | None = None,
) -> dict[str, Any] | None:
    """重算单批出货资格；资格输入实际变化时追加版本并联动已确认核验单。

    force=True（原料批号锁定/解除）：只要直接用料集合的锁定状态发生变化就递增版本，
    即使该批因 HOLD 本就不具备出货资格——资格依据变了，版本也要递增。
    force=False（批次更正用料）：仅当可出货结论发生变化才递增版本；调用方须按
    “更正前的裁决 + 更正前用料”现场算好 prev_eligible 传入（存储标志可能因
    声明更正导致 HOLD/RELEASE 翻转而滞后，不能直接用作对比基准）。
    返回新版本块；无变化返回 None。
    """
    row = conn.execute(
        "SELECT * FROM shipping_eligibility WHERE batch_no = ?", (batch_no,)
    ).fetchone()
    prev_locked = loads(row["locked_lots"]) if row else []
    stored_prev_eligible = bool(row["eligible"]) if row else False
    if prev_eligible is None:
        prev_eligible = stored_prev_eligible
    locked_lots, disposition, eligible, reasons = _evaluate_eligibility(conn, batch_no)
    changed = (locked_lots != prev_locked) if force else (eligible != prev_eligible)
    if not changed:
        # 资格依据未变、不递增版本；但可出货标志可能因 HOLD↔RELEASE 的裁决更正已翻转，
        # 同步标志与原因使当前状态自洽（不写历史版本）。
        if row is not None and bool(row["eligible"]) != eligible:
            conn.execute(
                "UPDATE shipping_eligibility SET eligible=?, reasons=?, updated_at=? WHERE batch_no=?",
                (1 if eligible else 0, dumps(reasons), _now(), batch_no),
            )
        return None

    new_version = (row["version"] if row else 0) + 1
    now = _now()
    if row:
        conn.execute(
            """UPDATE shipping_eligibility SET version=?, eligible=?, locked_lots=?,
               reasons=?, updated_at=? WHERE batch_no=?""",
            (new_version, 1 if eligible else 0, dumps(locked_lots), dumps(reasons), now, batch_no),
        )
    else:
        conn.execute(
            """INSERT INTO shipping_eligibility
               (batch_no, version, eligible, locked_lots, reasons, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (batch_no, new_version, 1 if eligible else 0, dumps(locked_lots), dumps(reasons), now),
        )
    conn.execute(
        """INSERT INTO shipping_eligibility_version
           (batch_no, version, eligible, locked_lots, reasons, trigger, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_no,
            new_version,
            1 if eligible else 0,
            dumps(locked_lots),
            dumps(reasons),
            trigger,
            now,
        ),
    )
    _flip_confirmed_verifications(conn, [batch_no])
    return _eligibility_block(new_version, locked_lots, disposition, now)


# ---------------------------------------------------------------- 序列化


def _material_lot_payload(conn, row) -> dict[str, Any]:
    return {
        "lot_no": row["lot_no"],
        "material": row["material"],
        "allergens": loads(row["allergens"]),
        "updated_at": row["updated_at"],
        "lock_status": row["lock_status"],
        "lock_reason": row["lock_reason"],
        "locked_at": row["locked_at"],
    }


def _batch_full(conn, row) -> dict[str, Any]:
    adj = conn.execute(
        "SELECT * FROM adjudication WHERE batch_no = ?", (row["batch_no"],)
    ).fetchone()
    se = conn.execute(
        "SELECT * FROM shipping_eligibility WHERE batch_no = ?", (row["batch_no"],)
    ).fetchone()
    disposition = adj["disposition"] if adj else "RELEASE"
    locked_lots = loads(se["locked_lots"]) if se else []
    return {
        "batch_no": row["batch_no"],
        "line_id": row["line_id"],
        "seq": row["seq"],
        "material_lots": loads(row["material_lots"]),
        "declared_allergens": loads(row["declared"]),
        "cleaned_allergens": loads(row["cleaned"]),
        "created_at": row["created_at"],
        "adjudication": _adj_payload(adj) if adj else None,
        "shipping_eligibility": _eligibility_block(
            se["version"], locked_lots, disposition, se["updated_at"]
        )
        if se
        else None,
    }


def _adj_payload(adj) -> dict[str, Any]:
    return {
        "version": adj["version"],
        "actual_allergens": loads(adj["actual"]),
        "undeclared_allergens": loads(adj["undeclared"]),
        "disposition": adj["disposition"],
        "reasons": loads(adj["reasons"]),
        "sources": loads(adj["sources"]),
        "decided_at": adj["decided_at"],
    }


# ---------------------------------------------------------------- 服务入口


class Service:
    def __init__(self, store: Store):
        self.store = store

    # ---- 参考数据 ----

    def list_allergens(self) -> list[dict[str, str]]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT code, name_zh FROM allergen ORDER BY code").fetchall()
            return [{"code": r["code"], "name_zh": r["name_zh"]} for r in rows]

    def create_material_lot(self, lot_no: str, material: str, allergens: list[str]) -> dict[str, Any]:
        lot_no = (lot_no or "").strip()
        material = (material or "").strip()
        if not lot_no or not material:
            raise bad_request("INVALID_PAYLOAD", "lot_no 与 material 不能为空")
        codes = _normalize_allergen_list(allergens, "allergens")
        with self.store.write() as conn:
            exists = conn.execute(
                "SELECT 1 FROM material_lot WHERE lot_no = ?", (lot_no,)
            ).fetchone()
            if exists:
                raise conflict("DUPLICATE_MATERIAL_LOT", f"原料批号已存在: {lot_no}")
            now = _now()
            conn.execute(
                "INSERT INTO material_lot(lot_no, material, allergens, updated_at) VALUES (?, ?, ?, ?)",
                (lot_no, material, dumps(codes), now),
            )
        return {"lot_no": lot_no, "material": material, "allergens": codes}

    def get_material_lot(self, lot_no: str) -> dict[str, Any]:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM material_lot WHERE lot_no = ?", (lot_no,)
            ).fetchone()
            if not row:
                raise not_found("MATERIAL_LOT_NOT_FOUND", f"原料批号不存在: {lot_no}")
            return _material_lot_payload(conn, row)

    def list_material_lots(self) -> list[dict[str, Any]]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT * FROM material_lot ORDER BY lot_no").fetchall()
            return [_material_lot_payload(conn, r) for r in rows]

    def correct_material_lot(
        self, lot_no: str, material: str | None, allergens: list[str] | None
    ) -> dict[str, Any]:
        """更正原料批号档案：按产线顺序重算所有引用该批号的批次及其后续批次。"""
        with self.store.write() as conn:
            row = conn.execute(
                "SELECT * FROM material_lot WHERE lot_no = ?", (lot_no,)
            ).fetchone()
            if not row:
                raise not_found(
                    "CORRECTION_TARGET_NOT_FOUND", f"更正目标不存在，原料批号: {lot_no}"
                )

            new_material = (material or row["material"]).strip()
            old_allergens = loads(row["allergens"])
            new_allergens = (
                _normalize_allergen_list(allergens, "allergens")
                if allergens is not None
                else old_allergens
            )
            conn.execute(
                "UPDATE material_lot SET material=?, allergens=?, updated_at=? WHERE lot_no=?",
                (new_material, dumps(new_allergens), _now(), lot_no),
            )

            recomputed = []
            # 仅过敏原集合实际变化才会影响裁决；只改名称不产生新版本
            if new_allergens != old_allergens:
                # 受影响产线：引用该批号的最小 seq；每条产线各自从该点重算
                refs = conn.execute(
                    """SELECT b.line_id AS line_id, MIN(b.seq) AS start_seq
                       FROM batch_material bm JOIN batch b ON b.batch_no = bm.batch_no
                       WHERE bm.lot_no = ? GROUP BY b.line_id ORDER BY b.line_id""",
                    (lot_no,),
                ).fetchall()
                for ref in refs:
                    recomputed.extend(
                        recompute_from(conn, ref["line_id"], ref["start_seq"], "CORRECT_LOT")
                    )
                affected = _affected_verifications(conn, [s["batch_no"] for s in recomputed])
            else:
                affected = []

        return {
            "corrected": {
                "type": "material_lot",
                "lot_no": lot_no,
                "material": new_material,
                "allergens": new_allergens,
                "lock_status": "LOCKED" if row["lock_status"] == "LOCKED" else "UNLOCKED",
            },
            "recomputed": recomputed,
            "affected_verifications": affected,
        }

    # ---- 原料批号待检锁定 ----

    def set_material_lot_lock(
        self, lot_no: str, lock_status: str, reason: str
    ) -> dict[str, Any]:
        """按批号锁定（待检）或解除锁定并记录原因。

        - 未知批号拒绝；空原因拒绝；
        - 重复提交相同状态不产生新变更（返回 changed=false，版本不递增）；
        - 每次实际变化都使“直接使用”该批号的批次出货资格版本递增，
          并使含这些批次的已确认核验单转待复核；
        - 不改动过敏原裁决（adjudication 版本与历史不变）；
        - 同线后续批次若未直接使用该批号，不因残留传递而连带受影响。
        """
        lot_no = (lot_no or "").strip()
        lock_status = (lock_status or "").strip().upper()
        reason = (reason or "").strip()
        if lock_status not in LOT_LOCK_STATUSES:
            raise bad_request(
                "INVALID_PAYLOAD",
                f"lock_status 必须是 {', '.join(LOT_LOCK_STATUSES)} 之一",
            )
        if not reason:
            raise bad_request("INVALID_PAYLOAD", "锁定/解除原因 reason 不能为空")

        with self.store.write() as conn:
            row = conn.execute(
                "SELECT * FROM material_lot WHERE lot_no = ?", (lot_no,)
            ).fetchone()
            if not row:
                raise not_found(
                    "MATERIAL_LOT_NOT_FOUND", f"原料批号不存在: {lot_no}"
                )

            changed = row["lock_status"] != lock_status
            now = _now()
            if changed:
                conn.execute(
                    """UPDATE material_lot
                       SET lock_status=?, lock_reason=?, locked_at=?, updated_at=?
                       WHERE lot_no=?""",
                    (
                        lock_status,
                        reason,
                        now if lock_status == "LOCKED" else None,
                        now,
                        lot_no,
                    ),
                )
                conn.execute(
                    """INSERT INTO material_lot_lock_event
                       (lot_no, lock_status, reason, changed_at)
                       VALUES (?, ?, ?, ?)""",
                    (lot_no, lock_status, reason, now),
                )

            eligibility_changes: list[dict[str, Any]] = []
            if changed:
                # 直接使用该批号的全部批次（跨产线）逐个重算出资格版本；
                # 同线后续未直接使用该批号的批次不连带（残留传递不产生锁定）。
                direct = [
                    r["batch_no"]
                    for r in conn.execute(
                        """SELECT DISTINCT batch_no FROM batch_material
                           WHERE lot_no = ? ORDER BY batch_no""",
                        (lot_no,),
                    ).fetchall()
                ]
                trigger = "LOT_LOCK" if lock_status == "LOCKED" else "LOT_UNLOCK"
                for batch_no in direct:
                    block = _bump_eligibility(conn, batch_no, trigger, force=True)
                    if block is not None:
                        eligibility_changes.append(
                            {"batch_no": batch_no, **block}
                        )

            payload = _material_lot_payload(
                conn,
                conn.execute(
                    "SELECT * FROM material_lot WHERE lot_no = ?", (lot_no,)
                ).fetchone(),
            )
            payload["changed"] = changed
            payload["reason"] = reason
            payload["eligibility_changes"] = eligibility_changes
            return payload

    def material_lot_lock_history(self, lot_no: str) -> dict[str, Any]:
        """查询原料批号当前锁定状态及历次锁定/解除变更（只追加）。"""
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM material_lot WHERE lot_no = ?", (lot_no,)
            ).fetchone()
            if not row:
                raise not_found("MATERIAL_LOT_NOT_FOUND", f"原料批号不存在: {lot_no}")
            events = conn.execute(
                """SELECT lock_status, reason, changed_at
                   FROM material_lot_lock_event WHERE lot_no = ? ORDER BY id""",
                (lot_no,),
            ).fetchall()
            return {
                "lot_no": lot_no,
                "lock_status": row["lock_status"],
                "lock_reason": row["lock_reason"],
                "locked_at": row["locked_at"],
                "history": [
                    {
                        "lock_status": e["lock_status"],
                        "reason": e["reason"],
                        "changed_at": e["changed_at"],
                    }
                    for e in events
                ],
            }

    # ---- 产线 ----

    def list_lines(self) -> list[dict[str, Any]]:
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT l.line_id,
                          COUNT(b.batch_no) AS batch_count,
                          COALESCE(MAX(b.seq), 0) AS last_seq
                   FROM line l LEFT JOIN batch b ON b.line_id = l.line_id
                   GROUP BY l.line_id ORDER BY l.line_id""",
            ).fetchall()
            return [
                {"line_id": r["line_id"], "batch_count": r["batch_count"], "last_seq": r["last_seq"]}
                for r in rows
            ]

    # ---- 批次 ----

    def register_batch(
        self,
        batch_no: str,
        line_id: str,
        material_lots: list[str],
        declared_allergens: list[str],
        cleaned_allergens: list[str],
    ) -> dict[str, Any]:
        batch_no = (batch_no or "").strip()
        line_id = (line_id or "").strip()
        if not batch_no or not line_id:
            raise bad_request("INVALID_PAYLOAD", "batch_no 与 line_id 不能为空")
        lots = _normalize_str_list(material_lots, "material_lots")
        declared = _normalize_allergen_list(declared_allergens, "declared_allergens")
        cleaned = _normalize_allergen_list(cleaned_allergens, "cleaned_allergens")

        with self.store.write() as conn:
            if conn.execute("SELECT 1 FROM batch WHERE batch_no = ?", (batch_no,)).fetchone():
                raise conflict("DUPLICATE_BATCH", f"批次号重复: {batch_no}")

            # 未知原料批号：必须先登记原料批号档案
            if lots:
                placeholders = ",".join("?" * len(lots))
                known = {
                    r["lot_no"]
                    for r in conn.execute(
                        f"SELECT lot_no FROM material_lot WHERE lot_no IN ({placeholders})",
                        lots,
                    ).fetchall()
                }
                unknown = [l for l in lots if l not in known]
                if unknown:
                    raise bad_request(
                        "UNKNOWN_MATERIAL_LOT", f"未知原料批号: {', '.join(unknown)}"
                    )

            now = _now()
            conn.execute(
                "INSERT OR IGNORE INTO line(line_id, created_at) VALUES (?, ?)",
                (line_id, now),
            )

            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM batch WHERE line_id = ?",
                (line_id,),
            ).fetchone()
            seq = row["max_seq"] + 1

            conn.execute(
                """INSERT INTO batch
                   (batch_no, line_id, seq, material_lots, declared, cleaned, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (batch_no, line_id, seq, dumps(lots), dumps(declared), dumps(cleaned), now),
            )
            conn.executemany(
                "INSERT INTO batch_material(batch_no, lot_no) VALUES (?, ?)",
                [(batch_no, lot) for lot in lots],
            )

            recompute_from(conn, line_id, seq, "REGISTER")
            # 新登记批次按当前（各原料批号）锁定状态确定出货资格，版本从 v1 起
            _init_eligibility(conn, batch_no, "REGISTER")
            full = _batch_full(
                conn,
                conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone(),
            )
        return full

    def get_batch(self, batch_no: str) -> dict[str, Any]:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone()
            if not row:
                raise not_found("BATCH_NOT_FOUND", f"批次不存在: {batch_no}")
            return _batch_full(conn, row)

    def list_batches(self, line_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.read() as conn:
            if line_id:
                rows = conn.execute(
                    "SELECT * FROM batch WHERE line_id = ? ORDER BY line_id, seq",
                    (line_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM batch ORDER BY line_id, seq").fetchall()
            return [_batch_full(conn, r) for r in rows]

    def correct_batch(
        self,
        batch_no: str,
        material_lots: list[str] | None,
        declared_allergens: list[str] | None,
        cleaned_allergens: list[str] | None,
    ) -> dict[str, Any]:
        """更正批次的原料批号 / 声明 / 清洁核验，重算本批及后续批次。"""
        with self.store.write() as conn:
            row = conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone()
            if not row:
                raise not_found("CORRECTION_TARGET_NOT_FOUND", f"更正目标不存在，批次号: {batch_no}")

            new_lots = (
                _normalize_str_list(material_lots, "material_lots")
                if material_lots is not None
                else loads(row["material_lots"])
            )
            new_declared = (
                _normalize_allergen_list(declared_allergens, "declared_allergens")
                if declared_allergens is not None
                else loads(row["declared"])
            )
            new_cleaned = (
                _normalize_allergen_list(cleaned_allergens, "cleaned_allergens")
                if cleaned_allergens is not None
                else loads(row["cleaned"])
            )

            if new_lots:
                placeholders = ",".join("?" * len(new_lots))
                known = {
                    r["lot_no"]
                    for r in conn.execute(
                        f"SELECT lot_no FROM material_lot WHERE lot_no IN ({placeholders})",
                        new_lots,
                    ).fetchall()
                }
                unknown = [l for l in new_lots if l not in known]
                if unknown:
                    raise bad_request("UNKNOWN_MATERIAL_LOT", f"未知原料批号: {', '.join(unknown)}")

            # 更正用料若改变出货资格（按“更正前裁决+旧用料”对比“更正后裁决+新用料”）
            # 也递增资格版本；只改声明/清洁、用料集合不变时不产生资格新版本。
            old_lot_nos = loads(row["material_lots"])
            old_locked = _locked_lots_for_batch(conn, batch_no, old_lot_nos)
            old_adj = conn.execute(
                "SELECT disposition FROM adjudication WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            old_disposition = old_adj["disposition"] if old_adj else "RELEASE"
            old_eligible = not old_locked and old_disposition == "RELEASE"

            conn.execute(
                "UPDATE batch SET material_lots=?, declared=?, cleaned=? WHERE batch_no=?",
                (dumps(new_lots), dumps(new_declared), dumps(new_cleaned), batch_no),
            )
            conn.execute("DELETE FROM batch_material WHERE batch_no = ?", (batch_no,))
            conn.executemany(
                "INSERT INTO batch_material(batch_no, lot_no) VALUES (?, ?)",
                [(batch_no, lot) for lot in new_lots],
            )

            recomputed = recompute_from(conn, row["line_id"], row["seq"], "CORRECT_BATCH")

            eligibility_change = None
            if new_lots != old_lot_nos:
                eligibility_change = _bump_eligibility(
                    conn,
                    batch_no,
                    "CORRECT_BATCH",
                    force=False,
                    prev_eligible=old_eligible,
                )
            if eligibility_change is None:
                # 未产生资格新版本（仅声明/清洁更正，或可出货结论未变）：同步当前标志、
                # 锁定批号集合与原因，但不递增版本
                _sync_eligibility_state(conn, batch_no)

            affected = _affected_verifications(conn, [s["batch_no"] for s in recomputed])
            full = _batch_full(
                conn,
                conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone(),
            )

        return {
            "corrected": full,
            "recomputed": recomputed,
            "eligibility_change": eligibility_change,
            "affected_verifications": affected,
        }

    def correct_cleaning(self, batch_no: str, cleaned_allergens: list[str]) -> dict[str, Any]:
        """专用入口：仅更正“批次开始前清洁核验确认清除的过敏原”。"""
        return self.correct_batch(batch_no, None, None, cleaned_allergens)

    def batch_history(self, batch_no: str) -> dict[str, Any]:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone()
            if not row:
                raise not_found("BATCH_NOT_FOUND", f"批次不存在: {batch_no}")
            versions = conn.execute(
                "SELECT * FROM adjudication_version WHERE batch_no = ? ORDER BY version",
                (batch_no,),
            ).fetchall()
            return {
                "batch_no": batch_no,
                "line_id": row["line_id"],
                "seq": row["seq"],
                "history": [
                    {
                        "version": v["version"],
                        "actual_allergens": loads(v["actual"]),
                        "undeclared_allergens": loads(v["undeclared"]),
                        "disposition": v["disposition"],
                        "reasons": loads(v["reasons"]),
                        "sources": loads(v["sources"]),
                        "trigger": v["trigger"],
                        "decided_at": v["decided_at"],
                    }
                    for v in versions
                ],
            }

    def batch_eligibility(self, batch_no: str) -> dict[str, Any]:
        """逐批查询出货资格：当前资格（版本/可出货/锁定批号/原因）与历次资格版本。"""
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone()
            if not row:
                raise not_found("BATCH_NOT_FOUND", f"批次不存在: {batch_no}")
            se = conn.execute(
                "SELECT * FROM shipping_eligibility WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            adj = conn.execute(
                "SELECT disposition FROM adjudication WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            disposition = adj["disposition"] if adj else "RELEASE"
            versions = conn.execute(
                """SELECT * FROM shipping_eligibility_version
                   WHERE batch_no = ? ORDER BY version""",
                (batch_no,),
            ).fetchall()
            return {
                "batch_no": batch_no,
                "line_id": row["line_id"],
                "seq": row["seq"],
                "current": _eligibility_block(
                    se["version"], loads(se["locked_lots"]), disposition, se["updated_at"]
                )
                if se
                else None,
                "history": [
                    {
                        "version": v["version"],
                        "eligible": bool(v["eligible"]),
                        "locked_lots": loads(v["locked_lots"]),
                        "reasons": loads(v["reasons"]),
                        "trigger": v["trigger"],
                        "updated_at": v["updated_at"],
                    }
                    for v in versions
                ],
            }

    # ---- 出货核验单 ----

    def create_verification(self, verification_no: str, batch_nos: list[str]) -> dict[str, Any]:
        """创建出货核验单（草稿）：逐批保存当前裁决版本与结论，返回核验单快照。

        单号须唯一、批次号列表须非空且无重复、批次均须已登记；
        任一校验失败整体回滚，不留下部分单据。
        """
        verification_no = (verification_no or "").strip()
        if not verification_no:
            raise bad_request("INVALID_PAYLOAD", "verification_no 不能为空")
        if not isinstance(batch_nos, list) or not batch_nos:
            raise bad_request("INVALID_PAYLOAD", "batch_nos 必须是非空批次号数组")
        normalized: list[str] = []
        for bn in batch_nos:
            if not isinstance(bn, str) or not bn.strip():
                raise bad_request("INVALID_PAYLOAD", "batch_nos 必须是非空字符串数组")
            bn = bn.strip()
            if bn in normalized:
                raise bad_request(
                    "DUPLICATE_BATCH_IN_VERIFICATION", f"批次号在列表中重复: {bn}"
                )
            normalized.append(bn)

        with self.store.write() as conn:
            if conn.execute(
                "SELECT 1 FROM verification WHERE verification_no = ?", (verification_no,)
            ).fetchone():
                raise conflict("DUPLICATE_VERIFICATION", f"出货核验单号已存在: {verification_no}")

            placeholders = ",".join("?" * len(normalized))
            known = {
                r["batch_no"]
                for r in conn.execute(
                    f"SELECT batch_no FROM batch WHERE batch_no IN ({placeholders})",
                    normalized,
                ).fetchall()
            }
            unknown = [bn for bn in normalized if bn not in known]
            if unknown:
                raise bad_request(
                    "UNKNOWN_BATCH", f"未知批次（未登记）: {', '.join(unknown)}"
                )

            now = _now()
            conn.execute(
                """INSERT INTO verification
                   (verification_no, status, created_at, updated_at, confirmed_at)
                   VALUES (?, 'DRAFT', ?, ?, NULL)""",
                (verification_no, now, now),
            )
            _refresh_snapshots(conn, verification_no, normalized)
            row = conn.execute(
                "SELECT * FROM verification WHERE verification_no = ?", (verification_no,)
            ).fetchone()
            return _verification_payload(conn, row)

    def list_verifications(self, status: str | None = None) -> dict[str, Any]:
        if status is not None:
            status = status.strip().upper()
            if status not in VERIFICATION_STATUSES:
                raise bad_request(
                    "INVALID_PAYLOAD",
                    f"status 必须是 {', '.join(VERIFICATION_STATUSES)} 之一",
                )
        with self.store.read() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM verification WHERE status = ? ORDER BY verification_no",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM verification ORDER BY verification_no"
                ).fetchall()
            return {
                "verifications": [
                    _verification_payload(conn, r, with_history=False) for r in rows
                ]
            }

    def get_verification(self, verification_no: str) -> dict[str, Any]:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM verification WHERE verification_no = ?", (verification_no,)
            ).fetchone()
            if not row:
                raise not_found(
                    "VERIFICATION_NOT_FOUND", f"出货核验单不存在: {verification_no}"
                )
            return _verification_payload(conn, row)

    def reverify_verification(self, verification_no: str) -> dict[str, Any]:
        """重新核验：刷新逐批快照（取各批当前裁决版本），单据回到草稿。

        仅草稿/待复核可重新核验；已确认单据不可重新核验。
        """
        with self.store.write() as conn:
            row = conn.execute(
                "SELECT * FROM verification WHERE verification_no = ?", (verification_no,)
            ).fetchone()
            if not row:
                raise not_found(
                    "VERIFICATION_NOT_FOUND", f"出货核验单不存在: {verification_no}"
                )
            if row["status"] == "CONFIRMED":
                raise conflict(
                    "VERIFICATION_ALREADY_CONFIRMED",
                    "已确认单据不能重新核验；如所含批次产生新裁决版本，单据会自动转待复核",
                )
            batch_nos = [
                r["batch_no"]
                for r in conn.execute(
                    """SELECT batch_no FROM verification_batch
                       WHERE verification_no = ? ORDER BY position""",
                    (verification_no,),
                ).fetchall()
            ]
            _refresh_snapshots(conn, verification_no, batch_nos)
            row = conn.execute(
                "SELECT * FROM verification WHERE verification_no = ?", (verification_no,)
            ).fetchone()
            return _verification_payload(conn, row)

    def confirm_verification(self, verification_no: str) -> dict[str, Any]:
        """原子确认：逐批核对四项——快照裁决版本仍当前、快照出货资格版本仍当前、
        当前裁决为 RELEASE、当前无直接使用的待检锁定批号。

        任一不符：返回全部不符批次及原因（STALE_SNAPSHOT /
        STALE_ELIGIBILITY_SNAPSHOT / BATCH_ON_HOLD / MATERIAL_LOT_LOCKED），
        单据保持未确认（不写入确认记录）。HOLD 批次、锁定批号始终不能确认。
        """
        with self.store.write() as conn:
            row = conn.execute(
                "SELECT * FROM verification WHERE verification_no = ?", (verification_no,)
            ).fetchone()
            if not row:
                raise not_found(
                    "VERIFICATION_NOT_FOUND", f"出货核验单不存在: {verification_no}"
                )
            if row["status"] == "CONFIRMED":
                raise conflict(
                    "VERIFICATION_ALREADY_CONFIRMED",
                    f"出货核验单已确认: {verification_no}",
                )

            mismatches: list[dict[str, Any]] = []
            for b in _verification_batches(conn, verification_no):
                snapshot = b["snapshot"]
                current = b["current"]
                if current is None:
                    mismatches.append(
                        {
                            "batch_no": b["batch_no"],
                            "reason_code": "STALE_SNAPSHOT",
                            "message": "批次缺少当前裁决，无法确认",
                        }
                    )
                    continue

                cur_adj = current["version"]
                cur_elig = current["shipping_eligibility"]["version"]
                cur_locked = current["shipping_eligibility"]["locked_lots"]
                stale = cur_adj != snapshot["version"]
                elig_stale = cur_elig != snapshot["shipping_eligibility"]["version"]
                # HOLD 批次始终不能确认：版本未漂移看快照结论，版本漂移看当前结论
                held = current["disposition"] != "RELEASE"
                # 锁定期间，即使过敏原裁决 RELEASE 也不得通过出货核验
                lot_locked = bool(cur_locked)
                if stale:
                    mismatches.append(
                        {
                            "batch_no": b["batch_no"],
                            "reason_code": "STALE_SNAPSHOT",
                            "message": (
                                f"快照裁决版本 v{snapshot['version']} 已不是当前版本 "
                                f"v{cur_adj}，请先重新核验"
                            ),
                            "snapshot_version": snapshot["version"],
                            "current_version": cur_adj,
                            "change_reasons": b["change_reasons"],
                        }
                    )
                if elig_stale:
                    mismatches.append(
                        {
                            "batch_no": b["batch_no"],
                            "reason_code": "STALE_ELIGIBILITY_SNAPSHOT",
                            "message": (
                                f"快照出货资格版本 v{snapshot['shipping_eligibility']['version']} "
                                f"已不是当前版本 v{cur_elig}，请先重新核验"
                            ),
                            "snapshot_eligibility_version": snapshot["shipping_eligibility"]["version"],
                            "current_eligibility_version": cur_elig,
                            "change_reasons": b["eligibility_change_reasons"],
                        }
                    )
                if held:
                    mismatches.append(
                        {
                            "batch_no": b["batch_no"],
                            "reason_code": "BATCH_ON_HOLD",
                            "message": (
                                f"批次{'当前' if stale else '快照'}裁决为 HOLD（非 RELEASE），"
                                "HOLD 批次不能确认"
                            ),
                            "snapshot_version": snapshot["version"],
                            "current_version": cur_adj,
                            "disposition": current["disposition"],
                            "undeclared_allergens": current["undeclared_allergens"],
                        }
                    )
                if lot_locked:
                    mismatches.append(
                        {
                            "batch_no": b["batch_no"],
                            "reason_code": "MATERIAL_LOT_LOCKED",
                            "message": (
                                f"直接使用的原料批号 {', '.join(cur_locked)} 当前处于待检锁定状态；"
                                "即使过敏原裁决为 RELEASE 也不得通过出货核验"
                            ),
                            "locked_lots": cur_locked,
                            "current_eligibility_version": cur_elig,
                            "disposition": current["disposition"],
                        }
                    )

            if mismatches:
                raise unprocessable(
                    "VERIFICATION_NOT_CONFIRMABLE",
                    "存在不符批次，核验单保持未确认；请处理后重新核验再确认",
                    mismatches,
                )

            now = _now()
            cur = conn.execute(
                "INSERT INTO verification_confirmation(verification_no, confirmed_at) VALUES (?, ?)",
                (verification_no, now),
            )
            confirmation_id = cur.lastrowid
            snapshot_rows = conn.execute(
                """SELECT verification_no, position, batch_no, snapshot_version,
                          snapshot_disposition, snapshot_actual, snapshot_undeclared,
                          snapshot_reasons, snapshot_eligibility_version,
                          snapshot_eligible, snapshot_locked_lots
                   FROM verification_batch WHERE verification_no = ? ORDER BY position""",
                (verification_no,),
            ).fetchall()
            conn.executemany(
                """INSERT INTO verification_confirmation_batch
                   (confirmation_id, verification_no, position, batch_no, snapshot_version,
                    snapshot_disposition, snapshot_actual, snapshot_undeclared, snapshot_reasons,
                    snapshot_eligibility_version, snapshot_eligible, snapshot_locked_lots)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        confirmation_id,
                        sr["verification_no"],
                        sr["position"],
                        sr["batch_no"],
                        sr["snapshot_version"],
                        sr["snapshot_disposition"],
                        sr["snapshot_actual"],
                        sr["snapshot_undeclared"],
                        sr["snapshot_reasons"],
                        sr["snapshot_eligibility_version"],
                        sr["snapshot_eligible"],
                        sr["snapshot_locked_lots"],
                    )
                    for sr in snapshot_rows
                ],
            )
            conn.execute(
                """UPDATE verification
                   SET status='CONFIRMED', confirmed_at=?, updated_at=?
                   WHERE verification_no=?""",
                (now, now, verification_no),
            )
            row = conn.execute(
                "SELECT * FROM verification WHERE verification_no = ?", (verification_no,)
            ).fetchone()
            return _verification_payload(conn, row)


# ---------------------------------------------------------------- 出货核验单

VERIFICATION_STATUSES = ("DRAFT", "PENDING_REVIEW", "CONFIRMED")

_TRIGGER_LABELS = {
    "REGISTER": "登记批次时裁决",
    "CORRECT_BATCH": "批次更正重算",
    "CORRECT_LOT": "原料批号更正重算",
}


def _trigger_label(trigger: str) -> str:
    return _TRIGGER_LABELS.get(trigger, trigger)


def _affected_verifications(conn, batch_nos: list[str]) -> list[dict[str, Any]]:
    """更正重算后，所含批次被重算的核验单及其当前状态（CONFIRMED 已自动转 PENDING_REVIEW）。"""
    if not batch_nos:
        return []
    placeholders = ",".join("?" * len(batch_nos))
    rows = conn.execute(
        f"""SELECT DISTINCT v.verification_no AS verification_no, v.status AS status,
                   v.updated_at AS updated_at
            FROM verification v
            JOIN verification_batch vb ON vb.verification_no = v.verification_no
            WHERE vb.batch_no IN ({placeholders})
            ORDER BY v.verification_no""",
        batch_nos,
    ).fetchall()
    return [
        {"verification_no": r["verification_no"], "status": r["status"], "updated_at": r["updated_at"]}
        for r in rows
    ]


def _change_reasons(conn, batch_no: str, snapshot_version: int) -> list[dict[str, Any]]:
    """快照版本之后产生的裁决版本即“变化”，逐条给出变化原因（取该版本 trigger）。"""
    rows = conn.execute(
        """SELECT version, disposition, trigger, decided_at
           FROM adjudication_version
           WHERE batch_no = ? AND version > ? ORDER BY version""",
        (batch_no, snapshot_version),
    ).fetchall()
    return [
        {
            "code": "ADJUDICATION_VERSION_CHANGED",
            "message": (
                f"裁决版本 v{r['version']}（{_trigger_label(r['trigger'])}）"
                f"晚于快照版本 v{snapshot_version}，结论 {r['disposition']}"
            ),
            "from_version": snapshot_version if idx == 0 else rows[idx - 1]["version"],
            "to_version": r["version"],
            "trigger": r["trigger"],
            "decided_at": r["decided_at"],
        }
        for idx, r in enumerate(rows)
    ]


def _eligibility_change_reasons(
    conn, batch_no: str, snapshot_version: int
) -> list[dict[str, Any]]:
    """快照资格版本之后产生的出货资格版本，逐条给出变化原因。"""
    rows = conn.execute(
        """SELECT version, eligible, locked_lots, trigger, updated_at
           FROM shipping_eligibility_version
           WHERE batch_no = ? AND version > ? ORDER BY version""",
        (batch_no, snapshot_version),
    ).fetchall()
    return [
        {
            "code": "ELIGIBILITY_VERSION_CHANGED",
            "message": (
                f"出货资格版本 v{r['version']}（{_eligibility_trigger_label(r['trigger'])}）"
                f"晚于快照版本 v{snapshot_version}，"
                + (
                    f"直接使用的锁定批号 {loads(r['locked_lots'])}，不可出货。"
                    if loads(r["locked_lots"])
                    else "该版本未记录直接使用的锁定批号。"
                )
            ),
            "from_version": snapshot_version if idx == 0 else rows[idx - 1]["version"],
            "to_version": r["version"],
            "eligible": bool(r["eligible"]),
            "locked_lots": loads(r["locked_lots"]),
            "trigger": r["trigger"],
            "updated_at": r["updated_at"],
        }
        for idx, r in enumerate(rows)
    ]


def _verification_batches(conn, verification_no: str) -> list[dict[str, Any]]:
    """组装逐批记录：裁决/出货资格的快照版本、当前版本、当前结论与变化原因。"""
    rows = conn.execute(
        """SELECT vb.position AS position, vb.batch_no AS batch_no,
                  vb.snapshot_version AS snapshot_version,
                  vb.snapshot_disposition AS snapshot_disposition,
                  vb.snapshot_actual AS snapshot_actual,
                  vb.snapshot_undeclared AS snapshot_undeclared,
                  vb.snapshot_reasons AS snapshot_reasons,
                  vb.snapshot_eligibility_version AS snapshot_eligibility_version,
                  vb.snapshot_eligible AS snapshot_eligible,
                  vb.snapshot_locked_lots AS snapshot_locked_lots,
                  b.line_id AS line_id, b.seq AS seq,
                  a.version AS current_version, a.disposition AS current_disposition,
                  a.actual AS current_actual, a.undeclared AS current_undeclared,
                  a.reasons AS current_reasons, a.decided_at AS current_decided_at,
                  se.version AS current_elig_version,
                  se.locked_lots AS current_locked_lots,
                  se.updated_at AS current_elig_updated_at
           FROM verification_batch vb
           JOIN batch b ON b.batch_no = vb.batch_no
           LEFT JOIN adjudication a ON a.batch_no = vb.batch_no
           LEFT JOIN shipping_eligibility se ON se.batch_no = vb.batch_no
           WHERE vb.verification_no = ?
           ORDER BY vb.position""",
        (verification_no,),
    ).fetchall()
    batches = []
    for r in rows:
        current_locked = loads(r["current_locked_lots"]) if r["current_locked_lots"] is not None else []
        current_eligible = (
            bool(r["current_disposition"] == "RELEASE" and not current_locked)
            if r["current_version"] is not None
            else None
        )
        snapshot_locked = loads(r["snapshot_locked_lots"])
        batches.append(
            {
                "position": r["position"],
                "batch_no": r["batch_no"],
                "line_id": r["line_id"],
                "seq": r["seq"],
                "snapshot": {
                    "version": r["snapshot_version"],
                    "disposition": r["snapshot_disposition"],
                    "actual_allergens": loads(r["snapshot_actual"]),
                    "undeclared_allergens": loads(r["snapshot_undeclared"]),
                    "reasons": loads(r["snapshot_reasons"]),
                    "shipping_eligibility": {
                        "version": r["snapshot_eligibility_version"],
                        "eligible": bool(r["snapshot_eligible"]),
                        "locked_lots": snapshot_locked,
                    },
                },
                "current": None
                if r["current_version"] is None
                else {
                    "version": r["current_version"],
                    "disposition": r["current_disposition"],
                    "actual_allergens": loads(r["current_actual"]),
                    "undeclared_allergens": loads(r["current_undeclared"]),
                    "reasons": loads(r["current_reasons"]),
                    "decided_at": r["current_decided_at"],
                    "shipping_eligibility": {
                        "version": r["current_elig_version"],
                        "eligible": current_eligible,
                        "locked_lots": current_locked,
                        "updated_at": r["current_elig_updated_at"],
                    },
                },
                "changed": r["current_version"] is not None
                and (
                    r["current_version"] != r["snapshot_version"]
                    or r["current_elig_version"] != r["snapshot_eligibility_version"]
                ),
                "change_reasons": _change_reasons(conn, r["batch_no"], r["snapshot_version"]),
                "eligibility_change_reasons": _eligibility_change_reasons(
                    conn, r["batch_no"], r["snapshot_eligibility_version"]
                ),
            }
        )
    return batches


def _confirmation_history(conn, verification_no: str) -> list[dict[str, Any]]:
    confirms = conn.execute(
        """SELECT id, confirmed_at FROM verification_confirmation
           WHERE verification_no = ? ORDER BY id""",
        (verification_no,),
    ).fetchall()
    history = []
    for c in confirms:
        crows = conn.execute(
            """SELECT position, batch_no, snapshot_version, snapshot_disposition,
                      snapshot_actual, snapshot_undeclared, snapshot_reasons,
                      snapshot_eligibility_version, snapshot_eligible, snapshot_locked_lots
               FROM verification_confirmation_batch
               WHERE confirmation_id = ? ORDER BY position""",
            (c["id"],),
        ).fetchall()
        history.append(
            {
                "confirmation_id": c["id"],
                "confirmed_at": c["confirmed_at"],
                "batches": [
                    {
                        "position": cr["position"],
                        "batch_no": cr["batch_no"],
                        "version": cr["snapshot_version"],
                        "disposition": cr["snapshot_disposition"],
                        "actual_allergens": loads(cr["snapshot_actual"]),
                        "undeclared_allergens": loads(cr["snapshot_undeclared"]),
                        "reasons": loads(cr["snapshot_reasons"]),
                        "shipping_eligibility": {
                            "version": cr["snapshot_eligibility_version"],
                            "eligible": bool(cr["snapshot_eligible"]),
                            "locked_lots": loads(cr["snapshot_locked_lots"]),
                        },
                    }
                    for cr in crows
                ],
            }
        )
    return history


def _verification_payload(conn, row, *, with_history: bool = True) -> dict[str, Any]:
    payload = {
        "verification_no": row["verification_no"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "confirmed_at": row["confirmed_at"],
        "batches": _verification_batches(conn, row["verification_no"]),
    }
    if with_history:
        payload["confirmations"] = _confirmation_history(conn, row["verification_no"])
    return payload


def _refresh_snapshots(conn, verification_no: str, batch_nos: list[str]) -> None:
    """按提交顺序用各批“当前裁决版本/结论与出货资格版本”重写逐批快照（草稿/待复核重新核验）。"""
    now = _now()
    conn.execute("DELETE FROM verification_batch WHERE verification_no = ?", (verification_no,))
    rows = conn.execute(
        f"""SELECT b.batch_no AS batch_no, a.version AS version, a.disposition AS disposition,
                   a.actual AS actual, a.undeclared AS undeclared, a.reasons AS reasons,
                   se.version AS eligibility_version, se.locked_lots AS locked_lots
            FROM batch b
            JOIN adjudication a ON a.batch_no = b.batch_no
            JOIN shipping_eligibility se ON se.batch_no = b.batch_no
            WHERE b.batch_no IN ({','.join('?' * len(batch_nos))})""",
        batch_nos,
    ).fetchall()
    by_no = {r["batch_no"]: r for r in rows}
    for position, batch_no in enumerate(batch_nos, start=1):
        a = by_no[batch_no]
        locked_lots = loads(a["locked_lots"])
        eligible = 0 if locked_lots or a["disposition"] != "RELEASE" else 1
        conn.execute(
            """INSERT INTO verification_batch
               (verification_no, batch_no, position, snapshot_version, snapshot_disposition,
                snapshot_actual, snapshot_undeclared, snapshot_reasons,
                snapshot_eligibility_version, snapshot_eligible, snapshot_locked_lots)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                verification_no,
                batch_no,
                position,
                a["version"],
                a["disposition"],
                a["actual"],
                a["undeclared"],
                a["reasons"],
                a["eligibility_version"],
                eligible,
                dumps(locked_lots),
            ),
        )
    conn.execute(
        "UPDATE verification SET status='DRAFT', updated_at=? WHERE verification_no=?",
        (now, verification_no),
    )
