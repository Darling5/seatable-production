# 形式化建模 → 匹配开发 · 开工手册

> 用途：把 `docs/formal-runtime-model.md`（v0.2，G1–G13）与 `docs/domain-model.md`（v0.1，G14–G30）
> 两份**建模文档**变成**可执行、可验证的代码与测试**。
> 本文件是**自包含**的：执行者即使没参与过此前的讨论，读完即可开工。
>
> 基线：`main` @ `b5f94ef`（2026-10-02 15:21）｜本文件与两份模型文档**均为未跟踪状态**。
>
> **三项决策已拍板**（2026-10-02）：**先下后上** / **编号方案 ①** / **后端先本地** —— 见 §10，施工时不再询问。

---

## 0. 怎么用这份文件

**分两个阶段，中间有一道门。本手册是"阶段 B（执行）"的说明书。**

```
阶段 A · 对齐（先判断）                     阶段 B · 执行（后开工）
读本侧建模 → 申报己方产出                     ↓ 只有在「结合计划」被确认后才进入
→ 9 维度差异对照 → 结合计划  ──── ✓ 确认 ────→ Step 0 覆盖矩阵 → §4 改名 → §7 四阶段开发
                          ↑
                    ★ 必须停下等确认
```

> **若对方窗口已有自己的产出，禁止跳过阶段 A 直接开工。**
> 强行开工的结果是：两套实体/状态/编号并行，越写越对不上。

### 0.1 ★ 阶段 A 开场（对齐阶段 · 先判断，后开工）

**用 `docs/OPENING-PROMPT.md`** —— 该文件只含一段**可直接全选复制**的开场提示词，
引导对方窗口走完：读本侧建模 → 申报己方产出 → 9 维度差异对照
→ 产出 `docs/merge-plan.md`（骨架见 `docs/merge-plan-template.md`）→ **停下等确认**。

阶段 A 的硬边界：

- **只读 + 只写 `docs/merge-plan.md` 一个文件**：不改业务代码、不改两份建模文档、不改本文件；
- 差异**不许被抹平** —— "看起来不一致"本身就是阶段 A 的产出，抹平它等于把冲突藏起来；
- 每条判断必须给 `文件路径 + 章节/符号名`，不许凭印象。

### 0.2 阶段 B 开场（开工阶段 · 结合计划确认之后才用）

**阶段 A 的 `docs/merge-plan.md` 经确认后**，再把下面这段粘贴给同一个窗口：

```
【阶段 A 已通过：结合计划我已确认。现在进入执行阶段。】

读这几个文件，然后按第一个文件里的 §6 开工：
  docs/DEV-KICKOFF.md            ← 开工手册（§6 第一步，§4 必须先做，§10 已定决策）
  docs/formal-runtime-model.md   ← 运行框架形式化模型（G1–G13）
  docs/domain-model.md           ← 企业域模型（G14–G30）
  docs/invariant-coverage.md     ← Step 0 交付物骨架（30 行，命题已填，其余列待你核对）
  docs/merge-plan.md             ← 你上一阶段产出的结合计划（已确认，按它执行）

工作目录：C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0

已定决策（不要再问，直接执行；详见 §10）：
1. 路线：先下后上 —— 阶段一先做「制造执行最小闭环」（BOM→PR/PO→GR→工单）。
2. 编号：文档 G1–G30 为权威；代码/测试注释里的 G* 一律改 FIX-*（§4 有逐条映射表与脚本）。
3. 后端：先纯本地控制平面（domain/order_to_cash.py 风格），本地不变量全绿后才谈迁 SeaTable/禅道。

硬约束（违反即返工）：
1. 这是 PUBLIC 仓库。任何真实主体名（公司/客户/供应商/产品名）、手机号、
   文件里的真实数据，一律不得写进被跟踪的文件。业务内容一律用占位符 ⟨·⟩。
   本地真实值只允许放在 config.yaml（已 gitignore），代码里只留泛化默认值。
2. 第一步不写业务代码 —— 先把覆盖矩阵填完（§6 / docs/invariant-coverage.md）。
3. ★ 先做 §4 的改名，否则后面所有不变量讨论都会串号。
4. 跑测试一律在仓库副本里跑，不要直接在仓库根跑（见 §8.4）。
```

### 0.3 对方窗口的完整阅读顺序

| 序 | 文件 | 用途 |
|---|---|---|
| 1 | `docs/OPENING-PROMPT.md` | 阶段 A 开场（直接复制粘贴） |
| 2 | `docs/formal-runtime-model.md` → `docs/domain-model.md` | 建模正文（G1–G30） |
| 3 | `docs/merge-plan-template.md` | 阶段 A 产出物骨架 |
| 4 | 本文件 | 阶段 B 手册：§3 资产地图 → §4 改名 → §6 覆盖矩阵 → §7 路线 |

