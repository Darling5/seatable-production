# -*- coding: utf-8 -*-
"""application/decision_support/schedule.py — 交付依赖：前置关系 → CPM → 关键路径。

口径先钉死，三条：

1. **三种日期不混用。**
   `contract_date`（合同日期，对外承诺）≠ `internal_plan_date`（内部计划日期）
   ≠ `forecast_date`（预测日期）。本模块只**产出** forecast，
   contract / internal 一律**原样读出**，绝不回写、绝不覆盖。
   一个常见的坏味道是「预测晚了，就把计划日期改掉让它看起来没晚」——
   这里从结构上杜绝：本模块没有任何写入 contract/internal 的代码路径。

2. **关键路径按「仅前置关系」口径算。** 不含资源约束。
   资源约束下的进度见 `resources.py`，两者分开命名（`cpm` / `resource_feasible`），
   因为把它们混成一句「关键路径」就是过度声称。

3. **不知道就说不知道。** 工期未知、或前置条件（如未收款）没有确认时间时，
   该工序及其**全部下游**的预测日期一律为 `None`，并给出原因码。
   绝不拿一个好看的数字填空 —— 伪确定的交期比没有交期危险得多，
   因为它会被当成承诺用出去。

纯计算、无 I/O、无副作用。
"""
from __future__ import annotations

import datetime as _dt

from . import schema as S
from .calendar import WorkCalendar
from .graph import DependencyGraph

_DT = "%Y-%m-%dT%H:%M"


# ────────────────────────── 输入规范化 ──────────────────────────
def normalize_step(raw: dict) -> dict:
    """把快照里的一条工序读成内部结构。只做规范化，不做任何补全猜测。"""
    dur = raw.get("duration_hours")
    try:
        dur = None if dur is None or dur == "" else float(dur)
    except (TypeError, ValueError):
        dur = None
    src = str(raw.get("duration_source") or "").strip()
    if dur is None:
        src = S.DUR_SOURCE_UNKNOWN
    elif not src:
        src = S.DUR_SOURCE_INPUT
    preconds = []
    for c in (raw.get("preconditions") or []):
        if not isinstance(c, dict):
            continue
        preconds.append({
            "kind": str(c.get("kind") or S.COND_OTHER),
            "status": str(c.get("status") or S.COND_UNKNOWN),
            "available_at": c.get("available_at") or "",
            "ref": str(c.get("ref") or ""),
            "note": str(c.get("note") or ""),
            "assumed": bool(c.get("assumed")),
        })
    resources = []
    for r in (raw.get("resources") or []):
        if isinstance(r, str):
            resources.append({"resource_id": r, "hours": dur})
        elif isinstance(r, dict):
            hours = r.get("hours")
            resources.append({
                "resource_id": str(r.get("resource_id") or r.get("id") or ""),
                "hours": float(hours) if hours is not None else dur,
                "units": r.get("units"),
            })
    return {
        "step_id": str(raw.get("step_id") or raw.get("id") or "").strip(),
        "plan_id": str(raw.get("plan_id") or "").strip(),
        "name": str(raw.get("name") or raw.get("process") or "").strip(),
        "process": str(raw.get("process") or "").strip(),
        "duration_hours": dur,
        "duration_source": src,
        "status": str(raw.get("status") or S.STEP_NOT_STARTED).strip(),
        "actual_start": raw.get("actual_start") or "",
        "actual_finish": raw.get("actual_finish") or "",
        "planned_start": raw.get("planned_start") or "",
        "contract_date": raw.get("contract_date") or "",
        "internal_plan_date": raw.get("internal_plan_date") or "",
        "predecessors": [str(p).strip() for p in (raw.get("predecessors") or [])
                         if str(p).strip()],
        "preconditions": preconds,
        "resources": resources,
        "source_row_id": str(raw.get("source_row_id") or ""),
        "source_table": str(raw.get("source_table") or ""),
    }


