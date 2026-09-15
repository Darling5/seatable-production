# 微信情报 · 行情查价 · 交叉核对 · 风险预测 —— 命令详解

> 从 SKILL.md §11.1~11.5 下沉。涉及拉群消息、物料/原料查价、消息↔SeaTable
> 核对（wxmatch）、风险预测（foresee）时阅读本文件。

### 11.1 微信情报命令速查（`wechat_intake.py`）

先跑 `doctor` 自检（**用装了 cryptography 的那个解释器**，缺包会报误导性的「引擎A不可用」）。
自检通过的标准是出现 `数据库：19 个，已解密 19 个`，而不是任何 `[!]`。

```bash
python wechat_intake.py doctor      # 体检：找库 / 提密钥 / 给指引
python wechat_intake.py groups      # 列全部群聊（挑监控对象）
python wechat_intake.py pull        # 增量事件流：书签续读 -> 微信事件.csv 待确认
python wechat_intake.py summary     # 群聊摘要：按时间窗口回溯对话（见下）
python wechat_intake.py list --status 待确认
python wechat_intake.py approve <编号>   # 确认事件并写 SeaTable 云端
```

**`summary` 群聊摘要**（替代已失效的 WeChat-Summary 类 GUI 工具）：

```bash
# 单个/多个群，72 小时窗口
python wechat_intake.py summary --group "生产协调群" --hours 72
python wechat_intake.py summary --group "采购群A,生产群B" --hours 168

# 不指定 --group = config.yaml 的 watch_groups；--all = 全部群
python wechat_intake.py summary --hours 24
python wechat_intake.py summary --all --hours 24 --limit 5000

# 写文件 / 输出 JSON（供 AI 直接消费）
python wechat_intake.py summary --hours 24 --out data/wechat_intake/summary_24h.md
python wechat_intake.py summary --group "群名" --hours 72 --json --out sum.json
```

参数：`--group` 可多次指定也可逗号分隔、**支持部分匹配**；`--hours` 时间窗口（默认 24）；
`--limit` 每群最多扫描条数（默认 2000，触顶会在输出里提示可能截断）；
`--keep-empty` 保留 0 条消息的群章节（**默认折叠**为末尾一行汇总）。

> **空群折叠**：不给 `--group` 时覆盖全部 `watch_groups`（实测 31 个），其中绝大多数
> 当天无消息。默认把它们折叠成末尾一行「另有 N 个监控群在该窗口内无消息：…」，
> 实测 422 行 → 288 行（省 32%）。JSON 输出不受影响，始终包含全部群。

输出结构：时间窗口与统计 → 每群发言分布 → **需关注**（按交期/价格/供应/决策/风险
五类关键词自动标注，避免人工翻全量）→ 完整对话记录（正序，含图片/文件等非文本类型标注）。

**与 `pull` 的分工**：`pull` 是增量事件流（书签续读、只收文本、进「待确认」队列、可写业务表）；
`summary` 是一次性回溯（按小时窗口、保留全部消息类型、供人工或 AI 速读）。两者不冲突。

**已踩过的坑（别再改回去）**：
- 发言人名解析要走「**别名映射 → 群成员表 → `wdb.get_nickname()` 兜底」三级**。
  别名映射（config.yaml `wechat.sender_aliases`，wxid → 显示名）优先级最高，
  用于同一人多账号/多昵称的场景（实例：`jixu911: 刘俊良`——其昵称 Dylan-刘，
  企微互通号却显示刘俊良且从不发言，两个化身靠别名聚合成一个人）。
  只用 `member_names.json` 缓存会漏掉非好友/不活跃成员，摘要里显示成一串 `wxid_xxx`。
- 微信把发送者账号冗余拼在正文前，分隔符是**换行不是空格**（`"wxid_xxx:\n正文"`，全角冒号也见过），
  必须剥掉，否则每条都显示成「昵称：helei270640: 正文」。
- `type == "系统消息"` 和 content 为 `[文本]`（解析失败的空占位）要过滤，否则混进撤回提示等噪声。

#### 11.1.1 群聊 AI 总结（脚本取数 + AI 提炼）

`summary` 只负责**取数和结构化**，真正的"总结"由 AI（你）完成——**不需要接任何 LLM API，你就是 LLM**。
完整提示词模板见 `references/wx-ai-summary-prompt.md`，核心要求：

