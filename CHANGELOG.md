# 变更日志

本文件面向**使用者**，记录每个发布版本新增了什么、修了什么、有什么破坏性变化。
面向维护者的详细设计决策、踩坑过程与实测数据见 [`references/changelog.md`](references/changelog.md)。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [v2.1.2] — 2026-10-01

> **主题**：把「第二大脑」的存储底座从「SeaTable 专用」变成**可插拔的六个后端**。
> 本版是 v2.1.0 多底座改造的**收官**（路线图 4/5），也是**飞书任务（Task）底座**的上线版。

### ✨ 新增 · 飞书任务（Task）底座（`backend: feishu_task`）

飞书在项目管理上是**两个产品、两套 API**：多维表格（Base，v2.1.1 已接）与**任务（Task）**。
本版接的是后者：

| 中立概念 | 飞书任务里是什么 |
|---|---|
| 表 `table` | **任务清单（tasklist）**，可用清单名或 guid |
| 行 `row` | **任务（task）** |
| `__row_id__` | 任务的 **guid** |
| 列 `column` | 任务内置属性（摘要/描述/完成状态/开始/截止/负责人/关注人/里程碑…）+ 该清单的**自定义字段** |

**为什么必须单独一个后端**：两者除都叫「飞书」外没有任何共同点 —— 没有共同的表概念、
没有共同的 id 空间，连「写一个字段」的语义都不同（Base 是整条 record 覆盖，
Task 是 **`update_fields` 白名单**）。硬塞进一个类只会造出一个到处 `if` 的怪物。

**四轮真实读写探测**（全程只碰自建的一次性清单、反序清理、基线比对，**四轮零残留**），
约 18 条协议事实，其中 **6 条与直觉相反**：

1. ★★ **`tasks.patch` 的 `update_fields` 是必填的、固定 14 项白名单**，
   且 **`members` 不在其中** → 改负责人/关注人**只能**走 `members add/remove`。
2. ★★ **列了就必须给非空值**：列了 `summary` 而 body 里没有 → `1470400`，
   **整条调用失败**（不是「忽略那一项」）。
3. ★★ **给了却没列 → 静默忽略**：body 塞 `description`、`update_fields` 只写 `summary`，
   调用**成功**且 `description` 原文不动 —— **只看返回值发现不了**。
4. ★★ **`tasklists.tasks` 是摘要投影**：列表项**只有 7 个键**（连 `description` 都没有），
   而 `tasks.get` 返回 **28 个键** → **拿列表当行用，会让所有详情列看起来「本来就是空的」**。
5. ★★ **`tasks.get` 对不存在的 guid 抛 `1470404`**，不是回空 `data.task`。
6. ★★ **`tasks.create` 不带 `tasklists` 照样成功** → 任务成为**孤儿**，任何清单都看不到。

其余要点：`--yes` 是另一条命令分界线（4 个 high-risk 命令不加时服务端**什么都没做**、`rc=10`）；
**单选写入必须给选项 guid**，给名字报 `1470400`，而读回来也是 guid（**写名/读 guid 不对称**）；
完成状态不是布尔位（判据 `completed_at != "0"`，取消完成 = patch 成字符串 `"0"`）；
**创建类接口回的是包装键**（`data.task.guid` 等，不是 `data.guid`）。

**本机真实数据**：清单「项目」（99 条任务）与「付款」（70 条任务）；「项目」有 6 个自定义字段
（优先级 / 项目预算 / 工时 / 状态 / 配合人员 / 任务完成度）与 7 个分组。

### ✨ 新增 · 禅道全量摸透（第二轮实测）

`adapters/zentao.py` 实体 **10 → 16**，补 **17 条**实测事实（19 资源 × 88 路径可用性矩阵）。

### 🐞 修复

- **🔴 `get_row()` 没翻译 `1470404`**：真实服务端对不存在的 guid **抛错**而不是回空 `data.task`，
  而离线 mock 恰好回的是空 `data` —— **mock 掩盖了真实行为**。契约要求返回 `None`，
  已修 + 补 3 项回归测试。**这个缺陷是真实验证（不是单元测试）抓到的。**
