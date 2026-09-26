# -*- coding: utf-8 -*-
"""application/decision_support/resources.py — 资源占用、冲突检测、资源可行排程。

两个容易混淆的概念，这里分得很开：

  · **关键路径**（schedule.py）= 只看前置关系，不管资源够不够。
  · **资源可行排程**（本模块）= 在前置关系之上，再加「人/设备/外协同一时间只能用一次」。
    本模块用的是**可解释的启发式**（就绪工序里最早可开始者先排，资源被占就让位），
    结果**不是**资源约束下的最优排程。RCPSP 是 NP-hard，本期不做最优求解。
    所以每次输出都带 `disclaimer`，宁可显得啰嗦，也不背一个「最优」的名头。

为什么必须**保护已开始/已完成的工序**：
一个算法如果能把昨天已经贴了一天的板子「重排」到后天，
它排出来的计划再漂亮也是废纸 —— 因为现场已经把料和人都投进去了。
所以冻结工序的时间是**硬约束**：它占的资源先扣掉，别人绕开它走，
并且任何试图移动它的重排都会被记录为 `GAP_FROZEN_MOVED` 而不是静默接受。

纯计算、无 I/O、无副作用。
"""
from __future__ import annotations

import datetime as _dt

from . import schema as S
from .calendar import WorkCalendar
from .schedule import build_cpm, normalize_step, _condition_gate, _parse_at

_EPS = 1e-6
_MAX_RETRY = 80