- 输出结构：一句话结论 → 待办事项表（**事项|负责人|提出时间|状态**）→ 决策共识 → 分话题要点 → 风险 → ❓ 悬而未决
- **待办必须带负责人**，从 @提及、指派语句（「明天一早查一下」）推断
- **必须单独列出「提问后无人回复 / 被追问仍未闭环」**——这是人工翻聊天最容易漏的，也是这一步最大的价值
- 消息数 < 5 条直接跳过；超 300 条先分段摘要再合并（map-reduce）
- 不臆造，每条结论可追溯到原文

参考耗时：全量监控群（31 群）扫 24 小时窗口约 **3 分 20 秒**，每日任务可接受。

长中文正文入队一律用 `notify.py send --body-file <文件>`，**不要用 `--body` 命令行传参**
（Windows 命令行有长度上限，且引号转义会毁掉内容）。

```bash
python wechat_intake.py summary --hours 24 --out data/wechat_intake/summary_24h.md
# → AI 读文件生成 data/wechat_intake/ai_summary_24h.md
python notify.py send --subject "💬 群聊总结 <日期>" \
    --body-file data/wechat_intake/ai_summary_24h.md --level info
```

> 每日 9 点自动化已内置此流程，产物随发件箱一并推企微。自动化还必须把微信候选
> 标为 `production` / `tasks`，但只生成待确认清单，不运行 `approve`。完整模板见
> `automations/README.md`。

#### 11.1.2 双 Base 分流、附件与证据保留

- `production`：项目、订单、收付款、交期、采购、库存、生产、发货、质量等业务事实。
- `tasks`：负责人、截止日期、提醒、催办、追问、未闭环问题等执行事项。
- 一条消息同时含业务事实和行动要求时拆成两条候选；写入前分别读取目标 Base metadata，
  完整展示目标 Base、目标表和字段，等用户确认。CLI 显式选择方式为
  `python op.py --base production ...` / `python op.py --base tasks ...`。
- 图片允许在确认后上传为证据；默认先登记本地路径、OCR/视觉摘要、来源、时间、哈希。
  普通文件先文本化，失败或必须看版式/签章时才考虑上传原件。
- 季度运行 `python evidence.py scan --root data/wechat_intake --days 90 --json` 只列候选。
  仅超过 90 天、已闭环、非长期保留的证据入选；明确批准后才运行
  `python evidence.py prune --root data/wechat_intake --days 90 --yes`。没有 `--yes` 不得删除。

### 11.2 物料行情与代理商查价速查（`market.py` / `suppliers.py`)

```bash
python market.py watchlist [--refresh]        # 生成/刷新监控清单
python market.py report                       # 最新行情 vs 上次采购价
python market.py alerts                       # 涨跌超阈值 / 停产·NRND 告警

# 代理商自动查价（得捷 DigiKey / 贸泽 Mouser）
python suppliers.py doctor [--live]           # 凭证自检（脱敏）；--live 联网探测
python market.py lookup <型号> [--qty 100]    # 只查不写
python market.py compare <型号>               # 多源比价，标出最低/最高/差价
python market.py sync [--dry-run] [--limit N] [--force]   # 按节奏批量拉价写快照
```

**执行顺序建议**：用户要行情/比价时，先 `doctor` 确认凭证在位 → `lookup` 或 `compare` 看即时价
（**只读，不写任何文件**）→ 确认无误才 `sync` 写快照。`sync` 会真写 `data/物料行情记录.csv`，
属于写操作，**给用户看预览更稳妥时先跑 `--dry-run`**。

**自适应节奏**：按库存电子料数量（取 `data/partdb_snapshot.json` 的 `part_count`）
自动选复查间隔——≤100 种每 7 天、101~300 种每 15 天、>300 种每 30 天，
逐型号比对上次快照日期，未到期跳过以省 API 配额。当前 369 种 → 每月一次。

**涨跌幅口径（易错）**：有渠道时**只跟同渠道的上一条比**。多渠道各写一条快照时，
若拿得捷价去比贸泽价，渠道差价随随便便超 10% 阈值，会刷出假涨跌告警。

**三道省配额闸门**（`sync` 依次过滤）：① **本地预筛** —— `P1`/`Z3.5`/`458*3`/`26MHz/0.5ppm`
这类内部料号与规格描述不是制造商型号，永远查不到，本地判掉（实测拦 7/25）；
② **未知缓存** —— 近 30 天确认「查无此型号 / 无报价」的型号记进
`data/.cache/unknown_mpn.json`，TTL 内不再查（实测再拦 17/25，第二轮只剩 1 个发请求）；
③ **节奏到期** —— 上次快照未超复查间隔则跳过。缓存最多挡 30 天，`--force` 随时绕过。

