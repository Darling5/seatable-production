# -*- coding: utf-8 -*-
"""application/decision_support/scenarios.py — 方案比较 + 模拟台账。

本模块回答的问题是：「换个条件，能不能好一点？好多少？代价是什么？」
而不是「就这么干吧」。

三条边界（写死在实现里）：

1. **模拟零业务写入。** 所有方案在内存里的快照副本上跑，一个字节都不落业务表。
   这不是靠自觉 —— `SimulationLedger` 把任何「写入意图」都送到第一期
   `application.authorization.authorize_write(None, ...)`，无授权必被拒；
   而且默认写入端 `SimulationWriter` 一旦被调用就抛异常。
   测试直接断言这两点，而不是断言「我们没调用写函数」。

2. **方案选择 ≠ 执行授权。** 比较结果里可以出现「建议加一台测试设备」，
   但它只能以 `write_intents`（authorized=False）的形式存在，永远不自动执行。

3. **费用不编造。** 增量成本必须能指到来源：方案自带的显式成本、
   或成本模型里的单价。两者都没有 → 记成「待核实条件」，而不是填个 0 假装免费。
   「免费的加速」在现实里不存在，账面为 0 往往是漏项。

纯计算，无 I/O，无副作用。
"""
from __future__ import annotations

import copy
import datetime as _dt

from . import schema as S
from .calendar import WorkCalendar, from_dict as cal_from_dict
from .resources import resource_feasible_schedule
from .schedule import build_cpm

try:                                     # 第一期可信执行闸门（统一复用，不自建）
    from application import authorization as AUTH
except ImportError:                      # pragma: no cover
    from .. import authorization as AUTH


# ────────────────────────── 模拟写入端 ──────────────────────────
class SimulationWriter:
    """模拟器的写入端。**默认实现只记录、不执行，且一被调用就抛错。**

    现实中如果有人在模拟链路上接了一个真的 adapter，这就是最后一道防线：
    模拟器绝不会因为「顺手」而把建议写成事实。
    """

    def __init__(self):
        self.calls: list[tuple] = []

    def append_row(self, table, row):
        self.calls.append(("append", table, row))
        raise PermissionError(
            "模拟期禁止写业务表（%s）；要真实写入请走第一期闸门 + 显式授权" % table)

    def update_row(self, table, row_id, row):
        self.calls.append(("update", table, row_id, row))
        raise PermissionError("模拟期禁止更新业务表（%s）" % table)


class SimulationLedger:
    """记录模拟过程中产生的**写入/执行意图**，但一条都不执行。

    `request_execution()` 会真的去问第一期闸门要授权 —— 本期没有任何授权来源，
    所以它必然会带着拒绝理由回来。这不是缺陷，这正是要证明的事：
    没有人的授权，算法自己走不通任何一步。
    """

    def __init__(self, writer=None, actor: str = "decision_support.simulation"):
        self.writer = writer if writer is not None else SimulationWriter()
        self.actor = actor
        self.intents: list[dict] = []
        self.rejected: list[dict] = []

    def request_execution(self, table: str, action: str, row: dict,
                          why: str, source: str = "") -> dict:
        """记录一条执行意图，并走闸门裁决。返回裁决结果（本期恒为拒绝）。"""
        allowed, reason = AUTH.authorize_write(None, table, action)
        rec = {
            "table": table, "action": action, "row": row, "why": why,
            "source": source,
            "gate": S.GATE_MODULE,
            "requires_authorization": True,
            "authorized": bool(allowed),
            "gate_reason": reason,
            "executed": False,
        }
        self.intents.append(rec)
        if not allowed:
            self.rejected.append(rec)
        return rec

    def to_dict(self) -> dict:
        return {
            "intents": self.intents,
            "intents_total": len(self.intents),
            "rejected_total": len(self.rejected),
            "executed_total": sum(1 for i in self.intents if i.get("executed")),
            "writer_calls": len(getattr(self.writer, "calls", [])),
            "note": "所有执行意图均未经授权、未执行；" + S.AUTH_NOTE,
        }


