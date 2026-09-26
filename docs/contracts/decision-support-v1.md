# 项目决策辅助契约 v1（decision-support-v1）

> 第三期交付物。本期只做**离线算法**：交付依赖、资源与方案比较、预测复盘。
> 不接真实写入、不自动下单、不对外承诺交期、不修改定时任务或发布、不做新 UI。
>
> 契约版本 `decision-support-v1`　规则版本 `ds-rules-1.0.0`
> 公共字段契约由第二期 `project-brain-v1` 定义，本期**只读对齐**，不重新定义。

---

## 0. 一句话定位

第二期回答「**谁说了什么、谁答应了什么**」（事实/承诺/行动/决策）；
第三期回答「**这些事排下来会怎样、换个条件能怎样、我们以前判得准不准**」。

两者靠 `project_id` / `plan_id` / `snapshot_id` 对接，各管一段，互不侵入。

| | 第二期 project-brain-v1 | 第三期 decision-support-v1 |
|---|---|---|
| 管什么 | 记忆五类、行动闭环、证据、上下文查询 | 工序依赖、工期、日历、资源、方案比较、预测复盘 |
| 数据形态 | 人说过的话、承诺、决策 | 计划域结构（工序/前置/工期/日历/资源） |
| 产出 | `get_project_context()` | `analyze()` / `compare()` / `delay()` / `explain()` / `backtest()` |
| 写的边界 | 本地记忆库（需授权） | **一个字节都不写**（纯计算） |

---

## 1. 与公共契约的接口

### 1.1 统一字段（八个，只读对齐）

任何一期的记录都携带：`project_id` `plan_id` `action_id` `evidence_id`
`decision_id` `run_id` `snapshot_id` `version`。

第三期的输入输出**原样透传**这些字段，不新增别名、不做自动兼容。
来源侧保留 `source_system` `source_base` `source_table` `source_row_id`
`source_message_id` —— **名称一律不作关联键**。

> **实测证实**：这三个字段曾经在二期投影层被漏掉（`_brief()` 丢 `plan_id`、
> `actions_out` 丢 `plan_id`/`run_id`/`snapshot_id`、`add_evidence` 不传 `plan_id`），
> 结果是「记忆/行动/证据 → 生产计划」在契约层断链。已在二期补齐，
> 并由 `alignment.check_alignment()` 每次实跑校验 —— **不靠读文档相信**。

### 1.1.1 跨期对齐层（`application/decision_support/alignment.py`）

只读对齐、字段权威对账、语义分歧裁决，全部落在这一处：

| 能力 | 接口 |
|---|---|
| 回填八个统一字段 | `make_unified(...)` → 键集合恒等于 `UNIFIED_FIELDS` |
| 生成运行编号 | `new_run_id()` → `RUN-YYYYMMDD-XXXX`（与二期同格式） |
| 两期输入一致性校验 | `check_alignment(ctx, planning)` → `{ok, items[], summary}` |
| 决策守卫 | `check_no_authored_decision_id(payload, allowed)` |
| 版本重查 | `compare_versions(ctx_version, current_version)` |
| 时间容差解析 | `parse_time()` / `normalize_time()`（空格与 ISO-T 两种都吃） |
| 人/模型预测分家 | `human_prediction_records()` / `split_prediction_records()` / `merge_prediction_records()` |
| 二期模块定位 | `find_brain_root()` / `import_brain()` / `load_context()` |

对齐检查项与语义分歧裁决详见 **`docs/contracts/alignment-p2-p3-v1.md`**；
机器可读字段映射见 `examples/alignment-field-map.json`。

### 1.2 输入：ProjectContext

```python
from application.decision_support import load_inputs, from_inputs

inputs = load_inputs(
    "PRJ-20260928-0001",
    brain_root="<二期 worktree 路径>",                     # 优先用真实模块
    mock_context_path="docs/contracts/examples/decision-support-context-mock.json",
    mock_planning_path="docs/contracts/examples/decision-support-planning-sample.json",
)
ds = from_inputs(inputs)
```

二期模块可用时走 `application.project_brain.get_project_context`；
不可用时自动回退到合成 mock，**读法不变**（`application/decision_support/ports.py`）。

### 1.3 计划域输入：PlanningSnapshot

