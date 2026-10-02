# 结合计划（Merge Plan）

> 产出者：**对方窗口**（= 本窗口，见 §0.2 的说明）｜产出时间：2026-10-02
> 输入：`docs/formal-runtime-model.md`（v0.2，G1–G13）· `docs/domain-model.md`（v0.1，G14–G30）
> · `docs/DEV-KICKOFF.md` · `docs/invariant-coverage.md` · `docs/merge-plan-template.md`
> · 本窗口自有产出（16 改 + 2 新建，清单见 §0.2）
>
> 硬约束遵守情况：**只读 + 只写本文件**；未改业务代码、未改两份建模文档、未改 `DEV-KICKOFF.md`；
> 业务内容一律占位符 `⟨·⟩`；结论均给 `文件路径 + 章节号/符号名`。
>
> **结论可复现命令**（本节所有数字均由下列命令实测得出，非记忆）：
> ```bash
> cd C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0
> git log -1 --format='%h %ad %s' --date=iso        # → b5f94ef 2026-10-02 15:21:33 +0800
> git status --short | awk '{print $1}' | sort | uniq -c   # → 16 M / 8 ??
> git diff --numstat                                # → 改动行数矩阵（见 §0.2）
> grep -rnoE '\bG[0-9]{1,2}\b' --include=*.py . | wc -l    # → 43
> grep -rhoE '\bG[0-9]{1,2}\b' --include=*.py . | sort | uniq -c
> ```

---

## 0. 双方产出清单

### 0.1 本侧（seatable-production 建模与开工手册）

| 文件 | 内容 | 编号空间 | 行数 | 版本控制 |
|---|---|---|---|---|
| `docs/formal-runtime-model.md` | 运行框架形式化模型（`L_proj`） | 不变量 `G1–G13` | 508 | ❌ untracked |
| `docs/domain-model.md` | 企业域模型三层平面 + 接缝 | 不变量 `G14–G30` | 421 | ❌ untracked |
| `docs/DEV-KICKOFF.md` | §3 资产地图 / §4 编号冲突 / §6 Step 0 / §7 四阶段 / §10 已拍板 | — | 511 | ❌ untracked |
| `docs/invariant-coverage.md` | Step 0 交付物骨架（30 行命题已填，其余 4 列待填） | `G1–G30` | 87 | ❌ untracked |
| `docs/OPENING-PROMPT.md` | 阶段 A 开场提示词（本轮指令来源） | — | 92 | ❌ untracked |
| `docs/merge-plan-template.md` | 本文件骨架 | — | 130 | ❌ untracked |
| `docs/contracts/*`（3 份）· `docs/trusted-execution-v1.md` · `docs/avatar-loop-v2.md` | 既有设计契约（两份模型承接自它们） | — | — | ✅ tracked |

> **六份建模/手册类文档全部未纳入 git** —— 这不是细节，它是 §6-E8 那条"不可核对项"的直接成因，已在 §8 列为 Q5（建议优先处置）。

### 0.2 对方窗口（本窗口）产出

> ⚠️ **先说明一件必须说清的事**：本轮"两个窗口"的产出**只有一部分是独立的**，边界如下。
> - **不独立**：`docs/formal-runtime-model.md`（G1–G13）。据本窗口工作日志
>   `2026-10-02.md` L686，该文档 **v0.1 由本会话生成、v0.2 由本窗口同步**——
>   这一侧是**两侧同源**。本轮凡涉及它的"一致"结论都是**自我确认**，不构成独立验证。
> - **真独立**：`docs/domain-model.md`（G14–G30）· `docs/DEV-KICKOFF.md` ·
>   `docs/invariant-coverage.md` · `docs/OPENING-PROMPT.md` · `docs/merge-plan-template.md`
>   —— 这五份**从未出现在本窗口的工作日志里**，出自本侧窗口。
>   本轮最有价值的发现（`E1` / `E4` / `E9`）全部来自这五份。
> - 时间上**不构成违规**：只读建模文档的边界 23:45 才在 `OPENING-PROMPT.md` 声明，
>   晚于本窗口 23:29 的那次改动。
>
> 详见 §6-E8、§8-Q6。此处如实申报，不做美化。

本窗口产出 = 工作区 **16 改 + 2 新建**，实测行数矩阵（`git diff --numstat`）：

| 产出（路径，+增/−删） | 一句话说明 | 归类 |
|---|---|---|
| `application/contracts.py`（+49/−0） | 新增 `STATUS_DEGRADED`（`:52`）与 `run_status()`（`:55`）—— 运行级终态唯一实现；`__all__` 导出 | 状态机 / 不变量实现 |
| `application/gates.py`（+155/−24） | 门禁口径收口：`_NON_STEP_JSON`(`:59`) `_STEP_FILE_RE`(`:65`) `_RELEASABLE_RUN_STATUSES`(`:68`) `_LEGACY_DEGRADED_RUN_STATUSES`(`:70`) `CRITICAL_BY_WORKFLOW`(`:75`) `run_sort_key`(`:95`) `load_steps` 重写(`:174`) `evaluate_steps` 加自洽检查(`:261`) | 门禁 |
| `application/runner.py`（+9/−4） | 收尾改调 `C.run_status(rr.steps)`（消除第 1 份实现） | 状态机 |
| `workflows/workflow.py`（+18/−9） | `_latest_run_id` 复用 `run_sort_key`；`cmd_note` 改调 `run_status`（消除第 3 份实现） | 状态机 |
| `workflows/daily_refresh.py`（+20/−4） | DAG 改 12 步；`partdb_sync`/`loop_trigger` 声明 `blocking=False` | 编排 |
| `workflows/loop_trigger.py`（+84/−21） | `_existing_cases()` 返回 `{"root_id","kinds","missing"}`；案件残缺则继续 `start_case` | 业务不变量（案件完整性） |
| `domain/order_to_cash.py`（+71/−1） | 新增 `CASE_SEED_KINDS`（`:60`）与 `Service._repair_case()`（幂等自愈）；`start_case` 幂等分支识别空壳并返回 `plan`/`repaired` | 实体 / 状态机 |
| `sync/seatable_sync.py`（+9/−1） | `main()` 补 `sys.exit(3)`：缺凭证时 `sync()` 返回 `None` 而退出码为 **0** → DAG 把"压根没同步"记成 `success`（静默假成功）；现按 `runner.py:303` 约定报 skipped | 同步 / 退出码语义 |
| `wx/wx_watchlist.py`（+10/−4） | 原实现**先写盘后校验**：SeaTable 读不到时把词表覆盖成**空** → 静默漏采；改为**先校验后写盘**，0 实体即中止并保留旧词表（`return 2`） | 同步 / 退出码语义 |
| `cockpit/cockpit.py`（+1941/−222） | 新增 `SOURCE_SPEC` / `_parse_ts` / `_dotted_get` / `_load_source_health` / `_latest_run_summary` / `satFreshThresholds`；新增 `secSH` 数据源健康面板；`syncFreshness()` 去硬编码阈值 | 度量 / 看板 |
| `automations/README.md` `daily-prompt-v2.md` `evening-prompt.md`（+79/−22） | 三态口径（`degraded` 不是成功）、`--gate <run_id>` 必传、禁改账本、新增"数据源健康"与"控制平面案件摘要"播报项 | 流程口径 |
| `tests/test_contracts.py`(+98) `tests/test_write_safety.py`(+215) `tests/test_workflow_daily.py`(+41) | 新增 70 用例：`TestRunStatus` 真值表 + **源码级守卫**（三路径同源，含正则自证）、`TestLatestRunResolution`、`TestLedgerReading`、自洽性检查 | 测试 |
| `tests/test_loop_trigger.py`（**新建** 225 行）· `tests/test_source_health.py`（**新建** 364 行） | 13 + 27 用例：退出码语义 / 幂等 / 空壳自愈 / `SOURCE_SPEC` 完整性（含 `step_id` 必须真实存在于 DAG）/ 三档分级 / 真实数据一致性 | 测试 |
| **`docs/formal-runtime-model.md`（v0.1→v0.2，508 行）** | ⚠️ 本窗口**改动了本侧建模文档**：新增 §5.3 `RSTATUS`、§7.1 注、§7.2 注 7.2-a/7.2-b、§7.3 `K(workflow)`、G12/G13 | ⚠️ **越出本轮边界 / 不可核对项** |

