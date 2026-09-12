# 项目经理替身 · 闭环 v2 设计（决策稿）

> 日期：2026-09-12 ｜ 状态：**P0-P2 框架已落地，2026-09-14（周一）实战验收**
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

| 阶段 | 内容 | 不做什么 | 状态 |
|---|---|---|---|
| P0 | contracts.py 契约 + crm Base 示例补齐 + 本设计文档 | 不动任何现有脚本行为 | ✅ 426f9eb |
| P1 | workflow runner（subprocess 包装旧脚本）+ 每日自动化改走 `workflow.py daily` | 不重写业务逻辑 | ✅ 5c43d51 |
| P1.5 | 四项架构收口：写入渲染解耦 / 口令私有化 / 统一幂等键 / DataService | 旧 CLI 行为不变 | ✅ 340f603 |
| P2 | 写链迁移 DataService（crm_dispatch 读回验证）+ `workflow.py verify` 验收命令 + 本文档定稿 | 不迁移 data/ 目录 | ✅ 2026-09-12 |
| P3 | 根脚本变薄壳；补报价/方案版本对象（最大新功能增量） | 不动 SeaTable adapter 语义 | 待启动 |
| P4 | 拆 cockpit/wechat_intake/market；SKILL.md 缩到 ~250 行 | 不上微服务/消息队列 | 待启动 |

## 7. 与 v1.x 的兼容

- 根 CLI 全保留：op/market/wechat_intake/cockpit/crm_dispatch 行为不变；
- 现有表不动结构，只**加列**（prj_id/mo_id/po_id/shp_id/as_id 可空，旧数据回填脚本另做）；
- CRM「自动写入+台账」原则升格为 v2 通用原则：`auto_with_ledger`；
- production/tasks 候选制原则不变，升格为 `approval_required`。

## 8. v2.0 执行框架全景（当前真实形态）

```
┌─ 自动化 Prompt 层（每日 9 点，automation-1786757548346）
│    只做三件事：启动工作流 / AI 语义步骤（事件登记·CRM 识别·群聊总结）
│    / 发布通知播报（读 final.json，禁止凭记忆；禁止播报口令）
│
├─ workflow.py（统一入口）
│    run daily --mode apply        10 步 DAG 真实执行
│    run daily --mode preview      演练（写入类步骤全拦截）
│    run daily --resume <run_id>   断点续跑（成功步骤跳过）
│    status / list                 历史查询
│    verify latest                 ★ 运行后验收（周一实战核对工具）
│
├─ application/（执行框架核心，零业务逻辑）
│    contracts.py   RunContext/StepSpec/三道闸/12 类对象 ID/14 态状态机
│    runner.py      DAG 拓扑执行 + checkpoint（data/runs/<run_id>/NN_*.json）
│                   + final.json（播报唯一数据源）+ retry/abort/resume
│    dataservice.py 统一写入：路由策略（production/tasks 候选，crm 自动+台账）
│                   + 读回验证（中文列名静默丢列 → verify_failed）
│                   + 幂等键 + data/write_ledger.csv
│                   + write_verified() 原语（供写链复用）
│
├─ workflows/daily_refresh.py（10 步 DAG 定义）
│    seatable_sync → partdb_sync →(abort 级) 微信 pull/summary → wxmatch
│    → alerts → foresee(+review) → daily_brief(--push) → cockpit
│    AI 语义步骤（分流/AI 总结/CRM 识别/播报）留在 Prompt 层
│
├─ 写入链（已收口 DataService）
│    crm_dispatch.py  lead/follow：查重 → 幂等键 → write_verified()
│                     （读回验证）→ 单向关联 → crm_dispatch_ledger.csv
│    won_deal.py      plan 只读 → 人核对 → apply --yes 三表写入+读回+台账
│    op.py            数据写入与驾驶舱刷新解耦（--refresh 或环境变量显式开）
│
└─ 安全边界
     口令：passwords.py（show/check/rotate）手动专用，自动播报零口令
     台账：crm_dispatch_ledger / won_deal_ledger / write_ledger 三账并行可核对
     幂等：同键重写 → idempotent_reuse，自动化重跑不重复写
```

**`workflow.py verify` 验收逻辑**（退出码 0=通过，可接 CI）：
1. 步骤状态：failed/blocked 任何一步 → 不通过；
2. 产物核对：声称成功的步骤必须在 data/ 留下新鲜产物（mtime ≥ 运行日期-1 天，
   防止拿旧文件充数——这正是 9-12 晨「微信 summary 静默跳过」事故的检测器）；
3. 安全检查：final.json 不得出现口令字段名；CRM 台账幂等键列迁移状态提示。

## 9. 2026-09-14（周一）实战验收清单

自动化 9 点跑完后，按序执行：

```
# ① 核心验收：工作流自身声称的 vs 磁盘真实存在的
python workflow.py verify latest

# ② 播报一致性：抽 2-3 个数字（异常数/待确认事件数/CRM 写入数）
#    对照 final.json 与收到的播报文本，不一致 = 播报凭记忆（违规）
python workflow.py status <当日 run_id>

# ③ 口令零泄露：搜当日播报全文（含企微/邮件正文）中不得出现任何口令
python passwords.py check          # 确认口令体系完整

# ④ CRM 幂等实测：人为触发一次重跑（同一条来单消息再写一次）
python crm_dispatch.py follow --customer "<测试客户>" --data '{"跟进状态":"初步沟通","本次跟进内容":"<当日已有内容>"}'
#    预期：返回 idempotent_reuse，台账不新增行；若 created = 幂等失效

# ⑤ 驾驶舱新鲜度：打开「项目管理驾驶舱.html」确认数据是当日（对照①的产物核对）

# ⑥ （可选）断点恢复演练：删掉当日 final.json 里某步的 checkpoint 不会发生——
#    直接观察真实失败时的 --resume 行为即可，不必人为制造
```

**判定标准**：①②③④ 全过 = v2.0 框架实战验收通过，P3 启动；
任一失败 = 记录现象 + final.json + 台账，当天修复后周二复验。

## 10. P3 起步清单（验收通过后）

1. 根脚本薄壳化：op.py 的 apply-wizard/apply-text 内部切 DataService；
2. 报价对象落地：报价表（新）+ QUO-xxx ID + 版本快照，接 pipeline BOM 成本；
3. ID 生成器：12 类对象 ID 的统一发放与查重（contracts.new_object_id 已有雏形）；
4. 状态轨迹表扩展：把阶段轨迹泛化为 (对象类型, 对象ID, 原状态, 新状态, …)。
