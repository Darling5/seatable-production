# 单项目第二大脑契约 v1（project-brain-v1）

> 第二期交付物。本文件是**第二期与第三期之间的共同契约**，也是此后任何
> 「项目记忆 / 行动闭环 / 项目查询」实现的唯一字段口径来源。
>
> - 契约版本：`project-brain-v1`（代码常量 `application/project_brain/schema.py::BRAIN_CONTRACT_VERSION`）
> - 合成样例：`docs/contracts/examples/project-brain-sample.json`（可回放的项目场景）
> - 合成输出：`docs/contracts/examples/project-brain-context-sample.json`（`get_project_context` 的真实产出）
> - 契约测试：`tests/test_project_brain.py`（62 项）
> - 日期：2026-09-26

---

## 0. 一句话定位

**给「一个项目」建一个可查账的大脑**：把散在微信、邮件、业务表里的
观察、事实、承诺、决策、预测收进同一处，把「答应做的事」变成有负责人、
有期限、有验收标准的行动，并且**任何一条结论都能回指它的证据和数据时点**。

三条不可让步的原则（贯穿全文）：

1. **不覆盖历史。** 所有记录 append-only，纠正靠追加新记录 + supersede 指针。
2. **不混淆可信度。** 观察 ≠ 已核实事实；已发货 ≠ 已到货；部分到货 ≠ 齐套。
3. **不替人做决定。** 冲突并列展示、关闭必须验收、写入必须授权。

---

## 1. 统一字段（八个）

任何一期、任何模块产出的**记忆 / 证据 / 行动 / 决策**记录，都必须携带这八个字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `project_id` | str | 项目主键，`PRJ-YYYYMMDD-XXXX` |
| `plan_id` | str | 生产计划主键，`PLN-YYYYMMDD-XXXX`；不适用时 `""` |
| `action_id` | str | 行动主键，`ACT-YYYYMMDD-XXXX`；不适用时 `""` |
| `evidence_id` | str | 证据主键，`EVD-YYYYMMDD-XXXX` |
| `decision_id` | str | 决策主键，`DEC-YYYYMMDD-XXXX`（决策类记忆的 `memory_id` 即此值） |
| `run_id` | str | 产生这条记录的运行编号，与第一期 `contracts.new_run_id()` 同源 |
| `snapshot_id` | str | 生成该记录时所处的快照编号，`SNP-YYYYMMDD-XXXX`；无则 `""` |
| `version` | int | 该条记录的版本号，从 1 开始，每次写入 +1（乐观锁依据） |

ID 生成与校验：`application/project_brain/ids.py`（`new_id` / `validate_id`）。
格式与第一期 `application/contracts.py::new_object_id` 一致，第一期原有前缀全部保留。

### 1.1 来源：不用名称做主键

除了八个统一字段，每条记录还必须能回到它的来源那一行：

| 字段 | 说明 |
|---|---|
| `source_system` | `wechat` / `email` / `seatable` / `partdb` / `mpp` / `manual` / `doc` |
| `source_base` | `production` / `tasks` / `crm` / `local`（第一期三套 Base 的哪一套） |
| `source_table` | 表名。**仅用于展示与定位，不得作为关联键** |
| `source_row_id` | 来源行 ID —— 这才是关联键 |
| `source_message_id` | 来源消息唯一键，采集去重的第一依据 |

> 为什么死守这条：名称会改（「客户B」会被写成「客户B」「ZZYF」），
> ID 不会。用名称做键，改名那天全库关联一起断，而且**断得悄无声息**。

### 1.2 两个时间必须分开记

| 字段 | 含义 |
|---|---|
| `occurred_at` | 事情**发生**的时间（消息里说的时间） |
| `captured_at` | 系统**采集**到它的时间 |

相对日期（「下周一」「3 天后」）一律以 **`occurred_at` / 消息时间** 为基准解析，
**严禁**以 `captured_at` 或 `now()` 为基准（实现：`timeparse.resolve_date(text, base_dt)`）。
否则一条 8 月 3 日发出的「明天到货」会被解析成采集当天，期限整体漂移，
而且漂移量随采集延迟变化 —— 这种错在账面上完全看不出来。

解析不出来一律返回 `None`，**绝不猜测**。

---

## 2. 记忆的五种类型

五类**各自独立存储、独立查询**，不允许合并成一个 `notes` 字段。

