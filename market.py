#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""market.py — 物料行情监控（价格涨跌 + 停产/EOL 生命周期）。

数据文件（均在 data/ 下，本地专用，不会被 seatable_sync 覆盖）：
  物料监控清单.csv  —— 盯哪些物料（型号/名称/类别/上次采购价/启用）
  物料行情记录.csv  —— 行情快照，纯追加（日期/型号/渠道/单价/涨跌幅/生命周期/来源）

命令：
  python market.py watchlist [--refresh]   # 生成/刷新监控清单（--refresh 从采购记录重新提取合并）
  python market.py add <型号> [--name 名称] [--category 类别] [--price 上次采购价]
  python market.py remove <型号> [--yes]
  python market.py enable <型号> 0|1        # 暂停/恢复监控
  python market.py snapshot --model <型号> [--price 12.5] [--channel 立创]
                             [--lifecycle 在产|NRND|EOL停产|未知]
                             [--source URL] [--note 备注]
                                          # 追加一条行情快照（涨跌幅自动对比上一条）
  python market.py report                  # 打印各物料最新行情 vs 上次采购价
  python market.py alerts                  # 打印告警（涨跌超阈值 / 停产·NRND）
  python market.py lookup <型号> [--source digikey|mouser] [--qty 100]
                                          # 代理商官方 API 查价（**只查不写**）
  python market.py compare <型号> [--qty 100]   # 多源比价，原价与人民币并列
  python market.py sync [--source ...] [--force] [--dry-run] [--limit N]
                                          # 按自适应节奏批量拉价并写快照

自适应节奏（省 API 配额）：
  按「库存电子料数量」自动决定复查间隔，逐型号比对上次快照日期，未到期直接跳过：
    ≤100 种 → 每 7 天（每周） · 101~300 种 → 每 15 天（半月） · >300 种 → 每 30 天（每月）
  数量来源 data/partdb_snapshot.json 的 part_count，阈值可用 config.yaml
  market.cadence / market.cadence_tiers 覆盖；market.cadence 写死数字则不做自适应。
  节奏之外还有两道闸门（sync 依次过滤，越往后越省）：
    ① 本地预筛：P1 / Z3.5 / 458*3 / 26MHz/0.5ppm 这类内部料号与规格描述，
       不是制造商型号，永远查不到 —— 本地判掉，不烧配额
    ② 未知缓存：近 30 天确认「查无此型号 / 无报价」的型号记进
       data/.cache/unknown_mpn.json，TTL 内不再查（--force 可绕过）

注意：得捷/贸泽是欧美代理商，对国产料与定制料号基本零覆盖。
本仓库 25 个启用型号（国产芯片为主）实测贸泽有效命中 0 条 ——
这两个 API 适合选型阶段查新料号（lookup/compare），国产 BOM 的行情巡检
仍以人工录入与立创商城为主。

涨跌幅口径：
  有渠道时，**只跟同渠道的上一条有单价快照比**（得捷跟得捷比、贸泽跟贸泽比）——
  多渠道各写一条时，串渠道比对会把渠道差价误报成涨跌；
  渠道留空（人工录入）才退回「同型号任意渠道的上一条」。
  「vs 上次采购价」单独列出，作为备货决策基准。
生命周期：在产（绿）/ NRND（橙，即将停产）/ EOL停产（红）/ 未知（灰）。
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
WATCH_PATH = os.path.join(DATA, "物料监控清单.csv")
HIST_PATH = os.path.join(DATA, "物料行情记录.csv")

WATCH_COLS = ["物料型号", "物料名称", "类别", "来源", "上次采购价", "上次采购日期", "启用", "备注"]
HIST_COLS = ["日期", "物料型号", "渠道", "单价", "涨跌幅", "生命周期", "来源链接", "备注"]

# 查无此型号的缓存：代理商不代理的料，没必要每月重复消耗配额。
# 2026-09-02 实测：25 个启用型号里 24 个在贸泽查无此型号（多为国产料/内部规格描述），
# 若不缓存，每月白白烧掉 24 次配额且刷屏。--force 可绕过。
UNKNOWN_CACHE = os.path.join(DATA, ".cache", "unknown_mpn.json")
UNKNOWN_TTL_DAYS = 30

LIFECYCLE_OK = "在产"
LIFECYCLE_WARN = "NRND"
LIFECYCLE_BAD = "EOL停产"
LIFECYCLE_UNK = "未知"
_BAD_LIFECYCLE = {LIFECYCLE_WARN, LIFECYCLE_BAD, "停产", "EOL", "Not Recommended", "NRND/LTB"}