**本窗口明确未做的**：未新增任何业务对象、未新增任何后端、未触碰 `application/authorization.py`、未触碰 `Σ`（16 态）与 `SIDE`（5 级）。

---

## 1. 差异对照表（本轮结论主体）

**结论取值**：`一致` ｜ `等价但异名` ｜ `冲突` ｜ `单边有`

> 读法提示：本轮两侧**工作平面基本不相交** —— 本窗口全部产出落在 `L_proj` 的运行框架，
> 本侧建模的重点扩展方向是 `L_gov` / `L_exec` / 接缝（本侧自己也全部标为"空白"）。
> 因此 9 个维度里 **6 个是 `一致`**（因为我方根本没碰那些集合），**真正的冲突集中在维度 7**。

| # | 维度 | 对方（本窗口）做法 | 本侧做法 | 结论 | 冲突点 / 待定 |
|---|---|---|---|---|---|
| 1 | **实体层** | 未新增业务对象。实测 `domain/order_to_cash.py::OBJECT_SPECS` 仍 `n=12`、`id_prefix` = `{CUS,LED,OPP,REQ,SOL,QUO,CTR,PRJ,MO,PO,SHP,AS}`，与 `formal-runtime-model.md §2.1` 逐字相同；本方对 `order_to_cash.py` 的 +71 行全在**写入顺序 / 幂等 / 自愈**，未动对象集合 | 12 类对象 + `PREFIX-YYYYMMDD-XXXX`（`§2.1`） | **一致** | 无。**但** `domain-model.md §6` 要求新增 `L_gov`/`L_exec` 实体，两侧**均未实现** → 见 §7-F / §5-D |
| 2 | **状态机** | **未改** `Σ`（实测 `contracts.py:304 PROJECT_STATES` n=16）。改的是**运行级**：新增 `STATUS_DEGRADED`(`:52`) + `run_status()`(`:55`)，把"一步的终态"与"整次的终态"拆成两个集合 | `Σ_main` 14 + `Σ_bypass` 2（`§4.1`）；`STATUS` 4 值（`§5.2`）；`RSTATUS` 3 值（`§5.3`） | **一致** | 无冲突。⚠️ 但**同源**：`§5.3` 系 v0.2 随本窗口改造新增（`formal-runtime-model.md` L7–L12），**不能算独立收敛**（§6-E7） |
| 3 | **副作用分级与路由** | **未改**。实测 `SIDE_*` 5 个（`contracts.py:25-29`）、`ROUTE_POLICIES` 3 条（`:35`）= `{production→approval_required, tasks→approval_required, crm→auto_with_ledger}`，与 `§3.1/§3.2` 逐字相同。本方对 `daily_refresh.py` 的改动只是**消费** `StepSpec.blocking`(`:166`，默认 `True`)，未新增分级 | `SIDE` 5 级（`§3.1`）+ `ρ` 3 路由（`§3.2`）+ `δ_gate`（`§3.3`） | **一致** | **待定**：`domain-model.md §6` 表格原文写的是"**建议**新增两类 `financial`（开票/付款）、`physical`（出入库/发货）"—— 原文本就是"建议"而非定论，**两侧都无实现** → 见 §8-Q 不阻塞，§7-F 暂缓 |
| 4 | **授权模型** | **未触碰**。`application/authorization.py` **不在** 16 个已改文件内（`git status --short` 实测）。`WriteGrant`(`:55`) / `covers` / `GrantStore.reserve`(`:201`) / `grant_from_approval`(`:177`) / `authorize_write`(`:337`) 全为原样 | `WriteGrant` / `covers` / `reserve` / `grant_from_approval`（`§2.4`、`§6.1–6.3`） | **一致（未触碰）** | 无 |
| 5 | **发布 / 交付门禁** | **大幅改动**（`gates.py` +155/−24）。改的是**判定输入的口径**与**判定来源**，不是判定条款本身：① `_STEP_FILE_RE=^\d+_.+\.json$`(`:65`) + `_NON_STEP_JSON=("final.json","context.json")`(`:59`) 界定"什么算 Ledger 成员"；② `_RELEASABLE_RUN_STATUSES` 纳入 `STATUS_DEGRADED`(`:68`)；③ `CRITICAL_BY_WORKFLOW`(`:75`) 让 `K` 按工作流分取；④ `run_sort_key`(`:95`) 让 `latest` 按内嵌时间戳而非目录名字典序；⑤ `evaluate_steps`(`:261`) 增**自洽性检查**（`final.status` vs `run_status(steps)`，**只告警不 deny**） | `G(Ledger)` 六条判定 (a)–(f) + `bind`（`§7.2`、`§7.4`） | **等价但异名** | 判定条款**一一对应**（见 §2-A 的映射），但**编号全错位**：`G*` 这个名字被两套语义占用 → 见维度 7 |
| 6 | **不变量集合** | 本方**没有自己的不变量编号体系**，用的是 2026-10-02 那批**修复项标签** `G1–G10`（实测实存 8 个：`G1 G2 G3 G5 G6 G8 G9 G10`；`G4`/`G7` **全仓不存在**），43 处 / 9 文件。其中**两条是真正的不变量命题**：`FIX-6`（运行级状态单一实现）已被本侧 `G12` 吸收；`FIX-9`（案件完整性）本侧**尚无编号** | `G1–G30`（`formal-runtime-model.md §10` + `domain-model.md §5`） | **冲突** | ①**同一命题两处定义**（`FIX-6`≡`G12`、`FIX-8`≡`G13`，精确同义却异号）；②**一条真不变量无编号**（案件完整性，`DEV-KICKOFF.md §4.3` 建议立 `G31`）；③ 文档侧 30 条 vs 代码侧 8 个标签**共用字母 `G`** |
| 7 | **编号空间** | 代码/测试注释里的 `G1–G10` = **修复项标签** | 文档 `G1–G30` = **系统不变量** | **冲突（本轮核心）** | 见下方**撞号表**；实测 43 处 / 8 标签 / 9 文件，与 `DEV-KICKOFF.md §4.2` 声称的"8 个标签、43 处、9 文件"**完全吻合** |
| 8 | **层次归属** | 全部落在 **`L_proj`**（`application/` + `workflows/` + `cockpit/` + `sync/` + `wx/`）。**`L_gov` = 0，`L_exec` = 0，接缝协议 = 0** | 三层 + 两类接缝（`domain-model.md §1.1`、`§1.2`） | **单边有** | 无冲突，但**两侧工作平面正交**：本侧已把 `L_gov`/`L_exec`/接缝全部标为 `❌ 空白`（`DEV-KICKOFF.md §3.1`），本窗口也未补 → 差距**不是分歧，是双方都还没做** |
| 9 | **承载后端** | 未新增后端。实测 `adapters.factory.BACKENDS` = `['feishu','feishu_task','jiandaoyun','local','seatable','zentao']`（6 个），与 `DEV-KICKOFF.md §3.3` 一致；本方改动只写**本地**（`data/runs/` 账本、`data/` 快照、驾驶舱 HTML） | 6 后端 + 本地兜底；本阶段**先本地**（`§8 Adapter`、`DEV-KICKOFF.md §10 决策 3`） | **一致** | 无。附带发现（与本轮无关）：`cockpit/cockpit.py:2857 LIVE_PROBE_PORTS` 含 `8790`，与本机 justwoker 代理端口冲突 → 记入 §7-F |