| `kind` | 中文 | 含义 | 能否当事实用 |
|---|---|---|---|
| `observation` | 观察 | 未经核实的感知，可能是错的 | **否** |
| `fact` | 已核实事实 | 有 `evidence_id` 指向证据，且记录了核实人 | 是 |
| `commitment` | 承诺 | 谁答应在何时做什么（`actor` + `due_date`） | 否 |
| `decision` | 决策 | 结论 + `rationale`（为什么这么定） | 否 |
| `prediction` | 预测 | 对未来的判断，带 `confidence` 与 `rationale` | 否 |

记忆生命周期 `status`：`active` → `confirmed`（观察已升格为事实）/ `superseded`（已被纠正）。
状态变化只追加新版本，旧版本与原始文本永远留在事件流里。

### 2.1 去重

`dedupe_key = sha1(source_message_id | source_row_id | text, project_id, kind, aspect_key)`

- 优先用**来源标识**：同一来源消息 ⇒ 同一条记忆，重复采集不再建；
- 来源缺失时退化到内容指纹；
- 键里带 `project_id` / `aspect_key`，避免不同项目的同名事项互相吞并。

### 2.2 冲突

同一 (`project_id`, `aspect_key`) 下出现**两个以上不同 `value`** ⇒ 判为冲突：

- 冲突双方**都保留**，不自动取最新值、不自动合并；
- 冲突在上下文里以 `conflicts[]` 返回，并计入 `missing_info`
  （`code = unresolved_conflict`）；
- 冲突的消解方式只有两种：**追加纠正**（人确认哪个对）或**核实升格**。

> 「有人改口」和「两个数据源打架」的处置方式完全不同，只有人能分清。

### 2.3 追加纠正

`correct_memory(memory_id, new_value, reason)`：

1. 新建一条同 `kind`、同 `aspect_key` 的记忆（记录新值）；
2. 把旧记录标成 `superseded`，写 `superseded_by = 新 memory_id`；
3. **旧记录的业务字段一个都不改**，原值仍在事件流中可回溯。

---

## 3. 行动闭环

### 3.1 状态机

```
候选 candidate
   → 待确认 pending_confirm
   → 待执行 ready
   → 进行中 in_progress
   → 待验收 pending_acceptance
   → 已关闭 closed

旁路：blocked（阻塞） / cancelled（取消）
重开：closed / cancelled → in_progress（或 pending_confirm）
```

合法迁移表见 `schema.ACTION_TRANSITIONS`。每次迁移都写一条
**行动事件**（表 `行动事件`，append-only），含 `from_state` / `to_state` /
`reason` / `actor` / `evidence_id` / `created_at` —— 「谁在什么时候为什么改的」
永远查得到。非法迁移直接拒绝（`illegal_transition`），**不留任何事件**。

### 3.2 行动必备字段

| 字段 | 说明 |
|---|---|
| `owner` | 负责人（缺失 ⇒ 计入信息缺口 `action_no_owner`） |
| `due_date` | 截止时间 |
| `due_date_original` | **原始**截止时间，跨天滚动时不变 |
| `acceptance_criteria` | 验收标准（缺失 ⇒ 信息缺口 `action_no_criteria`） |
| `blocked_reason` / `blocked_owner` / `blocked_since` | 阻塞原因、被谁卡住、从何时起 |
| `evidence_ids` | 关联证据列表 |
| `carry_over_count` / `overdue_days` | 跨天结转次数、逾期天数 |
| `reopen_count` / `cancel_reason` | 重开次数、取消原因 |

`kind` 决定关闭时用哪套硬规则：`general` / `kitting`（齐套）/ `shipment` /
`delivery` / `acceptance` / `purchase` / `production`。

### 3.3 四条不许讲人情的硬规则

| # | 规则 | 失败原因码 |
|---|---|---|
| 1 | **关闭必须通过验收** —— 没有验收证据（`kind=acceptance` 且 `claim_type=accepted`）一律拒绝 | `need_acceptance_evidence` |
| 2 | **「已发货」不算「已到货」** —— 需要到货的行动，只有 `shipped` / `in_transit` 证据时拒绝 | `shipped_not_arrived` |
| 3 | **「部分到货」不能关闭齐套行动** —— `kitting` 只认 `full` 或「实收 ≥ 总量」的 `arrived` | `partial_not_kitting` |
| 4 | **未完成事项跨天保留原期限** —— 滚动只累加 `carry_over_count` / `overdue_days`，`due_date` 一个字符都不改 | —（约束） |

