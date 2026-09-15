# -*- coding: utf-8 -*-
"""audit_wx_coverage.py — 业务实体 ↔ 微信群 覆盖自查（只读）

回答的问题：
  **SeaTable 里的业务实体，在微信监控白名单（`config.yaml → wechat.watch_groups`）里有对应群吗？**
  实体分**三类**（业主 2026-09-15 明确）：
    类 1 供应商            —— 采购表「供应商」+ 贴片厂 / 组装厂（原脚本只查这一类）
    类 2 客户              —— 项目表「项目」列（格式 `客户（渠道）` 或 `客户-产品说明`）
    类 3 客户的技术服务/售后 —— 群名带 售后/技术对接/技术交流/证书/标书/部署… 且挂客户或供应商

背景（详见 references/wx-intake-and-check.md §11.4.1 / §11.4.2）：
  `pull` 只采 `watch_groups` 里的群，且匹配规则是「群名精确 / 群ID / **子串**」三选一
  （`wechat_intake.py` 的 `_wx4_pull:267`、引擎B `pull:517`、`summary:850` 同一谓词）。
  所以白名单里既可写完整群名，也可写「振道」「采购」这种关键词。
  ⚠️ 群在微信库里存在 ≠ 会被采集 —— 这类「台账有、群没说」的漏采，第一步就要查本表。

用法：
  python audit_wx_coverage.py                    # 直连 SeaTable（实时），三类全查
  python audit_wx_coverage.py --csv              # 用本地 data/*.csv（离线，可能落后云端）
  python audit_wx_coverage.py --only customer    # 只查一类：supplier | customer | service
  python audit_wx_coverage.py --min-records 3    # 供应商侧只列记录数 >= 3 的
  python audit_wx_coverage.py --groups           # 额外列出「实际会被监控的全部群」
  python audit_wx_coverage.py --buckets          # 把 386 群按五桶分类打印
  python audit_wx_coverage.py --excluded         # 打印业主已裁定排除的全部群名
  python audit_wx_coverage.py --json out.json    # 落 JSON

供应商侧判定：
  A  有群·已监控
  A2 供应商名匹配不到，但按『**产品名**』找到群（弱匹配 · 建议确认归属）
  B  有群·未监控（★应补）   C 无任何群（需人工确认）   D 购买平台/非物料（排除）

供应商名匹配三层（`resolve()`）：
  ① 供应商名（全称/简称/双向子串）→ ② 前缀退化（前3/前2字）→ ③ **产品名**（`PRODUCT_ALIAS`）

⚠️ 第 ③ 层是业主 2026-09-15 的口径：「搜不到的那些可能是以**所采购的产品**命名的，
   比如新隆声是喇叭、西崖是基站的外壳、引石磁业是磁铁。」
   —— 所以「供应商名匹配不到群」**≠**「该供应商没群」，群名可能只写料件。
   实测印证：引石磁业→`智环-磁铁`、欣荣声→`智环-喇叭`、倍耐达→`智环-充电线`。
   但产品名是**弱证据**（同一料件可能有几家供），所以单列 A2 档，不并进 A。
   🔴 已剔除的错误映射（业主亲口纠正）：`正扬五金→按键`（按键群属桐露/百耀）、
      `亿创源→马达`（华远电机也供马达）、`光锥测控→rtk基站`（基站群属展讯/黄总）。
      注意 SeaTable **判不出独家** —— 桐露压根不在那 8 张表里，纯数据驱动会把它当独家。

⚠️ 业主 2026-09-15 裁定**永久排除**（见 `EXCLUDED_GROUPS_*`）：
   ① 普特无忧 ×34 群（另一条业务线）②「客户新品/方案开发」临时群 ×9
   ③ 非生产业务主体 ×16（资质代办/个人事务）④ 72 个只有 ID 没有群名的无名群。
   这四类**不进任何缺口统计**（`to_add` 也过滤），避免每次审计重复来问。
   实测与白名单**零重叠** → 白名单保持 68 条，无需改动。
   新增群若命中 EXCLUDED_GROUPS_EXACT 会**静默消失**，所以泛称条目一律精确匹配。

⚠️ 维护点：新增客户后，若本脚本提示「新客户（未登记别名）」，请把其简称补进 `CUST_ALIAS`，
   否则该客户在群名里的匹配会漏（群名常用简称，例如 项目表写「合为智能」而群名只写「合为」）。
   同理，发现「某供应商只在产品名群里出现」时，把该料件词补进 `PRODUCT_ALIAS`（词要窄）。
"""
import collections
import csv
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# ══ 类 1 · 供应商来源 ══════════════════════════════════════
# 表 → (环节标签, 供应商列名)。发货清单的「快递单号」是运单号，不在此列。
SOURCES = [
    ("贴片厂", "贴片生产记录", "贴片厂"),
    ("组装厂", "组装记录", "组装厂"),
    ("PCB", "PCB下单记录", "供应商"),
    ("外壳", "外壳采购记录", "供应商"),
    ("IC", "IC采购记录", "供应商"),
    ("PCBA", "PCBA半成品采购记录", "供应商"),
    ("组装料", "组装料采购记录", "供应商"),
    ("成品", "成品采购记录", "供应商"),
]

