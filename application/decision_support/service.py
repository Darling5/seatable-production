# -*- coding: utf-8 -*-
"""application/decision_support/service.py — 本期门面：把算法组装成「能回答问题的东西」。

对外四个动作，对应四种问法：

  analyze()   —— 「这活什么时候能完？卡在哪？」（交付依赖 + 关键路径 + 三日期）
  compare()   —— 「换个条件能不能好一点？代价多大？要谁点头？」（方案比较）
  backtest()  —— 「我们以前的预测准不准？」（复盘 + 基线对照）
  explain()   —— 「凭什么这么说？」（把每个数字的推理链摊开）

贯穿所有输出的两条纪律：
  · 每个结论都带 `basis`（依据）与 `caveats`（保留意见），
    依据指得到快照/规则版本/假设，保留意见包含二期上下文里的来源时效；
  · 任何「建议执行」的东西只能是 `write_intents`（未授权、未执行），
    绝不自动落地。「方案选择不等于执行授权」在这里是结构而非提醒。
"""
from __future__ import annotations

import datetime as _dt

from . import schema as S
from .backtest import backtest as _backtest
from .calendar import WorkCalendar, default_calendar, from_dict as cal_from_dict
from .ports import (ASSUMPTIONS, MockPlanningSnapshotPort, PlanningSnapshotPort,
                    ProjectContextPort, build_inputs, context_notes,
                    freshness_caveats)
from .resources import detect_conflicts, resource_feasible_schedule
from .scenarios import SimulationLedger, SimulationWriter, compare_scenarios as _compare
from .schedule import build_cpm, propagate_delay


class DecisionSupport:
    """本期算法门面。构造一次，可反复问不同的问题（全部只读）。"""

    def __init__(self, snapshot: dict, calendar: WorkCalendar = None,
                 context: dict = None, ledger: SimulationLedger = None):
        self.snapshot = dict(snapshot or {})
        self.calendar = calendar or (cal_from_dict(self.snapshot.get("calendar") or {})
                                     if self.snapshot.get("calendar") else default_calendar())
        self.context = dict(context or {})
        self.ledger = ledger or SimulationLedger(SimulationWriter())
        self._cpm = None
        self._res = None

    # ── 内部：跑一次并存住结果（同一份快照不重复算）──
    def _run(self, force: bool = False):
        if self._cpm is None or force:
            self._cpm = build_cpm(self.snapshot, self.calendar)
            self._res = (resource_feasible_schedule(self.snapshot, self.calendar,
                                                    cpm=self._cpm)
                         if self._cpm.get("ok") else
                         {"ok": False, "steps": [], "plans": [],
                          "issues": self._cpm.get("issues", []),
                          "unresolved": [], "resources": [], "protected_steps": []})
        return self._cpm, self._res

    # ── 1) 交付依赖 ───────────────────────────────
    def analyze(self) -> dict:
        cpm, res = self._run()
        notes = context_notes(self.context)
        caveats = freshness_caveats(self.context) if self.context else []
        return {
            "contract_version": S.DS_CONTRACT_VERSION,
            "rules_version": S.RULES_VERSION,
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "project_id": self.snapshot.get("project_id") or "",
            "snapshot_id": self.snapshot.get("snapshot_id") or "",
            "as_of": self.snapshot.get("as_of") or "",
            "data_as_of": notes.get("data_as_of") or self.snapshot.get("as_of") or "",
            "calendar": self.calendar.describe(),

            # 三种日期分开放：合同 / 内部计划 / 预测。谁都不覆盖谁。
            "dates": _date_table(cpm, res),

            "cpm": {
                "method": cpm.get("method"), "method_cn": cpm.get("method_cn"),
                "disclaimer": cpm.get("disclaimer"),
                "critical_path": cpm.get("critical_path") or [],
                "project_finish": cpm.get("project_finish"),
                "steps": cpm.get("steps") or [],
                "issues": cpm.get("issues") or [],
            },
            "resource_feasible": {
                "ok": res.get("ok"),
                "method": res.get("method"), "method_cn": res.get("method_cn"),
                "disclaimer": res.get("disclaimer"),
                "plans": res.get("plans") or [],
                "steps": res.get("steps") or [],
                "resources": res.get("resources") or [],
                "conflicts_resolved": res.get("conflicts_resolved") or [],
                "protected_steps": res.get("protected_steps") or [],
                "unresolved": res.get("unresolved") or [],
                "issues": res.get("issues") or [],
            },
            "gaps": _collect_gaps(cpm, res),
            "caveats": caveats,
            "assumptions": ASSUMPTIONS,
            "evidence": {
                "evidence_ids": notes.get("evidence_ids") or [],
                "decision_ids": notes.get("decision_ids") or [],
                "unresolved_conflicts": notes.get("unresolved_conflicts") or [],
                "missing_info": notes.get("missing_info") or [],
                "note": "证据索引只做引用，事实本体在二期 project-brain-v1，本期不复制。",
            },
            "authorization_note": S.AUTH_NOTE,
        }

    # ── 2) 方案比较 ───────────────────────────────
    def compare(self, scenarios=None, cost_model=None) -> dict:
        if scenarios is None:
            scenarios = default_scenarios(self.snapshot)
        return _compare(self.snapshot, scenarios, calendar=self.calendar,
                        cost_model=cost_model, ledger=self.ledger)

    # ── 3) 延期传播 ───────────────────────────────
    def delay_impact(self, step_id: str, extra_hours: float) -> dict:
        return propagate_delay(self.snapshot, step_id, extra_hours, self.calendar)

    # ── 4) 资源冲突（对当前排程）──────────────────
    def conflicts(self) -> dict:
        _, res = self._run()
        alloc = []
        for s in self.snapshot.get("steps") or []:
            row = next((x for x in res.get("steps") or []
                        if x["step_id"] == s.get("step_id")), None)
            if not row:
                continue
            for r in (s.get("resources") or []):
                rid = r if isinstance(r, str) else (r or {}).get("resource_id")
                hours = s.get("duration_hours") if isinstance(r, str) else (r or {}).get("hours")
                alloc.append({"step_id": s.get("step_id"), "plan_id": s.get("plan_id"),
                              "resource_id": rid, "hours": hours,
                              "start": row.get("start"), "finish": row.get("finish"),
                              "frozen": bool(row.get("frozen"))})
        return detect_conflicts(alloc, self.snapshot.get("resources") or [],
                                self.calendar,
                                self.snapshot.get("resource_groups") or [])

    # ── 5) 解释一个工序 ───────────────────────────
    def explain(self, step_id: str = "", plan_id: str = "") -> dict:
        return explain_step(self, step_id=step_id, plan_id=plan_id)