ALERT_THRESHOLD = 10.0  # 默认涨跌预警阈值（%），config.yaml market.alert_threshold 可覆盖


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("¥", "").strip())
    except Exception:
        return 0.0


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _threshold():
    try:
        from adapters.factory import load_config
        cfg = load_config() or {}
        mk = cfg.get("market") or {}
        return float(mk.get("alert_threshold") or ALERT_THRESHOLD)
    except Exception:
        return ALERT_THRESHOLD


# ---------------------------------------------------------------- watchlist
_MODEL_STOP = {"型号", "料号", "型号/规格", "物料", "名称", "描述"}
_PRICE_HDR = {"单价", "含税单价", "价格", "单价(元)"}


def _norm_lifecycle(v):
    v = (v or "").strip()
    if not v:
        return LIFECYCLE_UNK
    low = v.upper()
    if LIFECYCLE_OK in v:
        return LIFECYCLE_OK
    if "NRND" in low or "不推荐" in v or "即将停产" in v or "LAST" in low:
        return LIFECYCLE_WARN
    if "EOL" in low or "停产" in v or "DISCONTINU" in low or "已停" in v:
        return LIFECYCLE_BAD
    if "未知" in v or "?" == v.strip():
        return LIFECYCLE_UNK
    return v


def _guess_category(name):
    s = (name or "").upper()
    if "GNSS" in s or "GPS" in s:
        return "GNSS模块"
    if "UWB" in s:
        return "UWB模块"
    if "马达" in s or "MOTOR" in s:
        return "马达"
    if "天线" in s or "ANT" in s or "顶针" in s or "PG" in s:
        return "天线/顶针"
    if "SIM" in s:
        return "SIM卡座"
    if re.search(r"[A-Z]{2,}[0-9]{2,}", s):
        return "IC"
    if "PCB" in s:
        return "PCB"
    if "电池" in s or "BATT" in s:
        return "电池"
    return "其他"


def _clean_model(name):
    """去掉 markdown 转义与 HTML 实体残留。"""
    s = name.replace("\\_", "_").replace("\\*", "*").replace("&amp;", "&")
    s = re.sub(r"&#x?[0-9A-Fa-f]+;", "", s).replace("\xa0", " ").strip()
    return s


def _is_model_like(name):
    """像料号的才自动监控：含字母或中文部件名；排除纯数字、长单号、乱码。"""
    s = name.strip()
    if not s or len(s) > 32:
        return False
    if re.fullmatch(r"[0-9.\-/ ]+", s):          # 纯数字/日期
        return False
    for m in re.findall(r"[0-9]{6,}", s):        # 含 6 位以上连续数字（单号/序列号）
        if len(m) >= 10:
            return False
    if re.search(r"[\u4e00-\u9fff]", s) and not re.search(r"[A-Za-z0-9]{2,}", s):
        return False                              # 纯中文长描述
    return True


def _extract_from_purchase_tables():
    """从采购记录 CSV 的物料清单列里提取（型号, 单价, 来源表）。

    物料清单列混杂 Markdown 表格（含型号/数量/单价）和纯文本行（只有型号），
    只做保守提取：表格行取第一列当型号、数字尾列当单价；纯文本行整行当型号。
    """
    out = {}  # model -> {price, table, date}
    tables = ["IC采购记录", "组装料采购记录", "成品采购记录", "外壳采购记录", "PCBA半成品采购记录"]
    for t in tables:
        path = os.path.join(DATA, t + ".csv")
        if not os.path.exists(path):
            continue
        for row in _read_csv(path):
            blob = row.get("物料清单") or row.get("采购清单") or ""
            date = row.get("下单时间") or row.get("采购时间") or ""
            lines = [ln.strip() for ln in re.split(r"[\n\r]+", blob) if ln.strip()]
            for ln in lines:
                if ln.startswith("#"):
                    continue
                if ln.startswith("|"):
                    cells = [c.strip() for c in ln.strip("|").split("|")]
                    cells = [c for c in cells if c != ""]
                    if len(cells) < 2:
                        continue
                    head = _clean_model(cells[0])
                    if not head or head in _MODEL_STOP or set(head) <= {"-", " ", ":"}:
                        continue
                    if re.fullmatch(r"[-: ]+", ln.replace("|", "")):
                        continue
                    # 最后一列若是数字且 ≤5 位数（单价），倒数第二列若是千级数字是数量
                    price = ""
                    for c in reversed(cells[1:]):
                        n = _num(c)
                        if c and re.fullmatch(r"[0-9.,]+", c) and 0 < n < 100000:
                            # 数量列通常是整数且大，单价常有小数；都记录，取更小的当单价
                            price = c
                            break
                    out.setdefault(head, {"price": "", "table": t, "date": date})
                    if price and not out[head]["price"]:
                        # 简单区分：带小数点或 <100 视为单价
                        try:
                            pv = float(price.replace(",", ""))
                            if "." in price or pv < 100:
                                out[head]["price"] = price
                        except Exception:
                            pass
                else:
                    # 纯文本行：去掉markdown/HTML残留，长度限制防整段文字当型号
                    name = _clean_model(re.sub(r"[*`>#]+", "", ln).strip())
                    if 1 < len(name) <= 40 and not re.search(r"[。；;！!？?，,]", name):
                        out.setdefault(name, {"price": "", "table": t, "date": date})
    # 过滤明显不是物料的词
    drop = {"数量", "单价", "合计", "总计", "备注", "序号"}
    return {k: v for k, v in out.items() if k not in drop and not k.isdigit()}


