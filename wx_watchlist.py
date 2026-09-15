#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wx_watchlist.py — 让「SeaTable 里新登记的供应商/客户」自动进入微信群监控范围。

════════════════════════════════════════════════════════════════════════
要解决的问题（业主 2026-09-15 原话）
════════════════════════════════════════════════════════════════════════
> 如果我后面新加了供应商的群，它会自动识别吗？比如说我在群名称上加上公司的名字，
> 或者是说供应商的名字什么的，然后在 seatable 表格上也加入对应的供应商名字。

改造前：**不会**。`config.yaml → wechat.watch_groups` 是一份**手写清单**，
新供应商只在 SeaTable 里登记、群名写了它的名字，采集侧完全不知情 → 静默漏采。

改造后：**会**。本模块把 SeaTable 里的实体名（供应商 + 客户）抽出来、归一化成
「群名里可能出现的词」，写进 `data/wechat_intake/watch_auto.json`；
再由 `wechat_intake._wechat_cfg()` 把这批词**并入** watch_groups。
因为采集与审计两条链路的匹配谓词都走同一个配置入口，所以：

    三处谓词（_wx4_pull / 引擎B pull / summary）与审计，全部自动继承，无需改 config.yaml。

════════════════════════════════════════════════════════════════════════
为什么是「子串匹配」所以能自动 —— 以及自动的边界（别误解）
════════════════════════════════════════════════════════════════════════
`watch_groups` 的匹配是 `any(w in g["name"] for w in watch)`，即**子串包含**。
所以只要词表里有「牧泰莱」，群名 `智环-牧泰莱PCB` 就会被自动纳入 —— 新群也一样。

**自动识别的三个前提（缺一不可，务必对业主讲清）**：
  ① 群名里**真的出现**了那个名字。群名叫「临时沟通群」「讨论组」的，神仙也认不出。
  ② SeaTable 里**登记了**该实体（这是词表的唯一数据源）。
  ③ 本模块**跑过一次**（每晚 19:00 全量任务里已挂上）。

另外：客户名默认**不经**此处注入（客户别名表 `audit_wx_coverage.CUST_ALIAS` 是人工维护的
短名映射，交给它更准）；本模块默认只注入**供应商侧**。要连客户一起注入用 `--with-customers`。

CLI：
  python wx_watchlist.py refresh [--with-customers]   # 从 SeaTable 抽词并落盘
  python wx_watchlist.py show                         # 看当前词表
  python wx_watchlist.py check                        # 词表里每个词命中几个群（找出过宽/无效的词）