另有：`acceptance_criteria_not_met`（验收标准未逐条满足）、
`acceptance_rejected`（验收结论为不通过）、`no_evidence_linked`（无任何证据）、
`not_pending_acceptance`（不在待验收状态）。

> 规则 4 的理由：期限是被承诺过的东西。隔一夜就自己往后跑，
> 等于系统偷偷替人改了承诺。

### 3.4 证据与断言口径

证据 `claim_type` 是**互斥枚举**，禁止混用：

`shipped` 已发货 / `in_transit` 在途 / `arrived` 已到货 / `partial` 部分到货 /
`full` 齐套到货 / `accepted` 验收通过 / `rejected` 验收不通过 / `unknown`

其中「真正到货」只认 `arrived` / `partial` / `full`（`schema.ARRIVAL_CLAIMS`）。
证据去重键：同一来源 + 同一口径 ⇒ 同一条证据。

### 3.5 提醒：去重 + 失败恢复

- `reminder_key = action_id + kind + due_date`，落到 `提醒` 表的
  `__row_id__ = RMD-sha1(reminder_key)` ⇒ **同一提醒只可能有一条**；
- 带 `due_date` 是刻意的：期限改了要重推，**没改就绝不重复推**；
- 投递失败不丢：记录 `attempts` + `last_error`，状态置 `failed`，
  下次重试**复用同一条记录**（不产生第二条），成功即置 `sent`；
- 失败恢复入口：`ProjectBrain.failed_reminders()`。

提醒类型：`overdue` 已逾期 / `due_today` 今日到期 / `blocked` 被阻塞 /
`pending_acceptance` 待验收 / `unassigned` 未指派负责人。

---

## 4. 四类授权语义（分别定义）

业主明确要求把四件事分开定义。本期的落地方式如下：

| 类别 | 常量 | 回答什么问题 | 谁说了算 | 本期实现 |
|---|---|---|---|---|
| **事实核实** | `fact_verification` | 这条观察能不能升格成事实 | 核实人 + 证据 | `verify_observation(observation_id, evidence_id, verified_by)`；无证据或无署名一律 `rejected` |
| **行动业务** | `action_business` | 这个行动的状态该不该走这一步 | 状态机 + 四条硬规则 | `transition` / `close_action`；非法迁移与硬规则违规一律拒绝 |
| **执行授权** | `execution_grant` | 人同不同意这次写入 | **人**（`WriteGrant`） | 见 §5 |
| **系统执行状态** | `system_execution` | 这步到底跑没跑成 | 运行账本（`data/runs/<run_id>/final.json`） | 复用第一期 `application/gates.py`；不由人授权 |

前两类在写台账时以 `[fact_verification]` / `[action_business]` /
`[system_execution]` 前缀记入「备注」列，审计时可直接 grep
（沿用第一期 `[GRT-xxxxxx]` 前缀的既有惯例）。

---

## 5. 写入闸门：复用第一期，不新造一套

**本期没有实现任何新的授权机制。** 所有写入都走第一期那两个模块：

```
ProjectBrain._write()
  ├─ ① application/authorization.authorize_write(grant, table, action)   ← 授权裁决，唯一出处
  ├─ ② 乐观锁：expected_version != 当前版本 → stale_version（不写）
  └─ ③ application/dataservice.DataService.write(WriteRequest, mode)     ← 统一写入路径
         · preview            → candidate（一行都不落盘）
         · apply + 无授权     → blocked（一行都不落盘）
         · 幂等键已用过        → skipped_reuse
         · 写后读回验证不过    → verify_failed + 台账留证
         · 成功               → written + 台账（含授权编号）
```

因此「高置信不等于授权」「消息和文件内容不能作为执行指令」在本期是
**结构上不可能违反**的：`ProjectBrain` 的写入签名里**根本没有置信度参数**。

授权文件格式与第一期完全一致（`data/approvals/<grant_id>.json`），
只有 `tables` 需要填本期的本地记忆表名：

```json
{
  "grant_id": "GRT-20260926-a1b2c3",
  "tables": ["来源消息", "证据", "项目记忆", "行动项", "行动事件", "提醒"],
  "actions": ["append", "update"],
  "actor": "老板",
  "reason": "合成项目演练通过，同意写入本地记忆库",
  "issued_at": "2026-09-26T10:00:00",
  "expires_at": "2026-09-26T20:00:00",
  "max_uses": 0
}
```

