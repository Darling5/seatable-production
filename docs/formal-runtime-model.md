# seatable-production 运行框架形式化建模（Formal Runtime Model）

> 状态：草稿 v0.2 ｜ 抽象层次：**脱离业务内容，只看运行框架**
> 提炼自：`application/contracts.py`、`application/authorization.py`、`application/gates.py`
> 以及设计契约 `docs/avatar-loop-v2.md`、`docs/trusted-execution-v1.md`
>
> **v0.2 修订（2026-10-02，随运行级三态改造同步）**：
>   · 新增 §5.3 运行级终态 `RSTATUS`（含 `degraded`），原 5.3/5.4 顺延为 5.4/5.5；
>   · §7.1 明确 Ledger 成员由 **`NN_<step>.json` 命名契约**界定；
>   · §7.2 判定 (a) 纳入 `degraded`，(b) 作用域收窄到步骤文件，补注 7.2-a / 7.2-b；
>     2026-10-03 批次 0b 追加注 7.2-c（`skipped` 移出运行级可发布集，见 `merge-plan.md` Q11/E12）；
>     2026-10-03 追加注 7.2-d（`latest` 口径 —— `FIX-5` 按 `Q3` 降格为本节操作约定，不立不变量）；
>   · §7.3 关键步骤集合 K 改为**按工作流分别定义**；
>   · 新增不变式 G12（运行级状态单一实现）、G13（账本自洽）。
>
> **关系**：本文建模的是**中层——项目执行控制平面**（收口 G1–G13）；
> 其**向上**（公司 / 组合 / 经营）与**向下**（制造执行 / 实物）的扩展见
> `docs/domain-model.md`（续编 G14–G30）。
>
> 本文不描述任何具体业务字段（产品名、客户名、金额…一律以占位符 ⟨·⟩ 表示），
> 只对一个"生产交付协同系统"的**运行骨架**做离散数学/形式化建模。
> 目的是回答两个问题：
> 1. 这套系统由哪些**实体**、**状态**、**转换规则**构成；
> 2. 哪些性质是系统**必须保持的不变量**（Invariants）。

---

## 0. 为什么要建模（而不是画流程图）

流程图（README 里的 mermaid）回答"有哪些模块、数据怎么流"。
形式化建模回答"系统在任何时刻**必须满足什么约束**"——它把框架从**图**提升为**可推理的命题集**。

对于一个以"可信执行"为核心卖点的系统，后者比前者重要：图看不出漏洞，命题能。

---

## 1. 符号约定

| 符号 | 含义 |
|---|---|
| `Set` | 一个有限集合 |
| `⊆` `∈` `∉` | 子集 / 属于 / 不属于 |
| `×` | 笛卡尔积 |
| `→` | 全函数（每个输入有唯一输出） |
| `↦` | 偏函数（部分输入有定义） |
| `⊢` | "推导出 / 判定为" |
| `⟨x⟩` | 业务占位符（与框架无关的具体内容） |
| `≡` | 恒等于 / 定义为 |

文中所有大写英文标识符（如 `SIDE_*`、`STATUS_*`、`PROJECT_STATES`）均直接取自源码常量，可在对应 `.py` 中逐字核对。

---

## 2. 实体模型 E（Entities）

系统的一切操作都作用在以下实体上。

### 2.1 业务对象集合 O 与 ID

系统追踪 **12 类业务对象**，每类有稳定的 ID 前缀（ID 是主键，名称只是展示）：

```
O_kind  ≡ { customer, lead, opportunity, requirement, solution,
            quote, contract, project, production_order,
            purchase_order, shipment, after_sales }

prefix : O_kind → { CUS, LED, OPP, REQ, SOL, QUO, CTR, PRJ, MO, PO, SHP, AS }

new_object_id(kind, seq) ≡ prefix(kind) · "-" · ⟨YYYYMMDD⟩ · "-" · ⟨XXXX⟩
```

**铁律（对应 avatar-loop-v2 §1）**：业务主键是生成的 ID，不是名称。
`validate_object_id(kind, value)` 用正则 `PREFIX-\d{8}-[0-9A-Za-z]{4}` 校验。

### 2.2 运行上下文 RunContext

