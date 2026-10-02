# seatable-production 企业域模型（Enterprise Domain Model）

> 状态：草稿 v0.1 ｜ 层次：**公司整体视角的三层贯通**
> 关系：本文是 `docs/formal-runtime-model.md`（运行框架形式化模型）的**上位与下位扩展**。
> 那份文档（v0.2）只建模了「中层」——项目执行控制平面（PM 第二大脑），
> 收口执行/项目/运行级不变量 **G1–G13**；
> 本文回答更高一层的诉求：**站在公司整体视角，这个区块向上要接什么、向下要接什么**，
> 续编治理/执行/接缝不变量 **G14–G30**。
>
> 承接既有契约，不另造体系：
> `docs/contracts/project-brain-v1.md`（记忆 / 行动 / 证据 / 决策）、
> `docs/contracts/decision-support-v1.md`（工序 / 资源 / 日历 / 方案比较，向下延伸的**规划半边**）、
> `docs/contracts/alignment-p2-p3-v1.md`（跨期接缝范式）。
>
> 与前述文档一致：全文以占位符 ⟨·⟩ 表示具体业务内容，
> **不含任何真实主体名、产品名、联系方式**，符合 public 仓库铁律 1。
> 本文只做建模与"需要做什么"的定义，不涉及任何真实数据写入。

---

## 0. 视角：为什么「项目经理第二大脑」不够

现有系统的能力全景是**单项目、单平面**的：

- `formal-runtime-model.md` + `project-brain-v1` ＋ `decision-support-v1`，合起来管的是
  「**一个项目从线索到交付**」这条主线上，记忆、行动、排产、回款条件；
- 它回答的是"**这个项目**做到了哪一步、谁答应了什么、排下来会怎样"。

但公司视角要的是另外两问：

1. **向上**：这些项目合起来，对公司的经营是否成立？——钱从哪来、投到哪去、
   产能够不够、多个项目互相抢不抢资源、有没有回款风险、要不要砍掉某个项目。
2. **向下**：这个项目落成实物，靠什么执行？——料齐不齐、谁在线上做、出了几台、
   良率多少、货到没到、能不能按时交。

于是本文把系统重新组织为**三个平面**，现有模型是中间层，向上、向下各补一层与一条接缝。

---

## 1. 三层平面总览

### 1.1 平面划分

```
P ≡ { L_gov, L_proj, L_exec }

L_gov   治理平面（公司 / 组合 / 经营）
        时间尺度：季度 ~ 年   追问：该不该做、钱从哪来、谁拍板
L_proj  项目平面（项目 / 交付 / 回款）    ← 现有系统所在层
        时间尺度：周 ~ 月     追问：做到哪、卡在哪、承诺给谁
L_exec  执行平面（制造 / 供应链 / 现场）
        时间尺度：小时 ~ 天   追问：料齐没齐、线上在做什么、出了几台
```

| 平面 | 关注 | 时间尺度 | 主实体（本文新增以 ● 标） | 拥有人 | 现有落点 |
|---|---|---|---|---|---|
| `L_gov` | 组合、战略、财务、产能、决策 | 季~年 | Portfolio ● / OKR ● / Invoice ● / Receivable ● / CashReceipt ● / ResourcePool ● / DecisionRecord ● | 经营层 | **空白** |
| `L_proj` | 线索→交付→回款、记忆、行动 | 周~月 | `O_kind` 12 类（现有）、Action、Evidence、Decision | 项目负责人 | `formal-runtime-model.md`、`project-brain-v1`、`decision-support-v1` |
| `L_exec` | BOM、采购、工单、质量、库存、发货 | 时~天 | Item ● / BOM ● / PR ● / GR ● / WorkOrder ● / Routing ● / Operation ● / Inventory ● / Shipment ● | 生产 / 供应链 | 仅 `decision-support` 的**排产规划**，实物执行**空白** |

### 1.2 接缝：上行流与下行流

层与层之间不是隔离，而是**接缝（seam）**。接缝上只跑两类流：

```
UpFlow   : L_exec → L_proj → L_gov      （上报 / 归集：状态、成本、风险、产能、里程碑）
DownFlow : L_gov  → L_proj → L_exec     （分解 / 下达：目标、预算、授权、工单、采购指令）
```