---

## 1. 30 秒背景

`seatable-production` 是一个**小批量电子制造的生产交付协同系统**（Python 技能包，
运行时是「感知 → 判断 → 编排 → 执行 → 汇报」五层 + 可信执行层）。

最近为它补了两份**建模文档**（脱离业务内容，只看运行骨架）：

| 文档 | 建模对象 | 不变量 |
|---|---|---|
| `docs/formal-runtime-model.md` v0.2 | **中层**：项目执行控制平面（PM 第二大脑） | **G1–G13** |
| `docs/domain-model.md` v0.1 | **上位+下位**：治理平面 `L_gov` / 执行平面 `L_exec` + 三层接缝 | **G14–G30** |

**「匹配开发」= 让代码与测试真正兑现这 30 条不变量**，并补齐模型里定义了、
但系统尚无对应实体的那些层（治理平面几乎全空白、执行平面只到"排产规划"）。

---

## 2. 基线快照（开工前先看清楚）

```bash
cd C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0
git log -1 --format='%h %ad %s' --date=iso   # → b5f94ef 2026-10-02 15:21:33 +0800（开工基线）
git status --short                            # → 当时：16 个已改 + 9 个未跟踪（计数原记错，见下注）
```

**⚠️（历史快照）当时工作区不干净 —— 那是开工第一道关。现况见本注。**

> **★ 2026-10-03 批次 0b 收口：这道关已过。** 这批改动**已全部入库**，脏工作区问题消解。
> 处置方式：分 4 个 commit —— `b05b7c1`（运行级三态 + 账本口径修复）/ `48dfcc5`（建模文档入库）/
> `4926fcf`（`G*`→`FIX-*` 编号统一 + 三档退出码 + `loop_sync` 降级）/ `7766803`（独立复核补录）；
> 其后另有 `b629b4d`（前序方案归档）/ `fb3d8f6`（G12 单一口径）/ `0760a24`（覆盖矩阵）。
> **当前 HEAD**：`0760a24` · **工作区干净、未跟踪 0**。
> 测试规模（实测）：冒烟 `tests/test_smoke.py` **164** 项 · `unittest discover` **905** 项。

> **计数订正（`merge-plan.md` E14）**：下面两张名单的计数**原文自相矛盾** ——
> 正文写「16 个已改 + **4** 个未跟踪」，未跟踪名单标题却写「未跟踪（**8**）」，
> 而 `git status` **实测为 9**（三处互不相等）。原因是正文那处的「4」是别的口径被误当总数。
> 现按实测订正为 **16 + 9**，并保留本注以防以讹传讹。

已修改（16）：
`application/contracts.py` · `application/gates.py` · `application/runner.py` ·
`automations/README.md` · `automations/daily-prompt-v2.md` · `automations/evening-prompt.md` ·
`cockpit/cockpit.py` · `domain/order_to_cash.py` · `sync/seatable_sync.py` ·
`tests/test_contracts.py` · `tests/test_workflow_daily.py` · `tests/test_write_safety.py` ·
`workflows/daily_refresh.py` · `workflows/loop_trigger.py` · `workflows/workflow.py` ·
`wx/wx_watchlist.py`

未跟踪（实测 **9**，订正后）：
`docs/formal-runtime-model.md` · `docs/domain-model.md` · `docs/DEV-KICKOFF.md` ·
`docs/invariant-coverage.md` · `docs/OPENING-PROMPT.md` · `docs/merge-plan-template.md` ·
`tests/test_loop_trigger.py` · `tests/test_source_health.py`

> 名单只列了 **8** 项 —— 第 9 项当时未记入名单，现已随 `48dfcc5` 入库、无法逐一回溯，
> 故**如实标注这个缺口**而不是凑数。

**这批改动是 2026-10-02 那次「运行级三态（degraded）改造」的产物**，不是垃圾。
开工前必须先做二选一（**别直接覆盖**）：

- **推荐**：先跑一遍测试确认这批改动是绿的 → 提交成一个基线 commit（消息写清
  "运行级三态 + 账本口径修复"），拿到一个干净的起点；
- 或者：`git stash` 暂存，在干净基线上开工，完工后再 `stash pop`。

> 判断依据：`application/gates.py` / `runner.py` / `contracts.py` 的改动注释里
> 自称 "2026-10-02 修 G1/G2/G3/G5/G6/G8/G10"，与 `formal-runtime-model.md` v0.2 的
> 修订说明是**同一批工作**。它们已高度自洽，是可信基线的一部分。

**运行环境**（managed 环境缺 `bs4`/`pandas`，必须用这个）：

```
C:/Users/11430/.workbuddy/binaries/python/envs/default/Scripts/python.exe   # 3.13.14
```