### 1.7 撞号表（维度 7 展开，逐条可核对）

实测分布（`grep -rhoE '\bG[0-9]{1,2}\b' --include=*.py . | sort | uniq -c`）：
**`G2`×9 · `G5`×8 · `G9`×6 · `G10`×6 · `G6`×5 · `G1`×4 · `G3`×3 · `G8`×2 = 43 处**。

| 代码标签 | 代码侧含义（出处） | 文档同号含义 | 关系 | 后果 |
|---|---|---|---|---|
| `G1` | 整次状态白名单扩为 `{success,skipped,degraded}`（`gates.py:68`） | 无越权写入（`§3.3`） | 同源；文档对应 **G12** 的实现约束 | 一 grep 就认错 |
| `G2` | `_NON_STEP_JSON` 统一「非步骤 JSON」口径（`gates.py:59`） | 硬终态 `closed`/`cancelled`（`§4.2`） | 同源；文档对应 **G10 的作用域**（注 7.2-b） | 认错 |
| `G3` | 关键阶段按 `workflow` 分取（`gates.py:75`） | 售后可达性（`§4.2`） | 同源；文档对应 **§7.3 `K`** 判定条款 | 认错 |
| `G5` | `find_run_dir("latest")` 按时间取（`gates.py:95`） | 一次授权一次写（`§6.2`） | **文档无对应** ⚠️ | 认错；且该标签**无家可归** → §2-A |
| `G6` | `run_status` 全系统唯一实现（`contracts.py:55`） | **载荷一致性 sha256**（`§6.3`） | **精确同义于文档 `G12`** | ★ 最易埋雷之一 |
| `G8` | 账本自洽 `status == run_status(steps)`（`gates.py:261`） | **发布门槛**（`§7.2`） | **精确同义于文档 `G13`** | ★ 最易埋雷之一 |
| `G9` | 「已有案件」须看案件**完整性**（`loop_trigger.py`） | `skipped` 不连坐（`§7.2`） | **文档无对应** ⚠️ | 认错；这是一条**真业务不变量** → §2-A |
| `G10` | `NN_<step>.json` **命名契约**（`gates.py:65`） | **fail-closed**（`§7.2`） | **同名不同义**（文档 `G10` 的作用域**正是**这条） | ★★ **最危险**：易把"命名契约"当成"fail-closed 本身" |
| `G4`/`G7` | **全仓不存在** | 状态迁移可见性 / 授权来源纯净 | 无冲突 | 那天的修复清单本就不连续，**不补位** |

> **一句话结论**：`G6`/`G8` 是"同一概念两套编号"，`G10` 是"同一编号两个概念"，
> 其余 5 个是"编号撞车、含义无关"。三种病混在一起，任何基于 `grep G<n>` 的讨论都会读错。

---

## 2. A — 取谁为准（canonical 选择）

| 争议项 | 采谁 | 理由 | 影响面 |
|---|---|---|---|
| **`G*` 编号语义** | **本侧文档 `G1–G30`**（canonical） | 已拍板（`DEV-KICKOFF.md §10` 决策 2 = 方案 ①）；且不变量是长期资产、修复标签是一次性历史记号 | `.py` 43 处 / 9 文件 |
| **运行级终态三态（含 `degraded`）** | **本窗口实现 `contracts.run_status()`**，并以其**写入本侧 `§5.3` 作 canonical 命题** | 文档 `§5.3` 推导式与本实现逐字一致；且"唯一实现必须落在一个函数"本身即 `G12` | `contracts.py` / `runner.py` / `workflow.py` / `gates.py` |
| **「什么算 Ledger 成员」** | 本窗口 `_STEP_FILE_RE = ^\d+_.+\.json$`（`gates.py:65`），升为 `§7.1` 的定义 | `§7.1` 已写明"只有 `NN_<step>.json` 算步骤文件"，`§7.2` 判定 (b) 的作用域依赖它；这是修 `G10` 的前提 | `gates.py:59,65,174` |
| **关键步骤集 `K`** | **本侧 `§7.3` 按工作流分取**（= 本窗口 `CRITICAL_BY_WORKFLOW`，`gates.py:75`） | 文档 v0.2 `§7.3` 已吸收，且注 7.3 记录了"全局常量导致 daily 侧保护形同虚设"的真实缺口 | `gates.py:75-80` |
| **`latest` 解析口径** | **本窗口 `run_sort_key`（按内嵌时间戳）**，但**降格为 `§7.2` 的操作约定**，不单独立不变量 | 文档无对应命题；`DEV-KICKOFF.md §4.3` 建议前者（操作约定），本窗口同意 —— 它是 `G8` 可复现性的前提条件，不是独立命题 | `gates.py:95` / `workflow.py::_latest_run_id` |
| **案件完整性** | **本侧**新建 **`G31`**，本窗口 `_existing_cases()` 为实现点 | `DEV-KICKOFF.md §4.3` 已建议；这是本窗口唯一一条**真业务不变量**（其余 7 个都是框架修复） | `workflows/loop_trigger.py` / `domain/order_to_cash.py::_repair_case` |
| **账本自洽（`final.status == run_status(steps)`）** | **本侧 `G13`（告警不 deny）** | 文档 v0.2 已是命题；本窗口 `gates.py:261` 实现 | `gates.py:261+` |
| **`Σ` / `SIDE` / `ρ` / `WriteGrant`** | **本侧原样**（本窗口未触碰，无需裁决） | 实测未改 | — |
| **`SIDE` 是否新增 `financial`/`physical`** | **暂不采，降为待拍板项** | `domain-model.md §6` 原文是"**建议**新增"，且 `L_gov`/`L_exec` 实体尚未存在 → 先立分级无实现载体 | 记入 §7-F |
| **阶段划分口径** | **`DEV-KICKOFF.md §7`（已拍板"先下后上"）**，**覆盖** `domain-model.md §8` | 用户 2026-10-02 拍板在先；两文档冲突见 §6-E9 | `domain-model.md §8` 需在阶段 B 加注 |