**接缝协议的全部内容**（与既有体系严格一致）：

1. **只用 ID 关联，名称不作键**（承接 `project-brain-v1 §1.1`）。
   跨层引用一律用 `project_id` / `plan_id` / `<实体前缀>-YYYYMMDD-XXXX`。
2. **任何下行指令必须带 `cmd_id` + 幂等键，并等待回执 `Ack`**；
   **无 Ack = 未执行**，上层不得据此推进（见 §4.3，与既有 fail-closed 同构）。
3. **任何上行数据必须带 `as_of`（数据截至时间，不是"现在几点"）与来源**；
   来源过期须显式标注（承接 `decision-support-v1 A4` 与 `source_freshness`）。

---

## 2. 向上延伸：治理平面 `L_gov`

> 一句话：把"单个项目"放进"公司的一盘棋"里——组合选优、经营算账、产能定价、权力分工、度量贯通。

### 2.1 组合层（Portfolio）

```
Portfolio Π ≡ { P₁ … Pₙ }                  # 项目集合
Program    ≡ 一组共享目标/资源的项目
BusinessUnit ≡ 组织单元
StrategicObjective / OKR ≡ (objective, KRs[], owner, horizon)
align : Project → 2^OKR                    # 项目对战略目标的挂载（多对多）
```

**组合选优（形式化，不含数值计算）**：给定资源上限，在 Π 中选子集以最大化价值、控制风险。

```
score(p) = w_val·⟨价值⟩ − w_risk·⟨风险⟩ + w_fit·⟨战略契合⟩ − w_res·⟨资源占用⟩

maximize   Σ_{p ∈ Π'} score(p)
s.t.       Σ_{p ∈ Π'} resource_need(p, r) ≤ ResourcePool.available(r)   ∀ r
           Π' ⊆ Π
```

> 这是**背包型组合选择**的抽象（权重与量纲留给业务），
> 目的是让"该上哪些项目、砍哪个"变成一个**可解释的比较**，而不是拍脑袋。

### 2.2 经营与订单到现金（O2C）

现有模型的 `quote / contract` 只到"签约"，没有"钱"的状态机。补：

> **注（2026-10-03 批次 0b，`merge-plan.md` E13/Q10）**：本链原写
> `Quote → Contract → **SalesOrder** → Shipment → …`，但 **`SalesOrder` 是悬空实体** ——
> `domain/order_to_cash.py::OBJECT_SPECS` 的 `O_kind` 12 类里没有它（`CUS LED OPP REQ SOL QUO
> CTR PRJ MO PO SHP AS`），§5/§6 也从未引用，全仓 `.py` 零命中。本业务的销售侧是
> **合同直接发货**（无独立销售订单），故从链中移除，而非新增实体。

```
O2C 顺序链：
  Quote → Contract → Shipment → Invoice → Receivable → CashReceipt → RevenueRecognition

Receivable 状态机：
  Receivable.state ∈ { open, partially_settled, settled, overdue, written_off }
  迁移函数 δ_recv : (Receivable.state × CashReceipt) ↣ Receivable.state
```

- 关键：**"验收"是"开票/收款"的前置条件**（用户业务即"验收回款"），
  而不是像常见做法那样"发货即开票"。
- 金额链是**单调**的：`Σ回款 ≤ 应收 ≤ 开票 ≤ 合同额`（按币种，见 G17）。

### 2.3 资源池与产能（Capacity）

```
ResourcePool ≡ 人 / 设备 / 产线 / 供应商产能 四类（与 decision-support RESOURCE_TYPES 对齐：人员/设备/外协）
capacity_commitment(project, resource, window) → 承诺工时/台班
```

**多项目资源冲突**在治理层被形式化为**承诺总量约束**（G18）——
这正是既有 `decision-support` 只在一个项目内做资源可行排程、
而治理层要把"多个项目抢同一台测试设备"这件事管起来的理由。

### 2.4 治理与决策权

```
Decidable(decision_type, role) → bool        # 工业 RACI：谁有权决定哪类事
threshold(decision_type) → ⟨金额档 / 风险档⟩
DecisionRecord ≡ { decision_id, type, options[], chosen, rationale, decided_by, decided_at, refs[] }
```

