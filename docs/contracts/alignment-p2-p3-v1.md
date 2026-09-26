# 二期 / 三期契约对齐 · `p2p3-align-v1`

> 本文不是第三套契约。契约只有两份：
> **二期 `project-brain-v1`（公共契约的所有者）** 与 **三期 `decision-support-v1`（消费方）**。
> 本文规定的是**两者之间的接缝**：谁是字段权威、谁不得写谁、同名词怎么区分，
> 以及**对齐怎么被自动检查**（不是靠约定俗成）。

- 对齐层版本：`p2p3-align-v1`
- 二期契约：`project-brain-v1`（`application/project_brain/`，分支 `feat/p2-project-brain-v1`）
- 三期契约：`decision-support-v1`，规则版本 `ds-rules-1.0.0`（`application/decision_support/`，分支 `feat/p3-decision-support-v1`）
- 执行实现：`application/decision_support/alignment.py`
- 机器可读映射：`docs/contracts/examples/alignment-field-map.json`（**由 `alignment.FIELD_MAP` 生成**，测试断言两者一致）

## 0. 一句话分工

| | 二期 | 三期 |
|---|---|---|
| 管什么 | 「**谁说了什么、谁答应了什么**」——记忆、证据、行动闭环 | 「**这活该怎么排**」——工序、依赖、工期、日历、资源、方案、复盘 |
| 权威字段 | 八个统一字段、`data_as_of`、`source_freshness`、`conflicts`、`missing_info` | `contract_date` / `internal_plan_date` / `forecast_date`、`forecast_status`、`rules_version` |
| 写业务表 | 是（经第一期闸门） | **否。一行都不写** |
| 对接键 | 只有 `project_id` / `plan_id` / `snapshot_id`；**名称一律不作键** | 同左 |

## 1. 八个统一字段：谁产出、谁读、谁不许写

| 字段 | 二期 | 三期 | 规则 |
|---|---|---|---|
| `project_id` | 写 | 读+回填 | 两期必须同项目，否则 `project_id_mismatch`（error） |
| `plan_id` | 写 | 读+回填+校验 | 三期快照的每个 `plan_id` 应能在二期上下文里找到引用；找不到报 `plan_link_unverified` |
| `action_id` | 写 | **只引用** | 三期不新建行动 |
| `evidence_id` | 写 | **只引用** | 事实本体在二期，三期只留索引 |
| `decision_id` | 写 | **只引用，永不创作** | 见 §3.2 |
| `run_id` | 写 | **写自己的** | 三期每次运行生成 `RUN-YYYYMMDD-XXXX`，供二期按 run 追溯算法运行 |
| `snapshot_id` | 写 | 读+回填 | 两期必须同快照，否则 `snapshot_id_mismatch`（error） |
| `version` | 写（顶层+单条） | 读+回填 | 执行前用 `compare_versions()` 重查；不一致即 `version_stale` 拒绝 |

**三期的任何产出都必须带 `unified` 块**，由 `alignment.make_unified()` 生成——键集合恒等于 `UNIFIED_FIELDS`，不多不少（有断言保证）。

## 2. 字段映射

完整表见 `alignment-field-map.json`（16 条，含 `brain_path` / `ds_path` / `direction` / `required` / `note`）。
要点：

- `data_as_of` 的权威口径是「**最晚一条采集时间**」，**不是**「现在几点」；
- `source_*` 只用于溯源展示，**关联一律用 `source_row_id`**；
- `source_freshness[].stale=true` → 三期转成 `GAP_STALE_SNAPSHOT` 保留意见，**并降低结论确定性表述**；
- `conflicts[]` 未解决 → 三期把相关前置条件按 `unknown` 处理，**不放行、不替人裁决**；
- `missing_info[]` 结构固定为 `{code, message, ref, subject, as_of}`。

## 3. 五处语义分歧与裁决

同名不同义是两期并行开发最容易踩的坑。以下五条**由 `SEMANTIC_SPLITS` 定义并被测试逐条断言**。

### 3.1 「预测」

| | 二期 | 三期 |
|---|---|---|
| 载体 | `predictions[]`（`kind=prediction`） | `forecast_date` / `forecast_status` |
| 主体 | **人**（带 `actor` / `rationale` / `confidence` / `aspect_key`） | **算法**（带 `rules_version` / `method`） |
| 例子 | 「预计 A 批 2026-09-30 完成」（confidence 0.6） | 由工序 8h+8h、日历、共享测试台算出的日期 |