# ────────────────────────── 条件裁决 ──────────────────────────
def _condition_gate(steps: list[dict], extra_conditions: list[dict]):
    """汇总前置条件对「最早可开始」的约束。

    返回 (按 step_id 索引的限制, 说明列表)。三种条件的处理**故意不一样**：

      confirmed  已核实、有时间   → 硬约束，可产出「确定」日期；
      assumed    情景假设、有时间 → 硬约束**仅在该情景内成立**，结论标 conditional；
      announced  消息说了、未核实 → 也参与排程（因为完全忽略它同样是撒谎），
                                    但结论标 conditional，并明说「依赖未核实信息」；
      unknown / 无时间            → 不可确定，不给日期。

    为什么 announced 也要参与：如果直接忽略它，算法会退化成「物料周一就到」，
    那是一个**更离谱**的假设。用它 + 明确标注，才是诚实的做法。
    """
    notes: list[dict] = []
    per_step: dict[str, dict] = {}
    for st in steps:
        hard = None          # 确定的可用时刻（datetime）
        soft = None          # 未核实/假设给出的可用时刻
        assumed = False
        unknown: list[dict] = []
        conds = list(st["preconditions"]) + list(extra_conditions or [])
        for c in conds:
            at = c.get("available_at")
            if c["status"] in S.COND_TRUSTED and at:
                dt = _parse_at(at)
                if dt is None:
                    unknown.append(dict(c, why="available_at 无法解析"))
                    continue
                hard = dt if hard is None else max(hard, dt)
                notes.append({"step_id": st["step_id"], "kind": c["kind"],
                              "status": c["status"], "available_at": dt.isoformat(),
                              "trusted": True, "ref": c.get("ref", "")})
            elif c["status"] == S.COND_ASSUMED and at:
                dt = _parse_at(at)
                if dt is None:
                    unknown.append(dict(c, why="available_at 无法解析"))
                    continue
                hard = dt if hard is None else max(hard, dt)
                assumed = True
                notes.append({"step_id": st["step_id"], "kind": c["kind"],
                              "status": c["status"], "available_at": dt.isoformat(),
                              "trusted": False, "ref": c.get("ref", ""),
                              "say": "情景假设：仅在该假设成立的条件下有效"})
            elif c["status"] == S.COND_ANNOUNCED and at:
                dt = _parse_at(at)
                if dt is None:
                    unknown.append(dict(c, why="available_at 无法解析"))
                    continue
                soft = dt if soft is None else max(soft, dt)
                notes.append({"step_id": st["step_id"], "kind": c["kind"],
                              "status": c["status"], "available_at": dt.isoformat(),
                              "trusted": False, "ref": c.get("ref", ""),
                              "say": "消息所述时间未核实，结论为有条件预测"})
            else:
                unknown.append(dict(c))
                notes.append({"step_id": st["step_id"], "kind": c["kind"],
                              "status": c["status"], "available_at": "",
                              "trusted": False, "ref": c.get("ref", ""),
                              "say": "无可用时间或未确认，下游不可确定"})
        per_step[st["step_id"]] = {"hard": hard, "soft": soft, "assumed": assumed,
                                   "unknown": unknown}
    return per_step, notes


def _parse_at(value):
    """解析时刻。

    纯日期（含日期+时间的字符串）一律解析为**当天上班时刻 09:00**：
    「9 月 30 日到货」意味着 9 月 30 日一上班就能用，而不是 9 月 30 日凌晨 0 点。
    （Python 3.11+ 的 datetime.fromisoformat 能把 '2026-09-30' 解析成 00:00，
    这里显式挡掉，否则条件日期会被算早一天。）
    """
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= 10:
        try:
            d = _dt.date.fromisoformat(text[:10])
            return _dt.datetime.combine(d, _dt.time(9, 0))
        except ValueError:
            return None
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