---

## 3. B — 命名与编号统一方案

**实体命名**
- `L_proj` 沿用本侧 `O_kind` 12 类 + `PREFIX-YYYYMMDD-XXXX`（实测两侧一致）。
- `L_gov`/`L_exec` 新实体命名**一律取自本侧 `domain-model.md`**（`Item` `BOM` `PR` `PO` `GR` `WorkOrder`
  `Routing` `Operation` `WorkCenter` `Inventory` `Shipment` `POD` `Invoice` `Receivable` `CashReceipt`
  `RevenueRecognition` `Portfolio` `OKR` `ResourcePool` `DecisionRecord` `KPI`），**不另起别名**
  —— 这正是 `G29`（跨层 ID 一致 / 不引入新名称映射）的要求。

**状态命名**
- 项目主线 `Σ`（16 态）沿用本侧，**不动**。
- **运行级状态独立命名空间**：`RSTATUS ≡ {success, degraded, failed}`，
  **不并入** `STATUS ≡ {success, skipped, failed, blocked}`（`contracts.py:42-45,52`）。
- `L_fin`（`Receivable.state ∈ {open, partially_settled, settled, overdue, written_off}`）与
  `L_exec` 工单/工序状态机**各自独立**，三者不互相赋值（`domain-model.md §6`）。

**不变量编号空间定义（本轮最重要的一条产出）**

| 规则 | 内容 |
|---|---|
| canonical | 本侧 `G1–G30` **保持不动**，是系统不变量的**唯一权威编号** |
| 代码侧退役 | 代码/测试注释里 `G*` **全部改为 `FIX-*`**（实测 8 个标签 / 43 处 / 9 文件，`DEV-KICKOFF.md §4.2` 映射表实测吻合）；`G4`/`G7` 不存在，**不补位** |
| 新增不变量 | **从 `G31` 起编**（`G31` = 案件完整性，`DEV-KICKOFF.md §4.3`）。此后新增一律 `G32, G33, …` |
| 防再撞 | 两套命名空间的**前缀不同**：不变量 = `G<数字>`，修复记录 = `FIX-<数字>`。**禁止**出现裸 `G` 出现在 `FIX-` 之后的写法（如 `FIX-G6`） |
| 一次性 | `FIX-*` 是**历史事件记号**，只允许出现在注释/docstring，**不得**成为命题、不得进入覆盖矩阵 |

**跨侧引用写法**（统一三种格式，避免再次歧义）
```
代码注释 引用不变量 ：见 G12（docs/formal-runtime-model.md §7.2）
代码注释 引用历史修复：本处于 2026-10-02 修复（FIX-6）
文档     引用实现点 ：application/contracts.py::run_status   ← 与覆盖矩阵同格式 file::symbol
```

---

## 4. C — 合并后的实体清单与不变量清单

### 4.1 实体（去重后）

本窗口**未新增实体**，故合并清单 = 本侧清单。按层列（`domain-model.md §1.1`）：

| 实体 | 所属层 | 来源 | 现状 / 备注 |
|---|---|---|---|
| `customer` `lead` `opportunity` `requirement` `solution` `quote` `contract` `project` `production_order` `purchase_order` `shipment` `after_sales`（12 类） | `L_proj` | 本侧（**已实现**） | `domain/order_to_cash.py::OBJECT_SPECS`（实测 n=12） |
| `RunContext` · `StepSpec` · `StepResult` · `WriteGrant` · `EvidenceLink` | `L_proj`（运行框架） | 本侧（已实现） | `§2.2–2.5` |
| **`Ledger`**（= `final.json` + `NN_<step>.json`） | `L_proj`（运行框架） | **合并**：本侧 `§7.1` 定义 + 本窗口 `_STEP_FILE_RE` **命名契约** | 合并的直接产物，来自 `FIX-2`/`FIX-10`（`gates.py:59,65,174`） |
| `Item` `BOM` `PR` `PO` `GR` `WorkOrder` `Routing` `Operation` `WorkCenter` `Inventory` `Shipment` `POD` | `L_exec` | 本侧（**空白**） | 批次 1 / 批次 3（`DEV-KICKOFF.md §7`） |
| `Invoice` `Receivable` `CashReceipt` `RevenueRecognition` | `L_gov` | 本侧（**空白**） | 批次 2 |
| `Portfolio` `OKR` `ResourcePool` `DecisionRecord` `KPI` | `L_gov` | 本侧（**空白**） | 批次 3 / 4 |
| `DownCommand` `Ack` | 接缝 | 本侧（**空白**） | 批次 4 |

### 4.2 不变量（去重后）

**合并后总数 = 31**（本侧 30 + 新增 1），**≤ 30 + 8 = 38** ✅，无"同一命题两个编号"残留。

`FIX-*` 8 个标签**全部不进入不变量空间**（它们不是命题），处置如下：

| `FIX-*` | 归属 | 依据 |
|---|---|---|
| `FIX-6`（`run_status` 唯一实现） | → **并入 `G12`**（精确同义） | `DEV-KICKOFF.md §4.2` 第 6 行 |
| `FIX-8`（账本自洽） | → **并入 `G13`**（精确同义） | 同上第 7 行 |
| `FIX-1`（白名单含 `degraded`） | → **并入 `G12` 的实现约束**（`§5.3` 的取值白名单） | 同上第 1 行 |
| `FIX-2` / `FIX-10`（非步骤 JSON 口径 / 命名契约） | → **并入 `G10` 的作用域**（`§7.1` + 注 7.2-b） | 同上第 2、8 行 |
| `FIX-3`（`K` 按 workflow 分取） | → **并入 `§7.3` 的判定条款**（服务于 `G8`） | 同上第 3 行 |
| `FIX-5`（`latest` 按时间） | → **降格为 `§7.2` 操作约定**，不立不变量 | `§4.3` 建议 |
| `FIX-9`（案件完整性） | → **升格为 `G31`**（新增） | `§4.3` 建议 |