一次工作流运行 `r` 拥有唯一上下文 `rc_r`，所有步骤共享，禁止各自推导路径：

```
RunContext ≡ {
  run_id, workflow,
  actor, mode ∈ {preview, apply},
  yes ∈ {true, false},            # 显式确认（destructive 必需）
  dry_run, base_name, skill_dir, data_dir, config_path, resume_of
}

new_run_id(workflow) ≡ workflow · "-" · ⟨YYYYMMDD⟩ · "-" · ⟨HHMM⟩ · "-" · ⟨abcd⟩
```

### 2.3 步骤 StepSpec 与结果 StepResult

```
StepSpec ≡ {
  id, name, run,
  depends_on ⊆ StepID,
  side_effect ∈ SIDE,              # 见 §3.1
  write_mode ∈ {approval_required, auto_with_ledger, ∅},
  retry ∈ ℕ, failure_policy ∈ {continue, abort},
  idempotency_key, blocking ∈ {true, false},
  expect_artifacts ⊆ Path
}

StepResult ≡ {
  step_id, status ∈ STATUS,        # 见 §5.2
  counts, artifacts, warnings, error, writes
}
```

### 2.4 授权 WriteGrant

```
WriteGrant ≡ {
  grant_id, tables ⊆ Table, actions ⊆ {append, update}, routes ⊆ {production, tasks, crm},
  actor, reason, issued_at, expires_at,
  max_uses ∈ ℕ₀, used ∈ ℕ₀, source ∈ {manual, approval},
  approval_id, row_id, payload_hash ∈ {ϵ} ∪ SHA256
}
```

### 2.5 证据链 EvidenceLink

每条业务数据都可反查来源（avatar-loop-v2 §3）：

```
EvidenceLink ≡ ( event_id → intent_id → candidate_id → write_id,
                 related_object_type, related_object_id, source, source_ref )
```

**不变式**：任意一行业务数据 `w` 都存在一条 `EvidenceLink` 使 `w.write_id` 可回溯到某 `event_id`。

---

## 3. 副作用与路由模型（Side-effect & Route）

### 3.1 副作用分级集合 SIDE

每个步骤**必须声明**其一（contracts.py:25-29）：

```
SIDE ≡ { read_only, local_append, online_write, destructive, publish }

read_only       ≡ 只读（lookup、foresee --json）
local_append    ≡ 写本地快照/台账/事件（可重放、可清理）
online_write    ≡ 写 SeaTable / CRM / PartDB 等业务后端
destructive     ≡ 删除文件、清证据（永远需要显式 --yes）
publish         ≡ 对外发布驾驶舱 / 稳定链接
```

### 3.2 路由策略函数 ρ

写入按"路由"分流到不同策略（contracts.py:35-39）：

```
ROUTE_POLICIES ≡ {
  production → approval_required,   # 候选制：人工点头才写
  tasks      → approval_required,
  crm        → auto_with_ledger      # 自动写 + 台账核对 + 可撤销
}

ρ(route) : {production, tasks, crm} → {approval_required, auto_with_ledger}
```

### 3.3 三道闸裁决函数 δ_gate

执行前对每个步骤统一裁决（contracts.py `StepSpec.allowed_in`）：

```
δ_gate(step, rc) ⊢ (bool, reason)

δ_gate(step, rc) ≡
  if side_effect = destructive  ∧ ¬rc.yes
      → (false, "destructive 需要显式 --yes")
  if side_effect ∈ {online_write, destructive, publish} ∧ ¬rc.is_apply
      → (false, "写入/删除/发布在 preview 模式下被拦截")
  if side_effect = online_write ∧ ρ(write_mode) = approval_required ∧ ¬rc.yes
      → (false, "approval_required 写入需要人工确认")
  else → (true, "")
```

**不变式 G1（无越权写入）**：对任意 `step` 与 `rc`，若 `exec(step, rc)` 实际产生了 `online_write` 或 `destructive` 或 `publish` 副作用，则 `δ_gate(step, rc) = (true, _)` **且**该写入还须通过 §6 的授权模型。

---

## 4. 状态机模型（Project State Machine）

### 4.1 状态集合 Σ

项目主线 14 态 + 2 旁路（contracts.py `PROJECT_STATES`）：