- 阈值化授权：决策类型 × 金额/风险档 → 授权层级
  （例：⟨金额 ≤ T₁⟩ 项目负责人可决；超过则需 ⟨上级⟩），对应 G19。
- **决策记录是一等实体**（不再是备注），因为"当时说好了但没人记"是项目混乱的头号来源。

### 2.5 经营度量（KPI 分层）

```
度量树：  L_gov KPI   ←归集—   L_proj KPI   ←归集—   L_exec KPI
          （自上而下派目标，自下而上收数据）
```

**硬约束（G20）**：每个 KPI 值必须能追溯到实体
（不许手填"黑数"；这是把现有 `EvidenceLink` 从"写库证据"升级为"度量证据"）。

### 2.6 向上延伸「需要做什么」清单

| # | 要做的事 | 产出实体 | 关联不变量 |
|---|---|---|---|
| U1 | 建组合与项目挂载（项目↔战略目标） | Portfolio / OKR / `align` | G14 |
| U2 | 接经营财务链（合同→订单→发货→开票→应收→回款→收入确认） | Invoice / Receivable / CashReceipt / RevenueRecognition | G15 G16 G17 |
| U3 | 建公司级资源池与产能承诺 | ResourcePool / capacity_commitment | G18 |
| U4 | 建决策权矩阵与决策记录 | Decidable / DecisionRecord | G19 |
| U5 | 建度量树与 KPI 可溯 | KPI 节点 / metric 溯源链 | G20 |

---

## 3. 向下延伸：执行平面 `L_exec`

> 一句话：把"项目阶段"落成"实物与工时"——BOM、采购、工单、工序、质量、库存、发货。
> 既有 `decision-support` 只到"排产规划"，本节补"执行与实物"。

### 3.1 制造执行链

```
需求 → BOM 展开 → MRP 运算 → 采购申请 PR → 采购订单 PO → 到料 GR → IQC
     → 排产（承接 decision-support）→ 生产工单 MO → 工单 WO → 工序 Operation
     → IPQC → 总装 → OQC → 入库 → 发货 Shipment → 签收 POD
```

> 中段「排产」**不重复造**：直接消费 `decision-support-v1` 的
> `PlanningSnapshot` / `forecast_date` / `resource_feasible`，
> 由 `plan_id` 对接。本节只补**排产之后**的实物执行。

### 3.2 物料与 BOM

```
Item ≡ ⟨物料主数据⟩
BOM  ≡ 多级结构：BOM(parent, child, qty_per, version, substitutes[])
```

**结构不变量（G21）**：BOM 无环、同一版本内父件唯一、用量守恒、
替代料受控（替代关系必须显式声明，不得临时口头替换）。

### 3.3 采购与供应商（含 OEM / 外协）

```
Supplier ≡ ⟨供应商主数据 + 绩效⟩
PR (采购申请) → PO (采购订单) → GoodsReceipt (收货) → 来料验收 → 付款
```

闭环与既有 contract 承接：寻源 → 比价/招标 → 合同 → 交付 → 验收 → 付款。
**OEM / 外协特有**：委外工序、来料/回料、受托方在制与在库。
**不变量（G23）**：付款 ⟹ 存在到货验收记录（无验收不付款）。

### 3.4 排产与工单

```
ProductionOrder (MO) → WorkOrder (WO) → Operation (工序)
Routing ≡ 工艺路线（工序顺序）
WorkCenter ≡ 工作中心（产能约束的落点）
Schedule ≡ 排产结果（承接 decision-support 的 resource_feasible）
```

排产是**约束满足问题（CSP）**：

```
变量：工序开始时间 t(op)
约束：① 工序顺序 ② 工作中心日产能 ③ 物料齐套 ④ 交期
```

**不变量（G22）**：任何工单开工 ⟹ 该工单物料齐套率 = 100%，
**或**存在一条显式的"缺料放行"授权（缺料放行必须留痕，不得默认放行）。

### 3.5 质量

```
IQC（来料） / IPQC（过程） / OQC（出货）
不合格品处置：MRB（返工 / 返修 / 让步接收 / 报废）
```