"""
import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
INTAKE = os.path.join(DATA, "wechat_intake")
AUTO_PATH = os.path.join(INTAKE, "watch_auto.json")
GROUPS_CACHE = os.path.join(INTAKE, "groups.json")
TZ = timezone(timedelta(hours=8))

# ── 归一化：从公司全称剥出「群名里可能出现的那一段」 ──
CITY_PREFIX = [
    "深圳市", "深圳", "东莞市", "东莞", "广州市", "广州", "上海市", "上海", "北京市", "北京",
    "惠州市", "惠州", "中山市", "中山", "佛山市", "佛山", "珠海市", "珠海", "杭州市", "杭州",
    "苏州市", "苏州", "南京市", "南京", "成都市", "成都", "重庆市", "重庆", "武汉市", "武汉",
    "西安市", "西安", "长沙市", "长沙", "郑州市", "郑州", "青岛市", "青岛", "厦门市", "厦门",
    "宁波市", "宁波", "天津市", "天津", "合肥市", "合肥", "福州市", "福州", "无锡市", "无锡",
    "常州市", "常州", "昆山市", "昆山", "中国",
]
COMP_SUFFIX = [
    "股份有限公司", "有限责任公司", "有限公司", "科技有限公司", "电子有限公司", "实业有限公司",
    "有限公司分公司", "分公司", "公司",
    "集团", "股份有限公司分公司",
]
# 行业通用词：剥掉后剩余的才是「字号」（如 正阳五金 → 正阳；牧泰莱电路技术 → 牧泰莱）
INDUSTRY_WORDS = [
    "电路技术", "电路", "电子科技", "电子", "科技", "精密", "五金", "塑胶", "模具", "包装",
    "印刷", "标识", "光电", "通信", "通讯", "智能", "自动化", "仪器", "仪表", "实业", "贸易",
    "商行", "经营部", "厂", "制造",
]

# 过宽 / 无意义词：命中群数过多或根本不是主体名，一律丢弃
STOPWORDS = {
    "科技", "电子", "有限公司", "公司", "集团", "五金", "塑胶", "模具", "包装", "印刷", "智能",
    "通信", "通讯", "仪器", "仪表", "实业", "贸易", "商行", "工厂", "厂", "电路", "精密",
    "供应商", "采购", "客户", "深圳", "东莞", "广州", "上海", "北京", "苏州", "杭州",
    "淘宝", "天猫", "京东", "拼多多", "1688", "阿里巴巴", "阿里", "顺丰", "中通", "圆通",
    "韵达", "申通", "立创", "华秋", "得捷", "贸泽", "银行", "财税", "代账", "税务",
    "维保", "售后", "技术", "项目", "测试", "样品", "询价", "报价",
}
MIN_LEN = 2


def _strip_any(s, words):
    changed = True
    while changed:
        changed = False
        for w in sorted(words, key=len, reverse=True):
            if s.startswith(w) and len(s) > len(w):
                s = s[len(w):]
                changed = True
                break
    return s


def _strip_suffix(s, words):
    changed = True
    while changed:
        changed = False
        for w in sorted(words, key=len, reverse=True):
            if s.endswith(w) and len(s) > len(w):
                s = s[:-len(w)]
                changed = True
                break
    return s


def normalize(name):
    """公司名 → 群名里可能出现的主词。返回候选词列表（含全称，去重、过滤）。"""
    raw = re.sub(r"[\s（）()【】\[\]、,，。.·\-_/\\]+", "", str(name or "")).strip()
    if not raw:
        return []
    out = [raw]
    s = _strip_any(raw, CITY_PREFIX)
    s = _strip_suffix(s, COMP_SUFFIX)
    if s and s != raw:
        out.append(s)
    # 再剥行业词，得到「字号」——要够长才收，避免把「正阳」这种短字号之外的噪音收进来
    s2 = _strip_suffix(s, INDUSTRY_WORDS)
    s2 = _strip_suffix(s2, INDUSTRY_WORDS)
    if s2 and s2 != s and len(s2) >= MIN_LEN:
        out.append(s2)

    seen, res = set(), []
    for w in out:
        w = w.strip()
        if len(w) < MIN_LEN or w in STOPWORDS or w in seen:
            continue
        seen.add(w)
        res.append(w)
    return res


# ── 数据源：复用审计脚本的供应商表清单，避免两处各写一份 ──
def _supplier_values(with_customers=False):
    """返回 [(实体原值, 来源标签)]。优先直播 SeaTable，取不到回退 audit 的 resolve 表。"""
    vals = []
    try:
        sys.path.insert(0, HERE)
        import audit_wx_coverage as A
    except Exception as e:
        print("[warn] 无法导入 audit_wx_coverage：%s" % e, file=sys.stderr)
        return vals
    try:
        live = A.load_live()
    except Exception as e:
        print("[warn] 直播读取 SeaTable 失败：%s" % e, file=sys.stderr)
        live = {}
    try:
        rows = A.collect_suppliers(live) if hasattr(A, "collect_suppliers") else []
    except Exception:
        rows = []
    for item in rows:
        # collect_suppliers 返回 (环节, 表名, 供应商取值, 次数)
        try:
            v = item[2]
        except Exception:
            continue
        if v:
            vals.append((str(v), str(item[0])))
    if with_customers:
        try:
            for r in (live.get("项目") or []):
                nm = r.get("项目")
                if isinstance(nm, str) and nm.strip():
                    vals.append((nm.strip(), "客户"))
        except Exception:
            pass
    return vals


def build_with_customers(with_customers=False, live_vals=None):
    """返回 {"generated_at","source","entities":{原值:[词]}, "keywords":[...], "note"}"""
    vals = live_vals if live_vals is not None else _supplier_values(with_customers)
    entities = {}
    for raw, src in vals:
        ws = normalize(raw)
        if not ws:
            continue
        entities.setdefault(raw, {"src": src, "words": ws})
    kws = []
    seen = set()
    for raw, info in entities.items():
        for w in info["words"]:
            if w not in seen:
                seen.add(w)
                kws.append(w)
    kws.sort(key=lambda x: (-len(x), x))
    return {
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "SeaTable 采购/生产环节表「供应商」" + ("+ 项目表「项目」(客户)" if with_customers else ""),
        "entities": entities,
        "keywords": kws,
        "note": "本文件由 wx_watchlist.py 自动生成；wechat_intake 会把这些词并入 watch_groups 做子串匹配。",
    }


def auto_keywords():
    """供 wechat_intake / audit_wx_coverage 读取。读不到返回 []（绝不抛异常）。"""
    try:
        with io.open(AUTO_PATH, encoding="utf-8") as f:
            return [str(x) for x in (json.load(f).get("keywords") or [])]
    except Exception:
        return []


# ── CLI ──
def cmd_refresh(args):
    data = build_with_customers(args.with_customers)
    os.makedirs(INTAKE, exist_ok=True)
    with io.open(AUTO_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    n_ent = len(data["entities"])
    print("[ok] 词表已刷新：%d 个实体 → %d 个匹配词" % (n_ent, len(data["keywords"])))
    print("     落盘 %s" % AUTO_PATH)
    if n_ent == 0:
        print("     [!] 0 个实体 —— 多半是 SeaTable 读不到（token/网络）。检查后重跑。")
        return 2
    # 打印几个示例，便于一眼确认
    for raw in list(data["entities"])[:10]:
        print("     %-28s → %s" % (raw[:28], " / ".join(data["entities"][raw]["words"])))
    return 0


def cmd_show():
    data = {}
    try:
        with io.open(AUTO_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        print("尚无词表（先跑 refresh）")
        return 1
    print("生成时间 %s" % data.get("generated_at"))
    print("数据源   %s" % data.get("source"))
    print("实体 %d 个 · 匹配词 %d 个" % (len(data.get("entities") or {}), len(data.get("keywords") or [])))
    for w in data.get("keywords") or []:
        print("   %s" % w)
    return 0


def cmd_check():
    """每个自动词命中多少群 —— 命中过多的说明词太宽，应加进 STOPWORDS。"""
    try:
        with io.open(GROUPS_CACHE, encoding="utf-8") as f:
            groups = [g.get("name") or "" for g in json.load(f)]
    except Exception:
        print("读不到 groups.json（先跑 python wechat_intake.py groups）")
        return 1
    kws = auto_keywords()
    if not kws:
        print("自动词表为空（先跑 refresh）")
        return 1
    rows = []
    for w in kws:
        hit = [g for g in groups if w in g]
        rows.append((len(hit), w, hit[:4]))
    rows.sort(reverse=True)
    print("自动词 %d 个 · 群库 %d 个群" % (len(kws), len(groups)))
    print("命中数为 0 的词（在群库里落空，可考虑清理）：")
    for n, w, _ in [r for r in rows if r[0] == 0][:20]:
        print("   0   %s" % w)
    print("命中最多的词（>12 个要警惕过宽）：")
    for n, w, ex in rows[:15]:
        print("   %-3d %-16s %s" % (n, w, " | ".join(x[:24] for x in ex)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="SeaTable 实体名 → 微信群监控词表（自动纳入）")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("refresh")
    r.add_argument("--with-customers", action="store_true",
                   help="连项目表的客户名一起注入（默认只注入供应商侧）")
    sub.add_parser("show")
    sub.add_parser("check")
    a = ap.parse_args()
    if a.cmd == "refresh":
        return cmd_refresh(a)
    if a.cmd == "show":
        return cmd_show()
    if a.cmd == "check":
        return cmd_check()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