工序、前置关系、工期、日历、资源**不在二期契约范围内**（二期管记忆，不管排产）。
本期以 `PlanningSnapshot` 提供，字段名与 `adapters/schema.py` 的
「生产工序 / 资源 / 资源分配」对齐：

| 结构 | 关键字段 |
|---|---|
| `steps[]` | `step_id` `plan_id` `name` `process` `duration_hours` `duration_source` `status` `predecessors[]` `preconditions[]` `resources[]` |
| `plans[]` | `plan_id` `contract_date` `internal_plan_date` |
| `resources[]` | `resource_id` `name` `type` `daily_capacity_hours` `in_service` |
| `resource_groups[]` | `group_id` `name` `members[]`（资源池：工序引用池 = 池内任一成员可用即可） |
| `calendar` | `workdays` `start_hour` `end_hour` `holidays[]` `extra_workdays[]` |

接真实表时**只换数据源，不改算法**；所需的字段映射只应写在适配层。

### 1.4 所有跨期假设（集中在适配层，逐条可查）

| id | 假设 | 风险 | 现状 |
|---|---|---|---|
| A1 | 八个统一字段由二期定义，本期只读对齐 | 二期改字段名则本期需同步 | **已实测**：二期投影层曾漏 `plan_id`/`run_id`/`snapshot_id`/`version`，已补齐 |
| A2 | 工序/依赖/工期/日历/资源属计划域，二期未覆盖，本期以 PlanningSnapshot 提供 | 接真实表需要字段映射，映射只改适配层 | 待真实表接入 |
| A3 | `observations`（未核实观察）**不**作为排程的确定输入 | 当事实用会产出伪确定交期 | 已实现（`conditions_from_context`） |
| A4 | `source_freshness` 中 `stale=true` 的来源转成「结论待复核」提示 | 忽略时效会让旧数据看起来像新结论 | 依赖 `data_as_of` 口径；二期原把它算成「现在」，已修正为「最晚采集时间」 |
| A5 | `steps[*].plan_id` 与二期行动/记忆的 `plan_id` 同源可比；名称不作关联键 | 用名称关联会在改名时静默断链 | **已实测**：plan_id 必须过二期 `validate_id`；找不到引用时报 `plan_link_unverified` |
| A6 | 本期不写业务表、不建审批器；执行一律走第一期闸门 | 无 | **已实测**：`check_no_authored_decision_id` 断言产出里无自造决策 |
| A7 | 二期 `predictions[]` 是**人**的预测；本期 `forecast_date` 是**算法**的 | 合并统计会得到没有意义的「准确率」 | 已实现（`prediction_source` 分家） |

---

## 2. 交付依赖

### 2.1 三种日期，永不互相覆盖

| 字段 | 含义 | 谁产出 |
|---|---|---|
| `contract_date` | 合同日期，对外承诺，改动需双方确认 | **只读透传**，本期不改 |
| `internal_plan_date` | 内部计划日期，可内部调整 | **只读透传**，本期不改 |
| `forecast_date` | 预测日期，算法产出 | 本期 |

常见坏味道是「预测晚了就把计划日期改掉，让它看起来没晚」。
本模块**没有任何写入 contract/internal 的代码路径**，这在结构上就不可能发生。

### 2.2 工期可信度

| `duration_source` | 含义 |
|---|---|
| `input` | 来自计划表/工序表的实测工期 |
| `history` | 由历史中位工期推出的估计 |
| `assumed` | 情景假设（如「假设加急可压缩到 4h」） |
| `unknown` | **未知 —— 不可用于产出确定日期** |

`duration_hours: null` 会让该工序及其**全部下游**的 `forecast_date` 为 `null`，
并给出原因码 `unknown_duration`。绝不拿一个好看的数字填空。

### 2.3 前置条件四态（含商业条件）

| `status` | 处理 |
|---|---|
| `confirmed` | 硬约束，可产出**确定**日期 |
| `assumed` | 硬约束，但**仅在该情景内成立**；结论标 `conditional` |
| `announced` | 参与排程，但结论标 `conditional`，并明说「依赖未核实信息」 |
| `unknown` / 无时间 | **不可确定**，不给日期 |

「未收款」是 `kind: payment` 的条件，不是工期。它没有满足日期时，
下游停在「不可确定」，**不会**变成一个看着很确定的交期。