**测试入口**（项目有"脚本式测试"，普通 discover 抓不住，两个都要跑）：

```bash
"$PY" tests/test_smoke.py            # 官方冒烟入口（CI 只跑这个）
"$PY" -m unittest discover -s tests  # 其余 unittest 用例
```

---

## 3. 已有资产地图（先复用，别重造）

### 3.1 模型文档 → 代码锚点

| 模型要素 | 代码落点 | 状态 |
|---|---|---|
| `O_kind` 12 类业务对象 + 稳定 ID | `domain/order_to_cash.py` → `OBJECT_SPECS` | ✅ 已实现 |
| `Σ` 项目状态机（14 主态 + 2 旁路） | `application/contracts.py` → `PROJECT_STATES` / `_build_transitions` / `transition_severity` | ✅ 已实现 |
| `SIDE` 副作用 5 级 / `ρ` 路由 | `application/contracts.py` → `SIDE_*` / `ROUTE_POLICIES` / `StepSpec.allowed_in` | ✅ 已实现 |
| `STATUS` / `RSTATUS` / `run_status` | `application/contracts.py` → `run_status()`；调用点 `application/runner.py`、`workflows/workflow.py` | ✅ 已实现 |
| `WriteGrant` / `covers` / `reserve` / `grant_from_approval` | `application/authorization.py` | ✅ 已实现 |
| 发布门禁 `G` / 关键步骤 `K` / `bind` | `application/gates.py` | ✅ 已实现 |
| `EvidenceLink` 证据链 | `domain/order_to_cash.py`（`EVIDENCE_FIELDS`）、`domain/evidence.py` | ✅ 已实现 |
| `Adapter` 三层契约 + 12 能力位 + fail-closed | `adapters/base.py`、`adapters/schema.py`、`adapters/factory.py` | ✅ 已实现（6 后端） |
| 工序/资源/日历/关键路径（**排产规划半边**） | `application/decision_support/`（`resources.py`/`calendar.py`/`schedule.py`/`alignment.py`…） | ✅ 已实现 |
| 记忆/行动/证据/决策（八字段） | `application/project_brain/` | ✅ 已实现 |
| **`L_gov` 全部实体**（Portfolio/OKR/Invoice/Receivable/CashReceipt/ResourcePool/DecisionRecord/KPI） | —— | ❌ **空白** |
| **`L_exec` 实物执行**（Item/BOM/PR/PO/GR/WO/Routing/Operation/WorkCenter/Inventory/Shipment/POD） | 仅 `adapters/partdb.py` + `sync/partdb_sync.py` 有"零件/库位"雏形 | ❌ **基本空白** |
| 接缝协议（Command/Ack、reconcile 三账） | —— | ❌ **空白** |

### 3.2 已验证的不变量（`tests/test_trusted_execution.py`，416 行）

该文件的用例实际覆盖：置信度不构成授权、grant 范围/过期/次数、审批→授权强约束
（载荷一致性、一次性）、preview 不留痕、幂等去重、resume 语义、abort 阻断下游、
发布门禁（ok / skipped 不连坐 / 缺关键步骤 / 有 failed / verify_failed / 账本缺失）、
工作流注册与步骤顺序。

> 这就是模型文档所说的"测试本质上是对不变量的**抽样验证**"——
> 但它是**抽样**，不是全覆盖。把抽样变全覆盖，正是本次开发的主线。

### 3.3 六后端（`adapters/`）

`local` · `seatable` · `feishu` · `feishu_task` · `jiandaoyun` · `zentao`
（+ `partdb` 作为零件库适配）。**禅道在这里只是"被适配的存储实现之一"**——
这是既定结论，本次开发**不要**去动这个定位。

---

## 4. ★ P0：先解决「G 编号双轨冲突」（不解决，后面全乱）

**问题**：仓库里存在**两套 `G*` 编号，含义完全不同**。

- **文档侧**（`formal-runtime-model.md`）：`G1–G13` = 系统不变量（`domain-model.md` 续编 `G14–G30`）。
- **代码侧**（`gates.py` / `contracts.py` / `runner.py` 注释 + 测试 docstring）：
  `G1–G10` = 2026-10-02 那批**修复项标签**，含义与文档完全不同。

**撞号实例**（左=文档，右=代码）：

| 编号 | 文档含义 | 代码注释含义 | 后果 |
|---|---|---|---|
| `G1` | 无越权写入 | 整次状态白名单 | 一 grep 就认错 |
| `G2` | 硬终态（closed/cancelled） | 非步骤 JSON 口径（`_NON_STEP_JSON`） | 认错 |
| `G5` | 一次授权一次写 | `find_run_dir("latest")` 按时间取 | 认错 |
| `G6` | 载荷一致性（sha256） | `run_status` 唯一实现 | 认错 |
| `G8` | 发布门槛 | 账本自洽（= 文档 `G13`） | 认错 |
| `G10` | fail-closed | `NN_<step>.json` 命名契约 | 认错 |

