# -*- coding: utf-8 -*-
"""foresee.py — 风险预测引擎（项目经理的第二大脑·第 4 层「想」）

三层能力到此补齐第二大脑最后缺口：
  记（SeaTable 单一真源）→ 看（驾驶舱）→ 听（微信情报）→ 【想（本引擎）】→ 说（推送）

三个计算模块（全部只读，绝不写业务库）：
  1. 合同倒排 backward：新合同/计划立项 → 历史周期分位 → 各环节最晚开始日 → 「必须立刻执行」清单
  2. 供应商画像 supplier：承诺交期 vs 实际交期 → 按类别/供应商历史偏差 → 建议采购 buffer
  3. 缺料预警 shortage：BOM 缺口 × 在途采购 → 每个在制单的缺料时点 / 最晚下单日

数据源（全部 data/ 下本地快照，由 seatable_sync.py / partdb_sync.py 维护）：
  项目.csv 生产计划.csv IC/PCBA/组装料/外壳/成品采购记录.csv partdb_snapshot.json

产出：data/foresee.json —— cockpit.py 驾驶舱「风险雷达」section 消费。

用法：
  python foresee.py            # 计算并写 data/foresee.json，终端打印风险摘要
  python foresee.py --json     # 只输出 JSON 到 stdout（调试用）
"""
import csv
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

# ── 各采购表字段差异（已实测对齐，改列名只需改这里）──
PURCHASE_TABLES = {
    "IC":     {"file": "IC采购记录.csv",      "id": "IC采购编号",   "name": "物料清单",
               "promise": "交期", "actual": "实际交期（天）", "order": "下单时间",
               "arrival": "到货时间", "status": "状态", "supplier": "供应商"},
    "PCBA":   {"file": "PCBA半成品采购记录.csv", "id": "PCBA采购编号", "name": "物料名称",
               "promise": "交期", "actual": "实际交期", "order": "下单时间",
               "arrival": "到货时间", "status": "状态", "supplier": "供应商"},
    "组装料": {"file": "组装料采购记录.csv",   "id": "组装采购编号", "name": "组装料名称",
               "promise": "交期", "actual": "实际交期", "order": "下单时间",
               "arrival": "到货时间", "status": "状态", "supplier": "供应商"},
    "外壳":   {"file": "外壳采购记录.csv",     "id": "外壳采购编号", "name": "外壳名称",
               "promise": "交期", "actual": None, "order": "采购时间",
               "arrival": "到货时间", "status": None, "supplier": "供应商"},
    "成品":   {"file": "成品采购记录.csv",     "id": "成品采购编号", "name": "物料名称",
               "promise": "交期", "actual": "实际交期", "order": "下单时间",
               "arrival": "到货时间", "status": "状态", "supplier": "供应商"},
}
# 交期字段是「天数」不是日期；实际交期同义
INTRANSIT_STATUS = ("已下单", "已付款-未到货", "未下单")   # 未到货的状态集合

# 生产环节链条（倒排的各 milestone，按「距离交货的缓冲」粗分档）
# 经验值来源：24 个已交付计划 立项→交货 中位数 24 天；采购是主要不确定项
STAGE_CHAIN = [
    # (环节, 历史观察到的合理提前量下限天数, 说明)
    ("BOM 核对/缺料确认", 30, "盘点 BOM 缺口、确认替代料"),
    ("IC/PCB 采购下单",   25, "交期最长的 IC 先下单（历史中位 8-13 天，留 buffer）"),
    ("组装料/外壳采购",   20, "历史平均晚 29 天，最不稳定，务必盯"),
    ("贴片/组装排产",     10, "齐套后贴片+组装"),
    ("测试/质检/发货",     4, "成品测试、打包、物流"),
]