- **🔴 两处「关注人」写入分支完全没有测试**：`append_row` 与 `_to_body` 各有一份**独立**的
  成员处理，把 `@follower` 偷偷改成写进 `patch`，**测试依然全绿**。已补 4 项测试。
  **这个盲区是反向验证抓到的。**

### 🔧 内部 · 测试方法学（这一版真正值钱的部分）

本版对「怎么证明测试有效」做了三件事，因为**「测试全绿」不等于「守卫有效」**：

1. **反向验证（revert-verify）成为常规手段**：把每处守卫**逐个临时退回**，确认对应测试
   **真的变红**，再恢复并做**字节级 md5 校验**。飞书任务 **14/14** 守卫被证明
   「退回即断言失败」，恢复后源文件逐字节一致。
2. **新增真实服务端验证器 `tools/verify_feishu_task.py`**（只读 **24 项** / 写路径 **56 项**），
   在一次性清单里跑完整读写路径后反序清理并比对基线。
3. **修正了两条测试方法学错误**：
   - 反向验证脚本用「模块.方法名」调用类里的测试 → unittest 统一包成 `_FailedTest`(ERROR)，
     **12/12 被误判成「代码坏得加载不了」**，一次有效验证都没做到。现改为用 `ast`
     自动解析「方法名 → 所属类」，解析不到直接报错退出。
   - 「只认 `FAIL` 不认 `ERROR`」这条纪律**本身是错的**：测试体里**未被捕获的异常**
     同样报 `ERROR`。筛掉它等于人为制造假阴性。现在只把 `_FailedTest` 当作无效。

### ⚠️ 已知局限（诚实记录）

- **飞书任务刻意不声明三项能力**：`CAP_LINK` / `CAP_LINK_READ`（任务的 `dependencies`
  是**任务↔任务**不是表↔表，实测**只读**，且在本机 99 条任务里**全为空** →
  「读出来是空」与「这字段根本不生效」**无法区分**，硬做等于猜）、`CAP_BATCH_WRITE`、
  `CAP_IDEMPOTENT` / `CAP_OPTIMISTIC_LOCK`。`link()` / `list_linked()` **如实抛 `Unsupported`**。
- **`multi_select` 与 `datetime` 两种自定义字段的值键未实测**（按同构推断实现）；
  `text` / `number` / `single_select` / `member` 四种已实测。
- **成员写入不是并发安全的**：`members add` 是追加、`remove` 只摘指定 `(id, role)`，
  「改负责人」实际是**读-改-写**，并发改同一任务可能丢失更新。
- **飞书项目（Meego / Meegle）尚未打通**：实测路由全在（`plugin_token` 401、
  `user_plugin_token` 401、`refresh_token` 400、`work_item/filter` 500），
  但 **lark-cli 的 OAuth token 打不了它** —— 要 `plugin_id` + `plugin_secret`，
  须**空间管理员**在飞书项目开放平台建插件后发布。官方 CLI 是 `meego-cli`。
- **金蝶未开工**（路线图 5 项中的最后一项）。

### 🧪 测试

`742 → 828` 全绿（其中飞书任务契约 **86 项**，全离线 mock）。
真实服务端：只读 **24/24**、写路径 **56/56**；反向验证 **14/14**。

---

## [v2.1.1] — 2026-09-30

> 多底座改造**第 1～3 期**：一次接入三个真后端（飞书多维表格 / 简道云 / 禅道）。
> 从这一版起，「第二大脑」不再绑定 SeaTable。

### ✨ 新增 · 飞书多维表格（`backend: feishu`）

**全部协议事实来自 4 轮只读探测**（不照文档猜），含：`PUT /rows/` 的**整条 record 覆盖**语义、
关联字段双向是**双列**、字段类型字符串**大小写混用**（`link`/`LINK`、`date`/`DATE` 并存）等。
真实 Base **只读**端到端验证通过，含「**零写请求**」取证。

### ✨ 新增 · 简道云（`backend: jiandaoyun`）