一条命令即可生成：`python project_brain.py grant --actor 老板 --hours 8 --out <path>`

### 5.1 写结果状态词表

| `status` | 含义 | 是否落盘 |
|---|---|---|
| `written` | 已写入并读回验证通过 | 是 |
| `candidate` | preview：候选，未写 | 否 |
| `blocked` | 无授权 / 授权不覆盖该表或动作 / 已过期 / 次数用尽 | 否 |
| `skipped_reuse` | 幂等键已写入过 | 否 |
| `duplicate` | 业务级重复（同一来源消息 / 同一行动） | 否 |
| `stale_version` | 乐观锁失败，旧版本不许覆盖 | 否 |
| `rejected` | 业务规则拒绝（如关闭未通过验收），附 `reason_code` | 否 |
| `illegal_transition` | 状态机不允许的迁移 | 否 |
| `verify_failed` | 写入异常或读回验证失败 | 视情况 |
| `not_found` | 目标记录不存在 | 否 |

### 5.2 并发：旧版本不覆盖

本地记忆库是 **append-only 事件流 + 折叠**（`store.LocalMemoryAdapter`）：

- 每次写入只**追加一行事件**，当前状态由折叠算出 —— 历史永不丢失；
- 行版本 `__version__` = 该行已发生的事件数；
- 写入方可携带 `__expected_version__`，不匹配即抛 `StaleVersionError`，
  上层返回 `stale_version` 且**不写任何内容**；
- 进程内用可重入锁，跨进程用文件锁（Windows `msvcrt` / POSIX `flock`）。

---

## 6. 接口：`get_project_context(project_id, snapshot_id)`

```python
from application.project_brain import get_project_context

ctx = get_project_context("PRJ-20260926-0001")             # 读最新
ctx = get_project_context("PRJ-20260926-0001", "SNP-...")  # 读快照，可复现
```

等价调用：`ProjectBrain(root=...).context(project_id, snapshot_id)`。

- 传 `snapshot_id` 时**只读快照文件**：同一 id 反复调用结果完全一致，
  不受此后任何写入影响（第三期可对着固定版本复核）；
- 快照不存在时返回 `{"error": "snapshot_not_found", ...}`，不抛异常；
- 快照由 `ProjectBrain.snapshot(project_id, mode=apply, grant=...)` 生成，
  落在 `<root>/snapshots/<snapshot_id>.json`；`mode=preview` 时不落盘。

### 6.1 返回结构

```jsonc
{
  "contract_version": "project-brain-v1",
  "project_id": "PRJ-20260926-0001",
  "project": { /* 项目行（若有），内部字段已剥离 */ },
  "snapshot_id": "SNP-20260926-0001",          // 非快照读取时为 ""
  "version": 48,                                // 上下文版本 = 各记录版本之和，只增不减
  "generated_at": "2026-09-26T11:00:00",
  "data_as_of": "2026-09-25T15:30:00",          // **数据截至时间**（不是「现在几点」）

  "source_freshness": [                         // 来源时效（只统计真从外部读回的原始记录）
    {"source_system": "email", "source_base": "", "source_table": "供应商邮件",
     "records": 2, "age_hours": 92.7, "oldest_age_hours": 92.7,
     "stale": true, "stale_after_hours": 24}
  ],

  "facts":         [ /* 已核实事实 */ ],
  "observations":  [ /* 未核实观察 —— 不得当事实用 */ ],
  "commitments":   [ /* 承诺 */ ],
  "decisions":     [ /* 决策（含 rationale） */ ],
  "predictions":   [ /* 预测（含 confidence / rationale） */ ],

  "actions":       [ /* 行动：状态/负责人/截止/原期限/逾期/阻塞/证据/每条自带 as_of */ ],
  "action_events": [ /* 行动迁移留痕 */ ],
  "blockers":      [ /* 阻塞：原因 + 负责人 + 被谁卡住 + 起算时间 */ ],
  "today_focus":   [ /* 今日重点：到期/逾期/阻塞/待验收 */ ],

  "evidence_refs": [ /* 证据全量（含八个统一字段 + 来源溯源） */ ],
  "missing_info":  [ /* 信息缺口：[{code, message, ref, subject, as_of}] */ ],
  "conflicts":     [ /* 冲突：[{conflict_id, aspect_key, values, members, resolved}] */ ],

  "counts": {"actions_open": 2, "actions_closed": 1, "blockers": 0,
             "conflicts": 1, "gaps": 10},

  "answers": {           // 五个问题，每条结论都带证据 + 数据截至时间
    "status":    {"answer": "...", "items": [...], "evidence_ids": [...],
                  "as_of": "...", "caveats": [...]},
    "blockers":  {...},
    "today":     {...},
    "decisions": {...},
    "gaps":      {...}
  }
}
```