# ────────────────────────── 日期表 ──────────────────────────
def _date_table(cpm: dict, res: dict) -> dict:
    """三日期一览。**forecast 是唯一由算法产出的字段**，另两个只读透传。"""
    by_plan = {}
    for p in (cpm.get("plans") or []):
        by_plan[p["plan_id"]] = {
            "plan_id": p["plan_id"], "name": p.get("name", ""),
            "contract_date": p.get("contract_date") or "",
            "internal_plan_date": p.get("internal_plan_date") or "",
            "forecast_date": p.get("forecast_date"),
            "forecast_status": p.get("forecast_status"),
            "delay_vs_contract_days": p.get("delay_vs_contract_days"),
            "undetermined_reasons": p.get("undetermined_reasons") or [],
            "source": "cpm（仅前置关系口径）",
        }
    for p in (res.get("plans") or []):
        e = by_plan.setdefault(p["plan_id"], {"plan_id": p["plan_id"],
                                              "name": p.get("name", ""),
                                              "contract_date": p.get("contract_date") or "",
                                              "internal_plan_date": p.get("internal_plan_date") or ""})
        e["resource_feasible_date"] = p.get("forecast_date")
        e["resource_feasible_status"] = p.get("forecast_status")
        e["delay_vs_contract_days"] = _delta_days(p.get("contract_date"),
                                                  p.get("forecast_date"))
    return by_plan