简道云 API v5。⚠️ **这是唯一一个协议细节全部来自官方文档、没有实测环境的后端** ——
`adapters/jiandaoyun.py` 里逐条标注了 ⚠️。接入真实企业时**第一件事**是跑
`python tools/verify_jiandaoyun.py --read-only` 逐条核对那些推断，尤其是
**「日期按 UTC 存」**与**「格式不合法的值会被静默写成空」**两条。

### ✨ 新增 · 禅道（`backend: zentao`）

禅道 REST v2。**这是唯一一个「实体与字段系统固定、不能建表」的底座**，所以
`adapters/schema.py` 的 `BACKEND_TYPES` **刻意不给它类型表** —— 硬凑一张会让人误以为能建列；
`ensure_table()` 老实抛 `Unsupported`，「字段类型 ↔ 中立类型」的读向对应改由
`adapters/zentao.py::neutral_type()` 提供。覆盖 PM 实体：项目集/项目/执行/任务/产品/需求/
Bug/测试用例/测试单/用户（**采购、发货、库存那类表禅道承载不了**，`_resolve()` 会明确拒绝）。

⚠️ **实测副作用**：写「项目」会顺手建一个**同名产品**（删项目不删产品），而且那个名字
**用过一次就不可回收**（再建同名项目会被拒，报的还是「『产品名称』已经有…」）——
**批量建项目前必须用一次性名字**。

### 🐞 修复

- **修掉 `factory` 的静默退回**：未知 `backend` 值此前会**静默**返回 local（把「写线上」
  变成「写本地 CSV」且读回照样通过）→ 现在默认打 `[warn]`，**写入路径**（`strict=True`）
  直接抛错拒绝退回。

### 🧪 测试

`466 → 742` 全绿，新增飞书 / 简道云 / 禅道三套契约测试（其中禅道 1188 行）。

---

## [v2.1.0] — 2026-09-30

> **多底座改造 · 第 0 期地基**。业主原话：「把这套生产经理的第二大脑整体也兼容……
> 适配更多不同的底座，比如 partdb 简道云 飞书多维表格 禅道 金蝶等」→「我肯定是要全套支持」。
>
> **本期不新增后端**，只把「接一个新后端」的代价从「**改 6 处硬编码 + 碰 4 个私有属性**」
> 降到「**登记一行**」。

### ✨ 新增 · 三层适配器契约（`adapters/base.py`）

| 层 | 内容 | 约束 |
|---|---|---|
| ① 必修方法（**8 个**） | `auth` / `list_rows` / `get_metadata` / `append_row` / `update_row` / `delete_rows` / `link` / `list_linked` | `@abstractmethod`，ABC 实例化时即强制 |
| ② 能力声明 | `capabilities()` 或类属性 `CAPS` | **声明式、无副作用、建连前可读** |
| ③ 可选方法（**9 个**） | `close` / `query` / `get_row` / `table_exists` / `append_rows` / `link_append` / `link_one_way` / `ensure_table` / `version_of` | 有通用兜底；**拿不到正确答案时抛 `Unsupported`，不许静默返回空** |

**12 个能力位**：`read` · `write` · `update` · `delete` · `link` · `link_read` ·
`batch_write` · `schema_manage` · `idempotent` · `optimistic_lock` · `query_pushdown` ·
`server_row_id`。`link` / `link_read` **必须显式声明** —— `link()` 完全可能存在却是个空实现，
靠方法存在性推断会直接踩坑。

### ✨ 新增 · 后端注册表（`adapters/factory.py`）

新增一个后端 = 「实现 8 个必修方法 + 在 `schema.BACKEND_TYPES` 加类型映射 +
`register_backend()` 登记一行 + 加契约测试」，**不用改工厂一行代码**。

### 🐞 修复 · 三处历史静默失败

1. **🔴 `SeaTableAdapter.list_linked` 的空桩**（`return []`）—— 命令成功、返回空、**无报错**。
   根因：`GET /links/` = 405、`metadata` 顶层**无 `links` 键** → 旧 `_resolve_link_id`
   是**从未走通的死代码**。
