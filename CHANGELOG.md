# 变更日志

本文件面向**使用者**，记录每个发布版本新增了什么、修了什么、有什么破坏性变化。
面向维护者的详细设计决策、踩坑过程与实测数据见 [`references/changelog.md`](references/changelog.md)。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
