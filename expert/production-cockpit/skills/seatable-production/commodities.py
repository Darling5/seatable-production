# -*- coding: utf-8 -*-
"""原料行情监控：金/银/铜/锡等金属 + 石化塑料代理指标 + ABS/PC/PS 现货人工录入。

用法：
  python commodities.py fetch [--dry-run]           # 拉实时价并写库（每日跑一次即可积累走势）
  python commodities.py show  [--days 30]           # 打印走势（sparkline + 首末值 + 涨跌）
  python commodities.py trend [--days 30]           # 涨跌幅与超阈值告警
  python commodities.py backfill --contract au2610  # 补历史（东财 K 线，一次性）
  python commodities.py add ABS --price 11800       # 人工录入现货价（ABS/PC/PS）

数据源与口径（2026-09-02 实测定案）
  1. 实时：新浪财经期货接口  https://hq.sinajs.cn/list=nf_XXX0
     - 免费、无 key、无需申请；人民币计价；**一次请求可取全部品种**
     - 必须带 Referer: https://finance.sina.com.cn，返回 GBK 编码
     - 字段：名称,时间,开,高,低,?,买,卖,最新,?,昨结算,买量,卖量,持仓,成交,交易所,品种,日期
  2. 历史：新浪日 K 线服务**已下线**（返回 Service not found），历史改走东方财富
     - secid = 市场号.合约代码小写，如 113.au2610（113 上期所 / 114 大商所 / 115 郑商所）
     - 补录的历史标注具体合约（如「合约au2610」），与「连续」口径分开
  3. 现货（ABS/PC/PS）：**无免费公开 API** —— 生意社/卓创/中塑在线等资讯商垄断报价。
     走人工录入；若日后接入含产业数据的付费源（如同花顺 iFinD），
     在 fetch 里加一个 source 适配器即可，CSV 结构与展示层不用动。

铁律
  1. 只读：只取市场行情，绝不触碰交易
  2. 无凭证：本模块不存任何 API key（走的都是免费源），隐私零风险
  3. 失败降级：网络失败/接口改版/字段缺失一律返回错误标记，不抛异常中断批量
  4. 同口径环比：「连续」只跟「连续」比，具体合约只跟同一合约比 ——
     拼接不同口径（连续 vs au2610）会出现假涨跌，与物料行情踩过的坑同源
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HIST_PATH = os.path.join(DATA, "原料行情记录.csv")
HIST_COLS = ["日期", "原料", "类别", "单价", "单位", "涨跌幅", "口径", "来源", "备注"]

# 口径常量：写进 CSV 的「口径」列，环比只跟同口径比
BASIS_CONT = "连续"                 # 新浪 nf_XXX0 连续合约
BASIS_MANUAL = "现货人工"           # 人工录入的现货价
SINA_SRC = "新浪期货"
EM_SRC = "东方财富"

SINA_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn",
                "User-Agent": "Mozilla/5.0"}
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

# (key, 名称, 类别, 新浪代码, 单位, 用途说明)
COMMODITIES = [
    ("AU", "黄金", "金属", "nf_AU0", "元/克", "键合金线 / PCB 沉金 / 连接器镀金"),
    ("AG", "白银", "金属", "nf_AG0", "元/千克", "导电银浆 / 锡银焊料"),
    ("CU", "铜", "金属", "nf_CU0", "元/吨", "PCB 铜箔 / 线材 / 端子"),
    ("SN", "锡", "金属", "nf_SN0", "元/吨", "锡膏与焊料，SMT 成本敏感项"),
    ("AL", "铝", "金属", "nf_AL0", "元/吨", "外壳 / 散热件"),
    ("NI", "镍", "金属", "nf_NI0", "元/吨", "电池极片 / 不锈钢"),
    ("PP", "聚丙烯", "塑料代理", "nf_PP0", "元/吨", "ABS/PC/PS 上游石化链代理指标"),
    ("L", "聚乙烯", "塑料代理", "nf_L0", "元/吨", "LLDPE，上游石化链代理指标"),
    ("V", "PVC", "塑料代理", "nf_V0", "元/吨", "线缆护套，上游石化链代理指标"),
]

# (key, 名称, 类别, 单位, 说明) —— 无免费公开 API，人工录入
MANUAL_ONLY = [
    ("ABS", "ABS 树脂", "塑料", "元/吨", "外壳/结构件；现货无免费 API，人工录入"),
    ("PC", "PC 树脂", "塑料", "元/吨", "透明件/外壳；现货无免费 API，人工录入"),
    ("PS", "PS 树脂", "塑料", "元/吨", "包装/结构件；现货无免费 API，人工录入"),
]

BY_KEY = {}
for _r in COMMODITIES:
    BY_KEY[_r[0]] = {"key": _r[0], "name": _r[1], "cat": _r[2], "sina": _r[3],
                     "unit": _r[4], "desc": _r[5], "auto": True}
for _r in MANUAL_ONLY:
    BY_KEY[_r[0]] = {"key": _r[0], "name": _r[1], "cat": _r[2], "sina": "",
                     "unit": _r[3], "desc": _r[4], "auto": False}

# 东财市场号（补历史用）：113 上期所 / 114 大商所 / 115 郑商所
EM_MARKET = {"AU": "113", "AG": "113", "CU": "113", "SN": "113",
             "AL": "113", "NI": "113", "PP": "114", "L": "114", "V": "114"}


# ------------------------------------------------------------------ 配置
def _cfg():
    """读 config.yaml 的 commodities 段；读不到就用默认值，不因配置缺失而崩。"""
    try:
        from adapters.factory import load_config
        c = load_config() or {}
        return c.get("commodities") or {}
    except Exception:
        return {}


def _alert_threshold():
    try:
        return float(_cfg().get("alert_threshold") or 3)
    except Exception:
        return 3.0


# ------------------------------------------------------------------ 工具
def _fmt_num(v, nd=2):
    if v is None:
        return "—"
    try:
        f = float(v)
    except Exception:
        return str(v)
    if f == int(f) and abs(f) >= 1000:
        return "{:,.0f}".format(f)
    return ("{:,.%df}" % nd).format(f)


def _sparkline(vals, width=24):
    """把一串数值压成 sparkline（▁▂▃▄▅▆▇█）。点少或全平时给个平直提示。"""
    blocks = "▁▂▃▄▅▆▇█"
    vs = [v for v in vals if v is not None]
    if len(vs) < 2:
        return "—"          # 单点画不出走势，别输出吓人的「数据点不足」
    vs = vs[-width:]
    lo, hi = min(vs), max(vs)
    if hi == lo:
        return blocks[0] * len(vs)
    return "".join(blocks[int((v - lo) / (hi - lo) * (len(blocks) - 1))] for v in vs)


def _read_csv(path):
    if not os.path.exists(path):
        return []
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
        except Exception:
            return []
    return []


def _write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _prev_price(name, basis, rows):
    """同口径的上一条有单价记录 —— 环比基准（口径不同不比，避免拼接假涨跌）。"""
    for r in reversed(rows):
        if (r.get("原料") == name and (r.get("口径") or BASIS_CONT) == basis):
            try:
                p = float(str(r.get("单价") or "").replace(",", ""))
            except Exception:
                continue
            if p > 0:
                return p
    return None


def _pct(cur, prev):
    if prev in (None, 0) or cur is None:
        return None
    return round((cur - prev) / prev * 100, 2)


# ------------------------------------------------------------------ 实时行情
def fetch_quotes(keys=None):
    """批量拉新浪实时行情。返回 {key: {'ok':..,'price':..,'date':..,'error':..}}。

    一次 HTTP 请求取回全部品种；单个品种解析失败不影响其他品种。
    """
    import requests
    targets = [k for k in (keys or list(BY_KEY)) if BY_KEY.get(k, {}).get("auto")]
    if not targets:
        return {}
    codes = [BY_KEY[k]["sina"] for k in targets]
    out = {}
    try:
        r = requests.get(SINA_URL + ",".join(codes), headers=SINA_HEADERS, timeout=20)
        r.encoding = "gbk"
        body = r.text or ""
    except Exception as e:
        for k in targets:
            out[k] = {"ok": False, "error": "行情请求失败：%s" % e}
        return out

    parsed = {}
    for line in body.strip().split("\n"):
        m = re.match(r'var\s+hq_str_(nf_\w+)="(.*)";?', line.strip())
        if not m:
            continue
        code, payload = m.group(1), m.group(2)
        fields = payload.split(",")
        if len(fields) < 18 or not fields[0]:
            continue
        try:
            price = float(fields[8])
        except Exception:
            price = None
        parsed[code] = {"name": fields[0], "price": price,
                        "date": fields[17].strip() or datetime.now().strftime("%Y-%m-%d"),
                        "open": fields[2], "high": fields[3], "low": fields[4]}

    for k in targets:
        c = BY_KEY[k]
        p = parsed.get(c["sina"])
        if not p:
            out[k] = {"ok": False, "error": "接口无此品种返回（代码可能已变更）"}
        elif p["price"] is None:
            out[k] = {"ok": False, "error": "价格字段解析失败"}
        else:
            out[k] = {"ok": True, "price": p["price"], "date": p["date"],
                      "raw": p, "error": ""}
    return out


# ------------------------------------------------------------------ 历史补录
def backfill(key, contract, days=60):
    """用东财 K 线补历史。返回写入条数。

    口径说明：补录的是**具体合约**（如 au2610），会写进「口径」列，
    与日常的「连续」分开 —— 环比只跟同口径比，避免拼接假涨跌。
    """
    import requests
    meta = BY_KEY.get(key)
    if not meta:
        print("未知原料：%s（可用：%s）" % (key, "、".join(BY_KEY)))
        return 0
    mkt = EM_MARKET.get(key)
    if not mkt:
        print("%s 无东财市场号映射，无法补历史" % key)
        return 0
    secid = "%s.%s" % (mkt, contract.strip().lower())
    try:
        r = requests.get(EM_KLINE, headers=EM_HEADERS, timeout=25, params={
            "secid": secid, "fields1": "f1,f2,f3", "klt": "101", "fqt": "1",
            "end": "20500101", "lmt": int(days),
            "fields2": "f51,f52,f53,f54,f55,f56"})
        d = (r.json() or {}).get("data") or {}
        klines = d.get("klines") or []
    except Exception as e:
        print("补录失败：%s" % e)
        return 0
    if not klines:
        print("未取到 K 线：secid=%s 可能无效或已到期（换一个合约试试）" % secid)
        return 0

    rows = _read_csv(HIST_PATH)
    have = {(r.get("日期"), r.get("原料"), r.get("口径")) for r in rows}
    basis = "合约%s" % contract.strip().lower()
    added = 0
    for line in klines:
        # 东财 K 线：日期,开,收,高,低,成交量,...
        parts = line.split(",")
        if len(parts) < 3:
            continue
        date, close = parts[0].strip(), parts[2].strip()
        if (date, meta["name"], basis) in have:
            continue
        rows.append({"日期": date, "原料": meta["name"], "类别": meta["cat"],
                     "单价": close, "单位": meta["unit"], "涨跌幅": "",
                     "口径": basis, "来源": EM_SRC, "备注": meta["desc"]})
        added += 1
    # 按日期排序后重算同口径环比
    rows.sort(key=lambda r: (r.get("日期") or "", r.get("原料") or ""))
    _recalc_pct(rows)
    _write_csv(HIST_PATH, HIST_COLS, rows)
    print("补录 %s（%s）历史 %d 条，口径标记「%s」" % (meta["name"], secid, added, basis))
    return added


def _recalc_pct(rows):
    """就地重算每条的同口径环比。"""
    last = {}
    for r in rows:
        key = (r.get("原料"), r.get("口径") or BASIS_CONT)
        try:
            cur = float(str(r.get("单价") or "").replace(",", ""))
        except Exception:
            cur = None
        prev = last.get(key)
        r["涨跌幅"] = "" if _pct(cur, prev) is None else _pct(cur, prev)
        if cur:
            last[key] = cur


# ------------------------------------------------------------------ 命令
def _spot_adapter():
    """现货数据源适配器（ABS/PC/PS 这类没有免费 API 的品种走这里）。

    目前**没有**任何可用的免费现货源，所以内置表是空的——这是有意的，
    不编造数据比假装能查更重要。将来拿到付费授权（生意社/卓创/中塑在线/
    同花顺 iFinD），只要在这里注册一个 `{key: fn}` 的取价函数，
    `fetch` 就会自动带上这些品种，CSV 结构与展示层完全不用动。

    config.yaml 里 `commodities.spot_source: ifind` 只是**声明**，
    真正生效需要在下表注册同名实现，否则 fetch 会明确告诉你「未实现」而不是静默跳过。
    """
    return {}


def _fetch_spot(keys, rows, today):
    """走现货适配器补录人工品种。返回 (写入数, 提示行列表)。"""
    cfg = _cfg()
    want = cfg.get("spot_source")
    manual = [k for k in (keys or list(BY_KEY)) if not BY_KEY.get(k, {}).get("auto")]
    if not manual:
        return 0, []
    adapter = _spot_adapter()
    if not want:
        return 0, ["   · %s 无免费公开现货源，用 `raw add %s --price <单价>` 人工录入"
                   % (BY_KEY[k]["name"], k) for k in manual]
    fn = adapter.get(str(want).strip().lower())
    if not fn:
        return 0, ["   · 已配置 spot_source=%s，但尚未实现该适配器（见 _spot_adapter）" % want]
    wrote, notes = 0, []
    for k in manual:
        try:
            got = fn(k, BY_KEY[k]) or {}
        except Exception as e:
            notes.append("   · %s 现货源取价异常：%s" % (BY_KEY[k]["name"], str(e)[:60]))
            continue
        price = got.get("price")
        if not price:
            notes.append("   · %s 现货源未返回价格" % BY_KEY[k]["name"])
            continue
        prev = _prev_price(BY_KEY[k]["name"], BASIS_MANUAL, rows)
        pct = _pct(price, prev)
        rows.append({"日期": got.get("date") or today, "原料": BY_KEY[k]["name"],
                     "类别": BY_KEY[k]["cat"], "单价": price, "单位": BY_KEY[k]["unit"],
                     "涨跌幅": "" if pct is None else pct, "口径": BASIS_MANUAL,
                     "来源": str(want), "备注": got.get("note") or BY_KEY[k]["desc"]})
        wrote += 1
        notes.append("   ✓ %-8s %10s %-8s %s" % (
            BY_KEY[k]["name"], _fmt_num(price), BY_KEY[k]["unit"],
            "" if pct is None else "%+.2f%%" % pct))
    return wrote, notes


def cmd_fetch(dry_run=False, keys=None):
    targets = [k for k in (keys or list(BY_KEY)) if BY_KEY.get(k, {}).get("auto")]
    print("原料行情拉取 · %d 个品种（新浪期货连续合约，人民币）" % len(targets))
    if dry_run:
        print("\n--dry-run 预览（不联网、不写库）：")
        for k in targets:
            print("  将查 %-4s %s（%s）" % (k, BY_KEY[k]["name"], BY_KEY[k]["unit"]))
        return 0
    quotes = fetch_quotes(targets)
    rows = _read_csv(HIST_PATH)
    today = datetime.now().strftime("%Y-%m-%d")
    wrote = failed = 0
    alerts = []
    for k in targets:
        meta = BY_KEY[k]
        q = quotes.get(k) or {}
        if not q.get("ok"):
            failed += 1
            print("  ✗ %-8s %s" % (meta["name"], q.get("error") or "无结果"))
            continue
        price = q["price"]
        prev = _prev_price(meta["name"], BASIS_CONT, rows)
        pct = _pct(price, prev)
        row = {"日期": q.get("date") or today, "原料": meta["name"],
               "类别": meta["cat"], "单价": price, "单位": meta["unit"],
               "涨跌幅": "" if pct is None else pct, "口径": BASIS_CONT,
               "来源": SINA_SRC, "备注": meta["desc"]}
        rows.append(row)
        wrote += 1
        arrow = "" if pct is None else ("↑" if pct > 0 else ("↓" if pct < 0 else "→"))
        print("  ✓ %-8s %10s %-8s %s%s" % (
            meta["name"], _fmt_num(price), meta["unit"],
            "" if pct is None else "%+.2f%% %s " % (pct, arrow),
            "" if prev is None else ""))
        if pct is not None and abs(pct) >= _alert_threshold():
            alerts.append("%s %+.2f%%（%s→%s %s）" % (
                meta["name"], pct, _fmt_num(prev), _fmt_num(price), meta["unit"]))

    # 现货品种（ABS/PC/PS）：有适配器就自动补，没有就明确提示人工录入，绝不静默跳过
    spot_wrote, spot_notes = _fetch_spot(keys, rows, today)
    wrote += spot_wrote

    _write_csv(HIST_PATH, HIST_COLS, rows)
    print("\n" + "-" * 62)
    if spot_notes:
        print("现货品种（无免费公开源）：")
        for n in spot_notes:
            print(n)
    print("写入 %d 条 · 失败 %d 条 · 数据文件 data/原料行情记录.csv" % (wrote, failed))
    if alerts:
        print("\n[!] 波动超阈值（%.1f%%）%d 项：" % (_alert_threshold(), len(alerts)))
        for a in alerts:
            print("   ·", a)
    return 0


def cmd_show(days=30):
    rows = _read_csv(HIST_PATH)
    if not rows:
        print("还没有原料行情记录。先跑：python commodities.py fetch")
        return 0
    # 按原料 + 口径分组
    groups = {}
    for r in rows:
        if not r.get("日期"):
            continue
        if days and r["日期"] < (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d"):
            continue
        groups.setdefault((r.get("原料"), r.get("口径") or BASIS_CONT), []).append(r)
    print("原料行情走势（近 %s 天）" % days)
    print("-" * 78)
    print("%-8s %-14s %12s %-8s %10s  %s" % (
        "原料", "口径", "最新价", "单位", "区间涨跌", "走势"))
    print("-" * 86)
    for (name, basis), rs in sorted(groups.items()):
        rs.sort(key=lambda r: r.get("日期") or "")
        vals = []
        for r in rs:
            try:
                vals.append(float(str(r.get("单价") or "").replace(",", "")))
            except Exception:
                pass
        if not vals:
            continue
        unit = (rs[-1].get("单位") or "")
        # 单点序列没有「区间」可言，标 — 而不是 +0.00%
        pct = _pct(vals[-1], vals[0]) if len(vals) >= 2 else None
        print("%-8s %-14s %12s %-8s %10s  %s" % (
            name, _basis_label(basis), _fmt_num(vals[-1]), unit,
            "—" if pct is None else "%+.2f%%" % pct, _sparkline(vals)))
    print("-" * 86)
    print("共 %d 个序列 · %d 条记录" % (len(groups), len(rows)))
    print("口径说明：连续·实时=新浪连续合约当日价（每日 fetch 积累）；"
          "xxxx·历史=东财具体合约 K 线（backfill 补录）")
    return 0


def _basis_label(b):
    """把内部口径值显示成人话。"""
    if b == BASIS_CONT:
        return "连续·实时"
    if b == BASIS_MANUAL:
        return "现货·人工"
    if b.startswith("合约"):
        return b[2:] + "·历史"
    return b


def cmd_trend(days=30):
    rows = _read_csv(HIST_PATH)
    if not rows:
        print("还没有原料行情记录。先跑：python commodities.py fetch")
        return 0
    th = _alert_threshold()
    cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    groups = {}
    for r in rows:
        if r.get("日期") and r["日期"] >= cutoff:
            groups.setdefault((r.get("原料"), r.get("口径") or BASIS_CONT), []).append(r)
    hits = []
    for (name, basis), rs in sorted(groups.items()):
        rs.sort(key=lambda r: r.get("日期") or "")
        vals = []
        for r in rs:
            try:
                vals.append(float(str(r.get("单价") or "").replace(",", "")))
            except Exception:
                pass
        if len(vals) < 2:
            continue
        pct = _pct(vals[-1], vals[0])
        if pct is not None and abs(pct) >= th:
            hits.append((abs(pct), name, basis, pct, vals[0], vals[-1], rs[-1].get("单位") or ""))
    hits.sort(reverse=True)
    print("原料波动告警（近 %s 天，阈值 ±%.1f%%）" % (days, th))
    print("-" * 70)
    if not hits:
        print("无超阈值波动。")
        return 0
    for _, name, basis, pct, first, last, unit in hits:
        print("  %-8s %-14s %+7.2f%%   %s → %s %s" % (
            name, _basis_label(basis), pct, _fmt_num(first), _fmt_num(last), unit))
    print("-" * 70)
    print("提示：锡/铜直接决定 SMT 焊料与 PCB 成本，金银影响镀层，波动大时复核报价有效期。")
    return 0


def cmd_add(key, price, date=None, note=""):
    """人工录入现货价（ABS/PC/PS 等无免费 API 的品种）。"""
    meta = BY_KEY.get(key.upper())
    if not meta:
        print("未知原料 %s。可用：%s" % (key, "、".join(BY_KEY)))
        return 1
    try:
        p = float(str(price).replace(",", ""))
    except Exception:
        print("价格解析失败：%s" % price)
        return 1
    rows = _read_csv(HIST_PATH)
    prev = _prev_price(meta["name"], BASIS_MANUAL, rows)
    pct = _pct(p, prev)
    rows.append({"日期": date or datetime.now().strftime("%Y-%m-%d"),
                 "原料": meta["name"], "类别": meta["cat"], "单价": p,
                 "单位": meta["unit"], "涨跌幅": "" if pct is None else pct,
                 "口径": BASIS_MANUAL, "来源": "人工录入",
                 "备注": note or meta["desc"]})
    _write_csv(HIST_PATH, HIST_COLS, rows)
    print("已录入 %s %s %s（环比 %s）" % (
        meta["name"], _fmt_num(p), meta["unit"],
        "—" if pct is None else "%+.2f%%" % pct))
    return 0


def cmd_list():
    """列出可监控品种、数据源与口径。"""
    print("%-6s %-8s %-8s %-8s %s" % ("代码", "名称", "类别", "数据源", "说明"))
    print("-" * 88)
    for k, m in BY_KEY.items():
        print("%-6s %-8s %-8s %-8s %s" % (
            k, m["name"], m["cat"], "新浪" if m["auto"] else "人工", m["desc"]))
        print("       └ %s" % (m["sina"] + "（连续合约）" if m["auto"]
                              else "现货无免费公开 API，需人工录入"))
    print("-" * 88)
    print("塑料说明：ABS/PC/PS 是石化下游现货，公开免费 API 缺位；"
          "PP/LLDPE/PVC 期货作为其上游石化链的代理指标。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="原料行情监控（金/银/铜/锡 + 塑料原料）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="拉实时价并写库")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", help="只查指定品种，逗号分隔，如 AU,CU,SN")

    p = sub.add_parser("show", help="打印走势")
    p.add_argument("--days", type=int, default=30)

    p = sub.add_parser("trend", help="波动告警")
    p.add_argument("--days", type=int, default=30)

    p = sub.add_parser("backfill", help="补历史（东财 K 线）")
    p.add_argument("key", help="品种代码，如 AU")
    p.add_argument("--contract", required=True, help="东财合约代码，如 au2610")
    p.add_argument("--days", type=int, default=60)

    p = sub.add_parser("add", help="人工录入现货价（ABS/PC/PS）")
    p.add_argument("key")
    p.add_argument("--price", required=True)
    p.add_argument("--date")
    p.add_argument("--note", default="")

    sub.add_parser("list", help="列出可监控品种与数据源")
    a = ap.parse_args()

    if a.cmd == "fetch":
        keys = [k.strip().upper() for k in (a.only or "").split(",") if k.strip()] or None
        return cmd_fetch(dry_run=a.dry_run, keys=keys)
    if a.cmd == "show":
        return cmd_show(days=a.days)
    if a.cmd == "trend":
        return cmd_trend(days=a.days)
    if a.cmd == "backfill":
        backfill(a.key.upper(), a.contract, a.days)
        return 0
    if a.cmd == "add":
        return cmd_add(a.key, a.price, a.date, a.note)
    if a.cmd == "list":
        return cmd_list()
    return 0


if __name__ == "__main__":
    sys.exit(main())