# ────────────────────────── 资源定义 ──────────────────────────
def normalize_resource(raw: dict) -> dict:
    cap = raw.get("daily_capacity_hours", raw.get("capacity_hours"))
    try:
        cap = None if cap is None or cap == "" else float(cap)
    except (TypeError, ValueError):
        cap = None
    return {
        "resource_id": str(raw.get("resource_id") or raw.get("id") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "type": str(raw.get("type") or S.RESOURCE_TYPE_DEFAULT).strip(),
        "daily_capacity_hours": cap,
        "in_service": bool(raw.get("in_service", True)),
        "processes": [str(p) for p in (raw.get("processes") or [])],
        "daily_cost": _num(raw.get("daily_cost")),
        "source_row_id": str(raw.get("source_row_id") or ""),
    }


def normalize_group(raw: dict) -> dict:
    """资源组（资源池）。真实业务里「测试设备」很少只有一台，
    工序要的是「这台设备，或者那一台」—— 这种 OR 语义必须能表达，
    否则「再加一台设备」这个方案在模型里根本落不了地。"""
    return {
        "group_id": str(raw.get("group_id") or raw.get("id") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "type": str(raw.get("type") or S.RESOURCE_TYPE_DEFAULT).strip(),
        "members": [str(m).strip() for m in (raw.get("members") or []) if str(m).strip()],
    }


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


# ────────────────────────── 占位表 ──────────────────────────
class ResourceTimeline:
    """资源的日粒度占用表。key = (resource_id, date) → 已占小时。

    resources：具体的人/设备；groups：资源池（工序引用池时，池内任一成员可用即可）。
    """

    def __init__(self, resources: list[dict], groups: list[dict] = None):
        self.resources = {r["resource_id"]: r for r in resources if r["resource_id"]}
        self.groups: dict[str, list[str]] = {}
        for g in (groups or []):
            if g.get("group_id"):
                self.groups[g["group_id"]] = [m for m in g.get("members") or []]
        self.used: dict[tuple, float] = {}
        self.by_step: dict[tuple, list[str]] = {}
        self.issues: list[dict] = []
        for r in resources:
            if not r["resource_id"]:
                continue
            if r["daily_capacity_hours"] is None:
                self.issues.append({
                    "code": S.GAP_RESOURCE_UNKNOWN_CAP, "severity": "warning",
                    "ref": r["resource_id"],
                    "message": "资源「%s」未定义日产能，本次排程对它不做超载校验"
                               % (r["name"] or r["resource_id"])})
            if r["in_service"] is False:
                self.issues.append({
                    "code": "resource_out_of_service", "severity": "warning",
                    "ref": r["resource_id"],
                    "message": "资源「%s」当前不在岗/停用，却仍被工序引用"
                               % (r["name"] or r["resource_id"])})

    def candidates(self, rid: str) -> list[str]:
        """把工序引用的 id 解析成候选资源：是池就展开成员，否则就是它自己。"""
        if rid in self.groups:
            return [m for m in self.groups[rid] if m in self.resources]
        return [rid] if rid in self.resources else []

    def capacity(self, rid) -> float:
        r = self.resources.get(rid)
        if r is None or r["daily_capacity_hours"] is None:
            return float("inf")     # 未知产能 = 不校验（并在 issues 里说明）
        return float(r["daily_capacity_hours"])

    def remaining(self, rid, d) -> float:
        cap = self.capacity(rid)
        if cap == float("inf"):
            return float("inf")
        return cap - self.used.get((rid, d), 0.0)

    def occupy(self, rid, d, hours: float, step_id: str) -> None:
        key = (rid, d)
        self.used[key] = self.used.get(key, 0.0) + float(hours)
        self.by_step.setdefault(key, []).append(step_id)

    def load_rows(self) -> list[dict]:
        rows = []
        for r in self.resources.values():
            days = sorted(d for (rid, d) in self.used if rid == r["resource_id"])
            cap = r["daily_capacity_hours"]
            rows.append({
                "resource_id": r["resource_id"], "name": r["name"], "type": r["type"],
                "daily_capacity_hours": cap,
                "in_service": r["in_service"],
                "days": [{"date": d.isoformat(), "used_hours": round(self.used[(r["resource_id"], d)], 2),
                          "capacity_hours": cap,
                          "overload": bool(cap is not None and self.used[(r["resource_id"], d)] > cap + _EPS),
                          "steps": self.by_step.get((r["resource_id"], d), [])}
                         for d in days],
                "total_hours": round(sum(self.used[(r["resource_id"], d)] for d in days), 2),
            })
        rows.sort(key=lambda x: x["resource_id"])
        return rows


def _daily_demand(hours: float, start: _dt.datetime, finish: _dt.datetime,
                  cal: WorkCalendar) -> dict:
    """把某工序对某资源的总需求摊到它跨越的工作日上。

    规则直白、可手算复现：每天最多摊 8 小时。
      8h/1 天 → {d: 8}；16h/2 天 → {d1: 8, d2: 8}；12h/2 天 → {d1: 8, d2: 4}。

    注意起点要先 `align_forward`：若 start 是「17:00 下班时刻」，
    真正干活的是**下一个工作日**，占位也必须记在下一天，
    否则两批共用一台设备时会双双记在同一天，冲突就检测不出来了。
    """
    if hours is None or hours <= 0 or start is None:
        return {}
    out: dict = {}
    remain = float(hours)
    cur = cal.align_forward(start)
    guard = 0
    while remain > _EPS:
        guard += 1
        if guard > 4000:
            break
        d = cur.date()
        if cal.is_workday(d):
            take = min(remain, S.HOURS_PER_DAY)
            out[d] = out.get(d, 0.0) + take
            remain -= take
        cur = cal.day_start(cal.next_workday(d + _dt.timedelta(days=1)))
    return out


# ────────────────────────── 冲突检测（对任意已给排程）──────────────────────────
def detect_conflicts(allocations: list[dict], resources: list[dict],
                     calendar: WorkCalendar, groups: list[dict] = None) -> dict:
    """对一份**已经排好的**占用清单做冲突检测。

    allocations: [{step_id, plan_id, resource_id, hours, start, finish, frozen}]
    返回：超载日 + 时段重叠 + 冻结工序被移动的情况。
    """
    tl = ResourceTimeline(resources, groups or [])
    for a in allocations:
        rid = a.get("resource_id")
        hours = a.get("hours")
        if hours is None:
            hours = _span_hours(a.get("start"), a.get("finish"), calendar)
        dm = _daily_demand(float(hours or 0), _parse_at(a.get("start")),
                           _parse_at(a.get("finish")), calendar)
        for d, h in dm.items():
            tl.occupy(rid, d, h, a.get("step_id") or "")

    overloads = []
    for row in tl.load_rows():
        for day in row["days"]:
            if day["overload"]:
                overloads.append({
                    "kind": "daily_overload",
                    "resource_id": row["resource_id"],
                    "resource_name": row["name"],
                    "date": day["date"],
                    "demand_hours": day["used_hours"],
                    "capacity_hours": day["capacity_hours"],
                    "steps": day["steps"],
                    "message": "%s 在 %s 被占 %.1fh，超过日产能 %.1fh"
                               % (row["name"] or row["resource_id"], day["date"],
                                  day["used_hours"], day["capacity_hours"] or 0),
                })

    overlaps = _time_overlaps(allocations, resources)
    frozen_hits = [a for a in allocations if a.get("frozen") and a.get("moved")]
    return {
        "ok": not overloads and not overlaps,
        "overloads": overloads,
        "time_overlaps": overlaps,
        "frozen_violations": [{
            "step_id": f.get("step_id"), "plan_id": f.get("plan_id"),
            "message": "已开始/已完成的工序 %s 被移动（不应发生）" % f.get("step_id"),
        } for f in frozen_hits],
        "resource_load": tl.load_rows(),
        "issues": tl.issues,
    }


def _time_overlaps(allocations, resources) -> list[dict]:
    """同一资源、两个工序的时间段真重叠（比日粒度更细）。"""
    by_res: dict[str, list] = {}
    for a in allocations:
        rid = a.get("resource_id")
        s, f = _parse_at(a.get("start")), _parse_at(a.get("finish"))
        if not rid or s is None or f is None:
            continue
        by_res.setdefault(rid, []).append((s, f, a))
    out = []
    for rid, items in by_res.items():
        items.sort(key=lambda x: x[0])
        for i in range(len(items) - 1):
            s1, f1, a1 = items[i]
            s2, f2, a2 = items[i + 1]
            if s2 < f1:      # 后一个在前一个结束前开始 → 重叠
                out.append({
                    "kind": "time_overlap", "resource_id": rid,
                    "steps": [a1.get("step_id"), a2.get("step_id")],
                    "window": ["%s ~ %s" % (s1.strftime("%Y-%m-%dT%H:%M"),
                                            f1.strftime("%Y-%m-%dT%H:%M")),
                               "%s ~ %s" % (s2.strftime("%Y-%m-%dT%H:%M"),
                                            f2.strftime("%Y-%m-%dT%H:%M"))],
                    "message": "资源 %s 上 %s 与 %s 的占用时段重叠"
                               % (rid, a1.get("step_id"), a2.get("step_id")),
                })
    return out


def _span_hours(start, finish, cal) -> float:
    s, f = _parse_at(start), _parse_at(finish)
    if s is None or f is None:
        return 0.0
    n = 0
    cur = s
    while cur < f:
        d = cur.date()
        if cal.is_workday(d):
            n += 1
        cur = cal.day_start(cal.next_workday(d + _dt.timedelta(days=1)))
    return n * S.HOURS_PER_DAY


# ────────────────────────── 资源可行排程（启发式）──────────────────────────
def resource_feasible_schedule(snapshot: dict, calendar: WorkCalendar = None,
                               cpm: dict = None, moved_steps=None) -> dict:
    """在 CPM 之上叠加资源约束，产出**资源可行**的排程（启发式，非最优）。

    moved_steps: 允许重排的工序 id 列表（None = 全部未开始工序都可重排）。
                 已开始/已完成的工序永远不可移动 —— 由冻结机制保证，
                 而非由调用方记得传对参数。
    """
    cpm = cpm if cpm is not None else build_cpm(snapshot, calendar)
    if not cpm.get("ok"):
        return {"ok": False, "issues": cpm.get("issues", []),
                "reason": "CPM 不可用（图或日历有问题），资源排程终止",
                "method": S.SCHEDULE_METHOD, "disclaimer": S.SCHEDULE_DISCLAIMER}

    cal = calendar
    if cal is None:
        from .calendar import from_dict
        cal = from_dict(snapshot.get("calendar") or {})

    steps = [normalize_step(s) for s in (snapshot.get("steps") or [])]
    by_id = {s["step_id"]: s for s in steps if s["step_id"]}
    resources = [normalize_resource(r) for r in (snapshot.get("resources") or [])]
    groups = [normalize_group(g) for g in (snapshot.get("resource_groups") or [])]
    tl = ResourceTimeline(resources, groups)

    cond_per_step, _ = _condition_gate(steps, snapshot.get("conditions") or [])
    origin = _parse_at(snapshot.get("planning_start") or snapshot.get("as_of")
                       or "") or _dt.datetime.combine(_dt.date.today(), _dt.time(9, 0))
    movable = set(moved_steps) if moved_steps is not None else None

    issues: list[dict] = list(tl.issues)
    for s in steps:
        for r in s["resources"]:
            if r["resource_id"] and not tl.candidates(r["resource_id"]):
                issues.append({
                    "code": S.GAP_NO_RESOURCE, "severity": "warning",
                    "ref": s["step_id"],
                    "message": "工序「%s」引用的资源 %s 不在快照里（也不属于任何资源池），"
                               "资源约束对它不生效"
                               % (s["name"] or s["step_id"], r["resource_id"])})

    placed: dict[str, dict] = {}
    unresolved: list[dict] = []
    protected: list[dict] = []

    # ── 1) 冻结工序先占位：它们的时间是硬约束 ──
    for s in steps:
        sid = s["step_id"]
        if s["status"] not in S.FROZEN_STATUSES:
            continue
        row = next((x for x in cpm["steps"] if x["step_id"] == sid), None)
        start = _parse_at((row or {}).get("earliest_start")) or _parse_at(
            s["actual_start"] or s["planned_start"])
        dur = s["duration_hours"]
        finish = None
        if start is not None and dur is not None:
            finish = cal.add_working_hours(start, dur)
        af = _parse_at(s["actual_finish"])
        if af is not None:
            finish = af
        if start is None:
            unresolved.append({"step_id": sid, "plan_id": s["plan_id"],
                               "reason": "已开始/已完成工序缺少实际开始时间，无法占位",
                               "reason_code": S.GAP_FROZEN_MOVED})
            continue
        placed[sid] = {"step_id": sid, "plan_id": s["plan_id"], "name": s["name"],
                       "start": start, "finish": finish, "frozen": True,
                       "moved_from": "", "shift_reason": "", "choice": {}}
        protected.append({"step_id": sid, "plan_id": s["plan_id"],
                          "status": s["status"], "status_cn": S.STEP_STATUS_CN.get(s["status"], ""),
                          "start": start.strftime("%Y-%m-%dT%H:%M"),
                          "say": "已开始/已完成，时间锁定，重排必须绕开它"})
        # 冻结工序的占用是**既成事实**，无条件记上，不做超载校验
        _occupy_step(tl, s, start, finish, cal)

    # ── 2) 就绪优先，逐个安排未开始工序 ──
    pending = {s["step_id"]: s for s in steps
               if s["step_id"] and s["status"] not in S.FROZEN_STATUSES}
    guard = 0
    while pending:
        guard += 1
        if guard > 10000:
            unresolved.append({"step_id": "", "reason": "排程迭代超上限，提前终止"})
            break
        ready = []
        for sid, s in pending.items():
            if all(p in placed for p in s["predecessors"] if p in by_id):
                ready.append((sid, s))
        if not ready:
            # 前置里含悬空引用或不可安排的工序 → 剩下的无法推进
            for sid, s in pending.items():
                unresolved.append({
                    "step_id": sid, "plan_id": s["plan_id"],
                    "reason": "前置工序无法完成，本工序无法安排",
                    "reason_code": S.GAP_MISSING_PREDECESSOR,
                    "missing": [p for p in s["predecessors"] if p not in by_id]})
            break

        scored = []
        for sid, s in ready:
            if movable is not None and sid not in movable and s["planned_start"]:
                # 不在「允许重排」范围内：钉在它的计划开始时间，但仍占资源
                es = _parse_at(s["planned_start"])
            else:
                es = _earliest_start(s, placed, by_id, cond_per_step, origin)
            scored.append((es or _dt.datetime.max, s["plan_id"], sid, s))
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        _, _, sid, s = scored[0]

        if s["duration_hours"] is None:
            unresolved.append({"step_id": sid, "plan_id": s["plan_id"],
                               "reason": S.GAP_CN[S.GAP_UNKNOWN_DURATION],
                               "reason_code": S.GAP_UNKNOWN_DURATION})
            placed[sid] = {"step_id": sid, "plan_id": s["plan_id"], "name": s["name"],
                           "start": None, "finish": None, "frozen": False,
                           "moved_from": "", "shift_reason": ""}
            pending.pop(sid)
            continue

        if movable is not None and sid not in movable and s["planned_start"]:
            es = _parse_at(s["planned_start"])
        else:
            es = _earliest_start(s, placed, by_id, cond_per_step, origin)
        if es is None:
            unresolved.append({"step_id": sid, "plan_id": s["plan_id"],
                               "reason": S.GAP_CN[S.GAP_UNCONFIRMED_PRECONDITION],
                               "reason_code": S.GAP_UNCONFIRMED_PRECONDITION})
            placed[sid] = {"step_id": sid, "plan_id": s["plan_id"], "name": s["name"],
                           "start": None, "finish": None, "frozen": False,
                           "moved_from": "", "shift_reason": ""}
            pending.pop(sid)
            continue

        res = _place_with_resources(s, es, tl, cal)
        # 「被资源顺延」= 实际开始晚于**对齐后的**最早可开始。
        # 只把 17:00 → 次日 09:00 这种「下班顺延」算作顺延是不对的，
        # 那只是日历对齐，不是资源挤占 —— 两者混淆会让人误以为产能不够。
        base_start = cal.align_forward(es) if es is not None else None
        shifted = bool(base_start and res["start"] and res["start"] > base_start)
        placed[sid] = {"step_id": sid, "plan_id": s["plan_id"], "name": s["name"],
                       "start": res["start"], "finish": res["finish"], "frozen": False,
                       "moved_from": (base_start if shifted else ""),
                       "shift_reason": (res["shift_reason"] if shifted else ""),
                       "retries": res["retries"],
                       "choice": res.get("choice") or {}}
        if not res["ok"]:
            unresolved.append({"step_id": sid, "plan_id": s["plan_id"],
                               "reason": res["shift_reason"] or "多次尝试后仍无法避开资源冲突",
                               "reason_code": "resource_unresolved"})
        pending.pop(sid)

    # ── 3) 组织输出 ──────────────────────────────
    # 资源层不能把「依赖未核实假设」这个标记弄丢：日期算得出来，
    # 不代表它是确定的 —— 那条假设还没人核实过。
    cpm_steps = {x.get("step_id"): x for x in (cpm.get("steps") or [])}
    out_steps = []
    for s in steps:
        sid = s["step_id"]
        p = placed.get(sid) or {}
        start, finish = p.get("start"), p.get("finish")
        reasons = []
        if start is None:
            u = next((x for x in unresolved if x["step_id"] == sid), None)
            reasons.append({"code": (u or {}).get("reason_code", S.GAP_UNKNOWN_DURATION),
                            "message": (u or {}).get("reason", "未安排"),
                            "ref": sid})
        unverified = bool((cpm_steps.get(sid) or {}).get("depends_on_unverified"))
        if finish is None:
            status = "undetermined"
        elif unverified:
            status = "conditional"
        else:
            status = "determined"
        out_steps.append({
            "step_id": sid, "plan_id": s["plan_id"], "name": s["name"],
            "process": s["process"], "status": s["status"],
            "frozen": bool(p.get("frozen")),
            "resources": [r["resource_id"] for r in s["resources"]],
            # 池引用 → 实际选中哪台，必须落纸
            "resolved_resources": dict(p.get("choice") or {}),
            "start": _fmt(start), "finish": _fmt(finish),
            "forecast_date": _fmt_date(finish),
            "forecast_status": status,
            "depends_on_unverified": unverified,
            "shifted_by_resource": bool(p.get("moved_from")),
            "shifted_from": _fmt(p.get("moved_from")) or "",
            "shift_reason": p.get("shift_reason") or "",
            "undetermined_reasons": reasons,
        })

    plans = _plan_finish(out_steps, snapshot)
    return {
        "ok": True,
        "method": S.SCHEDULE_METHOD,
        "method_cn": S.SCHEDULE_METHOD_CN,
        "disclaimer": S.SCHEDULE_DISCLAIMER,
        "steps": out_steps,
        "plans": plans,
        "resources": tl.load_rows(),
        "issues": issues,
        "unresolved": unresolved,
        "protected_steps": protected,
        "conflicts_resolved": [
            {"step_id": x["step_id"], "shifted_from": x["shifted_from"],
             "start": x["start"], "why": x["shift_reason"]}
            for x in out_steps if x["shifted_by_resource"]],
        "as_of": snapshot.get("as_of") or "",
    }


def _earliest_start(s, placed, by_id, cond_per_step, origin):
    """本工序最早可开始：前置完成时刻 ∩ 条件放行时刻 ∩ 项目起点。

    任一前置条件未确认（且无可用时间）→ 返回 None（不可确定），不猜。
    """
    cond = cond_per_step.get(s["step_id"]) or {}
    if cond.get("unknown"):
        return None
    gate = cond.get("hard")
    base = origin
    for p in s["predecessors"]:
        if p not in by_id:
            continue
        pr = placed.get(p)
        if pr is None:
            return None
        if pr.get("finish") is None:
            return None
        base = max(base, pr["finish"])
    if gate is not None:
        base = max(base, gate)
    return base


def _place_with_resources(s, es, tl: ResourceTimeline, cal: WorkCalendar):
    """从 es 起找第一个资源都不冲突的窗口。冲突则整体顺延到下一个工作日。"""
    start = cal.align_forward(es)      # 「下班时刻」起算 → 次日上班
    last_reason = ""
    for attempt in range(_MAX_RETRY):
        finish = cal.add_working_hours(start, s["duration_hours"])
        ok, why, choice = _fits(s, start, finish, tl, cal)
        if ok:
            _occupy_step(tl, s, start, finish, cal, choice)   # 订下资源，否则后到的看不见冲突
            return {"ok": True, "start": start, "finish": finish,
                    "shift_reason": (("资源冲突顺延：" + last_reason) if last_reason else ""),
                    "retries": attempt, "choice": choice}
        last_reason = why
        start = cal.day_start(cal.next_workday(start.date() + _dt.timedelta(days=1)))
    return {"ok": False, "start": start, "finish": None,
            "shift_reason": last_reason or "资源冲突无法解决",
            "retries": _MAX_RETRY, "choice": {}}


def _fits(s, start, finish, tl: ResourceTimeline, cal: WorkCalendar):
    """检查该工序在 [start, finish] 内对每个资源的需求是否都放得下。

    资源项引用资源池（组）时走 **OR 语义**：池内任一成员放得下就算放得下，
    并把实际选中的成员记在 choice 里 —— 「哪台测试台干的活」必须落纸，
    否则加了设备也说不清到底有没有用上。
    """
    choice: dict[str, str] = {}
    for r in s["resources"]:
        rid = r["resource_id"]
        if not rid:
            continue
        cands = tl.candidates(rid)
        if not cands:
            continue                     # 未定义资源 → 已在 issues 里警告，不参与校验
        need_total = r["hours"] if r.get("hours") is not None else s["duration_hours"]
        placed_one = False
        last_why = ""
        for c in cands:
            ok, why = _fits_one(c, need_total, start, finish, tl, cal)
            if ok:
                choice[rid] = c
                placed_one = True
                break
            last_why = why
        if not placed_one:
            return False, last_why, choice
    return True, "", choice


def _fits_one(rid, hours, start, finish, tl: ResourceTimeline, cal: WorkCalendar):
    dm = _daily_demand(float(hours or 0), start, finish, cal)
    for d, need in dm.items():
        if tl.remaining(rid, d) + _EPS < need:
            cap = tl.capacity(rid)
            name = (tl.resources.get(rid) or {}).get("name") or rid
            return False, ("%s 在 %s 已占 %.1fh / 产能 %.1fh，容不下 %.1fh"
                           % (name, d.isoformat(), tl.used.get((rid, d), 0.0),
                              0.0 if cap == float("inf") else cap, need))
    return True, ""


def _occupy_step(tl: ResourceTimeline, s, start, finish, cal, choice=None) -> None:
    """按实际选中的资源占位（choice 为空时退化为引用 id 本身）。"""
    if start is None or finish is None:
        return
    choice = choice or {}
    for r in s["resources"]:
        rid = r["resource_id"]
        if not rid:
            continue
        actual = choice.get(rid) or (tl.candidates(rid)[0] if tl.candidates(rid) else rid)
        hours = r["hours"] if r.get("hours") is not None else s["duration_hours"]
        for d, h in _daily_demand(float(hours or 0), start, finish, cal).items():
            tl.occupy(actual, d, h, s["step_id"])


def _plan_finish(out_steps, snapshot) -> list[dict]:
    meta = {str(p.get("plan_id") or ""): p for p in (snapshot.get("plans") or [])}
    buckets: dict[str, list[dict]] = {}
    for s in out_steps:
        buckets.setdefault(s["plan_id"] or "", []).append(s)
    out = []
    for pid, items in buckets.items():
        dates = [i["forecast_date"] for i in items if i["forecast_date"]]
        m = meta.get(pid) or {}
        if not dates:
            status = "undetermined"
        elif any(i["forecast_status"] == "conditional" for i in items):
            status = "conditional"
        else:
            status = "determined"
        out.append({
            "plan_id": pid,
            "name": m.get("name") or (items[0]["name"] if items else ""),
            "forecast_date": max(dates) if dates else None,
            "forecast_status": status,
            "depends_on_unverified": any(i.get("depends_on_unverified") for i in items),
            "contract_date": _date_str(m.get("contract_date")),
            "internal_plan_date": _date_str(m.get("internal_plan_date")),
            "step_ids": [i["step_id"] for i in items],
        })
    out.sort(key=lambda p: p["plan_id"])
    return out


def _fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M") if isinstance(dt, _dt.datetime) else None


def _fmt_date(dt):
    return dt.date().isoformat() if isinstance(dt, _dt.datetime) else None


def _date_str(v):
    if not v:
        return ""
    if isinstance(v, _dt.datetime):
        return v.date().isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    return str(v).strip()[:10]


__all__ = ["ResourceTimeline", "detect_conflicts", "resource_feasible_schedule",
           "normalize_resource", "normalize_group"]
