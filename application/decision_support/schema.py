# -*- coding: utf-8 -*-
"""application/decision_support/schema.py — 第三期「项目决策辅助」契约常量层。

本期只做**离线算法**：交付依赖（工作日历 / 前置关系 / 工期 / 拓扑 / 关键路径 /
延期传播）、资源与方案比较、预测复盘。

三条不可越界的线（写进常量，供测试直接断言）：
  1. 合同日期 ≠ 内部计划日期 ≠ 预测日期。三者分字段存，永不互相覆盖。
  2. 未知工期、未确认前置条件（如未收款）**不得**变成伪确定的交期 —— 只能是 None
     + 一个明确的原因码。
  3. 本模块产出的任何东西都是**建议**，不是执行授权。方案选择不构成授权；
     未来执行统一走第一期 `application.authorization` 闸门。

公共字段（project_id / plan_id / action_id / evidence_id / decision_id / run_id /
snapshot_id / version）由第二期 contract `project-brain-v1` 定义，本期只**读取与回填**，
不重新定义、不另建映射。
"""
from __future__ import annotations

# ────────────────────────── 版本 ──────────────────────────
SKILL_VERSION = "1.9.0"
# 本期对外契约版本
DS_CONTRACT_VERSION = "decision-support-v1"
# 规则版本：预测复盘必须记录它，否则两次预测不可比
RULES_VERSION = "ds-rules-1.0.0"
# 公共契约（第二期负责定义，本期对齐）
BRAIN_CONTRACT_VERSION = "project-brain-v1"

# 八个统一字段（只读对齐，本期不改名）
UNIFIED_FIELDS = (
    "project_id", "plan_id", "action_id", "evidence_id",
    "decision_id", "run_id", "snapshot_id", "version",
)

# 来源溯源字段（名称不作主键）
SOURCE_FIELDS = (
    "source_system", "source_base", "source_table",
    "source_row_id", "source_message_id",
)

# ────────────────────────── 三种日期口径 ──────────────────────────
# 混用是交期失控的头号原因：对外承诺的日期被内部计划日期悄悄顶替，
# 或者算法的预测被当成承诺播报出去。所以三个字段名分开，禁止互相赋值。
DATE_CONTRACT = "contract"   # 合同日期：写进合同的对外承诺，改动需双方确认
DATE_INTERNAL = "internal"   # 内部计划日期：我方排产计划，可内部调整
DATE_FORECAST = "forecast"   # 预测日期：算法产出，必须带假设与规则版本

DATE_KINDS = (DATE_CONTRACT, DATE_INTERNAL, DATE_FORECAST)
DATE_KIND_CN = {
    DATE_CONTRACT: "合同日期",
    DATE_INTERNAL: "内部计划日期",
    DATE_FORECAST: "预测日期",
}

# ────────────────────────── 工期可信度 ──────────────────────────
DUR_SOURCE_INPUT = "input"          # 来自计划表/工序表的实测工期
DUR_SOURCE_HISTORY = "history"      # 由历史中位工期推出的估计
DUR_SOURCE_ASSUMED = "assumed"      # 情景假设（如「假设加急可压缩到 4h」）
DUR_SOURCE_UNKNOWN = "unknown"      # 未知 —— 不可用于产出确定日期

# ────────────────────────── 前置条件（含商业条件）──────────────────────────
# 「未收款」不是工期，是一条**商业前置条件**。它没有满足日期，
# 所以下游只能停在「不可确定」，不能算出一个好看的日期来。
COND_MATERIAL = "material"           # 物料到货
COND_PAYMENT = "payment"             # 收款（含预付款/尾款）
COND_DRAWING = "drawing"             # 图纸/技术资料
COND_CUSTOMER = "customer_confirm"   # 客户确认（样机签样、色卡…）
COND_CAPACITY = "capacity"           # 产能/排产窗口
COND_OTHER = "other"

COND_KIND_CN = {
    COND_MATERIAL: "物料到货",
    COND_PAYMENT: "收款条件",
    COND_DRAWING: "技术资料",
    COND_CUSTOMER: "客户确认",
    COND_CAPACITY: "产能窗口",
    COND_OTHER: "其他条件",
}

COND_CONFIRMED = "confirmed"   # 已核实满足（有证据）
COND_ASSUMED = "assumed"       # 情景假设：「假设它周二到」—— 算得出来，但结论必须带条件
COND_ANNOUNCED = "announced"   # 消息里说了时间，但未核实 —— 只够做有条件推演
COND_UNKNOWN = "unknown"       # 未知（含未收款且无到账日期）

COND_STATUS_CN = {
    COND_CONFIRMED: "已确认",
    COND_ASSUMED: "情景假设",
    COND_ANNOUNCED: "未核实（消息所说）",
    COND_UNKNOWN: "未知",
}

# 只有 confirmed 才允许让下游产出**确定**的预测日期
COND_TRUSTED = (COND_CONFIRMED,)
# 情景假设与未核实消息：可以用来推演（标 conditional），但**永远不能**变成合同日期
COND_CONDITIONAL = (COND_ASSUMED, COND_ANNOUNCED)

# ────────────────────────── 工序执行状态（资源保护用）──────────────────────────
STEP_NOT_STARTED = "not_started"   # 未开始 —— 可重排
STEP_IN_PROGRESS = "in_progress"   # 进行中 —— 冻结，不可移动
STEP_DONE = "done"                 # 已完成 —— 冻结，且消耗历史资源
STEP_CANCELLED = "cancelled"