**裁决**：两者都保留，**复盘时按 `prediction_source` 分开统计**。
`alignment.human_prediction_records()` 把人说的预测转成与模型预测同构的记录
（`prediction_source="human"`，`source_ref` 指回二期 `memory_id`），
`merge_prediction_records()` 并列保存、**不按 plan_id 去重、不互相覆盖** ——
「人算的 vs 算法算的」正是要比较的东西。

**禁止**：合并成一个「准确率」。混合口径的数字看着漂亮，但没有指向任何东西。

### 3.2 「决策」

- 二期 `decisions[]` 是**人**的决策记录，`decision_id` 即其 `memory_id`，带 `rationale`；
- 三期方案比较只产出**候选**与**所需批准**（`required_approvals`），**不产出 `decision_id`**。

**裁决**：候选方案被人采纳后，由**二期经第一期闸门**写入决策库。三期永不写决策。
这条不是纪律要求，是可执行检查：

```python
alignment.check_no_authored_decision_id(payload, allowed=set(ctx_decision_ids))
# → {"ok": bool, "referenced": [...], "authored": [...]}
```

产出里出现的每一个 `decision_id` 都必须来自二期上下文，否则 `ok=False`。

### 3.3 「状态」

- 二期：`action.status ∈ {candidate, pending_confirm, ready, in_progress, blocked, pending_acceptance, closed, cancelled}`
- 三期：`step.status ∈ {planned, in_progress, done}`；`forecast_status ∈ {determined, conditional, undetermined}`

**裁决**：三套词表各管一段，**不得互相赋值**（例如把 `conditional` 写成 `pending_confirm`）。

### 3.4 `as_of` / `data_as_of`

- 二期 `data_as_of`：这批数据的**采集截止时间**；
- 三期 `as_of`：**计划快照时点**；`data_as_of` 回填二期值。

**裁决**：两个字段并列存放、**不互相覆盖**；差异超过 24 小时报 `as_of_mismatch`（warning）。
工程含义：计划快照比数据新，不算错（先有数据、后有排产）；但差太远就说明一边陈旧，
结论必须标注「基于过期数据」。

### 3.5 `version`

- 二期顶层 `version` = 所有记录版本之和（单调增，粗粒度护栏）；单条记录 `version` 是乐观锁依据；
- 三期只回填、只读比对。

**裁决**：三期产出必带 `context_version`。未来执行前：

```python
alignment.compare_versions(ctx_version, current_version)
# 一致 → {"ok": True, "code": "version_match"}
# 不一致 → {"ok": False, "code": "version_stale", ...}  ← 必须重跑，不用旧结论写库
```

## 4. 对齐检查项（`check_alignment` 的 `items[].code`）

| code | 级别 | 含义 | 处置 |
|---|---|---|---|
| `context_absent` | error | 没取到 ProjectContext | 传 `context_port` / `mock_context_path`，或确认二期可导入 |
| `contract_version_mismatch` | error | 契约版本不是 `project-brain-v1` | 确认二期分支是否已合并 |
| `project_id_mismatch` | error | 两期不是同一个项目 | 修数据源；跨项目取数会让结论张冠李戴 |
| `snapshot_id_mismatch` | error | 两期不是同一时刻的数据 | 统一 `snapshot_id`，否则结论不可复现 |
| `as_of_mismatch` | warning | 快照时点与数据截止差 > 24h | 结论标注「基于过期数据」 |
| `plan_id_invalid_format` | error | `plan_id` 不符合 `PLN-YYYYMMDD-XXXX` | 改 4 位尾码；否则二期 `validate_id` 拒绝，联表键形同虚设 |
| `plan_id_absent_upstream` | warning | 二期上下文里没有任何记录带 `plan_id` | 二期补齐记录字段 |
| `plan_link_unverified` | warning | 三期某个计划在二期查不到引用 | 可能新计划/二期未记录；**该关联未经证实** |
| `time_format_non_iso` | info | 存在空格写法时间（如 `2026-09-24 09:35:00`） | 新字段统一 ISO-T；读取端**必须容忍两种** |
| `unified_fields_incomplete` | warning | 某类记录缺统一字段 | 二期投影层补齐 |
| `plan_link_ok` / `align_ok` | info | 对齐通过 | — |