> 注意 `G6`/`G8` 这对：代码的 `G6`≈文档 `G12`、代码的 `G8`≈文档 `G13`——
> **同一概念，两套编号**，是最容易埋雷的两处。

### 4.1 已定方案：**① 文档 canonical，代码侧改 `FIX-*`**

> 用户已拍板（2026-10-02）：文档侧 `G1–G30` = 系统不变量的**唯一权威编号**，
> 代码/测试注释里的 `G*` 一律改为 `FIX-*`（"修复项"标签，一次性）。
> 理由：不变量是长期资产，修复标签是历史事件的临时记号。

**现状实测**（`grep -rnoE '\bG[0-9]{1,2}\b' --include=*.py .`）：
共 **8 个标签、43 处命中、9 个文件**。

### 4.2 映射表（逐条已核对，直接照此改名）

| 代码标签 | 代码侧含义（出处） | 文档对应 | 关系 |
|---|---|---|---|
| `G1` | 整次状态白名单扩为 `{success, skipped, degraded}`（`gates.py:32`） | §5.3 `RSTATUS` + **G12** | 同源，术语待并 |
| `G2` | `_NON_STEP_JSON` 统一「非步骤 JSON」口径（`gates.py:29`） | **G10 的作用域**（注 7.2-b）/ §7.1 | 同源，**异号** |
| `G3` | 关键阶段按 `workflow` 分取（`CRITICAL_BY_WORKFLOW`，`gates.py:37`） | §7.3 关键步骤集 `K` | 同源（门禁判定条款） |
| `G5` | `find_run_dir("latest")` 改为**按时间**取最新（`gates.py:35`） | **无对应** ⚠️ | 需判定归属（见 4.3） |
| `G6` | `run_status` 全系统**唯一实现**（`contracts.py:65`） | **G12** | **精确同义，异号** |
| `G8` | 账本自洽：`status == run_status(steps)`（`gates.py:291`） | **G13** | **精确同义，异号** |
| `G9` | 「已有案件」须看案件**完整性**（空壳自愈，`loop_trigger.py`） | **无对应** ⚠️ | 需判定归属（见 4.3） |
| `G10` | `NN_<step>.json` **命名契约**（`gates.py:62`） | G10 的**作用域**（§7.1） | **同名不同义（最危险）** |

**按文件分布**（改名范围，共 43 处）：

```
application/gates.py        G1 G2 G3 G5 G8 G10     （其中 G2×4, G5×3, G3×3, G10×2）
application/contracts.py    G6
application/runner.py       G1 G6
workflows/workflow.py       G1 G5 G6
workflows/loop_trigger.py   G9
tests/test_contracts.py     G1 G6
tests/test_source_health.py G2 G5
tests/test_write_safety.py  G2 G5 G8 G10
tests/test_loop_trigger.py  G9
```

> `G4` / `G7` **全仓不存在** —— 那天的修复清单本来就不连续，不必补位。

### 4.3 两个"无文档对应"的标签，需判定归属并记录

| 标签 | 含义 | 建议处置 |
|---|---|---|
| `G5` | `latest` = 按时间而非目录名字典序 | 并入 `§7.2` 的注作为**操作约定**（它是 G8 发布门槛可复现性的前提），不单独立不变量；改名为 `FIX-5` 后在文档 §7.2 补一句来源注 |
| `G9` | 案件空壳必须能被补建（此前"已有案件"短路导致永不修复） | 这是一条**真实的业务不变量**，建议在 `formal-runtime-model.md` 新增 **G31**（案件完整性：以 ID 命中案件 ⟹ 案件结构完整，否则就地自愈）；改名 `FIX-9` 并交叉引用 G31 |

> 两条都要**改完就在本文件记一笔**（谁判的、依据什么），别默默处理。

### 4.4 改名与验证（照做即可）

```bash
# 1) 改名：代码/测试注释里的 G<数字> → FIX-<数字>
#    范围严格限定在 §4.2 列出的 9 个文件；不要动 docs/*.md
for f in application/gates.py application/contracts.py application/runner.py \
         workflows/workflow.py workflows/loop_trigger.py \
         tests/test_contracts.py tests/test_source_health.py \
         tests/test_write_safety.py tests/test_loop_trigger.py ; do
  sed -i -E 's/\bG([0-9]{1,2})\b/FIX-\1/g' "$f"
done

# 2) 断言：.py 里再不存在裸 G<数字>
grep -rnoE '\bG[0-9]{1,2}\b' --include=*.py .   # 期望：无输出

# 3) 跑绿（改名不应改变行为；改的是注释）
"$PY" tests/test_smoke.py && "$PY" -m unittest discover -s tests
```