**空价不写库（易错，2026-09-02 踩到）**：命中但 `price_cny` 为空的**绝不写快照**。
一旦写入，这条空价记录会成为该型号的「上次快照日期」，后续按节奏判定时被当作已复查而跳过——
等于用一条废记录把该型号在整个复查间隔内屏蔽掉。例外：生命周期已是 NRND/EOL 的照写，
停产预警比价格重要。

**⚠️ 适用性边界（务必先跟用户说清）**：得捷/贸泽是**欧美代理商**，对国产料与定制料号
基本零覆盖。实测本仓库 25 个启用型号（FR8018HD 奉加微、ML307N 中移物联、OM6626B 等国产芯片为主），
贸泽**有效命中 0 条**。正确用法是**选型阶段查新料号**（`lookup`/`compare` 打价、查库存、查生命周期），
国产 BOM 的行情巡检仍以人工录入与立创商城为主 —— 别让用户误以为接上就万事大吉。

**得捷 V4 vs v3（2026-09-02 实战定案）**：App 状态 Approved ≠ 能调通 —— **代码调的端点
必须在 App 的订阅列表里**（My Apps 页右侧 APIs 列表）。实测用户的 App 订阅的是
ProductInformation V4（非 Search v3），调 v3 一律 401 "not subscribed"，改调 V4 后秒通。
默认用 **V4 keyword 端点** `POST /products/v4/search/keyword`（价 + 库存 + 中文生命周期
全有）；**别用 productdetails 端点**——2-legged 下库存恒 0。语言码差异：v3 用 `zh`，V4 用
`zhs`（config 写 zh 会自动映射）。判别仍保留 `_dk_subscription_hint()`：令牌 200 但查询
401 "not subscribed" = 端点不在订阅列表，给出指引且不重试。`api_version: v3` 可切回旧端点。
前缀变体命中：查 `2SK3541` 会命中 `2SK3541T2L`（T2L 是包装编码），desc 加 `[近似命中→...]`
前缀，不会误报精确匹配。

**凭证纪律**：得捷/贸泽凭证只存本地 `config.yaml` 的 `market.api_keys`
（已被 `.gitignore` 排除）。报错与日志里**一律脱敏**（只显示前 4 位），
**禁止把 key 写进 skill 文件、仓库或对话输出**。仓库里只有 `config.yaml.example` 的空占位符，
从 GitHub clone 的人自行去官网申请。

**只读铁律**：这批凭证只用于查价与生命周期查询，**绝不调下单/报价/购物车接口**，
下单必须人工在官网完成。

### 11.3 原料行情速查（`commodities.py`，统一入口 `market.py raw`）

跟**上游原料**价格，跟元器件行情是两条线：元器件看的是某个型号贵了还是停产了，
原料看的是**金/银/铜/锡和石油衍生品的整体成本走向**——它决定一批料未来几个月的报价基调。

```bash
python market.py raw list                      # 列出 12 个品种与各自数据源
python market.py raw fetch [--dry-run] [--only AU,CU]   # 拉实时价写库（一次取全部品种）
python market.py raw show  [--days 30]         # 走势表（含 sparkline 字符走势条）
python market.py raw trend [--days 30]         # 波动告警，超阈值高亮
python market.py raw add ABS --price 11800 --date 2026-09-03 --note "东莞现货含税"
python market.py raw backfill AU --contract au2610 --days 40   # 补历史 K 线
```

**三层数据，别混为一谈**：

| 层 | 品种 | 数据源 | 口径 |
|---|---|---|---|
| 全自动 | 金/银/铜/锡/铝/镍 | 新浪期货实时（**无需 key**） | 连续合约 |
| 代理指标 | 聚丙烯 PP / 聚乙烯 LLDPE / PVC | 新浪期货实时 | 连续合约，**代理** |
| 人工录入 | ABS / PC / PS 树脂 | 无免费 API，人工填 | 现货 |

**⚠️ 塑料这块必须如实跟用户说清**：ABS/PC/PS 是石化下游**现货**，被生意社、卓创、
中塑在线几家垄断，**没有公开免费 API**。本版**不编造数据**，改用 PP/LLDPE/PVC 期货
作**上游石化链代理指标**（口径字段标「塑料代理」）。以后接了同花顺 iFinD 再补现货，
架构已留 `source` 适配器位，不用返工。