# 购买平台 / 非物料供应商：用户明确要求排除（这些不上微信群，走电商后台）
PLATFORM = ["淘宝", "天猫", "京东", "拼多多", "1688", "阿里巴巴", "阿里", "立创", "华秋",
            "得捷", "digikey", "mouser", "贸泽", "顺丰", "中通", "圆通", "韵达", "申通",
            "麦当劳", "美团", "饿了么", "中国移动", "中国电信", "中国联通", "支付宝",
            "大熊财税", "财税", "代账", "银行", "税务"]

# 群库里的噪音群（即使命中也不算有效覆盖）
NOISE = ("淘宝", "麦当劳", "顺丰", "福利", "闪购", "美生活", "山姆", "拼多多", "京东")

# ══ 类 2 · 客户别名（源：项目表「项目」列；人工维护，群名常用简称） ══
CUST_ALIAS = {
    "动联": ["动联"],
    "郑州云峰": ["郑州云峰", "云峰"],
    "天工微能": ["天工微能", "微能"],
    "天工测控": ["天工测控"],
    "昊想科技": ["昊想"],
    "上海汇撰": ["汇撰"],
    "重庆智石": ["智石"],
    "云南天奥": ["天奥"],
    "合为智能": ["合为"],
    "北天通讯": ["北天通讯"],
    "美迪": ["美迪"],
    "柳工": ["柳工"],
    "柳矿": ["柳矿"],
    "慧寻": ["慧寻"],
    "大学（禾木）": ["学院部署", "大学"],
    "先锋露天煤矿": ["先锋"],
    "枢极": ["枢极"],
    "北京优波": ["优波"],
    "北极新云": ["北极新云"],
    "成都名泽": ["名泽"],
}
# 客户主体清洗用：出现这些词即认为产品描述开始，截断
PROD_CUT = ["采购", "合同", "信标", "工卡", "小卡", "大卡", "定位", "系统", "蓝牙", "智能",
            "发卡", "充电", "防爆", "车载", "续费", "借测", "项目", "批量", "新订单",
            "UWB", "RTK", "网关", "芯片", "模组", "人员", "露天", "矿山", "北斗", "三融合"]

# ══ 类 3 · 技术服务 / 售后 关键词 ═══════════════════════════
SVC_KW = ["售后", "维修", "返修", "技术支持", "技术对接", "技术交流", "技术沟通",
          "技术服务", "证书", "标书", "部署", "调试", "客诉", "厂家技术", "服务群", "技术群"]
SVC_BIZ = ["振道", "智环", "定位", "信标", "工卡", "小卡", "大卡", "UWB", "物联", "物联网"]

# 五桶分类辅助
SELF_KW = ["振道技术公司", "智环未来生产", "智环未来品宣", "高精度定位内部", "工厂生产",
           "测试群", "硬件群", "AI应用沟通", "开发群", "内部群"]
NONBIZ = ["淘宝", "京东", "拼多多", "麦当劳", "肯德基", "瑞幸", "美团", "山姆", "顺丰",
          "闪购", "美生活", "外卖", "驾校", "广场舞", "村民", "会员", "福利", "红包",
          "财务", "工商税务", "记账", "物业", "健身", "电影", "C4D", "插画", "打卡",
          "训练营", "学习", "粉丝", "玩家", "闲置", "招聘", "房产", "相亲", "聚会",
          "二手", "课程", "教室", "派对", "Party", "闲聊"]

