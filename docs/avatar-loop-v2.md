# 项目经理替身 · 闭环 v2 设计（决策稿）

> 日期：2026-09-12 ｜ 状态：已与用户确认方向（替身 + 闭环）
> 基线：v1.9（d3c1cbe）｜ 本文档是 v2.x 的唯一架构依据，冲突时以本文为准

## 0. 目标定义（用户原话的工程化翻译）

**替身** = 系统能代替项目经理完成「感知 → 判断 → 编排 → 执行 → 追踪 → 汇报」中的前三环和部分第四环，项目经理只保留「确认 + 决策」。

**闭环** = 从微信第一句客户来单，到最终交付、回款、售后结案，每个业务对象都有：
**稳定 ID、明确状态、责任人、下一步动作、来源证据、审批记录**；任何时刻能回答
「现在在哪一步、卡在谁手里、依据是什么」。

## 1. 核心业务对象（12 个）

| 对象 | 稳定 ID | 主表（兼容现有） | 生命周期终点 |
|---|---|---|---|
| 客户 Customer | `CUS-YYYYMMDD-XXXX` | 销售线索表（客户维度） | 长期 |
| 线索 Lead | `LED-…` | 销售线索表 | 转商机 / 停止跟进 |
| 商机 Opportunity | `OPP-…` | 项目表（前置阶段） | 赢单→立项 / 丢单 |
| 需求 Requirement | `REQ-…` | 项目表.产品需求 + 版本表 | 方案确认 |
| 方案 Solution | `SOL-…`-V*n* | 文档目录 + 版本表 | 报价依据 |
| 报价 Quote | `QUO-…`-V*n* | 报价表（新） | 客户确认 |
| 合同 Contract | `CTR-…` | 合同信息表 | 签订+收首款 |
| 项目 Project | `PRJ-…` | 项目表（现有，加 prj_id 列） | 结案 |
| 生产订单 ProductionOrder | `MO-…` | 生产计划（现有，加 mo_id） | 完货入库 |
| 采购订单 PurchaseOrder | `PO-…` | 5 张采购记录表（统一 po_id） | 到货入库 |
| 发货 Shipment | `SHP-…` | 发货清单（加 shp_id） | 客户签收 |
| 售后 AfterSales | `AS-…` | 维修记录（加 as_id） | 关闭 |

**铁律：业务主键是生成的 ID，不是名称。** 名称只作展示。查重仍用「客户名称+联系方式」，
但查重命中后必须绑定到同一个 `customer_id`，禁止同名多档。

## 2. 统一状态机（项目主线 14 态）

```
lead → opportunity → requirement_confirming → solution_confirming
     → quotation_confirming → contract_pending → won_and_funded
     → procurement → in_production → quality_check → ready_to_ship
     → delivering → acceptance → closed
（旁路：cancelled / after_sales 可从任意交付后状态进入）
```

每次状态迁移写一条「状态轨迹」（现有 阶段轨迹 表扩展）：

```
(对象类型, 对象ID, 原状态, 新状态, 触发事件ID, 操作人, 时间, 依据, 关联文档)
```

**阶段 vs 工序**：本状态机挂在 `项目`（生命周期）；`生产计划.阶段` 仍存工序，
两者映射关系固化在 domain/stage_policy.py，不再散落在 SKILL.md。

## 3. 来源证据链（替身的记忆本体）

每条微信消息/文件/审批都登记：

```
event_id（唯一，bookmark+msg 哈希）
  → 产出 intent_id → candidate_id → write_id
related_object: (对象类型, 对象ID)   # 可空，未关联=待处理
```

要求：**任何一行业务数据都能反查「它来自哪条消息、哪次确认」。**
现有 工作日志（原话存档）保留，作为 text 层；证据链是 id 层。

## 4. 三层能力与自动/人工边界

| 层 | 内容 | 模式 |
|---|---|---|
| 感知 | 拉微信、识别来单/风险/待办、行情、库存 | 全自动 |
| 判断与编排 | 分流、查重、风险分级、排期建议、生成草稿（方案/报价/采购单/出货资料） | 全自动，产物=草稿 |
| 执行 | CRM 写入+台账 | **自动**（唯一例外） |
| 执行 | production/tasks 写入、报价发出、合同签订、正式下单、发货、结案、删除 | **ApprovalGate 必须人工点头** |

执行框架契约（application/contracts.py，Phase 0 已落）：
`RunContext / StepResult / StepSpec / ApprovalRequest`，
每个 Step 声明 `side_effect`（read_only / local_append / online_write / destructive / publish），
`--mode preview|apply`、`--yes`、`--resume` 由 runner 统一解释，Prompt 不再自行编排。

## 5. 首条真实项目贯通路线（MVP 验收）

选一个真实项目（候选：云南亚雄后续订单）走完整链：

```
① 微信来单 → CRM 自动建线索/跟进（已通，v1.9）
② 线索→商机：群消息出现「报价/方案」关键词 → 生成商机草稿（人工确认立项意向）
③ 需求+方案版本：AI 从聊天/PDF 提取需求 → 需求表 + 方案版本目录（草稿）
④ 报价：方案版本 + BOM 成本（现有 pipeline）→ 报价草稿 → 人工确认发出
⑤ 合同：合同 PDF（wxmatch 已能发现）→ 合同信息表 + 审批记录 → 签订
⑥ 立项：项目表建 PRJ-xxx，状态 won_and_funded，回链线索/商机
⑦ 生产订单+BOM：现有 pipeline prepare/audit/plan（人工审核关卡保留）
⑧ 采购执行：PO 写入 5 张采购表 + po_id；到货回写（微信「到货」消息→候选）
⑨ 排产：foresee 消费到货 ETA → 工序排期建议；缺料风险→工作项
⑩ 生产：贴片/组装记录回写，状态机自动推进 quality_check
⑪ 出货：出货资料（ID/SIM 格式规则）自动生成草稿 → 人工确认 → 发货清单 + SHP-xxx
⑫ 验收回款：wxmatch 收款匹配 → 回款记录候选 → 状态 acceptance→closed
⑬ 售后：维修记录 + as_id；全部对象可从 customer_id 一键穿透
```

每步验收 = 台账有记录 + 对象有 ID + 状态可查 + 证据可反查。

## 6. 分期实施

| 阶段 | 内容 | 不做什么 |
|---|---|---|
| P0（本次） | contracts.py 契约 + crm Base 示例补齐 + 本设计文档 | 不动任何现有脚本行为 |
| P1 | workflow runner（subprocess 包装旧脚本）+ 每日自动化改走 `workflow.py daily` | 不重写业务逻辑 |
| P2 | domain 层收回分流/风险/证据/行情策略；ID 生成器 + 状态轨迹写入 | 不迁移 data/ 目录 |
| P3 | 根脚本变薄壳；补报价/方案版本对象（最大新功能增量） | 不动 SeaTable adapter 语义 |
| P4 | 拆 cockpit/wechat_intake/market；SKILL.md 缩到 ~250 行 | 不上微服务/消息队列 |

## 7. 与 v1.x 的兼容

- 根 CLI 全保留：op/market/wechat_intake/cockpit/crm_dispatch 行为不变；
- 现有表不动结构，只**加列**（prj_id/mo_id/po_id/shp_id/as_id 可空，旧数据回填脚本另做）；
- CRM「自动写入+台账」原则升格为 v2 通用原则：`auto_with_ledger`；
- production/tasks 候选制原则不变，升格为 `approval_required`。
