# -*- coding: utf-8 -*-
"""application/exec_plane/procurement.py — 采购执行链：PR → PO → GR → 付款。

本模块落地 `G23`：**付款 ⟹ 存在到货验收记录**（`docs/domain-model.md` §5、覆盖矩阵 B 段）。

## 这条链上的实体怎么分家

    PR  请购单        —— 本层新建（`purchase_requisition`）
    PO  采购订单      —— **复用第二期 L_proj 的 `purchase_order` 对象**，不另建！
                         同一个现实对象在两个平面各有一个实体，就变成「两处都能改」，
                         而 `decision_support/alignment.py` 的宗旨正是「同一实体 ID 跨层对话」。
    GR  到货验收记录  —— 本层新建（`goods_receipt`）：到货 + 验收两条口径合一
    付款单            —— 本层新建（`payment`）：必须挂 GR

## 为什么到货与验收合成一条记录

`project_brain/evidence_ledger.py` 已经把两条口径都判好了
（`is_arrival_evidence` / `is_acceptance_evidence`），且明确钉死
「**已发货 ≠ 已到货**」「**部分到货不能算齐套**」。本层**直接复用这些判据**，
不重新定义一套 —— 同一条事实两种口径正是 E12 那类漂移的病根。

## G23 的实现口径（写清楚，便于审计）

命题原文只要求「存在到货验收记录」。本模块按**「存在**覆盖该笔付款的**有效**到货验收记录」**
实现，即一条 GR 要同时满足：

  1. 属于**同一张 PO**（拿 A 单的收货去付 B 单的钱不算「存在」）；
  2. **真的到货**且**齐套**（`is_full_arrival`：claim=full，或实收 ≥ 总量）；
     —— 部分到货不行（`partial_not_kitting` 的同一条道理）；
  3. **验收通过**（state=accepted）。

    ★ 这是对原命题的**收紧**而非另立不变量：不覆盖本笔付款的 GR，
      本来就不构成「本笔付款的前置记录」。数量判定单独放在
      `check_payment_amount()` 里，且**「判不了」必须显式可见**（见该函数说明）。

## 不做什么

  · 不判断金额单调链（`Σ回款 ≤ 应收 ≤ 开票 ≤ 合同额`）—— 那是 `G17`，属阶段二；
  · 不做三账对账 —— `G27`；
  · 不写任何真实业务库；落库走 `service.py`（复用第一期授权闸门）。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Mapping, Optional, Sequence

from .. import contracts as C
from ..project_brain import evidence_ledger as EV
from ..project_brain import schema as PBS
from . import schema as S

# ────────────────────────────────────────────────────────────────────
# 原因码（机器可判、可测；与二期 actions.py 同款范式）
# ────────────────────────────────────────────────────────────────────
R_NO_PO = "payment_without_po"
R_PO_ID_INVALID = "purchase_order_id_not_canonical"
R_NO_GOODS_RECEIPT = "no_goods_receipt"
R_GR_CANCELLED = "goods_receipt_cancelled"
R_GR_REJECTED = "goods_receipt_rejected"
R_GR_NOT_ARRIVED = "goods_receipt_not_arrived"
R_GR_PARTIAL = "goods_receipt_partial_only"
R_GR_NOT_ACCEPTED = "goods_receipt_not_accepted"
R_GR_QUANTITY_INSUFFICIENT = "goods_receipt_quantity_insufficient"

REASON_CN = {
    R_NO_PO: "付款单没有挂采购订单 —— 付款必须能追溯到采购订单",
    R_PO_ID_INVALID: "采购订单号不是跨层规范形态（规范：PUR-YYYYMMDD-XXXX）",
    R_NO_GOODS_RECEIPT: "该采购订单没有任何到货验收记录 —— 无 GR 不得付款",
    R_GR_CANCELLED: "该采购订单的到货验收记录已全部取消",
    R_GR_REJECTED: "到货验收未通过（已拒收）—— 不得付款",
    R_GR_NOT_ARRIVED: "只有「已发货/在途」记录，货未到 —— 已发货不等于已到货",
    R_GR_PARTIAL: "只有「部分到货」记录，未齐套 —— 部分到货不足以付款",
    R_GR_NOT_ACCEPTED: "货已到但尚未验收通过 —— 未验收不得付款",
    R_GR_QUANTITY_INSUFFICIENT: "已验收数量小于本笔付款数量 —— 不得超付",
}

# 每条 GR 的「可付款性」分级。数值只用于取**最优**那条来决定报什么原因，
# 不参与任何金额计算；顺序即业务优先级。
RK_PAYABLE = 5          # 验收通过 + 齐套到货
RK_NOT_ACCEPTED = 4     # 齐套到货，待验收
RK_PARTIAL = 3          # 部分到货
RK_NOT_ARRIVED = 2      # 只有发货/在途
RK_REJECTED = 1         # 验收不通过（已拒收）
RK_CANCELLED = 0        # 已取消

_RANK_REASON = {
    RK_NOT_ACCEPTED: R_GR_NOT_ACCEPTED,
    RK_PARTIAL: R_GR_PARTIAL,
    RK_NOT_ARRIVED: R_GR_NOT_ARRIVED,
    RK_REJECTED: R_GR_REJECTED,
    RK_CANCELLED: R_GR_CANCELLED,
}


def _now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _num(value: Any) -> Optional[float]:
    """尽量转成数字；**转不了返回 None**（不拿 0 冒充「数量为零」）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ────────────────────────────────────────────────────────────────────