# ══ 业主 2026-09-15 裁定：以下四类**永久排除**，不纳入监控、也不再报为缺口 ══
# 原话：「123不要，4出现这种情况，都是临时拉的群聊，都是可以排除的。」
#
#   ① 普特无忧 ×34 群 —— 另一条业务线（文具/礼品/化妆品/碳化硅渠道网），
#      SeaTable、台账、微信摘要里零痕迹，与智环未来/振道无关 → 不要。
#   ② 疑似「客户新品/方案开发」群 ×9 —— 业主：「都是临时拉的群聊」→ 不要。
#   ③ 身份不明群 ×16（资质代办 / 个人事务 / 学习群）→ 不要。
#   ④ 群库里 72 个**只有 ID 没有群名**的群（name 形如 `12307067401@chatroom`）→ 临时群，排除。
#
# 🔴 结论：白名单**不需要再加任何条目** —— 实测这 131 个群**无一被现行 68 条命中**
#    （2026-09-15 核实：① 34 群 0 命中 · ② 9 群 0 命中 · ③ 16 群 0 命中 · ④ 72 群 0 命中）。
#    本清单的唯一作用是让以后的审计**不再重复来问**，别把它当噪音删掉。
# 匹配方式：`_SUBSTR` 里的按**子串**（这些主体后面总跟着店名/版本号），
#    其余按**全名精确**（「硬件群」「设备加工」这类泛称用子串会误伤未来的同名群）。
EXCLUDED_GROUPS_EXACT = {
    # ② 临时方案 / 开发群
    "摇奶器方案开发": "② 临时方案/开发群（业主：临时拉的群）",
    "风扇IOT开发群": "② 临时方案/开发群（业主：临时拉的群）",
    "增压排气扇UI": "② 临时方案/开发群（业主：临时拉的群）",
    "钟哥方案点屏群": "② 临时方案/开发群（业主：临时拉的群）",
    "何总方案沟通": "② 临时方案/开发群（业主：临时拉的群）",
    "镜头研发": "② 临时方案/开发群（业主：临时拉的群）",
    "设备加工": "② 临时方案/开发群（业主：临时拉的群）",
    "硬件群": "② 临时方案/开发群（业主：临时拉的群）",
    "激光机沟通": "② 临时方案/开发群（业主：临时拉的群）",
    # ③ 非生产业务主体
    "正道服务群": "③ 非生产业务主体（资质代办/个人事务）",
    "奇幻森林工作交流": "③ 非生产业务主体（资质代办/个人事务）",
    "黑芒果": "③ 非生产业务主体（资质代办/个人事务）",
    "AIU 维护群": "③ 非生产业务主体（资质代办/个人事务）",
    "宣传资料对接": "③ 非生产业务主体（资质代办/个人事务）",
    "发票问题沟通": "③ 非生产业务主体（资质代办/个人事务）",
    "高速交警用电服务群": "③ 非生产业务主体（资质代办/个人事务）",
}
EXCLUDED_GROUPS_SUBSTR = {
    "普特无忧": "① 另一条业务线（普特无忧 · 34 群）",
    "中科伟智": "③ 非生产业务主体（资质代办/个人事务）",
    "唐一休":  "③ 非生产业务主体（资质代办/个人事务）",
    "枭瞳":    "③ 非生产业务主体（资质代办/个人事务）",
}
ANON_GROUP_REASON = "④ 无名群（仅 ID · 临时群）"


def excluded_reason(name):
    """业主裁定永久排除的群 → 返回排除理由；不在清单里返回 None。"""
    n = str(name or "")
    if not n:
        return None
    if re.match(r"^\d+@chatroom$", n):
        return ANON_GROUP_REASON
    if n in EXCLUDED_GROUPS_EXACT:
        return EXCLUDED_GROUPS_EXACT[n]
    for k, why in EXCLUDED_GROUPS_SUBSTR.items():
        if k in n:
            return why
    return None


def is_excluded(name):
    return excluded_reason(name) is not None

# 全称/异常写法 → 规范简称（人工维护，发现新的就往这里加）
ALIAS = {
    "禾平（深圳）技术有限公司": "禾平",
    "深圳市云本位科技有限公司": "云本位",
    "深圳市思普瑞美电子材料有限公司": "思普瑞美",
    "亿创源-马达散料": "亿创源",
    "跃之": "众跃之",
    "齐犇物联": "齐犇",
}

# ══ 产品命名群（业主 2026-09-15 口径）═══════════════════════
# 「搜不到的那些可能是以**所采购的产品**命名的，比如新隆声是喇叭、西崖是基站的外壳、
#   引石磁业是磁铁。」→ 供应商名匹配不到群 ≠ 该供应商没群，可能群名写的是它供的料件。
# 键 = 供应商规范简称（或台账原写法）；值 = 群名里可能出现的**产品/料件词**。
#
# ⚠️⚠️ 用之前必须过「**独家性**」这一关 —— 同一个料件常有多家供，此时产品词不可用：
#     业主 2026-09-15 亲口纠正：*「不会，正扬五金的按键是组装料，桐露的是贴片料」*
#     —— 「按键」既被正扬五金供（SOS按键·组装料），也被桐露供（贴片料），
#        所以「振道-硅胶按键 / 振道金属按键」**不是**正扬五金的群，映射过去就是误认。
#     🔴 注意：**光靠 SeaTable 判不出独家** —— 桐露压根不在 8 张采购表里，
#        纯数据驱动的「产品词→唯一供应商」会把它算成独家。所以本表**人工维护、宁缺勿滥**：
#        只在「群名 = 本公司品牌 + 该料件」且该料件确无第二家时收录。
#     已剔除的错误映射：正扬五金→按键、亿创源→马达（华远电机也供马达）、
#                       光锥测控→rtk基站（展讯/黄总各有一家基站群）。
PRODUCT_ALIAS = {
    "引石磁业": ["磁铁"],        # 供 小卡磁铁 / 大卡磁铁 → 智环-磁铁
    "欣荣声":   ["喇叭"],        # 供 喇叭 → 智环-喇叭 / 智环-喇叭膜
    "新隆声":   ["喇叭"],        # 业主口述写法（台账里是「欣荣声」，两种都兜）
    "西崖/鼎昇": ["基站外壳"],    # 供 基站外壳 → 防爆窄带基站外壳（见 PRODUCT_DENY）
    "西崖":     ["基站外壳"],
    "鼎昇":     ["基站外壳"],
    "倍耐达":   ["充电线"],      # 供 充电线 → 智环-充电线
    "鑫隆庆":   ["防水膜"],      # 供 防水膜+导电布（当前群库里无此名 → 仍留 C 类）
    "云本位":   ["数据线"],      # 供 2P数据线（同上，无群）
}