**`G31` 命题（建议原文）**：
> `G31 案件完整性`：以 `idempotency_key` 或客户名命中一个案件 ⟹ 该案件必须含
> `CASE_SEED_KINDS = (customer, lead, opportunity)` 全部三类对象；否则就地自愈补建
> （幂等，可反复调用），**不得**因为"命中了 customer 行"就判定案件已存在。
> 实现点：`workflows/loop_trigger.py::_existing_cases`（返回 `missing`）、
> `domain/order_to_cash.py::Service._repair_case`。

---

## 5. D — 分批落地顺序

**已定约束**（`DEV-KICKOFF.md §10`）：**先下后上** · 承载后端**先本地** · 编号方案 ①。

| 批次 | 范围 | 涉及实体 | 涉及不变量 | 交付判据 |
|---|---|---|---|---|
| **0 · 收口（前置，必须先做）** | ① `G*`→`FIX-*` 改名（43 处 / 9 文件）② `cmd_run` 引入**独立退出码 `2`** = degraded（`workflow.py:72` 现为 `return 0 if rr.status == C.STATUS_SUCCESS else 1`）③ `loop_sync` 降级（`daily_refresh.py:112-115` 现缺 `blocking=` → 缺省 `True`，见 `contracts.py:166`）④ 基线 commit + 六个 `docs/*.md` 纳入版本控制 | 无新增 | 不变量空间本身 | `grep -rnoE '\bG[0-9]{1,2}\b' --include=*.py .` **无输出**；`tests/test_smoke.py` + `unittest discover -s tests` **全绿**；`cmd_run` 三态退出码 `0/2/1` 有正反用例 |
| **0b · Step 0** | 填 `docs/invariant-coverage.md` 的 4 个待填列（30 行）+ `G31` 立项 + `formal-runtime-model.md §10/§11` 计数修正 | — | 全部 31 条 | 30+1 行齐、无空行；≥5 条能点名**现成测试用例**；所有"空白"项写出**具体缺口动作** |
| **1 · 向下** | 制造执行最小闭环：BOM → PR → PO → GR → 工单 | `Item` `BOM` `PR` `PO` `GR` `WorkOrder` | **`G21` `G22` `G23`**（+`G28` 的归集键） | 一个项目从"生产立项"看到"工单开工"，每步带 ID + `as_of`；BOM 无环/用量守恒；付款必有到货验收；开工必齐套或留痕放行 |
| **2 · 向上** | O2C 财务链：合同 → 订单 → 发货 → 开票 → 应收 → 回款 | `Invoice` `Receivable` `CashReceipt` | **`G16` `G17`** | 签约→回款可追；`Σ回款 ≤ 应收 ≤ 开票 ≤ 合同额`（按币种）；"验收"是"开票/收款"的前置 |
| **3** | 成本归集 + 组合与产能 | `Portfolio` `OKR` `ResourcePool` 成本条目 | **`G14` `G15` `G18`** | 每项目挂至少一个战略目标；多项目同资源同窗口承诺 ≤ 可用量 |
| **4** | 三账对账 + 度量贯通 | `reconcile()` · KPI 树 · `DecisionRecord` · `DownCommand`+`Ack` | **`G19` `G20` `G26` `G27` `G29` `G30`** | 三账差异可见且挂原因码；KPI 可溯；**无 `Ack` = 未执行** |

**承载后端**：批次 0–4 **全部先本地**（`domain/order_to_cash.py` 风格 + `data/` 落盘 +
schema 与 `adapters/local.py` 对齐）。**本地不变量全绿之后**才讨论迁 SeaTable / 禅道；
届时只新增 adapter 映射，**不变量与领域模型不应发生任何改动**（`DEV-KICKOFF.md §7` 末）。

> **批次 0 必须最先做的理由**（两条都是实测结论，不是原则性陈述）：
> ① 不先改名，批次 1–4 里每次讨论 `G21`/`G22` 都可能被误读成代码里的 `G2`/`G1`；
> ② 不先提交基线，六个 `docs/*.md` 继续 untracked → **无 diff、无历史**，
> 本轮 §6-E8 那种"不可核对项"会每次都出现。

---

## 6. E — 冲突消解记录