def _default_enabled(model, price):
    """有采购价 + 型号干净 → 默认启用；其余进清单但停用（可手动 enable）。
    纯内部 IPN（P0059 这类）网上查不到行情，默认停用。"""
    if re.fullmatch(r"P\d{3,6}", model.strip()):
        return "0"
    return "1" if (price and _is_model_like(model)) else "0"


def cmd_watchlist(refresh=False):
    exists = os.path.exists(WATCH_PATH)
    current = {r["物料型号"]: r for r in _read_csv(WATCH_PATH)}
    if not exists or refresh:
        got = _extract_from_purchase_tables()
        merged = dict(current)  # 已有手工条目优先，不覆盖
        for model, info in got.items():
            if model in merged:
                # 刷新上次采购价/日期（仅在为空时补）
                r = merged[model]
                if not r.get("上次采购价") and info["price"]:
                    r["上次采购价"] = info["price"]
                if not r.get("上次采购日期") and info["date"]:
                    r["上次采购日期"] = str(info["date"])[:10]
                r["来源"] = info["table"]
                continue
            merged[model] = {
                "物料型号": model, "物料名称": model, "类别": _guess_category(model),
                "来源": info["table"], "上次采购价": info["price"],
                "上次采购日期": str(info["date"])[:10],
                "启用": _default_enabled(model, info["price"]), "备注": "",
            }
        rows = sorted(merged.values(), key=lambda r: (r.get("类别", ""), r["物料型号"]))
        _write_csv(WATCH_PATH, WATCH_COLS, rows)
        print(f"[ok] 监控清单：{len(rows)} 项 -> {os.path.basename(WATCH_PATH)}"
              + ("（已合并采购记录新提取）" if refresh else "（首次生成）"))
    else:
        rows = _read_csv(WATCH_PATH)
        print(f"[ok] 监控清单已存在：{len(rows)} 项（--refresh 可从最新采购记录合并新物料）")
    for r in rows:
        print("  %-4s %-28s %-10s 采购价 %s %s" % (
            "启用" if r.get("启用", "1") not in ("0", "否", "false") else "停用",
            r.get("物料型号", "")[:28], r.get("类别", ""),
            r.get("上次采购价", "") or "—", r.get("上次采购日期", "")))


def cmd_add(model, name=None, category=None, price=None):
    rows = _read_csv(WATCH_PATH)
    for r in rows:
        if r["物料型号"] == model:
            print("[skip] 已存在：%s（如需修改请先 remove）" % model)
            return
    rows.append({
        "物料型号": model, "物料名称": name or model,
        "类别": category or _guess_category(name or model),
        "来源": "手工添加", "上次采购价": price or "", "上次采购日期": "",
        "启用": "1", "备注": "",
    })
    _write_csv(WATCH_PATH, WATCH_COLS, rows)
    print("[ok] 已添加监控物料：%s" % model)


def cmd_remove(model, yes=False):
    rows = _read_csv(WATCH_PATH)
    keep = [r for r in rows if r["物料型号"] != model]
    if len(keep) == len(rows):
        print("[skip] 清单里没有：%s" % model)
        return
    if not yes:
        print("将删除监控物料「%s」及其中断监控；确认请加 --yes" % model)
        return
    _write_csv(WATCH_PATH, WATCH_COLS, keep)
    print("[ok] 已删除：%s" % model)


def cmd_enable(model, flag):
    rows = _read_csv(WATCH_PATH)
    hit = False
    for r in rows:
        if r["物料型号"] == model:
            r["启用"] = "1" if flag in ("1", "true", "是") else "0"
            hit = True
    if not hit:
        print("[skip] 清单里没有：%s" % model)
        return
    _write_csv(WATCH_PATH, WATCH_COLS, rows)
    print("[ok] %s → %s" % (model, "启用" if flag in ("1", "true", "是") else "停用"))