**不变量（G24）**：OQC 不合格 ⟹ 不得入库、不得发货
（与既有发布门禁的 fail-closed 精神一致：不得"先发出去再说"）。

### 3.6 库存与交付

```
Inventory ≡ 结存（按 Location 库位）
StockMove ≡ 出入库流水
Shipment → ProofOfDelivery (POD 签收)
```

**不变量（G25）**：`Σ入库 − Σ出库 = 结存`（库存守恒）。
发货 + 签收 ⟹ 触发 `L_proj` 的交付/验收状态迁移（跨层触发，走接缝）。

### 3.7 向下延伸「需要做什么」清单

| # | 要做的事 | 产出实体 | 关联不变量 |
|---|---|---|---|
| D1 | 建物料主数据与多级 BOM | Item / BOM | G21 |
| D2 | 建采购闭环（PR→PO→GR→验收→付款）与 OEM/外协 | PR / PO / GR / Supplier | G23 |
| D3 | 排产只做对接，不重造（消费 decision-support） | MO / WO / Schedule | G22 G18 |
| D4 | 建工单/工序/工作中心执行态 | WorkOrder / Operation / WorkCenter | G22 |
| D5 | 建质量闸（IQC / IPQC / OQC + MRB） | Inspection / MRB | G24 |
| D6 | 建库存与发货签收 | Inventory / StockMove / Shipment / POD | G25 |

---

## 4. 三层接缝协议（Seams）

### 4.1 上行流 UpFlow

| 流 | 内容 | 跨层引用 |
|---|---|---|
| 进度上报 | 工单完成率 / 工序进度 | `project_id` ↔ `plan_id` |
| 成本归集 | 采购实际价 / 工时 / 制造费用 | 成本条目 → 项目 / 工单 / PO |
| 风险上浮 | 缺料 / 延期 / 质量异常 | 风险条目（对应既有 `blockers` 的治理层投影） |
| 产能承诺 | 已占用的资源窗口 | `capacity_commitment` |
| 里程碑 | 试产 / 量产移交 / 终验 | 里程碑回执 |

### 4.2 下行流 DownFlow

| 流 | 内容 |
|---|---|
| 目标分解 | OKR → 项目目标 → 里程碑 |
| 预算下达 | 项目预算 → 成本中心 |
| 授权 | 既有 `WriteGrant` / 审批（`formal-runtime-model §6`） |
| 工单 / 排产 | 生产订单 → 工单 → 工序 |
| 采购指令 | PR → PO |

### 4.3 指令-回执闭环（Command / Ack）

```
DownCommand ≡ (cmd_id, type, payload, issued_by, target_layer, idempotency_key)
Ack         ≡ (cmd_id, status ∈ {accepted, rejected, done}, at, evidence_ref)
```

**不变量（G26）**：任一 `DownCommand` 若在账本中找不到匹配 `Ack`，
则**视为未执行**，上层不得据此推进。
幂等：`cmd_id + idempotency_key` 重复下达不产生重复副作用（沿用既有 `reserve` 思路）。

> 这条把既有「闸门 fail-closed」从"写库"推广到"跨层指挥"：
> **没有回执的指令等于没下过**，杜绝"我以为下发了、对方说没收到"。

### 4.4 三账合一（Reconciliation）

```
Account_project ≡ 项目账（进度 / 验收）
Account_exec    ≡ 执行账（工单 / 入库 / 发货实物）
Account_fin     ≡ 财务账（开票 / 回款）

reconcile(a, b) → { 结论 ∈ {一致, 差异}, 差异表[ {项, a值, b值, 原因码} ] }
```

**不变量（G27）**：三账**允许差异，但必须可见且可解释**；
差异超阈值即触发对账任务（**不得静默**）。
这条直接回答最常见的一线乱象：**项目说交了、生产说没做完、财务说没回款**——
三本账各说各话时，系统必须把差异**摆出来**，而不是各显示各的漂亮数字。

### 4.5 闭环飞轮

```
客户需求/线索 ↑ → 立项 ↓ → 采购/生产 ↓ → 执行反馈 ↑
            → 成本/进度归集 ↑ → 经营决策 ↑ → （下一轮更准）
```

