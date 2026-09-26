# -*- coding: utf-8 -*-
"""application/decision_support — 第三期「项目决策辅助」（离线算法）。

本期四个能力：

  交付依赖   calendar / graph / schedule —— 工作日历、前置关系、工期、
                                          拓扑与循环校验、延期向下游传播、关键路径
  资源与方案  resources / scenarios      —— 人员·设备·外协冲突、保护已开始/已完成工序、
                                          基准 vs 候选方案比较（完成时间/增量成本/
                                          影响项目/待核实条件/所需批准/依据）
  预测复盘   backtest                    —— 预测时点·输入快照·规则版本·假设·样本范围·
                                          实际结果；回测不用未来信息；与 foresee 口径
                                          及历史中位工期基线对照
  契约适配   ports                       —— 读第二期 project-brain-v1 的 ProjectContext，
                                          所有跨期假设集中于此

三条边界（与第二期共同契约一致，本期不越界）：
  · 合同日期 / 内部计划日期 / 预测日期分开存，永不互相覆盖；
  · 未知工期与未确认前置条件（如未收款）不产出伪确定交期，只给原因码；
  · 方案选择不等于执行授权 —— 一切执行走第一期 application.authorization 闸门。

用法：
    from application.decision_support import DecisionSupport, compare_scenarios

    ds = DecisionSupport(snapshot)
    ds.analyze()          # 交付依赖：三日期 + 关键路径 + 资源可行排程
    ds.compare()          # S0/S1/S2 方案比较
    ds.explain("STP-A-ASM")   # 这条日期凭什么这么算
"""
from . import (backtest, calendar, graph, ports, resources, scenarios, schedule,
               schema, service)
from .backtest import (HIT_TOLERANCE_DAYS, MIN_SAMPLE, VERDICT_SOURCE, audit_no_lookahead,
                       backtest as backtest_predictions, filter_as_of,
                       history_median_duration, make_record_from_schedule,
                       naive_baseline_finish, review_prediction)
from .calendar import WorkCalendar, default_calendar
from .graph import DependencyGraph, GraphIssue
from .ports import (ASSUMPTIONS, BrainProjectContextPort, MockProjectContextPort,
                    MockPlanningSnapshotPort, PlanningSnapshotPort,
                    ProjectContextPort, build_inputs)
from .resources import ResourceTimeline, detect_conflicts, resource_feasible_schedule
from .scenarios import (SimulationLedger, SimulationWriter, apply_mutations,
                        compare_scenarios)
from .schedule import build_cpm, propagate_delay
from .schema import (BRAIN_CONTRACT_VERSION, DS_CONTRACT_VERSION, RULES_VERSION,
                     SCHEDULE_DISCLAIMER)
from .service import (DecisionSupport, analyze, compare, explain_step, from_inputs,
                      load_inputs, run_backtest)

__all__ = [
    # 门面
    "DecisionSupport", "analyze", "compare", "explain_step", "load_inputs",
    "from_inputs", "run_backtest",
    # 交付依赖
    "WorkCalendar", "default_calendar", "DependencyGraph", "GraphIssue",
    "build_cpm", "propagate_delay",
    # 资源与方案
    "ResourceTimeline", "detect_conflicts", "resource_feasible_schedule",
    "SimulationLedger", "SimulationWriter", "apply_mutations", "compare_scenarios",
    # 复盘
    "backtest_predictions", "review_prediction", "audit_no_lookahead", "filter_as_of",
    "history_median_duration", "make_record_from_schedule", "naive_baseline_finish",
    "VERDICT_SOURCE", "MIN_SAMPLE", "HIT_TOLERANCE_DAYS",
    # 适配层
    "ProjectContextPort", "MockProjectContextPort", "BrainProjectContextPort",
    "PlanningSnapshotPort", "MockPlanningSnapshotPort", "build_inputs", "ASSUMPTIONS",
    # 版本与口径
    "DS_CONTRACT_VERSION", "BRAIN_CONTRACT_VERSION", "RULES_VERSION",
    "SCHEDULE_DISCLAIMER",
    # 子模块
    "backtest", "calendar", "graph", "ports", "resources", "scenarios", "schedule",
    "schema", "service",
]
