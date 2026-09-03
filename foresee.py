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
  python foresee.py                     # 计算并写 data/foresee.json + 落预测台账，终端打印风险摘要
  python foresee.py --json              # 只输出 JSON 到 stdout（调试用）
  python foresee.py review              # 预测复盘：台账预测 vs 实际结果 → 准度报告
  python foresee.py ask <编号|产品|供应商|类别>   # 对话式追问风险细节
  python foresee.py log                 # 只更新预测台账（不重算）
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


# ────────────────── 模块 4：预测台账 + 复盘（自我学习闭环）──────────────────
LEDGER_FILE = "预测台账.csv"
LEDGER_COLS = ["记录日期", "计划编号", "产品", "预测类型", "预测值", "目标交期",
               "剩余天数", "判定", "结论", "实际交货", "误差天数", "复盘状态"]

# 何时算「预测对了」——复盘的裁判规则（plan 类型）：
#   高风险/偏紧 预警 → 实际晚于合同交期 = 预警正确（救了一命 or 至少判断对趋势）
#                      实际没晚        = 误报（宽松可接受：宁可虚惊）
#   正常        → 实际晚于交期 = 漏报（这是最伤的，重点盯）
#                      实际没晚 = 正确
VERDICT_REVIEW = {
    ("高风险", "晚了"): "预警正确", ("偏紧", "晚了"): "预警正确",
    ("高风险", "没晚"): "误报", ("偏紧", "没晚"): "误报",
    ("正常", "晚了"): "漏报", ("正常", "没晚"): "正确",
}