> 为什么 `announced` 也参与排程而不是直接忽略：忽略它等于假设「物料周一就到」，
> 那是比标注风险更离谱的假设。用它 + 明确标注，才是诚实的做法。

### 2.4 两道口径，分开命名

| 口径 | 函数 | 说明 |
|---|---|---|
| 关键路径 | `build_cpm()` | **仅前置关系**，不含资源约束。给 ES/EF/LS/LF/松弛/关键路径 |
| 资源可行排程 | `resource_feasible_schedule()` | 在前置关系之上叠加产能约束，**启发式**，非最优 |

⚠️ **禁止把普通关键路径称作「资源约束最优排程」。**
资源约束下的最优排程是 RCPSP（NP-hard），本期不做最优求解。
每次输出都带 `disclaimer`。

### 2.5 延期传播

```python
ds.delay_impact("STP-A-ASM", 8)     # 延 8 小时，会连累谁
```

做法是「对同一份快照跑两遍取差集」，输出三样东西：

- `affected_steps[]`：被推后的工序及天数；松弛足够的下游标 `absorbed_by_slack`；
- `affected_plans[]`：**仅前置口径**的计划完成日变化；
- `resource_level_impact[]`：叠加资源约束后的**二次连锁**（CPM 看不到产能挤占）。

两者分开列，不可混为一谈。

---

## 3. 资源与方案比较

### 3.1 资源

- 三类：`人员` / `设备` / `外协`（与 `adapters/schema.py` 的 `RESOURCE_TYPES` 一致）；
- 资源池 `resource_groups`：工序引用池 → 池内任一成员可用即可（OR 语义），
  实际选中哪台记在 `resolved_resources`；
- **冲突检测**：日粒度超载（`overloads`）+ 时段真重叠（`time_overlaps`）；
- **保护已开始/已完成工序**：`status ∈ {in_progress, done}` 即冻结，
  时间锁死、资源先扣、别人绕开；任何试图移动它的重排记为 `GAP_FROZEN_MOVED`。
  理由很简单：昨天已经贴了一天的板子，算法不能把它「重排」到后天 ——
  那排出来的计划再漂亮也是废纸。

### 3.2 方案比较：六件事必须交代

| 字段 | 说明 |
|---|---|
| `completion` | 完成时间（含相对基准的变化天数） |
| `delta_cost` | 增量成本，含 `is_synthetic` 标记与逐项 `basis` |
| `affected_projects` | 影响项目（相对基准有日期变化的计划） |
| `open_conditions` | 待核实条件 |
| `required_approvals` | 所需批准（每条都注明「批准 ≠ 执行授权」） |
| `basis` | 依据：快照 id、日历、规则版本、方法、假设 |

**费用不编造**：估不出来就记「待核实条件」并说明缺什么，不填 0 假装免费。
新增资源若一次都没排上，会明说「**未被用到**（瓶颈不在这里），采购它不会改善交期」。

### 3.3 模拟零业务写入（可断言，不靠自觉）

```python
from application.decision_support import SimulationLedger, SimulationWriter

writer = SimulationWriter()                 # 一被调用就抛 PermissionError
ledger = SimulationLedger(writer)
ds = DecisionSupport(snapshot, ledger=ledger)
res = ds.compare()

assert writer.calls == []                   # 写入端一次都没被调用
assert res["simulation_ledger"]["executed_total"] == 0
assert all(not i["authorized"] for i in ledger.intents)   # 所有执行意图都未授权
```

`SimulationLedger.request_execution()` 会真的去问第一期闸门
`application.authorization.authorize_write(None, table, action)`，
本期没有任何授权来源，所以它必然带着拒绝理由回来 —— 这不是缺陷，是要证明的事。

### 3.4 固定快照

模拟只在**内存副本**上跑（`copy.deepcopy`），传入的快照对象跑完不变（有测试断言）。
不修改真实业务数据。

### 3.5 首个验收样例

合成项目 A、B 两批：各需组装 8h、测试 8h；组装资源独立，共享一台测试设备。
日历：周一至周五 09:00—17:00，无额外节假日。

| 场景 | A 批完成 | B 批完成 |
|---|---|---|
| 材料均周一（09-28）可用 | 周二 09-29 | 周三 09-30 |
| A 材料改为周三（09-30）可用 | 周四 10-01 | **周二 09-29（提前）** |