2. **🔴 `wx/wxmatch.py` 写入路径在本机 100% 死掉** —— 旧实现只认命名 Base `business`，
   而本机用 `bases:` 形态 → 必然抛 RuntimeException。
3. **🔴 `factory` 对未知 `backend` 值静默返回 local**（见 v2.1.1 修复条目）。

另新增 `link_append()` / `link_one_way()`，让 `crm_dispatch` 不再碰私有属性；
类型归一中立化 + `link_id_for()` 方向敏感。

### 🧪 测试

`403 → 466` 全绿 + 真实 Base **只读**端到端验证通过（含「零写请求」取证）。
4 轮只读探测**推翻多项旧假设**：`POST /links/` 契约、双向关联是**双列**、
`schema.LINKS` 里的 `PlAl` / `RsAl` **根本不存在**。

---

## [v2.0.0] — 2026-09-26

> **断代原因**：写库权限的归属变了。
>
> 此前「匹配算法判定高置信」即可直接写真实业务表；v2.0 起，**授权从置信度里彻底摘出**，只有人能给出写入许可。同时二期「项目第二大脑」与三期「项目决策辅助」落地，44 个顶层脚本按功能域重组。

**本版覆盖 v1.8.0 之后的全部工作**（含内部文档曾标注为 v1.9.0 的那批能力，它们从未单独发版）。

### ✨ 新增 · 可信执行层 v1（`application/`）

一切「机器自己觉得对」与「人同意」的分界都在这一层。

- **统一写库授权**（`application/authorization.py`）—— 任何 production / tasks 业务表写入必须携带 `WriteGrant`，来源只有两个：已通过的审批，或人亲手写的授权文件。该模块**刻意不接收任何置信度字段**，高/中/低一视同仁。无授权 → `blocked`（旧行为是返回 `candidate`，语义上仍暗示「等 approve 就能写」）。
- **统一写入服务**（`application/dataservice.py`）—— 所有 SeaTable 写入收口到一个入口；写完**立刻读回验证**关键字段（中文列名不一致 = 写入失败）；支持**幂等键**（同键重写复用，台账可追溯）；每次自动写记 `data/write_ledger.csv`。
- **副作用分级契约**（`application/contracts.py`）—— 每个工作流步骤必须声明 `read_only` / `local_append` / `online_write` / `destructive` / `publish`，执行器据此裁决闸门。`preview` 模式拦截全部 `online_write` 与 `publish`；`destructive` 永远额外要求显式 `--yes`。
- **DAG 执行器**（`application/runner.py`）—— 按 `depends_on` 拓扑排序执行，每步结果落 `data/runs/<run_id>/`，`final.json` 汇总；`--resume <run_id>` 时已成功步骤跳过、failed/blocked 重试。
- **发布门禁**（`application/gates.py`）—— 「账本里没证明过的，不许对外发布」。整次运行不得有 failed；`wechat_collect` / `wxmedia_ocr` / `wxmatch_scan` / `evening_write` 关键阶段不得 failed 或 blocked；关键阶段报出 `verify_failed` 计数同样视为关键失败。`skipped` **不算失败**（数据源不可用、无授权本就是正常状态）。

### ✨ 新增 · 统一工作流入口（`workflows/workflow.py`）

每日自动化从「Prompt 记 13 步」改为「跑一条命令」：

```bash
python workflows/workflow.py run daily   --mode preview|apply
python workflows/workflow.py run evening --mode preview|apply
python workflows/workflow.py run daily   --resume <run_id>
python workflows/workflow.py status|list|verify|gate|note
```

- **`daily`（09:00 轻同步）** 11 步：同步 Base / PartDB → 微信增量 → 24h 摘要 → 消息核对 → 异常检测 → 风险预测 + 复盘 → 闭环台账镜像 CRM → 站会摘要 → 重建驾驶舱。
- **`evening`（19:00 全量重扫）** 11 步：微信采集 → **图片解密 + OCR** → 消息核对 → **授权写入及读回** → 业务表 + 库存快照 → 异常 + 风险 + 复盘 → 重建驾驶舱 → **带门禁发布**。

