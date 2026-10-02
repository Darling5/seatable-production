# -*- coding: utf-8 -*-
"""application/exec_plane/work_order.py — 工单与齐套开工前置（`G22`）。

`G22` 命题（`docs/domain-model.md` §5）：

> **齐套开工：开工 ⟹ 齐套 = 100% ∨ 显式缺料放行授权**

覆盖矩阵把它标为**半闭**，缺的那半边说得很清楚：

> 齐套判定 + 阻断已有，但**「开工」动作与工单实体不存在**，故「开工前置」无从挂载。

本模块补的正是这半边 —— **工单实体 + `release()` 前置**，把已有判据挂上去：

  · 齐套到货口径 **直接复用** `project_brain/evidence_ledger.py::is_full_arrival`
    （claim=full，或 claim=arrived 且实收 ≥ 总量）——
    部分到货**不算齐套**，这条口径不在本层重新定义；
  · 原因码 `partial_not_kitting` **直接复用** `project_brain/actions.py::R_PARTIAL_NOT_KITTING`
    的**同一个字符串** —— 同一条业务事实两个原因码是双口径漂移的起点。

## 两条分支，都必须留痕

    release()
      ├─ 无缺口                → 放行（`release_shortage_override` 为空）
      ├─ 有缺口 + 有效放行授权 → 放行，但把**授权人/授权号/理由**原样写进工单
      └─ 有缺口 + 无有效授权   → 拒绝，原因码报齐套状态
                                 （`partial_not_kitting` / `material_shortage`），
                                 并把**授权的判定结论**（`override` 三态）一并带出 ——
                                 「没给授权」与「给了但无效」必须能区分开。

「显式缺料放行授权」的校验是 fail-closed 的：授权对象缺 `approver` / 缺 `reason` /
`status != approved` / 授权范围与本工单不符 —— 一律**视为没有授权**。
缺料放行不是「点一下就过」，它必须能回答「谁批的、为什么批」。

## 为什么需求来自 BOM 而不是工单自带

齐套是「**按 BOM 展开后的物料需求**」与「**实际齐套到货**」的比对。
工单只写「生产什么、多少台」，需求由 `bom.explode()` 算 ——
否则工单上的物料清单就成了第二份 BOM，两份都能改，必然对不上。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Mapping, Optional, Sequence

from ..project_brain import actions as ACT
from ..project_brain import evidence_ledger as EV
from ..project_brain import schema as PBS
from . import bom as BOM
from . import schema as S

# ────────────────────────────────────────────────────────────────────
# 原因码
# ────────────────────────────────────────────────────────────────────
R_NO_REQUIREMENTS = "work_order_without_requirements"
R_BOM_STRUCTURE_INVALID = "work_order_bom_structure_invalid"
R_MATERIAL_SHORTAGE = "material_shortage"
R_WO_STATE_INVALID = "work_order_state_invalid"
# ★ 与二期同一个字符串，**不要另起一个说法**
R_PARTIAL_NOT_KITTING = ACT.R_PARTIAL_NOT_KITTING          # "partial_not_kitting"

# ── 缺料放行授权的**判定结论**（与上面的「开工前置原因码」不是同一条轴）
#
# ★ 2026-10-03 修正：初版把 `R_SHORTAGE_OVERRIDE_MISSING` / `R_OVERRIDE_INVALID`
#   摆在「原因码」里、写进了 `__all__` 与 `REASON_CN`，但
#   `check_release_precondition()` **从来不会返回它们**（有缺口时返回的是
#   `partial_not_kitting` / `material_shortage`）—— 也就是两个**永远取不到的值**。
#   「`__all__` 里有一个取不到的码」本身就是一种假指标。
#
#   现在把它们摆到正确的轴上：它们描述的是**授权对象的判定结论**（三态），
#   由 `override_verdict()` 返回、由 `release()` 原样带出。
#   这样既不丢信息（上层仍能机器区分「没给授权」与「给了但无效」），
#   也不再有任何取不到的原因码。
OV_MISSING = "shortage_override_missing"     # 压根没给授权
OV_INVALID = "shortage_override_invalid"     # 给了，但无效（谁能批/为什么批说不清）
OV_VALID = "valid"
OV_STATES = (OV_MISSING, OV_INVALID, OV_VALID)

REASON_CN = {
    R_NO_REQUIREMENTS: "工单没有可用的物料需求（缺已发布 BOM），无法判定齐套",
    R_BOM_STRUCTURE_INVALID: "BOM 结构不合规，不得据其下推工单",
    R_MATERIAL_SHORTAGE: "物料未齐套到货 —— 未齐套不得开工",
    R_WO_STATE_INVALID: "工单状态非法",
    R_PARTIAL_NOT_KITTING: "只有「部分到货」—— 部分到货不能开工（二期同一条口径）",
}

# 放行授权三态的人话（与 REASON_CN 分开 —— 两条轴不混在一张表里）
OV_CN = {
    OV_MISSING: "存在缺料且未提供「显式缺料放行授权」—— 不得开工",
    OV_INVALID: "缺料放行授权无效（缺授权人/理由，或状态非已批准，或范围不符）",
    OV_VALID: "授权有效",
}

OVERRIDE_ACTION = "release_shortage"        # 授权动作名（唯一取值，便于机器判定）
OVERRIDE_APPROVED = "approved"


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ────────────────────────────────────────────────────────────────────
# 实体构造
# ────────────────────────────────────────────────────────────────────
def build_work_order_row(*, project_id: str, parent_item_id: str, quantity: Any,
                         unit: str = "台", state: str = S.WO_DRAFT,
                         plan_id: str = "", bom_version: str = "",
                         owner: str = "", due_date: str = "", line: str = "",
                         note: str = "", run_id: str = "", snapshot_id: str = "",
                         now: Optional[_dt.datetime] = None, seq: int = 0) -> dict:
    """构造一张工单（MO / WO）。"""
    if state not in S.WO_STATES:
        raise ValueError("未知工单状态：%r（合法：%s）"
                         % (state, "|".join(S.WO_STATES)))
    ts = now or _now()
    from ..decision_support.alignment import make_unified
    return {
        "unified": make_unified(project_id=project_id, plan_id=plan_id,
                                run_id=run_id, snapshot_id=snapshot_id),
        "wo_id": S.new_id("work_order", now=ts, seq=seq),
        "project_id": project_id, "plan_id": plan_id,
        "parent_item_id": parent_item_id, "quantity": quantity, "unit": unit,
        "bom_version": bom_version, "owner": owner, "due_date": due_date,
        "line": line, "state": state, "state_cn": S.WO_STATE_CN[state],
        # 缺料放行留痕（无授权时为空 dict，字段恒存在，便于比对）
        "release_shortage_override": {},
        "shortages": [],
        "note": note,
        "created_at": ts.isoformat(timespec="seconds"),
    }


# ────────────────────────────────────────────────────────────────────
# 齐套判定
# ────────────────────────────────────────────────────────────────────
def _claim_full_gr(gr: Mapping[str, Any]) -> bool:
    """这条 GR 是否以**定性口径**声明「齐套到货」（claim=full）。"""
    return str(gr.get("claim_type") or "") == PBS.CLAIM_FULL


def arrived_kitting_by_item(gr_rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """按物料汇总**齐套到货**数量（**只汇总填了数量的那些**）。

    ★ 只认 `is_full_arrival`（齐套），部分到货不计入 —— 与二期
    `actions.check_close` 里 `AK_KITTING` 那一条完全同口径。
    已取消的 GR 不算数。

    ⚠️ `claim=full` 但**没填数量**的记录在这里汇不出来（没有数可加）。
    它**不是被丢弃** —— 按二期口径 `is_full_arrival` 已判它是齐套，
    所以它走 `claimed_full_items()` 那条**定性**通道，
    在 `compute_shortages(satisfied=...)` 里把该物料整条排除。
    两条通道**互斥**：填了数量的走这里，没填的走那边 —— 不会重复计入。
    """
    out: dict[str, float] = {}
    for gr in (gr_rows or []):
        if str(gr.get("state") or "") == S.GR_CANCELLED:
            continue
        if not EV.is_full_arrival(gr):
            continue
        item = str(gr.get("item_id") or "")
        if not item:
            continue
        qty = _num(gr.get("quantity_received"))
        if qty is None:
            qty = _num(gr.get("quantity"))
        if qty is None:
            continue
        out[item] = out.get(item, 0.0) + qty
    return out


def claimed_full_items(gr_rows: Sequence[Mapping[str, Any]]) -> set:
    """以**定性**方式声明齐套到货、且**没有数量可比**的物料集合。

    为什么必须单独有这条通道（2026-10-03 实测纠正）：

        二期 `is_full_arrival` 的口径是「claim=full **或** claim=arrived 且实收≥总量」。
        也就是说 `claim=full` **本身就是**齐套结论，不需要数量支撑。

        本模块初版只按「取数量 → 加总」一条路走：`claim=full` 且未填数量的记录
        因为取不到数而被跳过，于是需求侧算出「一项没到」—— 等于本层
        **自己加了一条「必须有数量」的新要求，把二期的口径换掉了**。
        那正是模块头声明的「同一条业务事实两种口径」这种病。

        修法不是「当成数量 0」也不是「猜一个数」（那都是编数据），
        而是**承认这条记录是定性齐套**，据二期口径把该物料从缺口计算里排除，
        并把「哪些物料是靠定性声明过的」原样带出给上层（`release()` 的
        `satisfied_by_claim`），**不隐藏**这个事实。
    """
    out = set()
    for gr in (gr_rows or []):
        if str(gr.get("state") or "") == S.GR_CANCELLED:
            continue
        if not _claim_full_gr(gr):
            continue
        item = str(gr.get("item_id") or "")
        if not item:
            continue
        if not EV.is_full_arrival(gr):     # 状态/口径兜底，与汇总侧同一判据
            continue
        qty = _num(gr.get("quantity_received"))
        if qty is None:
            qty = _num(gr.get("quantity"))
        if qty is not None:
            continue                       # 有数量 ⇒ 归定量通道，避免两条通道重复计入
        out.add(item)
    return out


def has_partial_arrival(gr_rows: Sequence[Mapping[str, Any]], item_id: str) -> bool:
    """该物料是否只有「部分到货」记录（用于区分「缺料」与「部分到货」两种原因）。"""
    for gr in (gr_rows or []):
        if str(gr.get("item_id") or "") != str(item_id or ""):
            continue
        if str(gr.get("state") or "") == S.GR_CANCELLED:
            continue
        if str(gr.get("claim_type") or "") == PBS.CLAIM_PARTIAL:
            return True
    return False


def compute_shortages(requirements: Mapping[str, float],
                      arrived: Mapping[str, float],
                      satisfied: Sequence[str] = ()) -> list[dict]:
    """算缺口。返回按缺口量倒序的列表（确定性排序）。

    ``satisfied`` 里列出的物料**整条排除** —— 那是 `claimed_full_items()`
    给出的「按二期定性口径已齐套」的物料。**不是漏算，是口径如此**。
    """
    skip = {str(x) for x in (satisfied or ())}
    out: list[dict] = []
    for item, need in (requirements or {}).items():
        if str(item) in skip:
            continue
        got = float((arrived or {}).get(item, 0.0))
        if got + 1e-9 < float(need):
            out.append({"item_id": item, "required": float(need), "arrived": got,
                        "shortage": round(float(need) - got, 6)})
    out.sort(key=lambda r: (-r["shortage"], r["item_id"]))
    return out


# ────────────────────────────────────────────────────────────────────
# 授权校验（fail-closed）
# ────────────────────────────────────────────────────────────────────
def override_verdict(approval: Optional[Mapping[str, Any]],
                     work_order: Mapping[str, Any]) -> tuple[str, str]:
    """判定「显式缺料放行授权」的结论，返回 ``(OV_*, 说明)``。

    三态：``OV_MISSING``（没给）/ ``OV_INVALID``（给了但无效）/ ``OV_VALID``。

    fail-closed：**只要有一项说不清，就当没有授权**。
    这照搬 `G7`（`router.py`）的「显式授权」范式与二期的授权闸门精神 ——
    缺料放行必须留下「谁批的、为什么批」，否则它就不是授权，是绕过。
    """
    if not approval:
        return OV_MISSING, "未提供授权"
    if str(approval.get("status") or "") != OVERRIDE_APPROVED:
        return OV_INVALID, "授权状态不是 approved（实际 %r）" % approval.get("status")
    if str(approval.get("action") or "") != OVERRIDE_ACTION:
        return OV_INVALID, ("授权动作不是 %s（实际 %r）"
                            % (OVERRIDE_ACTION, approval.get("action")))
    if not str(approval.get("approver") or "").strip():
        return OV_INVALID, "授权缺少授权人（approver）"
    if not str(approval.get("reason") or "").strip():
        return OV_INVALID, "授权缺少理由（reason）—— 讲不清为什么批就不是授权"
    wo_id = str(work_order.get("wo_id") or "")
    scope = str(approval.get("work_order_id") or "")
    if scope and scope != wo_id:
        return OV_INVALID, "授权范围是 %s，本工单是 %s" % (scope, wo_id)
    if not scope:
        pid = str(approval.get("project_id") or "")
        if pid and pid != str(work_order.get("project_id") or ""):
            return OV_INVALID, ("授权范围是项目 %s，本工单属于 %s"
                                % (pid, work_order.get("project_id")))
    return OV_VALID, ""


def validate_override(approval: Optional[Mapping[str, Any]],
                      work_order: Mapping[str, Any]) -> tuple[bool, str]:
    """`override_verdict` 的布尔包装：``(是否有效, 无效说明)``。"""
    state, why = override_verdict(approval, work_order)
    return state == OV_VALID, why


def override_trace(approval: Mapping[str, Any]) -> dict:
    """授权留痕：原样记下「谁批的、什么时候、为什么」。"""
    return {"approval_id": str(approval.get("approval_id") or ""),
            "approver": str(approval.get("approver") or ""),
            "reason": str(approval.get("reason") or ""),
            "action": OVERRIDE_ACTION,
            "granted_at": str(approval.get("granted_at") or "")}


# ────────────────────────────────────────────────────────────────────
# G22：开工前置
# ────────────────────────────────────────────────────────────────────
def check_release_precondition(
    work_order: Mapping[str, Any],
    *,
    bom_versions: Sequence[Mapping[str, Any]],
    bom_lines: Sequence[Mapping[str, Any]],
    gr_rows: Sequence[Mapping[str, Any]],
    approval: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, str, str]:
    """`G22` 齐套开工前置。返回 ``(是否允许, 原因码, 人话)``。

    判定顺序（先排除**结构性**问题，再判齐套 —— 顺序反了会把「BOM 本身有环」
    报成「缺料」，让人去补一批根本不需要的料）：

      1. BOM 结构不合规 → 拒绝（报 `work_order_bom_structure_invalid`）
      2. 展开得不到任何物料需求 → 拒绝（报 `work_order_without_requirements`）
      3. 无缺口 → **放行**
      4. 有缺口 + 有效放行授权 → **放行**（授权留痕写进工单）
      5. 有缺口 + 无授权 → 拒绝
    """
    if str(work_order.get("state") or "") not in S.WO_STATES:
        return False, R_WO_STATE_INVALID, REASON_CN[R_WO_STATE_INVALID]

    structure = BOM.validate_bom(bom_versions, bom_lines)
    if not structure["ok"]:
        return (False, R_BOM_STRUCTURE_INVALID,
                "%s：%s" % (REASON_CN[R_BOM_STRUCTURE_INVALID], structure["summary"]))

    qty = _num(work_order.get("quantity"))
    if qty is None or qty <= 0:
        return (False, R_NO_REQUIREMENTS,
                "工单数量缺失或非正数（%r），无法按 BOM 展开需求"
                % work_order.get("quantity"))

    exp = BOM.explode(bom_versions, bom_lines,
                      str(work_order.get("parent_item_id") or ""),
                      quantity=qty, version=str(work_order.get("bom_version") or ""))
    if exp.get("cycles"):
        return (False, R_BOM_STRUCTURE_INVALID,
                "%s（展开时发现环：%s）" % (REASON_CN[R_BOM_STRUCTURE_INVALID],
                                        "；".join(exp["cycles"][:3])))
    requirements = exp.get("requirements") or {}
    if not requirements:
        return False, R_NO_REQUIREMENTS, REASON_CN[R_NO_REQUIREMENTS]

    arrived = arrived_kitting_by_item(gr_rows)
    # claim=full 但没数量的那些：按二期定性口径已齐套，整条排除（见 claimed_full_items）
    by_claim = claimed_full_items(gr_rows)
    shortages = compute_shortages(requirements, arrived, satisfied=by_claim)
    if not shortages:
        return True, "", ("物料齐套到货，允许开工（%d 种物料全部满足%s）"
                          % (len(requirements),
                             "，其中 %d 种按「齐套到货」定性声明" % len(by_claim)
                             if by_claim else ""))

    state, why = override_verdict(approval, work_order)
    if state == OV_VALID:
        return True, "", ("存在缺料 %d 项，但持有显式缺料放行授权（授权人 %s）"
                          % (len(shortages), approval.get("approver")))

    # 区分「部分到货」与「完全没有到货」—— 二期对前者有专门口径，沿用之
    only_partial = all(has_partial_arrival(gr_rows, s["item_id"]) for s in shortages)
    code = R_PARTIAL_NOT_KITTING if only_partial else R_MATERIAL_SHORTAGE
    return (False, code,
            "%s（缺 %d 项，共缺 %s；放行授权 %s：%s）"
            % (REASON_CN[code], len(shortages),
               "、".join("%s×%g" % (s["item_id"], s["shortage"]) for s in shortages[:3]),
               state, why))


def release(work_order: Mapping[str, Any], *,
            bom_versions: Sequence[Mapping[str, Any]],
            bom_lines: Sequence[Mapping[str, Any]],
            gr_rows: Sequence[Mapping[str, Any]],
            approval: Optional[Mapping[str, Any]] = None,
            actor: str = "") -> dict:
    """尝试下达工单（开工）。**纯计算**：不改入参、不落库。

    返回 ``{allowed, code, message, next_state, patch, shortages}``；
    `patch` 是应当写回工单的字段增量（仅在 allowed 时非空）——
    落库由 `service.py` 走第一期授权闸门完成，本函数不碰存储。
    """
    allowed, code, msg = check_release_precondition(
        work_order, bom_versions=bom_versions, bom_lines=bom_lines,
        gr_rows=gr_rows, approval=approval)
    ov_state, ov_why = override_verdict(approval, work_order)

    qty = _num(work_order.get("quantity")) or 0.0
    exp = BOM.explode(bom_versions, bom_lines,
                      str(work_order.get("parent_item_id") or ""),
                      quantity=qty, version=str(work_order.get("bom_version") or ""))
    by_claim = claimed_full_items(gr_rows)
    shortages = compute_shortages(exp.get("requirements") or {},
                                  arrived_kitting_by_item(gr_rows),
                                  satisfied=by_claim)

    patch: dict[str, Any] = {}
    if allowed:
        patch = {"state": S.WO_RELEASED, "state_cn": S.WO_STATE_CN[S.WO_RELEASED],
                 "shortages": shortages,
                 "satisfied_by_claim": sorted(by_claim),
                 "release_shortage_override": (override_trace(approval)
                                               if shortages and approval else {}),
                 "released_by": actor,
                 "released_at": _now().isoformat(timespec="seconds")}
    return {"allowed": allowed, "code": code, "message": msg,
            "next_state": patch.get("state", ""), "patch": patch,
            "shortages": shortages, "requirements": exp.get("requirements") or {},
            "satisfied_by_claim": sorted(by_claim),
            "override": ov_state, "override_note": ov_why}


__all__ = [
    "R_NO_REQUIREMENTS", "R_BOM_STRUCTURE_INVALID", "R_MATERIAL_SHORTAGE",
    "R_WO_STATE_INVALID", "R_PARTIAL_NOT_KITTING", "REASON_CN",
    "OV_MISSING", "OV_INVALID", "OV_VALID", "OV_STATES", "OV_CN",
    "OVERRIDE_ACTION", "OVERRIDE_APPROVED",
    "build_work_order_row", "arrived_kitting_by_item", "claimed_full_items",
    "has_partial_arrival", "compute_shortages", "override_verdict",
    "validate_override", "override_trace", "check_release_precondition", "release",
]