# ────────────────────────── 主算法 ──────────────────────────
def build_cpm(snapshot: dict, calendar: WorkCalendar = None) -> dict:
    """按「仅前置关系」口径做 CPM：ES / EF / LS / LF / 松弛 / 关键路径。

    snapshot 至少含 steps / calendar / as_of。
    """
    cal = calendar
    if cal is None:
        cdata = snapshot.get("calendar")
        if not cdata:
            return {"ok": False, "issues": [{
                "code": S.GAP_NO_CALENDAR, "severity": "error",
                "message": S.GAP_CN[S.GAP_NO_CALENDAR], "ref": ""}],
                "steps": [], "plans": [], "critical_path": []}
        from .calendar import from_dict
        cal = from_dict(cdata)

    raw_steps = list(snapshot.get("steps") or [])
    steps = [normalize_step(s) for s in raw_steps]
    by_id = {st["step_id"]: st for st in steps if st["step_id"]}

    graph = DependencyGraph(list(by_id))
    for st in steps:
        sid = st["step_id"]
        if not sid:
            continue
        for p in st["predecessors"]:
            if p not in by_id:
                graph.add_missing(sid, p, source=st.get("source_table", ""))
            else:
                graph.add_edge(p, sid, source=st.get("source_table", ""))
    graph.validate()

    cond_per_step, cond_notes = _condition_gate(steps, snapshot.get("conditions") or [])

    origin = _parse_at(snapshot.get("planning_start") or snapshot.get("as_of")
                       or "") or _dt.datetime.combine(_dt.date.today(), _dt.time(9, 0))

    if graph.has_blocking_issue():
        # 图有硬错误（自环/悬空/循环）→ 不给排程结论，只给问题清单
        return {
            "ok": False,
            "calendar": cal.describe(),
            "as_of": snapshot.get("as_of") or "",
            "origin": origin.strftime(_DT),
            "issues": graph.issues_dict(),
            "gaps": [{"code": i.code, "message": i.message, "ref": i.ref}
                     for i in graph.issues if i.blocking],
            "steps": [], "plans": [], "critical_path": [],
            "project_finish": None,
            "method": S.SCHEDULE_METHOD,
            "disclaimer": S.SCHEDULE_DISCLAIMER,
        }

    try:
        order = graph.topo_order()
    except ValueError:
        return {"ok": False, "issues": graph.issues_dict(), "steps": [], "plans": [],
                "critical_path": [], "project_finish": None}

    # ── 前推 ────────────────────────────────────
    ef: dict[str, _dt.datetime] = {}
    es: dict[str, _dt.datetime] = {}
    undet: dict[str, list[dict]] = {}
    cond_meta: dict[str, dict] = {}
    extra_issues: list[dict] = []

    for sid in order:
        st = by_id[sid]
        reasons: list[dict] = []
        cond = cond_per_step.get(sid) or {}
        gate = cond.get("hard")
        soft = cond.get("soft")

        if st["duration_hours"] is None:
            reasons.append(_gap(S.GAP_UNKNOWN_DURATION, sid,
                                detail={"step_id": sid, "name": st["name"]}))
        if cond.get("unknown"):
            kinds = "、".join(sorted({c.get("kind", "?") for c in cond["unknown"]}))
            reasons.append(_gap(S.GAP_UNCONFIRMED_PRECONDITION, sid,
                                detail={"step_id": sid, "kinds": kinds,
                                        "items": [{"kind": c.get("kind"),
                                                   "status": c.get("status"),
                                                   "note": c.get("note", "")}
                                                  for c in cond["unknown"]]}))

        # 冻结工序（进行中/已完成）以实际/计划开始为准，不参与重排
        frozen = st["status"] in S.FROZEN_STATUSES
        if frozen:
            base = st["actual_start"] or st["planned_start"]
            start = _parse_at(base)
            if start is None:
                reasons.append(_gap(S.GAP_FROZEN_MOVED, sid,
                                    detail={"step_id": sid,
                                            "say": "工序已开始但缺实际开始时间"}))
                start = None
        else:
            start = origin

        # 前置完成时刻
        pred_finish = None
        for p in st["predecessors"]:
            if p not in ef:
                continue
            if ef[p] is None:
                reasons.append(_gap(S.GAP_UNKNOWN_DURATION, sid,
                                    detail={"step_id": sid, "blocked_by": p}))
                pred_finish = None
                break
            pred_finish = ef[p] if pred_finish is None else max(pred_finish, ef[p])

        if start is not None:
            if frozen:
                # 已经开工了，时间就是既成事实 —— 不能因为「物料条件写的是后天」
                # 就把它推到后天。那是把历史改掉，比排错更糟。
                if pred_finish is not None and pred_finish > start:
                    extra_issues.append({
                        "code": S.GAP_FROZEN_MOVED, "severity": "warning", "ref": sid,
                        "message": "工序 %s 已开始于 %s，却晚于其前置完成时间 %s，"
                                   "数据可能不一致（已按实际开始时间处理，未改动）"
                                   % (sid, start.strftime(_DT),
                                      pred_finish.strftime(_DT))})
            else:
                # 未核实（announced）条件不作为硬约束，但也不允许被忽略：
                # 没有已核实的门，就用未核实的时间起算，结论标 conditional。
                effective_gate = gate if gate is not None else soft
                candidates = [x for x in (start, effective_gate, pred_finish)
                              if x is not None]
                if candidates:
                    start = max(candidates)
                if pred_finish is None and any(
                        p not in ef for p in st["predecessors"]):
                    start = None
        else:
            start = None

        hard_ok = start is not None
        cond_meta[sid] = {
            "frozen": frozen,
            "gate": gate.isoformat() if gate else "",
            "soft": soft.isoformat() if soft else "",
            "assumed": bool(cond.get("assumed")),
            "unverified": bool(soft) or bool(cond.get("assumed")),
        }

        if hard_ok and st["duration_hours"] is not None:
            es[sid] = cal.align_forward(start) if not frozen else start
            ef[sid] = cal.add_working_hours(es[sid], st["duration_hours"])
        elif frozen and start is not None and st["status"] == S.STEP_DONE:
            # 已完成：完成时间已知就用已知，否则按工期推（仅作占位，不对外称确定）
            fin = _parse_at(st["actual_finish"])
            es[sid] = start
            ef[sid] = fin or (cal.add_working_hours(start, st["duration_hours"])
                              if st["duration_hours"] is not None else None)
            if ef[sid] is None:
                reasons.append(_gap(S.GAP_UNKNOWN_DURATION, sid,
                                    detail={"step_id": sid, "say": "已完成但缺完成时间与工期"}))
        else:
            es[sid] = start
            ef[sid] = None
        if reasons:
            undet[sid] = reasons

    # ── 项目完成（仅用已确定的 EF）───────────────
    known_ef = [v for v in ef.values() if v is not None]
    project_finish = max(known_ef) if known_ef else None

    # ── 反推 LS / LF / 松弛 ─────────────────────
    ls: dict[str, _dt.datetime] = {}
    lf: dict[str, _dt.datetime] = {}
    slack_min: dict[str, int] = {}
    for sid in reversed(order):
        st = by_id[sid]
        succs = [m for m in graph.succs.get(sid, []) if graph.has_edge(sid, m)]
        vals = [ls[m] for m in succs if ls.get(m) is not None]
        if st["status"] == S.STEP_DONE and ef.get(sid):
            # 已完成工序的 LF 即它的实际完成（历史已定，无松弛可言）
            lf[sid] = ef[sid]
        elif vals:
            lf[sid] = min(vals)
        elif project_finish is not None:
            lf[sid] = project_finish
        else:
            lf[sid] = None
        if lf[sid] is not None and st["duration_hours"] is not None:
            ls[sid] = cal.sub_working_hours(lf[sid], st["duration_hours"])
        else:
            ls[sid] = None
        if es.get(sid) is not None and ls.get(sid) is not None:
            slack_min[sid] = int((ls[sid] - es[sid]).total_seconds() // 60)
        else:
            slack_min[sid] = None

    # ── 组织输出 ───────────────────────────────
    out_steps = []
    for sid in order:
        st = by_id[sid]
        cm = cond_meta.get(sid, {})
        preds = [p for p in st["predecessors"] if graph.has_edge(p, sid)]
        succs = [m for m in graph.succs.get(sid, []) if graph.has_edge(sid, m)]
        reasons = list(undet.get(sid) or [])
        status, fc = _forecast_status(ef.get(sid), cm, reasons)
        out_steps.append({
            "step_id": sid,
            "plan_id": st["plan_id"],
            "name": st["name"],
            "process": st["process"],
            "status": st["status"],
            "frozen": bool(cm.get("frozen")),
            "duration_hours": st["duration_hours"],
            "duration_source": st["duration_source"],
            "predecessors": preds,
            "successors": succs,
            "resources": list(st["resources"]),
            "earliest_start": _fmt(es.get(sid)),
            "earliest_finish": _fmt(ef.get(sid)),
            "latest_start": _fmt(ls.get(sid)),
            "latest_finish": _fmt(lf.get(sid)),
            "slack_hours": (round(slack_min[sid] / 60.0, 2)
                            if slack_min.get(sid) is not None else None),
            "critical": bool(slack_min.get(sid) == 0),
            "forecast_date": fc,
            "forecast_status": status,
            "undetermined_reasons": reasons,
            "condition_gate": cm.get("gate") or "",
            "condition_soft": cm.get("soft") or "",
            "condition_assumed": bool(cm.get("assumed")),
            "depends_on_unverified": bool(cm.get("unverified")),
            "contract_date": _date_only(st["contract_date"]),
            "internal_plan_date": _date_only(st["internal_plan_date"]),
            "source_row_id": st["source_row_id"],
            "source_table": st["source_table"],
        })

    critical_path = [s["step_id"] for s in out_steps if s["critical"]]

    return {
        "ok": True,
        "calendar": cal.describe(),
        "as_of": snapshot.get("as_of") or "",
        "origin": origin.strftime(_DT),
        "method": S.SCHEDULE_METHOD,
        "method_cn": S.SCHEDULE_METHOD_CN,
        "disclaimer": S.SCHEDULE_DISCLAIMER,
        "steps": out_steps,
        "plans": _aggregate_plans(out_steps, snapshot),
        "critical_path": critical_path,
        "project_finish": _fmt(project_finish),
        "issues": graph.issues_dict() + extra_issues,
        "gaps": [r for s in out_steps for r in s["undetermined_reasons"]],
        "condition_notes": cond_notes,
    }


def _forecast_status(ef_value, cm, reasons):
    """返回 (状态, 预测日期)。状态：determined / conditional / undetermined。"""
    if ef_value is None:
        return "undetermined", None
    if cm.get("unverified"):
        return "conditional", ef_value.date().isoformat()
    return "determined", ef_value.date().isoformat()


def _aggregate_plans(out_steps, snapshot) -> list[dict]:
    """按 plan_id 聚合出三日期。合同/内部日期只读，预测日期只写 forecast 字段。"""
    meta = {str(p.get("plan_id") or ""): p for p in (snapshot.get("plans") or [])}
    buckets: dict[str, list[dict]] = {}
    for s in out_steps:
        buckets.setdefault(s["plan_id"] or "", []).append(s)
    out = []
    for pid, items in buckets.items():
        finishes = [i["forecast_date"] for i in items if i["forecast_date"]]
        statuses = {i["forecast_status"] for i in items}
        if not finishes or "undetermined" in statuses:
            status = "undetermined"
        elif "conditional" in statuses:
            status = "conditional"
        else:
            status = "determined"
        reasons = [r for i in items for r in i["undetermined_reasons"]]
        m = meta.get(pid) or {}
        # 计划级三日期：contract/internal 来自快照（只读），forecast 由算法产出
        plan_contract = _date_only(m.get("contract_date"))
        plan_internal = _date_only(m.get("internal_plan_date"))
        if not plan_contract:
            plan_contract = next(
                (i["contract_date"] for i in items if i["contract_date"]), "")
        if not plan_internal:
            plan_internal = next(
                (i["internal_plan_date"] for i in items if i["internal_plan_date"]), "")
        out.append({
            "plan_id": pid,
            "name": m.get("name") or (items[0]["name"] if items else ""),
            "step_ids": [i["step_id"] for i in items],
            "contract_date": plan_contract,
            "internal_plan_date": plan_internal,
            "forecast_date": max(finishes) if finishes and status != "undetermined" else None,
            "forecast_status": status,
            "depends_on_unverified": any(i.get("depends_on_unverified") for i in items),
            "undetermined_reasons": reasons,
            "delay_vs_contract_days": _days_between(
                max(finishes) if finishes and status != "undetermined" else None,
                plan_contract),
        })
    out.sort(key=lambda p: p["plan_id"])
    return out


def _days_between(forecast_date, contract_date):
    """预测比合同晚几天（正=晚）。合同日期缺失则返回 None，不猜。"""
    f, c = _date_only(forecast_date), _date_only(contract_date)
    if not f or not c:
        return None
    return (_dt.date.fromisoformat(f) - _dt.date.fromisoformat(c)).days


def _gap(code, ref, detail=None):
    return {"code": code, "message": S.GAP_CN.get(code, code), "ref": ref,
            "detail": detail or {}}


def _fmt(dt):
    return dt.strftime(_DT) if isinstance(dt, _dt.datetime) else None


def _date_only(value):
    if not value:
        return ""
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value).strip()[:10]