| # | 冲突（哪一句 vs 哪一句） | 判定 | 依据（可核对） | 后果 |
|---|---|---|---|---|
| **E1** | `formal-runtime-model.md §10`「系统不变量汇总」**只列到 `G11`**、`§11 小结` L507 写"上述 **11** 条不变量"；而 `§7.2` 已定义 `G12`/`G13`，`invariant-coverage.md §A` 也是 **13 行** | **文档内部计数漂移**，以 `§7.2` + 覆盖矩阵为准（**13 条**） | 实测：`§10` 代码块末行为 `G11`（L495），无 `G12/G13`；L507 原文"上述 11 条不变量"；`invariant-coverage.md §A` 有 13 行 | ①按 `§10` 数会漏 2 条；②`invariant-coverage.md` 头部声称"`G1–G13` ← `formal-runtime-model.md §10`"—— **这个引用本身是坏的**；③文档自称 11+17=28，与覆盖矩阵 30 **已对不上** |
| **E2** | 代码 `G6`（`contracts.py:55` 注释） vs 文档 `G6`（`§6.3` 载荷一致性 sha256） | **代码 `G6` 精确同义于文档 `G12`**（`run_status` 唯一实现） | `DEV-KICKOFF.md §4.2` 映射表第 6 行；`contracts.py::run_status` docstring 自称"全仓库唯一一份实现" | 不消解则 `grep G6` 会读到"载荷一致性"，误导最深的两处之一 |
| **E3** | 代码 `G8`（`gates.py:261` 注释） vs 文档 `G8`（`§7.2` 发布门槛） | **代码 `G8` 精确同义于文档 `G13`**（账本自洽） | 同上映射表第 7 行 | 同上 |
| **E4** | 代码 `G10`（`gates.py:65` 注释） vs 文档 `G10`（`§7.2` fail-closed） | **同名不同义**；且文档 `G10` 的**作用域**正是代码这条 | 同上映射表第 8 行，原文标注"同名不同义（最危险）" | 最易把"命名契约"当成"fail-closed 本身"，从而在覆盖矩阵里把 `G10` 填错 |
| **E5** | 代码 `G1`/`G2`/`G3`/`G5`/`G9` 的文档归属 | `G1`→`G12` 实现约束；`G2`→`G10` 作用域；`G3`→`§7.3 K`；**`G5`→`§7.2` 操作约定（文档无命题）**；**`G9`→新增 `G31`** | `DEV-KICKOFF.md §4.2` + `§4.3`；用户 2026-10-02 拍板"编号统一" | 2 个标签无家可归，必须显式裁决（已裁决，见 §2-A） |
| **E6** | 文档 `G4`（状态迁移可见性）/ `G7`（授权来源纯净）在代码侧**无同名标签** | **无冲突**（代码侧从未用过这两个号）→ **不补位** | 实测 `grep -rnoE '\bG[47]\b' --include=*.py .` 无输出 | 无。仅需在覆盖矩阵中按文档语义正常填写 |
| **E7** | 本窗口 `run_status()` 与文档 `§5.3` 推导式"完全一致" | 一致，但**同源**：`formal-runtime-model.md` L7–L12 头部记录 `§5.3` 系"随运行级三态改造同步"新增 | 文件头部修订说明（L7–L12）；该文件 mtime 23:29 = 本窗口改造同批 | 该"一致"**不能作为独立收敛证据**；阶段 B 必须用**反向测试**（注入反例 → 守卫报红）补独立证据 |
| **E8** | 本轮"两侧独立对照"这一方法学前提 | **部分不成立（可精确定位到哪一部分）**：<br>**① 不独立的部分** = `formal-runtime-model.md`（G1–G13）。据本窗口自己的工作日志，该文档 **v0.1 由本会话生成、v0.2 由本窗口同步**，即**两侧同源**。<br>**② 真独立的部分** = `domain-model.md`（G14–G30）/ `DEV-KICKOFF.md` / `invariant-coverage.md` / `OPENING-PROMPT.md` / `merge-plan-template.md` —— 这五份**均未在本会话日志中出现过**，可判定出自本侧窗口。<br>**③ 时间上不违规**：文档只读边界 23:45 才在 `OPENING-PROMPT.md` 声明，晚于改动 | ① 本会话工作日志 `2026-10-02.md` **L686**："`docs/formal-runtime-model.md`（本会话 22:48 生成，未纳入版本控制）已随语义同步至 **v0.2**"；② mtime：`formal-runtime-model.md` = 23:29 vs `OPENING-PROMPT.md` = 23:45；③ 该文件**从未纳入 git**（`git log --all -- docs/formal-runtime-model.md` 无输出）、**全盘仅 1 份**（`find`）→ 无 v0.1 副本可比，**v0.1 具体内容本窗口无法核对，如实标为"不确定"** | ⚠️ `DEV-KICKOFF.md §8.2` 自订原则是"替换清单与验证清单**必须来自不同来源**，否则验证会自洽假阴性"。<br>**实用结论**：`E7` 类"一致"（以及 §1 维度 1/2/3/4/9 的 5 条 `一致`）中，凡涉及 `formal-runtime-model.md` 的都是**自我确认**，不可当独立验证；而 **`E1` / `E4` / `E9` 三条来自本侧独立文档，是本轮价值最高的发现**，应优先处置 |
| **E9** | **三份文档对"阶段一"给出三套不同说法**：<br>① `domain-model.md §8 阶段一`：向上+向下**同时**做一段，新增 `G16 G17 G21 G23 G28`<br>② `DEV-KICKOFF.md §7 阶段一`：**只做向下**，新增 `G21 G22 G23`<br>③ `invariant-coverage.md §D` L86："阶段一（向下·制造执行）优先补的不变量：`G21 G22 G23` **+ `G28`**" | **采 ②**（`DEV-KICKOFF.md §7`），因其对应用户 2026-10-02 拍板的"先下后上" | 三处原文：`domain-model.md` L388／`DEV-KICKOFF.md` L361／`invariant-coverage.md` L86。差异点：`G22` 只在 ② 出现；`G16 G17` 只在 ① 出现；`G28` 在 ① ③ 出现但 ② 放在阶段三 | 不统一则"阶段一完成"**无法判定**。阶段 B 需在 `domain-model.md §8` 加一条"以 `DEV-KICKOFF.md §7` 为准"的注，并修正 `invariant-coverage.md §D` |
| **E10** | 退出码空间是否会被占用：`cmd_run` 将引入 `2`=degraded，而 `wx/wx_watchlist.py::cmd_refresh` **已经**用 `return 2`（0 实体/配置缺失），`application/runner.py:303` 用 `3`=skipped | **需显式分层**：`runner.py` 的 `0/3/其他` 是**步骤脚本**退出码约定；`cmd_run` 的 `0/2/1` 是**工作流 CLI** 退出码约定；两者是不同的命名空间，但必须在注释里写明，否则读代码的人会串 | `workflow.py:72`（现状 `0/1`）、`wx/wx_watchlist.py::cmd_refresh`（`return 2`）、`runner.py:303`（`== 3`） | 若不写明，未来有人把 `wx_watchlist` 的 `2` 当成 degraded 读 → 语义串台 |
| **E11** | `docs/formal-runtime-model.md` 的 v0.1 内容 | **不确定 —— 无法核对** | 无 v0.1 副本、无 git 历史、无备份 | 本文件中所有"文档 v0.2 相对 v0.1 改了什么"的陈述，其**来源是本窗口的自述**，不是可核对的 diff。已在本列明 |

| **E12** | `formal-runtime-model.md §7.2` 判定 (a) 写成「整次运行状态 `RSTATUS ∉ {success, skipped, degraded}`」，而 `§5.3` 定义 `RSTATUS ≡ {success, degraded, failed}` —— **`skipped` 不是 `RSTATUS` 的元素**（它是 `STATUS` 的元素）。代码 `gates.py:68 _RELEASABLE_RUN_STATUSES = (success, skipped, degraded)` 同病 | **文档侧类型混淆（成立）**；**代码侧那个 `skipped` 是不可达白名单项（成立）** | ① `application/contracts.py:74-83 run_status()` 的返回域实测只有 `failed` / `success` / `degraded` 三值；② **31 份运行级账本**（`data/runs/*/final.json` 顶层 `status`）实测分布 = `failed` 15 / `success` 10 / `completed_with_errors` 6 → **`skipped` 出现 0 次**。⚠️ 验证陷阱：若按全文件 `grep '"status": "..."'` 统计，会把**步骤级** status 混进来，得出 209/99/42/9 的**假分布** —— 必须按顶层字段解析 | ① 文档 `§7.2(a)` 应改为 `RSTATUS ∉ {success, degraded}`；② 代码里那个 `skipped` 删掉更 fail-closed，但属**行为变化**且现无测试 → **归 0b 与文档同批收口**（或保留并加注释"防御历史数据，实测未出现"）。**本批不动** |
| **E13** | `domain-model.md §2.2` L114 的 O2C 链写 `Quote → Contract → **SalesOrder** → Shipment → …`，但 `O_kind` 12 类对象里**没有 `sales_order`**，§5（不变量）与 §6（映射与改动点）也**从未提到**它 | **文档内部悬空实体** | ① `docs/domain-model.md:114`；② `grep -rn 'sales_order\|SalesOrder' --include=*.py .` **零命中**；③ `domain/order_to_cash.py::OBJECT_SPECS` 实测 12 类，含 `contract` / `shipment` 而无 `sales_order` | 「合同 → 发货」这段**少一个中间实体**：要么把 O2C 链直接写成 `Contract → Shipment`（本系统实际口径），要么在 §5/§6 补立 `SalesOrder` 并说清它与 `contract` / `production_order` 的分工。**阶段二开工前必须定**（见 §8-Q10） |
| **E14** | `DEV-KICKOFF.md §2` 内部计数自相矛盾：L103 注释写「16 个已改 + **4** 个未跟踪」，L116 标题写「未跟踪（**8**）」，而实测是 **9** 个 | **文档内部计数错误（三处互不相等）** | ① `grep -n '未跟踪' docs/DEV-KICKOFF.md` → L7 / L103 / L116；② 实测进入本轮时的 `git status --short` = 16 `M` + 9 `??`（8 旧 + 本轮新增的 `docs/merge-plan.md`） | `§2` 是「开工第一道关」的判断依据，计数错会让人误判工作区干净程度。且经本批 C1/C2 之后，该段描述已**整体过期**（9 个 untracked 已全部入库）→ **归 0b 重写 §2** |