### ✨ 新增 · 二期「单项目第二大脑」（`project_brain.py`）

把单个项目从「一堆记录」变成有 ID、有状态、有证据、有下一步的活体。

- 命令：`grant` / `doctor` / `ingest` / `replay` / `context` / `query`（项目五问）/ `snapshot` / `actions` / `transition` / `close` / `rollover` / `reminders` / `evidence` / `verify-observation` / `correct`。
- **不带 `--mode apply` 一律是 preview**：所有写入退化成 candidate，一行都不落盘。
- **关闭行动必须带验收证据**；**纠正走追加，不覆盖历史**；跨天滚动保留原期限。
- 契约：[`docs/contracts/project-brain-v1.md`](docs/contracts/project-brain-v1.md)。

### ✨ 新增 · 三期「项目决策辅助」（`decision_support.py`）

**只读、离线**。四个命令对应四个问题：`analyze`（这活什么时候完？卡在哪？）· `compare`（换个条件能不能好一点？要谁点头？）· `delay`（A 晚 8 小时会连累谁？）· `explain`（凭什么算成这天？）。另有 `backtest`（预测复盘，含**防未来信息审计**）、`align`（二期/三期契约对齐检查）、`demo`。

- `compare` 会明确列出「执行意图（**未授权、未执行**）」，绝不越权；
- 退出码：0 正常 / 1 输入错误 / 2 排程不可用。
- 契约：[`docs/contracts/decision-support-v1.md`](docs/contracts/decision-support-v1.md) · 对齐：[`docs/contracts/alignment-p2-p3-v1.md`](docs/contracts/alignment-p2-p3-v1.md)。

### ✨ 新增 · 微信图片离线解密 + OCR（`wx/wxmedia.py`）

> ⚠️ **本条推翻此前文档结论**：「图片拿不到字节 / 付款截图无法 OCR」**作废**。根因是旧排查把 `xwechat_files` 父目录当成了账号目录。

- 支持 `.dat` 三种加密格式 **V0 / V1 / V2**；V2 密钥**离线派生**，无需扫进程内存；结构自检用总长恒等式 `file_size == 15 + aes_size + 16 + xor_size`。
- 自研 `wxgf` 格式转码（调 `VoipEngine.dll`）。
- 端到端实测：扫 122 会话 / 解密 80 张 / 命中业务关键词 18 张，其中读出一张建行电子回执全文。

### ✨ 新增 · 供应商群自动纳入（`wx/wx_watchlist.py`）

从 SeaTable 的供应商/客户实体自动生成归一化关键词表（三层剥离：城市前缀 → 公司后缀 → 行业通用词），**并进** `watch_groups`。以后**群名带上供应商名就自动进监控**，无需手改配置。首次实测 43 实体 → 49 匹配词。

### ✨ 新增 · 其他

- **CRM 赢单转化引擎**（`domain/won_deal.py`）—— 线索 → 商机落地 → 转生产立项闭环。
- **CRM 自动录入引擎**（`domain/crm_dispatch.py`）—— 自动写入 + 台账核对，驾驶舱支持批量操作。
- **业务闭环控制平面**（`domain/order_to_cash.py`）—— 12 类业务对象（CUS/LED/OPP/REQ/SOL/QUO/CTR/PRJ/MO/PO/SHP/AS）本地控制平面 + 状态机，离线可预演；`workflows/loop_sync.py` 幂等镜像到 CRM Base。
- **项目矩阵板块** —— 驾驶舱新增项目全表 / 生产计划全表 / 流程思维导图。
- **对内共享页**（`cockpit/build_share.py`）—— 给同事看的精简版视图。
- **飞书薪资表对账子技能**（`feishu-paytable-reconcile/`）。
- **甘特图三状态同轴** —— 实际进度 / 合同交期（实心菱形）/ 历史推算交期（空心虚线菱形）三者并存，tooltip 给出松紧差 `slack`。推算值**只读展示，绝不写回**。

### ♻️ 变更 · 仓库结构重组（破坏性）

