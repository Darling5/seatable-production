# 驾驶舱进阶 —— 在线模式 · 前端增强 · 架构收口 · 业务闭环

> 从 SKILL.md §11.6b~11.8 下沉。涉及 cockpit_server 在线模式、表格工具/甘特
> 交互/分析图表、v2.0 架构收口、客户到售后业务闭环控制平面时阅读本文件。

### 11.6b 驾驶舱在线模式（cockpit_server.py，消灭复制粘贴割裂感）

驾驶舱是静态快照 HTML，写操作原本要「复制→切 WorkBuddy→粘贴→等重刷」三段割裂。
**伴生服务器**把这条路压成一次点击：

```bash
python cockpit_server.py        # 默认 127.0.0.1:8801，占用自动顺延到 8810；自动开浏览器
```

- 页面加载时探测 `http://127.0.0.1:{8801..8803,8790}/api/ping`，在线则进入
  **在线直连模式**（顶栏徽标显示「⚡ 在线直连」）：
  - 微信情报台「确认选中」→ `POST /api/wx/approve` → 服务端逐条跑
    `wechat_intake.py approve`（确认/留痕逻辑与 CLI 完全同一份）→ 自动刷新快照
  - 「忽略选中」→ `POST /api/wx/ignore` 同上
  - 补录「一键复制」→ `POST /api/backfill` → 存 `data/补录回传.csv`，
    之后对话说「处理补录回传」即可确认写库
  - 「重新生成说明」→ `POST /api/refresh` → 重跑 cockpit.py 并 reload 页面
- 探测失败静默回退纯静态模式，行为与从前 100% 一致（离线打开/没起服务器都不受影响）
- 安全边界：只绑 127.0.0.1；事件编号白名单校验（`[A-Za-z0-9-]{3,40}`）防注入；
  写操作全部 subprocess 调既有 CLI，不复制任何业务逻辑；refresh 只重渲染本地快照

### 11.6c 驾驶舱表格工具 + 甘特交互 + 分析图表（cockpit.py 前端增强）

**通用表格工具（`initTableTools()`，render() 末尾、initPagination() 之前调用）**：
- `table[data-filter="1"]` → 自动在表格上方插入工具条：🔍 文本搜索（全行匹配）
  + 状态下拉（自动聚合「状态」列或 pill 列的取值与计数）+ 计数显示（如「显示 3 / 31 条」）
- `table[data-select="1"]` → 追加行勾选：表头全选（只全选**当前筛选可见**行）+ 行首复选框
  + 勾选后浮出批量操作栏：「导出选中 CSV」（BOM 带 utf-8 头，Excel 直开）/
  「复制名称列」（贴到 WorkBuddy 对话说「跟进这几个项目」）/「取消选择」
- 与分页联动：`initPagination` 已升级为基于可见行重算页数（`tbl._pgRedraw` 钩子），
  筛选后翻页正确；`_tfInit/_pgInit` 防重入（render() 重跑不重复插工具条）
- 已覆盖 18 张表：PW 项目/在制、C 应收、Q 维修、Sup 采购逾期/供应商、Inv 缺料/安全库存/
  库存核对、Rs 资源、Mkt 物料行情、Raw 原料、WXC 待确认/最近事件、WXM 核对、FC 倒排/缺料
- WXC 待确认表保留原有 `.wx-sel` 勾选语义（写库动作），只加 data-filter 搜索，不加通用勾选

**甘特图（`renderGantt` + `initGanttTools`）**：
- 状态筛选药丸：全部/进行中/逾期/已交付（带实时计数），+ 产品名/状态搜索框
- 悬停（含键盘 Tab 聚焦）条形 → 浮出详情卡：状态/立项/交期/计划工期/距交期/进度
- 点击行 → 高亮该行并**联动高亮 PW 项目清单表**对应行（`.hl` 类，`var(--selbg)`）
- 颜色改为 CSS 变量类（`.gantt-bar.run/.done/.overdue` → primary/green/red），深色模式自动适配

**新增分析图表**（纯表格区域补可视化，用既有 `bars()` 助手 + CSS 变量配色）：
- Sup：供应商准时率排行（最差优先，红<70/橙<90/绿≥90）
- C：应收账龄分布（已逾期/30/60/90 天金额）
- Inv：在产缺料缺口 Top（红=零确认库存，橙=部分缺口）
- Rs：资源负载排行（超载优先，红>100/橙≥70/绿正常/灰闲置）

前端约定：新增表格只要写 `data-filter="1"`（可选 `data-select="1"`）即可获得全套能力，
无需写任何 JS；新增图表用 `bars([{name,value,color}],{fmt})`，color 传 `var(--xx)` 自动适配深色。

### 11.7 v2.0 P0 架构收口（已完成）

- **写入与渲染解耦**：`op.py` 的 `apply-wizard / apply-text / intake` 默认**不再**自动
  刷新驾驶舱；需要旧行为时加 `--refresh` 或设 `SEATABLE_AUTO_REFRESH_COCKPIT=1`。
  驾驶舱刷新统一由 `workflow.py run daily` 的 cockpit 步骤或手动 `python cockpit.py` 负责。