# ---------------------------------------------------------------- snapshot
def cmd_snapshot(model, price=None, channel="", lifecycle="", source="", note=""):
    watch = {r["物料型号"]: r for r in _read_csv(WATCH_PATH)}
    if watch and model not in watch:
        print("[warn] 「%s」不在监控清单，仍写入快照（可 market.py add 收编）" % model)
    hist = _read_csv(HIST_PATH)
    prev_price = None
    for r in hist:
        if r.get("物料型号") != model:
            continue
        # 多渠道同步（得捷/贸泽各写一条）时，**只跟同渠道的上一条比**。
        # 否则 A 渠道的价会拿去跟 B 渠道比，渠道差价随随便便就超 10% 阈值，
        # 刷出一堆假涨跌告警。渠道为空（人工录入）才退回「同型号任意渠道」。
        if channel and (r.get("渠道") or "") != channel:
            continue
        if _num(r.get("单价")) > 0:
            prev_price = _num(r.get("单价"))
    chg = ""
    if price is not None and prev_price:
        try:
            p = float(price)
            chg = round((p - prev_price) / prev_price * 100, 1)
        except Exception:
            chg = ""
    row = {
        "日期": _today(), "物料型号": model, "渠道": channel or "",
        "单价": "" if price is None else price, "涨跌幅": chg,
        "生命周期": _norm_lifecycle(lifecycle), "来源链接": source or "", "备注": note or "",
    }
    hist.append(row)
    _write_csv(HIST_PATH, HIST_COLS, hist)
    print("[ok] 行情快照已写入：%s ¥%s %s（环比 %s）生命周期 %s" % (
        model, row["单价"] or "—", channel or "", ("%+.1f%%" % chg) if chg != "" else "—",
        row["生命周期"]))
    # 高危提醒
    if row["生命周期"] in _BAD_LIFECYCLE:
        print("[!!!] 生命周期告警：%s 状态 %s —— 建议尽快确认替代料/最后采购窗口" % (model, row["生命周期"]))


# ---------------------------------------------------------------- report/alerts
def _latest_state():
    """返回 [{model, watch_row, latest, prev, hist_prices, vs_buy_pct}]。"""
    watch = _read_csv(WATCH_PATH)
    hist = _read_csv(HIST_PATH)
    by_model = {}
    for r in hist:
        by_model.setdefault(r.get("物料型号", ""), []).append(r)
    out = []
    for w in watch:
        if w.get("启用", "1") in ("0", "否", "false"):
            continue
        m = w["物料型号"]
        snaps = [s for s in by_model.get(m, []) if _num(s.get("单价")) > 0]
        latest_all = by_model.get(m, [])
        latest = latest_all[-1] if latest_all else None
        buy = _num(w.get("上次采购价"))
        cur = _num((latest or {}).get("单价"))
        vs_buy = round((cur - buy) / buy * 100, 1) if (buy > 0 and cur > 0) else None
        out.append({
            "model": m, "watch": w, "latest": latest,
            "hist_prices": [_num(s.get("单价")) for s in snaps][-12:],
            "hist_dates": [s.get("日期", "") for s in snaps][-12:],
            "vs_buy_pct": vs_buy,
        })
    return out


def _alerts(states=None):
    states = states if states is not None else _latest_state()
    th = _threshold()
    out = []
    for s in states:
        lc = (s["latest"] or {}).get("生命周期", "")
        if lc in _BAD_LIFECYCLE:
            out.append({"type": "停产", "model": s["model"], "text":
                        "生命周期 %s —— 确认替代料/最后采购窗口" % lc})
        vb = s["vs_buy_pct"]
        if vb is not None and abs(vb) >= th:
            out.append({"type": "涨跌", "model": s["model"], "text":
                        "现价较上次采购价 %+.1f%%（阈值 ±%.0f%%）" % (vb, th)})
        chg = (s["latest"] or {}).get("涨跌幅", "")
        if chg not in ("", None):
            try:
                if abs(float(chg)) >= th:
                    out.append({"type": "涨跌", "model": s["model"], "text":
                                "最新快照环比 %+.1f%%" % float(chg)})
            except Exception:
                pass
    return out


def cmd_report():
    states = _latest_state()
    if not states:
        print("（监控清单为空，先 python market.py watchlist 生成）")
        return
    print("%-28s %-10s %10s %10s %8s  %-8s %s" % (
        "物料型号", "类别", "上次采购价", "最新行情", "vs采购", "生命周期", "渠道/日期"))
    for s in states:
        w, l = s["watch"], s["latest"] or {}
        vb = ("%+.1f%%" % s["vs_buy_pct"]) if s["vs_buy_pct"] is not None else "—"
        print("%-28s %-10s %10s %10s %8s  %-8s %s %s" % (
            s["model"][:28], w.get("类别", ""),
            w.get("上次采购价", "") or "—",
            l.get("单价", "") or "—", vb,
            l.get("生命周期", "") or "未知",
            l.get("渠道", ""), l.get("日期", "")))