# ────────────────────────── 变更应用 ──────────────────────────
def apply_mutations(snapshot: dict, mutations: list[dict]) -> tuple[dict, list[dict]]:
    """在**快照副本**上应用方案变更，返回 (新快照, 变更说明)。

    绝不在传入的快照上原地改 —— 否则「模拟」会污染调用方的基线。
    """
    snap = copy.deepcopy(snapshot)
    log: list[dict] = []
    for m in mutations or []:
        op = str(m.get("op") or "").strip()
        if op == "set_condition":
            target = str(m.get("step_id") or "")
            kind = str(m.get("kind") or "")
            hit = 0
            for st in snap.get("steps") or []:
                if target and str(st.get("step_id")) != target:
                    continue
                for c in st.get("preconditions") or []:
                    if kind and str(c.get("kind")) != kind:
                        continue
                    c["status"] = m.get("status", c.get("status"))
                    if m.get("available_at"):
                        c["available_at"] = m["available_at"]
                    c["assumed"] = True
                    hit += 1
            log.append({"op": op, "step_id": target, "kind": kind,
                        "status": m.get("status"), "available_at": m.get("available_at"),
                        "applied_to": hit, "say": m.get("say") or
                        "把条件改为「%s %s」" % (m.get("status"), m.get("available_at") or "")})
        elif op == "add_resource":
            r = dict(m.get("resource") or {})
            r.setdefault("assumed", True)
            snap.setdefault("resources", []).append(r)
            grp = m.get("to_group")
            if grp:
                # 加进资源池 = 「这台设备开始可以干这道工序的活」
                for g in snap.setdefault("resource_groups", []):
                    if str(g.get("group_id")) == str(grp):
                        g.setdefault("members", [])
                        if r.get("resource_id") not in g["members"]:
                            g["members"].append(r.get("resource_id"))
            log.append({"op": op, "resource_id": r.get("resource_id"),
                        "to_group": grp,
                        "say": m.get("say") or "新增资源 %s" % r.get("resource_id")})
        elif op == "set_duration":
            for st in snap.get("steps") or []:
                if str(st.get("step_id")) == str(m.get("step_id")):
                    st["duration_hours"] = m.get("duration_hours")
                    st["duration_source"] = S.DUR_SOURCE_ASSUMED
            log.append({"op": op, "step_id": m.get("step_id"),
                        "duration_hours": m.get("duration_hours"),
                        "say": m.get("say") or "工期改为 %s 小时" % m.get("duration_hours")})
        elif op == "set_resource_capacity":
            for r in snap.get("resources") or []:
                if str(r.get("resource_id")) == str(m.get("resource_id")):
                    r["daily_capacity_hours"] = m.get("daily_capacity_hours")
            log.append({"op": op, "resource_id": m.get("resource_id"),
                        "say": m.get("say") or "产能改为 %s" % m.get("daily_capacity_hours")})
        else:
            log.append({"op": op, "applied_to": 0,
                        "say": "未知变更类型 %r，已忽略（不做任何猜测性处理）" % op})
    return snap, log


# ────────────────────────── 费用 ──────────────────────────
_SYNTHETIC_COST_NOTE = ("费用与加急可行性均为合成假设，不是真实报价；"
                        "仅用于演示方案比较的口径，不得用于对外报价或承诺。")