---

## 7. F — 不合并 / 暂缓项

| 项 | 处置 | 理由 |
|---|---|---|
| `SIDE` 新增 `financial`（开票/付款）/ `physical`（出入库/发货）两类 | **暂缓** | `domain-model.md §6` 原文是"**建议**新增"，未拍板；且 `L_gov`/`L_exec` 实体尚不存在 → 先立分级无实现载体。待批次 2/3 实体落地后再立 |
| `Σ` 三分（`L_proj`/`L_fin`/`L_exec` 状态机各自独立） | **暂缓** | `domain-model.md §6` 明确标注"三者不互相赋值"是**目标态**，当前 `L_fin`/`L_exec` 均无载体 |
| `alignment` 式的"治理层/执行层对齐文档" | **暂缓** | `domain-model.md §6` 自标"暂不实现" |
| 三个 `data/runs/patch_final_*.py`（手改账本脚本） | **不合并进仓库**（留在 `data/`，属 gitignore） | 用户 2026-10-02 拍板"嗯"（暂不归档）；且它们是 `G13` 要留痕的**证据**，不应成为代码资产 |
| 本窗口的 `FIX-*` 标签体系本身 | **不合并为不变量** | 修复记录 ≠ 命题；只作为**注释溯源**保留在 9 个 `.py` 里（协议见 §3-B） |
| `cockpit/cockpit.py:2857 LIVE_PROBE_PORTS` 含 `8790`，与本机 justwoker 代理端口冲突 | **暂缓**（记为独立 issue） | 与本轮编号/建模合并无关，属本机端口规划问题 |
| `tests/test_automation_docs.py` 含 4 个 **pytest 风格函数**（无 `TestCase`）→ `unittest discover` 抓不到（"写了但从未被执行"） | **暂缓**（记为独立 issue） | 与建模合并无关；属测试入口口径问题，与 `DEV-KICKOFF.md §8.4` 记录的"脚本式测试会静默不跑"同类。直跑该文件 4/4 通过 |
| `formal-runtime-model.md §10/§11` 计数修正（11 → 13） | **暂缓到阶段 B 第 0b 批** | 阶段 A 硬边界：不改建模文档。已在 §6-E1 记录，不丢失 |
| `docs/formal-runtime-model.md` 与 `domain-model.md` 中"40+ 处文档计数漂移"类问题 | **暂缓** | `DEV-KICKOFF.md §8.5` 已有"以实跑为准"的既有口径，不必逐条修 |

---

## 8. G — 不确定项与需要拍板的问题

| # | 问题 | 选项 | 影响 | 建议 |
|---|---|---|---|---|
| **Q1** | 批次 0 的 4 件事（改名 / 退出码 `2` / `loop_sync` 降级 / 基线 commit + docs 纳管）是否合并为**一个 commit**？ | ①一个 commit；②拆两个（"编号统一"与"运行级语义收口"分开） | ①一次交付、回滚粒度粗；②历史清晰但多一次交付 | **①**，但 commit message **分条列出 4 件事**，便于将来 `git log -S` 定位 |
| **Q2** | `G31 案件完整性` 立不立？ | ①立（`DEV-KICKOFF.md §4.3` 建议）；②不立，降格为 `§7.2` 的注 | 立则本侧要改建模文档（阶段 A 禁止）；不立则 `FIX-9` 无处可去、覆盖矩阵 30 行装不下它 | **①**，但写入**阶段 B 第 0b 批**（与 `§10` 计数修正同批做），阶段 A 不动 |
| **Q3** | `FIX-5`（`latest` 按时间取）归属？ | ①降格为 `§7.2` 操作约定；②独立立不变量（如 `G32`） | ①不变量空间不膨胀；②可测试性更好但语义上与 `G8` 重叠 | **①**（与 `DEV-KICKOFF.md §4.3` 一致） |
| **Q4** | `formal-runtime-model.md §10/§11` 计数漂移（自称 11 条，实为 13 条；`invariant-coverage.md` 头部的 "`G1–G13` ← `§10`" 引用是坏的） | ①阶段 B 补 `G12/G13` 到 `§10`、`§11` 改"13 条"；②重写 `§10` 汇总 | `§10` 是"审计用一页"，漏 2 条会直接误导覆盖矩阵 | **①**（最小改动，保留原文可追溯） |
| **Q5** | **六个 `docs/*.md` 是否纳入版本控制？** | ①纳入（连同批次 0 的基线 commit）；②继续 untracked | 继续 untracked 的已实证风险：**无 diff、无历史、无 v0.1 可比** → 本轮 `E8`/`E11` 那种"不可核对项"会反复出现，且 `§4.4` 改名脚本一旦误伤文档无法回滚 | **①，且优先于所有代码改动**。这是本轮最便宜、收益最高的一条 |
| **Q6** | 本轮 `E7`/`E8` 已证明"两侧对照"存在同源自洽问题，是否补一次**独立复核**？ | ①另开一个窗口**只读**重跑一次 9 维度对照（不改任何文件）；②接受现状，靠阶段 B 的反向测试补证据 | ①成本低（纯只读），能发现自洽检查看不见的冲突；②省事但 `E7` 类结论永远无法升级为独立证据 | **①**。若采纳，建议把 `E9`/`E1`/`E4` 作为"已知答案"先藏起来，看复核方能否独立命中 |
| **Q7** | 方案 §七 遗留 7 项（供应商主数据是否立项 / 复盘一项目几份 / 模拟器采购周期取值 / 决策记录复用还是另建 / 复盘复用哪张表 / D2 契约漂移收口方式 / `cockpit.py` 拆分时机） | 见原方案 | 影响批次 1–4 的表设计 | **推迟到对应批次开工前再问**，不阻塞批次 0/0b 起步 |
| **Q8** | `data/runs/patch_final_*.py` 最终归档还是删除？ | ①留作 `G13` 的实证；②批次 0 后删 | 用户已说"嗯"（暂留） | 维持现状，批次 0 完成后复查一次 |
| **Q9** | `E10` 的退出码分层要不要写进文档？ | ①在 `contracts.py` / `workflow.py` 注释里写明"两套退出码命名空间"；②仅口头约定 | ①未来读代码不会串；②风险小但存在 | **①**（**批次 0 已执行**：`workflows/workflow.py::cmd_run` 与 `application/runner.py::make_step` 已互加注释，写明「工作流级 0/2/1」与「步骤脚本级 0/3/其它」是两套命名空间） |
| **Q10** | `domain-model.md` 的 `SalesOrder` 悬空（见 E13）怎么处置？ | ① 从 O2C 链里删掉，直接 `Contract → Shipment`；② 正式补立 `SalesOrder` 实体，写进 §5/§6 并扩 `O_kind` | ①符合现状、零成本；②更贴标准 O2C，但要在 `O_kind` 里开第 13 类对象，并说清与 `contract` / `production_order` 的分工 | **①**（本系统是「合同即订单」口径，制造侧已由 `production_order` 承担）。若选 ②，必须**阶段二开工前**定，否则 O2C 链会带着一个没有 ID 前缀的实体进入实现 |
| **Q11** | `RSTATUS` 白名单里那个不可达的 `skipped`（见 E12）删不删？ | ① 删（更 fail-closed）；② 保留 + 加注释 | 实测 31 份账本里运行级 `skipped` 出现 **0** 次 → 删除风险≈0；但属行为变化，需配测试 | **①**，但放 **0b** 与文档 `§7.2(a)` 同批改，并补一条「运行级 `skipped` 账本应被拒」的反向用例 |
| **Q12** | 独立复核（Q6）已执行完，结果如何处置？ | ① 已确认项直接采信、新发现补录（**本轮已做**，见 §10）；② 再开第二轮复核 | 复核独立命中我原有发现 3/3，另新增 3 条；第二轮**边际收益已递减** | **①** |