# 实体构造
# ────────────────────────────────────────────────────────────────────
def build_pr_row(*, project_id: str, item_id: str, quantity: Any,
                 unit: str = "", required_date: str = "", owner: str = "",
                 reason: str = "", state: str = S.PR_DRAFT,
                 plan_id: str = "", run_id: str = "", snapshot_id: str = "",
                 now: Optional[_dt.datetime] = None, seq: int = 0) -> dict:
    """构造一条请购单（PR）。"""
    if state not in S.PR_STATES:
        raise ValueError("未知请购单状态：%r" % state)
    ts = (now or _dt.datetime.now())
    return {
        "unified": _unified(project_id, plan_id, run_id, snapshot_id),
        "pr_id": S.new_id("purchase_requisition", now=ts, seq=seq),
        "project_id": project_id, "plan_id": plan_id,
        "item_id": item_id, "quantity": quantity, "unit": unit,
        "required_date": required_date, "owner": owner, "reason": reason,
        "state": state, "state_cn": S.PR_STATE_CN[state],
        "converted_po_id": "", "created_at": ts.isoformat(timespec="seconds"),
    }


def build_gr_row(*, po_id: str, project_id: str, item_id: str = "",
                 claim_type: str, quantity_received: Any = "",
                 quantity_ordered: Any = "", quantity_accepted: Any = "",
                 unit: str = "", state: str = S.GR_ARRIVED,
                 gr_id: str = "", received_at: str = "", accepted_at: str = "",
                 evidence_id: str = "", note: str = "",
                 plan_id: str = "", run_id: str = "", snapshot_id: str = "",
                 now: Optional[_dt.datetime] = None, seq: int = 0) -> dict:
    """构造一条到货验收记录（GR）。

    ``claim_type`` 必须来自二期的 `CLAIM_TYPES`（本层不另立一套口径）；
    ``quantity_received / quantity_ordered`` 用于齐套判定
    （`evidence_ledger.is_full_arrival` 在 claim=arrived 时要求实收 ≥ 总量）。
    """
    if claim_type not in PBS.CLAIM_TYPES:
        raise ValueError("未知到货口径：%r（合法：%s）"
                         % (claim_type, "|".join(PBS.CLAIM_TYPES)))
    if state not in S.GR_STATES:
        raise ValueError("未知 GR 状态：%r" % state)
    ts = (now or _dt.datetime.now())
    row = {
        "unified": _unified(project_id, plan_id, run_id, snapshot_id),
        "gr_id": gr_id or S.new_id("goods_receipt", now=ts, seq=seq),
        "project_id": project_id, "plan_id": plan_id,
        "po_id": po_id, "item_id": item_id,
        "claim_type": claim_type, "claim_cn": PBS.CLAIM_CN.get(claim_type, claim_type),
        "quantity": quantity_received,          # 与 evidence 同名字段，便于复用判据
        "quantity_total": quantity_ordered,     # 同上
        "quantity_received": quantity_received,
        "quantity_ordered": quantity_ordered,
        "quantity_accepted": quantity_accepted,
        "unit": unit, "state": state, "state_cn": S.GR_STATE_CN[state],
        "received_at": received_at, "accepted_at": accepted_at,
        "evidence_id": evidence_id, "note": note,
        "created_at": ts.isoformat(timespec="seconds"),
    }
    return row