# ────────────────────────── 工具 ──────────────────────────
def _read_csv(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except Exception:
        return []


def _num(s):
    try:
        v = float(str(s).strip())
        return v
    except (TypeError, ValueError):
        return None


def _date(s):
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(s).strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _pct(vals, q):
    """分位数（0-1），空列表返回 None。"""
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


# ────────────────── 模块 1：合同倒排 ──────────────────
def backward_schedule(today):
    """在制/计划中的生产计划 → 倒推各环节最晚开始日 + 立刻执行清单。

    周期基准：已交付计划的 立项→交货 实际天数（不是合同承诺），
    分位数取 p75 —— 第二大脑的原则：按「历史上真的花了多久」预警，不按「希望花多久」。
    """
    plans = _read_csv("生产计划.csv")
    projects = {r.get("项目", "").strip(): r for r in _read_csv("项目.csv") if r.get("项目")}

    # 历史周期样本（已交付计划：立项→交货）
    hist = []
    for p in plans:
        if p.get("状态") != "已交付":
            continue
        a, b = _date(p.get("立项日期")), _date(p.get("交货时间（自动记录）"))
        if a and b and (b - a).days >= 0:
            hist.append((b - a).days)
    hist_med = statistics.median(hist) if hist else None
    hist_p75 = _pct(hist, 0.75) if hist else None
    hist_p90 = _pct(hist, 0.90) if hist else None

    rows = []
    act_now = []
    for p in plans:
        if p.get("状态") in ("已交付", "已取消"):
            continue
        pid = p.get("生产计划编号", "")
        prod = (p.get("生产产品") or "")[:16]
        # 目标交期：计划自己的合同交期 > 关联项目的合同交期 > 立项+历史中位
        proj = projects.get((p.get("关联项目") or "").strip())
        due = _date(p.get("合同交期")) or (proj and _date(proj.get("合同交期")))
        start = _date(p.get("立项日期")) or _date(p.get("创建时间"))
        basis = "计划合同交期"
        if not due and proj and _date(proj.get("合同交期")):
            due = _date(proj.get("合同交期"))
            basis = "项目合同交期"
        if not due and start and hist_med:
            due = start + timedelta(days=int(hist_med))
            basis = "立项+历史中位估算"
        if not due:
            # 完全没有锚点，只能提示补数据
            rows.append({
                "plan": pid, "product": prod, "due": None, "basis": "缺交期数据",
                "days_left": None, "est_cycle": None, "verdict": "缺数据",
                "note": "无合同交期且无立项日期，请补录后重跑",
            })
            continue

        days_left = (due - today).days
        est = hist_p75 if hist_p75 is not None else None
        verdict, note = "", ""
        if days_left < 0:
            verdict = "已逾期"
            note = "交期已过 %d 天，需与客户重新确认" % (-days_left)
        elif est is not None and days_left < est:
            verdict = "高风险"
            note = "剩余 %d 天 < 历史周期 p75（%.0f 天），大概率赶不上" % (days_left, est)
        elif est is not None and days_left < hist_med:
            verdict = "偏紧"
            note = "剩余 %d 天 < 历史中位 %.0f 天，按中位节奏刚好压线" % (days_left, hist_med)
        else:
            verdict = "正常"
            note = "剩余 %d 天，周期余量充足" % days_left if est is None else \
                   "剩余 %d 天 ≥ 历史中位 %.0f 天" % (days_left, hist_med)

        # 各环节最晚开始日 = 交期 - 提前量
        stages = []
        for name, lead, desc in STAGE_CHAIN:
            latest = due - timedelta(days=lead)
            urgent = (latest - today).days <= 0
            stages.append({
                "stage": name, "lead": lead, "latest": latest.isoformat(),
                "days": (latest - today).days, "urgent": urgent, "desc": desc,
            })
        rows.append({
            "plan": pid, "product": prod, "due": due.isoformat(), "basis": basis,
            "days_left": days_left, "est_cycle": est, "verdict": verdict,
            "note": note, "stages": stages,
        })
        if verdict in ("已逾期", "高风险", "偏紧"):
            # 立刻执行 = 最晚开始日已到/已过的环节
            overdue_stages = [s["stage"] for s in stages if s["urgent"]]
            if overdue_stages:
                act_now.append({
                    "plan": pid, "product": prod, "verdict": verdict,
                    "due": due.isoformat(), "days_left": days_left,
                    "do_now": overdue_stages,
                })

    rows.sort(key=lambda r: (r.get("days_left") is None, r.get("days_left") or 999))
    act_now.sort(key=lambda a: a["days_left"])
    return {
        "hist_n": len(hist), "hist_median": hist_med,
        "hist_p75": hist_p75, "hist_p90": hist_p90,
        "plans": rows, "act_now": act_now,
    }


# ────────────────── 模块 2：供应商交期画像 ──────────────────
def supplier_profile():
    """承诺 vs 实际交期 → 类别/供应商偏差 → 建议 buffer。

    数据形态（实测）：交期列 = 承诺天数，实际交期列 = 实际天数，
    偏差 = 实际 - 承诺（正=晚）。样本 ≥2 才单独画像，否则并入类别均值。
    """
    cat_profile, sup_detail, samples = {}, {}, 0
    for cat, cols in PURCHASE_TABLES.items():
        if not cols["actual"]:
            continue  # 外壳表没有「实际交期」列，跳过画像
        rows = _read_csv(cols["file"])
        per_cat, per_sup = [], defaultdict(list)
        for r in rows:
            pr, ac = _num(r.get(cols["promise"])), _num(r.get(cols["actual"]))
            if pr is None or ac is None or pr <= 0 or ac <= 0:
                continue
            diff = ac - pr
            per_cat.append(diff)
            sup = (r.get(cols["supplier"]) or "").strip() or "未知"
            per_sup[sup].append(diff)
            samples += 1
        cat_profile[cat] = {
            "n": len(per_cat),
            "mean": round(statistics.mean(per_cat), 1) if per_cat else None,
            "max": max(per_cat) if per_cat else None,
            "buffer": int(max(0, statistics.mean(per_cat))) if per_cat else 0,
        }
        for sup, diffs in per_sup.items():
            sup_detail.setdefault(cat, []).append({
                "supplier": sup,
                "n": len(diffs),
                "mean": round(statistics.mean(diffs), 1),
                "max": max(diffs),
                # 建议 buffer = 该供应商平均偏差向上取整（至少 0）
                "buffer": int(max(0, statistics.mean(diffs))),
            })
    # 排序：类别按平均偏差降序；供应商同类别内降序
    cat_order = sorted(cat_profile, key=lambda c: -(cat_profile[c]["mean"] or 0))
    for cat in sup_detail:
        sup_detail[cat].sort(key=lambda x: -x["mean"])
    return {
        "samples": samples, "cat_order": cat_order,
        "cat_profile": cat_profile, "sup_detail": sup_detail,
    }


# ────────────────── 模块 3：缺料预警 ──────────────────
def shortage_forecast(today, sup_profile):
    """BOM 缺口 × 在途采购 → 各计划缺料结论 + 预计齐套日。

    链路：partdb_snapshot.bom.shortage（当前活动计划的缺口）
        → 采购表「生产计划」列按产品名关联（PCBA 表按编号）
        → 在途（状态∈已下单/已付款-未到货）且承诺交期能换算到货日的，抵扣缺口
        → 无在途的缺口 = 必须立刻下单，最晚下单日 = 交期 - 历史交期 - buffer
    """
    snap_path = os.path.join(DATA, "partdb_snapshot.json")
    result = {"snapshot_at": None, "plans": [], "must_order": []}
    if not os.path.exists(snap_path):
        return result
    try:
        with open(snap_path, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        return result
    result["snapshot_at"] = snap.get("generated_at")
    boms = snap.get("bom") or {}
    # partdb_sync 的 bom 是单项目 dict；防御性兼容未来多项目 list
    if isinstance(boms, dict):
        boms = [boms]
    shortage_all = []
    for b in boms:
        pname = (b.get("project_name") or "").strip()
        for s in (b.get("shortage") or []):
            shortage_all.append(dict(s, project_name=pname))
    if not shortage_all:
        return result

    plans = _read_csv("生产计划.csv")
    plan_ids = {p.get("生产计划编号", ""): p for p in plans}
    plan_products = {}
    for p in plans:
        nm = (p.get("生产产品") or "").strip()
        if nm:
            plan_products[nm] = p

    def match_plan(pname):
        """PartDB 项目名 → 生产计划：精确 → 前 6 字模糊（PartDB 命名与计划产品名非同一体系）。"""
        if pname in plan_products:
            return plan_products[pname]
        for nm, p in plan_products.items():
            if len(pname) >= 6 and (pname[:6] in nm or nm[:6] in pname):
                return p
        return None

    # 在途采购按「产品名/编号 → 预计到货日」索引
    intransit = defaultdict(list)
    for cat, cols in PURCHASE_TABLES.items():
        buf = ((sup_profile.get("cat_profile") or {}).get(cat) or {}).get("buffer") or 0
        for r in _read_csv(cols["file"]):
            status = r.get(cols["status"]) or ""
            if status not in INTRANSIT_STATUS:
                continue
            key_raw = (r.get("生产计划") or "").strip()
            # 关联键：优先产品名，PCBA 表存的是编号
            key = key_raw
            if cat == "PCBA" and key_raw in plan_ids:
                key = (plan_ids[key_raw].get("生产产品") or "").strip()
            # 预计到货日 = 下单日 + 承诺天数 + 类别 buffer
            od = _date(r.get(cols["order"]))
            pr = _num(r.get(cols["promise"]))
            eta = None
            if od and pr:
                eta = (od + timedelta(days=int(pr) + int(buf))).isoformat()
            intransit[key].append({
                "cat": cat, "id": r.get(cols["id"], ""), "name": (r.get(cols["name"]) or "")[:20],
                "supplier": (r.get(cols["supplier"]) or "")[:12], "eta": eta,
                "promise": pr, "buffer": buf, "status": status,
            })

    # 对每个有缺口的计划：缺口清单 + 在途覆盖 + 风险结论
    by_plan = defaultdict(list)
    for s in shortage_all:
        by_plan[s.get("project_name") or "?"].append(s)
    for pname, items in by_plan.items():
        p = match_plan(pname) if pname != "?" else None
        matched = bool(p)
        total_gap = sum(int(i.get("gap") or 0) for i in items)
        zero_gap = [i for i in items if (i.get("confirmed") or 0) == 0]
        # 在途匹配键：生产产品名（在途索引键）
        tr = intransit.get((p.get("生产产品") or "").strip(), []) if p else []
        tr_eta = max((t["eta"] for t in tr if t["eta"]), default=None)
        # 结论：缺口>0 且 无在途 → 必须立刻下单
        if total_gap > 0 and not tr:
            verdict = "必须立刻下单"
        elif total_gap > 0 and tr_eta:
            # 在途 ETA 晚于该计划合同交期 → 追加采购/催货
            due = _date(p.get("合同交期")) if p else None
            if due and tr_eta > due.isoformat():
                verdict = "在途来不及"
            else:
                verdict = "在途可覆盖"
        else:
            verdict = "部分覆盖"
        entry = {
            "product": pname[:20], "plan": (p or {}).get("生产计划编号", ""),
            "matched": matched,
            "due": ((p or {}).get("合同交期") or "")[:10],
            "gap_items": len(items), "total_gap": total_gap,
            "zero_stock_items": len(zero_gap),
            "top_gaps": [{
                "name": i.get("name", ""), "ipn": i.get("ipn", ""),
                "need": i.get("need"), "confirmed": i.get("confirmed"),
                "gap": i.get("gap"), "risk": ",".join(i.get("risk") or []),
            } for i in items[:6]],
            "intransit_n": len(tr), "intransit_eta": tr_eta,
            "verdict": verdict,
        }
        result["plans"].append(entry)
        if verdict in ("必须立刻下单", "在途来不及"):
            result["must_order"].append(entry)

    result["plans"].sort(key=lambda e: (0 if not e["matched"] else 1,
                                        0 if e["verdict"] == "必须立刻下单" else 1))
    return result


# ────────────────────────── 主流程 ──────────────────────────
def compute(today=None):
    today = today or date.today()
    sup = supplier_profile()
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "generated_date": today.isoformat(),
        "backward": backward_schedule(today),
        "supplier": sup,
        "shortage": shortage_forecast(today, sup),
    }


def print_summary(model):
    b, s, sh = model["backward"], model["supplier"], model["shortage"]
    print("=" * 64)
    print("风险雷达 foresee · 生成于 %s" % model["generated_at"])
    print("=" * 64)
    print("\n【1】合同倒排（历史样本 n=%s，中位 %s / p75 %s / p90 %s 天）"
          % (b["hist_n"], b["hist_median"], b["hist_p75"], b["hist_p90"]))
    for r in b["plans"]:
        if r["verdict"] == "缺数据":
            print("  %-14s %-16s %s" % (r["plan"], r["product"], r["note"]))
            continue
        flag = {"已逾期": "🔴", "高风险": "🟠", "偏紧": "🟡", "正常": "🟢"}.get(r["verdict"], "·")
        print("  %s %-14s %-16s 交期 %s（剩 %3d 天）%-4s %s"
              % (flag, r["plan"], r["product"], r["due"], r["days_left"], r["verdict"], r["note"]))
    if b["act_now"]:
        print("  ── 必须立刻执行 ──")
        for a in b["act_now"]:
            print("  ▶ %-14s %s：%s" % (a["plan"], a["product"], "、".join(a["do_now"])))
    print("\n【2】供应商交期画像（样本 %d 条，偏差=实际-承诺）" % s["samples"])
    for cat in s["cat_order"]:
        c = s["cat_profile"][cat]
        print("  %-6s n=%-3d 平均 %+.1f 天 最差 %+.0f 天 → 建议 buffer +%d 天"
              % (cat, c["n"], c["mean"], c["max"], c["buffer"]))
        for x in (s["sup_detail"].get(cat) or [])[:3]:
            if x["mean"] > 5:
                print("         ⚠ %-14s n=%d 平均 %+.1f 最差 %+.0f"
                      % (x["supplier"][:14], x["n"], x["mean"], x["max"]))
    print("\n【3】缺料预警（快照 %s）" % (sh["snapshot_at"] or "无"))
    if not sh["plans"]:
        print("  活动计划无缺口（或快照缺失）")
    for e in sh["plans"]:
        tag = "" if e.get("matched") else "（未关联生产计划）"
        print("  %-20s%s 缺口 %d 项/共 %d 件 · 在途 %d 条（ETA %s）→ %s"
              % (e["product"], tag, e["gap_items"], e["total_gap"],
                 e["intransit_n"], e["intransit_eta"] or "—", e["verdict"]))
    print()


def main():
    args = sys.argv[1:]
    model = compute()
    if "--json" in args:
        print(json.dumps(model, ensure_ascii=False, indent=1))
        return
    print_summary(model)
    out = os.path.join(DATA, "foresee.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=1)
    print("已写入 %s（cockpit.py 风险雷达 section 消费）" % out)


if __name__ == "__main__":
    main()