**唯一硬约束：`answers` 里每一项都必须有 `evidence_ids` 与 `as_of`。**
`as_of` 是「这条结论依赖的最新一条证据/记忆的采集时间」，
不是「我们什么时候跑的脚本」—— 前者才有信息量。

### 6.2 信息缺口原因码

| code | 含义 |
|---|---|
| `action_no_owner` | 行动没有负责人 |
| `action_no_due` | 行动没有截止时间 |
| `action_no_criteria` | 行动没有验收标准 |
| `action_no_evidence` | 行动没有任何证据支撑 |
| `fact_no_evidence` | 事实缺少证据引用 |
| `stale_source` | 来源数据超过时效阈值（默认 24h）未更新 |
| `unresolved_conflict` | 存在未解决的来源冲突 |

---

## 7. 合成样例怎么用

```bash
# 1) 造授权（人亲手做的一步）
python project_brain.py grant --actor 老板 --hours 8 --out data/approvals/GRT-demo.json

# 2) 预演：一行都不写
python project_brain.py replay --file docs/contracts/examples/project-brain-sample.json

# 3) 真实写入（仅本地记忆库）
python project_brain.py replay --file docs/contracts/examples/project-brain-sample.json \
       --mode apply --grant-file data/approvals/GRT-demo.json

# 4) 五问
python project_brain.py query --project-id PRJ-20260926-0001 --question today
python project_brain.py query --project-id PRJ-20260926-0001 --question decisions

# 5) 读固定快照（可复现）
python project_brain.py context --project-id PRJ-20260926-0001 --snapshot-id SNP-20260926-0001
```

样例里刻意埋了这几处，第三期可用来验证自己的读法：

| 现象 | 期望 |
|---|---|
| 同一 `aspect_key` 两个不同 `value` | `conflicts` 长度 1，双方都保留 |
| 用 `shipped` 证据关闭齐套行动 | 拒绝，`reason_code = shipped_not_arrived` |
| 用 `partial` 证据关闭齐套行动 | 拒绝，`reason_code = partial_not_kitting` |
| 同年同月同日重复 `rollover` | 第二次「滚动 0 条」（幂等） |
| 「下周五」（消息时间 2026-09-14） | 截止 2026-09-25 |
| 「3天后」（消息时间 2026-09-21） | 截止 2026-09-24 |
| 关闭后跨天滚动 | 已关闭行动不再被滚动 |

> 注意：`evidence_id` / `memory_id` / `action_id` 每次回放都不同（随机后缀），
> **字段结构恒定**。第三期请按结构读，不要硬编码 ID。

---

## 8. 本轮边界（明确不做）

- ❌ 不写任何真实业务表（SeaTable production / tasks / crm 一张都没碰）
- ❌ 不改云表结构
- ❌ 不向客户 / 供应商发送任何消息
- ❌ 不发布任何页面
- ❌ 不修改任何定时任务
- ❌ 不做新 UI
- ❌ 不做预测算法（`prediction` 只做**记录**，不做推演）

第二期的写入**全部落在本地记忆库** `<skill>/data/project_brain/`（已 gitignore）。
真实项目只读影子运行需另行申请。

---

## 9. 关键词表（中英对照）

| 概念 | 代码值 |
|---|---|
| 观察 / 已核实事实 / 承诺 / 决策 / 预测 | `observation` / `fact` / `commitment` / `decision` / `prediction` |
| 候选 / 待确认 / 待执行 / 进行中 / 阻塞 / 待验收 / 已关闭 / 已取消 | `candidate` / `pending_confirm` / `ready` / `in_progress` / `blocked` / `pending_acceptance` / `closed` / `cancelled` |
| 已发货 / 在途 / 已到货 / 部分到货 / 齐套到货 / 验收通过 / 验收不通过 | `shipped` / `in_transit` / `arrived` / `partial` / `full` / `accepted` / `rejected` |
| 齐套 / 到货 / 验收 | `kitting` / `delivery` / `acceptance` |