方案比较（基准 = A 材料周三到）：

| 方案 | 内容 | A 批 | 结论 |
|---|---|---|---|
| `S0` | 保持条件 | 周四 10-01 | 基准 |
| `S1` | 假设 A 材料提前到周二 | 周三 09-30 | 提前 1 天，**依赖未核实假设** |
| `S2` | 仅加一台测试设备，材料仍周三到 | 周四 10-01 | **不改善 A 交期**（新增设备未被用到） |

费用与加急可行性均为**合成假设**，不是真实报价。

---

## 4. 预测复盘

### 4.1 每条预测记录必须带

`predicted_at`（预测时点）、`input_snapshot_id`（输入快照）、`rules_version`（规则版本）、
`assumptions`（假设）、`sample_scope`（样本范围）、`actual`（实际结果）。

**没有预测时点就没有复盘可言** —— 事后补的时点等于事后编的准确率。

### 4.2 不用未来信息（可断言的性质）

> 回测输入严格截断在预测时点（`as_of`）——预测时点之后采集的数据一律不可见。

三条实现约束：

1. `filter_as_of(records, as_of)` 是所有回测的唯一入口；
   **缺时间字段的记录直接丢弃**（无法证明它在预测之前存在，就不能用它）；
2. `audit_no_lookahead(record, samples)` 对每条记录审计样本采集时间是否晚于预测时点；
3. 可测的等价性质：往样本里塞一条 `as_of` 之后采集的极端值，
   `history_median_duration()` 的结果**必须不变**。变了就是泄漏。

### 4.3 对照基线

- **历史中位工期基线**：不做任何推理的笨办法。排程若跑不赢它，就没有价值；
- **与既有 `foresee.py` 口径对齐**：复用 `VERDICT_REVIEW`
  （`预警正确 / 误报 / 漏报 / 正确`）。取不到时降级为等价副本并在
  `verdict_source` 里标明，**不假装是同一把尺子**。

### 4.4 样本不足就不给数字

可复盘样本 < `MIN_SAMPLE`(5) 时返回 `accuracy_status = "insufficient_sample"`，
**不给 MAE / 偏差 / 命中率**。给小样本一个百分比比不给更糟：
它看起来像结论，其实是噪声。

---

## 5. 接口清单

```python
from application.decision_support import DecisionSupport

ds = DecisionSupport(planning_snapshot, calendar=None, context=None)

r1 = ds.analyze()                       # 三日期 + 关键路径 + 资源可行排程 + 缺口 + 保留意见
r2 = ds.compare(scenarios=None, cost_model=None)   # S0/S1/S2 方案比较
r3 = ds.delay_impact("STP-A-ASM", 8)    # 延期向下游传播
r4 = ds.conflicts()                     # 对当前排程做资源冲突检测
r5 = ds.explain(step_id="STP-A-TEST")   # 这条日期凭什么这么算（推理链）
```

模块级便捷函数：`analyze()` / `compare()` / `run_backtest()` / `load_inputs()` / `from_inputs()`。

CLI：

```bash
python decision_support.py analyze|compare|delay|explain|backtest|align|demo [--json]
python decision_support.py align --brain-root auto        # 两期契约对齐检查
```

### 5.1 `analyze()` 返回结构（要点）

```
contract_version / rules_version / project_id / snapshot_id / as_of / data_as_of
unified{ project_id, plan_id, action_id, evidence_id, decision_id,
         run_id, snapshot_id, version }        # 八个统一字段（本次运行的回填）
alignment{ ok, items[{code,severity,message,ref,how_to_fix}],
           errors[], warnings[], summary{...}, context_version }
calendar{...}
dates{ <plan_id>: {contract_date, internal_plan_date,
                   resource_feasible_date, resource_feasible_status,
                   delay_vs_contract_days, undetermined_reasons[]} }
cpm{ critical_path[], project_finish, steps[], issues[], method, disclaimer }
resource_feasible{ plans[], steps[], resources[], conflicts_resolved[],
                   protected_steps[], unresolved[], issues[] }
gaps[]                      # 缺口 / 不可确定的原因码
caveats[]                   # 二期来源时效 + 对齐 error（error 会并入这里）
assumptions[]               # A1..A7
evidence{ evidence_ids[], decision_ids[], action_ids[],
          unresolved_conflicts[], missing_info[] }
human_predictions[]         # 二期里**人**的预测（prediction_source=human）
execution_gate{ gate_module, rule, pre_execution, ds_writes_business_tables }
authorization_note
```