```
Σ_main  ≡ { lead, opportunity, requirement_confirming, solution_confirming,
            quotation_confirming, contract_pending, won_and_funded,
            procurement, in_production, quality_check,
            ready_to_ship, delivering, acceptance, closed }

Σ_bypass≡ { cancelled, after_sales }
Σ       ≡ Σ_main ∪ Σ_bypass
```

### 4.2 合法转移关系 T

（contracts.py `_build_transitions`，主链**跳步/回退一律放行**，仅收口少数硬约束）

```
T ⊆ Σ × Σ 定义如下：

  T(closed)      = { after_sales }                       # 结案只能进售后
  T(after_sales) = { closed }                            # 售后可回结案
  ∀ s ∈ Σ_main \ {closed}:
      T(s) = (Σ_main \ {s, after_sales})
             \ (若 s ≠ acceptance 则去掉 closed)
             ∪ { cancelled }                             # 任何非终态可取消
             ∪ (若 s ∈ {ready_to_ship, delivering, acceptance} 则加 after_sales)
  # cancelled 只进不出（不在任何 T(s) 的值域里）
```

### 4.3 严重性函数 sev

合法 ≠ 正常。跳步/回退不被阻断，但必须让人**看见**（contracts.py `transition_severity`）：

```
sev(old, new) ≡
    if (old,new) ∉ T           → illegal
    if old=cancelled ∨ new=cancelled ∨ old=closed ∨ new=closed
                              → normal
    let i = index(old), j = index(new)  in Σ_main 的顺序
    if i,j 有其一 ∉ Σ_main     → normal      # 旁路转移
    if j = i+1                 → normal
    if j > i                   → skip        # 跳步（加急）
    if j < i                   → rollback     # 回退（返工）
```

**不变式 G2（硬终态）**：`closed` 之后只允许 `after_sales`；`cancelled` 之后无任何合法转移。
**不变式 G3（售后可达性）**：`after_sales` 只能从交付后状态 `{ready_to_ship, delivering, acceptance}` 进入。
**不变式 G4（可见性）**：任意一次状态迁移 `st` 都落一条 `StateTransition` 记录，且 `st.severity ∈ {normal, skip, rollback, illegal}`，`illegal` 不允许落库。

---

## 5. 执行模型（Execution / DAG）

### 5.1 工作流即 DAG

```
Workflow W ≡ (V, E)，其中
  V ⊆ StepID 是步骤集合，
  E ⊆ V × V 是依赖边（E 取自各 StepSpec.depends_on）。
```

### 5.2 步骤终态集合 STATUS

```
STATUS ≡ { success, skipped, failed, blocked }
  success  步骤正常完成
  skipped  正常跳过（无微信消息 / 无 OCR 环境 / 当晚未授权）
  failed   步骤出错
  blocked  前置失败 或 等待人工确认
```

### 5.3 运行级终态集合 RSTATUS

步骤终态（5.2）描述**一步**，运行级终态描述**一次运行**。两者不可混用：

```
RSTATUS ≡ { success, degraded, failed }

run_status : seq StepResult → RSTATUS        # 唯一口径，见不变式 G12
run_status(S) ≡
    if   S = ∅                        → failed      # DAG 为空 = 配置错误（fail-closed）
    elif ∃ s ∈ S : s.status ∉ {success, skipped} ∧ blocking(s)
                                      → failed      # 阻断型失败
    elif ∃ s ∈ S : s.status ∉ {success, skipped}
                                      → degraded    # 仅非阻断失败（降级）
    else                              → success
```

`degraded` 的语义与后果：

| 值 | 含义 | 门禁 | 播报 |
|---|---|---|---|
| `success` | 全部步骤 success/skipped | allow | 正常 |
| `degraded` | 跑完，但有非阻断失败（如 PartDB 不可达 → 库存沿用旧快照） | **allow + 强制告警** | **必须点名是哪一步、沿用哪份旧数据** |
| `failed` | 有阻断型失败，或 DAG 为空 | deny | 逐条点名 |

> **注 5.3**：`RSTATUS` 与 `STATUS` 必须分开建模 —— 把它们混成一个集合会导致
> 「运行成功了」与「某一步成功了」在同一套判定里互相污染。

### 5.4 执行函数