> ⚠️ `sed` 会一并改掉**字符串字面量**里的 `G*`（若有）。改完务必 `git diff` 通读一遍，
> 确认没有把用户可见文案或正则里的字符改坏。

**验收**：`.py` 中 `grep -rnoE '\bG[0-9]{1,2}\b'` 为空；测试全绿；
`FIX-* ↔ G*` 映射已记入本文件（即本节）。

---

## 5. 任务定义：什么叫"与形式建模匹配"

每条不变量 `G_i` 在系统里可能处于三态之一，开发动作不同：

| 状态 | 判据 | 开发动作 |
|---|---|---|
| **已闭** | 有实现点 **且** 有测试对该实现做**反向验证** | 补进覆盖矩阵，不动 |
| **半闭** | 有实现，但测试只覆盖部分分支；或反之 | 补测试 / 补实现缺口分支 |
| **空白** | 无实现载体（尤其 `L_gov` / `L_exec` 实体） | 需新增实体 + 不变量 + 测试 |

**这类项目的既有惯例（必须遵守）**：
> 凡是新增/修改一条守卫（不变量），**必须自测"它会失败"**——
> 注入反例 → 守卫必须报红。只验证"绿"不算数（历史上曾有守卫改坏还一路绿灯）。
> 参照 `tests/test_smoke.py` 的「去敏守卫」与 `tests/test_reorg_imports.py` 的反向对照做法。

**三份交付物**：
1. **覆盖矩阵**（新增 `docs/invariant-coverage.md`）——30 行，逐步闭合；
2. **实现**——按阶段补齐空白（见 §7）；
3. **测试**——每条新增不变量配至少一个正向 + 一个反向用例。

---

## 6. 第一步（Step 0）：产出覆盖矩阵，**不写业务代码**

> ⛔ **前置门**：若对方窗口有自己的产出，**必须先完成 §0.1 的对齐阶段**并产出
> `docs/merge-plan.md`、经确认后，才进入本节。否则先对齐。

这是开工的第一个动作，也是唯一必须先做的事。理由：动手改代码前，
必须知道 30 条不变量里**哪些已经成立、哪些是纸面**，否则会重复实现或改错地方。

### 6.1 产出文件（**骨架已生成，直接填空**）

`docs/invariant-coverage.md` **已就位**：`G1–G30` 共 30 行的「命题」列已按建模文档填好，
其余四列（实现点 / 测试 / 状态 / 缺口动作）全部是 `_(待填)_`，**由执行者打开实际文件逐格核对后填写**。

表头（固定，勿改列）：

```
| 不变量 | 命题（一句话） | 实现点 file::symbol | 测试 file::case | 状态 | 缺口动作 |
```

### 6.2 填写规则（硬性）

- **实现点 / 测试** 必须是**可核对的具体路径与符号名**（`application/gates.py::Gate.check`），
  不许写"在 gates 里"这种模糊描述，**不许凭记忆填**（必须实际打开文件确认）。
- **状态** 只能取 `已闭 / 半闭 / 空白` 三值。
- **半闭** 必须写清"缺哪半边"（缺分支？缺反向用例？缺整个实体？）。
- **空白** 不许留空，必须写出一条**具体的缺口动作**（如"新增 `Receivable` 状态机 + G17 单调性测试"）。
- `G1–G13` 与 `G14–G30` 分两张表，或一张表加"层"列（`L_proj` / `L_gov` / `L_exec` / 接缝）。

### 6.3 填表起点（别从零猜）

- `G1–G13`：**从已有测试倒推**。读 `tests/test_trusted_execution.py` 的 32 个用例名，
  逐个对到文档 §10 的命题上；对不上的不变量就是缺口。
- `G14–G30`：**从代码倒推**。对每条不变量，在 `domain/` `application/` `adapters/` `sync/`
  里 grep 对应实体的关键词（`Receivable` `BOM` `WorkOrder` `Inventory` `GoodsReceipt`…）。
  预判：**绝大多数是"空白"**（已知 `domain/order_to_cash.py` 内搜不到
  `Receivable/Invoice/CashReceipt`，治理层与执行层实体基本不存在）。

### 6.4 验收（Step 0 的 Definition of Done）

1. `docs/invariant-coverage.md` 中 **G1–G30 共 30 行齐**，无一空行；
2. 至少有 **5 条**能点名到现成测试用例（证明矩阵不是空转）；
3. 所有"空白"项都写出了**具体的缺口动作**（不是"待定"）；
4. §4 的编号冲突已按方案 ① 或 ② 处置完毕，并有映射记录；
5. **不改动任何业务代码**（本步只产出矩阵 + 编号统一）。

---

