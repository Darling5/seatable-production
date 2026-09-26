# -*- coding: utf-8 -*-
"""application/project_brain/actions.py — 行动闭环：状态机 + 业务硬规则。

━━ 状态机 ━━
    候选 → 待确认 → 待执行 → 进行中 → 待验收 → 已关闭
    （任何在办状态 ⇄ 阻塞；任何非终态 → 已取消；已关闭/已取消 → 重开）

每次迁移都写一条**行动事件**（append-only），所以「谁在什么时候把它改成
什么、为什么」永远查得到。

━━ 四条不许讲人情的硬规则 ━━
  1. **关闭必须通过验收** —— 没有验收证据，`close` 一律拒绝；「我觉得做完了」
     不是验收。
  2. **「已发货」不算「已到货」** —— 需要到货才能关的行动，只有 shipped /
     in_transit 证据时拒绝关闭，报 ``shipped_not_arrived``。
  3. **「部分到货」不能关闭齐套行动** —— 齐套（kitting）只认 full 或
     「实收 ≥ 总量」的 arrived，partial 一律拒绝，报 ``partial_not_kitting``。
  4. **未完成事项跨天保留原期限** —— 滚动到新的一天时只累加 ``carry_over_count``
     与 ``overdue_days``，**绝不改 due_date**。期限是被承诺过的东西，
     不能因为隔了一夜就自己往后跑。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import ids
from . import schema as S
from . import evidence_ledger as EV

# ────────────────────────────────────────────────────────────────────
# 关闭失败原因码（机器可判、可测）
# ────────────────────────────────────────────────────────────────────
R_NEED_ACCEPTANCE = "need_acceptance_evidence"
R_ACCEPTANCE_REJECTED = "acceptance_rejected"
R_CRITERIA_NOT_MET = "acceptance_criteria_not_met"
R_SHIPPED_NOT_ARRIVED = "shipped_not_arrived"
R_PARTIAL_NOT_KITTING = "partial_not_kitting"
R_NO_EVIDENCE_LINK = "no_evidence_linked"
R_NOT_IN_ACCEPTANCE = "not_pending_acceptance"

REASON_CN = {
    R_NEED_ACCEPTANCE: "缺少验收证据（关闭必须通过验收）",
    R_ACCEPTANCE_REJECTED: "验收结论为不通过",
    R_CRITERIA_NOT_MET: "验收标准未逐条满足",
    R_SHIPPED_NOT_ARRIVED: "仅有「已发货/在途」证据 —— 已发货不等于已到货",
    R_PARTIAL_NOT_KITTING: "仅有「部分到货」证据 —— 部分到货不能关闭齐套行动",
    R_NO_EVIDENCE_LINK: "行动未关联任何证据",
    R_NOT_IN_ACCEPTANCE: "行动当前不在「待验收」状态",
}


# ────────────────────────────────────────────────────────────────────
# 构造
# ────────────────────────────────────────────────────────────────────
def action_idem_key(project_id: str, title: str) -> str:
    """行动去重键：同项目 + 归一化目标 ⇒ 同一件事（重复采集不重复建行动）。

    只归一化标题文本，不含日期 —— 因为「下周一发货」和「明天发货」是同一件事
    的两次说法，靠不同日期建两条行动正是要避免的重复。
    """
    norm = re.sub(r"[\s，。！？、；：\"'（）()\[\]【】…~·\-—]+", "", str(title or "")).lower()
    return "ACTK-" + hashlib.sha1(
        ("%s|%s" % (project_id, norm)).encode("utf-8")).hexdigest()[:24]


def build_action_row(
    *,
    project_id: str,
    title: str,
    kind: str = S.AK_GENERAL,
    owner: str = "",
    due_date: str = "",
    acceptance_criteria: Any = "",
    plan_id: str = "",
    evidence_id: str = "",
    decision_id: str = "",
    run_id: str = "",
    snapshot_id: str = "",
    status: str = S.ST_CANDIDATE,
    now: Optional[_dt.datetime] = None,
    seq: int = 0,
) -> dict:
    """构造一条行动行（未落盘）。"""
    if kind not in S.ACTION_KINDS:
        raise ValueError("未知行动类型：%r（合法：%s）"
                         % (kind, "|".join(S.ACTION_KINDS)))
    if status not in S.ACTION_STATES:
        raise ValueError("未知行动状态：%r" % status)
    now = now or _dt.datetime.now()
    label = now.isoformat(timespec="seconds")
    return {
        # ── 统一字段 ──
        "project_id": project_id,
        "plan_id": plan_id,
        "action_id": ids.new_id("action", now=now, seq=seq),
        "evidence_id": evidence_id,
        "decision_id": decision_id,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "version": 1,
        # ── 业务 ──
        "title": str(title or "").strip(),
        "kind": kind,
        "kind_cn": S.ACTION_KIND_CN[kind],
        "owner": owner,
        "due_date": due_date,
        "due_date_original": due_date,   # 原期限一旦写下就不再改
        "acceptance_criteria": acceptance_criteria,
        "status": status,
        "status_cn": S.ACTION_STATE_CN[status],
        "blocked_reason": "",
        "blocked_owner": "",
        "blocked_since": "",
        "prev_state": "",
        "carry_over_count": 0,
        "last_rollover_date": "",
        "overdue_days": 0,
        "evidence_ids": [evidence_id] if evidence_id else [],
        "created_from": evidence_id,
        "created_at": label,
        "updated_at": label,
        "last_transition_at": label,
        "reopen_count": 0,
        "cancel_reason": "",
        "idem_key": action_idem_key(project_id, title),
    }


def build_action_event(*, action: Mapping[str, Any], from_state: str, to_state: str,
                       reason: str = "", actor: str = "", evidence_id: str = "",
                       run_id: str = "", snapshot_id: str = "",
                       now: Optional[_dt.datetime] = None, seq: int = 0) -> dict:
    """构造一条行动迁移事件（append-only 留痕）。"""
    now = now or _dt.datetime.now()
    return {
        "event_id": ids.new_id("event", now=now, seq=seq),
        "project_id": action.get("project_id", ""),
        "plan_id": action.get("plan_id", ""),
        "action_id": action.get("action_id", ""),
        "evidence_id": evidence_id,
        "decision_id": "",
        "run_id": run_id or action.get("run_id", ""),
        "snapshot_id": snapshot_id or action.get("snapshot_id", ""),
        "version": 1,
        "from_state": from_state,
        "from_state_cn": S.ACTION_STATE_CN.get(from_state, from_state),
        "to_state": to_state,
        "to_state_cn": S.ACTION_STATE_CN.get(to_state, to_state),
        "reason": reason,
        "actor": actor,
        "created_at": now.isoformat(timespec="seconds"),
    }


# ────────────────────────────────────────────────────────────────────
# 状态机
# ────────────────────────────────────────────────────────────────────
def can_transition(old: str, new: str) -> bool:
    return new in S.ACTION_TRANSITIONS.get(str(old), set())


def transition_error(old: str, new: str) -> str:
    if new not in S.ACTION_STATES:
        return "未知目标状态：%r" % new
    if old not in S.ACTION_STATES:
        return "未知当前状态：%r" % old
    return "非法迁移：%s(%s) → %s(%s)" % (
        S.ACTION_STATE_CN.get(old, old), old, S.ACTION_STATE_CN.get(new, new), new)


def is_open(row: Mapping[str, Any]) -> bool:
    return str(row.get("status") or "") in S.OPEN_STATES


# ────────────────────────────────────────────────────────────────────
# 关闭前的业务校验（四条硬规则落地点）
# ────────────────────────────────────────────────────────────────────
def check_close(action: Mapping[str, Any],
                evidence_rows: Sequence[Mapping[str, Any]]) -> tuple[bool, str, str]:
    """判断这条行动能不能关。返回 ``(是否允许, 原因码, 人话说明)``。

    ``evidence_rows`` 必须是**已经按 action_id 过滤过的**该行动证据。
    """
    cur = str(action.get("status") or "")
    if cur != S.ST_PENDING_ACCEPTANCE:
        return False, R_NOT_IN_ACCEPTANCE, REASON_CN[R_NOT_IN_ACCEPTANCE]

    ev = [dict(e) for e in evidence_rows if str(e.get("status") or "active") == "active"]
    if not ev:
        return False, R_NO_EVIDENCE_LINK, REASON_CN[R_NO_EVIDENCE_LINK]

    kind = str(action.get("kind") or S.AK_GENERAL)

    # 规则 3：齐套行动只认「齐套到货」——部分到货一律不关
    if kind == S.AK_KITTING:
        if not any(EV.is_full_arrival(e) for e in ev):
            partials = [e for e in ev if str(e.get("claim_type")) == S.CLAIM_PARTIAL]
            if partials:
                return False, R_PARTIAL_NOT_KITTING, REASON_CN[R_PARTIAL_NOT_KITTING]
            return False, R_SHIPPED_NOT_ARRIVED, REASON_CN[R_SHIPPED_NOT_ARRIVED]

    # 规则 2：需要真到货的行动 —— 只有 shipped / in_transit 不放行
    if kind in S.NEEDS_ARRIVAL_KINDS and kind != S.AK_KITTING:
        if not any(EV.is_arrival_evidence(e) for e in ev):
            if any(EV.is_shipped_only(e) for e in ev):
                return False, R_SHIPPED_NOT_ARRIVED, REASON_CN[R_SHIPPED_NOT_ARRIVED]
            return False, R_NEED_ACCEPTANCE, REASON_CN[R_NEED_ACCEPTANCE]

    # 规则 1：关闭必须通过验收
    acc = [e for e in ev if EV.is_acceptance_evidence(e)]
    if not acc:
        return False, R_NEED_ACCEPTANCE, REASON_CN[R_NEED_ACCEPTANCE]
    accepted = [e for e in acc if str(e.get("claim_type") or "") == S.CLAIM_ACCEPTED
                or e.get("criteria_met") is True]
    if not accepted:
        return False, R_ACCEPTANCE_REJECTED, REASON_CN[R_ACCEPTANCE_REJECTED]

    # 验收标准逐条满足：写明了标准就必须显式确认
    criteria = action.get("acceptance_criteria")
    if _has_criteria(criteria):
        ok = any(str(e.get("claim_type") or "") == S.CLAIM_ACCEPTED
                 and e.get("criteria_met") is True for e in acc)
        if not ok:
            return False, R_CRITERIA_NOT_MET, REASON_CN[R_CRITERIA_NOT_MET]

    return True, "", "验收证据充分，允许关闭"


def _has_criteria(criteria: Any) -> bool:
    if criteria is None:
        return False
    if isinstance(criteria, (list, tuple, set)):
        return any(str(c).strip() for c in criteria)
    return bool(str(criteria).strip())


# ────────────────────────────────────────────────────────────────────
# 跨天滚动：期限不动，只累计逾期
# ────────────────────────────────────────────────────────────────────
def rollover_payload(action: Mapping[str, Any], as_of_date: str) -> Optional[dict]:
    """给出某条未完结行动的跨天更新载荷；不需要滚动时返回 ``None``。

    关键：``due_date`` **不出现在载荷里**。原期限保持不变，只累加
    ``carry_over_count`` / ``overdue_days``，并把 ``due_date_original`` 补上。
    """
    if not is_open(action):
        return None
    today = _parse_date(as_of_date)
    if today is None:
        return None
    last = _parse_date(action.get("last_rollover_date"))
    if last is not None and last >= today:
        return None       # 今天已经滚过，重复调用不叠加（幂等）

    payload: dict[str, Any] = {
        "last_rollover_date": today.isoformat(),
        "carry_over_count": int(action.get("carry_over_count") or 0) + 1,
    }
    if not str(action.get("due_date_original") or "").strip():
        payload["due_date_original"] = action.get("due_date") or ""
    due = _parse_date(action.get("due_date"))
    if due is not None:
        payload["overdue_days"] = max(0, (today - due).days)
    else:
        payload["overdue_days"] = int(action.get("overdue_days") or 0)
    return payload


def _parse_date(value: Any) -> Optional[_dt.date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


# 公开别名：上下文层与第三期都会用到，不以下划线名对外
parse_date = _parse_date
has_criteria = _has_criteria


# ────────────────────────────────────────────────────────────────────
# 提醒（去重 + 失败恢复）
# ────────────────────────────────────────────────────────────────────
RK_OVERDUE = "overdue"
RK_DUE_TODAY = "due_today"
RK_BLOCKED = "blocked"
RK_PENDING_ACCEPTANCE = "pending_acceptance"
RK_UNASSIGNED = "unassigned"

REMINDER_KINDS = (RK_OVERDUE, RK_DUE_TODAY, RK_BLOCKED,
                  RK_PENDING_ACCEPTANCE, RK_UNASSIGNED)
REMINDER_KIND_CN = {
    RK_OVERDUE: "已逾期", RK_DUE_TODAY: "今日到期", RK_BLOCKED: "被阻塞",
    RK_PENDING_ACCEPTANCE: "待验收", RK_UNASSIGNED: "未指派负责人",
}

RS_PENDING = "pending"
RS_SENT = "sent"
RS_FAILED = "failed"


def reminder_key(action_id: str, kind: str, due_date: str = "") -> str:
    """提醒去重键：同一行动 + 同一原因 + 同一期限 ⇒ 同一条提醒。

    带 due_date 是刻意的：期限改了应该重新提醒一次，但**没改就绝不重复推**。
    """
    return "RMK-%s-%s-%s" % (action_id, kind, due_date or "none")


def reminder_candidates(action: Mapping[str, Any], as_of_date: str) -> list[dict]:
    """算出某条行动在当前日期下应该产生哪些提醒（纯计算，不去重）。"""
    if not is_open(action):
        return []
    today = _parse_date(as_of_date)
    if today is None:
        return []
    due = _parse_date(action.get("due_date"))
    aid = str(action.get("action_id") or "")
    status = str(action.get("status") or "")
    out: list[dict] = []

    if status == S.ST_BLOCKED:
        out.append(_cand(aid, RK_BLOCKED, action.get("due_date", ""), action))
    if status == S.ST_PENDING_ACCEPTANCE:
        out.append(_cand(aid, RK_PENDING_ACCEPTANCE, action.get("due_date", ""), action))
    if due is not None:
        if due < today:
            out.append(_cand(aid, RK_OVERDUE, action.get("due_date", ""), action))
        elif due == today:
            out.append(_cand(aid, RK_DUE_TODAY, action.get("due_date", ""), action))
    if not str(action.get("owner") or "").strip() and status not in (
            S.ST_CANDIDATE, S.ST_PENDING_CONFIRM):
        out.append(_cand(aid, RK_UNASSIGNED, action.get("due_date", ""), action))
    return out


def _cand(action_id: str, kind: str, due_date: str, action: Mapping[str, Any]) -> dict:
    key = reminder_key(action_id, kind, due_date)
    return {
        "reminder_key": key,
        "action_id": action_id,
        "project_id": action.get("project_id", ""),
        "kind": kind,
        "kind_cn": REMINDER_KIND_CN.get(kind, kind),
        "due_date": due_date,
        "owner": action.get("owner", ""),
        "title": action.get("title", ""),
        "status": RS_PENDING,
    }


__all__ = ["can_transition", "transition_error", "is_open", "check_close",
           "build_action_row", "build_action_event", "action_idem_key",
           "rollover_payload", "reminder_candidates", "reminder_key",
           "parse_date", "has_criteria",
           "R_NEED_ACCEPTANCE", "R_ACCEPTANCE_REJECTED", "R_CRITERIA_NOT_MET",
           "R_SHIPPED_NOT_ARRIVED", "R_PARTIAL_NOT_KITTING", "R_NO_EVIDENCE_LINK",
           "R_NOT_IN_ACCEPTANCE", "REASON_CN", "S"]