STEP_STATUS_CN = {
    STEP_NOT_STARTED: "未开始",
    STEP_IN_PROGRESS: "进行中（冻结）",
    STEP_DONE: "已完成（冻结）",
    STEP_CANCELLED: "已取消",
}
# 冻结 = 保护：已开始/已完成的工序不许被重排（改历史会让人无法判断现状）
FROZEN_STATUSES = (STEP_IN_PROGRESS, STEP_DONE)

# ────────────────────────── 资源类型 ──────────────────────────
# 与 adapters/schema.py 的 RESOURCE_TYPES 对齐（那边是 SeaTable 表的取值域），
# 这里只做常量引用，不重复定义表的列结构。
RESOURCE_TYPES = ("人员", "设备", "外协")
RESOURCE_TYPE_DEFAULT = "人员"

# ────────────────────────── 结论原因码 ──────────────────────────
# 「不可确定」不是失败，是一种诚实：说清缺什么，比编一个日期强。
GAP_UNKNOWN_DURATION = "unknown_duration"
GAP_UNCONFIRMED_PRECONDITION = "unconfirmed_precondition"
GAP_MISSING_PREDECESSOR = "missing_predecessor"
GAP_CYCLE = "cycle_detected"
GAP_SELF_LOOP = "self_loop"
GAP_DUP_EDGE = "duplicate_dependency"
GAP_NO_RESOURCE = "resource_not_found"
GAP_RESOURCE_UNKNOWN_CAP = "resource_capacity_unknown"
GAP_FROZEN_MOVED = "frozen_step_would_move"
GAP_NO_CALENDAR = "no_calendar_defined"
GAP_NO_BASELINE = "insufficient_baseline_sample"
GAP_NO_ACTUAL = "actual_result_missing"
GAP_STALE_SNAPSHOT = "stale_source_snapshot"

GAP_CN = {
    GAP_UNKNOWN_DURATION: "工期未知，无法推算日期",
    GAP_UNCONFIRMED_PRECONDITION: "前置条件未确认（如未收款/物料未核实到货），不得当作确定日期",
    GAP_MISSING_PREDECESSOR: "引用了不存在的前置工序",
    GAP_CYCLE: "前置关系存在循环依赖",
    GAP_SELF_LOOP: "工序把自己设为前置",
    GAP_DUP_EDGE: "同一对工序重复声明了前置关系",
    GAP_NO_RESOURCE: "工序引用了不存在的资源",
    GAP_RESOURCE_UNKNOWN_CAP: "资源日产能未知，无法判断是否超载",
    GAP_FROZEN_MOVED: "重排会移动已开始/已完成的工序，已阻止",
    GAP_NO_CALENDAR: "未定义工作日历",
    GAP_NO_BASELINE: "历史样本不足，不给准确率结论",
    GAP_NO_ACTUAL: "尚无实际结果，无法复盘",
    GAP_STALE_SNAPSHOT: "输入快照过旧",
}

# ────────────────────────── 方案比较口径 ──────────────────────────
# 资源约束下的最优排程是 RCPSP（NP-hard）。本期用的是**可解释的启发式规则**：
#   「就绪工序中最早可开始者先排，资源被占则顺延到下一个空档」。
# 所以结论里必须带 SCHEDULE_DISCLAIMER，杜绝把普通关键路径叫成「资源约束最优排程」。
SCHEDULE_METHOD = "heuristic_earliest_start_first"
SCHEDULE_METHOD_CN = "就绪优先启发式（最早可开始优先，资源冲突顺延）"
SCHEDULE_DISCLAIMER = (
    "本排程为可解释启发式规则结果，非资源约束下的最优排程（RCPSP 属 NP-hard，"
    "本期不做最优求解）。关键路径按「仅前置关系」口径计算，不包含资源约束。"
)

# 方案对比必须交代的六件事
SCENARIO_FIELDS = (
    "completion",        # 完成时间
    "delta_cost",        # 增量成本
    "affected_projects", # 影响项目
    "open_conditions",   # 待核实条件
    "required_approvals",# 所需批准
    "basis",             # 依据（可追溯到快照/规则/假设）
)

# 方案选择的定性
PLAN_BASELINE = "baseline"       # 基准（当前条件，不做事）
PLAN_CANDIDATE = "candidate"     # 候选方案

# ────────────────────────── 复盘口径 ──────────────────────────
# 与 foresee 保持一致：复核不使用未来信息。
# 「预测时点」之后才产生的数据，一律不得进入该次回测的输入。
BACKTEST_NO_LOOKAHEAD = (
    "回测输入严格截断在预测时点（as_of）——预测时点之后采集的数据一律不可见。"
)
ACCURACY_UNDEFINED = "insufficient_sample"   # 样本不足时给这个，而不是给一个数字

# ────────────────────────── 预测来源（与二期对齐）──────────────────────────
# 二期 `predictions[]` 是**人**说的预测；本期 `forecast_date` 是**算法**算的。
# 两者都叫「预测」，但混在一起统计出来的准确率没有意义 —— 分开存、分开算。
PREDICTION_SOURCE_MODEL = "model"    # 算法产出（本期 forecast_date）
PREDICTION_SOURCE_HUMAN = "human"    # 人的判断（二期 predictions[]，带 actor/confidence）
PREDICTION_SOURCES = (PREDICTION_SOURCE_MODEL, PREDICTION_SOURCE_HUMAN)

# ────────────────────────── 授权口径 ──────────────────────────
GATE_MODULE = "application.authorization"
AUTH_NOTE = (
    "方案选择不等于执行授权。本模块只产出建议，任何落库/下单/对外承诺"
    "都必须携带 approve 的 WriteGrant 走 application.authorization 闸门。"
)

# ────────────────────────── 单位 ──────────────────────────
HOURS_PER_DAY = 8.0
UNIT_HOUR = "小时"
UNIT_DAY = "工作日"
UNIT_CNY = "元"