## 7. 然后：按四阶段推进（**先下后上**，承载后端**先本地**）

> **已定排序（用户 2026-10-02 拍板）**：**先向下（制造执行）→ 再向上（O2C 财务链）→ 组合/产能 → 对账/度量**。
> **已定承载后端**：**先做纯本地控制平面**（`domain/order_to_cash.py` 风格），
> 理由 —— 本地是最总兜底方案，**必须先在本地把不变量跑通**，再决定落到 SeaTable / 禅道 / 其他。
> 原则不变：先跑通**一条线**，再扩展为**一个面**。

### 阶段一（向下）· 制造执行最小闭环 ✅ 已闭合（2026-10-03 批次 1）

| 要接的一段 | 新增实体 | 不变量 |
|---|---|---|
| BOM → PR → PO → GR → 工单 | `Item` `BOM` `PR` `PO` `GR` `WorkOrder` | **G21 G22 G23** |

**交付判据**：一个项目能从"生产立项"一路看到"工单开工"，每步**带 ID、带 `as_of`**；
`BOM` 无环且用量守恒（G21）、付款必有到货验收（G23）、开工必齐套或留痕放行（G22）。

**落点**：新建 `domain/manufacturing.py`（或按 `decision_support/` 风格建
`domain/manufacturing/` 包），复用 `domain/order_to_cash.py` 已有的
**对象 / 状态 / 证据 / 审批 / 转移**五件套范式（不要另造一套）。

> ★ **实际落点与本文的差异（2026-10-03 批次 1，commit `e9ab364`）**：
> 实建在 **`application/exec_plane/`**（`schema` / `store` / `bom` / `procurement` /
> `work_order` / `service`），**不是** `domain/manufacturing/`。两点原因，均为实测所得：
>
>   1. `domain/` 是**第一期的业务对象层**（`order_to_cash.py` 那套），而 `L_exec`
>      要挂的是**第二期的授权闸门与 `DataService`**（写入必须与二期同构）。
>      放 `application/` 下才能直接 `from .. import authorization`，不必跨层反向依赖。
>   2. 本文要求的「复用五件套」**事实上被 `project_brain` 更彻底地满足了**：
>      `evidence_ledger`（到货/验收判定）、`store.LocalMemoryAdapter`（append-only + 折叠 + 乐观锁）、
>      `actions.R_PARTIAL_NOT_KITTING`（齐套口径）、`dataservice`（写入闸门）**一行没改就复用**。
>      没有另造一套 —— 但复用的是**二期**那套，不是一期那套。
>
> **`as_of` 已落实**：本层所有**判定结论**都带 `as_of`
> （`ExecPlane.validate_bom()` / `explode()` / `can_pay()` /
> 开工结论 `evidence["__verdict__"]["G22"]["as_of"]`），
> 取值是「这条结论读了哪些行、那些行里最新的时间戳」——
> 取不到就是空串，**不拿「现在」冒充数据时间**（`tests/test_exec_plane.py` 有专门用例）。
>
> **未做**：`PR → PO` 的转换动作（`请购单` 实体与状态已建，但「PR 转 PO」未接线）；
> 排产侧对接（`decision_support` 按 `plan_id` 的对接留待阶段三）。

> 排产**不重造**：工序/资源/日历/关键路径直接消费 `application/decision_support/`
> （`PlanningSnapshot` / `resource_feasible`，按 `plan_id` 对接）。本阶段只做**排产之后**的实物执行。

### 阶段二（向上）· O2C 财务链最小闭环

| 要接的一段 | 新增实体 | 不变量 |
|---|---|---|
| 合同 → 订单 → 发货 → 开票 → 应收 → 回款 | `Invoice` `Receivable` `CashReceipt` (+ `RevenueRecognition`) | **G16 G17** |

**交付判据**：一个项目能从"签约"一路看到"回款"，每步**带 ID、带 `as_of`**。

**关键约束**（`domain-model.md` §2.2）：**"验收"是"开票/收款"的前置条件**（本业务即"验收回款"），
不是常见的"发货即开票"；金额链**单调**：`Σ回款 ≤ 应收 ≤ 开票 ≤ 合同额`（按币种，G17）。
`Receivable` 状态机 `{open, partially_settled, settled, overdue, written_off}` 挂进
`domain/order_to_cash.py`（该文件已有 `MAIN_CHAIN` 与转移/严重性机制，天然同构）。

### 阶段三 · 成本归集 + 组合与产能

`Portfolio` + `align`（项目挂战略）· `ResourcePool` + `capacity_commitment`（多项目资源约束）·
成本条目 → 项目/工单/PO 归集 → 不变量 **G14 G15 G18 G28**。

### 阶段四 · 三账对账 + 度量贯通