`alignment.ok=false` 只表示有 error 级问题；warning 不阻断算法，但**必须出现在结论里**。
所有 error 同时并入 `caveats`，保证「前提错了」不会被漂亮的数字盖过去。

### 5.2 原因码

| code | 含义 |
|---|---|
| `unknown_duration` | 工期未知，无法推算日期 |
| `unconfirmed_precondition` | 前置条件未确认（如未收款/物料未核实到货） |
| `missing_predecessor` | 引用了不存在的前置工序 |
| `cycle_detected` / `self_loop` | 循环依赖 / 自环 |
| `duplicate_dependency` | 重复声明前置关系（warning，不阻断） |
| `resource_not_found` | 工序引用了不存在的资源 |
| `resource_capacity_unknown` | 资源日产能未知，无法判断超载 |
| `frozen_step_would_move` | 重排会移动已开始/已完成的工序，已阻止 |
| `no_calendar_defined` | 未定义工作日历 |
| `insufficient_baseline_sample` | 历史样本不足，不给准确率 |
| `stale_source_snapshot` | 输入来源过旧 |

---

## 6. 使用样例

```bash
# 1) 交付依赖：看三种日期与关键路径（内置合成样例）
python decision_support.py analyze

# 2) 方案比较 S0/S1/S2：含增量成本、待核实条件、所需批准
python decision_support.py compare

# 3) 延期传播：A 批组装延 8 小时，谁被连累
python decision_support.py delay --step STP-A-ASM --hours 8

# 4) 解释单条日期
python decision_support.py explain --step STP-A-TEST

# 5) 预测复盘（含防未来信息审计）
python decision_support.py backtest

# 6) 一条命令跑通全链路
python decision_support.py demo
```

样例文件：

| 文件 | 用途 |
|---|---|
| `examples/decision-support-planning-sample.json` | 计划域快照（A/B/C 三批；C 批演示「不可确定」） |
| `examples/decision-support-context-mock.json` | 二期 ProjectContext 合成 mock |
| `examples/decision-support-predictions-sample.json` | 预测记录 + 历史样本（可做防未来信息实验） |

---

## 7. 本轮边界（明确不做）

- ❌ 不接真实写入、不自动下单、不对外承诺交期；
- ❌ 不修改定时任务、不发布页面；
- ❌ 不做新 UI；不做预测算法（`prediction` 只记录不推演）；
- ❌ 不做 RCPSP 最优求解（只有可解释启发式）；
- ❌ 不新建项目 ID 映射、事实库、审批器 —— 授权只有一个出口：第一期闸门。

**方案选择不等于执行授权。** 未来执行统一走第一期闸门，执行前重查版本。
`version` 与 `snapshot_id` 是这份契约的版本锚点：结论永远对得上某个快照，
不会出现「结论是昨天的、数据是今天的」这种说不清的状态。

---

## 8. 关键词表（中英对照）

| 中文 | 代码字段 |
|---|---|
| 合同日期 / 内部计划日期 / 预测日期 | `contract_date` / `internal_plan_date` / `forecast_date` |
| 关键路径（仅前置口径） | `cpm.critical_path` |
| 资源可行排程（启发式） | `resource_feasible` |
| 已核实 / 情景假设 / 未核实 / 未知 | `confirmed` / `assumed` / `announced` / `unknown` |
| 冻结（已开始/已完成） | `frozen` |
| 松弛 | `slack_hours` |
| 被资源顺延 | `shifted_by_resource` / `shifted_from` |
| 增量成本 | `delta_cost` |
| 待核实条件 | `open_conditions` |
| 所需批准 | `required_approvals` |
| 预测时点 | `predicted_at` |
| 防未来信息 | `no_lookahead` |
| 样本不足 | `insufficient_sample` |
| 人的预测 / 算法的预测 | `prediction_source=human` / `prediction_source=model` |
| 八个统一字段 | `UNIFIED_FIELDS` / `unified` |
| 跨期对齐 | `check_alignment` / `align_version` |