```
exec : Step × RunContext ↣ StepResult
  满足：exec(step, rc) 仅在 δ_gate(step, rc) = (true,_) 时产生副作用。
```

### 5.5 失败策略与阻断

每个步骤声明 `failure_policy`（continue / abort）与 `blocking`（默认 true）：

```
blocking(step) = true   ⇒ 该步 failed 会阻断其下游依赖步骤 与 对外发布
blocking(step) = false  ⇒ 失败只告警（真实降级，但仍须可见）
```

> 关键设计：把 `blocking=false` 设为**显式声明**而非默认，避免"嘴上说 continue、实际被门禁拦住"的假降级（见 gates.py 注释）。
> ⚠️ 但仅靠声明不够：只要**运行级状态**不含 `degraded`（§5.3 修复前），
> 声明了 `blocking=false` 的步骤一失败，整次仍是 `failed` → 门禁 deny →
> 上面这条"失败只告警"永远到不了，只能靠手改账本绕过。两条必须同时成立。

---

## 6. 授权模型（Authorization）

> 核心信条（trusted-execution-v1 §2.1）：**置信度不构成授权。**

### 6.1 覆盖谓词 covers

```
covers(g : WriteGrant, table, action, now, route, check_uses) ⊢ (bool, reason)

covers(g, t, a, now, r, ck) ≡
    require validate(g) = (true,_)
    require a ∈ {append, update}
    require now ≥ issued_at                     # 未生效
    require expires_at = ∅ ∨ now < expires_at   # 过期
    require ¬ck ∨ max_uses=0 ∨ used < max_uses  # 次数用尽
    require tables = ∅ ∨ t ∈ tables            # 表范围
    require actions = ∅ ∨ a ∈ actions          # 动作范围
    require routes = ∅ ∨ r ∈ routes            # 路由范围
```

### 6.2 原子预占 reserve

为防止"用重试扩大一次性授权"，写库前必须原子预占一次（authorization.py `GrantStore.reserve`）：

```
reserve(g, scope) ⊢ g'   其中 g'.used = g.used + 1
  · 超时 / 读回失败 / 进程中断 均不退还次数
  · 同一 grant_id 不可更换来源路径
```

**不变式 G5（一次授权一次写）**：`max_uses=1` 的授权在第一次 `reserve` 后即 `used=1`，第二次 `covers(...,check_uses=true)` 必返回 false。

### 6.3 审批 → 授权（强约束）

审批通过才可转授权，且**必须绑定批准的载荷**（authorization.py `grant_from_approval`）：

```
grant_from_approval(approval, route, action) ⊢ g  当且仅当
    approval.status = approved
    ∧ g.routes 为单元素 ∧ g.tables 为单元素 ∧ g.actions 为单元素
    ∧ g.max_uses = 1
    ∧ g.payload_hash = sha256(approval.after)        # 实际写入载荷须与批准一致
    ∧ (action=update ⇒ g.row_id ≠ ∅)
```

**不变式 G6（载荷一致性）**：`authorize_write` 在 `g.payload_hash ≠ ∅` 时，要求实际写库行 `row` 的 `sha256(row) = g.payload_hash`；任一字节不一致即拒绝。这堵死了"批准 A、写 B"的篡改。

**不变式 G7（授权来源纯净）**：`authorization.py` 刻意不接收置信度字段；`source=approval` 的授权必定来自某条 `status=approved` 的 `ApprovalRequest`，不存在"算法自评即授权"的路径。

---

## 7. 发布门禁模型（Publish Gate）

> 原则（gates.py 首行）：**账本里没证明过的，不许对外发布。**

### 7.1 账本 Ledger

```
Ledger(run) ≡ { final.json（权威汇总） } ∪ { NN_<step>.json : step ∈ V }
  每步落盘的 StepResult，含 status / counts / artifact_hashes(SHA256)
  ⚠️ 只有 NN_<step>.json 算步骤文件。其余 .json（context.json、人工 dump、
     旁路产物）不属于 Ledger，读取时**跳过**而不得判账本不可信（见 7.2-b 注）。
```

### 7.2 门禁判定函数 G