def _resource_day_cost(rid: str, cost_model: dict) -> float:
    rdc = (cost_model or {}).get("resource_day_cost") or {}
    v = rdc.get(rid)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _estimate_cost(mutations: list[dict], cost_model: dict,
                   resource_days: dict) -> tuple[list[dict], list[dict]]:
    """逐条变更估成本。估不出来就记「待核实」，不填 0。"""
    items, gaps = [], []
    cm = cost_model or {}
    for m in mutations:
        op = m.get("op")
        explicit = m.get("cost")
        if explicit is not None:
            try:
                items.append({"item": m.get("say") or op, "amount": float(explicit),
                              "currency": S.UNIT_CNY, "basis": "方案显式给出的合成成本"})
                continue
            except (TypeError, ValueError):
                pass
        if op == "add_resource":
            rid = (m.get("resource") or {}).get("resource_id")
            unit = _resource_day_cost(rid, cm)
            days = resource_days.get(rid)
            if unit is not None and days:
                items.append({"item": "新增资源 %s 使用 %s 天" % (rid, days),
                              "amount": round(unit * days, 2), "currency": S.UNIT_CNY,
                              "basis": "成本模型：%s 元/天 × %s 天" % (unit, days)})
            elif unit is not None:
                # 资源加进来了但一次都没排上 —— 这本身就是结论：钱花了、交期没动。
                gaps.append({
                    "code": "resource_never_used",
                    "message": "新增资源 %s 在本次排程中**未被用到**（瓶颈不在这里），"
                               "采购它不会改善交期；增量成本按 0 计，"
                               "但真实采购支出仍是 %s 元/天" % (rid, unit),
                    "ref": rid})
            else:
                gaps.append({
                    "code": "cost_unknown",
                    "message": "新增资源 %s 的费用尚无依据（缺单价），"
                               "增量成本无法给出——不填 0 冒充免费" % rid,
                    "ref": rid})
        elif op == "set_condition":
            # 提前到货通常有加急费；没有单价就必须挂待核实
            fee = cm.get("expedite_fee")
            if fee is None:
                gaps.append({
                    "code": "cost_unknown",
                    "message": "条件「%s → %s」若需加急，费用无依据，增量成本待核实"
                               % (m.get("kind"), m.get("available_at")),
                    "ref": m.get("step_id") or ""})
            else:
                items.append({"item": "加急/催料费（合成）", "amount": float(fee),
                              "currency": S.UNIT_CNY, "basis": "成本模型 expedite_fee"})
        elif op == "set_duration":
            extra = m.get("extra_hours")
            rate = cm.get("overtime_hour_cost")
            if extra is not None and rate is not None:
                items.append({"item": "加班 %s 小时" % extra,
                              "amount": round(float(rate) * float(extra), 2),
                              "currency": S.UNIT_CNY,
                              "basis": "成本模型：%s 元/小时 × %s 小时" % (rate, extra)})
            elif extra is not None:
                gaps.append({"code": "cost_unknown",
                             "message": "加班费率无依据，增量成本待核实",
                             "ref": m.get("step_id") or ""})
    return items, gaps