def build_payment_row(*, po_id: str, project_id: str, gr_id: str = "",
                      amount: Any = "", quantity: Any = "", currency: str = "CNY",
                      state: str = S.PAY_DRAFT, payee: str = "", note: str = "",
                      approval_id: str = "", plan_id: str = "", run_id: str = "",
                      snapshot_id: str = "",
                      now: Optional[_dt.datetime] = None, seq: int = 0) -> dict:
    """构造一条付款单。``gr_id`` 可留空 —— 校验会据此报 `no_goods_receipt`。"""
    if state not in S.PAY_STATES:
        raise ValueError("未知付款单状态：%r" % state)
    ts = (now or _dt.datetime.now())
    return {
        "unified": _unified(project_id, plan_id, run_id, snapshot_id),
        "payment_id": S.new_id("payment", now=ts, seq=seq),
        "project_id": project_id, "plan_id": plan_id,
        "po_id": po_id, "gr_id": gr_id,
        "amount": amount, "quantity": quantity, "currency": currency,
        "state": state, "state_cn": S.PAY_STATE_CN[state],
        "payee": payee, "note": note, "approval_id": approval_id,
        "created_at": ts.isoformat(timespec="seconds"),
    }


def _unified(project_id: str, plan_id: str, run_id: str, snapshot_id: str) -> dict:
    """八个统一字段（G29：三层共用一套关联键，不引入新名称映射）。"""
    from ..decision_support.alignment import make_unified
    return make_unified(project_id=project_id, plan_id=plan_id,
                        run_id=run_id, snapshot_id=snapshot_id)


# ────────────────────────────────────────────────────────────────────
# GR 口径判定（复用二期判据，不另立一套）
# ────────────────────────────────────────────────────────────────────
def is_arrived(gr: Mapping[str, Any]) -> bool:
    """这条 GR 是否证明「货真的到了」——直接复用二期 `is_arrival_evidence`。"""
    return EV.is_arrival_evidence(gr)


def is_kitting(gr: Mapping[str, Any]) -> bool:
    """是否齐套到货 —— 复用二期 `is_full_arrival`（部分到货不算）。"""
    return EV.is_full_arrival(gr)


def rank_of(gr: Mapping[str, Any]) -> int:
    """给一条 GR 打「可付款性」等级，用于选出**最接近可付**的那条来报原因。"""
    if str(gr.get("state") or "") == S.GR_CANCELLED:
        return RK_CANCELLED
    if str(gr.get("state") or "") == S.GR_REJECTED:
        return RK_REJECTED
    if is_kitting(gr):
        return RK_PAYABLE if str(gr.get("state") or "") == S.GR_ACCEPTED \
            else RK_NOT_ACCEPTED
    if is_arrived(gr):
        return RK_PARTIAL
    return RK_NOT_ARRIVED


def grs_of_po(gr_rows: Sequence[Mapping[str, Any]], po_id: str) -> list[dict]:
    """取该采购订单下的全部 GR（按创建时间稳定排序）。"""
    out = [dict(g) for g in (gr_rows or [])
           if str(g.get("po_id") or "") == str(po_id or "")]
    out.sort(key=lambda g: (str(g.get("created_at") or ""), str(g.get("gr_id") or "")))
    return out


# ────────────────────────────────────────────────────────────────────
# G23：付款前置
# ────────────────────────────────────────────────────────────────────
def check_payment_precondition(
    payment: Mapping[str, Any],
    gr_rows: Sequence[Mapping[str, Any]],
    *,
    require_canonical_po_id: bool = True,
) -> tuple[bool, str, str]:
    """`G23` 付款前置：付款 ⟹ 存在**覆盖该笔付款的**到货验收记录。

    返回 ``(是否允许, 原因码, 人话说明)``；允许时原因码为 ``""``。
    与二期 `actions.check_close` 同款三元组，便于上层统一渲染。

    判定是 **fail-closed** 的：拿不准一律不放行，并给出机器可判的原因码。
    """
    po_id = str(payment.get("po_id") or "").strip()
    if not po_id:
        return False, R_NO_PO, REASON_CN[R_NO_PO]
    if require_canonical_po_id and not C.is_canonical_object_id("purchase_order", po_id):
        # 采购订单是**跨层共用**的实体 ID，形态不对就无法与 L_proj 对上账。
        # 这里用**严格**判定（不认历史两字母形态）：`PO-…` 已被 G29 废弃，
        # 拿它做**新**付款的引用属于新写入一个新形态 —— 必须挡住，
        # 否则 R_PO_ID_INVALID 这条原因码永远取不到（死分支）。
        # 读旧数据仍走 `validate_object_id`（规范 ∪ 历史）。
        return False, R_PO_ID_INVALID, "%s（实际：%r）" % (REASON_CN[R_PO_ID_INVALID], po_id)

    grs = grs_of_po(gr_rows, po_id)
    if not grs:
        return False, R_NO_GOODS_RECEIPT, REASON_CN[R_NO_GOODS_RECEIPT]

    # 逐条打分，取最高的那条决定结论 —— 这样「已有一个可付的 GR」不会被
    # 另一个坏 GR 连坐，而「全都不合格」时报的是最接近可付的那个原因。
    best = max(grs, key=rank_of)
    rank = rank_of(best)
    if rank == RK_PAYABLE:
        return True, "", "存在验收通过的齐套到货记录（%s）" % best.get("gr_id", "")
    return False, _RANK_REASON[rank], REASON_CN[_RANK_REASON[rank]]


