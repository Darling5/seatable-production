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

涨跌幅口径：
  vs 同型号上一条有单价的快照（渠道间可能有差价，趋势仅供参考）；
  「vs 上次采购价」单独列出，作为备货决策基准。
生命周期：在产（绿）/ NRND（橙，即将停产）/ EOL停产（红）/ 未知（灰）。
"""
import argparse
import csv
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
WATCH_PATH = os.path.join(DATA, "物料监控清单.csv")
HIST_PATH = os.path.join(DATA, "物料行情记录.csv")

WATCH_COLS = ["物料型号", "物料名称", "类别", "来源", "上次采购价", "上次采购日期", "启用", "备注"]
HIST_COLS = ["日期", "物料型号", "渠道", "单价", "涨跌幅", "生命周期", "来源链接", "备注"]

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
        if r.get("物料型号") == model and _num(r.get("单价")) > 0:
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
     "alerts": cmd_alerts}[a.cmd]()


if __name__ == "__main__":
    main()
