# 生产交付协同助手（v2.1.3）

**描述**：通过自然语言控制「生产」业务数据的增删改查，覆盖项目立项 → 生产计划 → 采购 → 生产执行 → 库存核对 → 发货 → 维修售后的完整周期，并支持工时/成本/质量/供应链四维分析。

**本版最大变化（vs 旧版）**：
- ✅ **多底座可插拔 · 6 个后端即为终态（v2.1.3）**。契约层 `adapters/base.py` 重写为**三层**（必修方法 / 能力声明 / 可选方法）+ 12 个能力位；`adapters/factory.py` 重写为**后端注册表**——新增一个后端只需「实现 8 个必修方法 + 在 `schema.BACKEND_TYPES` 加类型映射 + `register_backend()` 登记一行 + 加契约测试」，**不用改工厂一行代码**。现状：**local / seatable / feishu（多维表格）/ feishu_task（飞书任务）/ jiandaoyun / zentao**。**金蝶经评估不兼容，已从路线图移除**（见 §0.5「金蝶为什么不做」）；若客户已有金蝶，走**库存源**的通用 `api` 连接器，不影响业务底座。详见 §0.5。
- ✅ **协议事实一律实测，不照文档猜（v2.1.x）**。飞书多维表格 4 轮只读探测 + 飞书任务 4 轮读写探测（共约 40 条实测事实）+ 禅道 2 轮全量探测（88 路径可用性矩阵，实体 10→16）；**简道云是唯一例外**（无实测环境，全部来自官方文档，模块内逐条标注 ⚠️）。
- ✅ **修掉三个历史静默失败**（v2.1.0）：① `SeaTableAdapter.list_linked` 的**空桩** `return []`（命令成功、返回空、无报错）；② `wx/wxmatch.py` 写入路径**在本机 100% 死掉**（只认命名 Base `business`，而本机用 `bases:` 形态）；③ `factory` 对未知 `backend` 值**静默**返回 local（会把「写线上」变成「写本地 CSV」且读回照样通过）—— 现在至少会打 `[warn]`，而**写入路径**（`strict=True`）直接抛错拒绝退回。
- ✅ **彻底解耦后端**。领域知识（流程、表规则、格式、分析公式）与存储完全分离。
- ✅ **零配置开箱即用**。默认 `local` 后端，数据存本地 CSV/Excel，**不需要任何 SeaTable 账号，也不需要 PartDB**，离线就能用。
- ✅ **凭证不再写死**。旧的 `API Token` / `Base UUID` / PartDB 内网 IP 已全部移除，统一由 `config.yaml` 配置；分发技能不再泄露你的账号。
- ✅ **SeaTable / PartDB 改为可选**。有自己 Base 的，在 `config.yaml` 填 token/uuid 即切换；有 PartDB 的，开启 `partdb.enabled` 才有缺料检查。
- ✅ **所有数据操作走 `domain/op.py` 这一统一入口**，模型与用户都不碰具体存储和凭证。
- ✅ **新增微信情报反哺（v1.4.0 双引擎）**：引擎A 直读微信 4.x 本地加密库（`wx/wa_db.py`，主密钥从运行中的 Weixin.exe 内存提取，无需第三方工具）；引擎B 回退 `merge_all.db`（微信3.x + win-wechat-summary）。拉监控群消息 → 事件待确认 → 确认后写入 SeaTable 业务表，全程留痕。
- ✅ **新增第二大脑三件套（v1.4.0）**：`wx/wxwatch.py` 实时哨兵（1 秒轮询监听，关键词命中自动登记事件+通知）、`workflows/alerts.py` 异常检测引擎（A1-A7 七条规则，让数据主动喊）、`workflows/daily_brief.py` 站会摘要（四路数据合成一段话）+ `workflows/notify.py` 发件箱（Agent Mail 触达）。
- ✅ **新增物料行情监控**：从采购记录自动生成物料监控清单，保存价格快照，计算涨跌幅并跟踪 NRND/EOL 停产状态，驾驶舱展示趋势和告警。
- ✅ **新增代理商 API 自动查价（v1.5.0）**：`domain/suppliers.py` 接入得捷 DigiKey（OAuth2 + Search/v3）与贸泽 Mouser（Search API，一次 10 个型号），官方报价与生命周期直接入库，统一折算人民币 ¥。凭证只存本地 `config.yaml`（不入库、日志脱敏），**只读不碰下单**。按库存电子料数量自适应复查节奏（≤100 每周 / ≤300 半月 / >300 每月），未到期跳过以省配额。
- ✅ **新增原料行情监控（v1.6.0）**：`domain/commodities.py` 跟上游原料成本——金/银/铜/锡/铝/镍全自动（新浪期货实时，零凭证），PP/LLDPE/PVC 作石化链代理指标，ABS/PC/PS 现货人工录入。统一入口 `python domain/market.py raw`。**同口径环比**（连续合约不与具体合约混比），走势用 sparkline 字符条。
- ✅ **新增消息↔SeaTable 核对引擎（v1.6.2）**：`wx/wxmatch.py` 把微信情报从归纳总结升级为结构化对账——群消息收款/下单/合同 PDF 与业务表**逐条匹配**（客户合同 ↔ 项目表、供应商合同 ↔ 5 张采购记录表），发现「群里说了但表里没有」的缺口；**只读核对**，高置信项生成预填意图等人确认。
- ✅ **新增风险预测引擎（v1.7.0）**：`domain/foresee.py` 补上第二大脑最后一层「想」——**合同倒排**（已交付计划真实工期分位 → 各环节最晚开始日 → 必须立刻执行清单）、**供应商交期画像**（承诺 vs 实际偏差，组装料平均晚 29 天自动加 buffer）、**缺料预警**（BOM 缺口 × 在途 ETA，判断必须立刻下单/在途来不及）。产出 `data/foresee.json`，驾驶舱「风险雷达」section + 行动建议联动。
- ✅ **微信事项双表分流与证据治理**：微信候选按 `production`（业务事实）/`tasks`（待办执行）两个命名 Base 分流，同一消息可拆两条且全部先待确认；图片证据允许在确认后上传，普通文件优先文本化；每季度只扫描超过 90 天、已闭环且非长期保留的清理候选，只有显式 `--yes` 才删除。
- ✅ **新增微信图片专线（v2.0.0）**：`wx/wxmedia.py` 离线解密微信 4.x 加密图片（`.dat` V0/V1/**V2**）→ 转码（含自研 `wxgf`）→ 落盘 → **RapidOCR 识别**，把「群里发的付款截图/回执」变成可检索、可对账的金额信号。已实测读出建行电子回执「**￥9,9XX.XX**」。**旧文档「图片拿不到字节、无法 OCR」作废**（根因是看错路径层级）。
- ✅ **新增供应商群自动纳入（v2.0.0）**：`wx/wx_watchlist.py` 从 SeaTable 的**供应商/客户实体**自动生成归一化关键词表（剥离城市前缀/公司后缀/行业通用词），**并进** `watch_groups`。以后**群名里带上供应商名 → 自动进监控**，无需手工改配置。
- ✅ **自动化双班定型（v2.0.0）**：**每晚 19:00 全量重扫**（全部可调用数据 + 微信图片 OCR）并重建发布看板；**每日 09:00 轻同步**只补 19:00→09:00 的群聊增量，**不重建、不发布**。
- ✅ **可信执行层 v1（v2.0.0 核心）**：**高置信 ≠ 授权**——所有业务表写入必须持有显式授权（人工审批或人亲手写的授权文件），`wx/wxmatch.py apply` 不带 `--grant-file` 时**只产待授权清单、一行都不写**；新增 `workflows/evening_full.py` 把 19:00 排成「采集 → OCR/提取 → 核对 → 授权写入及读回 → 快照 → 风险 → 生成发布」；OCR/核对/发布全部进运行账本（`data/runs/<run_id>/final.json`），`application/gates.py` 做**发布门禁**——关键阶段失败或读回失败即阻断发布；离线假数据验收去重/恢复/读回失败。详见 `docs/trusted-execution-v1.md`。

---

## 版本历史（摘要 · 完整版见 references/changelog.md）

| 版本 | 日期 | 一句话 |
|---|---|---|
| **v2.1.3** | **10-02** | ★**后端范围收敛：金蝶移除，6 个后端即终态**。业主「**金蝶不行就不兼容了**」。金蝶是财务/ERP 域产品，数据模型（会计期间 / 账套 / 凭证 / 存货核算）与「生产交付」的业务对象**不同构**，硬映射只会造出一层语义可疑的翻译层 —— 正是本项目最忌讳的「用猜的行为冒充支持」。① SKILL.md §0.5 新增「**金蝶为什么不做**」并写明**库存源不受影响**（客户已有金蝶仍可走 `inventory.source: api`/`mcp` 通用连接器，不需要新后端）。② 其余 5 个后端做了一轮健康检查：契约一致性 6/6（无缺必修方法、无非法能力位）、禅道真实只读 **30/30**、飞书任务真实只读 **24/24**；飞书多维表格与简道云**本机未接入**（config 段缺失，非代码缺陷），简道云仍是唯一「仅文档、无实测」的后端。③ 明确「**已配 / 未配**」区分，避免把「没配」误读成「坏了」。 |
| **v2.1.2** | **10-01** | ★★**多底座第 4 期 — 飞书任务（Task）底座** + **禅道全量摸透**。飞书在项目管理上是**两个产品两套 API**：多维表格（第 1 期）与**任务**（本期）。映射：表=任务清单、行=任务、`__row_id__`=guid、列=内置属性+清单自定义字段。① 4 轮真实读写探测拿到约 18 条事实，**6 条与直觉相反**：`tasks.patch` 的 `update_fields` 是**必填的固定 14 项白名单**且 `members` 不在其中、列了就必须给非空值（否则**全盘失败**不是忽略）、给了没列则**静默忽略**、`tasklists.tasks` 是**只有 7 个键的摘要投影**（拿它当行会让所有详情列看起来「本来就是空的」）、`tasks.get` 对不存在 guid **抛 1470404**（不是回空）、`tasks.create` 不带清单照样成功 → **孤儿任务**任何清单都看不到。② `--yes` 是另一条命令分界线（high-risk-write 不加时服务端**什么都没做**、rc=**10**）。③ 单选**写入必须给选项 guid**、读回来也是 guid → 写名/读 guid 不对称。④ 禅道第二轮实测（实体 10→16）补 17 条事实。⑤ **三重验证**：离线契约 **86 项**、真实只读 **24/24** + 写路径 **56/56**、反向验证 **14/14** 守卫「退回即断言失败」。⑥ 反向验证抓到 1 个真实缺陷（`get_row` 未翻译 1470404）+ 1 个测试盲区（两处「关注人」写入分支均无测试，退回后仍全绿）。测试 742 → **828 全绿**。详见 `CHANGELOG.md`。 |
| **v2.1.1** | **09-30** | ★★**多底座第 1～3 期 — 飞书多维表格 / 简道云 / 禅道三个后端**。① `feishu`（多维表格 Base）：4 轮只读探测**全部协议事实实测**，含 `PUT /rows/` 覆盖语义、关联字段双向**双列**、字段类型大小写混用等；真实 Base 只读端到端验证 + 「零写请求」取证。② `jiandaoyun`（简道云 API v5）：⚠️ **唯一一个全部来自官方文档、无实测环境**的后端 —— 模块内逐条标注 ⚠️，接真实企业第一件事是跑 `tools/verify_jiandaoyun.py --read-only` 核对推断（尤其**日期按 UTC 存**与**格式不合法的值被静默写成空**两条）。③ `zentao`（禅道 REST v2）：**唯一「实体与字段系统固定、不能建表」**的底座，故 `schema.BACKEND_TYPES` **刻意不给它类型表**（硬凑会让人误以为能建列），`ensure_table()` 老实抛 `Unsupported`；2 轮全量探测（19 资源 × 88 路径可用性矩阵）；写「项目」有实测副作用（顺手建**同名产品**、且名字**用过一次不可回收**）。 |
| **v2.1.0** | **09-30** | ★★**多底座改造 · 第 0 期地基**（业主原话：「把这套生产经理的第二大脑整体也兼容……适配更多不同的底座，比如 partdb 简道云 飞书多维表格 禅道 金蝶等」→「我肯定是要全套支持」）。**本期不新增后端**，只把「接一个新后端」的代价从「改 6 处硬编码 + 碰 4 个私有属性」降到「登记一行」。① `adapters/base.py` 重写为**三层契约**（必修方法 / 能力声明 / 可选方法）+ 12 个能力位 + `Unsupported` 铁律。② 🔴 修掉 `list_linked` **空桩**（`return []` 静默失败；根因 `GET /links/` = 405、`metadata` 顶层**无 `links` 键** → 旧 `_resolve_link_id` 是从未走通的死代码）。③ 新增 `link_append()` / `link_one_way()`，`crm_dispatch` 去私有属性。④ `factory.py` 重写为**后端注册表**，未知后端不再静默（默认 warn + 退回 local，写入路径 `strict=True` 抛错拒绝退回）。⑤ 🔴 `wx/wxmatch.py` 写入路径**复活**（旧实现只认命名 Base `business`，而本机用 `bases:` 形态 → 必然抛 RuntimeException，本机 100% 死）。⑥ 类型归一中立化 + `link_id_for()` 方向敏感。⑦ 4 轮只读探测推翻多项旧假设（`POST /links/` 契约、双向关联**双列**、`schema.LINKS` 的 `PlAl`/`RsAl` 不存在等）。测试 **403 → 466 全绿** + 真实 Base **只读**端到端验证通过（含「零写请求」取证）。详见 §0.5 与 `references/changelog.md`。 |
| **v2.0.0** | **09-26** | ★★★**断代版**。① **可信执行层 v1**：统一写库授权（**高置信 ≠ 授权**，`wx/wxmatch.py apply` 无 `--grant-file` 只列清单）、副作用分级契约、`application/runner.py` DAG 执行器、`application/dataservice.py` 读回验证+幂等+台账、**发布门禁** `application/gates.py`（关键阶段失败即阻断发布）。详见 `docs/trusted-execution-v1.md`。② **二期「单项目第二大脑」** `project_brain.py`（上下文/行动/证据/记忆，先预演再授权才执行）。③ **三期「项目决策辅助」** `decision_support.py`（三日期+关键路径、方案比较、延期传播、预测复盘带防未来信息审计，只读离线）。④ **仓库结构重组**：44 个顶层脚本按域归组，命令由 `python op.py` 变为 `python domain/op.py`。⑤ 修 `domain/order_to_cash.py` / `workflows/loop_sync.py` 整理后无法直接执行的回归。 |
| v1.9.0 | 09-15 | 内部版本（**未单独发版，已并入 v2.0.0**）：甘特三状态 + 微信图片离线解密/OCR + 供应商群自动纳入 + 双班自动化 |
| v1.8.9 | 09-15 | ★★看板三条口径（业主）：① **无交期计划改用历史数据推算交期**（已交付 `花费天数` 中位 28 天，`est=True` 虚线+`≈` 标注，覆盖旧的「待补交期」沉底做法）② **进度取「生产计划.阶段」实测值**（不再按日期推算）③ **所有列表统一「计划中」优先排序**；顺带修掉适配器**吞掉 `ctime`/`mtime` 列**的 bug（导致 foresee 历史分位在直连下静默全空） |
| v1.8.8 | 09-15 | ★业主裁定**永久排除 131 群**（示例供应商AR×34 另一业务线 / 临时方案开发群×9 / 非生产主体×16 / 无名群×72）→ 写入 `EXCLUDED_GROUPS_*`，**不再计入缺口、不再反复来问**；实测与白名单**零重叠**，68 条不变 |
| v1.8.7 | 09-15 | ★「供应商名匹配不到 ≠ 没群」：`resolve()` 新增**第③层产品名匹配**（`PRODUCT_ALIAS`，仅限**独家**料件，A2 弱匹配档）；工艺群 `外壳/镭雕/治具` + 方案商群入白名单；**明确不删 `技术`**（返修 + 新开发需求）；白名单 68 条 → 实覆 **162/386 群** |
| v1.8.6 | 09-15 | ★口径修正：关键词群分**三类**（供应商 / **客户** / **客户的技术服务售后**）；客户实体在 `项目.项目` 列而非 CRM 测试表；`tools/audit_wx_coverage.py` 升级为三类审计 + 五桶分类 + 表外主体反向检测；白名单 54 条 → 实覆 **136/386 群** |
| v1.8.5 | 09-15 | ★发现 `watch_groups` 是**子串匹配**（可直接写关键词）；重写 `tools/audit_wx_coverage.py`（直播 SeaTable + 四类判定）；白名单 48 条 → 实覆 **130/386 群** |
| v1.8.4 | 09-15 | 新增 `tools/audit_wx_coverage.py` 白名单覆盖自查（贴片厂/组装厂/物料供应商）；文档 §11.4.2 |
| v1.8.2 | 09-15 | wxmatch v1.9：到货/发货核对 + apply 高置信自动写库；晚 19:00 自动化升级「当日复盘」 |
| v1.8.3 | 09-15 | P0 瘦身（−58%）+ P1 price-sensor + P2 专家壳模式（内嵌 51 文件→1 转介）+ P3 cockpit-studio |
| v1.8.1 | 09-14 | 项目矩阵板块（全表+思维导图）；修直连路径两处静默错误（select ID/ISO 日期）；甘特「待补交期」；应收口径对照；对内共享页 |
| v1.7.x | 09-03 | 风险预测引擎 domain/foresee.py（合同倒排/供应商画像/缺料预警）+ 预测台账自我学习 + ask 追问 |
| v1.6.x | 09-03 | 消息↔SeaTable 核对引擎 wx/wxmatch.py；原料行情 domain/commodities.py；微信 4.x 文件名明文破案 |
| v1.5.x | 09-02 | 代理商 API 查价（得捷 V4/贸泽）；三道省配额闸门；空价不写库 |
| v1.4.x | 09-01 | wxwatch 实时哨兵 + alerts 异常检测 + daily_brief + notify 发件箱；微信 4.x 引擎A直读（SQLCipher 解密）；update_row 列名静默失败修复 |
| v1.3.x | 08-31 | tools/deploy.py 一键部署；cockpit/publish.py 驾驶舱发布器；一产品一行/交期收款起算规则 |
| v1.1.x | 08-08 | 主题三态切换；角色写入口裁剪；视觉系统重建 |

## 0. 后端与配置（必读）

技能根目录下的 `config.yaml` 决定用哪种业务后端；采购库存源可单独选择 PartDB 或客户导出的 Excel/CSV：

> 新用户可跳过手改配置：把资料（SeaTable Token / PartDB / 微信 DB 路径）交给
> `python tools/deploy.py`（支持 CLI 参数、环境变量、`deploy.yaml` 资料文件、交互问答四种给法，
> `--demo` 零资料演示），它会自动生成配置并完成整套部署，详见仓库 README「一键全自动部署」。

```yaml
backend: local          # 已登记后端名（见 adapters/factory.py::list_backends()）：
                        #   local（默认，零配置）| seatable | feishu（飞书多维表格）
                        #   | feishu_task（飞书任务/待办）| jiandaoyun（简道云）
                        #   | zentao（禅道）
                        # 6 个后端即为终态（金蝶经评估不兼容，已移除）；未登记的名字不会静默退回（见下）。
local:
  data_dir: data
  format: csv
seatable:
  api_token: ""
  server: "https://cloud.seatable.cn"
  base_uuid: ""
inventory:
  source: file          # partdb | api | mcp | file
  file:
    path: "pipeline/customer/inventory/库存导出.xlsx"
    stock_is_confirmed: false
partdb:                 # 仅 source=partdb 时需要
  enabled: false
  url: ""
  token: ""
```

首次部署采购流水线：

```bash
python3 pipeline/run.py init      # 创建被 Git 忽略的客户资料目录与 rules.local.yaml
python3 pipeline/run.py preflight # 校验公司抬头、BOM、库存源与可选 SeaTable
```

`pipeline/customer/`、`pipeline/rules.local.yaml`、`config.yaml` 都是客户私有资料，已被 Git 忽略。已有金蝶、简道云、禅道或自建 ERP 时，优先配置 `inventory.source: api` 或 `mcp`；无法在线连接时再用 Excel/CSV 文件源兜底。

**数据操作一律用 `domain/op.py`**（路径相对于技能目录）：

```bash
python3 domain/op.py list 生产计划
python3 domain/op.py append 生产计划 '{"生产产品":"4G小卡","数量":100}'
python3 domain/op.py link 生产计划 PCB下单记录 <生产计划row_id> <PCB记录row_id>
python3 domain/op.py export-excel 生产数据.xlsx
python3 domain/op.py partdb-search 电容 10        # 仅 PartDB 启用时
```

**如何确认当前后端**（三条，从准到快）：

```bash
python3 -c "from adapters.factory import list_backends, get_backend; \
print('注册后端 =', list_backends()); print('当前后端 =', get_backend())"   # ① 最准
grep -n "^backend" config.yaml                      # ② 配置里写的值
python3 domain/op.py list 生产计划                   # ③ 实际能不能连通；有 [warn] 退回 local 就是降级了
```

`backend` 写了一个**未登记**的名字时（如 `feishu` 还没接完），行为分两种 —— 都**不再是静默**：

| 调用方式 | 实际行为 |
|---|---|
| 默认（读路径，`get_adapter(cfg)`） | 往 stderr 打 `[warn] 未知后端 backend='feishu'…，退回 local 模式`，**仍然退回 local** |
| `strict=True`（**写入路径**，`application/dataservice.py` 传入） | 抛 `RuntimeError`，**拒绝退回** |

🔴 区别是「**不静默**」而不是「**不退回**」—— 默认仍会退回 local，只是会喊一声。
真正防住「写线上变成写本地 CSV」的是 `strict=True` 这条写入路径。原因很关键：
**读回校验在本地库上照样会通过**，所以写入路径必须硬失败，不能拿「读回验证」当兜底。

> ⚠️ 本文件**不含任何 token / UUID / 内网地址**。需要连 SeaTable/PartDB 时，凭证只能来自用户的 `config.yaml`。

---

## 0.5 多底座适配器契约（改适配器 / 新增后端必读）

目标是把整套系统做成**多底座可插拔**（SeaTable / 飞书多维表格 / **飞书任务** / 简道云 / 禅道）。
契约层在 `adapters/base.py`，后端注册表在 `adapters/factory.py`。

> **金蝶为什么不做（2026-10-02 业主裁定）**：金蝶是**财务/ERP 域**的产品，
> 它的数据模型与「生产交付」这套业务对象（项目 / 生产计划 / 采购 / 发货）**不是同构的** ——
> 它有会计期间、账套、凭证、存货核算等本位概念，硬映射过来只会造出一层
> 语义可疑的翻译层，而这正是本项目最忌讳的「用猜的行为冒充支持」。
> **业主原话：「金蝶不行就不兼容了。」** 故 6 个后端即为终态，不再扩后端。
> ⚠️ 但**库存源**是另一回事：客户已有金蝶/简道云/禅道/自建 ERP 时，仍可走
> `inventory.source: api` 或 `mcp` 的通用连接器（见 §0 与 `config.yaml.example`），
> 那不需要新后端。

> ⚠️ **飞书是两个后端，不是一个**：`feishu` = **多维表格（Base）**，
> `feishu_task` = **任务（Task）**。两者除了都叫「飞书」外没有共同点 ——
> 没有共同的表概念、没有共同的 id 空间，连「写一个字段」的语义都不同
> （Base 是整条 record 覆盖，Task 是 `update_fields` **白名单**）。详见 §0.6。

### 三层契约

| 层 | 内容 | 约束 |
|---|---|---|
| ① 必修方法（**8 个**） | `auth` / `list_rows` / `get_metadata` / `append_row` / `update_row` / `delete_rows` / `link` / `list_linked` | `@abstractmethod`，ABC 实例化时即强制 |
| ② 能力声明 | `capabilities()` 或类属性 `CAPS` | **声明式、无副作用、建连前可读**；上层据此决定走哪条路 |
| ③ 可选方法（**9 个**） | `close` / `query` / `get_row` / `table_exists` / `append_rows` / `link_append` / `link_one_way` / `ensure_table` / `version_of` | 有通用兜底；**拿不到正确答案时抛 `Unsupported`，不许静默返回空** |

> ⚠️ `auth` 在**必修**里、`query` 在**可选**里 —— 别记反。
> `query` 有客户端过滤的具体实现（base.py），所以不是抽象方法；
> 而漏实现 `auth` 会在实例化时直接 `TypeError`（好在会自曝，不是静默错）。

### 12 个能力位（`adapters/base.py`）

`read` · `write` · `update` · `delete` · `link` · `link_read` · `batch_write` ·
`schema_manage` · `idempotent` · `optimistic_lock` · `query_pushdown` · `server_row_id`

`BASE_CAPABILITIES` 默认含前 5 个。**`link` / `link_read` 必须显式声明** ——
`link()` 完全可能存在却是个空实现，靠方法存在性推断会直接踩坑（本仓库踩过：`list_linked` 曾长期
`return []`，命令成功、返回空、**无报错**）。

### 铁律：`Unsupported` 而非空值

```python
class Unsupported(NotImplementedError):   # 继承 NotImplementedError，老代码 except 仍能兜住
    """拿不到正确答案就抛它。``return []`` / ``return None`` 冒充「没有」是禁止的。"""
```

为什么这么严：**静默失败是本仓库的历史主坑**。SeaTable 的 `PUT /rows/` 传错列 key 会返回
`{"success":true}` HTTP 200 却**不落库**；空桩 `list_linked` 让「读关联」永远返回空。
两者都「看起来成功」。所以：
- `link_append()` 默认抛 `Unsupported`，**不提供**「读-改-写硬凑」的兜底 ——
  在有并发写的后端上读-改-写会丢失更新，悄悄做这个加法比不做更危险。
- `link_one_way()` 默认抛 `Unsupported`，**不降级成双向 `link()`** ——
  那正是本方法要消灭的错误行为，悄悄降级等于把坑换个地方埋。

### 新增一个后端（五步）

1. 子类化 `BaseAdapter`，设 `backend = "feishu"` 与 `CAPS`
2. 实现 **8 个必修方法**（含 `auth`）
3. 在 `adapters/schema.py` 的 `BACKEND_TYPES` 里加该后端的中立类型映射
   （未知类型 `backend_type()` **抛错，绝不静默降级成 text**）。
   ⚠️ **例外**：若该后端的实体与字段是**系统固定的、不能建表**（禅道就是这样），
   应当**刻意不加**这张映射表并在原处写明理由 —— 硬凑一张只会让人误以为它能建列。
   此时「字段类型 ↔ 中立类型」的**读向**对应由该后端模块自己提供
   （`adapters/zentao.py::neutral_type()`），且 `ensure_table()` 必须老实抛 `Unsupported`。
4. `adapters/factory.py` 里登记一行（各参数都可省，只有 `name`/`label`/`make` 必给）：
   ```python
   register_backend(
       "feishu",                                  # name（位置参数）
       label="飞书多维表格",
       make=_make_feishu,                         # (selected_cfg, whole_cfg) -> adapter
       required_keys=("app_id", "app_secret"),    # 缺任一 → fail-closed，不静默退回 local
       key_aliases={"app_id": ("appid",)},        # 兼容旧写法/别名
       named_bases=True,                          # 支持 bases:<名字> 多实例
       default_server="",                         # 没配 server 时的兜底地址
       deploy={"verify": (...), "init_sync": (...)},   # 部署钩子，tools/deploy.py 据此拉活
   )
   ```
5. 在 `tests/test_adapter_contract.py` 加契约用例

**判据**：注册完不需要改工厂一行代码，`get_adapters()` / 命名 Base 解析 / 键别名归一化就都通了 ——
`tests/test_adapter_contract.py::PluggableBackendTests` 就是拿这事当验收标准的。

### 已知局限（诚实记录）

- `SeaTableAdapter.link_append()` 的读-改-写**不是并发安全**的（SeaTable 无行版本号、无条件写）
- `adapters/partdb.py` **至今未继承 `BaseAdapter`**，是侧挂集成，不在本契约内
- `version_of()` 目前只在基类定了契约，还没有后端真正实现（SeaTable 无行版本概念）
- 同一对表之间可能有多条关联 → 按表名解析 `link_id` 会报歧义，须用 `column=` 指定列名
- **列 `type` 字符串大小写混用**是实测事实（`link`/`LINK`、`date`/`DATE` 并存）→ 适配器里已全部
  改为 `.lower()` 比较。但 `sync/seatable_sync.py:136` 与 `sync/backfill_seatable.py:100`
  仍是精确比较（走 CSV 路径、不在契约内，且当前实测 0 个大写 select 列）—— **列为已知风险面，未修**。
- `sync/` 下的 CSV 同步路径**未纳入适配器契约**，多底座改造尚未覆盖它
- **禅道（`zentao`）只覆盖 PM 实体**：项目集/项目/执行/任务/产品/需求/Bug/测试用例/测试单/用户。
  采购、发货、库存那类表**禅道承载不了**，`_resolve()` 会明确拒绝并指回 seatable/feishu/jiandaoyun，
  不会硬塞进语义不符的实体。逻辑表名别名见 `adapters/zentao.py::ALIASES`。
- **禅道写「项目」有实测到的副作用**：会顺手建一个**同名产品**（删项目不删产品），
  且那个名字**用过一次就不可回收**（再建同名项目会被拒，报的还是「『产品名称』已经有…」）。
  适配器已在错误文案里加了大白话提示，但**批量建项目前必须用一次性名字**。
- **禅道部分实体列表读不了**（本版 `/tasks` `/stories` 的列表 GET 回 HTTP 200 + 0 字节）：
  适配器抛 `Unsupported` 而不是返回空列表冒充「没有数据」；`get_row` 走详情接口仍可用。
- **简道云（`jiandaoyun`）的全部协议细节来自官方文档、无实测环境**：接入真实企业时
  第一件事是跑 `tools/verify_jiandaoyun.py --read-only` 逐条核对 `adapters/jiandaoyun.py`
  里那些标注了 ⚠️ 的推断（尤其是**日期按 UTC 存**与**格式不合法的值会被静默写成空**两条）
- **飞书任务（`feishu_task`）刻意不声明三项能力**：`CAP_LINK` / `CAP_LINK_READ`
  （任务的 `dependencies` 是**任务↔任务**不是表↔表，实测**只读**且在本机 99 条任务里
  **全为空** → 「读出来是空」与「这字段根本不生效」无法区分，硬做等于猜）、
  `CAP_BATCH_WRITE`（任务 API 没有批量创建接口）、`CAP_IDEMPOTENT` /
  `CAP_OPTIMISTIC_LOCK`（删除不幂等、无行版本号）。`link()` 与 `list_linked()`
  **如实抛 `Unsupported`**。
- **飞书任务两种自定义字段类型的值键未实测**：`multi_select` 与 `datetime` 的
  写入路径是按同构推断实现的（`multi_select_value` / `datetime_value`），
  `member` / `single_select` / `text` / `number` 四种已实测。首次用到前请先跑
  `tools/verify_feishu_task.py --write-test`（会在一次性清单里跑完整读写并清理）。
- **飞书任务的成员写入不是并发安全的**：`members add` 是**追加**、`remove` 只摘指定
  `(id, role)`，「改负责人」实际是**读-改-写**（先读当前成员再算增删）。同一任务被
  并发改成员时可能丢失更新 —— 与 `SeaTableAdapter.link_append()` 的已知局限同类。
- **飞书项目（Meego / Meegle）尚未打通**：实测路由全在（`/open_api/authen/plugin_token`
  401、`user_plugin_token` 401、`refresh_token` 400、`work_item/filter` 500），
  但 **lark-cli 的 OAuth token 打不了它** —— 它要 `plugin_id` + `plugin_secret`，
  须**空间管理员**在飞书项目开放平台建插件后发布。官方 CLI 是 `meego-cli`（本机未装）。

---

## 0.6 飞书任务后端（`backend: feishu_task`）· 操作前必读

飞书在项目管理上是**两个产品、两套 API**：
`feishu` = **多维表格（Base / Bitable）**，路径 `/open-apis/bitable/v1`；
`feishu_task` = **任务（Task / 待办）**，路径 `/open-apis/task/v2`。

**映射关系**：表 = **任务清单（tasklist）**（可用清单名或 guid）；行 = **任务**；
`__row_id__` = 任务的 **guid**；列 = 任务内置属性（摘要/描述/完成状态/开始/截止/负责人/
关注人/里程碑…）+ 该清单的**自定义字段**。

**本机实测的清单**：「项目」（99 条任务）与「付款」（70 条任务）。
「项目」清单有 6 个自定义字段：**优先级**（单选：高/中/低/搁置）、**项目预算**（number/cny）、
**工时**（number，符号「/人天」）、**状态**（单选：进行中/已完成/暂停中/执行异常/已延迟/已取消）、
**配合人员**（member）、**任务完成度**（number/percentage）。分组：研发 / 采购 / 生产 / 商务 / 品宣 / 公司运营。

### ⚠️ 六条「与直觉相反」的实测事实（全在 `adapters/feishu_task.py` 顶部 docstring）

1. **`tasks.patch` 的 `update_fields` 是必填的、固定 14 项白名单**，且 **`members` 不在其中**
   → 改负责人/关注人**只能**走 `members add` / `members remove`，走 `patch` 一定失败。
2. **`update_fields` 列了就必须给非空值**：列了 `summary` 而 body 里没有 → `1470400`
   且**整条调用失败**（不是「忽略那一项」）。
3. **给了却没列 → 静默忽略**：body 塞 `description`、`update_fields` 只写 `summary`，
   调用**成功**且 `description` 原文不动 —— **只看返回值发现不了**。
   （适配器让两者由**同一个 dict 派生**，从结构上使 2/3 不可能发生。）
4. **`tasklists.tasks` 是摘要投影，不是详情**：列表项**只有 7 个键**
   （`completed_at / due / guid / members / start / subtask_count / summary`），
   连 `description` 都没有；而 `tasks.get` 返回 **28 个键**。
   **拿列表当行用，会让所有详情列看起来「本来就是空的」** → `list_rows()` 逐条补详情（N+1），
   只想要摘要请显式用 `list_rows_brief()`。
5. **`tasks.get` 对不存在的 guid 抛 `1470404 not_found`**（不是回空 `data.task`）
   → `get_row()` 必须翻译该错误码为 `None`，其余错误照抛。
6. **`tasks.create` 不带 `tasklists` 照样成功** → 任务成为**孤儿**，任何清单都看不到，
   而调用方以为建好了。适配器的 `append_row()` **强制要求目标清单**。

### 其他要点

- **`--yes` 是另一条命令分界线**：`tasks.delete` / `tasklists.delete` / `sections.delete` /
  `custom_fields.remove` 是 **high-risk-write**，不加 `--yes` 时服务端**什么都没做**，
  回 `{"ok":false,"error":{"type":"confirmation",…}}` 且 **rc=10**（既不是 0 也不是 1）。
  适配器按 `cmd` 自动补，不会漏。
- **单选写入必须给选项 guid，不能给选项名**（给名字报 `1470400 "isn't a visible option"`），
  而**读回来也是 guid** → 写名 / 读 guid **不对称**。适配器读向自动翻译成选项名、
  写向自动把名字翻成 guid。
- **完成状态不是布尔位**：判据是 `completed_at != "0"`；**取消完成 = patch `completed_at` 为字符串 `"0"`**。
- **时间字段两种形状**：`due` / `start` 是 `{"timestamp":"<ms>","is_all_day":bool}`；
  `completed_at` / `created_at` / `updated_at` 是毫秒字符串。
- **重名会撞车**：内置列与自定义字段可能同名（本机真有一个叫「状态」的自定义字段）。
  撞车时自定义字段被改名为 `原名(自定义)` 并保留 `origin_name`；**两个自定义字段之间**重名
  则直接抛 `KeyError`（按名字取值是歧义的）。
- **创建类接口回的都是包装键**：`data.tasklist.guid` / `data.task.guid` /
  `data.custom_field.guid` / `data.section.guid`（**不是** `data.guid`）。
- **没有必填配置键**：飞书任务是**账号级**的，清单（=表）是**方法参数**，
  凭据复用 `lark-cli` 的登录态。`identity` / `cli_path` 没配就回落 `feishu` 段。

### 怎么自检

```bash
python tools/verify_feishu_task.py --read-only    # 24 项，安全，随时可跑
python tools/verify_feishu_task.py --write-test   # 56 项，在一次性清单里跑完整读写并清理
```

---

## 1. 核心原则（铁律）

1. **任何增删改操作，必须先向用户展示完整数据，等用户确认后再执行。**
2. **操作前先 `domain/op.py meta <表名>` 或 `domain/op.py list` 读取最新结构**（表结构可能变化）。
3. **link 列的值格式为 `{表名}:row_id`（单条）或 `{表名}:id1,id2`（多条）**；写入后用 `domain/op.py link` 建立双向关联。
4. **日期格式统一 `YYYY-MM-DD`**。
5. **single-select / multiple-select 显示时必须翻译为中文标签，禁止显示原始值/ID。**
6. **「立项日期」默认今天，「交货日期」按交期天数自动计算，禁止追问。**
7. **询问 link 列时，必须先 `domain/op.py list` 目标表全部记录，完整列出供用户选择，禁止只描述不列表！**
8. **一句自然语言涉及多表新增时，必须自动建立所有双向关联**（每对表调一次 `domain/op.py link`）。
9. **微信候选必须先分流再确认**：生产业务事实进命名 Base `production`，负责人/截止日期/提醒/追问等执行事项进 `tasks`；同一消息可拆两条，禁止把事项都写进默认 Base。任何候选都不因自动化或高置信而跳过人工确认。
10. **图片证据允许上传但不默认自动上传**；先保留摘要/来源/哈希。PDF、Word、Excel 等普通文件优先文本化，只有文本化失败、必须核验版式/签章或用户明确要求时才上传原文件。
11. **证据季度复核、90 天候选**：只有超过 90 天、已闭环且非长期保留的证据可列入候选；`domain/evidence.py scan` 或不带 `--yes` 的 `prune` 只预览，显式 `--yes` 才能删除。
12. **后补字段（标记 📥 的）未提及就留空；每周五下午 2 点 cron 自动提醒填写。**（cron 在 WorkBuddy 自动化里配置，本技能只定义字段清单，见 §8）
13. **库存核对必须配置有效 `inventory.source`；通用库存列默认未确认，未确认库存不得用于生产承诺，人工审核关卡不可跳过。**
14. **写库必须持有显式授权（v1.10 起）**：production/tasks 业务表写入只能来自「人工审批」或「人亲手写的授权文件」，**置信度再高也不构成授权**（高置信只决定哪些条目进入待授权清单）。`python wx/wxmatch.py apply` 不带 `--grant-file` 时只列清单、不落任何写入。发布受**发布门禁**约束（`python workflows/workflow.py gate <run_id>`）：关键阶段失败或读回验证失败时，**不得**用任何方式绕过（手动上传、改链接、CloudStudio 部署）。详见 `docs/trusted-execution-v1.md`。

---

## 2. 列类型判断规则

解析表结构后，根据列类型决定处理方式（`domain/op.py meta <表>` 可看类型）：

- **自动跳过（不问用户）**：`auto-number`(自动编号)、`formula`(公式)、`link-formula`(关联公式)、`ctime`(创建时间)、`mtime`(修改时间)、`button`(按钮)、`creator`(创建者)。
- **需从自然语言提取或追问**：`text`、`long-text`、`number`、`date`(见 §3)、`single-select`(显示中文标签)、`multiple-select`(显示中文标签)、`collaborator`。
- **特殊处理**：`link`(关联，需问用户关联到哪条，先 list 目标表)、`file`(仅提示用户手动上传)、`geolocation`。

**选项值翻译**：single/multiple-select 返回的是选项ID或原始值，必须查 metadata 的 `data.options` 翻译成中文标签后再显示。

---

## 3. 日期自动计算规则（重要！）

**「立项日期」= 对话当天（Asia/Shanghai），不追问。**

| 用户说 | 处理 |
|--------|------|
| 「今天立项」「立项日期是今天」 | 自动取当前日期 |
| 「交期 X 个工作日」「X 天后交货」 | 立项日期 + X 工作日 = 交货日期（跳过周末） |
| 「X 月 X 日交货」 | 直接用该日期 |
| 用户明确说「立项日期是 X」 | 用用户给的日期 |

> 自然日 vs 工作日：说「X 个自然日」不跳周末；说「X 个工作日」跳周末。**禁止追问「立项/交货日期是哪天」——直接计算。**

---

## 4. 操作流程（全部经由 domain/op.py）

### 新增记录
1. `domain/op.py meta <表>` 看结构（或 `domain/op.py list <表>` 看现有数据）。
2. 从自然语言提取可填列值；link 列先 `domain/op.py list <目标表>` 列出供用户选。
3. **逐列检查非自动列，确保无遗漏。**
4. **列出完整待写入数据，等用户确认。**
5. 用户确认后：`python3 domain/op.py append <表> '<json>'` → 拿到 `row_id`。
6. 有关联：`python3 domain/op.py link <表A> <表B> <A的row_id> <B的row_id>`，**双向各建一次**（同语义关联分别用 A→B 和 B→A 两次调用）。
7. 返回结果。

### 查询 / 修改 / 删除
- 查询：`domain/op.py list <表>` 或 `domain/op.py query <表> --where 列=值`。
- 修改：先确认目标行 → 列出前后对比 → 确认后 `domain/op.py update <表> <row_id> '<json>'`。
- 删除：展示待删内容 → 确认后 `domain/op.py delete <表> <row_id> ...`。

---

## 5. 语义关联映射（17 条，供 domain/op.py link 引用）

同一语义关联在两张表上各出现一次，代表一对双向关联。`domain/op.py link` 会自动按表名解析真实 link_id（local 用内部标识，SeaTable 从 Base metadata 解析），**无需你写死 link_id**。

| 语义 | 主表 | 关联表 |
|------|------|--------|
| 生产计划 ↔ 贴片生产记录 | 生产计划 | 贴片生产记录 |
| 生产计划 ↔ 成品采购记录 | 生产计划 | 成品采购记录 |
| 生产计划 ↔ 项目（生产侧） | 生产计划 | 项目 |
| 发货清单 ↔ 维修记录 | 发货清单 | 维修记录 |
| 生产计划 ↔ IC采购记录 | 生产计划 | IC采购记录 |
| 生产计划 ↔ 组装记录 | 生产计划 | 组装记录 |
| 生产计划 ↔ PCB下单记录 | 生产计划 | PCB下单记录 |
| 项目 ↔ 发货清单 | 项目 | 发货清单 |
| 生产计划 ↔ 库存核对记录 | 生产计划 | 库存核对记录 |
| 生产计划 ↔ 组装料采购记录 | 生产计划 | 组装料采购记录 |
| 项目 ↔ 维修记录 | 项目 | 维修记录 |
| 生产计划 ↔ 外壳采购记录 | 生产计划 | 外壳采购记录 |
| 生产计划 ↔ 项目（另一条） | 生产计划 | 项目 |
| 生产计划 ↔ PCBA半成品采购记录 | 生产计划 | PCBA半成品采购记录 |
| 生产计划 ↔ 生产工序 | 生产计划 | 生产工序 |
| 资源 ↔ 资源分配 | 资源 | 资源分配 |
| 生产计划 ↔ 资源分配 | 生产计划 | 资源分配 |

**铁律**：主数据写入后立即 `domain/op.py link` 建立双向关联，不允许跳过。

---

## 6. 表级规则（18 张业务表 + 4 张闭环控制平面表，核心）

> 通用：带 `auto-number`/`formula`/`link-formula`/`ctime`/`mtime`/`button` 的列由系统自动处理，新增时**不写**；`link` 列新记录不预关联，关联用 §4/§5 的 `domain/op.py link`。

### 生产计划（新增）
- **填默认（不问）**：状态=`计划中`、阶段=`库存核对`、立项日期=今天。
- **必问/提取**：生产产品、数量、关联项目（列近 1 个月项目供选）、工序（列全部工序供多选，09测试/10出货(11 出货) 默认自动加）。
- 合同交期：提取「X 个工作日/X 天后/X月X日」，否则问（见 §3）。
- **⚠️ 一产品一行（2026-08-31 新规）**：同一合同/项目含多个生产产品时，**必须按产品逐行拆开建生产计划**，禁止把多个产品堆在一行（「生产产品」字段不得出现「A ×N；B ×M」多产品并列写法）。每行单独有自己的数量/工序/编号。
- **⚠️ 交期以收款起算（2026-08-31 新规）**：合同交货日期一律以**收到款项之日**起算（合同普遍注明「收款后 X 日内发货/交货」）。项目「实收」仍为 0 时，「交货时间」**留空不预填**；收款当日回填交货时间并重算倒计时。
  → 因此「合同交期为空」是**正常业务状态**，不是漏录。看板上这类计划会**用历史工期推算**一个交期并标 `≈`（只读展示，**不要据此回填 SeaTable**，见「甘特图」小节）。
- 其余（生产详情/版本说明）用户不说就跳过。

### 项目（新增）
- **填默认**：状态=`计划中`。
- **必填**：项目（名称）、产品需求（必须用 Markdown 表格 `| 产品名称 | 型号 | 数量 | 单价 | 金额 |`，用户明确说「无」才可跳过）。
- **📥 后补**：合同总价、签订日期、合同交期、下单付、收货付、验收付、实收、完货日期。

### 发货清单（新增）
- **必问**：相关项目（列近 1 个月）、类型（销售/借测/送样/维修）、发货内容。
- **发货内容必须 Markdown 表格**：`| 产品名称 | 固件版本 | 产品型号 | 产品数量 | ID号 | SIM卡号 |`。
- 借测→必问返还日期；维修→必关联维修记录（link）。
- 格式规则见 §7。

### 维修记录（新增）
- **必问**：相关项目（**列全部项目，不限近 1 个月**）、维修清单、问题描述、返修时间、要求交期（天数）。
- 完成时间/处理方法→后补。

### 库存核对记录（新增）
- 链接其他记录→link 生产计划（列近 1 个月）；最终完成时间→提取或问。

### PCB下单记录（新增）
- **必填**：PCB型号版本（优先从截图/文件名提取，形如 `⟨项目码⟩_板名_V1.0`）、打板数量(PCS数非SET数)、打板价格。
- 生产计划→必问（列近 1 个月）。
- **标准流程**：用户发打板截图(示例采购平台A/嘉立创)→AI 提取字段→确认后写入。
- **📥 后补**：打板数量、打板价格、交期(天)、最终完成时间、下单时间。

> **关于供应商**：供应商/贴片厂/组装厂**每家公司都不一样**，不在本文档里写死。
> 询问时的候选清单，**从该表历史记录里读现有取值**（`domain/op.py list <表>` 看「供应商」列的已有值）；
> 一条历史都没有就直接问用户。默认值可在 `config.yaml` 的 `defaults` 段配置。

### 外壳采购记录（新增）
- **填默认**：采购时间=今天。
- **必填**：外壳名称；数量/价格/交期/到货→后补📥。生产计划→必问。
- 供应商：列出历史已用过的，或问用户。

### IC采购记录（新增）
- **填默认**：状态=`未下单`。
- **必问**：物料名称、供应商（列历史取值供选）、生产计划。
- 下单时间未提及→留空后补（不默认今天）。

### 贴片生产记录（新增）
- **填默认**：状态=`待送料`。
- **必问**：贴片厂（列历史取值供选）、贴片数量、贴片价格、生产计划。
- 送料时间仅「已送料」才填；良品数量/完成时间/良品率说明/返料日期→后补📥。

### PCBA半成品采购记录（新增）
- **填默认**：状态=`未下单`(有描述按描述/谈判中)。
- **必填**：物料名称、生产计划。数量/花销/交期/到货→后补📥。

### 组装料采购记录（新增）
- **填默认**：状态=`谈判中`。
- **必问**：供应商（列历史取值供选）、采购花销、生产计划。交期/到货→后补📥。

### 组装记录（新增）
- **必填**：组装数量、组装价格（成本核算关键）。生产计划→必问。其余→后补📥。
- 组装厂：列历史取值供选，或问用户。

### 成品采购记录（新增）
- **填默认**：状态=`谈判中`、下单时间=今天(有描述按描述)。
- **必问**：物料名称、供应商（列历史取值供选）、数量、采购花销、生产计划。交期/到货→后补📥。

### 生产工序
- 工序主数据，被生产计划关联。13=09测试、14=10出货为默认工序。

### 资源（新增）
> 对标 MS Project 的「资源工作表」：先建档，再谈分配。
- **填默认（不问）**：类型=`人员`、在岗状态=`在岗`、单位=`人日`、日产能=`1`。
- **必问/提取**：姓名（人名 / 设备名 / 外协厂名）。
- **建议补**：所属工序（贴片/组装/测试…，用于按工序找人）、日费率（元，缺了就算不出人工成本）。
- 类型取值：`人员` / `设备` / `外协`；在岗状态：`在岗` / `请假` / `离职` / `维护中` / `停用`。
- **同名即更新**：`domain/op.py res-add` 对已存在的姓名执行覆盖更新，不会建重复档案。

### 资源分配（新增）
> 对标 MS Project 的「资源使用状况」：谁 · 在哪个计划的哪道工序 · 投入多少 · 什么时段。
- **填默认**：状态=`计划中`、单位=`人日`、开始日期=今天。
- **必问/提取**：资源（姓名）、生产计划、投入量、起止日期（可用「干 X 天」→ `--days X`）。
- 状态取值：`计划中` / `进行中` / `已完成` / `已取消`。**只有前两者计入负载**，`已完成` 仍计入人工成本。
- **写入时自动预检**：同一资源日期区间相交会打印「排程冲突」告警（不阻断，由人判断是改期还是换人）。
- 分配里出现、但资源表中无档案的人 → 驾驶舱标为 `未登记`，应补建档。

**资源域命令**：
```bash
python3 domain/op.py res-add 张三 --stage 贴片 --capacity 1 --rate 400   # 建档/更新
python3 domain/op.py alloc-add 张三 4G小卡二代 --stage 贴片 --qty 5 --days 5  # 排产
python3 domain/op.py res-load                                            # 负载报表（超载/闲置/冲突/成本）
```

**负载率口径**：本周（周一~周日）已排投入量 ÷（日产能 × 本周工作日数）。
>100% 判超载（红）、≥70% 繁忙（橙）、>0 正常（绿）、=0 且在岗判闲置（灰）。
驾驶舱会把「超载 / 排程冲突」升为**高优**行动建议，「闲置」为中优。

---

### 工作日志（新增）
> 「第二大脑」的记忆本体：你说的每句原话 + 系统的提取结果，**纯追加，永不修改**。
- **填默认**：日期=今天、类型=`进度`。
- 类型取值：`进度` / `决策` / `问题` / `变更` / `其他`。
- 每次经 `domain/op.py intake` 写入后都会自动落一条，无需手工维护。
- 原话必须原样保存：**结构化提取会出错，原话不会**。日后翻账只认原话。

### 阶段轨迹（新增）
> 谁在哪天把哪个项目推进到哪个阶段。用于回答「这个项目卡在打样多少天了」。
- **填默认**：日期=今天。自动计算「停留天数」（距上条轨迹的天数）。
- 每次 `domain/op.py stage --to` 成功后自动追加，不要手工写。

---

## 6.5 自然语言录入闭环（第二大脑，核心）

> 目标：你只管说人话，系统负责拆解、分级、确认、写入、留痕。
> **底线：重要数据必须列出来、你点头之后才执行。**

### 阶段 vs 工序（务必分清，这是两套东西）

| | 含义 | 取值 | 存在哪 |
|---|---|---|---|
| **阶段** | 产品生命周期位置 | 立项→研发→手板→打样→试产→小批量→批量→量产→交付→维修 | `项目.阶段` |
| **工序** | 板子在产线上的物理路径 | 库存核对/备料/贴片/组装/测试/出货/已交付 | `生产计划.阶段` |

一个处于「小批量」**阶段**的项目，其在制批次同时在走「贴片」**工序**。
`生产计划` 那列虽然也叫「阶段」，但存的是工序（历史列名，改名会破坏既有数据）。
**不要拿生命周期的值去写生产计划，也不要拿工序的值去写项目。**

### 你（AI）该怎么做

用户说一段进度（例："定位终端手板回来了，测试通过，下周转打样；另外这个项目交期客户要求推到 8 月 30"），你负责：

1. **拆成意图清单 JSON**（你做语义理解，Python 不猜人话）
2. `python3 domain/op.py intake 意图.json` → 系统分级并打印计划
3. **把系统打印的确认清单原样转述给用户**，等用户点头
4. 用户同意后 `python3 domain/op.py intake 意图.json --yes`

意图 JSON 格式：

```json
{
  "原话": "用户说的原始那句话，务必原样保留",
  "记录人": "老板",
  "intents": [
    {"op": "stage",  "table": "项目", "reason": "手板测试通过",
     "data": {"项目": "演示定位终端 DM-LOC1", "新阶段": "打样"}},
    {"op": "update", "table": "项目", "row_id": "row_1",
     "reason": "客户电话要求延期",
     "data": {"合同交期": "2026-08-30"}}
  ]
}
```

`op` 取值：`append`（新建）/ `update`（改）/ `stage`（推进阶段）/ `log`（只记不改）。
`update` 必须带 `row_id`——先 `domain/op.py list <表>` 拿到，**禁止凭印象猜行号**。

### 风险分级（系统自动判定，你无权绕过）

| 等级 | 什么情况 | 行为 |
|---|---|---|
| **自动** | 补备注/问题描述/良品率、写工作日志、合法阶段推进 | 直接写，事后可在工作日志回溯 |
| **需确认** | 改交期/金额/数量/单价、在采购表新建记录、阶段跳步或回退、覆盖已有值、任何删除 | **列出「改动前→改动后」，不加 `--yes` 一条都不写** |

**混合批次整批暂停**：一批里只要有一条高风险，低风险的也一并不写，
避免出现「写了一半」的中间态。这是刻意设计，不是 bug。

### 阶段命令

```bash
python3 domain/op.py stage                                  # 列出所有项目的当前阶段与停留天数
python3 domain/op.py stage 演示定位终端 --to 打样 --note 手板测试通过   # 合法推进 → 自动执行
python3 domain/op.py stage 演示定位终端 --to 量产 --yes       # 跳步 → 必须 --yes 才执行
```

跳步/回退不会被阻断（现实中确有加急和返工），但**必须让人看见**这个决定。

---

## 7. 发货内容格式规则（重要！）

- **≤20 张**：双表格（表格1 产品信息 + 表格2 每行一个 ID/SIM 卡号）。
  ```
  | 产品名称 | 固件版本 | 产品型号 | 产品数量 |
  | ---- | ---- | ---- | ---- |
  | DM-LOC1 演示定位终端 | V3.1 | DM-LOC1 | 6 |
  | ID号 | SIM卡号 |
  | ---- | ---- |
  | 85001200464 | |
  | 85001200467 | |
  ```
- **>20 张**：表格写「详见附件」，SIM 卡号列上传 Excel（ID 与 SIM 卡号对照表）。
- **多种产品**：每个产品一组表格，用 `---` 分隔。
- **ID 号字段**：≤3 个放单元格逗号分隔；>3 个必须换行每行一个。

---

## 8. 后补字段提醒（cron 字段清单）

每周五下午 2 点提醒填写下列 📥 字段（cron 在 WorkBuddy 自动化里建，本技能仅定义清单）：

| 表 | 字段 |
|----|------|
| 项目 | 合同总价、签订日期、合同交期、下单付、收货付、验收付、实收、完货日期 |
| PCB下单记录 | 打板数量、打板价格、交期(天)、最终完成时间、下单时间 |
| 外壳采购记录 | 数量、价格、交期(天)、到货时间 |
| IC采购记录 | 交期(天)、采购花销、到货时间、下单时间 |
| 贴片生产记录 | 送料时间、良品数量、贴片完成时间、良品率说明、返料日期 |
| PCBA半成品采购记录 | 采购详情、数量、采购花销、下单时间、交期(天)、到货时间 |
| 组装料采购记录 | 交期(天)、下单时间、到货时间 |
| 组装记录 | 组装数量、组装价格、成品完成时间、组装良品率、良品率说明、产品入库日期 |
| 成品采购记录 | 数量、采购花销、交期(天)、到货时间 |

> 用户说「忽略 XX 条/XX 字段」则跳过该条不提醒。

---

## 9. 分析功能（四维一体）

用户说「分析/统计/报表/看板」等触发。数据来源统一用 `domain/op.py list/query` 读取本地或 SeaTable 数据，**与后端无关**。

- **工时**：交期达成率(准时=完货≤合同交期)、生产周期(成品完成−立项)、工序耗时瓶颈、在制品看板(状态≠已完成，按剩余时间升序)。
- **成本**：项目利润(合同总价−生产总花销)、成本结构(PCB/外壳/IC/贴片/组装/成品占比)、预算vs实际、应收款(实收<合同总价)、现金流预测(30/60/90天)。
- **质量**：维修率(维修数/发货数)、良品率(贴片/组装)、返修时效、质量成本。
- **供应链**：采购逾期(到货>今天且未入库 / 下单+交期<今天仍无到货)、供应商交期准确率、供应商价格对比、库存预警。
- **综合看板**：「生成本周/本月/本季度看板」→ 输出工时+成本+质量+供应链四维摘要。
- **输出格式**：文字摘要(适合企微)、表格(对比)、图表(可视化)、详细报告(腾讯文档)。

---

## 9.5 项目矩阵板块（项目全表 / 生产计划全表 / 流程思维导图）

驾驶舱里除四维分析外还有一组「项目矩阵」板块，面向**项目经理（生产经理）角色**，默认进首屏：

| 板块 | id | 数据来源 | 说明 |
|---|---|---|---|
| 项目全表 | `sec-PT` | `model.projects_full`（项目表全字段） | 36 行 × 15 列，可搜索/按状态筛选/分页；末三列是关联的生产计划/发货/维修条数 |
| 生产计划全表 | `sec-PL` | `model.plans_full`（生产计划表） | 34 行，含状态/阶段/花费天数/生产花销/单片成本 |
| 流程思维导图 | `sec-MM` | `model.mindmaps` ← `data/思维导图.json` | 7 张 XMind 业务流程图，可折叠 SVG 水平树 |

角色注册见 `cockpit/cockpit.py` 的 `ROLES`：`production`（生产经理＝项目经理职责，`currentRole()` 默认值）
与 `boss` 可见；`warehouse` / `purchase` 按其「非项目经理」定位**不开放**。

### 思维导图怎么来的

业务方在 XMind 里维护流程规范（`Claw/xmind-download/*.xmind`，XMind Zen 格式 = zip + `content.json`，
注意 `content.json` 是一个 **list**（多 sheet）而不是 dict）。用 `tools/extract_mindmaps.py` 拍平成层级 JSON：

```bash
python tools/extract_mindmaps.py                       # 默认扫 Claw/xmind-download
python tools/extract_mindmaps.py <xmind目录> [输出json]  # 输出默认 data/思维导图.json
```

节点压缩成短键（`t` 标题 / `n` 备注 / `c` 子节点）以控制体积。**改了 XMind 要重跑此脚本 + 重跑 cockpit/cockpit.py**
（与 `data/微信事件.csv`、`data/核对结果.csv` 同一约定：`data/` 下的本地专用数据不走云端同步）。
缺少该文件时思维导图板块显示引导文案，不会报错。

前端 `mountMindmaps()`（`cockpit/cockpit.py`）自算树布局：x 按深度、y 按叶子顺序后序回填、父取首末子中点；
支持 7 图切换 / 节点点击折叠展开 / 搜索高亮 / 放大缩小。CSS 前缀 `mm-`。

---

## 9.6 ⚠️ 直连路径的两个静默错误（2026-09-14 修复，必读）

`cockpit/cockpit.py` 是**直连 SeaTable**读数据（`adapters/seatable.py` 的 `GET /rows/`），
而 `sync/seatable_sync.py` 是**走 CSV** 落盘。两者的单元格口径**不一致**，曾导致驾驶舱大面积静默出错：

| 现象 | 根因 | 修复位置 |
|---|---|---|
| 状态列显示 `58668` 而不是「已交付」；KPI 在产/计划/已交付**恒为 0** | `/rows/` 对 `single-select` 返回**选项 id**，不是选项名 | `adapters/seatable.py::_cell_meta()` + `_flat_cell()`：拉 `/columns/?table_name=` 的 `data.options` 建 `{id: 名}` 映射后翻译 |
| **甘特图为空**、`剩余（天）`为 null、交期达成率 N/A、现金流全 0 | 日期列是完整 ISO（`2026-05-07T00:00:00+08:00`），而 `_date()` 只 `strptime("%Y-%m-%d")` | `cockpit/cockpit.py::_date()`：先按 `T`/空格截到日期部分；并在 `adapters::_flat_cell()` 对 date 类型直接截 `v[:10]` |

排查口诀：**「驾驶舱数字明显不对」先比对 `data/*.csv`（同步路径，已是中文名/纯日期）与直连返回值。**
两者不一致就是这里漏翻了。

另注：
- 生产计划 `阶段` 是 `single-select`（可翻译）；项目 `阶段` 是 `link` 列。
  **⚠️ 口径已更新（2026-09-30）**：`/rows/` 对 link 列确实只给回 `display_value`（形如 `673602`），
  但**关联本身是可读的** —— 一个 link 列的值是 `[{"row_id": ..., "display_value": ...}]`，
  `row_id` 就在里面。要用 `adapter.list_linked(table, row_id, link_id)` 取回对方表行号，
  再按行号查对方表拿业务值。
  项目全表当前仍不展示该列，是**产品选择**（`display_value` 对人不友好），不是能力限制。
  - `link_id` **只存在于每个关联列的 `data.link_id`** 里（`GET /metadata/` 的
    `metadata` 顶层**没有** `links` 键，读它是空的）。本 Base 实测均为 4 位字符串 ——
    这是观察值、**代码里没有长度校验**，别当契约依赖。不确定时用 `adapter.link_columns(table)`
    列出该表所有关联列及其 `link_id`。
  - **同一对表之间可能有多条关联**（实测：`项目 ↔ 生产计划` 同时有 `3Fld` 与 `wana`），
    这时按「表名」解析 `link_id` 会报歧义 —— 必须用 `column=` 指定列名。
  - `link_id` 传空字符串给 `list_linked` = 返回该行在**所有**关联列上的对方 `row_id` 并集。
  - 🔴 **列 `type` 字符串大小写混用 —— 凡按类型分支一律 `.lower()` 比较**（2026-09-30 修）。
    实测本 Base：`link` 36 列但 **`LINK` 4 列**（`生产计划.工时记录` / `生产工序.工时记录` /
    `工时记录.关联生产计划` / `工时记录.关联工序`，link_id `W1Q1`/`Hh3j`）；
    `date` 40 列但 **`DATE` 2 列**（`工时记录.开始时间` / `结束时间`）；
    另有 `LONG_TEXT` 2 列、`NUMBER` 1 列；`link-formula` 9 列（**计算列、无 link_id**，不可真写）。
    - 关联列判定要**大小写不敏感 + 排除 `link-formula`**。踩过的坑：`_link_index()` 曾精确比较
      `!= "link"` → 4 个大写列被静默跳过，`known_link_ids()` 少 2 个，最糟的是
      `_assert_link_id_known("W1Q1")` **谎报「在本 Base 中不存在」**（守卫反过来拦合法值）。
    - 日期截断同理：`_flat_cell()` 曾精确比较 `date` → `DATE` 列**不截 ISO**，
      会留着 `2026-05-07T00:00:00+08:00` 而下游按 `YYYY-MM-DD` 写。
      （`工时记录` 表当前无数据，是**潜伏**坑，别等有数据才发现。）
    - 回归测试：`SeaTableLinkTypeCaseTests`（6 项）+ `SeaTableOtherTypeCaseTests`（3 项）。
- 甘特图需要「立项日期 + 交期」。实测 33 条计划里 **12 条没填合同交期**（多为合同写明「收款后 X 日内
  交货」、尚未收款故按规则留空）。
  **⚠️ 业主 2026-09-15 口径（覆盖旧的「一律不推算」）**：没填交期的 → **用历史数据推算**一个交期，
  照常画在时间轴上（`est=True`，虚线边框 + 名称后 `≈` 号 + tooltip 写明推算依据），不再沉底。
  推算 = 「立项日期 + 历史中位工期」；历史基线取**已交付计划的 `花费天数`** 分位
  （2026-09-15 实测 n=27 · 中位 **28** · p75 37 · p90 49 天，与 domain/foresee.py 的
  「立项→交货时间（自动记录）」口径逐值吻合）。`model["gantt_hist"]` / `gantt_est_days` 给出依据，
  `model["gantt_pending"]` = 推算条数；**推算值只读展示，绝不写回 SeaTable**。
- **进度取「生产计划.阶段」实测值**（业主 2026-09-15：「当前进度根据表格实际的数据，以每天晚上
  七点收集的各类数据为准」）→ 不再按日期推算（那是「今天该干到哪」，不是「实际干到哪」）。
  阶段 → 进度映射见 `cockpit/cockpit.py::STAGE_PCT`（最长键优先匹配，避免「组装料采购」被「组装」抢走）；
  已交付恒 100%，阶段认不出时按 0 低报。
- **所有列表统一「计划中」优先排序**（`cockpit/cockpit.py::status_rank` / `STATUS_ORDER`）：
  计划中(含暂放) → 暂停 → 进行中 → 其它 → 已交付。甘特图、生产计划全表、项目全表都用这一套。
  例外：应收口径差异明细表仍按**合同交期**排（那是金额核对表，不是进度表）。
- ⚠️ **适配器坑（2026-09-15 修）**：SeaTable 的 `ctime`/`mtime` 类列**不按列 key 回传**，而是行级
  `_ctime`/`_mtime`（这类列的 key 恰好就是 `_ctime`/`_mtime`）。生产计划表的
  「交货时间（自动记录）」就是一条 **mtime 列**、`创建时间` 是 **ctime 列**。
  `adapters/seatable.py::list_rows` 早先无条件 skip 这两个 key → **直连路径永远读不到这两列**
  （CSV 路径有，导出接口带真实列名）→ `domain/foresee.py` 的历史分位在直连下会**静默全空**。
  现改为「只要该 key 在列定义里对应业务列就照常映射回列名」。
- 生成器会读 `_sync_meta.json` 算「数据是否过期」。若只跑了 `cockpit/cockpit.py` 没跑 `sync/seatable_sync.py`，
  徽标会显示过期（纯提示，不影响直连数据的实时性）。
- `main()` 开头 `sys.stdout.reconfigure(encoding="utf-8")`：Windows 控制台默认 GBK，
  末行 `print` 的 `¥` 会抛 `UnicodeEncodeError` → 生成器**退出码=1**（HTML 其实已写好），
  自动化会误判成失败。报错修了，但**跑生成器时仍建议带 `PYTHONIOENCODING=utf-8`**。

---

## 9.7 应收口径对照（`model["recv_check"]`）— 两个数为什么不一样

驾驶舱里同时存在两个「欠收」数字，来源不同，**必须并列展示 + 说明差异**，否则看的人只会犯迷糊：

| 数字 | 来源 | 位置 |
|---|---|---|
| **待收**（表内） | SeaTable「项目」表的**公式列**，直接汇总 | 项目全表脚注 + 应收 KPI 副标题 |
| **应收**（看板） | `sum(合同总价) − sum(实收)`，看板**现算** | KPI 主值 |

两者本应逐项相等，实测差 **¥150,043**。`compute()` 里 `recv_check` 逐项比对（容差 ±1 元），
只列**对不上**的行，并按差额绝对值降序：

```python
recv_check = {"kpi": 应收, "stored": 表内待收合计, "diff": stored - kpi, "rows": [...], "n": len(rows)}
# rows[i] = {no, name, status, contract, received, stored, calc, diff, why}
```

`why` 自动生成两类成因文案：两边都 >0 判「元级舍入误差」；否则判「表内待收为空/0，实际仍有未收 ¥X」。
前端 `sec-PT` 里用 `<details class="recv-diff">` 折叠展示（默认收起，不占版面）。

**2026-09-15 实测 3 条对不上**：`20260914-003` 客户E ¥146,000、`20260519-001` 客户A防爆 ¥4,045
（两条都是**待收列为空/0 但实际仍有未收**，属公式列未覆盖新行 / 未重算）、`20260413-001` 某项目 ¥2 舍入。
→ 建议到 SeaTable「项目」表核对「待收」公式列。

> 口径选择：**不替业主决定**用哪个数。两个都摆出来 + 说清差额来源，让人自己核。

---

## 9.8 对内共享页（`cockpit/build_share.py`）— 给同事看的精简版

`cockpit/cockpit.py` 的产物含财务金额、成本、供应商、微信情报与口令门禁，**不能直接发同事**。
`cockpit/build_share.py` 复用同一个 `cockpit.compute()`（保证两页不会各说各话），只抽出三类内容
重排成一份**独立单文件 HTML**（约 35 KB，内联 CSS/JS，零外部依赖）：

| 板块 | 内容 | 刻意排除 |
|---|---|---|
| KPI 条 | 项目总数（进行中/计划中/已交付）、生产计划（未交付/已交付）、交期达成率、待补交期 | 任何金额 |
| 生产计划甘特图 | 立项→交期、月份刻度、今日红线、4 态筛选 + 搜索、待补交期虚线组 | — |
| 项目进度 | 编号/项目/状态/合同交期/剩余逾期天数/关联单据数 | 合同总价、已收、待收、生产花销 |
| 生产进度 | 计划编号/产品/数量/状态/阶段/关联项目/立项/交期/花费天数/批次号 | 生产总花销、单片成本 |

```bash
python cockpit/build_share.py                    # 默认输出 <本目录>/项目进度共享页.html
python cockpit/build_share.py <输出路径>          # 或 SHARE_OUT=... python cockpit/build_share.py
```

**安全护栏（改字段时务必守住）**：`build_model()` 是**白名单**式重建——只挑显式列出的键，
不是「把 model 里敏感键删掉」。新加字段要主动决定是否暴露。验收脚本务必包含
「金额数值泄漏检查」（拿完整驾驶舱的合计金额去共享页里搜，必须搜不到）。

发布：把生成的 html 复制成某目录的 `index.html`，用发布技能 `workbuddy_sites_deploy` 部署，
`appName="生产进度看板"`、`language="static"`。发布是一次对外释放，**每次都要用户当轮明确要求**。

---

## 10. 采购库存核对

采购流水线通过 `pipeline/run.py audit <run_id>` 使用 `inventory.source` 生成库存审核表：

- `partdb`：逐个命中料号读取 PartDB 批次，按确认日期区分可承诺与未确认库存。
- `api`：通用 REST/HTTP 连接器，支持鉴权、GET/POST、分页、响应路径与嵌套字段映射；可接金蝶、简道云、禅道及客户自建 ERP。
- `mcp`：通过 MCP stdio 调用客户现有库存工具，读取 structuredContent 或文本 JSON，并按统一字段映射。
- `file`：读取 Excel/CSV，作为离线系统或 API 暂不可用时的兜底。
- API/MCP/文件中的通用“库存”默认归入未确认库存；只有 `stock_is_confirmed: true` 或显式“已确认库存”字段才会抵扣缺口。
- 审核人员必须在 `库存审核表.csv` 复核库存、供应商、采购数量与单价，并将采购行标为“已批准”。

PartDB 专用的查询命令仍可选用：

```bash
python3 domain/op.py partdb-search <关键词> [数量]
python3 domain/op.py partdb-shortage <项目ID> <生产数量>
```

**价格导入（采购合同 PDF → PartDB）**：批量把合同型号与含税单价录入 PartDB 的流程——PDF 文本提取、全量零件结构化匹配、Hydra API 写价的关键坑（派生字段 / PATCH 内容类型 / MOQ 唯一约束）、以及新建料号的 IPN 命名约定——见 `references/price-import.md`。

---

## 11. 辅助命令速查

| 命令 | 作用 |
|------|------|
| `domain/op.py list <表>` | 列出某表全部行 |
| `domain/op.py query <表> --where 列=值` | 条件过滤 |
| `domain/op.py append <表> '<json>'` | 新增，返回 row_id |
| `domain/op.py update <表> <row_id> '<json>'` | 修改 |
| `domain/op.py delete <表> <row_id>...` | 删除 |
| `domain/op.py link <A> <B> <A_id> <B_id>...` | 双向关联 |
| `domain/op.py linked <表> <row_id>` | 查看某行关联 |
| `domain/op.py meta <表>` | 查看表结构 |
| `domain/op.py resolve-link <A> <B>` | 显示语义关联标识 |
| `domain/op.py export-excel [文件]` | 全部表导出为 Excel |
| `domain/op.py partdb-search / partdb-shortage` | PartDB 缺料（可选） |
| `domain/op.py --base production ...` | 显式操作生产业务 Base |
| `domain/op.py --base tasks ...` | 显式操作待办事项 Base |
| `domain/evidence.py register-image <图片> ...` | 登记图片证据元数据；允许后续确认上传，但此命令本身不上传 |
| `domain/evidence.py scan --days 90` | 季度扫描清理候选，不删除 |
| `domain/evidence.py prune --days 90 [--yes]` | 无 `--yes` 只预览；显式确认才删除 |

> 完整业务流程速查、BOM 成本算法、分析公式明细见 `references/` 目录。

### 深度速查（按需 Read，见 references/）

| 干什么 | 读哪个文件 |
|---|---|
| 拉群消息 / 消息↔SeaTable 核对（wxmatch v1.9）/ 风险预测（foresee） | `references/wx-intake-and-check.md`（原 §11.1~11.5） |
| **微信图片解密 + OCR（`wx/wxmedia.py`）** | `references/wx-intake-and-check.md` **§11.4.1.1**（V0/V1/V2 格式、密钥派生、命令、实测） |
| **新供应商群自动纳入（`wx/wx_watchlist.py`）** | `references/wx-intake-and-check.md` **§11.4.3**（三步机制、三前提、维护纪律） |
| 物料·原料查价（market/suppliers/commodities） | 独立子技能 **price-sensor**（命令卡+执行纪律；脚本与数据仍在本技能目录，勿复制） |
| CRM 线索赢单转生产立项（domain/won_deal.py） | `references/won-deal.md`（原 §11.6） |
| 驾驶舱在线模式（cockpit_server）/ 表格工具·甘特交互·分析图表 / v2.0 架构收口 / 客户到售后业务闭环 | 独立子技能 **cockpit-studio**（命令卡+执行纪律；深读 `references/cockpit-advanced.md`） |
| 查功能来历 / 历史设计决策 / 踩坑细节 | `references/changelog.md`（完整版本历史） |

## 12. 在自定义脚本里调 adapter（踩坑记录）

`domain/op.py` 内部会自己调 `adapter.auth()`，**但外部脚本不会**。直接 `get_adapter(cfg).list_rows(...)` 会报
`MissingSchema: Invalid URL 'None/api/v2/dtables/...'`，因为 `self._server` 要等 `auth()` 之后才有值。
正确写法：

```python
import sys; sys.path.insert(0, r'<技能目录>')
from adapters.factory import load_config, get_adapter
a = get_adapter(load_config(None)); a.auth()      # ← 这行不能少
rows = a.list_rows('发货清单')                     # 返回 dict 列表，含 __row_id__
```

**为什么值得自己写脚本**：`domain/op.py list` 是 TSV 输出，`long-text` 列（如「发货内容」）里的换行会把一行拆成多行，
没法解析出完整记录；需要读/比对整条记录时走 adapter 拿 dict 更可靠。

**single-select / multiple-select**：`list_rows` 返回的是**选项 ID**（如发货清单「类型」的销售=`909350`），
`domain/op.py meta` 的 `data.options` 又是 `None`。要拿 ID↔中文名映射，从 metadata 直接读：

```python
a._ensure_meta()
tbl = next(t for t in a._meta['tables'] if t['name'] == '发货清单')
opts = {o['id']: o['name'] for c in tbl['columns']
        if c['type'] == 'single-select' for o in c['data']['options']}
```

写入时**直接传中文名即可**（adapter 内部会翻译），不要自己查 ID 再传。

**⚠️ 写入后必须读回验证**（这是本次最大的教训）：

```python
a.update_row("成品采购记录", rid, {"交期（天）": 39})
# 必须重新读一次确认，不能只看有没有报错
```

`PUT /rows/` 在传错列标识（列 key 而非中文列名）时会返回 `{"success": true}` 且 HTTP 200，**数据却不落库**——
`domain/op.py update` 因此会打印 `OK`，让你以为写入成功。凡是走 adapter 的写操作，写完后用新的 adapter 实例
`list_rows` 读回比对一次；只有读回值等于写入值才算真的成功。

---