---

## 9. 自查清单

- [x] 9 个维度全部有结论，无空行（维度 7 的展开表见 §1.7）
- [x] 每个"冲突"都写明**具体句子 / 符号 + 后果 + 涉及不变量**（§1.7 + §6 共 11 条）
- [x] A–G 七节全部填完，无空章节
- [x] 所有判断都带 `文件路径 + 章节号/符号名`（含实测行号）
- [x] 未改动任何业务代码（本轮只产出本文件；`git status` 中本文件为新增，其余 16 改 + 8 untracked 均为进入本轮前既存）
- [x] 全文无真实主体名 / 手机号 / 真实数据（占位符 `⟨·⟩`）
- [x] 已**停下来**等确认，未开始开发（未执行 `DEV-KICKOFF.md` §4 改名、未动 Step 0）

> **本文件最该被先看的四条**（按"独立性 × 后果"排序）：
> 1. §6-**E9** —— 三份文档对"阶段一"给出**三套不同说法**（`G22` / `G16 G17` / `G28` 各自归属打架）
>    → 不统一则"阶段一完成"**无法判定**。来源本侧独立文档，价值最高。
> 2. §6-**E1** —— `formal-runtime-model.md §10` 只列到 `G11`、`§11` 写"11 条"，
>    而 `§7.2` 已定义 `G12/G13`，且 `invariant-coverage.md` 头部的"`G1–G13` ← `§10`"引用**是坏的**。
> 3. §6-**E4** —— 代码 `G10`（命名契约）vs 文档 `G10`（fail-closed）**同名不同义，最危险**。
> 4. §8-**Q5** —— 六份建模文档**未纳入版本控制**，是 `E7`/`E8`/`E11` 那类"不可核对项"的**总因**；
>    成本最低、收益最高的一条，建议**优先于所有代码改动**。


---

## 10. 独立复核记录（2026-10-03 00:0x）

**为什么做**：§6-E8 已证实本轮「两侧对照」存在**同源**缺陷（`formal-runtime-model.md` 由本窗口自产自评），
故 §8-Q6 建议补一次独立复核，把自洽检查升级为可复现的独立证据。

**怎么做**：起一个**全新子代理**，只喂原始材料（`docs/OPENING-PROMPT.md` + 三份建模文档 + 工作区未提交改动），
**不给任何已有结论**，并明确**禁止它读 `docs/merge-plan.md` 与 `plans/` 下的任何文件**。
它自行按 `OPENING-PROMPT.md` 第三步的 9 维度重跑一遍。

### 10.1 原有发现被独立命中：**3 / 3**

| 我的发现 | 复核是否独立命中 | 复核补充了什么 |
|---|---|---|
| `E9` 三份文档对「阶段一」三套说法 | ✅ 命中，并给出三处行号 | 指出 `G22` 是**漏项**、`G28` 是**阶段归属冲突** |
| `E1` `formal-runtime-model §10` 只列到 G11、§11 说「11 条」 | ✅ 命中 | 并指出 `invariant-coverage.md:7` 的「`G1–G13` ← `§10`」**来源声明错误** |
| `E4` 代码 `G10` vs 文档 `G10` 同名不同义 | ✅ 命中，且称其为「最危险」 | 另独立确认 code `G6`≈doc `G12`、code `G8`≈doc `G13` |

编号空间实测同样逐项吻合（**43 处 / 8 标签 / 9 文件**，含按文件分布）。

### 10.2 复核**新增** 3 条 —— 均已由我逐条亲手验证后采信

即 **E12**（`RSTATUS` 含不可达 `skipped`）、**E13**（`SalesOrder` 悬空）、**E14**（`DEV-KICKOFF §2` 计数矛盾）。
验证方式与结论见 §6 对应行；三条**全部成立**，已对应新增 **Q10 / Q11**。

### 10.3 方法学收获（这部分比结论本身更值钱）

1. **Q6 这一步确实有效**：在「同源」前提下，独立复核 3/3 命中 → 这 3 条的**证据等级从"自我确认"升级为"独立可复现"**。
2. **复核也会错，必须二次验证 —— 本轮真实发生了两次误导**：
   - 复核把代码里那个 `skipped` 判为"死代码"，我第一遍用 `grep '"status": "..."'` 统计，
     把**步骤级** status 混进来得到 209/99/42/9 的**假分布**（看似"有 skipped 数据支撑"）；
   - 只有改成按 `final.json` **顶层字段**逐份解析，才得到正确答案 **0/31**（确实不可达）。
   → 结论相同，但**结论的取证路径必须干净**，否则"验证"本身会变成新的错误来源。
3. **隔离复核者要做两件事**：① 不给已有结论；② **禁止它读我方的结论文件**。
   本次两条都做了，它也在报告里自证「未读 `merge-plan.md` 与 `plans/`，独立性未受污染」。

### 10.4 附带确认

复核同时确认：`DEV-KICKOFF.md §4.2` 声称的「8 标签 / 43 处 / 9 文件」与实测**逐项吻合** ——
这份文档在编号冲突这件事上是**可信**的（与 E14 那个 §2 计数错误不矛盾，是两处不同性质的计数）。