# ────────────────────────── 方案比较 ──────────────────────────
def compare_scenarios(snapshot: dict, scenarios: list[dict],
                      calendar: WorkCalendar = None,
                      cost_model: dict = None,
                      ledger: SimulationLedger = None) -> dict:
    """跑基准 + 若干候选方案，输出六件事：
    完成时间 / 增量成本 / 影响项目 / 待核实条件 / 所需批准 / 依据。
    """
    cal = calendar
    if cal is None:
        cal = cal_from_dict(snapshot.get("calendar") or {})
    led = ledger if ledger is not None else SimulationLedger()
    cm = dict(cost_model or {})
    if cm:
        cm.setdefault("is_synthetic", True)
        cm.setdefault("note", _SYNTHETIC_COST_NOTE)

    baseline_spec = next((s for s in scenarios if s.get("kind") == S.PLAN_BASELINE),
                         None)
    if baseline_spec is None:
        baseline_spec = {"scenario_id": "S0", "name": "基准（当前条件，不做事）",
                         "kind": S.PLAN_BASELINE, "mutations": []}

    def _run(spec):
        snap2, mlog = apply_mutations(snapshot, spec.get("mutations") or [])
        cpm = build_cpm(snap2, cal)
        res = resource_feasible_schedule(snap2, cal, cpm=cpm)
        return snap2, mlog, cpm, res

    base_snap, base_mlog, base_cpm, base_res = _run(baseline_spec)
    base_finish = {p["plan_id"]: p.get("forecast_date") for p in (base_res.get("plans") or [])}

    out_scenarios = []
    specs = [baseline_spec] + [s for s in scenarios
                               if s is not baseline_spec
                               and s.get("kind") != S.PLAN_BASELINE]
    for spec in specs:
        is_base = spec is baseline_spec
        if is_base:
            snap2, mlog, cpm, res = base_snap, base_mlog, base_cpm, base_res
        else:
            snap2, mlog, cpm, res = _run(spec)

        completion = []
        for p in (res.get("plans") or []):
            pid = p["plan_id"]
            new = p.get("forecast_date")
            old = base_finish.get(pid)
            completion.append({
                "plan_id": pid, "name": p.get("name", ""),
                "forecast_date": new,
                "forecast_status": p.get("forecast_status"),
                "baseline_forecast_date": old,
                "delta_days": _delta_days(old, new),
                "contract_date": p.get("contract_date") or "",
                "internal_plan_date": p.get("internal_plan_date") or "",
                "vs_contract_days": _delta_days(p.get("contract_date"), new),
            })
        affected = [c for c in completion if c["delta_days"]]

        # 资源使用天数（用于成本估算）
        rd = {}
        for row in res.get("resources") or []:
            rd[row["resource_id"]] = len(row.get("days") or [])

        risk_ledger = SimulationLedger(writer=led.writer, actor=led.actor)
        if is_base:
            cost_items, cost_gaps = [], []
        else:
            cost_items, cost_gaps = _estimate_cost(spec.get("mutations") or [], cm, rd)
            for m in (spec.get("mutations") or []):
                if m.get("op") == "add_resource":
                    risk_ledger.request_execution(
                        "资源", "append",
                        {"资源编号": (m.get("resource") or {}).get("resource_id"),
                         "姓名": (m.get("resource") or {}).get("name", ""),
                         "类型": (m.get("resource") or {}).get("type", "")},
                        why="方案 %s 需要新增资源" % spec.get("scenario_id"),
                        source=spec.get("scenario_id", ""))
            if cpm and not cpm.get("ok"):
                risk_ledger.request_execution("生产计划", "update", {},
                                              why="排程不可用，需人工修正计划数据",
                                              source=spec.get("scenario_id", ""))
        led.intents.extend(risk_ledger.intents)
        led.rejected.extend(risk_ledger.rejected)

        open_conds = _open_conditions(snap2, spec, res)
        approvals = _required_approvals(spec, completion, res)
        out_scenarios.append({
            "scenario_id": spec.get("scenario_id"),
            "name": spec.get("name", ""),
            "kind": spec.get("kind", S.PLAN_CANDIDATE),
            "is_baseline": bool(is_base),
            "mutations": mlog,
            "assumptions": list(spec.get("assumptions") or []),
            "completion": completion,
            "completion_text": "；".join(
                "%s %s" % (c["name"] or c["plan_id"],
                           ("%s（%s）" % (c["forecast_date"], _weekday_cn(c["forecast_date"]))
                            if c["forecast_date"] else "不可确定"))
                + ("，较基准 %+d 天" % c["delta_days"] if c["delta_days"] else "")
                for c in completion),
            "delta_cost": {
                "items": cost_items,
                "amount": round(sum(i["amount"] for i in cost_items), 2) if cost_items else 0.0,
                "currency": S.UNIT_CNY,
                "complete": not cost_gaps,
                "is_synthetic": bool(cm.get("is_synthetic", True)),
                "note": (cm.get("note") or _SYNTHETIC_COST_NOTE),
                "basis": "；".join(i["basis"] for i in cost_items) or "无成本项",
            },
            "affected_projects": [{
                "plan_id": c["plan_id"], "name": c["name"],
                "delta_days": c["delta_days"],
                "from": c["baseline_forecast_date"], "to": c["forecast_date"],
            } for c in affected],
            "open_conditions": open_conds + cost_gaps,
            "required_approvals": approvals,
            "basis": {
                "snapshot_id": snapshot.get("snapshot_id") or "",
                "as_of": snapshot.get("as_of") or "",
                "calendar": cal.describe(),
                "rules_version": S.RULES_VERSION,
                "schedule_method": S.SCHEDULE_METHOD,
                "schedule_disclaimer": S.SCHEDULE_DISCLAIMER,
                "write_intents": [i for i in led.intents
                                  if i.get("source") == spec.get("scenario_id")],
            },
            "schedule": {
                "cpm_critical_path": (cpm or {}).get("critical_path", []),
                "plans": res.get("plans") or [],
                "steps": res.get("steps") or [],
                "resources": res.get("resources") or [],
                "conflicts_resolved": res.get("conflicts_resolved") or [],
                "unresolved": res.get("unresolved") or [],
                "issues": (res.get("issues") or []) + (cpm.get("issues") or []),
                "protected_steps": res.get("protected_steps") or [],
                "method": res.get("method"),
                "method_cn": res.get("method_cn"),
                "disclaimer": res.get("disclaimer"),
            },
        })

    return {
        "contract_version": S.DS_CONTRACT_VERSION,
        "rules_version": S.RULES_VERSION,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "project_id": snapshot.get("project_id") or "",
        "snapshot_id": snapshot.get("snapshot_id") or "",
        "as_of": snapshot.get("as_of") or "",
        "baseline_id": baseline_spec.get("scenario_id"),
        "scenarios": out_scenarios,
        "simulation_ledger": led.to_dict(),
        "cost_model": cm,
        "authorization_note": S.AUTH_NOTE,
        "schedule_disclaimer": S.SCHEDULE_DISCLAIMER,
    }