def cmd_alerts():
    al = _alerts()
    if not al:
        print("（无行情告警：无停产物料，涨跌均在阈值内）")
        return
    for a in al:
        print("[%-2s] %s：%s" % (a["type"], a["model"], a["text"]))


# ---------------------------------------------------------------- 代理商查价
def _market_cfg():
    """读 config.yaml 的 market 段（失败返回空 dict，不抛异常）。"""
    try:
        from adapters.factory import load_config
        cfg = load_config() or {}
        return cfg.get("market") or {}
    except Exception:
        return {}


def _enabled_models():
    """监控清单里启用中的型号。"""
    return [w["物料型号"] for w in _read_csv(WATCH_PATH)
            if w.get("物料型号") and w.get("启用", "1") not in ("0", "否", "false")]


def _part_count():
    """(库存电子料数量, 来源说明)。优先 PartDB 快照，退回监控清单启用数。"""
    try:
        p = os.path.join(DATA, "partdb_snapshot.json")
        if os.path.exists(p):
            d = json.load(open(p, "r", encoding="utf-8"))
            if d.get("part_count"):
                return int(d["part_count"]), "PartDB 快照 %s" % (d.get("generated_at") or "")
    except Exception:
        pass
    return len(_enabled_models()), "监控清单（PartDB 快照不可用）"


DEFAULT_TIERS = [[100, 7], [300, 15], [9999999, 30]]


def _cadence_days(count):
    """按库存电子料数量返回 (复查间隔天数, 说明)。"""
    cfg = _market_cfg()
    c = cfg.get("cadence")
    if c not in (None, "", "auto"):
        try:
            n = int(c)
            return n, "固定每 %d 天（config market.cadence）" % n
        except Exception:
            pass
    tiers = cfg.get("cadence_tiers") or DEFAULT_TIERS
    try:
        for limit, days in tiers:
            if count <= int(limit):
                return int(days), "自适应：%d 种 → 每 %s 天" % (count, days)
        return int(tiers[-1][1]), "自适应：%d 种 → 每 %s 天" % (count, tiers[-1][1])
    except Exception:
        return 30, "自适应（阈值解析失败，退回 30 天）"


def _due_models(models, channels, interval, force=False):
    """按 (型号, 渠道) 的上次快照日期判断是否到期 → {型号: [待查渠道]}。"""
    if force:
        return {m: list(channels) for m in models}
    last = {}
    for r in _read_csv(HIST_PATH):
        k = (r.get("物料型号", ""), r.get("渠道", ""))
        d = r.get("日期", "")
        if d and d > last.get(k, ""):
            last[k] = d
    today = datetime.now().date()
    due = {}
    for m in models:
        need = []
        for ch in channels:
            d = last.get((m, ch))
            if not d:
                need.append(ch)
                continue
            try:
                if (today - datetime.strptime(d, "%Y-%m-%d").date()).days >= interval:
                    need.append(ch)
            except Exception:
                need.append(ch)
        if need:
            due[m] = need
    return due


def _offer_note(o):
    """把原始币种 / 汇率 / 库存 / 交期压进快照的备注列，便于日后追溯。"""
    bits = []
    if o.get("price") is not None:
        bits.append("原价 %s %s" % (o.get("currency") or "", o.get("price")))
    if o.get("fx_rate") not in (None, 1.0, 1):
        bits.append("汇率 %s(%s)" % (o.get("fx_rate"), o.get("fx_src") or ""))
    if o.get("stock") is not None:
        bits.append("库存 %s" % o["stock"])
    if o.get("lead_time"):
        bits.append("交期 %s" % o["lead_time"])
    if o.get("qty") and o.get("qty") != 1:
        bits.append("档位 MOQ%s" % o["qty"])
    return " · ".join(bits)


def _load_suppliers():
    try:
        import suppliers
        return suppliers
    except Exception as e:
        print("[!] 无法加载 suppliers.py：%s" % e)
        return None