def _collect_gaps(cpm: dict, res: dict) -> list[dict]:
    out = []
    for s in (cpm.get("steps") or []):
        for r in (s.get("undetermined_reasons") or []):
            out.append(dict(r, step_id=s["step_id"], plan_id=s.get("plan_id"),
                            stage="cpm"))
    for u in (res.get("unresolved") or []):
        out.append({"code": u.get("reason_code") or "unresolved",
                    "message": u.get("reason"), "step_id": u.get("step_id"),
                    "plan_id": u.get("plan_id"), "stage": "resource_feasible"})
    for i in (res.get("issues") or []) + (cpm.get("issues") or []):
        if i.get("severity") == "warning":
            out.append({"code": i.get("code"), "message": i.get("message"),
                        "ref": i.get("ref"), "stage": "validation"})
    seen, uniq = set(), []
    for g in out:
        k = (g.get("code"), g.get("step_id") or g.get("ref"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(g)
    return uniq


# ────────────────────────── 解释链 ──────────────────────────
def explain_step(ds: DecisionSupport, step_id: str = "", plan_id: str = "") -> dict:
    """把一个工序的日期「为什么是这个数」摊开。

    每条结论都要能被指着说：这个开始时间来自哪条前置、哪个条件、哪个资源让位。
    """
    cpm, res = ds._run()
    if not cpm.get("ok"):
        return {"ok": False, "step_id": step_id,
                "issues": cpm.get("issues") or [],
                "message": "排程不可用（图/日历有硬问题），先修数据再解释"}

    targets = [s for s in cpm["steps"]
               if (step_id and s["step_id"] == step_id)
               or (plan_id and s["plan_id"] == plan_id)]
    if not targets:
        return {"ok": False, "message": "找不到工序 %s / 计划 %s" % (step_id, plan_id)}

    by_id = {s["step_id"]: s for s in cpm["steps"]}
    out = []
    for s in targets:
        reasons = []
        if s["frozen"]:
            reasons.append({"what": "时间锁定", "why": "工序已开始/已完成，"
                            "重排必须绕开它，而不是移动它"})
        for p in s["predecessors"]:
            pr = by_id.get(p) or {}
            reasons.append({
                "what": "前置约束",
                "why": "必须等前置 %s（%s）完成于 %s"
                       % (p, pr.get("name") or "", pr.get("earliest_finish") or "未知"),
            })
        if s.get("condition_gate"):
            reasons.append({"what": "条件放行", "why": "前置条件（已确认）最早可满足于 %s"
                                                   % s["condition_gate"]})
        for r in (s.get("undetermined_reasons") or []):
            reasons.append({"what": "不可确定", "why": r.get("message"),
                            "code": r.get("code")})
        rrow = next((x for x in (res.get("steps") or []) if x["step_id"] == s["step_id"]), None)
        if rrow and rrow.get("shifted_by_resource"):
            reasons.append({"what": "资源让位",
                            "why": "原可 %s 开始，因%s，顺延到 %s"
                                   % (rrow.get("shifted_from"), rrow.get("shift_reason")
                                      or "资源冲突", rrow.get("start"))})
        if s["critical"]:
            reasons.append({"what": "关键路径", "why": "松弛 0 小时："
                            "本工序每延 1 小时，项目完成时间就延 1 小时（仅按前置关系口径）"})
        elif s.get("slack_hours") is not None:
            reasons.append({"what": "有松弛", "why": "松弛 %s 小时："
                            "在此范围内延后不影响项目完成时间（仅按前置关系口径）"
                            % s["slack_hours"]})
        out.append({
            "step_id": s["step_id"], "plan_id": s["plan_id"], "name": s["name"],
            "status": s["status"],
            "dates": {
                "contract_date": s["contract_date"],
                "internal_plan_date": s["internal_plan_date"],
                "forecast_date": s["forecast_date"],
                "forecast_status": s["forecast_status"],
            },
            "reasoning": reasons,
            "basis": {
                "rules_version": S.RULES_VERSION,
                "method": cpm.get("method"),
                "disclaimer": cpm.get("disclaimer"),
                "snapshot_id": ds.snapshot.get("snapshot_id") or "",
                "as_of": ds.snapshot.get("as_of") or "",
            },
        })
    return {"ok": True, "explained": out,
            "calendar": ds.calendar.describe(),
            "disclaimer": cpm.get("disclaimer")}


# ────────────────────────── 默认方案（首个验收样例）──────────────────────────
def default_scenarios(snapshot: dict) -> list[dict]:
    """给快照生成 S0/S1/S2 三档方案骨架。

    变更内容依赖快照里的工序 id；找不到对应工序时该方案只保留基准动作，
    并在 `assumptions` 里说明「未找到目标工序」，而不是硬套一个假 id。
    """
    step_ids = [str(s.get("step_id") or "") for s in (snapshot.get("steps") or [])]
    a_asm = next((s for s in step_ids if s.upper().startswith("A") and "ASM" in s.upper()), "")
    a_cond_step = a_asm or (step_ids[0] if step_ids else "")
    groups = snapshot.get("resource_groups") or []
    test_group = ""
    for g in groups:
        blob = "%s%s" % (g.get("group_id") or "", g.get("name") or "")
        if ("TEST" in blob.upper()) or ("测试" in blob):
            test_group = g.get("group_id") or ""
            break
    cond_at = next((c.get("available_at") for s in (snapshot.get("steps") or [])
                    if str(s.get("step_id")) == a_cond_step
                    for c in (s.get("preconditions") or [])
                    if c.get("kind") == S.COND_MATERIAL), "")
    earlier = _minus_workdays(cond_at, 1) if cond_at else ""
    return [
        {"scenario_id": "S0", "name": "保持条件（基准）", "kind": S.PLAN_BASELINE,
         "mutations": [], "assumptions": ["不做任何变更，按当前条件排程"]},
        {"scenario_id": "S1", "name": "假设 A 材料提前一个工作日到",
         "kind": S.PLAN_CANDIDATE,
         "mutations": ([{"op": "set_condition", "step_id": a_cond_step,
                         "kind": S.COND_MATERIAL, "status": S.COND_ASSUMED,
                         "available_at": earlier,
                         "say": "假设 A 物料 %s 到货（**情景假设**，需采购核实；"
                                "核实不了则本方案不成立）" % earlier}]
                       if (a_cond_step and earlier) else []),
         "assumptions": ["A 物料提前到货是**假设**，需要采购核实；核实不了则本方案不成立"]},
        {"scenario_id": "S2", "name": "仅增加一台测试设备", "kind": S.PLAN_CANDIDATE,
         "mutations": [{"op": "add_resource",
                        "resource": {"resource_id": "RES-TEST-2", "name": "测试台2",
                                     "type": "设备",
                                     "daily_capacity_hours": S.HOURS_PER_DAY,
                                     "assumed": True},
                        "to_group": test_group,
                        "say": "新增一台测试设备并加入测试资源池"
                               "（合成假设，非真实采购）"}],
         "assumptions": ["新增测试设备是**假设**，费用与交期未核实"]},
    ]


def _minus_workdays(date_text: str, n: int) -> str:
    """把日期往前推 n 个工作日（只用来造「提前一天」的假设，不参与正式排程）。"""
    try:
        d = _dt.date.fromisoformat(str(date_text)[:10])
    except ValueError:
        return ""
    step = 0
    while step < n:
        d -= _dt.timedelta(days=1)
        if d.weekday() < 5:
            step += 1
    return d.isoformat()


# ────────────────────────── 模块级便捷函数 ──────────────────────────
def analyze(snapshot: dict, calendar: WorkCalendar = None, context: dict = None) -> dict:
    return DecisionSupport(snapshot, calendar, context).analyze()


def compare(snapshot: dict, scenarios=None, calendar: WorkCalendar = None,
            cost_model: dict = None, context: dict = None) -> dict:
    return DecisionSupport(snapshot, calendar, context).compare(scenarios, cost_model)


def run_backtest(records, samples=None, baseline_samples=None, as_of=None) -> dict:
    return _backtest(records, samples, baseline_samples, as_of)


def load_inputs(project_id: str, *, context_port: ProjectContextPort = None,
                planning_port: PlanningSnapshotPort = None, snapshot_id: str = "",
                brain_root: str = "", mock_context_path: str = "",
                mock_planning_path: str = "") -> dict:
    return build_inputs(project_id, context_port=context_port,
                        planning_port=planning_port, snapshot_id=snapshot_id,
                        brain_root=brain_root, mock_context_path=mock_context_path,
                        mock_planning_path=mock_planning_path)


def from_inputs(inputs: dict) -> DecisionSupport:
    """从 port 组装出来的输入直接造门面。"""
    return DecisionSupport(inputs.get("planning") or {}, context=inputs.get("context") or {})


__all__ = ["DecisionSupport", "analyze", "compare", "run_backtest", "load_inputs",
           "from_inputs", "explain_step", "default_scenarios", "MockPlanningSnapshotPort"]


def _delta_days(old, new):
    if not old or not new:
        return None
    try:
        return (_dt.date.fromisoformat(str(new)[:10])
                - _dt.date.fromisoformat(str(old)[:10])).days
    except ValueError:
        return None