def update_ledger(model):
    """把本次预测追加进台账（同日同计划同类型只留最新一条）。

    台账是「预测快照的时间序列」：每天跑 foresee 自动调用，
    复盘时回看每个计划历史上被判定过什么、最后实际如何。
    """
    ledger = _read_csv(LEDGER_FILE)
    today_key = model["generated_date"]
    # 先构造本次新预测，再按 (计划编号, 预测类型) 替换当日旧条目
    # （同日重跑 = 覆盖当日快照，预测历史按天去重；复盘价值在跨天对照）
    new_rows = []
    for r in model["backward"]["plans"]:
        if r["verdict"] == "缺数据":
            continue
        new_rows.append({
            "记录日期": today_key, "计划编号": r["plan"], "产品": r["product"],
            "预测类型": "plan", "预测值": r.get("est_cycle") or "",
            "目标交期": r.get("due") or "", "剩余天数": r.get("days_left"),
            "判定": r["verdict"], "结论": r.get("note", ""),
            "实际交货": "", "误差天数": "", "复盘状态": "待复盘",
        })
    for e in model["shortage"].get("must_order") or []:
        new_rows.append({
            "记录日期": today_key, "计划编号": e.get("plan") or "", "产品": e["product"],
            "预测类型": "shortage", "预测值": "缺口%d项/%d件" % (e["gap_items"], e["total_gap"]),
            "目标交期": e.get("due") or "", "剩余天数": "",
            "判定": e["verdict"], "结论": "零库存 %d 项" % e.get("zero_stock_items", 0),
            "实际交货": "", "误差天数": "", "复盘状态": "待复盘",
        })
    today_keys = {(r["记录日期"], r["计划编号"], r["预测类型"]) for r in new_rows}
    keep = [r for r in ledger
            if (r.get("记录日期"), r.get("计划编号"), r.get("预测类型")) not in today_keys]
    out = keep + new_rows
    with open(os.path.join(DATA, LEDGER_FILE), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
        w.writeheader()
        w.writerows(out)
    return len(new_rows)


def review_predictions(today=None):
    """复盘：台账里的预测 vs 生产计划表的实际结果 → 准度报告。

    规则（VERDICT_REVIEW）：预警（高风险/偏紧）实际晚了=预警正确，没晚=误报（可容忍）；
    判「正常」实际却晚了=漏报（最伤，重点盯）。误差 = 实际交货日 - 目标交期。
    """
    today = today or date.today()
    ledger = _read_csv(LEDGER_FILE)
    if not ledger:
        return {"total": 0, "message": "台账为空，先跑 python foresee.py 积累预测"}
    plans = {p.get("生产计划编号", ""): p for p in _read_csv("生产计划.csv")}

    stats = defaultdict(int)          # 复盘结论 → 条数
    by_verdict = defaultdict(lambda: defaultdict(int))  # 判定 → 结论 → 条数
    reviewed = 0
    for r in ledger:
        if r.get("预测类型") != "plan" or r.get("复盘状态") == "已复盘":
            continue
        pid = r.get("计划编号", "")
        p = plans.get(pid)
        # 可复盘条件：计划已交付（有实际交货时间），或目标交期已过且状态停/取消
        actual = _date((p or {}).get("交货时间（自动记录）"))
        due = _date(r.get("目标交期"))
        if not actual:
            # 计划未交付：交期已过 30 天仍未交付 → 按「晚了≥30天」记漏报/预警正确
            if due and (today - due).days > 30 and (p or {}).get("状态") not in ("计划中", ""):
                late_days = (today - due).days
                outcome = "晚了"
            elif due and (today - due).days > 0:
                # 交期刚过还没交付：直接记「晚了（仍在制）」
                late_days = (today - due).days
                outcome = "晚了"
            else:
                continue  # 还没到期，无法复盘
        else:
            late_days = (actual - due).days if due else None
            outcome = "晚了" if (due and actual > due) else "没晚"
        key = (r.get("判定"), outcome)
        conclusion = VERDICT_REVIEW.get(key, "已逾期（判定时已过交期）" if r.get("判定") == "已逾期" else "未定义")
        if r.get("判定") == "已逾期":
            conclusion = "已逾期（判定时已过交期）"
        stats[conclusion] += 1
        by_verdict[r.get("判定")][conclusion] += 1
        r["实际交货"] = actual.isoformat() if actual else "（未交付）"
        r["误差天数"] = late_days if late_days is not None else ""
        r["复盘状态"] = "已复盘"
        r["结论"] = ("%s | %s" % (r.get("结论", ""), conclusion)).strip(" |")
        reviewed += 1

    if reviewed:
        with open(os.path.join(DATA, LEDGER_FILE), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
            w.writeheader()
            w.writerows(ledger)

    warned = stats["预警正确"] + stats["误报"]
    precision = round(stats["预警正确"] / warned * 100, 1) if warned else None
    total_judged = sum(stats.values())
    return {
        "total": len(ledger), "reviewed": reviewed,
        "stats": dict(stats), "by_verdict": {k: dict(v) for k, v in by_verdict.items()},
        "precision": precision,  # 预警准确率：说「会晚」的真晚了的占比
        "missed": stats["漏报"],  # 漏报数：判「正常」却晚了的——最伤
    }


def print_review(rep):
    print("=" * 64)
    print("预测复盘 review · 台账 %d 条，本次复盘 %d 条" % (rep["total"], rep["reviewed"]))
    print("=" * 64)
    if rep.get("message"):
        print(rep["message"])
        return
    print("  复盘结论分布：")
    for k, v in sorted(rep["stats"].items(), key=lambda x: -x[1]):
        print("    %-22s %d 条" % (k, v))
    if rep.get("precision") is not None:
        print("  预警准确率（预警且真晚/全部预警）：%.1f%%" % rep["precision"])
    print("  漏报（判「正常」却晚了，最伤）：", rep.get("missed", 0), "条")
    for verdict, dist in rep.get("by_verdict", {}).items():
        print("    判[%s] → %s" % (verdict, dist))
    print()


# ────────────────── 模块 5：对话式追问 ask ──────────────────
def ask(query):
    """自然语言追问：查风险雷达任意细节。只读，直接打印答案。

    支持：计划编号/产品名模糊匹配（风险判定、环节最晚开始日、缺口、在途），
    供应商名（交期画像），类别（组装料/IC/PCBA/成品/外壳），或无关键词看总览。
    """
    q = (query or "").strip()
    if not q:
        print("用法：python foresee.py ask <计划编号|产品名|供应商|类别>")
        return
    try:
        with open(os.path.join(DATA, "foresee.json"), encoding="utf-8") as f:
            model = json.load(f)
    except FileNotFoundError:
        print("无 foresee.json，先跑 python foresee.py")
        return
    today = date.today()
    gen = model.get("generated_at", "?")
    b, s, sh = model.get("backward", {}), model.get("supplier", {}), model.get("shortage", {})

    # 1) 匹配倒排计划（编号精确 / 产品名包含）
    hits = [r for r in b.get("plans", []) if r.get("plan") == q
            or q in (r.get("product") or "")]
    if hits:
        for r in hits:
            print("━━ %s %s（快照 %s）" % (r["plan"], r.get("product", ""), gen))
            if r.get("verdict") == "缺数据":
                print("   %s" % r["note"])
                continue
            print("   目标交期 %s（%s）· 剩余 %d 天 · 判定 %s"
                  % (r["due"], r.get("basis"), r["days_left"], r["verdict"]))
            print("   %s" % r.get("note", ""))
            print("   环节最晚开始日（快照日视角）：")
            for st in r.get("stages", []):
                mark = "🔴已过" if st["urgent"] else "    "
                print("     %s %s ← 最晚 %s（提前%d天）"
                      % (mark, st["stage"], st["latest"], st["lead"]))
            # 同产品缺料与在途
            for e in sh.get("plans", []):
                if e.get("plan") == r["plan"] or q in e.get("product", ""):
                    print("   缺料：%d 项/共 %d 件，在途 %d 条（ETA %s）→ %s"
                          % (e["gap_items"], e["total_gap"], e["intransit_n"],
                             e["intransit_eta"] or "—", e["verdict"]))
                    for g in e.get("top_gaps", [])[:5]:
                        print("     · %s（%s）确认 %s/需 %s，缺 %s %s"
                              % (g["name"], g["ipn"], g["confirmed"], g["need"],
                                 g["gap"], ("[" + g["risk"] + "]") if g.get("risk") else ""))
        return

    # 2) 匹配供应商（名字包含）
    sup_hits = []
    for cat, lst in (s.get("sup_detail") or {}).items():
        for x in lst:
            if q in x["supplier"]:
                sup_hits.append((cat, x))
    if sup_hits:
        for cat, x in sup_hits:
            print("━━ 供应商 %s（类别 %s，快照 %s）" % (x["supplier"], cat, gen))
            print("   样本 %d 单 · 平均偏差 %+.1f 天 · 最差 %+.0f 天 → 建议 buffer +%d 天"
                  % (x["n"], x["mean"], x["max"], x["buffer"]))
            cp = (s.get("cat_profile") or {}).get(cat, {})
            print("   （%s 类整体：n=%s 平均 %s 天 buffer +%s 天）"
                  % (cat, cp.get("n"), cp.get("mean"), cp.get("buffer")))
        return

    # 3) 匹配类别
    if q in (s.get("cat_profile") or {}):
        cp = s["cat_profile"][q]
        print("━━ 类别 %s（快照 %s）" % (q, gen))
        print("   样本 %d 单 · 平均偏差 %s 天 · 最差 %s 天 → 建议 buffer +%d 天"
              % (cp["n"], cp["mean"], cp["max"], cp["buffer"]))
        for x in (s.get("sup_detail", {}).get(q) or [])[:8]:
            print("   · %-16s n=%-2d 平均 %+.1f 最差 %+.0f"
                  % (x["supplier"][:16], x["n"], x["mean"], x["max"]))
        return

    # 4) 匹配缺料产品
    for e in sh.get("plans", []):
        if q in e.get("product", ""):
            print("━━ 缺料 %s（快照 %s）" % (e["product"], gen))
            print("   %s · 交期 %s · 在途 %d 条（ETA %s）"
                  % (e["verdict"], e.get("due") or "—", e["intransit_n"], e["intransit_eta"] or "—"))
            for g in e.get("top_gaps", [])[:8]:
                print("   · %s（%s）确认 %s/需 %s，缺 %s %s"
                      % (g["name"], g["ipn"], g["confirmed"], g["need"],
                         g["gap"], ("[" + g["risk"] + "]") if g.get("risk") else ""))
            return

    # 5) 都没匹配上 → 总览 + 提示
    print("未匹配到「%s」。当前快照（%s）总览：" % (q, gen))
    flags = defaultdict(list)
    for r in b.get("plans", []):
        if r.get("verdict") and r["verdict"] != "缺数据":
            flags[r["verdict"]].append(r["plan"])
    for v in ("已逾期", "高风险", "偏紧", "正常"):
        if flags.get(v):
            print("  %s %d 个：%s" % (v, len(flags[v]), "、".join(flags[v][:6])))
    if sh.get("must_order"):
        print("  必须立刻下单：%s" % "、".join(e["product"] for e in sh["must_order"][:5]))
    print("提示：ask 支持计划编号（20260831-001）、产品名、供应商名、类别（组装料/IC/PCBA/成品）")


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
    # 子命令：review（预测复盘）/ ask <query>（对话追问）
    if args and args[0] == "review":
        rep = review_predictions()
        print_review(rep)
        return
    if args and args[0] == "ask":
        ask(" ".join(args[1:]))
        return
    if args and args[0] == "log":
        n = update_ledger(compute())
        print("预测台账已更新：%d 条新预测" % n)
        return
    model = compute()
    # 每次计算顺手落台账（自我学习闭环：预测快照 → 到期复盘 → 准度报告）
    update_ledger(model)
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