**⚖️ 同口径环比（最重要的一条，易踩）**：期货「连续合约」（`AU0`）和「具体合约」
（`au2612`）是**两个口径**，环比时只跟**同口径的上一条**比。拿连续价去比某个具体合约价
会算出假涨跌——这跟元器件「串渠道假涨跌」是**同一个坑的两种形态**。口径写进 CSV 的
`口径` 列，`backfill` 补完历史会用 `_recalc_pct()` 按日期排序重算，保证口径内自洽。
`show` 里同一个品种出现两行（连续 + 某合约）是**正常的**，不是重复数据。

**数据源细节（踩过的坑）**：
- 新浪实时：`https://hq.sinajs.cn/list=nf_XXX0`，**必须带 `Referer: https://finance.sina.com.cn`**，
  返回 **GBK** 编码，一次请求能取回全部品种（省请求）。字段位：`[8]` 最新价、`[17]` 日期。
- **新浪日 K 线接口已下线**（返回 `{"__ERROR": 3, "__ERRORMSG": "Service not found"}`），
  历史改走东方财富 `push2his.eastmoney.com/api/qt/stock/kline/get`。
- 东财 secid 格式 `市场码.合约代码`：**113 上期所 / 114 大商所 / 115 郑商所**。
  别手拼（`113.AU0` 返回 0 条），用 `searchapi.eastmoney.com/api/suggest/get` 搜出
  正确合约名（如 `au2610` + `MktNum=113`）。

**隐私纪律**：这个模块**不存任何 API key**——新浪实时与东财 K 线都是公开行情接口。
但 `add` 人工录的现货价是你的**采购成本**，CSV 落 `data/` 目录，已被 `.gitignore` 排除，
**不进 GitHub**。写演示文档举例时一律用公开市场行情，并明确标注「示例数据，非公司数据」。

**驾驶舱联动（`cockpit.py` `_load_commodities`）**：`data/原料行情记录.csv` 有数据时，
驾驶舱自动出「原料行情（上游成本）」模块（老板/采购页可见）——最新价、区间涨跌、SVG 走势、
口径徽章。**阈值与回看天数从 `commodities` 段读，与 CLI 同一真源**；告警按波动幅度降序，
前 2 条进「下一步行动建议」（中优，cat=market）。行动建议只带 2 条是有意的——原料波动
是成本信号不是停线事件，全量塞进待办会淹没真正的紧急事项。

**现货源适配器（`_spot_adapter` / `_fetch_spot`）**：ABS/PC/PS 这类没有免费公开源的品种，
`fetch` 会明确打印「用 raw add 人工录入」而不是静默跳过。将来拿到付费授权（生意社/卓创/
中塑在线/同花顺 iFinD），在 `_spot_adapter()` 返回表里注册一个 `{key: 取价函数}`、
`config.yaml` 填 `commodities.spot_source` 即可自动补录，CSV 结构与展示层不用动。
**注意**：只填 config 不注册实现，fetch 会明确报「尚未实现该适配器」，绝不假装查过。

### 11.4 消息↔SeaTable 核对速查（`wxmatch.py`，v1.9）

微信情报（11.1）是「**提取事件**」，核对引擎是「**逐条对账**」：把群消息里的收款、
新下单、合同 PDF、到货/发货信号与 SeaTable 业务表**逐条匹配**，发现「群里说了但表里没有」的缺口。

```bash
python wxmatch.py scan [--days 3] [--pdf-days 60] [--no-write]   # 四类扫描，写本地核对台账
python wxmatch.py list [--status 待确认]                          # 查看核对台账
python wxmatch.py apply --dry                                     # 预览可自动闭环的高置信项
python wxmatch.py apply                                           # 显式写入高置信项并留痕
python wxmatch.py done WX-M-20260903-001 [--note "已登记"]        # 标记人工处置（留痕）
python wxmatch.py intent WX-M-20260903-001                        # 导出某项预填意图
```

**五类核对与匹配规则**：

| 类型 | 信号来源 | 匹配目标 | 规则 |
|---|---|---|---|
| 收款 | 群消息（已打款/到账等 20+ 词 + 金额） | 项目表「待收」 | 金额 ±2% 容差 → 高置信，生成回款预填意图 |
| 下单 | 群消息（下单/新订单/返单等） | 项目表客户名 | 匹配不到 → 提示可能漏立项 |
| 客户合同 | 微信磁盘文件名（明文 PDF 名） | 项目表「合同」列 | 客户名+产品词双维度，**≥2 词重叠**才命中 |
| 供应商合同 | 同上 | 5 张采购记录表供应商 | `SUPPLIER_ALIASES` 别名归一（处理合同与表里一字之差），金额差 >5% 警示 |
| 到货/发货 | 群消息（到货/签收/已发货/物流单号/已出库等，问句与提醒语排除） | 采购记录表「已下单/已付款-未到货」在途行 | 供应商名（含别名）唯一命中 → 高置信，生成「状态→已到货+到货时间」意图；多条在途或仅物料词 → 中置信 |

