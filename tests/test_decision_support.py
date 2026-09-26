#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三期「项目决策辅助」验收测试。

━━ 本文件的一条硬纪律 ━━
所有期望值都是**手工推导后写死的常量**（`2026-09-29` 这类），
不调用被测算法去反推期望。否则算法错、期望跟着错，测试全绿而结论全错。

━━ 日历底数（手算，仅供本文件对照）━━
2026-09-26 = 周六 → 09-28 周一、09-29 周二、09-30 周三、
10-01 周四、10-02 周五、10-03 周六、10-05 周一。
样例日历为周一至周五 09:00—17:00、无额外节假日，故 10-01 是工作日。

━━ 首个验收样例（题目给定，本文件独立复现）━━
A、B 两批各需组装 8h、测试 8h；组装资源独立，共享一台测试设备。
  · 材料均周一（09-28）可用 → A 周二（09-29）完成，B 周三（09-30）完成
  · A 材料改为周三（09-30）可用 → A 周四（10-01）完成，B 提前到周二（09-29）完成
  · S2 仅加测试设备、材料仍周三到 → A 交期不改善
"""
import copy
import json
import os
import sys
import unittest
from datetime import date, datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from application.decision_support import backtest as BT                    # noqa: E402
from application.decision_support import schema as S                       # noqa: E402
from application.decision_support.calendar import WorkCalendar             # noqa: E402
from application.decision_support.graph import DependencyGraph             # noqa: E402
from application.decision_support.ports import (MockProjectContextPort,    # noqa: E402
                                                MockPlanningSnapshotPort,
                                                build_inputs, context_notes,
                                                freshness_caveats)
from application.decision_support.resources import ResourceTimeline        # noqa: E402
from application.decision_support.resources import (detect_conflicts,      # noqa: E402
                                                   normalize_resource,
                                                   resource_feasible_schedule)
from application.decision_support.scenarios import (SimulationLedger,      # noqa: E402
                                                   SimulationWriter,
                                                   apply_mutations)
from application.decision_support.schedule import build_cpm, propagate_delay  # noqa: E402
from application.decision_support.service import (DecisionSupport,         # noqa: E402
                                                 default_scenarios)

EXAMPLES = os.path.join(HERE, "docs", "contracts", "examples")
PLANNING_SAMPLE = os.path.join(EXAMPLES, "decision-support-planning-sample.json")
CONTEXT_SAMPLE = os.path.join(EXAMPLES, "decision-support-context-mock.json")
PREDICTIONS_SAMPLE = os.path.join(EXAMPLES, "decision-support-predictions-sample.json")

# ── 手算日期常量（不要用算法算这些）──────────────────────
MON = "2026-09-28"      # 周一
TUE = "2026-09-29"      # 周二
WED = "2026-09-30"      # 周三
THU = "2026-10-01"      # 周四
FRI = "2026-10-02"      # 周五
SAT = "2026-10-03"      # 周六
NEXT_MON = "2026-10-05"  # 下周一


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def snap(**over):
    s = load(PLANNING_SAMPLE)
    s.update(over)
    return s


def with_a_material_available(date_text):
    """把 A 批物料可用日改成指定日期，其余保持样例原样。"""
    s = copy.deepcopy(load(PLANNING_SAMPLE))
    for st in s["steps"]:
        if st["step_id"] == "STP-A-ASM":
            st["preconditions"][0]["available_at"] = date_text
    return s


def plan_finish(result, plan_id):
    """从 analyze() 结果里取某计划的**资源可行**完成日。"""
    for p in result["resource_feasible"]["plans"]:
        if p["plan_id"] == plan_id:
            return p["forecast_date"]
    return None


def step_row(result, step_id, key="resource_feasible"):
    for s in result[key]["steps"]:
        if s["step_id"] == step_id:
            return s
    return None


# ══════════════════════════ 1. 日期边界 ══════════════════════════
class CalendarBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.cal = WorkCalendar()          # 周一至周五 09:00—17:00，无节假日

    def test_weekend_is_not_workday(self):
        self.assertFalse(self.cal.is_workday(date(2026, 10, 3)))   # 周六
        self.assertFalse(self.cal.is_workday(date(2026, 10, 4)))   # 周日
        self.assertTrue(self.cal.is_workday(date(2026, 10, 5)))    # 周一

    def test_full_day_from_monday_morning_ends_same_day(self):
        fin = self.cal.add_working_hours(datetime(2026, 9, 28, 9, 0), 8)
        self.assertEqual(fin, datetime(2026, 9, 28, 17, 0))
        self.assertEqual(self.cal.finish_date(fin), date(2026, 9, 28))

    def test_friday_evening_rolls_over_weekend_to_monday(self):
        fin = self.cal.add_working_hours(datetime(2026, 10, 2, 17, 0), 8)
        self.assertEqual(fin, datetime(2026, 10, 5, 17, 0))

    def test_start_after_hours_aligns_to_next_workday(self):
        self.assertEqual(self.cal.align_forward(datetime(2026, 9, 28, 20, 0)),
                         datetime(2026, 9, 29, 9, 0))

    def test_start_in_weekend_aligns_to_monday(self):
        self.assertEqual(self.cal.align_forward(datetime(2026, 10, 3, 10, 0)),
                         datetime(2026, 10, 5, 9, 0))

    def test_4h_plus_4h_spans_two_days(self):
        """4 小时跨天下班：09-28 09:00 + 4h = 13:00，再 4h 必须落次日。"""
        mid = self.cal.add_working_hours(datetime(2026, 9, 28, 9, 0), 4)
        self.assertEqual(mid, datetime(2026, 9, 28, 13, 0))
        fin = self.cal.add_working_hours(datetime(2026, 9, 28, 14, 0), 4)
        self.assertEqual(fin, datetime(2026, 9, 29, 10, 0))

    def test_holiday_actually_shifts_finish(self):
        """把 10-01 设成节假日：完成日必须整体后移 —— 证明日历真在驱动。"""
        cal = WorkCalendar(holidays=[THU])
        fin = cal.add_working_hours(datetime(2026, 9, 30, 17, 0), 8)
        self.assertEqual(fin, datetime(2026, 10, 2, 17, 0))

    def test_extra_workday_overrides_weekend(self):
        cal = WorkCalendar(extra_workdays=[SAT])
        fin = cal.add_working_hours(datetime(2026, 10, 2, 17, 0), 8)
        self.assertEqual(fin, datetime(2026, 10, 3, 17, 0))

    def test_sub_working_hours_is_inverse(self):
        ls = self.cal.sub_working_hours(datetime(2026, 10, 5, 17, 0), 8)
        self.assertEqual(ls, datetime(2026, 10, 5, 9, 0))

    def test_zero_duration_returns_aligned_start(self):
        self.assertEqual(self.cal.add_working_hours(datetime(2026, 10, 3, 10, 0), 0),
                         datetime(2026, 10, 5, 9, 0))


# ══════════════════════════ 2. 图校验 ══════════════════════════
class GraphValidationTest(unittest.TestCase):
    def _snap(self, steps):
        return {"snapshot_id": "SNP-T", "as_of": "2026-09-28T08:00:00",
                "planning_start": "2026-09-28T09:00:00",
                "calendar": {"workdays": [0, 1, 2, 3, 4], "start_hour": 9, "end_hour": 17},
                "steps": steps}

    def _step(self, sid, preds=(), dur=8, plan="P1"):
        return {"step_id": sid, "plan_id": plan, "name": sid, "duration_hours": dur,
                "predecessors": list(preds), "resources": []}

    def test_cycle_is_detected_and_blocks_schedule(self):
        s = self._snap([self._step("X", ["Z"]), self._step("Y", ["X"]), self._step("Z", ["Y"])])
        r = build_cpm(s)
        self.assertFalse(r["ok"])
        codes = {i["code"] for i in r["issues"]}
        self.assertIn(S.GAP_CYCLE, codes)

    def test_self_loop_detected(self):
        g = DependencyGraph(["A"])
        g.add_edge("A", "A")
        g.validate()
        self.assertIn(S.GAP_SELF_LOOP, {i.code for i in g.issues})

    def test_missing_predecessor_reported_not_swallowed(self):
        s = self._snap([self._step("A", ["GHOST"])])
        r = build_cpm(s)
        self.assertIn(S.GAP_MISSING_PREDECESSOR, {i["code"] for i in r["issues"]})

    def test_duplicate_edge_is_warning_only(self):
        g = DependencyGraph(["A", "B"])
        g.add_edge("A", "B")
        g.add_edge("A", "B")
        g.validate()
        codes = {i.code for i in g.issues}
        self.assertIn(S.GAP_DUP_EDGE, codes)
        self.assertFalse(g.has_blocking_issue())
        self.assertEqual(g.topo_order(), ["A", "B"])

    def test_topo_order_is_deterministic(self):
        g = DependencyGraph(["C", "A", "B"])
        g.add_edge("A", "C")
        g.add_edge("B", "C")
        self.assertEqual(g.topo_order(), g.topo_order())

    def test_valid_diamond_schedule_ok(self):
        s = self._snap([self._step("A"), self._step("B", ["A"]),
                        self._step("C", ["A"]), self._step("D", ["B", "C"])])
        self.assertTrue(build_cpm(s)["ok"])


# ══════════════════════════ 3. 交付依赖（含首个验收样例）══════════════════════════
class DeliveryScheduleTest(unittest.TestCase):
    """样例期望值全部来自题目给定，手工对照日历得出。"""

    def test_case1_materials_monday_a_tue_b_wed(self):
        r = DecisionSupport(with_a_material_available(MON)).analyze()
        self.assertEqual(plan_finish(r, "PLN-20260928-A"), TUE)     # A 周二
        self.assertEqual(plan_finish(r, "PLN-20260928-B"), WED)     # B 周三

    def test_case2_a_material_wednesday_a_thu_b_tue(self):
        r = DecisionSupport(snap()).analyze()
        self.assertEqual(plan_finish(r, "PLN-20260928-A"), THU)     # A 周四
        self.assertEqual(plan_finish(r, "PLN-20260928-B"), TUE)     # B 提前到周二

    def test_delaying_a_material_lets_b_move_earlier(self):
        """Case1 → Case2：A 材料推迟，B 反而提前一天（允许重排未开始工序）。"""
        before = DecisionSupport(with_a_material_available(MON)).analyze()
        after = DecisionSupport(snap()).analyze()
        self.assertEqual(plan_finish(before, "PLN-20260928-B"), WED)
        self.assertEqual(plan_finish(after, "PLN-20260928-B"), TUE)
        d1 = date.fromisoformat(plan_finish(after, "PLN-20260928-B"))
        d0 = date.fromisoformat(plan_finish(before, "PLN-20260928-B"))
        self.assertEqual((d1 - d0).days, -1)

    def test_step_timestamps_case2(self):
        r = DecisionSupport(snap()).analyze()
        self.assertEqual(step_row(r, "STP-B-ASM")["start"], "2026-09-28T09:00")
        self.assertEqual(step_row(r, "STP-B-TEST")["start"], "2026-09-29T09:00")
        self.assertEqual(step_row(r, "STP-A-ASM")["start"], "2026-09-30T09:00")
        self.assertEqual(step_row(r, "STP-A-TEST")["start"], "2026-10-01T09:00")

    def test_critical_path_is_precedence_only(self):
        r = DecisionSupport(snap()).analyze()
        self.assertEqual(r["cpm"]["critical_path"], ["STP-A-ASM", "STP-A-TEST"])
        self.assertIn("非资源约束下的最优排程", r["cpm"]["disclaimer"])

    def test_unknown_duration_blocks_self_and_downstream(self):
        s = snap()
        r = build_cpm(s)
        c_test = next(x for x in r["steps"] if x["step_id"] == "STP-C-TEST")
        self.assertIsNone(c_test["forecast_date"])
        self.assertEqual(c_test["forecast_status"], "undetermined")
        self.assertIn(S.GAP_UNKNOWN_DURATION,
                      {x["code"] for x in c_test["undetermined_reasons"]})

    def test_unpaid_condition_yields_undetermined_not_a_date(self):
        r = DecisionSupport(snap()).analyze()
        c = next(p for p in r["resource_feasible"]["plans"]
                 if p["plan_id"] == "PLN-20260928-C")
        self.assertIsNone(c["forecast_date"])
        self.assertEqual(c["forecast_status"], "undetermined")

    def test_contract_date_is_never_overwritten_by_forecast(self):
        """预测与合同不同也要各归各：合同日期必须原样读出。"""
        s = snap()
        r = DecisionSupport(s).analyze()
        a = r["dates"]["PLN-20260928-A"]
        self.assertEqual(a["contract_date"], "2026-10-09")          # 输入值
        self.assertEqual(a["resource_feasible_date"], THU)          # 算法产出
        # 输出里的合同日期必须与输入快照完全一致
        src = next(p for p in s["plans"] if p["plan_id"] == "PLN-20260928-A")
        self.assertEqual(a["contract_date"], src["contract_date"])

    def test_three_date_kinds_kept_separate(self):
        r = DecisionSupport(snap()).analyze()
        a = r["dates"]["PLN-20260928-A"]
        self.assertEqual(a["contract_date"], "2026-10-09")
        self.assertEqual(a["internal_plan_date"], "2026-10-02")
        self.assertEqual(a["resource_feasible_date"], THU)
        self.assertEqual(len({a["contract_date"], a["internal_plan_date"],
                              a["resource_feasible_date"]}), 3)

    def test_delay_vs_contract_computed(self):
        r = DecisionSupport(snap()).analyze()
        a = r["dates"]["PLN-20260928-A"]
        # 预测 10-01，合同 10-09 → 早 8 天（负数=提前）
        self.assertEqual(a["delay_vs_contract_days"], -8)

    def test_holiday_config_changes_forecast(self):
        """把 10-01 设为节假日，A 的交期必须后移一天 —— 结果由日历决定。"""
        s = snap()
        s["calendar"]["holidays"] = [THU]
        r = DecisionSupport(s).analyze()
        self.assertEqual(plan_finish(r, "PLN-20260928-A"), FRI)


# ══════════════════════════ 4. 资源冲突与保护 ══════════════════════════
class ResourceConflictTest(unittest.TestCase):
    def test_shared_test_equipment_serializes(self):
        """Case1：两批同日完成组装，共用一台测试台 → 后到的必须顺延一天。"""
        r = DecisionSupport(with_a_material_available(MON)).analyze()
        self.assertEqual(step_row(r, "STP-A-TEST")["start"], "2026-09-29T09:00")
        self.assertEqual(step_row(r, "STP-B-TEST")["start"], "2026-09-30T09:00")
        shifted = [c for c in r["resource_feasible"]["conflicts_resolved"]
                   if c["step_id"] == "STP-B-TEST"]
        self.assertEqual(len(shifted), 1)
        self.assertIn("2026-09-29T09:00", shifted[0]["shifted_from"])

    def test_no_conflict_when_batches_do_not_compete(self):
        r = DecisionSupport(snap()).analyze()
        self.assertEqual(r["resource_feasible"]["conflicts_resolved"], [])

    def test_detect_conflicts_finds_daily_overload(self):
        cal = WorkCalendar()
        resources = [normalize_resource({"resource_id": "R1", "name": "测试台",
                                         "daily_capacity_hours": 8})]
        alloc = [
            {"step_id": "S1", "resource_id": "R1", "hours": 8,
             "start": "2026-09-29T09:00", "finish": "2026-09-29T17:00"},
            {"step_id": "S2", "resource_id": "R1", "hours": 8,
             "start": "2026-09-29T09:00", "finish": "2026-09-29T17:00"},
        ]
        res = detect_conflicts(alloc, resources, cal)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["overloads"]), 1)
        self.assertAlmostEqual(res["overloads"][0]["demand_hours"], 16.0)
        self.assertEqual(res["overloads"][0]["capacity_hours"], 8.0)

    def test_time_overlap_detected(self):
        cal = WorkCalendar()
        resources = [normalize_resource({"resource_id": "R1", "daily_capacity_hours": 8})]
        alloc = [
            {"step_id": "S1", "resource_id": "R1", "hours": 4,
             "start": "2026-09-29T09:00", "finish": "2026-09-29T13:00"},
            {"step_id": "S2", "resource_id": "R1", "hours": 4,
             "start": "2026-09-29T11:00", "finish": "2026-09-29T15:00"},
        ]
        res = detect_conflicts(alloc, resources, cal)
        self.assertEqual(len(res["time_overlaps"]), 1)

    def test_frozen_step_keeps_its_time_and_blocks_others(self):
        """进行中的工序：时间锁死，且它占的设备别人抢不走。"""
        s = snap()
        for st in s["steps"]:
            if st["step_id"] == "STP-A-ASM":
                st["status"] = "in_progress"
                st["actual_start"] = "2026-09-28T09:00"
            if st["step_id"] == "STP-B-ASM":
                st["status"] = "in_progress"
                st["actual_start"] = "2026-09-28T09:00"
        r = DecisionSupport(s).analyze()
        a = step_row(r, "STP-A-ASM")
        self.assertTrue(a["frozen"])
        self.assertEqual(a["start"], "2026-09-28T09:00")     # 未被挪动
        self.assertEqual(a["finish"], "2026-09-28T17:00")
        prot = {p["step_id"] for p in r["resource_feasible"]["protected_steps"]}
        self.assertIn("STP-A-ASM", prot)
        self.assertIn("STP-B-ASM", prot)

    def test_unknown_capacity_is_flagged_not_silently_ignored(self):
        cal = WorkCalendar()
        tl = ResourceTimeline([normalize_resource({"resource_id": "RX", "name": "外协"})])
        self.assertTrue(any(i["code"] == S.GAP_RESOURCE_UNKNOWN_CAP for i in tl.issues))

    def test_missing_resource_is_reported(self):
        s = snap()
        for st in s["steps"]:
            if st["step_id"] == "STP-A-ASM":
                st["resources"] = [{"resource_id": "RES-NOT-EXIST", "hours": 8}]
        r = DecisionSupport(s).analyze()
        codes = {i.get("code") for i in r["resource_feasible"]["issues"]}
        self.assertIn(S.GAP_NO_RESOURCE, codes)


# ══════════════════════════ 5. 延期传播 ══════════════════════════
class DelayPropagationTest(unittest.TestCase):
    def test_delay_propagates_downstream(self):
        d = propagate_delay(with_a_material_available(MON), "STP-A-ASM", 8)
        self.assertTrue(d["ok"])
        moved = {x["step_id"]: x["delta_finish_days"] for x in d["affected_steps"]}
        self.assertEqual(moved.get("STP-A-ASM"), 1)
        self.assertEqual(moved.get("STP-A-TEST"), 1)      # 传播到下游
        self.assertEqual([p["plan_id"] for p in d["affected_plans"]],
                         ["PLN-20260928-A"])
        self.assertEqual(d["affected_plans"][0]["delta_days"], 1)

    def test_slack_absorbs_delay(self):
        """下游有 1 天松弛时，延 8 小时不该把它推后。"""
        s = {
            "snapshot_id": "SNP-SLACK", "as_of": "2026-09-28T08:00:00",
            "planning_start": "2026-09-28T09:00:00",
            "calendar": {"workdays": [0, 1, 2, 3, 4], "start_hour": 9, "end_hour": 17},
            "steps": [
                {"step_id": "P", "plan_id": "PL", "name": "前置", "duration_hours": 8},
                {"step_id": "Q", "plan_id": "PL", "name": "快支线",
                 "duration_hours": 8, "predecessors": ["P"]},
                {"step_id": "R", "plan_id": "PL", "name": "慢支线",
                 "duration_hours": 24, "predecessors": ["P"]},
                {"step_id": "Z", "plan_id": "PL", "name": "汇合",
                 "duration_hours": 8, "predecessors": ["Q", "R"]},
            ],
        }
        d = propagate_delay(s, "Q", 8)
        self.assertTrue(d["ok"])
        q = next(x for x in d["affected_steps"] if x["step_id"] == "Q")
        self.assertEqual(q["delta_finish_days"], 1)
        # Z 的总工期由 R 决定，Q 延一天仍在其松弛内 → Z 不该动
        self.assertNotIn("Z", {x["step_id"] for x in d["affected_steps"]})

    def test_delay_on_unknown_duration_refused(self):
        d = propagate_delay(snap(), "STP-C-TEST", 8)
        self.assertFalse(d["ok"])
        self.assertEqual(d["reason_code"], S.GAP_UNKNOWN_DURATION)

    def test_delay_reports_resource_level_impact_separately(self):
        d = propagate_delay(with_a_material_available(MON), "STP-A-ASM", 8)
        self.assertIn("resource_level_impact", d)
        self.assertIn("仅前置口径", d["basis"])


# ══════════════════════════ 6. 方案比较 ══════════════════════════
class ScenarioCompareTest(unittest.TestCase):
    def setUp(self):
        self.snap = snap()
        self.writer = SimulationWriter()
        self.ledger = SimulationLedger(self.writer)
        self.ds = DecisionSupport(self.snap, ledger=self.ledger)
        self.res = self.ds.compare(cost_model=self.snap.get("cost_model"))

    def _sc(self, sid):
        return next(s for s in self.res["scenarios"] if s["scenario_id"] == sid)

    def _finish(self, sid, plan_id):
        for c in self._sc(sid)["completion"]:
            if c["plan_id"] == plan_id:
                return c["forecast_date"]
        return None

    def test_s0_is_baseline_and_matches_acceptance_case2(self):
        self.assertTrue(self._sc("S0")["is_baseline"])
        self.assertEqual(self._finish("S0", "PLN-20260928-A"), THU)
        self.assertEqual(self._finish("S0", "PLN-20260928-B"), TUE)

    def test_s1_brings_a_one_day_earlier(self):
        self.assertEqual(self._finish("S1", "PLN-20260928-A"), WED)   # 周三
        delta = next(c for c in self._sc("S1")["completion"]
                     if c["plan_id"] == "PLN-20260928-A")["delta_days"]
        self.assertEqual(delta, -1)

    def test_s1_result_is_conditional_not_determined(self):
        """情景假设撑起来的日期只能是 conditional —— 它不是确定结论。"""
        c = next(x for x in self._sc("S1")["completion"]
                 if x["plan_id"] == "PLN-20260928-A")
        self.assertEqual(c["forecast_status"], "conditional")
        c0 = next(x for x in self._sc("S0")["completion"]
                  if x["plan_id"] == "PLN-20260928-A")
        self.assertEqual(c0["forecast_status"], "determined")

    def test_conditional_flag_survives_into_analyze_dates(self):
        s = snap()
        sc = next(x for x in default_scenarios(s) if x["scenario_id"] == "S1")
        mutated, _ = apply_mutations(s, sc["mutations"])
        r = DecisionSupport(mutated).analyze()
        a = r["dates"]["PLN-20260928-A"]
        self.assertEqual(a["resource_feasible_status"], "conditional")
        self.assertEqual(a["resource_feasible_date"], WED)

    def test_s2_does_not_improve_a(self):
        """题目明确要求：仅加测试设备、材料仍周三到 → A 交期不改善。"""
        self.assertEqual(self._finish("S2", "PLN-20260928-A"), THU)
        self.assertEqual(self._sc("S2")["affected_projects"], [])

    def test_s2_explains_the_new_resource_was_never_used(self):
        msgs = " ".join(c["message"] for c in self._sc("S2")["open_conditions"])
        self.assertIn("RES-TEST-2", msgs)
        self.assertIn("未被用到", msgs)

    def test_every_scenario_carries_six_required_fields(self):
        for sc in self.res["scenarios"]:
            for k in ("completion", "delta_cost", "affected_projects",
                      "open_conditions", "required_approvals", "basis"):
                self.assertIn(k, sc, "%s 缺字段 %s" % (sc["scenario_id"], k))

    def test_s1_has_open_conditions_and_required_approvals(self):
        s1 = self._sc("S1")
        self.assertTrue(s1["open_conditions"])
        self.assertTrue(any(a["what"] == "核实前置条件" for a in s1["required_approvals"]))
        for a in s1["required_approvals"]:
            self.assertIn("批准 ≠ 执行授权", a["note"])

    def test_zero_business_writes(self):
        """模拟零业务写入：写入端一次都没被调用，且所有执行意图都未授权。"""
        self.assertEqual(self.writer.calls, [])
        led = self.res["simulation_ledger"]
        self.assertEqual(led["executed_total"], 0)
        self.assertEqual(led["writer_calls"], 0)
        self.assertEqual(led["rejected_total"], led["intents_total"])
        for i in self.ledger.intents:
            self.assertFalse(i["authorized"])
            self.assertFalse(i["executed"])
            self.assertEqual(i["gate"], S.GATE_MODULE)

    def test_simulation_does_not_mutate_input_snapshot(self):
        before = json.dumps(self.snap, ensure_ascii=False, sort_keys=True)
        self.ds.compare(cost_model=self.snap.get("cost_model"))
        after = json.dumps(self.snap, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)

    def test_apply_mutations_returns_new_object(self):
        src = snap()
        out, log = apply_mutations(src, [{"op": "set_condition",
                                          "step_id": "STP-A-ASM",
                                          "kind": "material",
                                          "status": "assumed",
                                          "available_at": TUE}])
        self.assertIsNot(out, src)
        self.assertEqual(log[0]["applied_to"], 1)
        for st in src["steps"]:
            if st["step_id"] == "STP-A-ASM":
                self.assertNotEqual(st["preconditions"][0]["available_at"], TUE)

    def test_delta_cost_is_marked_synthetic(self):
        for sc in self.res["scenarios"]:
            self.assertTrue(sc["delta_cost"]["is_synthetic"])
            self.assertIn("合成假设", sc["delta_cost"]["note"])

    def test_s1_cost_points_to_a_basis(self):
        dc = self._sc("S1")["delta_cost"]
        self.assertEqual(len(dc["items"]), 1)
        self.assertIn("expedite_fee", dc["items"][0]["basis"])

    def test_unresolved_steps_surface_as_open_conditions(self):
        s2 = self._sc("S0")["open_conditions"]
        codes = {c.get("code") for c in s2}
        self.assertTrue({S.GAP_UNKNOWN_DURATION,
                         S.GAP_UNCONFIRMED_PRECONDITION} & codes)

    def test_writer_raises_if_someone_actually_tries_to_write(self):
        with self.assertRaises(PermissionError):
            self.writer.append_row("生产计划", {"x": 1})


# ══════════════════════════ 7. 预测复盘与防未来信息 ══════════════════════════
class BacktestNoLookaheadTest(unittest.TestCase):
    def setUp(self):
        self.data = load(PREDICTIONS_SAMPLE)
        self.samples = self.data["samples"]
        self.preds = self.data["predictions"]

    def test_median_ignores_samples_captured_after_as_of(self):
        """核心断言：塞进一条 as_of 之后采集的极端样本，中位工期必须纹丝不动。"""
        base = BT.history_median_duration(self.samples, "2026-08-31T00:00:00")
        self.assertEqual(base["value_hours"], 25.5)      # 手算：[22,24,25,26,28,30] → 25.5
        self.assertEqual(base["n_samples"], 6)
        poisoned = self.samples + [{
            "sample_id": "H-FUTURE", "duration_hours": 9999,
            "captured_at": "2026-09-27T10:00:00"}]
        after = BT.history_median_duration(poisoned, "2026-08-31T00:00:00")
        self.assertEqual(after["value_hours"], 25.5)     # 不受影响
        self.assertEqual(after["n_samples"], 6)

    def test_filter_as_of_drops_records_without_timestamp(self):
        """无法证明它在预测之前存在，就不能用它。"""
        recs = [{"sample_id": "ok", "captured_at": "2026-08-01T00:00:00"},
                {"sample_id": "no_ts"},
                {"sample_id": "later", "captured_at": "2026-09-30T00:00:00"}]
        kept = [r["sample_id"] for r in BT.filter_as_of(recs, "2026-08-31T00:00:00")]
        self.assertEqual(kept, ["ok"])

    def test_audit_flags_future_information(self):
        rec = {"prediction_id": "P", "predicted_at": "2026-08-10T09:00:00",
               "sample_ids": ["H-01", "H-99"]}
        samples = self.samples + [{"sample_id": "H-99", "duration_hours": 10,
                                   "captured_at": "2026-09-01T00:00:00"}]
        audit = BT.audit_no_lookahead(rec, samples)
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["violations"][0]["type"], "future_information")

    def test_audit_passes_on_clean_sample(self):
        rep = BT.backtest(self.preds, self.samples, self.samples,
                          as_of=datetime(2026, 9, 28, 9, 0))
        self.assertTrue(rep["no_lookahead_ok"])
        self.assertEqual(rep["no_lookahead_violations"], [])

    def test_accuracy_matches_hand_computed_values(self):
        """手算：误差 +2 / -2 / 0 / +4 / +3 → MAE 2.2，偏差 +1.4，命中 1/5。"""
        rep = BT.backtest(self.preds, self.samples, self.samples,
                          as_of=datetime(2026, 9, 28, 9, 0))
        s = rep["summary"]
        self.assertEqual(s["n_reviewed"], 5)
        self.assertEqual(s["accuracy_status"], "ok")
        self.assertEqual(s["mae_days"], 2.2)
        self.assertEqual(s["bias_days"], 1.4)
        self.assertEqual(s["hit_rate"], 0.2)

    def test_undetermined_prediction_is_not_reviewed(self):
        rep = BT.backtest(self.preds, self.samples, self.samples,
                          as_of=datetime(2026, 9, 28, 9, 0))
        self.assertEqual(rep["summary"]["n_undetermined"], 1)

    def test_foresee_alignment_uses_shared_verdict_table(self):
        rep = BT.backtest(self.preds, self.samples, self.samples,
                          as_of=datetime(2026, 9, 28, 9, 0))
        self.assertEqual(rep["foresee_alignment"],
                         {"预警正确": 2, "正确": 2, "漏报": 1})
        self.assertIn("foresee", rep["verdict_source"])

    def test_insufficient_sample_gives_no_numbers(self):
        rep = BT.backtest(self.preds[:2], self.samples, self.samples,
                          as_of=datetime(2026, 9, 28, 9, 0))
        s = rep["summary"]
        self.assertEqual(s["accuracy_status"], "insufficient_sample")
        self.assertNotIn("mae_days", s)
        self.assertIn("不给准确率数字", s["message"])

    def test_no_baseline_sample_states_so(self):
        b = BT.history_median_duration([], "2026-08-31T00:00:00")
        self.assertIsNone(b["value_hours"])
        self.assertEqual(b["status"], S.ACCURACY_UNDEFINED)

    def test_record_requires_predicted_at(self):
        """没有预测时点就没有复盘可言 —— 审计必须直接点出来。"""
        audit = BT.audit_no_lookahead({"prediction_id": "X"}, self.samples)
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["violations"][0]["type"], "missing_predicted_at")


# ══════════════════════════ 8. 二期契约适配 ══════════════════════════
class PortsContractTest(unittest.TestCase):
    def setUp(self):
        self.ctx = load(CONTEXT_SAMPLE)
        self.plan = load(PLANNING_SAMPLE)

    def test_mock_port_reads_contract_shaped_context(self):
        port = MockProjectContextPort(self.ctx)
        got = port.get_project_context("PRJ-20260928-0001")
        self.assertEqual(got["contract_version"], S.BRAIN_CONTRACT_VERSION)
        self.assertIn("source_freshness", got)

    def test_stale_source_becomes_caveat(self):
        caveats = freshness_caveats(self.ctx)
        self.assertTrue(any(c["code"] == S.GAP_STALE_SNAPSHOT for c in caveats))

    def test_context_notes_do_not_copy_facts(self):
        notes = context_notes(self.ctx)
        self.assertIn("evidence_ids", notes)
        self.assertNotIn("facts", notes)          # 事实的家在二期，不复制
        self.assertEqual(notes["contract_version"], S.BRAIN_CONTRACT_VERSION)

    def test_unresolved_conflict_is_surfaced(self):
        notes = context_notes(self.ctx)
        self.assertEqual(len(notes["unresolved_conflicts"]), 1)

    def test_build_inputs_assembles_both_ports(self):
        out = build_inputs("PRJ-20260928-0001",
                           context_port=MockProjectContextPort(self.ctx),
                           planning_port=MockPlanningSnapshotPort(self.plan))
        self.assertEqual(out["context_source"], "mock")
        self.assertEqual(out["planning_source"], "mock")
        self.assertEqual(out["planning"]["snapshot_id"], "SNP-20260928-0830")
        self.assertTrue(out["assumptions"])

    def test_assumptions_are_declared(self):
        ids = {a["id"] for a in __import__(
            "application.decision_support.ports", fromlist=["x"]).ASSUMPTIONS}
        self.assertTrue({"A1", "A2", "A3", "A4", "A5", "A6"} <= ids)

    def test_brain_port_falls_back_when_unavailable(self):
        from application.decision_support.ports import BrainProjectContextPort
        port = BrainProjectContextPort("")
        got = port.get_project_context("PRJ-20260928-0001")
        self.assertTrue(got.get("error") or got.get("contract_version"))

    def test_analyze_carries_context_caveats(self):
        ds = DecisionSupport(self.plan, context=self.ctx)
        r = ds.analyze()
        self.assertTrue(any(c["code"] == S.GAP_STALE_SNAPSHOT for c in r["caveats"]))


# ══════════════════════════ 9. 契约形状与可复现 ══════════════════════════
class ContractShapeTest(unittest.TestCase):
    def test_analyze_has_expected_top_level_keys(self):
        r = DecisionSupport(snap()).analyze()
        for k in ("contract_version", "rules_version", "project_id", "snapshot_id",
                  "as_of", "data_as_of", "calendar", "dates", "cpm",
                  "resource_feasible", "gaps", "caveats", "assumptions",
                  "evidence", "authorization_note"):
            self.assertIn(k, r)
        self.assertEqual(r["contract_version"], S.DS_CONTRACT_VERSION)

    def test_version_and_disclaimers_present(self):
        r = DecisionSupport(snap()).analyze()
        self.assertEqual(r["rules_version"], S.RULES_VERSION)
        self.assertIn("非资源约束下的最优排程", r["resource_feasible"]["disclaimer"])
        self.assertIn("不等于执行授权", r["authorization_note"])

    def test_every_conclusion_has_basis(self):
        r = DecisionSupport(snap()).analyze()
        for p in r["resource_feasible"]["plans"]:
            self.assertIn("step_ids", p)
        e = DecisionSupport(snap()).explain(step_id="STP-A-TEST")
        self.assertTrue(e["ok"])
        self.assertTrue(e["explained"][0]["reasoning"])
        self.assertIn("rules_version", e["explained"][0]["basis"])

    def test_explain_lists_predecessor_and_resource_reasons(self):
        e = DecisionSupport(snap()).explain(step_id="STP-B-TEST")
        kinds = {r["what"] for r in e["explained"][0]["reasoning"]}
        self.assertIn("前置约束", kinds)

    def test_schedule_is_deterministic(self):
        a = DecisionSupport(snap()).analyze()
        b = DecisionSupport(snap()).analyze()
        pick = lambda r: [(s["step_id"], s["start"], s["finish"])
                          for s in r["resource_feasible"]["steps"]]
        self.assertEqual(pick(a), pick(b))

    def test_default_scenarios_ids(self):
        ids = [s["scenario_id"] for s in default_scenarios(snap())]
        self.assertEqual(ids, ["S0", "S1", "S2"])

    def test_public_exports_importable(self):
        import application.decision_support as DS
        for name in DS.__all__:
            self.assertTrue(hasattr(DS, name), "缺少导出：%s" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