def _open_conditions(snap, spec, res) -> list[dict]:
    """待核实条件：方案依赖的、但还没被核实的东西。"""
    out = []
    assumed = {m.get("step_id") for m in (spec.get("mutations") or [])
               if m.get("op") == "set_condition"}
    for st in snap.get("steps") or []:
        for c in st.get("preconditions") or []:
            if c.get("status") in S.COND_TRUSTED and not c.get("assumed"):
                continue
            out.append({
                "code": "condition_unverified",
                "step_id": st.get("step_id"),
                "kind": c.get("kind"), "kind_cn": S.COND_KIND_CN.get(c.get("kind"), ""),
                "status": c.get("status"),
                "status_cn": S.COND_STATUS_CN.get(c.get("status"), ""),
                "available_at": c.get("available_at") or "",
                "message": "工序 %s 的「%s」为%s，方案结论建立在未核实的前提上"
                           % (st.get("name") or st.get("step_id"),
                              S.COND_KIND_CN.get(c.get("kind"), c.get("kind")),
                              S.COND_STATUS_CN.get(c.get("status"), c.get("status"))),
                "ref": c.get("ref") or "",
            })
    for u in res.get("unresolved") or []:
        out.append({"code": u.get("reason_code") or "unresolved",
                    "step_id": u.get("step_id"), "message": u.get("reason"),
                    "ref": u.get("step_id") or ""})
    return out


def _required_approvals(spec, completion, res) -> list[dict]:
    """所需批准。每条都强调：批准是「同意这么做」，不等于执行授权。"""
    out = []
    for m in spec.get("mutations") or []:
        if m.get("op") == "add_resource":
            out.append({"what": "新增资源", "ref": (m.get("resource") or {}).get("resource_id"),
                        "who": "生产/设备负责人",
                        "why": "新增人/设备涉及成本与排产占用，需业务批准"})
        elif m.get("op") == "set_condition":
            out.append({"what": "核实前置条件", "ref": m.get("step_id"),
                        "who": "采购/跟单",
                        "why": "方案把「%s」当成 %s，必须先核实，否则结论不成立"
                               % (S.COND_KIND_CN.get(m.get("kind"), m.get("kind")),
                                  m.get("available_at") or "")})
    for c in completion:
        if c["vs_contract_days"] and c["vs_contract_days"] > 0:
            out.append({"what": "对外交期变更", "ref": c["plan_id"],
                        "who": "销售/客户",
                        "why": "预测 %s 晚于合同日期 %s %d 天，改合同日期须客户确认"
                               % (c["forecast_date"], c["contract_date"], c["vs_contract_days"])})
    for u in res.get("unresolved") or []:
        out.append({"what": "数据修正", "ref": u.get("step_id"),
                    "who": "计划员", "why": u.get("reason")})
    seen, uniq = set(), []
    for a in out:
        k = (a["what"], a["ref"])
        if k not in seen:
            seen.add(k)
            uniq.append(dict(a, note="批准 ≠ 执行授权；执行仍须持 WriteGrant 走第一期闸门"))
    return uniq


def _weekday_cn(date_text):
    if not date_text:
        return ""
    try:
        d = _dt.date.fromisoformat(str(date_text)[:10])
    except ValueError:
        return ""
    return "周" + "一二三四五六日"[d.weekday()]


def _delta_days(old, new):
    if not old or not new:
        return None
    try:
        return (_dt.date.fromisoformat(str(new)[:10])
                - _dt.date.fromisoformat(str(old)[:10])).days
    except ValueError:
        return None


__all__ = ["compare_scenarios", "SimulationLedger", "SimulationWriter",
           "apply_mutations"]