一句话：**数据上行、指令下行**，形成"签单→交付→回款→复盘→改进"的闭环。

---

## 5. 企业级不变量汇总（承接 G1–G13，续编 G14–G30）

> 既有 `formal-runtime-model.md`（v0.2）已收口执行/项目/运行级 **G1–G13**。
> 本文不重复它们，只续编**治理平面 + 执行平面 + 跨层接缝**的不变量。

```
G14 战略对齐    ：每个立项项目至少挂载一个战略目标。
G15 预算护栏    ：项目累计支出 ≤ 下达预算 ⊕ 显式超支授权。
G16 收入确认前置：收入确认 ⟹ 存在验收记录。
G17 金额单调链  ：Σ回款 ≤ 应收 ≤ 开票 ≤ 合同额（按币种）。
G18 产能承诺约束：同一资源同一窗口的承诺总量 ≤ 可用量（多项目共享）。
G19 决策留痕    ：金额/风险达阈值 的决策必有 DecisionRecord（选项+理由+decided_by）。
G20 度量可溯    ：每个 KPI 值可追溯到实体，禁手填黑数。
G21 BOM 结构    ：无环、版本内父件唯一、用量守恒、替代料显式受控。
G22 齐套开工    ：开工 ⟹ 齐套=100% ∨ 显式缺料放行授权。
G23 付款前置    ：付款 ⟹ 存在到货验收记录。
G24 质量闸      ：OQC 不合格 ⟹ 不得入库、不得发货。
G25 库存守恒    ：Σ入库 − Σ出库 = 结存。
G26 指令-回执   ：下行指令无 Ack ⟹ 视为未执行，上层不得据此推进（跨层 fail-closed）。
G27 三账可对账  ：项目账 / 执行账 / 财务账 差异可见且挂原因码，不得静默。
G28 成本可归集  ：任一成本条目可追溯到 项目 / 工单 / 采购单。
G29 跨层 ID 一致：三层引用同一 project_id；计划域用 plan_id；
                 不引入新的名称映射（承接 alignment「名称不作键」）。
G30 上行新鲜度  ：上层结论须带 as_of；来源过期须标注（承接 source_freshness）。
```

---

## 6. 与现有模型 / 契约的映射与改动点

| 现有元素 | 在本文三层中的位置 | 需要新增 / 改动 |
|---|---|---|
| `O_kind` 12 类业务对象（`contracts.py`） | `L_proj` 主实体 | 新增 `L_gov` 实体（OKR/Portfolio/Invoice/Receivable/CashReceipt/ResourcePool/DecisionRecord）与 `L_exec` 实体（Item/BOM/PR/PO/GR/WO/Routing/Operation/WorkCenter/Inventory/Shipment） |
| `Σ` 项目状态机（14+2 态） | `L_proj` 主线，**保留** | 另立 `L_fin` 的 O2C 状态机、`L_exec` 的工单/工序状态机；三者不互相赋值（借 alignment §3.3 的分家做法） |
| `SIDE` 副作用 5 级 | 跨层通用分级 | **建议新增两类**：`financial`（开票/付款）、`physical`（出入库/发货）——它们比 `online_write` 更不可逆 |
| `WriteGrant` / 发布门禁 `G` | 升格为**跨层信任机制** | 不只管"写库"，还管"指令下发 + 回执"（G26） |
| `EvidenceLink` 证据链 | 升格为**跨层证据链** | 让财务凭证、实物条码、工单回执也能回溯到源头事件（G20 度量可溯的基础） |
| 八字段 / `unified`（`project-brain-v1`） | 跨层**通用关联键** | 三层共用，不新增别名 |
| `decision-support` 的计划域（工序/资源/日历） | `L_exec` 的**排产规划半边** | 保留；本文补其"排产之后"的实物执行半边 |
| `alignment` 的接缝范式（谁写谁读、名称不作键） | 跨层接缝**方法论模板** | 治理层与执行层各需一份类似对齐层（暂不实现） |

---

## 7. 落地载体（各归其位）