```
G : Ledger → { allow, deny }

G(L) ≡ deny  若任一成立：
    (a) 整次运行状态 RSTATUS ∉ {success, degraded}；
    (b) 存在**步骤文件**（NN_<step>.json）缺 step_id / 状态 ∉ STATUS /
        counts 非表结构 —— 账本不可信；
    (c) 存在阻断型 failed 步骤（status=failed ∧ blocking=true）；
    (d) 关键步骤 k ∈ K(workflow) 的 status ∈ {failed, blocked}；
    (e) 关键步骤 k ∈ K(workflow) 的 counts.verify_failed > 0
         （"以为写进去了，其实没写"，看板与真实数据已脱节）；
    (f) 某关键步骤 k ∈ K(workflow) 根本不在账本里（该阶段没跑）。
  else allow，并输出 warnings（不改变 allow/deny，但必须播报）
```

> **注 7.2-b（非步骤 JSON 不得连坐）**：判定 (b) 只作用于 `NN_<step>.json`。
> 真实事故：`data/runs/daily-20260930-1903-6c9e/` 内被丢进一份顶层是数组的
> `unsent_outbox.json`，旧实现遍历到它就返回「账本不可信」，导致该次运行
> **连 final.json 都读不到**、11 个完好步骤文件全部作废、发布永久被拒。

> **注 7.2-c（`skipped` 为何不在 (a) 的可发布集里 —— 2026-10-03 批次 0b 修正）**：
> 判定 (a) 原写作 `RSTATUS ∉ {success, skipped, degraded}`，把**步骤级**的 `skipped`
> 混进了**运行级**的可发布集 —— 这是**类型混淆，不是设计**。`RSTATUS`（§5.3）的值域是
> `{success, degraded, failed}`，`run_status()` **不可能**产出 `skipped`
> （31 份账本实测 0 次）。更要紧的是后果：把它列在「可发布」里等于给
> 「手写 `final.json` 声称 `skipped`」开后门 —— 实测改动前
> `gates.evaluate_dict({"status": "skipped"}, critical=())` 返回 `allowed=True`。
> 已随 `merge-plan.md` Q11/E12 移除（commit `fb3d8f6`），并各配一条用例锁死两个层级的
> 语义差异：**运行级 `skipped` 非法**（`tests/test_write_safety.py::TestPublishGate::test_运行级_skipped_判不可发布`）／
> **步骤级 `skipped` 合法**（`::test_步骤级_skipped_仍然放行`，对应 G9）。
> ⚠️ 判别口径：**不要用「路径/名字里有没有某个字符」这类现象特征**写判据
> （本次守卫修复时第一版自检就栽在这上面），要写因果关系。
> 同一病根的第二个实例是 `context.json`（RunContext 序列化，无 step_id）。
> 因此「哪些文件构成 Ledger」必须由命名契约给出（见 7.1），而不是"凡是 .json 都算"。

> **注 7.2-d（`latest` 口径 —— FIX-5 来源注，2026-10-03 补）**：
> 判定 (a) 的输入 `RSTATUS` 取自「最近一次运行」的账本，而**「最近」的定义是操作约定，
> 不是不变量** —— 故按 `merge-plan.md` Q3 / `DEV-KICKOFF.md §4.3` **降格为本节注**，
> 不单独立编号（它与 `G8` 发布门槛语义重叠：都是「拿哪份账本做判定」的前提）。
>
> **约定**：`latest` = **时间上最新**的一次运行，**不是**目录名字典序。
> 实现点 `application/gates.py::find_run_dir`（`":171"` 处，`run_id in ("", "latest")`）；
> 原实现用 `sorted(os.listdir())[-1]`，**实测会把 `daily-` 排在 `evening-` 前**
> —— 于是「最新」拿到的是**上一个时段的账本**，发布门禁判的是过期数据。
> 同类坑在 `cockpit/cockpit.py::_latest_run_summary`（`:702` 处 `max(cands, key=run_sort_key)`）
> 与 `tests/test_source_health.py` 的 `:228` 行（「按时间，不按目录名字典序」）各有一份回归。
> ⚠️ 这也是 `merge-plan.md §6-E9` 那类「阶段一完成无法判定」的同源问题：
> **口径若只活在注释里、不写进建模文档，复核者无法核对。**