44 个顶层脚本按功能域归组到 `domain/` `workflows/` `cockpit/` `wx/` `sync/` `tools/`。

**受影响**：所有 `python xxx.py` 调用方式改为 `python <域>/xxx.py`。

```diff
- python op.py list 项目表
+ python domain/op.py list 项目表

- python cockpit.py
+ python cockpit/cockpit.py

- python wxmatch.py scan
+ python wx/wxmatch.py scan
```

仓库根目录只保留 `project_brain.py` 与 `decision_support.py` 两个总入口。所有文档内引用已同步更新。

### 🐞 修复

- **`wecom_push.py` node 升级导致静默误判未授权** —— 新 node 版本下 `wecom-cli` shim 路径失效，`FileNotFoundError` 被误判成「未授权」而静默不推送。改为按 `.cmd` / `.exe` / `.ps1` / 无扩展名顺序探测真实入口，`cmd_check` 体检口径改为显示**实际生效**的 CLI 入口。
- **微信解密缓存并发撕裂** —— 修复并发场景下 `database disk image is malformed`。
- **`seatable_sync` 多 Base 配错库** —— 忽略 `bases` 子树，修复多 Base 配置下同步拉错库。
- **驾驶舱直连取数两个静默错误** —— 2026-09-14 修复。
- **整理后两处脚本无法直接执行**（本版发布前发现）—— `domain/order_to_cash.py` 与 `workflows/loop_sync.py` 缺仓库根路径引导，直接执行报 `ModuleNotFoundError`。已补 `sys.path` 引导。文档里写的命令现在真的能跑。

### 🔧 内部

- 测试：`tests/test_smoke.py` **172 项零依赖冒烟检查**，`python -m unittest discover -s tests` 共 **361 项**离线回归，全部不触碰线上数据。
- CI 在 Python 3.9 与 3.12 上跑零依赖冒烟 + 全量语法编译 + 敏感文件拦截（`config.yaml`、口令文件、驾驶舱 HTML 一律拒绝提交）。
- 自动化桥接层目录名改通配匹配 + `SKILL.md` 有效性校验，升级技能目录名后不再需要改桥接代码。

### 📄 文档

- **README 重写**为仓库门面：能力地图、可信执行架构、两条工作流 DAG、按场景折叠的详解。
- 新增本文件 `CHANGELOG.md`。
- `docs/usage-guide.html`（在线指南）与专家包快速命令卡中的过时裸脚本路径全部修正。

---

## 历史版本摘要

v1.8.0 及更早的发布说明见 [GitHub Releases](https://github.com/Darling5/seatable-production/releases)；
每个版本的完整设计决策与踩坑归档见 [`references/changelog.md`](references/changelog.md)。

| 版本 | 日期 | 一句话 |
|---|---|---|
| [v1.8.0](https://github.com/Darling5/seatable-production/releases/tag/v1.8.0) | 2026-09-03 | 微信事项双表分流与证据治理 |
| [v1.7.1](https://github.com/Darling5/seatable-production/releases/tag/v1.7.1) | 2026-09-03 | 预测自我学习闭环（台账 / 复盘 / 追问） |
| v1.7.0 | 2026-09-03 | 风险预测引擎 —— 第二大脑「想」层（合同倒排 · 供应商画像 · 缺料预警） |
| v1.6.2 | 2026-09-03 | 消息 ↔ SeaTable 核对引擎（只读对账） |
| v1.6.0 | 2026-09-03 | 上游原料行情（金 / 银 / 铜 / 锡 + 塑料） |
| v1.5.0 | 2026-09-02 | 得捷 / 贸泽代理商 API 自动查价 |
| v1.4.0 | 2026-09-01 | 第二大脑三件套（哨兵 · 异常 · 摘要）+ 微信情报双引擎（4.x 直读 / 3.x 回退） |
| v1.3.2 | 2026-08-31 | 驾驶舱发布器 + 混合架构定型（本地生成 · 库内分发） |
| v1.3.0 | 2026-08-29 | 一键全自动部署 `tools/deploy.py` |