def cmd_lookup(model, source=None, qty=1):
    """代理商官方 API 查价——**只查不写**。"""
    sp = _load_suppliers()
    if not sp:
        return
    srcs = [source] if source else sp.available_sources()
    if not srcs:
        print("没有已配置凭证的渠道：填 config.yaml 的 market.api_keys，"
              "或先跑 python suppliers.py doctor")
        return
    print("查价 %s（目标数量 %d）—— 只查不写\n" % (model, qty))
    for o in sp.lookup(model, sources=srcs, qty=qty):
        if not o.get("ok"):
            print("  %-6s ✗ %s" % (o["source"], o.get("error") or "无结果"))
            continue
        print("  %-6s ✓ %s" % (o["source"], o.get("mpn") or ""))
        if o.get("desc"):
            print("         %s" % o["desc"][:70])
        print("         价格 %s %s = ¥%s（%s）" % (
            o.get("currency") or "", o.get("price"), o.get("price_cny"),
            o.get("fx_src") or ""))
        print("         库存 %s · 生命周期 %s · %s" % (
            o.get("stock"), o.get("lifecycle"), o.get("url") or ""))


def cmd_compare(model, qty=1):
    """多源比价——只查不写。"""
    sp = _load_suppliers()
    if not sp:
        return
    srcs = sp.available_sources()
    if not srcs:
        print("没有已配置凭证的渠道。")
        return
    offers = [o for o in sp.lookup(model, sources=srcs, qty=qty) if o.get("ok")]
    if not offers:
        print("%s：所有渠道都无结果" % model)
        for o in sp.lookup(model, sources=srcs, qty=qty):
            print("  %-6s ✗ %s" % (o["source"], o.get("error") or ""))
        return
    print("比价 %s（目标数量 %d）" % (model, qty))
    print("%-6s %-22s %14s %14s %10s %-8s" % ("渠道", "型号", "原价", "人民币", "库存", "生命周期"))
    print("-" * 82)
    for o in sorted(offers, key=lambda x: (x.get("price_cny") is None, x.get("price_cny") or 0)):
        print("%-6s %-22s %14s %14s %10s %-8s" % (
            o["source"], (o.get("mpn") or "")[:22],
            ("%s %s" % (o.get("currency") or "", o.get("price"))) if o.get("price") is not None else "—",
            ("¥%s" % o["price_cny"]) if o.get("price_cny") is not None else "—",
            o.get("stock") if o.get("stock") is not None else "—",
            o.get("lifecycle") or ""))
    good = [o for o in offers if o.get("price_cny") is not None]
    if len(good) >= 2:
        lo = min(good, key=lambda x: x["price_cny"])
        hi = max(good, key=lambda x: x["price_cny"])
        print("-" * 82)
        print("最低：%s ¥%s ｜ 最高：%s ¥%s ｜ 差价 %.1f%%" % (
            lo["source"], lo["price_cny"], hi["source"], hi["price_cny"],
            (hi["price_cny"] - lo["price_cny"]) / lo["price_cny"] * 100))


# 不匹配制造商型号的特征：规格描述、单位、运算符、中文、过短、纯数字
_MPN_BAD_RE = [
    re.compile(r"[\u4e00-\u9fff]"),                                  # 中文
    re.compile(r"[/*]"),                                             # 458*3 / 26MHz/0.5ppm
    re.compile(r"^\d+(\.\d+)?$"),                                    # 纯数字
    re.compile(r"\d\s*(mhz|khz|ghz|ppm|mm|cm|uf|pf|nf|mh|uh|kω|ω|ma|a|v)(?![a-z0-9])", re.I),
    re.compile(r"^[A-Za-z]{1,2}\d+\.\d+$"),    # Z3.5 / Z6.0 —— 内部简称
    re.compile(r"\s+\S*\d+\.\d+"),             # 空格后带小数：GNSS 1.57G、Z3.5 PG2.0-...
]


def _looks_like_mpn(s):
    """本地预筛：明显不是制造商型号的条目不必浪费 API 配额。

    监控清单是从采购记录自动生成的，里面有不少「内部料号/规格描述」，
    例如 P1、Z3.5、458*3、26MHz/0.5ppm、Z3.5 PG2.0-3.5-2.5-1.54 —— 这些
    在得捷/贸泽永远查不到，直接本地判掉，省配额也减少噪音。
    """
    s = (s or "").strip()
    if len(s) < 4:
        return False
    return not any(p.search(s) for p in _MPN_BAD_RE)