> 是「有降级但仍可发布」的唯一合法表达。它被加入白名单之前，该系统里**不存在**
> 这个语义，运营只能手改账本把 `failed` 写成 `success/skipped` 才能发布
> （见 `data/runs/patch_final_*.py`，以及 2026-10-02 晚的账本：11 步含 1 步非阻断失败、
> 而 `status` 写的是 `success`，仓库内任何代码路径都算不出该值）——
> 结果是**真实故障在驾驶舱里彻底消失**。故：`degraded` 放行，但**必须**出现在 warnings 中。

### 7.3 关键步骤集合 K

K 是**按工作流分别定义**的 —— 两个工作流的关键阶段不同，用同一个全局集合是错的：

```
K : Workflow → 2^V
K(evening) ≡ { wechat_collect, wxmedia_ocr, wxmatch_scan, evening_write }
K(daily)   ≡ { seatable_sync, cockpit }
```
（这几步一旦没跑成，看板就不该发出去。）

> **注 7.3**：早期版本是单个全局常量，且四个成员**全属 evening**；
> 于是 daily 侧的关键步骤检查一次都不命中 —— `daily` 用的是
> `wechat_pull`/`wechat_summary`，保护形同虚设（"看起来有护栏，其实栏杆是空的"）。
> 现在按 `final.json` 的 `workflow` 字段取对应集合；未知工作流取**并集**（保守）。

### 7.4 产物绑定 bind

```
bind(artifact, L) ≡
    require artifact 在 L 的 artifact_hashes 中登记
    require sha256(artifact) = L 中记录的哈希
```
这保证"用旧账本放行新文件"被挡住——文件在账本生成后被改过即拒绝（gates.py 2026-09-27 收紧项）。

**不变式 G8（发布门槛）**：一次 `publish` 副作用实际对外释放信息 ⟹ `G(Ledger(this_run)) = allow` 且待发布文件通过 `bind`。
**不变式 G9（skipped 不连坐）**：`status=skipped` 的步骤在 G 中**不**构成拒绝条件（数据源偶发不可用是正常状态）。
**不变式 G10（fail-closed）**：账本找不到 / 读不懂 / 格式不合规 ⟹ `G = deny`，不存在"没看见 failed 就放行"的灰色通道。
  其作用域是 **Ledger 成员**（`NN_<step>.json`）；非成员 JSON 既不入账也不触发 deny（见注 7.2-b）。
**不变式 G12（运行级状态单一实现）**：`RSTATUS` 的推导在全系统中**有且只有一个函数** `run_status()`。
  任何一个改动 `steps` 的写者（runner 收尾、`workflow.py note` 补记 AI 步骤）都必须调用它。
  反例（真实发生）：该判定曾同时存在于**三处**（runner / gates 白名单 / `cmd_note`），
  其中 `cmd_note` 用的是旧口径 → evening 自动化每调一次 `note`（一晚 3 次）就把 runner
  刚判好的 `degraded` 覆盖回 `failed`，发布门禁随即又拦死，等于**静默撤销**降级能力。
**不变式 G13（账本自洽）**：`final.json` 的 `status` 必须等于 `run_status(final.steps)`。
  违反时不 deny，而是**告警**（"疑似人工改写，请复核"）——因为 `publish` 步骤跑在
  `final.json` 落盘**之前**（此时无 final.json，判定天然不适用），且历史账本多为人工补，
  deny 会误伤正常发布。真正危险的"阻断型失败写成 success"已由 (c) 拦下。
  设计意图：**让"手改账本才发得出去"这件事在账本里留下永久痕迹**，
  而不是靠人自觉（早期 3 个 `patch_final_*.py` 就是这么来的）。

---

## 8. 适配器抽象（Adapter Abstraction）

系统把具体存储后端（SeaTable / PartDB / 飞书 / 禅道 …）抽象为可替换实现：

```
Adapter ≡ 三层契约（必修方法 / 能力声明 / 可选方法）+ 12 个能力位
factory : backend_name → Adapter    # register_backend + fail-closed 路由
```

**不变式 G11（fail-closed 路由）**：未登记 / 未知后端名 **不**静默退回默认，而是写入路径直接抛错；未知后端不会"悄悄用本地 CSV 兜底"而产生数据分歧。