# ────────────────────────── 延期传播 ──────────────────────────
def propagate_delay(snapshot: dict, step_id: str, extra_hours: float,
                    calendar: WorkCalendar = None) -> dict:
    """把某工序延期 extra_hours，返回**它把谁推后了**的传播链。

    做法是「跑两遍再对比」，而不是只讲一句「下游会受影响」——
    因为「哪些下游真的被推了、推了几天、哪条链路是主因」必须能指着日期说清楚。
    有松弛的下游不会被推（它本来就等得起），这正是要区分出来的东西。
    """
    base = build_cpm(snapshot, calendar)
    if not base.get("ok"):
        return {"ok": False, "reason": "基线排程本身不可用", "issues": base.get("issues", [])}

    steps = list(snapshot.get("steps") or [])
    hit = False
    mod = []
    for s in steps:
        s2 = dict(s)
        if str(s2.get("step_id") or s2.get("id") or "").strip() == step_id:
            hit = True
            dur = s2.get("duration_hours")
            if dur is None:
                return {"ok": False, "reason": "该工序工期未知，无法模拟延期",
                        "reason_code": S.GAP_UNKNOWN_DURATION, "step_id": step_id}
            s2["duration_hours"] = float(dur) + float(extra_hours)
            s2["duration_source"] = S.DUR_SOURCE_ASSUMED
        mod.append(s2)
    if not hit:
        return {"ok": False, "reason": "找不到工序 %s" % step_id, "step_id": step_id}

    snap2 = dict(snapshot)
    snap2["steps"] = mod
    after = build_cpm(snap2, calendar)
    if not after.get("ok"):
        return {"ok": False, "reason": "延期后排程不可用", "issues": after.get("issues", [])}

    b = {s["step_id"]: s for s in base["steps"]}
    a = {s["step_id"]: s for s in after["steps"]}
    chain = []
    for sid in a:
        old, new = b.get(sid, {}), a[sid]
        d_es = _delta_days(old.get("earliest_start"), new.get("earliest_start"))
        d_ef = _delta_days(old.get("earliest_finish"), new.get("earliest_finish"))
        if d_ef or d_es:
            chain.append({
                "step_id": sid, "plan_id": new["plan_id"], "name": new["name"],
                "delta_start_days": d_es, "delta_finish_days": d_ef,
                "old_start": old.get("earliest_start"), "new_start": new.get("earliest_start"),
                "old_finish": old.get("earliest_finish"), "new_finish": new.get("earliest_finish"),
                "slack_hours_before": old.get("slack_hours"),
                "critical_before": old.get("critical"),
                "absorbed_by_slack": bool(old.get("slack_hours") and not d_ef),
            })
    chain.sort(key=lambda c: (c["plan_id"], c["new_start"] or ""))
    plan_delta = []
    bp = {p["plan_id"]: p for p in base["plans"]}
    for p in after["plans"]:
        o = bp.get(p["plan_id"], {})
        plan_delta.append({
            "plan_id": p["plan_id"], "name": p.get("name", ""),
            "old_forecast": o.get("forecast_date"), "new_forecast": p.get("forecast_date"),
            "delta_days": _delta_days(o.get("forecast_date"), p.get("forecast_date")),
            "forecast_status": p.get("forecast_status"),
        })

    # CPM 只看前置关系，看不到「产能挤占」造成的二次连锁。
    # 所以这里再跑一遍资源可行排程做对比：延期可能把某道工序挤到明天，
    # 而明天那台设备本来要给另一个计划用 —— 这种连坐在 CPM 里是隐形的。
    res_impact = []
    try:
        from .resources import resource_feasible_schedule
        rb = resource_feasible_schedule(snapshot, calendar)
        ra = resource_feasible_schedule(snap2, calendar)
        if rb.get("ok") and ra.get("ok"):
            mb = {p["plan_id"]: p for p in rb.get("plans") or []}
            for p in ra.get("plans") or []:
                o = mb.get(p["plan_id"], {})
                d = _delta_days(o.get("forecast_date"), p.get("forecast_date"))
                if d:
                    res_impact.append({
                        "plan_id": p["plan_id"], "name": p.get("name", ""),
                        "old_forecast": o.get("forecast_date"),
                        "new_forecast": p.get("forecast_date"),
                        "delta_days": d,
                        "stage": "resource_feasible",
                    })
    except Exception as e:      # pragma: no cover - 资源层不可用时不影响 CPM 结论
        res_impact.append({"error": str(e)})

    return {
        "ok": True,
        "step_id": step_id,
        "extra_hours": float(extra_hours),
        "affected_steps": chain,
        "affected_plans": [d for d in plan_delta if d["delta_days"]],
        "all_plans": plan_delta,
        "resource_level_impact": res_impact,
        "critical_path_before": base["critical_path"],
        "critical_path_after": after["critical_path"],
        "basis": ("对同一份快照跑两遍（原工期 / 延期后）取差集；"
                  "松弛足够的下游不会被推后，会显式标出 absorbed_by_slack。"
                  "resource_level_impact 是叠加资源约束后的二次连锁，"
                  "与 affected_plans（仅前置口径）分开列出，两者不可混为一谈。"),
    }


def _delta_days(a, b):
    da, db = _date_only(a), _date_only(b)
    if not da or not db:
        return None
    return (_dt.date.fromisoformat(db) - _dt.date.fromisoformat(da)).days


__all__ = ["build_cpm", "propagate_delay", "normalize_step"]