def _load_unknown():
    try:
        if os.path.exists(UNKNOWN_CACHE):
            return json.load(open(UNKNOWN_CACHE, "r", encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _mark_unknown(mpns):
    """把查无此型号的型号记进缓存（含日期），TTL 内不再重复查。"""
    try:
        d = _load_unknown()
        today = datetime.now().strftime("%Y-%m-%d")
        for m in mpns:
            d[m] = today
        os.makedirs(os.path.dirname(UNKNOWN_CACHE), exist_ok=True)
        json.dump(d, open(UNKNOWN_CACHE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass


def _unknown_skip(mpns, force=False):
    """返回 {型号: 上次确认日期} —— 缓存里未过期的型号。"""
    if force:
        return {}
    d = _load_unknown()
    cutoff = (datetime.now() - timedelta(days=UNKNOWN_TTL_DAYS)).strftime("%Y-%m-%d")
    return {m: dt for m, dt in d.items() if m in mpns and str(dt) >= cutoff}


def _brief_err(e, n=26):
    """把长错误压成一行短标签，完整原文留给末尾汇总。

    得捷「未订阅 API」这类指引有上百字符，78 个型号会刷 78 遍。
    """
    e = " ".join(str(e or "").split())
    return e if len(e) <= n else e[:n] + "…（见下方汇总）"


def cmd_sync(source=None, force=False, dry_run=False, limit=None, qty=1):
    """按自适应节奏批量拉价并写行情快照。"""
    sp = _load_suppliers()
    if not sp:
        return
    srcs = [source] if source else sp.available_sources()
    if not srcs:
        print("没有已配置凭证的渠道：填 config.yaml 的 market.api_keys")
        return
    models = _enabled_models()
    if not models:
        print("监控清单为空：先跑 python market.py watchlist")
        return
    count, count_src = _part_count()
    interval, why = _cadence_days(count)
    channels = [sp.SOURCE_CN.get(s, s) for s in srcs]
    due = _due_models(models, channels, interval, force=force)
    if limit:
        due = dict(list(due.items())[:int(limit)])

    print("批量拉价 · 渠道：%s" % "、".join(channels))
    print("库存电子料 %d 种（%s）→ %s" % (count, count_src, why))
    print("监控清单启用 %d 个型号：到期 %d 个，跳过 %d 个\n" % (
        len(models), len(due), len(models) - len(due)))
    if not due:
        print("全部未到期，无事可做（加 --force 强制重查）")
        return
    if dry_run:
        print("--dry-run 预览（不联网、不写库）：")
        for m in due:
            print("  将查 %s（%s）" % (m, "、".join(due[m])))
        return

    # 三道省配额闸门：本地预筛（非型号）→ 未知缓存（TTL 内）→ 节奏到期（已在 due 里）
    todo0 = list(due.keys())
    prescreen = [m for m in todo0 if not _looks_like_mpn(m)]
    todo = [m for m in todo0 if _looks_like_mpn(m)]
    unk = _unknown_skip(todo, force=force)
    todo = [m for m in todo if m not in unk]
    if prescreen:
        print("本地预筛跳过 %d 项（非制造商型号，如内部料号/规格描述）：%s\n" % (
            len(prescreen), "、".join(prescreen[:6]) + ("…" if len(prescreen) > 6 else "")))
    if unk:
        print("未知缓存跳过 %d 项（近 %d 天已确认代理商无此型号，--force 可绕过）：%s\n" % (
            len(unk), UNKNOWN_TTL_DAYS,
            "、".join(list(unk)[:6]) + ("…" if len(unk) > 6 else "")))
    if not todo:
        print("三道闸门后无可查型号，未消耗任何 API 配额。")
        return
    print("开始查询 %d 个型号…（贸泽 10 个/批，得捷逐个）\n" % len(todo))
    res = sp.lookup_many(todo, sources=srcs, qty=qty)
    ok = fail = wrote = noprice = 0
    bad_lc = []
    notfound = []            # 确认查无此型号的，结束前记进未知缓存
    err_groups = {}          # 错误原文 -> [型号]，末尾去重汇总，避免同因刷屏
    for m in todo:
        for o in (res.get(m) or []):
            if not o.get("ok"):
                fail += 1
                e = o.get("error") or "无结果"
                err_groups.setdefault(e, []).append(m)
                # 只把「确实查无此型号」的记进未知缓存；鉴权/网络类错误不记，
                # 否则订阅修好后这些型号反而被缓存挡住。
                if "无此型号" in e:
                    notfound.append(m)
                print("  ✗ %-24s %-6s %s" % (m, o["source"], _brief_err(e)))
                continue
            pc = _num(o.get("price_cny"))
            lc = o.get("lifecycle") or ""
            if pc <= 0 and lc not in _BAD_LIFECYCLE:
                # 命中但无人民币报价：不写库。
                # 一旦写进去，这条空价快照会成为该型号的「上次快照日期」，
                # 后续按节奏判定时被当作已复查而跳过——等于用一条废记录
                # 把该型号在整个复查间隔内屏蔽掉（2026-09-02 实测踩到）。
                # 例外：生命周期已是 NRND/EOL 的照写，停产预警比价格重要。
                noprice += 1
                e = "有记录但无人民币报价（可能已停产/不备货）"
                err_groups.setdefault(e, []).append(m)
                # 同样不值得重复消耗配额：无报价 = 写不进有效快照。
                # 2SK3541 就属于此类（贸泽有 ROHM 的记录，但 PriceBreaks 为空）。
                notfound.append(m)
                print("  ! %-24s %-6s %s" % (m, o["source"], e))
                continue
            ok += 1
            cmd_snapshot(m, price=(pc if pc > 0 else None), channel=o["source"],
                         lifecycle=lc, source=o.get("url") or "",
                         note=_offer_note(o))
            wrote += 1
            if lc in _BAD_LIFECYCLE:
                bad_lc.append("%s（%s：%s）" % (m, o["source"], lc))
    if notfound:
        _mark_unknown(sorted(set(notfound)))
        print("\n已把 %d 个「拿不到有效报价」的型号记入缓存（查无此型号 / 无报价，%d 天内不再重复查，--force 可绕过）" % (
            len(set(notfound)), UNKNOWN_TTL_DAYS))
    print("\n" + "-" * 62)
    print("完成：命中 %d 条 · 无结果 %d 条 · 无报价跳过 %d 条 · 写入快照 %d 条" % (
        ok, fail, noprice, wrote))
    if err_groups:
        print("\n失败原因汇总（同一原因只展开一次）：")
        for e, ms in sorted(err_groups.items(), key=lambda kv: -len(kv[1])):
            print("  [%d 项] %s" % (len(ms), e))
            print("       型号：%s%s" % ("、".join(ms[:8]), "…" if len(ms) > 8 else ""))
    if bad_lc:
        print("\n[!!!] 生命周期告警 %d 项：" % len(bad_lc))
        for x in bad_lc:
            print("  ·", x)
        print("建议：确认替代料 / 抓住最后采购窗口")


def main():
    ap = argparse.ArgumentParser(description="物料行情监控（价格涨跌 + 停产/EOL）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("watchlist", help="生成/刷新监控清单")
    p.add_argument("--refresh", action="store_true", help="从最新采购记录合并新物料")
    p = sub.add_parser("add", help="添加监控物料")
    p.add_argument("model")
    p.add_argument("--name"); p.add_argument("--category"); p.add_argument("--price")
    p = sub.add_parser("remove", help="删除监控物料")
    p.add_argument("model"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("enable", help="启用/停用监控")
    p.add_argument("model"); p.add_argument("flag")
    p = sub.add_parser("snapshot", help="写入一条行情快照")
    p.add_argument("--model", required=True)
    p.add_argument("--price")
    p.add_argument("--channel", default="")
    p.add_argument("--lifecycle", default="")
    p.add_argument("--source", default="")
    p.add_argument("--note", default="")
    sub.add_parser("report", help="打印行情报表")
    sub.add_parser("alerts", help="打印告警")
    p = sub.add_parser("lookup", help="代理商 API 查价（只查不写）")
    p.add_argument("model")
    p.add_argument("--source", choices=["digikey", "mouser"])
    p.add_argument("--qty", type=int, default=1, help="目标采购量，用于挑价格档")
    p = sub.add_parser("compare", help="多源比价（只查不写）")
    p.add_argument("model")
    p.add_argument("--qty", type=int, default=1)
    p = sub.add_parser("sync", help="按自适应节奏批量拉价并写快照")
    p.add_argument("--source", choices=["digikey", "mouser"])
    p.add_argument("--force", action="store_true", help="忽略节奏，强制全部重查")
    p.add_argument("--dry-run", action="store_true", help="只预览将查哪些，不联网不写库")
    p.add_argument("--limit", type=int, help="只查前 N 个型号（省配额调试用）")
    p.add_argument("--qty", type=int, default=1)
    a = ap.parse_args()
    price = None
    if a.cmd == "snapshot" and a.price not in (None, ""):
        price = a.price
    {"watchlist": lambda: cmd_watchlist(a.refresh),
     "add": lambda: cmd_add(a.model, a.name, a.category, a.price),
     "remove": lambda: cmd_remove(a.model, a.yes),
     "enable": lambda: cmd_enable(a.model, a.flag),
     "snapshot": lambda: cmd_snapshot(a.model, price, a.channel, a.lifecycle, a.source, a.note),
     "report": cmd_report,
     "alerts": cmd_alerts,
     "lookup": lambda: cmd_lookup(a.model, a.source, a.qty),
     "compare": lambda: cmd_compare(a.model, a.qty),
     "sync": lambda: cmd_sync(a.source, a.force, a.dry_run, a.limit, a.qty),
     }[a.cmd]()


if __name__ == "__main__":
    main()