> 与禅道的关系：禅道（zentao）是 `factory` 已注册的 6 个后端之一
> （`backend: zentao`，对接禅道 REST v2 的 PM 实体）。换言之，在框架里
> 禅道只是"一个被适配的存储实现"，不是"管理主体"——这正是前面对话中
> "禅道适合当落点、不适合当建模主体"结论的工程落地。

---

## 9. 框架要素 → 禅道 PM 实体 映射（落地参考）

若要把本框架的业务对象写进禅道（已内置 `adapters/zentao`），映射关系如下：

| 本框架要素 | 禅道落点 |
|---|---|
| `O_kind`（12 类业务对象） | 禅道"产品 / 项目 / 需求 / 任务"等 PM 实体 |
| `Σ`（14+2 状态机） | 禅道任务状态流 / 自定义工作流状态 |
| `δ_gate` 三道闸 + `ρ`（approval_required） | 禅道权限 / 审批流 |
| `WriteGrant` + `G`（发布门禁） | 禅道"发布 / 里程碑" + 发布前校验 |
| `EvidenceLink` 证据链 | 禅道对象关联 / 评论附件（来源反查） |
| `Adapter` 三层契约 + 12 能力位 | 禅道 REST v2 API 适配层 |

---

## 10. 系统不变量汇总（命题集）

把全文的不变量收口为一页，便于对照测试与审计：

```
G1  无越权写入：任何 online_write/destructive/publish 必过 δ_gate 且过授权模型。
G2  硬终态：closed→{after_sales} 唯一出口；cancelled 只进不出。
G3  售后可达性：after_sales 仅从 {ready_to_ship, delivering, acceptance} 进入。
G4  状态迁移可见性：每次迁移落 StateTransition 记录，illegal 不允许落库。
G5  一次授权一次写：max_uses=1 的授权 reserve 后即不可再用。
G6  载荷一致性：实际写入行 sha256 = 授权 payload_hash，否则拒绝。
G7  授权来源纯净：不接收置信度；approval 授权必来自 approved 的审批。
G8  发布门槛：publish 实际释放 ⟹ 门禁 allow 且产物 bind 通过。
G9  skipped 不连坐：正常跳过不阻断发布。
G10 fail-closed：账本缺失/不可读/格式错 ⟹ 门禁 deny。
G11 适配器 fail-closed：未知后端名直接抛错，不静默兜底。
G12 运行级状态单一实现：RSTATUS 的推导全系统有且只有一个函数 run_status()。
G13 账本自洽：final.json.status == run_status(final.steps)（违反**告警不 deny**）。
G31 案件完整性：建案必须有 CASE_SEED_KINDS 三个种子对象（customer/lead/opportunity），
    缺失即自愈或拒建；空壳不得被谎报为复用。
```

> **共 14 条**：`G1`–`G13` 定义于 §5–§7.2（本节为汇总，**不是**它们的定义处 ——
> `G12`/`G13` 本节此前漏列，`§11` 曾写「11 条」，2026-10-03 补正，见 `merge-plan.md` E1）。
> `G31` 于 2026-10-03 批次 0b **补充登记**：实现早于本模型
> （`domain/order_to_cash.py::CASE_SEED_KINDS` / `::Service._repair_case`），
> 与 `G1`–`G13` 同属 `L_proj`，此前从未登记故另编 `G31`（`merge-plan.md` Q2）。

---

## 11. 小结

这套运行框架的形式化骨架可以浓缩为三句话：

1. **实体层**——12 类对象各有稳定 ID，任何数据可经 `EvidenceLink` 回溯到来源事件；
2. **控制层**——步骤按 `SIDE` 分级、按 `ρ` 路由、过 `δ_gate` 三道闸，写库必经 `WriteGrant`（置信度不构成授权），状态按 `Σ`/`T` 迁移且 `sev` 强制可见；
3. **信任层**——执行落 `Ledger`，发布必经 `G` 门禁与 `bind` 产物绑定，全链路 `fail-closed`。

所谓"可信执行"，在形式化层面就是上述 **13 条**不变量（`G1`–`G13`）在每一次运行中恒为真
（另有 `L_proj` 同层的 `G31` 案件完整性，2026-10-03 补充登记，见 §10 末注）。
测试（如 `tests/test_trusted_execution.py`）本质上就是对这些不变量的抽样验证。