# 产品名匹配的**排除项**：命中的群名里若含这些词，说明该群另有主体，不算这家的。
PRODUCT_DENY = {
    "西崖/鼎昇": ["兴达卫视"],   # 「振道-兴达卫视 基站外壳」的供货方是兴达卫视，不是西崖
    "西崖":     ["兴达卫视"],
    "鼎昇":     ["兴达卫视"],
}

# 公司后缀（按长度降序剥离，可反复剥）
SUFFIX = ["股份有限公司", "有限责任公司", "有限公司", "股份公司", "有限", "公司", "工厂", "厂",
          "科技", "电子", "实业", "集团", "企业", "商行", "经营部", "事务所", "服务中心",
          "工作室", "贸易", "商贸", "五金", "精密", "模塑", "智能", "技术", "材料"]

GENERIC = {"深圳", "上海", "北京", "广州", "深圳市", "有限公司", "科技", "电子", "技术",
           "东莞", "惠州", "苏州", "杭州"}


def nospace(s):
    return re.sub(r"[\s\-_·、,，。；;()（）\[\]【】/\\|]+", "", str(s or "")).lower()


def short_name(s):
    s = str(s or "").strip()
    if s in ALIAS:
        return ALIAS[s]
    t = re.sub(r"[（(][^）)]*[）)]", "", s).strip()
    changed = True
    while changed:
        changed = False
        for suf in SUFFIX:
            if t.endswith(suf) and len(t) > len(suf):
                t = t[: -len(suf)]
                changed = True
    return t.strip() or s


def cust_core(project_name):
    """'动联（山东）-UWB定位工卡及信标' → '动联（山东）'；仅用于检测「新客户未登记别名」"""
    s = re.sub(r"[（(](振道|智环|禾木|振道技术)[）)]$", "", str(project_name or "").strip()).strip()
    s = re.split(r"[-—－=]", s)[0].strip()
    s = re.sub(r"[（(].*?[）)]", "", s).strip()
    for w in PROD_CUT:
        if w in s[2:]:
            s = s[: s.index(w, 2)]
    return re.sub(r"[\d张个版第\s]+", "", s).strip()


def load_watch_groups():
    """config.yaml 的手写白名单 + wx_watchlist 的**自动词表**（两者取并集）。

    ⚠️ 必须与采集侧口径一致：`wechat_intake._wechat_cfg()` 会把自动词表并入 watch_groups，
    若审计只读 config.yaml，就会把「靠自动词表已纳入监控」的群误报成缺口。
    """
    out = []
    try:
        import yaml
        cfg = yaml.safe_load(io.open(os.path.join(HERE, "config.yaml"), encoding="utf-8").read())
        out = [str(x) for x in ((cfg.get("wechat") or {}).get("watch_groups") or [])]
    except Exception as e:
        print("[warn] 读 config.yaml 失败: %s" % e, file=sys.stderr)
    try:
        sys.path.insert(0, HERE)
        import wx_watchlist
        extra = wx_watchlist.auto_keywords()
        low = {x.lower() for x in out}
        out += [x for x in extra if x.lower() not in low]
    except Exception:
        pass
    return out


def load_group_names():
    fp = os.path.join(DATA, "wechat_intake", "groups.json")
    if not os.path.exists(fp):
        return []
    gj = json.load(io.open(fp, encoding="utf-8", errors="replace"))

    def gname(x):
        if isinstance(x, str):
            return x
        if isinstance(x, dict):
            for k in ("name", "nickname", "remark", "group", "gname"):
                if x.get(k):
                    return str(x[k])
        return ""

    seq = gj if isinstance(gj, list) else list(gj.values())
    return [n for n in (gname(x) for x in seq) if n]


def is_watched(name, wg):
    """白名单判定 —— 必须与 `wechat_intake.py` 的真实谓词一致（否则本脚本会误报）。"""
    if not wg:
        return True  # 留空 = 全部群
    if name in wg:
        return True
    return any(isinstance(w, str) and w and w in name for w in wg)