| 平面 | 建议落点 | 说明 |
|---|---|---|
| `L_gov` | BI / 财务系统；禅道组合视图 | 经营数据天然在这些系统里，本层只做"语义统一 + 归集" |
| `L_proj` | seatable-production + 禅道 PM（现有） | 既有体系所在层，不动 |
| `L_exec` | ERP / MES / WMS；若无，先用 SeaTable 表结构承载 | 实物执行需要事务性强的系统 |

**本模型的角色 = 统一语义层**：让三层用**同一套实体 ID 与不变量**对话，
避免"项目说交了、生产说没做完、财务说没回款"。它不替代任何一层系统，只做"同一门语言"。

---

## 8. 分阶段路线图（需要做什么）

> 原则：不追求一次建全，先跑通**一条线**，再扩展为**一个面**。

### 阶段一 · 最小贯通（先跑通「一单到底」）

> **⚠️ 阶段归属一律以 `DEV-KICKOFF.md` §7 为准（2026-10-03 收敛，见 `merge-plan.md` E9）**
>
> 本节按「**能力贯通**」口径划分（一条线从签约看到回款），`DEV-KICKOFF` §7 按
> 「**先下后上**」的**执行序**划分 —— 两者粒度不同，若各自当开工依据必然串号。对应关系：
>
> | 本节口径 | 含 | `DEV-KICKOFF` §7 执行序 |
> |---|---|---|
> | 向下接一段（BOM → PR/PO → GR → 工单） | `G21` `G23`（**漏 `G22`**） | **阶段一** `G21 G22 G23` |
> | 向上接一段（合同 → … → 回款） | `G16` `G17` | **阶段二** `G16 G17` |
> | 成本归集 | `G28` | **阶段三**（非本阶段一） |
>
> 三处已修正的具体点：① 本阶段一**漏登记 `G22`**（齐套开工）—— 而它正是阶段一要跑的
> 「工单开工」的必要条件，已补；② `G28` 属阶段三，不属于本阶段一；
> ③ `invariant-coverage.md` §D 初稿曾按本节口径写成「`G21 G22 G23 + G28`」，已改为
> `G21 G22 G23`。**本节「新增不变量」列自此仅作能力归属，不作开工序。**

- **向上接一段**：合同 → 订单 → 发货 → 开票 → 应收 → 回款（O2C 的财务链最小闭环）；
- **向下接一段**：BOM → PR/PO → GR → 工单（制造执行最小闭环）；
- **交付物**：一个项目能从"签约"一路看到"回款"，中间每一步都**带 ID、带 as_of**。
- **新增不变量（能力归属，非开工序）**：`G16` `G17` `G21` `G22` `G23` `G28`。

### 阶段二 · 组合与产能

- 建 Portfolio + `align`（项目挂战略）；
- 建 ResourcePool + capacity_commitment（多项目资源约束）；
- 建成本归集（成本条目 → 项目/工单/PO）；
- **新增不变量**：G14 G15 G18。

### 阶段三 · 三账对账 + 度量贯通

- 实现 `reconcile()`（项目账/执行账/财务账）；
- 建度量树（KPI 自下而上归集）+ 决策记录；
- **新增不变量**：G19 G20 G26 G27 G29 G30。

---

## 9. 小结

把三个平面各一句话收口：

1. **`L_gov` 治理平面（向上）**：把项目放进组合与经营里——
   战略挂载（G14）、预算护栏（G15）、收入确认前置（G16）、金额单调（G17）、
   产能承诺（G18）、决策留痕（G19）、度量可溯（G20）。
2. **`L_proj` 项目平面（中间，现有）**：既有 `formal-runtime-model` 的 G1–G13 不动，
   记忆/行动/证据/决策/排产各安其位。
3. **`L_exec` 执行平面（向下）**：把项目落成实物——
   BOM（G21）、齐套开工（G22）、付款前置（G23）、质量闸（G24）、库存守恒（G25）。

三层的"缝"由两条协议保证：**指令-回执（G26）** 与 **三账可对账（G27）**，
外加两条底座约束：**跨层 ID 一致（G29）** 与 **上行数据新鲜度（G30）**。

> 所谓"从项目经理第二大脑升级为公司经营中枢"，形式化地说就是：
> **在原有 13 条不变量之上，再补 17 条治理/执行/接缝不变量，并让三层共用同一套 ID 与 as_of 口径。**