**数据源真相（微信 4.x）**：type=49 文件消息的数据库 content 是加密容器解不出 XML，
但磁盘 `msg/file/月份/` 下**文件名是明文**——核对引擎走磁盘扫描，这是唯一可靠数据源。
附件（`附件二…`）与重传后缀（`(1)(2)`）会被正则排除、canon 去重。

**我方主体识别**：文件名含自家公司名（销售合同以我方名义开出）时，客户名在 PDF 内文、
文件名识别不出 → 归低置信待人工，**不刷「漏立项」误报**。

**写库边界（v1.9，2026-09-15 用户决策）**：`scan` 始终只读业务表，只写本地
`data/核对结果.csv` 台账；**自动写库只发生在显式 `apply`**，且只处理同时满足
「状态=待确认、置信度=高、有预填意图」的记录——高置信（金额 ±2% 唯一收款匹配 /
供应商名唯一的在途到货）自动写，中低置信保持待确认只在复盘播报里列清单。
写入成功台账回填「已自动写入」，失败回退「待确认」（幂等：已处置行永不重写）；
`apply --dry` 只预览不落库。写库复用 `wechat_intake.py` 的 Intent + adapter 链路。
**每晚 19:00「当日复盘」自动化**（A 风险雷达 / B 群聊日报 / C 四类交叉核对+高置信 apply /
D 合并推送企微 / E 播报）消费本命令。

**驾驶舱联动（`cockpit.py` `_load_wxmatch`）**：`data/核对结果.csv` 有数据时，驾驶舱
自动出「消息↔SeaTable 核对台」模块（老板/生产/销售页可见）——待核对表（类型/置信度
徽章、匹配项目、建议动作）、分类与置信度分布、处置指引。高置信收款进「下一步行动
建议」红字置顶（cat=wechat），中置信有匹配项目的项带 3 条登记建议。

### 11.5 风险预测速查（`foresee.py`）

```bash
python foresee.py                     # 三路预测重算 + 写 data/foresee.json + 落预测台账 + 终端摘要
python foresee.py --json              # 只输出 JSON（调试用）
python foresee.py review              # 预测复盘：台账预测 vs 实际交货 → 准度报告（预警准确率/误报/漏报）
python foresee.py ask <编号|产品|供应商|类别>   # 对话式追问：某计划的风险细节/环节最晚开始日/供应商画像
python foresee.py log                 # 只更新预测台账（不重算）
```

**三个计算模块**（全部只读，数据源均为 data/ 本地快照）：

1. **合同倒排**：在制计划的剩余天数 vs 已交付计划真实工期分位（中位/p75/p90），
   输出 已逾期/高风险/偏紧/正常 四档 + 各环节最晚开始日。新合同一进来就能看
   「哪些环节必须立刻执行」。
2. **供应商交期画像**：5 张采购表承诺 vs 实际偏差，按类别/供应商出 buffer 建议
   （组装料平均 +29 天最不稳，IC 最准）。缺料 ETA 计算自动套用类别 buffer。
3. **缺料预警**：PartDB BOM 缺口 × 在途采购 ETA → 必须立刻下单 / 在途来不及 /
   在途可覆盖 三档结论。

**预测台账与复盘（自我学习闭环，`data/预测台账.csv`）**：每次运行自动落当日
预测快照（同日同计划去重覆盖）；`review` 子命令把已到期/已交付的预测与实际
对照——预警且真晚了=预警正确、预警但没晚=误报（可容忍）、判「正常」却晚了=
**漏报**（最伤，逐条点名）。预警准确率随台账积累逐月可信。

**刷新链路**：`seatable_sync.py`（业务表）→ `partdb_sync.py`（BOM/库存）→
`foresee.py`（预测+台账）→ `cockpit.py`（驾驶舱）。驾驶舱「风险雷达」section
（sec-FC，老板/生产/采购页）消费 `data/foresee.json`；已逾期/高风险项自动进
「下一步行动建议」。每日 9 点自动化跑 `foresee.py` + `foresee.py review`。

---