def check_payment_amount(
    payment: Mapping[str, Any],
    gr_rows: Sequence[Mapping[str, Any]],
) -> dict:
    """数量口径：本笔付款数量是否被已验收数量覆盖。

    返回 ``{checked, ok, code, message}``。**`checked=False` 表示「判不了」**——
    付款单没写数量、或 GR 没写已验收数量时不猜，直接标记为未判（上游据此决定
    是否要求补数据），**绝不静默当成通过**。

    ★ 本函数**只看同一张 PO 下的 GR 合计**，不跨 PO、不与历史付款累计比对：
      「累计付款 ≤ 累计验收」属金额单调链（`G17`，阶段二），本批不做。
      调用方若已按 PO 汇总过历史付款，请自行扣减后再传入。
    """
    pay_qty = _num(payment.get("quantity"))
    if pay_qty is None:
        return {"checked": False, "ok": True, "code": "",
                "message": "付款单未填数量 —— 数量覆盖未判定"}
    po_id = str(payment.get("po_id") or "")
    accepted = [_num(g.get("quantity_accepted")) for g in grs_of_po(gr_rows, po_id)
                if str(g.get("state") or "") == S.GR_ACCEPTED]
    have = [a for a in accepted if a is not None]
    if not have:
        return {"checked": False, "ok": True, "code": "",
                "message": "该采购订单下没有填写「已验收数量」的记录 —— 数量覆盖未判定"}
    total = sum(have)
    if pay_qty > total:
        return {"checked": True, "ok": False, "code": R_GR_QUANTITY_INSUFFICIENT,
                "message": "%s（本笔 %s > 已验收合计 %s）"
                           % (REASON_CN[R_GR_QUANTITY_INSUFFICIENT], pay_qty, total)}
    return {"checked": True, "ok": True, "code": "",
            "message": "已验收合计 %s ≥ 本笔付款 %s" % (total, pay_qty)}


def can_pay(payment: Mapping[str, Any],
            gr_rows: Sequence[Mapping[str, Any]]) -> dict:
    """合并两条判定，给上层一个完整结论。

    **「判不了」要看得见**：``amount.checked=False`` 会原样带在结果里，
    不会被折叠成「通过」。
    """
    allowed, code, msg = check_payment_precondition(payment, gr_rows)
    amount = check_payment_amount(payment, gr_rows)
    blocked = (not allowed) or (amount["checked"] and not amount["ok"])
    return {
        "allowed": not blocked,
        "code": code or (amount["code"] if (amount["checked"] and not amount["ok"]) else ""),
        "message": (msg if not allowed
                    else (amount["message"] if (amount["checked"] and not amount["ok"])
                          else "允许付款")),
        "precondition": {"allowed": allowed, "code": code, "message": msg},
        "amount": amount,
    }


__all__ = [
    "R_NO_PO", "R_PO_ID_INVALID", "R_NO_GOODS_RECEIPT", "R_GR_CANCELLED",
    "R_GR_REJECTED", "R_GR_NOT_ARRIVED", "R_GR_PARTIAL", "R_GR_NOT_ACCEPTED",
    "R_GR_QUANTITY_INSUFFICIENT", "REASON_CN",
    "RK_PAYABLE", "RK_NOT_ACCEPTED", "RK_PARTIAL", "RK_NOT_ARRIVED",
    "RK_REJECTED", "RK_CANCELLED",
    "build_pr_row", "build_gr_row", "build_payment_row",
    "is_arrived", "is_kitting", "rank_of", "grs_of_po",
    "check_payment_precondition", "check_payment_amount", "can_pay",
]