`reconcile()`（项目账/执行账/财务账）· 度量树 KPI 归集 · 决策记录
→ 不变量 **G19 G20 G26 G27 G29 G30**。

### 阶段一/二共用：后端隔离要求

因为**先本地**，两阶段的实体必须先能在**纯本地**闭环（落 `data/` 下的 JSON/CSV，
schema 与 `adapters/local.py` 对齐）。**只有本地不变量全绿之后**，才讨论把
`Receivable`/`BOM`/`WorkOrder` 迁到 SeaTable 或禅道——届时只需新增一个 adapter 映射，
**不变量与领域模型不应发生任何改动**（这也是"本地先行"能成立的原因）。

### 跨阶段必须同时遵守的两条接缝协议

- **指令-回执（G26）**：任何下行指令必须带 `cmd_id` + 幂等键并等 `Ack`；**无 Ack = 未执行**。
  这是把既有「出版门禁 fail-closed」从"写库"推广到"跨层指挥"。
- **三账可对账（G27）**：项目账/执行账/财务账**允许差异，但差异必须可见且挂原因码**，不得静默。

---

## 8. 硬规则（铁律，违反即返工）

### 8.1 ★ 真实业务主体名一律不得进仓库（PUBLIC 仓库）

任何真实主体名不得出现在被跟踪文件里。分层配置是**唯一**允许的写法：

```
代码里的泛化默认值（示例科技 / 示例集团 / 客户A… / 示例供应商A…）
        ↑ 被覆盖
本地 config.yaml → entities.*       （.gitignore 已排除；这是 10 张真实表的唯一存放处）
```

- 新增真实名 → **改 `config.yaml`，不改代码**（改代码就会被推上 GitHub）。
- **写测试必须自备值**（如 `wm.OWN_COMPANY_PAT = re.compile(r"示例科技|…")`），
  不得依赖本机 `config.yaml`，否则换台机器就挂。
- 新建文档（本任务会产出 `invariant-coverage.md` 等）一律用占位符 `⟨·⟩`，
  提交前跑一遍 `tests/test_smoke.py` 的「去敏守卫」（扫**全仓库**、「手机号 /
  未泛化企业名 / 真值表真名」三路检测 + 一条 `fp_context` 反绕过自检）。
  **顺序是硬纪律：先 `git add`，再跑守卫** —— 守卫扫的是 `git ls-files`，
  没 `add` 的文件根本不在扫描面内，跑出来是假绿。

### 8.1.1 主体名撞上通用词组（假阳性）怎么处置

中文没有词边界，2 字主体名会撞上更长通用词组的**内部**（如某 2 字简称恰好
落在「自动□□」这类词里）。实测十余句正常技术中文里大半会误报。

处置顺序（**前三档都不动真值表**）：

| # | 手段 | 代价 | 适用 |
|---|---|---|---|
| ① | **改文档措辞**避开那个词组 | 零配置、零代价 | **首选**，多数情况够用 |
| ② | `entities.fp_context`：登记「包裹片段的更长词组」 | 需维护一条配置 | 词组是本项目绕不开的固定术语 |
| ③ | 把 needle 加进 `entities.generic_ok` | **整条真值被删** | 仅当它根本不是主体名 |
| ④ | ~~把阈值提到 `len >= 3`~~ | **已实测推翻** | **禁止**：会让一批真实业务简称句全部漏扫 |

`fp_context` 的语义是**逐次出现**判定，不是整条白名单：

```
entities:
  fp_context:
    <2字needle>: [<包裹它的更长通用词组>, ...]
```

守卫自带反绕过自检（`_validate_fp_context`），下列写法会被守卫**自己**报红：

- 包裹片段**不严格长于** needle（如 `联动: [联动]`）→ 等于整条放行；
- 包裹片段**不含**该 needle → 永不生效的死配置（却让人以为已处置）；
- 包裹片段为空列表 → 同上；
- needle 不在真值表里 → 在豁免一个不存在的东西。

守卫的 FAIL 消息带 `文件:行 「needle」← …上下文…`，是真名还是偶然撞词一眼可判。

### 8.2 改了工作区 ≠ 改了历史

历史 blob 必须用 `git filter-repo` 单独处理，且**替换清单与验证清单必须来自不同来源**，
否则验证会自洽假阴性。本次开发若涉及新增文件，注意不要引入需清洗的内容。

### 8.3 域目录重组后，**函数内延迟导入**是唯一会漏的一类

`import pkg` ≠ `from pkg import mod`：`cockpit/`、`pipeline/` **没有 `__init__.py`**，
`import cockpit` 会解析成**空命名空间包**（导入"成功"、属性全无，且会被外层 `try` 吞掉，
表现为静默降级）。

- 守卫：`tests/test_reorg_imports.py`（[A] 关键调用点按限定名可导入且属性齐全；
  [B] 全仓静态扫描"模块已入子目录却按顶层名导入"）。