def load_csv(name):
    fp = os.path.join(DATA, name if name.endswith(".csv") else name + ".csv")
    if not os.path.exists(fp):
        return []
    with io.open(fp, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_live():
    """直连 SeaTable 取业务表，返回 {表名: [行, ...]}；失败返回 None。"""
    try:
        sys.path.insert(0, HERE)
        cwd = os.getcwd()
        os.chdir(HERE)
        try:
            import cockpit
            from adapters.factory import get_adapter, load_config
            ad = get_adapter(load_config(None))
            ad.auth()
            na = cockpit._NormAdapter(ad)
            out = {}
            for _, tbl, _ in SOURCES + [("客户", "项目", "项目")]:
                try:
                    out[tbl] = na.list_rows(tbl) or []
                except Exception:
                    out[tbl] = []
            return out
        finally:
            os.chdir(cwd)
    except Exception as e:
        print("[warn] 直连 SeaTable 失败（改用本地 CSV）: %s" % e, file=sys.stderr)
        return None


def rows_of(tbl, live):
    return ((live or {}).get(tbl) if live is not None else load_csv(tbl)) or []


def collect_suppliers(live):
    """→ [(环节, 表名, 供应商取值, 次数)]"""
    out = []
    for label, tbl, col in SOURCES:
        cnt = collections.Counter(str(r.get(col) or "").strip() for r in rows_of(tbl, live))
        for sup, n in cnt.most_common():
            if not sup or sup in ("None", "0"):
                continue
            out.append((label, tbl, sup, n))
    return out


def collect_customers(live):
    """→ {客户主体: [(项目编号, 项目名, 状态)]} + 未登记别名的新客户"""
    fam = collections.defaultdict(list)
    unknown = collections.Counter()
    for r in rows_of("项目", live):
        pn = str(r.get("项目") or "").strip()
        if not pn:
            continue
        hit = None
        for canon, als in CUST_ALIAS.items():
            if any(a in pn for a in als) or canon in pn:
                hit = canon
                break
        if hit:
            fam[hit].append((str(r.get("项目编号") or ""), pn, str(r.get("状态") or "")))
        else:
            c = cust_core(pn)
            if len(c) >= 2:
                unknown[c] += 1
    return fam, unknown


def resolve(sup, names):
    """供应商取值 → (规范简称, [候选群], 命中原语)

    三层匹配，逐层退化：
      ① 供应商名：全称 / 简称 / 双向子串
      ② 前缀退化：前 3 字、前 2 字（如 引石磁业 → 「引石-振道」）
      ③ **产品名**：业主 2026-09-15 口径 —— 群可能以「所采购的产品」命名，
         所以供应商名匹配不到 ≠ 该供应商没群（见 PRODUCT_ALIAS）。
    """
    s = short_name(sup)
    hits, how = set(), ""
    for nm in names:
        if any(z in nm for z in NOISE):
            continue
        nn, ns = nospace(nm), nospace(s)
        if not ns:
            continue
        if ns in nn or nn in ns or nospace(sup) in nn:
            hits.add(nm)
    if not hits:
        for L in (3, 2):
            pre = nospace(s)[:L]
            if len(pre) < 2 or pre in GENERIC:
                continue
            got = {nm for nm in names if pre in nospace(nm)
                   and not any(z in nm for z in NOISE)}
            if got:
                hits, how = got, "前缀%d字" % L
                break
    if not hits:
        # ③ 产品命名群：键同时容忍「台账原写法 / 规范简称 / 去空格写法」
        #    命中后还要过 PRODUCT_DENY（群名含另一主体 → 不是这家的）
        keys = [k for k in (sup, s, nospace(s), nospace(sup)) if k]
        deny = {z for k in keys for z in PRODUCT_DENY.get(k, [])}
        for key in keys:
            for pw in PRODUCT_ALIAS.get(key, []):
                got = {nm for nm in names
                       if pw.lower() in nm.lower()
                       and not any(z in nm for z in NOISE)
                       and not any(z in nm for z in deny)}
                if got:
                    hits, how = got, "产品名:%s" % pw
                    break
            if hits:
                break
    return s, sorted(hits), how


def is_platform(v):
    lv = str(v).lower()
    return any(p.lower() in lv for p in PLATFORM)


def groups_for(aliases, names):
    out = []
    for a in aliases:
        for n in names:
            if a in n and n not in out:
                out.append(n)
    return out


# 本方主体（群名里的「我们」）：出现这些词说明该群是本司与对方的对接群
SELF_BRAND = ["振道", "智环"]


def counterparties(name):
    """群名 → 对方主体候选。'微能 天工—振道（商务群）' → ['微能 天工']

    只在群名含「振道/智环」时才拆，否则返回空（避免把随机群名当业务群）。
    """
    if not any(b in name for b in SELF_BRAND):
        return []
    parts = re.split(r"[-—+~～&、,，/|]", name)
    out = []
    for p in parts:
        p = re.sub(r"[（(].*?[）)]", "", p).strip()
        p = re.sub(r"^[\d\.\s]+", "", p).strip()
        if not p or any(b in p for b in SELF_BRAND):
            continue
        out.append(p)
    return out


# 群名里出现但不是「对方主体」的词：品类 / 属性 / 泛称
SKIP_TAIL = ("群", "供应商", "代理", "对接", "沟通", "交流", "应用")
MATERIAL = {"充电线", "喇叭", "喇叭膜", "磁铁", "按键", "硅胶", "摄像头", "多视界摄像头",
            "基站外壳", "彩印", "天线", "电池", "线材", "纸箱", "弹针", "模具", "塑胶"}


def unknown_partners(names, wg, sup_names=()):
    """群名里出现、但 BOTH 客户别名与供应商名录都认不出的对方主体（= SeaTable 未登记）"""
    known = list(CUST_ALIAS.keys()) + [a for als in CUST_ALIAS.values() for a in als] \
        + list(ALIAS.keys()) + [str(s) for s in sup_names]
    out = collections.defaultdict(list)
    for n in names:
        if is_excluded(n):                      # 业主已裁定排除的群 → 不再问「这是谁」
            continue
        for c in counterparties(n):
            if any(z in c for z in NOISE) or any(z in n for z in NONBIZ):
                continue
            if excluded_reason(c):              # 主体本身已被排除（如「普特无忧」）
                continue
            if len(c) < 2 or len(c) > 14:            # 太长/太短 → 噪音
                continue
            if re.fullmatch(r"[A-Za-z0-9\-\s\.\+]+", c):   # 纯型号/英文（NFC / LM620S / GZ）
                continue
            if c.endswith(SKIP_TAIL) or c in MATERIAL:     # 品类 / 泛称，不是主体
                continue
            if any(k in c or c in k for k in known):       # 已被客户或供应商认出
                continue
            out[c].append(n)
    return {k: v for k, v in out.items() if v}


def entities_of(n, names_sup_cache):
    """群 → (客户主体列表, 供应商简称列表, 是否服务类群)"""
    cust = [k for k, als in CUST_ALIAS.items() if any(a in n for a in als)]
    sup = names_sup_cache(n)
    svc = any(k in n for k in SVC_KW)
    return cust, sup, svc


def bucket_of(n, sup_of):
    """五桶：S 供应商 / C 客户 / A 客户技术服务售后 / I 内部 / O 其他 / N 噪音"""
    if any(z in n for z in NONBIZ) or re.match(r"^\d+@chatroom$", n):
        return {"N"}
    b = set()
    if any(z in n for z in SELF_KW):
        b.add("I")
    cust = [k for k, als in CUST_ALIAS.items() if any(a in n for a in als)]
    sup = sup_of(n)
    svc = any(k in n for k in SVC_KW)
    if cust and svc:
        b.add("A")
    elif cust:
        b.add("C")
    if sup:
        b.add("S")
    if svc and not cust and not sup:
        b.add("A")
    if not b:
        b.add("O")
    return b


def audit(live=None, min_records=1, only=None):
    wg = load_watch_groups()
    names = load_group_names()
    monitored = [n for n in names if is_watched(n, wg)]

    # ── 业主已裁定排除的群（不进任何缺口统计）──
    excluded = collections.defaultdict(list)
    for n in names:
        why = excluded_reason(n)
        if why:
            excluded[why].append(n)
    excl_flat = sorted(g for v in excluded.values() for g in v)

    # ── 类 1 供应商 ──
    sup_hits = {}
    all_rows = collect_suppliers(live)
    all_sup_short = {short_name(s) for _, _, s, _ in all_rows}
    rows, stat = [], collections.Counter()
    for label, tbl, sup, n in all_rows:
        if n < min_records:
            continue
        plat = is_platform(sup)
        s, groups, how = resolve(sup, names)
        mon = [g for g in groups if is_watched(g, wg)]
        if plat:
            st = "购买平台"
        elif mon:
            # 产品名匹配是**弱证据**：群名写的是料件（如「智环-喇叭」），未必独家属于这家
            # （例：正扬五金供 SOS按键，但「振道-硅胶按键」其实是桐露/百耀的）→ 单列一档待确认。
            st = "产品名命中" if how.startswith("产品名") else "已监控"
        elif groups:
            st = "有群未监控"
        else:
            st = "无群"
        stat[st] += 1
        for g in groups:
            sup_hits.setdefault(g, set()).add(s)
        rows.append({"section": label, "table": tbl, "supplier": sup, "short": s,
                     "records": n, "platform": plat, "status": st,
                     "groups": groups, "monitored": mon, "match": how})
    _ORD = {"已监控": 0, "产品名命中": 1, "有群未监控": 2, "无群": 3, "购买平台": 4}
    rows.sort(key=lambda r: (_ORD[r["status"]], -r["records"], r["supplier"]))

    def sup_of(n):
        return sorted({s for g, ss in sup_hits.items() if g == n for s in ss})

    # ── 类 2 客户 ──
    fam, unknown = collect_customers(live)
    cust_rows = []
    for canon, als in sorted(CUST_ALIAS.items()):
        groups = groups_for(als, names)
        mon = [g for g in groups if is_watched(g, wg)]
        st = "已监控" if mon else ("有群未监控" if groups else "无群")
        cust_rows.append({"customer": canon, "projects": fam.get(canon, []),
                          "groups": groups, "monitored": mon, "status": st})
    cust_rows.sort(key=lambda r: ({"有群未监控": 0, "无群": 1, "已监控": 2}[r["status"]], r["customer"]))

    # ── 类 3 技术服务/售后 ──
    svc_rows = []
    for n in sorted(names):
        cust, sup, svc = entities_of(n, sup_of)
        if not svc and not any(b in n for b in SVC_BIZ):
            continue
        if not (cust or sup) and not any(b in n for b in SVC_BIZ):
            continue
        if not svc:
            continue
        svc_rows.append({"group": n, "customers": cust, "suppliers": sup,
                         "watched": is_watched(n, wg)})

    # ── 五桶 ──
    buck = collections.defaultdict(list)
    for n in names:
        for t in bucket_of(n, sup_of):
            buck[t].append(n)

    return {
        "watch_groups": wg, "group_total": len(names), "mode": "live" if live is not None else "csv",
        "monitored_total": len(monitored), "monitored": monitored,
        "supplier": {"rows": rows, "summary": dict(stat)},
        "customer": {"rows": cust_rows, "unknown": dict(unknown),
                     "unknown_partners": unknown_partners(names, wg, all_sup_short),
                     "stat": dict(collections.Counter(r["status"] for r in cust_rows))},
        "service": {"rows": svc_rows,
                    "stat": {"total": len(svc_rows),
                             "watched": len([r for r in svc_rows if r["watched"]])}},
        "buckets": {k: sorted(v) for k, v in buck.items()},
        "excluded": {"n": len(excl_flat), "groups": excl_flat,
                     "by_reason": {k: sorted(v) for k, v in sorted(excluded.items())},
                     "monitored_overlap": [g for g in excl_flat if is_watched(g, wg)]},
        "wg_missing_in_lib": [g for g in wg
                              if not any(g == n or (isinstance(g, str) and g and g in n)
                                         for n in names)],
        "to_add": sorted(g for g in (
            {g for r in rows if r["status"] == "有群未监控"
             for g in r["groups"] if not is_watched(g, wg)}
            | {g for r in cust_rows if r["status"] == "有群未监控"
               for g in r["groups"] if not is_watched(g, wg)}
            | {r["group"] for r in svc_rows if not r["watched"]})
            if not is_excluded(g)),
    }


BUCKET_LABEL = {"S": "类1 供应商群", "C": "类2 客户群", "A": "类3 客户技术服务/售后群",
                "I": "内部群（自有）", "O": "其他（未识别）", "N": "非业务噪音"}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = sys.argv[1:]
    min_records, js, use_csv, show_groups, show_buckets, only = 1, None, False, False, False, None
    show_excl = False
    i = 0
    while i < len(argv):
        if argv[i] == "--min-records" and i + 1 < len(argv):
            min_records = int(argv[i + 1]); i += 2; continue
        if argv[i] == "--json" and i + 1 < len(argv):
            js = argv[i + 1]; i += 2; continue
        if argv[i] == "--only" and i + 1 < len(argv):
            only = argv[i + 1]; i += 2; continue
        if argv[i] == "--csv":
            use_csv = True; i += 1; continue
        if argv[i] == "--live":
            use_csv = False; i += 1; continue
        if argv[i] in ("--groups", "--list-monitored"):
            show_groups = True; i += 1; continue
        if argv[i] == "--buckets":
            show_buckets = True; i += 1; continue
        if argv[i] == "--excluded":
            show_excl = True; i += 1; continue
        i += 1

    live = None if use_csv else load_live()
    res = audit(live, min_records, only)

    print("=" * 90)
    print("业务实体 ↔ 微信群 覆盖自查（三类）   白名单 %d 条 · 实际监控 %d 群 / 群库 %d 群 · 数据源 %s"
          % (len(res["watch_groups"]), res["monitored_total"], res["group_total"], res["mode"]))
    print("=" * 90)

    # ── 类 2 客户（业主本轮重点）──
    if only in (None, "customer"):
        cs = res["customer"]
        print("\n" + "=" * 90)
        print("【类 2】客户 × 微信群   已监控 %d / 有群未监控 %d / 无群 %d  （共 %d 家）"
              % (cs["stat"].get("已监控", 0), cs["stat"].get("有群未监控", 0),
                 cs["stat"].get("无群", 0), len(cs["rows"])))
        print("=" * 90)
        for r in cs["rows"]:
            tag = {"已监控": "✅ 已监控", "有群未监控": "★ 有群·未监控（应补）", "无群": "— 无群"}[r["status"]]
            print("  %-14s 项目%-2d %s" % (r["customer"], len(r["projects"]), tag))
            for g in r["groups"]:
                print("        %s %s" % ("✔" if g in r["monitored"] else "·", g))
        if cs["unknown"]:
            print("\n⚠ 项目表里这些客户尚未登记别名（群名可能匹配不到）：")
            for k, v in sorted(cs["unknown"].items(), key=lambda x: -x[1]):
                print("     %-16s %d 个项目" % (k, v))
        up = cs["unknown_partners"]
        if up:
            print("\n⚠ 群名里有、但 SeaTable 项目表没登记的对方主体（%d 个，需确认是客户还是供应商）：" % len(up))
            for k, v in sorted(up.items(), key=lambda x: (-len(x[1]), x[0])):
                print("     %-14s %d 群  %s" % (k, len(v), " | ".join(v[:3])[:70]))

    # ── 类 3 技术服务/售后 ──
    if only in (None, "service"):
        ss = res["service"]
        print("\n" + "=" * 90)
        print("【类 3】客户的技术服务 / 售后群   业务相关 %d 个 · 已监控 %d · ★未监控 %d"
              % (ss["stat"]["total"], ss["stat"]["watched"], ss["stat"]["total"] - ss["stat"]["watched"]))
        print("=" * 90)
        for r in sorted(ss["rows"], key=lambda x: (x["watched"], x["group"])):
            who = ",".join(r["customers"] + [s + "(供)" for s in r["suppliers"]]) or "—"
            print("   %s %-44s 主体: %s" % ("✔" if r["watched"] else "·", r["group"], who))

    # ── 类 1 供应商 ──
    if only in (None, "supplier"):
        print("\n" + "=" * 90)
        print("【类 1】供应商 × 微信群   %s" % res["supplier"]["summary"])
        print("=" * 90)
        fam = {"已监控": "A. 有群 · 已纳入监控",
               "产品名命中": "A2. 供应商名匹配不到，但按『产品名』找到群（弱匹配 · 建议确认归属）",
               "有群未监控": "B. 有群 · ★未监控（应补）",
               "无群": "C. 无任何群（需人工确认）", "购买平台": "D. 购买平台 / 非物料（排除）"}
        cur = None
        for r in res["supplier"]["rows"]:
            if r["status"] != cur:
                cur = r["status"]
                print("\n" + "-" * 90)
                print(fam[cur])
                print("-" * 90)
            print("  %-22s 简称=%-10s %2d次 %s" % (r["supplier"][:22], r["short"][:10],
                                                     r["records"], r["section"]))
            for g in r["groups"]:
                print("        %s %s" % ("✔" if g in r["monitored"] else "·", g))
            if r["match"]:
                print("        [%s]" % r["match"])

    # ── 五桶 ──
    if show_buckets:
        print("\n" + "=" * 90)
        print("386 群五桶分类")
        print("=" * 90)
        for t in ["S", "C", "A", "I", "O", "N"]:
            lst = res["buckets"].get(t, [])
            mon = [x for x in lst if is_watched(x, res["watch_groups"])]
            print("\n── %s：共 %d，已监控 %d，未监控 %d " % (BUCKET_LABEL[t], len(lst), len(mon),
                                                            len(lst) - len(mon)) + "─" * 34)
            for n in lst:
                print("   %s %s" % ("✔" if is_watched(n, res["watch_groups"]) else "·", n))

    add = res["to_add"]
    if add:
        print("\n" + "=" * 90)
        print("★ 建议补进 config.yaml → wechat.watch_groups 的群（%d 个）" % len(add))
        print("=" * 90)
        for k, g in enumerate(add, 1):
            print('  %2d. "%s"' % (k, g))

    print("\n" + "=" * 90)
    miss = res["wg_missing_in_lib"]
    print("实际监控 %d / %d 群" % (res["monitored_total"], res["group_total"]))
    if miss:
        print("⚠ 白名单里这些条目在群库中一条都匹配不上（改名/退群/打错字？）")
        for g in miss:
            print("     [%s]" % g)
    else:
        print("✅ 白名单 %d 条全部有效（精确名或关键词均命中）" % len(res["watch_groups"]))
    print("=" * 90)

    # ── 业主已裁定排除的群（不进缺口清单）──
    ex = res["excluded"]
    print("\n" + "=" * 90)
    print("🔒 业主已裁定排除的群：%d 个（不纳入监控，也不再计入缺口）" % ex["n"])
    print("=" * 90)
    for why, gs in ex["by_reason"].items():
        print("  %s —— %d 群" % (why, len(gs)))
        if show_excl:
            for g in gs:
                print("       · %s" % g)
        else:
            for g in gs[:3]:
                print("       · %s" % g)
            if len(gs) > 3:
                print("       · …其余 %d 个（--excluded 可全列）" % (len(gs) - 3))
    if ex["monitored_overlap"]:
        print("  ⚠ 其中 %d 个**已被白名单命中**，应从 watch_groups 删除：%s"
              % (len(ex["monitored_overlap"]), ex["monitored_overlap"]))
    else:
        print("  ✅ 与白名单零重叠 —— 这些群当前都没被监控，config.yaml 无需改动")

    if show_groups:
        print("\n实际监控的 %d 个群：" % res["monitored_total"])
        for k, g in enumerate(res["monitored"], 1):
            print("  %3d. %s" % (k, g))
        print("=" * 90)

    if js:
        io.open(js, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=2))
        print("已写出 %s" % js)


if __name__ == "__main__":
    main()