`ok=False` 只表示有 error；warning 不阻断算法，但**必须出现在结论里**。
所有 error 会同时并入 `build_inputs()["caveats"]`。

## 5. 本次对齐**实际发现并修掉**的五个缺陷

这一节是本文档最有价值的部分——全部是实跑发现的，不是评审出来的。

| # | 位置 | 问题 | 后果 | 修法 |
|---|---|---|---|---|
| 1 | 三期样例/测试 | `plan_id = PLN-20260928-A`，尾码仅 1 位 | 二期 `validate_id("plan", ...)` **拒绝**；合并后联表键直接失效 | 全量改为 `PLN-20260928-A001`（112 处），并用二期校验器逐一复核 |
| 2 | 二期 `context.py::_brief()` | 投影时漏掉 `plan_id` / `run_id` / `snapshot_id` / `version` | **记忆与生产计划在契约层断链**，下游只能拿名称去猜——而合同 §1.1 恰恰禁止名称做键 | 投影补齐八个统一字段 |
| 3 | 二期 `context.py::actions_out` / `blockers` | 行动行同样缺 `plan_id` / `run_id` / `snapshot_id` | 行动无法归属到计划 | 同上 |
| 4 | 二期 `service.py::add_evidence` | 构建器 `build_evidence_row` 支持 `plan_id`，但调用方**没往下传** | 所有证据行 `plan_id` 恒为空 | 补参数并透传；`scenario` 侧默认跟随所指向行动 |
| 5 | 二期 `context.py` | `data_as_of` 把 `action.updated_at`（系统写入时刻）算了进去 | `data_as_of ≈ generated_at ≈` 永远「现在」，与合同「**不是「现在几点」**」矛盾；三期拿它对齐快照会误判成「数据一样新」 | 改为只取 `captured_at`（采集时间），并显式排除 `occurred_at`（可能是**将来**的业务日期，会把「截至时间」顶到未来） |

修复后：二期 **220 项**测试全绿，三期 **78 + 44 项**全绿。

## 6. 开放项（需二期确认，本期不擅自决定）

1. **`plan_id` 缺失时的兜底**：若某项目完全没有生产计划（纯事务型项目），
   八字段里的 `plan_id` 应留空还是填 `""`？当前按合同「不适用时 `""`」处理。
2. **`data_as_of` 是否该纳入人工直接编辑行动的时间**：人工直接改的行动没有来源消息，
   其业务时间只有系统时间。当前**不计入** `data_as_of`（宁可保守，标明数据可能更新）。
3. **`snapshot_id` 的跨期一致性由谁保证**：当前由三期 `check_alignment` 报错，
   但**没有任何机制阻止**两期各写各的。建议二期在 `snapshot()` 时把
   `project_id + as_of` 一起落盘，供三期反查。

## 7. 怎么用

```python
from application.decision_support import (
    check_alignment, read_context, make_unified, new_run_id,
    human_prediction_records, merge_prediction_records,
    check_no_authored_decision_id, compare_versions,
)

# 1) 组装输入时顺带做对齐（真实模块优先，mock 兜底）
inputs = load_inputs("PRJ-20260928-0001", brain_root="auto",
                     mock_context_path="docs/contracts/examples/decision-support-context-mock.json",
                     mock_planning_path="docs/contracts/examples/decision-support-planning-sample.json")
print(inputs["alignment"]["summary"])      # 两期输入是否对得上
print(inputs["unified"])                   # 八个统一字段（含本次 run_id）

# 2) 直接用真实二期模块（合并后）
from application.project_brain import get_project_context
ctx = get_project_context("PRJ-20260928-0001", "SNP-20260928-0830")
rep = check_alignment(ctx, planning_snapshot)
```

CLI：

```bash
python decision_support.py align          # 打印对齐报告
python decision_support.py align --json   # 机器可读
```

## 8. 复现与验收

```bash
cd <worktree>/tests
python -m unittest discover -p "test_*.py"        # 三期全量：78 + 44
cd ../<p2 worktree>/tests
python -m unittest discover -p "test_*.py"        # 二期全量：220
```

跨期契约测试（`tests/test_p2_p3_alignment.py`）的运行前提：
能 `import application.project_brain`。合并后同树可达；分支部署时通过
`alignment.find_brain_root()` 自动定位兄弟 worktree；**两者都不可用时相关用例
显式 skip 并说明原因，不静默通过**。