- **口令移出自动播报**：新增 `passwords.py`（show / check / rotate），
  自动化提示词不再读取或播报任何口令明文；口令只在用户手动运行时当面展示。
- **统一幂等键**：`crm_dispatch.py` 的 lead/follow 支持 `--idem-key`（省略则自动用
  日期+客户+内容生成）；台账新增「幂等键」列，旧台账自动迁移补列。同键重写 →
  `idempotent_reuse`，不再重复写。自动化重跑同批微信消息不会再写两遍跟进。
- **DataService（`application/dataservice.py`）**：统一写入入口。所有 SeaTable 写入
  走 `DataService.write(WriteRequest(...))`：按路由策略收口（production/tasks → 候选，
  crm → 自动+台账）、写后**读回验证**（中文列名静默丢列会被判 verify_failed）、
  幂等键查重、统一台账 `data/write_ledger.csv`。新代码优先走这里，旧 CLI 行为不变。
  `crm_dispatch.py` 的 lead/follow 已实际接入（`write_verified()` 原语）：查重 → 幂等键 →
  读回验证 → 单向关联 → 台账，验证失败不入账不关联。
- **运行验收（`workflow.py verify latest`）**：每日自动化跑完后的一键核对——
  步骤状态全绿 + 成功步骤必须有新鲜产物（mtime 防旧文件充数，专治「静默跳步」）+
  final.json 零口令。退出码 0=通过。周一实战验收流程见 `docs/avatar-loop-v2.md` §9。

### 11.8 客户到售后业务闭环控制平面（v2.0 P3 前置，2026-09-13）

**`domain/order_to_cash.py` + `business_loop.py`**：12 类业务对象（CUS/LED/OPP/REQ/SOL/QUO/CTR/PRJ/MO/PO/SHP/AS）的
本地控制平面——对象台账、状态轨迹、证据链、审批记录四张 CSV（`data/business_loop/`），**不直接写 SeaTable**，
先离线贯通「微信来单→商机→需求→方案→报价→合同→立项→采购→生产→出货→验收→售后」。

```bash
python business_loop.py doctor                                   # 12 类对象 + 数据目录体检
python business_loop.py scenario --customer X --product Y        # 全链预览（纯只读）
python business_loop.py start --customer X --product Y --source-event EV --json   # 先预览
python business_loop.py start ... --yes                          # 确认后建客户/线索/商机（幂等）
python business_loop.py advance --root-id OPP-xxx --to quotation_confirming --reason 客户要求报价 --yes
python business_loop.py revise --root-id OPP-xxx --kind quote --summary V2 --reason 降价 --yes   # 版本递增旧版保留
python business_loop.py status --root-id OPP-xxx                 # 案件全貌（对象/轨迹/证据/审批）
python business_loop.py verify --root-id OPP-xxx                 # 闭环验收（ID/owner/next_action/轨迹/证据）
python business_loop.py cases --json                             # 在途案件清单（状态+覆盖率）
python business_loop.py report                                   # HTML 闭环看板
```

**边界铁律**：控制平面写本地 CSV；正式 production/tasks 写入仍走候选制（DataService
`approval_required`），CRM 自动写入（`auto_with_ledger`）不变。`advance --yes` 只是
登记审批轨迹，不代表已写业务表——立项真实写入走 `won_deal.py apply --yes`。

**每日摘要联动**：`daily_brief.py` 已带「🔗 业务闭环」段（在途案件数、状态、下一步命令），
读 `data/business_loop/objects.csv`，无数据自动跳过。

**交接建议全覆盖**：`handoff_suggestions` 对 14 主链 + 2 旁路状态都给出具体下一步命令
（如 `quotation_confirming → 方案版本 + pipeline BOM 成本 → 报价草稿`）。

**控制平面 → SeaTable 云端同步（`loop_sync.py`，2026-09-13 实测打通）**：把本地四表
幂等 upsert 到 CRM Base 的「业务对象台账/状态轨迹/证据链/审批记录」（表不存在时自动创建）。
默认 dry-run，`--yes` 才写云端；按主键（object_id/transition_id/event_id/approval_id）比对，
重复运行零重复。**云端只是台账镜像，业务动作仍在本地控制平面执行**——云端可直接用
SeaTable 视图/自动化做看板与提醒。

```bash
python loop_sync.py          # dry-run：打印将建表/新增/更新的行数
python loop_sync.py --yes    # 实际写入 CRM Base（幂等，可重复执行）
python loop_sync.py --tables objects --yes   # 只同步业务对象台账
```

踩坑：SeaTable 删表端点是 `DELETE /api/v2/dtables/{uuid}/tables/` + body `{"table_name": ...}`
（不是 `DELETE .../tables/{table_id}/`）；建表 body 列字段名是 `column_name/column_type`。

**微信来单自动触发（`loop_trigger.py`）**：扫描 CRM 销售线索表，为无案件的线索自动创建
控制平面案件（CUS/LED/OPP）。默认 preview，`--yes` 才建；按客户名+产品幂等，重复扫描
零重复；跟进内容含「终止/无效/放弃/已流失」的死单自动过滤。链路：
微信消息 → crm_dispatch lead（写销售线索表）→ loop_trigger（建控制平面案件）→
business_loop advance（推进状态）→ loop_sync（镜像 CRM 云端台账）。

---