- 改扫描器后**必须做反向对照**（在基线 HEAD 上跑必须判红并报全 6 处），否则绿是假的。

### 8.4 跑测试的口径

- **永远在仓库副本里跑**，不要直接在仓库根跑：
  `cockpit/cockpit.py::_load_pw()` 在 `config.yaml` 缺 `cockpit:` 段时会**追加写入**；
  `tests/test_smoke.py` 在无 `config.yaml` 时会**连带触发生成**——生成的那个只有口令、
  **没有 `entities`**，而 `config.yaml → entities.*` 是**单点真值表，误删即永久丢失**
  （已备份 `backups/config.yaml.bak-20260926-1915-with-entities`）。
- 隔离跑测标准做法：`git archive HEAD | tar -x` 建基线副本 + `sitecustomize.py`
  覆写 `socket.connect` / `getaddrinfo` 强制禁网（**必须设 `PYTHONPATH` 才生效**）。
- **脚本式测试会静默不跑**：`tests/` 里有若干脚本式测试（无 `TestCase`）带 `__main__` 守卫，
  `unittest discover` 只 import 不执行。改动脚本式测试后**必须直跑该文件**验返回码。

### 8.5 已知的文档计数漂移（别被误导）

README 写"361 项"、SKILL 写"172 项"，实测 `test_smoke` 打印 126 项、
`unittest discover` 403 项。**以实跑为准**。

---

## 9. 完成判据（本次开发的 Definition of Done）

1. `docs/invariant-coverage.md` 30 行全部判定为 **已闭**；未闭的挂明确 issue 编号与缺口动作；
2. §4 的 `G*` 编号冲突已消除，全仓不再存在两套含义不同的 `G*`；
3. 新增实现**每条不变量都配了反向测试**（注入反例必须报红，已实测）；
4. `tests/test_smoke.py` + `unittest discover -s tests` **全绿**，且在**副本里**跑过；
5. 涉及治理/执行面新表时，表结构与不变量一并落文档（`docs/` 下，占位符脱敏）；
6. 去敏守卫通过（无手机号 / 无未泛化企业名 / 无真值表真名 / `fp_context` 配置本身合法）。

---

## 10. 已拍板的决策（2026-10-02，施工时**不再询问**，直接照此执行）

| # | 决策 | **已定** | 落地含义 |
|---|---|---|---|
| **1** | 阶段一先向上还是先向下？ | **先下后上** | **阶段一 = 制造执行最小闭环**（BOM → PR/PO → GR → 工单，G21 G22 G23），跑通后才做阶段二 O2C 财务链。详见 §7。 |
| **2** | `G*` 编号统一方案？ | **方案 ①** | 文档 `G1–G30` = canonical；代码/测试注释 `G*` → `FIX-*`（43 处 / 9 文件），并留映射表。详见 §4。 |
| **3** | 新实体承载后端？ | **先纯本地控制平面** | 先在本地（`domain/order_to_cash.py` 风格 + `data/` 落盘）**把不变量跑通**——本地是最总兜底方案。**本地全绿之后**才讨论迁 SeaTable / 禅道。详见 §7 末「后端隔离要求」。 |

> 补充依据（`domain-model.md` §7）：`L_gov` 归 BI/财务系统、`L_exec` 归 ERP/MES/WMS
> （没有就用 SeaTable 承载）、`L_proj` 保持现状。
> **本模型的角色是"统一语义层"，不替代任何一层系统。**

### 10.1 施工方仍需自行判定的两处（不阻塞开工，但改完要记一笔）

1. `FIX-5`（`latest` 按时间取）—— 并入 §7.2 注作为**操作约定**，还是独立立不变量？（建议前者，见 §4.3）
2. `FIX-9`（案件空壳自愈）—— 建议在 `formal-runtime-model.md` 新增 **G31 案件完整性**（见 §4.3）

---

## 附：一键自检命令（开工前跑一遍）

```bash
PY="C:/Users/11430/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
cd C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0

git log -1 --format='%h %ad %s' --date=iso    # 基线
git status --short                            # 确认工作区状态（见 §2）

# G 编号双轨现状（§4）
grep -rnE '\bG(1[0-9]|[1-9]|2[0-9]|30)\b' --include=*.py --include=*.md . | head -40

# 治理/执行面实体是否已存在（§3）
grep -rlnE 'Receivable|Invoice|CashReceipt|WorkOrder|GoodsReceipt' --include=*.py . || echo "→ 空白，符合预期"

# 在副本里跑测试（§8.4）
rm -rf /tmp/base && mkdir -p /tmp/base && git archive HEAD | tar -x -C /tmp/base
cd /tmp/base && "$PY" tests/test_smoke.py
```
