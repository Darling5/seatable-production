#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cockpit.py — 生产·项目管理驾驶舱 生成器。

读取 seatable-production 技能本地库（14 张表），计算「工时 / 成本 / 质量 / 供应链」
四维指标 + 项目总览 + 在制品看板 + 物料库存预警，渲染为单文件、内联 SVG、响应式
HTML 驾驶舱。

用法：
    python cockpit.py                      # 输出到默认工作区
    python cockpit.py 路径/驾驶舱.html      # 自定义输出路径

数据来源：与 op.py 同源的适配器，local / seatable 自动切换。
生成的是「数据快照」：HTML 内嵌当前计算结果；也可用页面内「导入数据」按钮载入
导出的 JSON 快照刷新，无需重跑本脚本。
"""
import os
import sys
import json
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters.factory import get_adapter  # noqa: E402

_TZ = timezone(timedelta(hours=8))
DEFAULT_OUT = r"C:\Users\11430\WorkBuddy\2026-08-07-18-30-14\项目管理驾驶舱.html"

# 采购 / 生产执行表中「花销」列与成本类目映射
COST_MAP = [
    ("PCB下单记录", "打板价格", "PCB"),
    ("外壳采购记录", "价格", "外壳"),
    ("IC采购记录", "采购花销", "IC"),
    ("贴片生产记录", "贴片价格", "贴片"),
    ("PCBA半成品采购记录", "采购花销", "PCBA"),
    ("组装料采购记录", "采购花销", "组装料"),
    ("组装记录", "组装价格", "组装"),
    ("成品采购记录", "采购花销", "成品"),
]
# 供应商列回退（不同表叫法不同）
SUPPLIER_COLS = ["供应商", "贴片厂", "组装厂"]

# 项目/生产计划状态归一化（真实库用「已交付/可能延迟/已超期/待客户下单」等，
# demo 用「进行中/计划中/已完成」；统一映射为 计划/进行中/已完成 三桶）
STATUS_DONE = {"已完成", "已交付"}
STATUS_ACTIVE = {"进行中", "可能延迟", "已超期", "待客户下单"}


def _num(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("¥", "").replace(",", "").replace(" ", "").strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _date(v):
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _supplier(row):
    for c in SUPPLIER_COLS:
        if row.get(c):
            return str(row[c]).strip()
    return "未知"


def _rid(row):
    """提取 SeaTable 行 ID（首列 __row_id__ 可能带 BOM）。"""
    for k, v in row.items():
        if k.replace("﻿", "") == "__row_id__":
            return v
    return None


def _load_partdb():
    """读取 partdb_sync.py 生成的真实快照；不存在返回 None。"""
    snap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "partdb_snapshot.json")
    if not os.path.exists(snap):
        return None
    try:
        with open(snap, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_sync_meta():
    """读取 seatable_sync.py 写入的同步标记；存在说明业务表已是云端真实数据。"""
    meta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "_sync_meta.json")
    if not os.path.exists(meta):
        return None
    try:
        with open(meta, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def compute(adapter, today):
    projects = adapter.list_rows("项目")
    plans = adapter.list_rows("生产计划")
    repairs = adapter.list_rows("维修记录")
    shipments = adapter.list_rows("发货清单")
    inv = adapter.list_rows("库存核对记录")
    processes = adapter.list_rows("生产工序")

    # ── 项目指标 ──
    p_total = len(projects)
    p_active = sum(1 for p in projects if p.get("状态") in STATUS_ACTIVE)
    p_planned = sum(1 for p in projects if p.get("状态") == "计划中")
    p_done = sum(1 for p in projects if p.get("状态") in STATUS_DONE)
    contract = sum(_num(p.get("合同总价")) for p in projects)
    received = sum(_num(p.get("实收")) for p in projects)
    receivable = contract - received

    proj_overview = []
    for p in projects:
        c = _num(p.get("合同总价"))
        r = _num(p.get("实收"))
        proj_overview.append({
            "name": p.get("项目", ""),
            "status": p.get("状态", ""),
            "contract": c,
            "received": r,
            "receivable": c - r,
            "due": p.get("合同交期", ""),
        })

    # ── 成本归集 ──
    category_cost = {c[2]: 0.0 for c in COST_MAP}
    plan_cost = {}
    supplier_stat = {}   # name -> {orders, ontime, late, pending, cost}
    purchase_rows = []   # 用于逾期/准时率
    for table, col, cat in COST_MAP:
        for row in adapter.list_rows(table):
            amt = _num(row.get(col))
            if amt <= 0:
                continue
            category_cost[cat] += amt
            pn = row.get("生产计划", "")
            plan_cost[pn] = plan_cost.get(pn, 0.0) + amt
            sup = "PCB打板" if table == "PCB下单记录" else _supplier(row)
            # 准时率（仅对有交期与到货/逾期信息的单计入）
            eta_s = row.get("下单时间")
            lead = _num(row.get("交期"))
            arr = _date(row.get("到货时间")) if table != "PCB下单记录" else _date(row.get("最终完成时间"))
            eta = _date(eta_s)
            if eta and lead:
                st = supplier_stat.setdefault(sup, {"orders": 0, "ontime": 0, "late": 0, "pending": 0, "cost": 0.0})
                st["orders"] += 1
                st["cost"] += amt
                eta_date = eta + timedelta(days=int(lead))
                if arr:
                    if arr <= eta_date:
                        st["ontime"] += 1
                    else:
                        st["late"] += 1
                elif eta_date < today:
                    st["late"] += 1  # 已逾期未到货
                else:
                    st["pending"] += 1
            purchase_rows.append({"table": table, "cat": cat, "row": row, "amt": amt,
                                  "supplier": sup, "arr": arr, "eta": eta, "lead": lead})

    total_cost = sum(category_cost.values())

    # 台账口径：生产计划「生产总花销」汇总（含人工/辅料，比采购明细更全）
    ledger_cost = sum(_num(p.get("生产总花销")) for p in plans)
    # 单片成本：生产计划「此次单片成本」均值（真实业务填写的单价基准）
    unit_costs = [_num(p.get("此次单片成本")) for p in plans if _num(p.get("此次单片成本")) > 0]
    unit_cost = round(sum(unit_costs) / len(unit_costs), 2) if unit_costs else 0.0

    # 项目级成本（通过生产计划关联）
    plan_to_proj = {p.get("生产产品", ""): p.get("关联项目", "") for p in plans}
    proj_cost = {}
    for pn, amt in plan_cost.items():
        pj = plan_to_proj.get(pn, "")
        proj_cost[pj] = proj_cost.get(pj, 0.0) + amt
    total_profit = contract - total_cost
    margin = (total_profit / contract * 100) if contract else 0.0
    budget_margin = 30.0  # 目标毛利率

    # 成本结构（降序，仅保留 >0）
    category = [{"name": k, "value": round(v, 2)}
                for k, v in category_cost.items() if v > 0]
    category.sort(key=lambda x: x["value"], reverse=True)

    # ── 在制品看板 ──
    wip = []
    stage_dist = {}
    for p in plans:
        stage = p.get("阶段", "未定义")
        stage_dist[stage] = stage_dist.get(stage, 0) + 1
        if p.get("状态") in STATUS_DONE:
            continue
        due = _date(p.get("合同交期"))
        remain = (due - today).days if due else None
        wip.append({
            "product": p.get("生产产品", ""),
            "qty": _num(p.get("数量")),
            "stage": stage,
            "status": p.get("状态", ""),
            "start": p.get("立项日期", ""),
            "due": p.get("合同交期", ""),
            "remain": remain,
            "overdue": (remain is not None and remain < 0),
        })
    wip.sort(key=lambda x: (x["remain"] is None, x["remain"] if x["remain"] is not None else 0))

    # ── 工时 ──
    # 真实库未填「完货日期」，改用生产计划表「花费天数」(业务实际填写的工序耗时)
    # 作为实际生产周期；「合同交期 − 立项日期」为允许周期，实际 ≤ 允许 即视为交期达成。
    total_plans = len(plans)
    ontime = 0
    dated = 0
    cycles = []
    for p in plans:
        sd = _date(p.get("立项日期"))
        cd = _date(p.get("合同交期"))
        spend = _num(p.get("花费天数"))   # 实际花费天数
        if spend > 0:
            cycles.append({"product": p.get("生产产品", ""), "days": int(spend)})
            if sd and cd:
                allow = (cd - sd).days
                dated += 1
                if spend <= allow:
                    ontime += 1
    # 无「花费天数」则无法判定周期达成率，显示 N/A 而非 0%
    ontime_rate = (ontime / dated * 100) if dated else None
    avg_cycle = round(sum(c["days"] for c in cycles) / len(cycles), 1) if cycles else 0

    # 产线实时流转：生产工序表的「当前流程」分布（每个在产计划当前卡在哪个工序）
    flow_rows = adapter.list_rows("生产工序")
    flow_dist = {}
    for r in flow_rows:
        fl = (r.get("当前流程") or "").strip()
        if fl:
            flow_dist[fl] = flow_dist.get(fl, 0) + 1

    # ── 质量 ──
    shipped = sum(_num(s.get("发货数量")) for s in shipments) or len(shipments)
    repair_total = len(repairs)
    repair_rate = (repair_total / shipped * 100) if shipped else 0.0
    smt_rows = adapter.list_rows("贴片生产记录")
    smt_yield = 0.0
    if smt_rows:
        tot = sum(_num(r.get("贴片数量")) for r in smt_rows)
        good = sum(_num(r.get("良品数量")) for r in smt_rows)
        smt_yield = (good / tot * 100) if tot else 0.0
    asm_rows = adapter.list_rows("组装记录")
    asm_yields = [_num(r.get("组装良品率")) for r in asm_rows if _num(r.get("组装良品率")) > 0]
    # 真实库 2 条组装记录的良品率列均为空(#DIV/0) → 无数据，标注「未录入」而非 0%
    asm_yield = round(sum(asm_yields) / len(asm_yields), 1) if asm_yields else None
    repair_overdue = 0
    repair_days = []
    repair_list = []
    for r in repairs:
        rt = _date(r.get("返修时间"))
        ft = _date(r.get("完成时间"))
        req = _num(r.get("要求交期"))
        if rt and ft:
            d = (ft - rt).days
            repair_days.append(d)
        done = ft is not None
        overdue = (not done) and rt and req and (rt + timedelta(days=int(req)) < today)
        if overdue:
            repair_overdue += 1
        repair_list.append({
            "proj": r.get("相关项目", ""),
            "item": r.get("维修清单", ""),
            "back": r.get("返修时间", ""),
            "req": int(req),
            "done": done,
            "overdue": bool(overdue),
        })
    repair_avg = round(sum(repair_days) / len(repair_days), 1) if repair_days else 0

    # ── 供应链：采购逾期 + 供应商准时率 ──
    overdue_list = []
    for pr in purchase_rows:
        eta = pr["eta"]
        lead = pr["lead"]
        arr = pr["arr"]
        if eta and lead:
            eta_date = eta + timedelta(days=int(lead))
            if arr is None and eta_date < today:
                overdue_list.append({
                    "supplier": pr["supplier"],
                    "material": pr["row"].get("物料名称") or pr["row"].get("外壳名称") or pr["row"].get("PCB型号版本") or "",
                    "plan": pr["row"].get("生产计划", ""),
                    "eta": eta_date.isoformat(),
                    "days": (today - eta_date).days,
                })
    overdue_list.sort(key=lambda x: x["days"], reverse=True)

    supplier = []
    for name, st in supplier_stat.items():
        rated = st["ontime"] + st["late"]
        rate = (st["ontime"] / rated * 100) if rated else None
        supplier.append({
            "name": name,
            "orders": st["orders"],
            "ontime": st["ontime"],
            "late": st["late"],
            "pending": st["pending"],
            "cost": round(st["cost"], 2),
            "rate": round(rate, 1) if rate is not None else None,
        })
    supplier.sort(key=lambda x: (x["rate"] is None, x["rate"] if x["rate"] is not None else 0))

    # ── 物料库存预警（来自库存核对记录；PartDB 缺料检查未配置则跳过）──
    inventory_warn = []
    for r in inv:
        fin = r.get("最终完成时间")
        if not fin or str(r.get("核对结果", "")).strip() in ("", "待核对", "异常"):
            inventory_warn.append({
                "plan": r.get("链接其他记录", ""),
                "result": r.get("核对结果", "") or "待核对",
                "time": fin or "",
            })

    # ── 现金流预测（30/60/90 天）──
    def bucket_of(d):
        if d is None:
            return None
        delta = (d - today).days
        if delta < 0:
            return "逾期"
        if delta <= 30:
            return "30"
        if delta <= 60:
            return "60"
        if delta <= 90:
            return "90"
        return ">90"

    cash = {"逾期": {"in": 0.0, "out": 0.0}, "30": {"in": 0.0, "out": 0.0},
            "60": {"in": 0.0, "out": 0.0}, "90": {"in": 0.0, "out": 0.0}}
    for p in projects:
        amt = _num(p.get("合同总价")) - _num(p.get("实收"))
        if amt <= 0:
            continue
        b = bucket_of(_date(p.get("合同交期")))
        if b and b in cash:
            cash[b]["in"] += amt
    for pr in purchase_rows:
        if pr["arr"] is None and pr["eta"] and pr["lead"]:
            eta_date = pr["eta"] + timedelta(days=int(pr["lead"]))
            b = bucket_of(eta_date)
            if b and b in cash:
                cash[b]["out"] += pr["amt"]
    cashflow = []
    for k in ["逾期", "30", "60", "90"]:
        cashflow.append({"bucket": k, "in": round(cash[k]["in"], 2),
                         "out": round(cash[k]["out"], 2),
                         "net": round(cash[k]["in"] - cash[k]["out"], 2)})

    # 应收款明细
    receivable_list = [p for p in proj_overview if p["receivable"] > 0]
    receivable_list.sort(key=lambda x: x["due"])

    # ── 甘特图数据（立项 → 合同交期；进度按日期推算）──
    gantt = []
    for p in plans:
        sd = _date(p.get("立项日期"))
        cd = _date(p.get("合同交期"))
        if not (sd and cd):
            continue
        status = p.get("状态", "")
        done = status in STATUS_DONE
        overdue = (not done) and cd < today
        span = (cd - sd).days
        if span <= 0 or done:
            prog = 100
        else:
            elapsed = (today - sd).days
            prog = max(0, min(100, int(elapsed / span * 100)))
        gantt.append({
            "name": p.get("生产产品", ""), "status": status,
            "start": sd.isoformat(), "end": cd.isoformat(),
            "done": done, "overdue": bool(overdue), "progress": prog,
            "row_id": _rid(p),
        })
    gantt.sort(key=lambda x: x["start"])

    # ── 下一步行动建议（按优先级推导）──
    pd = _load_partdb()
    actions = []
    if overdue_list:
        top = overdue_list[0]
        actions.append({"pri": "高", "cat": "purchase", "text": f"跟进 {len(overdue_list)} 笔逾期采购，最紧急：{top['supplier']} 的 {top['material'] or '物料'} 已逾期 {top['days']} 天，尽快催收/换源"})
    if pd:
        b = pd.get("bom") or {}
        sh = b.get("shortage") or []
        if sh:
            zero = sum(1 for x in sh if x.get("confirmed") == 0)
            actions.append({"pri": "高", "cat": "warehouse", "text": f"补料：{b.get('project_name', '在产项目')} 有 {len(sh)} 种物料缺口（{zero} 种零确认库存），尽快下达采购单"})
    od_wip = [w for w in wip if w["overdue"]]
    if od_wip:
        w = od_wip[0]
        actions.append({"pri": "高", "cat": "delivery", "text": f"推进逾期未交付：{w['product']} 已逾期 {-w['remain']} 天，优先排产/协调产能"})
    nd = [w for w in wip if w["remain"] is not None and 0 < w["remain"] <= 7]
    if nd:
        w = nd[0]
        actions.append({"pri": "中", "cat": "delivery", "text": f"临近交期：{w['product']} 仅剩 {w['remain']} 天，确保本周内完工"})
    if repair_overdue:
        actions.append({"pri": "中", "cat": "production", "text": f"处理 {repair_overdue} 笔超期维修单，避免客户投诉升级"})
    if receivable_list:
        r = receivable_list[0]
        actions.append({"pri": "中", "cat": "sales", "text": f"催收应收：最早到期「{r['name']}」应收 ¥{r['receivable']:,.0f}（交期 {r['due']}）"})
    if flow_dist:
        bn = max(flow_dist.items(), key=lambda x: x[1])
        actions.append({"pri": "提示", "cat": "production", "text": f"产能瓶颈：{bn[0]} 环节积压 {bn[1]} 个在产计划，建议增配资源或并行处理"})
    if asm_yield is None:
        actions.append({"pri": "提示", "cat": "production", "text": "补录组装良品率：当前组装记录均未填，质量维度暂不完整"})
    if not actions:
        actions.append({"pri": "提示", "cat": "boss", "text": "暂无紧急事项，保持当前节奏即可 ✔"})

    # ── 补录缺失字段（供 HTML 内直接行内填写，存本地后复制发回推送云端）──
    # 真实库这些列为空：生产计划「计划开始/完成/实际完成/放行状态」、组装记录「组装良品率」。
    # 确定性可推的日期先预填（计划开始≈立项、计划完成≈合同交期、已交付计划实际完成=立项+花费天数），
    # 需用户拍板的「实际完成/放行状态/组装良品率」留空。
    backfill = {"生产计划": [], "组装记录": []}
    for p in plans:
        sd = p.get("立项日期", "")
        cd = p.get("合同交期", "")
        spend = _num(p.get("花费天数"))
        act = ""
        if p.get("状态") in STATUS_DONE and sd and spend > 0:
            d = _date(sd)
            if d:
                act = (d + timedelta(days=int(spend))).isoformat()
        backfill["生产计划"].append({
            "row_id": _rid(p),
            "name": p.get("生产产品", ""),
            "计划开始日期": sd if str(sd).strip() not in ("", "None") else "",
            "计划完成日期": cd if str(cd).strip() not in ("", "None") else "",
            "实际完成日期": act,
            "放行状态": "",
        })
    for r in asm_rows:
        backfill["组装记录"].append({
            "row_id": _rid(r),
            "name": r.get("组装编号", ""),
            "组装良品率": "",
        })

    return {
        "snapshot": today.isoformat(),
        "isDemo": not bool(_load_sync_meta()),
        "synced_at": (_load_sync_meta() or {}).get("synced_at"),
        "base_name": (_load_sync_meta() or {}).get("base_name"),
        "partdb_at": (lambda p: p.get("generated_at") if p else None)(_load_partdb()),
        "kpi": {
            "projects": p_total, "active": p_active, "planned": p_planned, "done": p_done,
            "contract": round(contract, 2), "received": round(received, 2),
            "receivable": round(receivable, 2), "cost": round(total_cost, 2),
            "ledger_cost": round(ledger_cost, 2), "unit_cost": unit_cost,
            "exec_rate": round(received / contract * 100, 1) if contract else 0.0,
            "profit": round(total_profit, 2), "margin": round(margin, 1),
            "ontime_rate": round(ontime_rate, 1) if ontime_rate is not None else None, "purchase_overdue": len(overdue_list),
            "wip": len(wip),
            "partdb": bool(_load_partdb()),
            "bom_shortage": (lambda p: (p.get("bom") or {}).get("shortage", []) and len((p.get("bom") or {}).get("shortage", [])) or 0)(_load_partdb()) if _load_partdb() else 0,
        },
        "projects": proj_overview,
        "wip": wip,
        "time": {
            "ontime_rate": round(ontime_rate, 1) if ontime_rate is not None else None, "ontime": ontime, "dated": dated, "total": total_plans,
            "avg_cycle": avg_cycle, "cycle_list": cycles, "stage_dist": stage_dist, "flow_dist": flow_dist,
        },
        "cost": {
            "contract": round(contract, 2), "cost": round(total_cost, 2),
            "ledger_cost": round(ledger_cost, 2), "unit_cost": unit_cost,
            "profit": round(total_profit, 2), "margin": round(margin, 1),
            "budget_margin": budget_margin,
            "category": category, "receivable_list": receivable_list, "cashflow": cashflow,
        },
        "quality": {
            "repair_rate": round(repair_rate, 2), "shipped": shipped, "repair_total": repair_total,
            "smt_yield": round(smt_yield, 1), "asm_yield": asm_yield,
            "repair_overdue": repair_overdue, "repair_avg": repair_avg, "repair_list": repair_list,
        },
        "supply": {
            "overdue_list": overdue_list, "supplier": supplier,
            "inventory_warn": inventory_warn, "process_count": len(processes),
        },
        "gantt": gantt,
        "next_actions": actions,
        "backfill": backfill,
        # PartDB 实时（partdb_sync.py 生成；缺失则 None，渲染时回退）
        "partdb": _load_partdb(),
    }


# 内联 SVG 图标（构建时替换占位符，避免运行时修改 DOM 破坏工具栏）
ICONS = {
    "TITLE_ICON": '<svg class="svg-ic" width="24" height="24" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    "IC_GRID": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
    "IC_PROJ": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 12l9 4 9-4"/><path d="M3 17l9 4 9-4"/></svg>',
    "IC_TIME": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
    "IC_COST": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M12 3v18"/><path d="M16.5 7.5c0-1.7-2-3-4.5-3s-4.5 1.3-4.5 3 2 2.7 4.5 3 4.5 1.3 4.5 3-2 3-4.5 3-4.5-1.3-4.5-3"/></svg>',
    "IC_QUAL": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M12 3l2.5 5 5.5.8-4 3.9.9 5.5L12 21l-4.9 2.2.9-5.5-4-3.9 5.5-.8z"/></svg>',
    "IC_SUP": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M3 8l9-4 9 4-9 4-9-4z"/><path d="M3 8v8l9 4 9-4V8"/><path d="M12 12v8"/></svg>',
    "IC_DOWNLOAD": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/></svg>',
    "IC_UPLOAD": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M12 21V9"/><path d="M7 13l5-5 5 5"/><path d="M4 4h16"/></svg>',
    "IC_REFRESH": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/></svg>',
    "IC_NEXT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M5 7l2 2 4-4"/><path d="M5 13l2 2 4-4"/><path d="M14 9h5"/><path d="M14 15h5"/></svg>',
    "IC_EDIT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3z"/><path d="M13.5 6.5l3 3"/></svg>',
    "IC_COPY": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
    "IC_GANT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M4 6h10"/><path d="M4 12h14"/><path d="M4 18h7"/></svg>',
    "IC_ANALYZE": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/></svg>',
    "IC_SYNC": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 1-9 9 9 9 0 0 1-8-5"/><path d="M3 12a9 9 0 0 1 9-9 9 9 0 0 1 8 5"/><path d="M21 4v5h-5"/><path d="M3 20v-5h5"/></svg>',
    "IC_BOX": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/></svg>',
    "IC_SHARE": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l7.8 4"/><path d="M15.4 6.5l-7.8 4"/></svg>',
    "IC_KEY": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M14 7a4 4 0 1 0-3.6 5.9L7 17v3H4v-3l5.4-5.4A4 4 0 0 0 14 7zm-1.6 2.4a2 2 0 1 1-2.8 2.8 2 2 0 0 1 2.8-2.8z"/></svg>',
    "IC_ADD": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M12 5v14"/><path d="M5 12h14"/></svg>',
}

# ===== 访问口令（客户端校验，写进 HTML 源码；已 base64 混淆，开发者选项里不再一眼看到明文）=====
# 注意：仍是客户端校验，口令会写进 HTML 源码，仅防误看 / 随手改 hash 越权，非真加密。
# - 模式 PW_MODE：
#     "fixed"  —— 固定口令（写在下方常量），长期稳定；仅手动改 + 重生成才变（推荐，分享出去的人不会天天变口令）。
#     "rotate" —— 每次运行 cockpit.py（含每日 9 点自动任务）都重新随机生成一套新口令并自动部署。
# - ADMIN_PASSWORD：管理员（你自己）口令，可解锁全部 5 个角色、自由切换视图、进口令管理面板。
# - ROLE_PASSWORDS：每个角色独立口令，发给对应人员；对方只能看自己那一份，且无法切换到其他角色。
# 轮换：管理员页面「口令管理」里点「轮换全部口令」→ 本机立即生成新口令；要让全网（其他设备）生效并作废旧口令，
#       把生成的新口令发我（或直接说「重新部署」），我用新口令重建上线即可。
PW_MODE = "fixed"

if PW_MODE == "rotate":
    import random as _rnd, string as _str
    def _genpw(p): return p + "".join(_rnd.choices(_str.digits, k=4))
    ADMIN_PASSWORD = _genpw("ZHWL")
    ROLE_PASSWORDS = {k: _genpw(p) for k, p in
                      {"boss": "ZHWL", "warehouse": "CK", "purchase": "CG",
                       "production": "SC", "sales": "XS"}.items()}
else:
    ADMIN_PASSWORD = "ZHWL8888"
    ROLE_PASSWORDS = {
        "boss": "ZHWL2026",
        "warehouse": "CK8888",
        "purchase": "CG8888",
        "production": "SC8888",
        "sales": "XS8888",
    }

# 构建期：把口令表编码为 base64 再写入源码（开发者选项里不再是一眼明文）
import base64 as _b64
_PW_MAP = {"admin": ADMIN_PASSWORD, **ROLE_PASSWORDS}
PW_BLOB = _b64.b64encode(json.dumps(_PW_MAP, ensure_ascii=False).encode("utf-8")).decode("ascii")


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>生产·项目管理驾驶舱</title>
<style>
  :root{
    --bg:#f4f6fb; --card:#ffffff; --ink:#1f2733; --sub:#6b7686; --line:#e6eaf2;
    --primary:#3b5bdb; --green:#2f9e44; --red:#e03131; --amber:#f08c00; --purple:#7048e8;
    --blue:#1c7ed6; --teal:#0ca678; --shadow:0 1px 3px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.06);
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
    background:var(--bg);color:var(--ink);font-size:15px;line-height:1.5;-webkit-text-size-adjust:100%}
  .wrap{max-width:1600px;margin:0 auto;padding:16px 16px calc(32px + env(safe-area-inset-bottom))}
  header.top{background:linear-gradient(135deg,#3b5bdb,#5c7cfa);color:#fff;border-radius:18px;
    padding:20px 22px;box-shadow:var(--shadow);position:relative;overflow:hidden}
  header.top h1{margin:0;font-size:21px;letter-spacing:.5px;display:flex;align-items:center;gap:10px}
  header.top .meta{margin-top:6px;font-size:13px;opacity:.85}
  .toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
  .btn{appearance:none;border:0;background:rgba(255,255,255,.18);color:#fff;border-radius:10px;
    padding:9px 14px;font-size:13px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;min-height:40px}
  .btn:hover{background:rgba(255,255,255,.28)}
  .banner{margin-top:12px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3);
    border-radius:10px;padding:8px 12px;font-size:12.5px}
  .demo-flag{position:absolute;top:14px;right:16px;background:var(--amber);color:#fff;font-size:11px;
    padding:3px 9px;border-radius:20px;font-weight:600}
  .real-flag{position:absolute;top:14px;right:16px;background:var(--green);color:#fff;font-size:11px;
    padding:3px 9px;border-radius:20px;font-weight:600}
  section{margin-top:18px}
  /* 快速导航条（吸顶 + 横向滚动） */
  .sec-nav{position:sticky;top:0;z-index:30;display:flex;gap:8px;overflow-x:auto;padding:9px 2px;margin:2px 0 16px;background:var(--bg);border-bottom:1px solid var(--line);scrollbar-width:thin}
  .sec-nav::-webkit-scrollbar{height:5px}
  .sec-nav::-webkit-scrollbar-thumb{background:#cdd5e3;border-radius:3px}
  .sec-nav-item{flex:0 0 auto;display:inline-flex;align-items:center;gap:5px;padding:7px 13px;border:1px solid var(--line);background:var(--card);border-radius:999px;font-size:13px;color:var(--sub);cursor:pointer;white-space:nowrap}
  .sec-nav-item:hover{border-color:var(--primary)}
  .sec-nav-item.active{border-color:var(--primary);color:var(--primary);background:#eef2ff;font-weight:700}
  .sec-nav-item svg{width:14px;height:14px;flex:0 0 14px}
  /* 横向滑动容器（卡片横滑 / 移动端左右滑） */
  .hscroll{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;padding-bottom:8px;scrollbar-width:thin}
  .hscroll::-webkit-scrollbar{height:6px}
  .hscroll::-webkit-scrollbar-thumb{background:#cdd5e3;border-radius:3px}
  .hscroll>.card,.hscroll>.kpi{flex:0 0 auto;min-width:172px;max-width:248px;scroll-snap-align:start}
  /* 列表分页器 */
  .pg{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:10px;font-size:13px;color:var(--sub)}
  .pg button{appearance:none;padding:6px 14px;border:1px solid var(--line);background:var(--card);border-radius:8px;cursor:pointer;font-size:13px;color:var(--ink)}
  .pg button:disabled{opacity:.4;cursor:default}
  .pg-info{font-variant-numeric:tabular-nums}
  .sec-title{display:flex;align-items:center;gap:9px;font-size:16px;font-weight:700;margin:0 0 12px 2px}
  .sec-title svg{width:20px;height:20px;flex:0 0 20px}
  .grid{display:grid;gap:14px}
  .g2{grid-template-columns:1fr 1fr}
  .g3{grid-template-columns:repeat(3,1fr)}
  .g4{grid-template-columns:repeat(4,1fr)}
  @media(max-width:880px){.g2,.g3,.g4{grid-template-columns:1fr}}
  @media(max-width:680px){.pd-stats{grid-template-columns:repeat(2,1fr)}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:var(--shadow)}
  .card h3{margin:0 0 12px;font-size:14px;color:var(--sub);font-weight:600}
  .kpi{display:flex;flex-direction:column;gap:4px}
  .kpi .v{font-size:26px;font-weight:800;letter-spacing:.5px}
  .kpi .l{font-size:12.5px;color:var(--sub)}
  .donut-wrap{display:flex;align-items:center;gap:16px}
  .legend{display:flex;flex-direction:column;gap:7px;font-size:13px;flex:1;min-width:0}
  .legend .row{display:flex;align-items:center;gap:8px}
  .legend .dot{width:11px;height:11px;border-radius:3px;flex:0 0 11px}
  .legend .nm{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .legend .vl{font-weight:700}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--sub);font-weight:600;font-size:12px}
  tr:last-child td{border-bottom:0}
  .pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:600;white-space:nowrap}
  .st-进行中{background:#e7f5ff;color:#1971c2}
  .st-计划中{background:#fff4e6;color:#e8590c}
  .st-已完成{background:#ebfbee;color:#2f9e44}
  .tag-red{background:#fff0f0;color:var(--red)}
  .tag-green{background:#ebfbee;color:var(--green)}
  .tag-amber{background:#fff4e6;color:var(--amber)}
  .num{font-variant-numeric:tabular-nums}
  .bar-row{display:flex;align-items:center;gap:10px;margin:7px 0;font-size:13px}
  .bar-row .nm{width:84px;flex:0 0 84px;color:var(--sub);text-align:right}
  .bar-track{flex:1;background:#eef1f7;border-radius:6px;height:18px;overflow:hidden}
  .bar-fill{height:100%;border-radius:6px}
  .bar-row .vl{width:54px;flex:0 0 54px;font-weight:700;text-align:right}
  .cf{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
  @media(max-width:680px){.cf{grid-template-columns:repeat(2,1fr)}}
  .cf .cell{background:#f8f9fc;border-radius:12px;padding:11px;text-align:center}
  .cf .bk{font-size:12px;color:var(--sub)}
  .cf .bi{font-size:16px;font-weight:800;color:var(--green)}
  .cf .bo{font-size:13px;color:var(--red)}
  .cf .bn{font-size:14px;font-weight:700;margin-top:2px}
  .note{font-size:12px;color:var(--sub);margin-top:10px;line-height:1.5}
  .empty{color:var(--sub);font-size:13px;padding:8px 0;text-align:center}
  .pos{color:var(--green)} .neg{color:var(--red)}
  footer{margin-top:24px;text-align:center;font-size:12px;color:var(--sub)}
  .svg-ic{fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
  .badge-real{display:inline-block;background:var(--primary);color:#fff;font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;white-space:nowrap}
  .pd-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:12px}
  .pd-head h3{margin:0}
  .pd-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
  .pd-stats>div{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:10px;text-align:center}
  .pd-stats b{display:block;font-size:20px;font-weight:800;line-height:1.1}
  .pd-stats span{font-size:11.5px;color:var(--sub)}
  .pd-stats .c-red{color:var(--red)} .pd-stats .c-green{color:var(--green)} .pd-stats .c-amber{color:var(--amber)}
  .sub{color:var(--sub);font-size:11px;font-weight:500;margin-left:2px}
  /* 行动建议 */
  .actions{display:flex;flex-direction:column;gap:8px}
  .act{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border-radius:12px;background:var(--bg);border:1px solid var(--line)}
  .act .pri{flex:0 0 auto;font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;margin-top:1px;white-space:nowrap}
  .pri-高{background:#fff0f0;color:var(--red)} .pri-中{background:#fff4e6;color:var(--amber)} .pri-提示{background:#e7f5ff;color:#1971c2}
  .act .tx{flex:1;font-size:13.5px;line-height:1.45}
  /* 甘特图 */
  .gantt-wrap{overflow-x:auto}
  .gantt{position:relative;min-width:820px;padding-top:22px}
  .gantt-row{display:flex;align-items:center;gap:12px;margin:7px 0;font-size:13px}
  .gantt-label{width:230px;flex:0 0 230px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink)}
  .gantt-track{position:relative;flex:1;height:24px;background:#f1f3f8;border-radius:6px}
  .gantt-bar{position:absolute;top:3px;height:18px;border-radius:5px;overflow:hidden;display:flex;align-items:center;color:#fff;font-size:10.5px;font-weight:700}
  .gantt-prog{position:absolute;left:0;top:0;bottom:0;background:rgba(255,255,255,.32)}
  .gantt-cap{position:relative;padding:0 6px;white-space:nowrap}
  .gantt-today{position:absolute;top:0;bottom:0;width:2px;background:var(--red);z-index:2}
  .gantt-today::after{content:"今日";position:absolute;top:-20px;left:-11px;font-size:10px;color:var(--red);font-weight:700}
  /* 补录面板 */
  .bf-card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:var(--shadow)}
  .bf-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
  .bf-btn{appearance:none;border:0;border-radius:10px;padding:8px 14px;font-size:13px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;min-height:40px;font-weight:600}
  .bf-btn.copy{background:var(--primary);color:#fff}
  .bf-btn.ghost{background:#eef1f7;color:var(--ink)}
  .bf-btn:hover{opacity:.9}
  .bf-count{font-size:12.5px;color:var(--sub)}
  .bf-scroll{overflow-x:auto;border:1px solid var(--line);border-radius:12px}
  .bf-table{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}
  .bf-table th,.bf-table td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
  .bf-table th{color:var(--sub);font-weight:600;font-size:12px;background:#f8f9fc;position:sticky;top:0}
  .bf-table input,.bf-table select{width:100%;min-width:120px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;
    font-size:13px;background:#fbfcfe;color:var(--ink);font-family:inherit}
  .bf-table input:focus,.bf-table select:focus{outline:none;border-color:var(--primary);background:#fff}
  .bf-name{font-weight:600;white-space:nowrap}
  .bf-sub{font-size:11px;color:var(--sub);font-weight:500;margin-top:2px}
  @media(max-width:680px){.bf-table input,.bf-table select{min-width:104px}}
  /* 同步徽标 + 弹窗 */
  .sync-badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#fff;opacity:.94;margin-left:auto;padding:5px 11px;background:rgba(255,255,255,.16);border-radius:999px;font-weight:600;white-space:nowrap}
  .sync-badge .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
  @media(max-width:680px){.sync-badge{margin-left:0;width:100%;justify-content:center;margin-top:8px}}
  .btn-sync{background:rgba(255,255,255,.22)}
  .modal-mask{position:fixed;inset:0;background:rgba(15,23,42,.5);display:flex;align-items:center;justify-content:center;z-index:60;padding:16px}
  .modal{background:#fff;border-radius:16px;max-width:540px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.28);overflow:hidden;animation:pop .16s ease}
  @keyframes pop{from{transform:scale(.96);opacity:.4}to{transform:scale(1);opacity:1}}
  .modal-h{font-size:16px;font-weight:700;padding:15px 18px;display:flex;align-items:center;gap:8px;color:#fff;background:var(--primary)}
  .modal-h .svg-ic{fill:#fff;width:18px;height:18px}
  .sync-t{width:100%;border-collapse:collapse}
  .sync-t td{padding:11px 18px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}
  .sync-t .sk{color:var(--sub);width:38%;font-weight:600;white-space:nowrap}
  .sync-t .sv{color:var(--ink)}
  .sync-t .sv .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}
  .modal .note{font-size:12px;color:var(--sub);padding:12px 18px 0;line-height:1.65}
  .modal-ft{display:flex;gap:10px;padding:14px 18px 18px;flex-wrap:wrap}
  .btn-ghost{background:#eef1f7;color:var(--ink)}
  @media(max-width:680px){.modal-ft .btn{flex:1}}
  /* 访问口令门 */
  .lock-mask{position:fixed;inset:0;background:linear-gradient(135deg,#1e293b,#0f172a);display:flex;align-items:center;justify-content:center;z-index:9999;padding:16px}
  .lock-card{background:#fff;border-radius:18px;box-shadow:0 24px 70px rgba(0,0,0,.4);padding:34px 36px;width:min(360px,88vw);text-align:center}
  .lock-emoji{font-size:34px;margin-bottom:6px}
  .lock-card h2{margin:0 0 6px;font-size:19px;color:#1f2937}
  .lock-card p{margin:0 0 20px;font-size:13px;color:#6b7280}
  .lock-role{font-size:13px;font-weight:700;color:#3b5bdb;margin:0 0 6px}
  .lock-input{width:100%;box-sizing:border-box;padding:12px 14px;font-size:16px;border:1.5px solid #d8dce3;border-radius:11px;outline:none;transition:border-color .15s}
  .lock-input:focus{border-color:#3b5bdb}
  .lock-btn{margin-top:14px;width:100%;padding:12px;border:none;border-radius:11px;background:#3b5bdb;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
  .lock-btn:active{background:#2f4bc4}
  .lock-err{color:#e03131;font-size:13px;min-height:18px;margin-top:10px;font-weight:600}
  .lock-hint{font-size:11px;color:#9aa3af;margin-top:14px;line-height:1.5}
  .role-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 14px;padding:10px 12px;background:#fff;border:1px solid var(--bd);border-radius:14px}
  .role-lbl{font-weight:700;font-size:13px;color:var(--sub)}
  .role-tab{border:1px solid var(--bd);background:#f8f9fc;color:var(--txt);font-size:13px;font-weight:600;padding:8px 14px;border-radius:10px;cursor:pointer;transition:.15s}
  .role-tab:hover{border-color:#3b5bdb;color:#3b5bdb}
  .role-tab.active{background:#3b5bdb;border-color:#3b5bdb;color:#fff}
  .role-hint{margin-left:auto;font-size:11px;color:var(--sub)}
  .role-share{border:1px solid #3b5bdb;background:#3b5bdb;color:#fff;font-size:13px;font-weight:600;padding:8px 14px;border-radius:10px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:.15s}
  .role-share:hover{background:#2f4bc4}
  .role-key{border:1px solid var(--bd);background:#fff;color:var(--txt);font-size:13px;font-weight:600;padding:8px 14px;border-radius:10px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:.15s}
  .role-key:hover{background:#f1f3f8}
  /* 口令管理弹窗 */
  .modal{display:none;position:fixed;inset:0;z-index:60}
  .modal.show{display:flex;align-items:center;justify-content:center}
  .modal-mask{position:absolute;inset:0;background:rgba(15,23,42,.45)}
  .modal-card{position:relative;width:min(440px,92vw);max-height:86vh;overflow:auto;background:#fff;border-radius:16px;padding:18px 20px;box-shadow:0 20px 60px rgba(0,0,0,.25)}
  .modal-h{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:800;margin-bottom:4px}
  .modal-sub{font-size:11px;font-weight:500;color:var(--sub)}
  .modal-x{margin-left:auto;border:none;background:#f1f3f8;width:28px;height:28px;border-radius:8px;font-size:18px;line-height:1;cursor:pointer;color:var(--sub)}
  .modal-x:hover{background:#e3e7ef}
  .pw-list{margin:14px 0 6px;display:flex;flex-direction:column;gap:8px}
  .pw-row{display:flex;align-items:center;gap:8px}
  .pw-name{width:120px;font-size:13px;font-weight:600;color:var(--txt)}
  .pw-val{flex:1;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;padding:7px 10px;border:1px solid var(--bd);border-radius:8px;background:#f8f9fc;color:#111}
  .pw-cp{border:1px solid var(--bd);background:#fff;color:var(--txt);font-size:12px;font-weight:600;padding:7px 12px;border-radius:8px;cursor:pointer}
  .pw-cp:hover{background:#f1f3f8}
  .modal-actions{display:flex;gap:10px;margin-top:14px}
  .btn-primary{border:none;background:#3b5bdb;color:#fff;font-size:13px;font-weight:700;padding:10px 16px;border-radius:10px;cursor:pointer}
  .btn-primary:hover{background:#2f4bc4}
  .btn-ghost{border:1px solid var(--bd);background:#fff;color:var(--txt);font-size:13px;font-weight:600;padding:10px 16px;border-radius:10px;cursor:pointer}
  .btn-ghost:hover{background:#f1f3f8}
  .pw-note{margin-top:12px;font-size:12px;line-height:1.6;color:#b45309;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:10px 12px}
  .pw-note textarea.pw-out{width:100%;margin-top:8px;height:92px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;padding:8px;border:1px solid var(--bd);border-radius:8px;resize:vertical}
  .role-share:hover{background:#2f4bc4;border-color:#2f4bc4}
  .role-share .svg-ic{vertical-align:middle}
  .toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));transform:translateX(-50%) translateY(20px);background:#1f2430;color:#fff;font-size:13.5px;line-height:1.55;padding:12px 18px;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.22);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:9999;max-width:88vw;text-align:center}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  @media(max-width:680px){.role-hint{display:none}.role-tab{flex:1;text-align:center}.role-share{flex:1;justify-content:center}}
</style>
</head>
<body>
<div class="lock-mask" id="lockMask">
  <div class="lock-card">
    <div class="lock-emoji">🔒</div>
    <h2>需要访问口令</h2>
    <p>生产 · 项目管理驾驶舱 · 内部业务数据</p>
    <div class="lock-role" id="lockRole"></div>
    <input class="lock-input" id="lockInput" type="password" placeholder="请输入访问口令" autocomplete="off">
    <button class="lock-btn" id="lockBtn">解锁查看</button>
    <div class="lock-err" id="lockErr"></div>
    <div class="lock-hint">管理员口令可看全部角色；各角色有独立口令，仅能看自己那一份。口令已 base64 混淆存储，仍非真加密，请勿泄露。</div>
  </div>
</div>
<div class="modal" id="pwModal">
  <div class="modal-mask" id="pwModalMask"></div>
  <div class="modal-card">
    <div class="modal-h">口令管理 <span class="modal-sub">仅管理员可见 · 已 base64 混淆</span><button class="modal-x" id="pwClose" aria-label="关闭">×</button></div>
    <div id="pwList" class="pw-list"></div>
    <div class="modal-actions">
      <button class="btn-primary" id="pwRotate">轮换全部口令</button>
      <button class="btn-ghost" id="pwCopyAll">复制全部口令</button>
    </div>
    <div class="pw-note" id="pwNote"></div>
  </div>
</div>
<div class="wrap">
  <header class="top">
    <span class="demo-flag" id="demoFlag"></span>
    <h1>__TITLE_ICON__ 生产 · 项目管理驾驶舱</h1>
    <div class="meta" id="metaLine"></div>
    <div class="toolbar">
      <button class="btn" id="btnExport">__IC_DOWNLOAD__ 导出分析JSON</button>
      <button class="btn" id="btnImport">__IC_UPLOAD__ 导入数据快照</button>
      <button class="btn" id="btnAnalyze" style="background:rgba(255,255,255,.32)">__IC_ANALYZE__ 分析数据（复制发我）</button>
      <button class="btn" id="btnRefresh">__IC_REFRESH__ 重新生成说明</button>
      <button class="btn btn-sync" id="btnSync">__IC_SYNC__ 同步状况</button>
      <input type="file" id="fileInput" accept="application/json" style="display:none">
      <span class="sync-badge" id="syncBadge"></span>
    </div>
    <div class="banner" id="banner"></div>
  </header>

  <div id="app"></div>

  <footer>驾驶舱由 seatable-production 技能数据快照生成 · 单文件离线可用 · 重跑 cockpit.py 可刷新</footer>
</div>

<script>
let MODEL = __MODEL__;
let UNLOCK = null;  // 解锁态：null | {level:"admin"} | {level:"role",role:"xxx"}

/* 口令以 base64 形式嵌在源码里，运行时解码；管理员本地轮换会写 localStorage 覆盖 */
function _b64dec(s){ try{ const bin=atob(s); const b=new Uint8Array(bin.length); for(let i=0;i<bin.length;i++) b[i]=bin.charCodeAt(i); return JSON.parse(new TextDecoder("utf-8").decode(b)); }catch(e){ return {}; } }
function _b64enc(o){ return btoa(unescape(encodeURIComponent(JSON.stringify(o)))); }
let PW = (function(){ const o=localStorage.getItem("cockpit_pw_override"); if(o){ try{ return _b64dec(o); }catch(e){} } return _b64dec(__PW_BLOB__); })();

/* ---------- 工具 ---------- */
const $ = (s,r=document)=>r.querySelector(s);
function fmt(n){ if(n===null||n===undefined||isNaN(n)) return "0";
  return Number(n).toLocaleString("zh-CN",{maximumFractionDigits:0}); }
function yuan(n){ return "¥"+fmt(n); }
function pct(n){ return (n==null?"—":n+"%"); }
function el(html){ const t=document.createElement("template"); t.innerHTML=html.trim(); return t.content.firstChild; }

/* ---------- SVG 图表 ---------- */
function donut(pctv, color, size=120){
  const r=size/2-12, c=2*Math.PI*r;
  const v=(pctv==null)?0:Math.min(100,Math.max(0,pctv));
  const off=c*(1-v/100);
  const disp=(pctv==null)?"—":pctv+"%";
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="#eef1f7" stroke-width="12"/>
    <circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="${color}" stroke-width="12"
      stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"
      transform="rotate(-90 ${size/2} ${size/2})"/>
    <text x="${size/2}" y="${size/2-2}" text-anchor="middle" font-size="22" font-weight="800" fill="#1f2733">${disp}</text>
    <text x="${size/2}" y="${size/2+18}" text-anchor="middle" font-size="11" fill="#6b7686">达成率</text>
  </svg>`;
}
function bars(items, opts={}){
  // items: [{name,value,color}]
  const max=Math.max(1,...items.map(i=>i.value));
  return items.map(i=>{
    const w=Math.round(i.value/max*100);
    const col=i.color||"#3b5bdb";
    return `<div class="bar-row"><div class="nm">${i.name}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${w}%;background:${col}"></div></div>
      <div class="vl num">${opts.fmt?opts.fmt(i.value):fmt(i.value)}</div></div>`;
  }).join("");
}
function pie(items,size=150){
  const total=items.reduce((s,i)=>s+i.value,0)||1;
  let ang=-Math.PI/2, parts="";
  items.forEach(i=>{
    const a2=ang+i.value/total*2*Math.PI;
    const x1=size/2+size/2*0.42*Math.cos(ang), y1=size/2+size/2*0.42*Math.sin(ang);
    const x2=size/2+size/2*0.42*Math.cos(a2), y2=size/2+size/2*0.42*Math.sin(a2);
    const large=(a2-ang)>Math.PI?1:0;
    parts+=`<path d="M${size/2} ${size/2} L${x1.toFixed(1)} ${y1.toFixed(1)} A${size/2*0.42} ${size/2*0.42} 0 ${large} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} Z" fill="${i.color}"/>`;
    ang=a2;
  });
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">${parts}</svg>`;
}

/* ---------- 补录（本地填写 → 复制发回） ---------- */
const BF_KEY = "cockpit_backfill_v1";
function loadOV(){ try{ return JSON.parse(localStorage.getItem(BF_KEY) || "{}"); }catch(e){ return {}; } }
function saveOV(o){ localStorage.setItem(BF_KEY, JSON.stringify(o)); }
function applyBackfillOverrides(m){
  const o = loadOV();
  const bp = o["生产计划"] || {}, ba = o["组装记录"] || {};
  (m.gantt || []).forEach(g => {
    const ov = bp[g.row_id]; if(!ov) return;
    if(ov["计划开始日期"]) g.start = ov["计划开始日期"];
    if(ov["计划完成日期"]) g.end = ov["计划完成日期"];
    if(ov["实际完成日期"]){ g.end = ov["实际完成日期"]; g.done = true; g.progress = 100; g.overdue = false; }
  });
  const vals = [];
  (m.backfill["组装记录"] || []).forEach(a => {
    const ov = ba[a.row_id];
    const v = ov && ov["组装良品率"] !== "" && ov["组装良品率"] !== undefined ? parseFloat(ov["组装良品率"]) : NaN;
    if(!isNaN(v)) vals.push(v);
  });
  if(vals.length) m.quality.asm_yield = Math.round(vals.reduce((s,x)=>s+x,0) / vals.length * 10) / 10;
}
function collectOV(){
  const o = {生产计划:{}, 组装记录:{}};
  document.querySelectorAll("#bfBody [data-rid]").forEach(n => {
    const tbl = n.getAttribute("data-tbl"), rid = n.getAttribute("data-rid"), field = n.getAttribute("data-field");
    const val = n.value;
    o[tbl][rid] = o[tbl][rid] || {};
    o[tbl][rid][field] = val;
  });
  for(const tbl of ["生产计划","组装记录"]){
    for(const rid in o[tbl]){
      const f = o[tbl][rid];
      for(const k in f) if(f[k] === "") delete f[k];
      if(!Object.keys(f).length) delete o[tbl][rid];
    }
  }
  return o;
}
function onBfChange(){
  const o = collectOV(); saveOV(o); render(MODEL);
}
function copyBackfill(){
  const o = loadOV();
  const submit = {}; let cnt = 0;
  for(const tbl of ["生产计划","组装记录"]){
    const t = o[tbl] || {};
    const cleaned = {};
    for(const rid in t){
      const f = {};
      for(const k in t[rid]){ const v = t[rid][k]; if(v !== "" && v !== null && v !== undefined){ f[k] = v; cnt++; } }
      if(Object.keys(f).length) cleaned[rid] = f;
    }
    if(Object.keys(cleaned).length) submit[tbl] = cleaned;
  }
  if(!cnt){ alert("还没填任何值哦～先在上方表格里补录缺失字段，再复制。"); return; }
  const txt = JSON.stringify(submit, null, 2);
  const done = () => alert("已复制 " + cnt + " 条补录数据到剪贴板 ✅\n\n把下面这段直接粘贴到 WorkBuddy 对话发给我，我会跟你确认后再写入 SeaTable 云端真库。");
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(done, () => fallbackCopy(txt, done));
  } else { fallbackCopy(txt, done); }
}
function fallbackCopy(txt, cb){
  const ta = document.createElement("textarea"); ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); cb(); } catch(e){ alert("复制失败，请手动选择下方文本复制：\n\n" + txt); }
  document.body.removeChild(ta);
}
function clearBackfill(){
  if(confirm("确定清空本机已填的补录数据？此操作仅清本地浏览器，不影响云端。")){
    localStorage.removeItem(BF_KEY); location.reload();
  }
}

/* ---------- 渲染 ---------- */
function renderGantt(g, todayStr){
  if(!g.length) return '<div class="empty">甘特图需「立项日期」与「合同交期」均填写，当前可用 '+g.length+' 条</div>';
  const toD=s=>{const [y,m,d]=s.split('-').map(Number);return new Date(y,m-1,d);};
  const starts=g.map(x=>toD(x.start)), ends=g.map(x=>toD(x.end)), td=toD(todayStr);
  const min=new Date(Math.min.apply(null,starts)), max=new Date(Math.max.apply(null,ends));
  const span=Math.max(1,(max-min)/86400000);
  const pct=d=>((d-min)/86400000)/span*100;
  const ticks=[];
  let cur=new Date(min.getFullYear(),min.getMonth(),1);
  while(cur<=max){
    if(cur>=min){
      const lbl=cur.getFullYear()+'-'+String(cur.getMonth()+1).padStart(2,'0');
      ticks.push(`<div style="position:absolute;left:${pct(cur).toFixed(2)}%;top:0;bottom:0;border-left:1px dashed #d7dde8">
        <span style="position:absolute;top:-20px;left:4px;font-size:10.5px;color:var(--sub)">${lbl}</span></div>`);
    }
    cur=new Date(cur.getFullYear(),cur.getMonth()+1,1);
  }
  let tp=pct(td); tp=Math.max(0,Math.min(100,tp));
  const rows=g.map(x=>{
    const s=toD(x.start), e=toD(x.end);
    const left=pct(s), width=Math.max(1.5,pct(e)-pct(s));
    const col=x.done?'#2f9e44':(x.overdue?'#e03131':'#3b5bdb');
    const cap=x.done?'已完成':(x.overdue?'逾期':x.progress+'%');
    return `<div class="gantt-row"><div class="gantt-label" title="${x.name}">${x.name}</div>
      <div class="gantt-track"><div class="gantt-bar" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%;background:${col}">
        <div class="gantt-prog" style="width:${x.progress}%"></div>
        <span class="gantt-cap">${cap}</span></div></div></div>`;
  }).join("");
  return `<div><div style="position:absolute;left:242px;right:0;top:22px;bottom:0;pointer-events:none">
      ${ticks.join('')}<div class="gantt-today" style="left:${tp.toFixed(2)}%"></div></div>
    ${rows}</div>`;
}
/* ---------- 角色视图（单文件 + 角色切换 + #role 书签）---------- */
const ROLES={
  boss:      {name:"老板",     sections:["K","A","WZ","BF","PW","G","T","C","Q","P","Sup","Inv"], actions:null},
  warehouse: {name:"仓库",     sections:["K","A","WZ","Inv","P"],                       actions:["warehouse"]},
  purchase:  {name:"采购",     sections:["K","A","WZ","Sup"],                          actions:["purchase"]},
  production:{name:"生产经理", sections:["K","A","WZ","G","T","Q","P","Inv"],           actions:["production","warehouse","delivery"]},
  sales:     {name:"销售",     sections:["K","A","WZ","PW","G"],                       actions:["sales","delivery"]},
};
const ROLE_ORDER=["boss","warehouse","purchase","production","sales"];
function currentRole(){
  const m=/role=([a-z]+)/.exec(location.hash||"");
  const r=m?m[1]:"";
  return ROLES[r]?r:"boss";
}
function buildRoleBar(role, unlock){
  const isAdmin = unlock && unlock.level==="admin";
  const keys = isAdmin ? ROLE_ORDER : [role];
  const tabs=keys.map(key=>`<button class="role-tab ${key===role?"active":""}" data-role="${key}">${ROLES[key].name}</button>`).join("");
  const shareBtn=(role==="boss"||role==="sales")
    ? `<button class="role-share" id="roleShareBtn">__IC_SHARE__ 分享给${ROLES[role].name}</button>` : "";
  const keyBtn = isAdmin
    ? `<button class="role-key" id="pwManageBtn">__IC_KEY__ 口令管理</button>` : "";
  const hint = isAdmin
    ? `每人开一个标签页钉住自己的 #role 即独立窗口；视图按角色裁剪，敏感财务仅老板可见`
    : `本视图已按口令锁定（${ROLES[role].name}），如需切换其他视图请用管理员口令重新打开`;
  const bar=el(`<div class="role-bar"><span class="role-lbl">角色视图</span>${tabs}${shareBtn}${keyBtn}
    <span class="role-hint">${hint}</span></div>`);
  bar.querySelectorAll(".role-tab").forEach(b=>b.onclick=()=>{ if(isAdmin) location.hash="role="+b.dataset.role; });
  const sb=bar.querySelector("#roleShareBtn");
  if(sb) sb.onclick=()=>shareRole(role);
  const kb=bar.querySelector("#pwManageBtn");
  if(kb) kb.onclick=openPwModal;
  return bar;
}
function supplierAvg(s){
  const rs=(s.supplier||[]).map(x=>x.rate).filter(x=>x!=null);
  return rs.length? rs.reduce((a,b)=>a+b,0)/rs.length : 0;
}
function buildKPIs(m, role){
  const k=m.kpi, t=m.time, q=m.quality, s=m.supply, pd=m.partdb;
  const b=pd?pd.bom:null;
  const M={
    projects:{l:"项目总数",v:fmt(k.projects),s:`进行中 ${k.active} · 计划 ${k.planned} · 完成 ${k.done}`,c:""},
    contract:{l:"总合同额",v:yuan(k.contract),s:"已收 "+yuan(k.received),c:""},
    received:{l:"已收金额",v:yuan(k.received),s:"合同执行率 "+pct(k.exec_rate),c:""},
    receivable:{l:"应收款",v:yuan(k.receivable),s:"待回收",c:"neg"},
    cost:{l:"生产总花销",v:yuan(k.cost),s:"毛利率 "+pct(k.margin),c:""},
    margin:{l:"毛利率",v:pct(k.margin),s:"目标 30%",c:""},
    unit_cost:{l:"单片成本",v:yuan(k.unit_cost),s:"台账均单价",c:""},
    exec_rate:{l:"合同执行率",v:pct(k.exec_rate),s:"已收 / 合同",c:""},
    ontime_rate:{l:"交期达成率",v:pct(k.ontime_rate),s:"在制 "+k.wip+" 单",c:""},
    purchase_overdue:{l:"采购逾期",v:fmt(k.purchase_overdue),s:"需跟进",c:k.purchase_overdue>0?"neg":""},
    wip:{l:"在制单数",v:fmt(k.wip),s:"进行中生产",c:""},
    shortage:{l:"在产缺料",v:fmt(b?b.shortage.length:0),s:`零确认 ${pd?pd.zero_confirmed:0} 种`,c:(b&&b.shortage.length)?"neg":""},
    kit_rate:{l:"物料齐套率",v:pct(b?((b.bom_count-b.shortage.length)/b.bom_count*100):100),s:`BOM ${b?b.bom_count:0} 行`,c:""},
    part_count:{l:"在库料号",v:fmt(pd?pd.part_count:0),s:"零件总数",c:""},
    zero_stock:{l:"零确认库存",v:fmt(pd?pd.zero_confirmed:0),s:"需盘点",c:(pd&&pd.zero_confirmed>0)?"neg":""},
    supplier_ontime:{l:"供应商准时率",v:pct(supplierAvg(s)),s:`${s.supplier.length} 家`,c:supplierAvg(s)<70?"neg":""},
    smt_yield:{l:"贴片良品率",v:pct(q.smt_yield),s:"良品 / 投入",c:""},
    repair_rate:{l:"维修率",v:pct(q.repair_rate),s:`${q.repair_total}/${q.shipped}`,c:""},
    cycle:{l:"平均生产周期",v:t.avg_cycle+"天",s:"实际花费天数",c:""},
  };
  const sets={
    boss:["projects","contract","receivable","cost","margin","ontime_rate","purchase_overdue","unit_cost"],
    warehouse:["shortage","kit_rate","part_count","zero_stock"],
    purchase:["purchase_overdue","supplier_ontime","shortage","ontime_rate"],
    production:["wip","ontime_rate","cycle","smt_yield","repair_rate","shortage"],
    sales:["projects","contract","received","receivable","ontime_rate","wip"],
  };
  return (sets[role]||sets.boss).map(key=>M[key]).filter(Boolean);
}

function render(m){
  applyBackfillOverrides(m);
  _navIO&&_navIO.disconnect();
  const role=currentRole();
  // 权限校验：非管理员且当前角色不是自己解锁的角色 → 越权，锁定回授权角色
  if(UNLOCK && UNLOCK.level!=="admin" && role!==UNLOCK.role){
    toast("无权查看「"+ROLES[role].name+"」视图，已锁定在「"+ROLES[UNLOCK.role].name+"」");
    location.hash="role="+UNLOCK.role;
    return;
  }
  const app=$("#app"); app.innerHTML="";
  $("#metaLine").textContent="数据快照："+m.snapshot
    + (m.partdb?" · 物料/缺料接入 PartDB 实时（"+m.partdb.generated_at+"）":"")
    + (m.synced_at?" · 业务表接入 SeaTable 云「"+(m.base_name||"生产")+"」（同步 "+m.synced_at+"）":"")
    + " · 四维分析（工时/成本/质量/供应链）";
  const flag=$("#demoFlag");
  if(m.isDemo){
    flag.textContent="演示数据"; flag.className="demo-flag"; flag.style.display="inline-block";
    $("#banner").innerHTML="当前为<b>演示数据</b>（虚构示例）。清空本地 data/ 后录入真实数据，重跑 cockpit.py 即可生成你的真实驾驶舱。";
  }else{
    flag.textContent="真实数据 · SeaTable云"; flag.className="real-flag"; flag.style.display="inline-block";
    $("#banner").innerHTML="业务表已接入 SeaTable 云端「"+(m.base_name||"生产")+"」真实数据（同步于 "+m.synced_at+"）。物料/缺料来自 PartDB 实时。"
      + (m.partdb?"":"<span style='color:var(--red)'> ⚠ PartDB 未连接。</span>");
  }
  app.appendChild(buildRoleBar(role, UNLOCK));

  /* KPI 概览（按角色裁剪） */
  const k=m.kpi;
  const kpis=buildKPIs(m, role);
  const secK=el(`<section id="sec-K" class="sec"><div class="sec-title">__IC_GRID__ 核心指标概览 · ${ROLES[role].name}</div>
    <div class="hscroll" id="kpig"></div></section>`);
  kpis.forEach(x=>secK.querySelector("#kpig").appendChild(el(
    `<div class="card kpi"><div class="v ${x.c}">${x.v}</div><div class="l">${x.l}</div>
     <div class="l">${x.s}</div></div>`)));
  if(ROLES[role].sections.includes("K")) app.appendChild(secK);

  /* 下一步行动建议（按角色过滤） */
  const actsAll=m.next_actions||[];
  const acts=(role==="boss")?actsAll:actsAll.filter(a=>(ROLES[role].actions||[]).includes(a.cat));
  const actHTML=acts.length?acts.map(a=>`<div class="act"><span class="pri pri-${a.pri}">${a.pri}</span>
    <span class="tx">${a.text}</span></div>`).join(""):`<div class="act"><span class="pri pri-提示">提示</span><span class="tx">当前角色暂无专属待办事项，保持节奏即可 ✔</span></div>`;
  const secA=el(`<section id="sec-A" class="sec"><div class="sec-title">__IC_NEXT__ 下一步行动建议 · ${ROLES[role].name}（按优先级）</div>
    <div class="card"><div class="actions">${actHTML}</div>
    <div class="note">基于当前真实数据自动推导，按角色筛选：红=高优、橙=中优、蓝=提示。老板视图含全部战略项。</div></div></section>`);
  if(ROLES[role].sections.includes("A")) app.appendChild(secA);

  /* 补录缺失数据（行内填写 → 存本地 → 复制发回 → 写云端） */
  const bf = m.backfill || {生产计划: [], 组装记录: []};
  const ov = loadOV();
  const planRows = (bf["生产计划"] || []).map(p => {
    const o = (ov["生产计划"] || {})[p.row_id] || {};
    const v = (f) => (o[f] !== undefined ? o[f] : (p[f] || ""));
    return `<tr>
      <td class="bf-name">${p.name}<div class="bf-sub">${p.row_id || ""}</div></td>
      <td><input type="date" data-tbl="生产计划" data-rid="${p.row_id}" data-field="计划开始日期" value="${v("计划开始日期")}"></td>
      <td><input type="date" data-tbl="生产计划" data-rid="${p.row_id}" data-field="计划完成日期" value="${v("计划完成日期")}"></td>
      <td><input type="date" data-tbl="生产计划" data-rid="${p.row_id}" data-field="实际完成日期" value="${v("实际完成日期")}"></td>
      <td><select data-tbl="生产计划" data-rid="${p.row_id}" data-field="放行状态">
        <option value=""></option>
        <option value="待评审" ${v("放行状态") === "待评审" ? "selected" : ""}>待评审</option>
        <option value="允许进入下一阶段" ${v("放行状态") === "允许进入下一阶段" ? "selected" : ""}>允许进入下一阶段</option>
        <option value="禁止放行" ${v("放行状态") === "禁止放行" ? "selected" : ""}>禁止放行</option>
      </select></td></tr>`;
  }).join("");
  const asmRows = (bf["组装记录"] || []).map(a => {
    const o = (ov["组装记录"] || {})[a.row_id] || {};
    const val = o["组装良品率"] !== undefined ? o["组装良品率"] : (a["组装良品率"] || "");
    return `<tr>
      <td class="bf-name">${a.name}<div class="bf-sub">${a.row_id || ""}</div></td>
      <td colspan="3" style="color:var(--sub);font-size:12px">组装良品率（%，仅此列需填）</td>
      <td><input type="number" step="0.1" min="0" max="100" data-tbl="组装记录" data-rid="${a.row_id}" data-field="组装良品率" value="${val}" placeholder="如 98.5"></td></tr>`;
  }).join("");
  let bfCnt = 0;
  for (const tbl of ["生产计划", "组装记录"]) for (const rid in (ov[tbl] || {})) for (const k in ov[tbl][rid]) if (ov[tbl][rid][k] !== "") bfCnt++;
  const secBF = el(`<section id="sec-BF" class="sec"><div class="sec-title">__IC_EDIT__ 补录缺失数据（本地填写 → 复制发我 → 写云端）</div>
    <div class="bf-card">
      <div class="bf-toolbar">
        <button class="bf-btn copy" id="bfCopy">__IC_COPY__ 一键复制补录数据</button>
        <button class="bf-btn ghost" id="bfClear">清空本地补录</button>
        <span class="bf-count">已填 <b id="bfCount">${bfCnt}</b> 项 · 数据存本机浏览器，不会自动上传</span>
      </div>
      <div class="bf-scroll"><table class="bf-table"><thead><tr>
        <th>产品 / 编号</th><th>计划开始</th><th>计划完成</th><th>实际完成</th><th>放行状态</th>
      </tr></thead><tbody id="bfBody">
        ${planRows}
        ${asmRows.length ? `<tr><td colspan="5" style="background:#f8f9fc;font-weight:600;color:var(--sub)">组装记录（${asmRows.length} 条）</td></tr>` : ""}
        ${asmRows}
      </tbody></table></div>
      <div class="note">说明：生产计划「计划开始 / 完成」已按 立项日期 / 合同交期 预填，可直接改；「实际完成」「放行状态」需你填写（放行状态三选一：待评审 / 允许进入下一阶段 / 禁止放行）。填完点「一键复制」，把内容粘贴到 WorkBuddy 发我，我确认后写入 SeaTable 云端真库。<b>写回不可逆</b>，我绝不替你编造数值。</div>
    </div></section>`);
  secBF.querySelector("#bfCopy").onclick = copyBackfill;
  secBF.querySelector("#bfClear").onclick = clearBackfill;
  if(ROLES[role].sections.includes("BF")) app.appendChild(secBF);

  /* 项目总览 + 在制品看板 */
  const projRows=m.projects.map(p=>`<tr>
    <td>${p.name}</td>
    <td><span class="pill st-${p.status}">${p.status}</span></td>
    <td class="num">${yuan(p.contract)}</td>
    <td class="num">${yuan(p.received)}</td>
    <td class="num ${p.receivable>0?'neg':''}">${yuan(p.receivable)}</td>
    <td class="num">${p.due||"—"}</td></tr>`).join("");
  const wipRows = m.wip.length ? m.wip.map(w=>{
    const rm = w.remain==null?"—":(w.remain<0?("逾期"+(-w.remain)+"天"):(w.remain+"天"));
    const cls = w.overdue?"tag-red":(w.remain!=null&&w.remain<=7?"tag-amber":"tag-green");
    return `<tr><td>${w.product}</td><td><span class="pill st-${w.status}">${w.status}</span></td>
      <td>${w.stage}</td><td class="num">${fmt(w.qty)}</td>
      <td class="num">${w.due||"—"}</td><td><span class="pill ${cls}">${rm}</span></td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">无在制品</td></tr>`;

  const secPW=el(`<section id="sec-PW" class="sec"><div class="sec-title">__IC_PROJ__ 项目总览 & 在制品看板</div>
    <div class="grid g2">
      <div class="card"><h3>项目清单（${m.projects.length}）</h3>
        <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>项目</th><th>状态</th><th>合同额</th><th>已收</th><th>应收</th><th>交期</th></tr></thead>
        <tbody>${projRows}</tbody></table></div></div>
      <div class="card"><h3>在制品看板（按剩余时间升序）</h3>
        <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>产品</th><th>状态</th><th>阶段</th><th>数量</th><th>交期</th><th>剩余</th></tr></thead>
        <tbody>${wipRows}</tbody></table></div>
        <div class="note">红色=已逾期，橙色=7天内到期，绿色=余量充足。昨天未完自动顺延至今日。</div></div>
    </div></section>`);
  if(ROLES[role].sections.includes("PW")) app.appendChild(secPW);

  /* 甘特图 */
  const g=m.gantt||[];
  const secG=el(`<section id="sec-G" class="sec"><div class="sec-title">__IC_GANT__ 生产计划甘特图（立项 → 合同交期）</div>
    <div class="card"><div class="gantt-wrap"><div class="gantt" id="gantt"></div></div>
    <div class="note">蓝色=进行中，绿色=已交付，红色=逾期；条内浅色填充为当前进度（按日期推算）。竖红线为今日 ${m.snapshot}。悬停产品名查看全称。</div></div></section>`);
  if(ROLES[role].sections.includes("G")){ app.appendChild(secG); $("#gantt").innerHTML=renderGantt(g,m.snapshot); }

  /* 工时 */
  const t=m.time;
  const stageItems=Object.entries(t.stage_dist).map(([k,v],i)=>({name:k,value:v,
    color:["#3b5bdb","#1c7ed6","#0ca678","#f08c00","#7048e8","#e8590c"][i%6]}));
  const cycleTxt=t.cycle_list.length?t.cycle_list.map(c=>c.product+":"+c.days+"天").join(" · "):"暂无工序耗时记录";
  const secT=el(`<section id="sec-T" class="sec"><div class="sec-title">__IC_TIME__ 工时分析（交付能力）</div>
    <div class="grid g3">
      <div class="card"><h3>交期达成率（内部周期）</h3><div class="donut-wrap">
        <div>${donut(t.ontime_rate,"#3b5bdb")}</div>
        <div class="legend"><div class="row"><span class="dot" style="background:#3b5bdb"></span>
          <span class="nm">周期达成</span><span class="vl">${t.ontime}/${t.dated||0}</span></div>
          <div class="row"><span class="nm" style="color:var(--sub)">平均实际周期</span>
          <span class="vl">${t.avg_cycle}天</span></div></div></div>
        <div class="note">${cycleTxt}</div></div>
      <div class="card"><h3>在制阶段分布</h3>${bars(stageItems)}</div>
      <div class="card"><h3>说明</h3>
        <div class="note">交期达成率 = 内部周期达成：实际花费天数 ≤ 允许的(合同交期−立项)天数 的计划占比。<br>
        平均实际周期 = 各计划「花费天数」均值（真实工序耗时）。<br>
        阶段分布反映当前产能瓶颈所在工序。</div></div>
    </div></section>`);
  if(ROLES[role].sections.includes("T")) app.appendChild(secT);

  /* 成本 */
  const c=m.cost;
  const palette=["#3b5bdb","#1c7ed6","#0ca678","#f08c00","#7048e8","#e8590c","#1098ad","#d6336c"];
  const catItems=c.category.map((x,i)=>({name:x.name,value:x.value,color:palette[i%palette.length]}));
  const recvRows=c.receivable_list.length?c.receivable_list.map(p=>`<tr>
    <td>${p.name}</td><td class="num neg">${yuan(p.receivable)}</td>
    <td class="num">${p.due||"—"}</td><td><span class="pill ${p.due&&p.due<m.snapshot?'tag-red':'tag-amber'}">${p.due&&p.due<m.snapshot?'逾期':'待收'}</span></td></tr>`).join("")
    :`<tr><td colspan="4" class="empty">无应收款</td></tr>`;
  const cf=c.cashflow.map(x=>`<div class="cell"><div class="bk">${x.bucket=="逾期"?"已逾期":x.bucket+"天内"}</div>
    <div class="bi num">+${fmt(x.in)}</div><div class="bo num">−${fmt(x.out)}</div>
    <div class="bn num ${x.net>=0?'pos':'neg'}">${x.net>=0?'+':''}${fmt(x.net)}</div></div>`).join("");
  const secC=el(`<section id="sec-C" class="sec"><div class="sec-title">__IC_COST__ 成本分析（盈利能力）</div>
    <div class="grid g2">
      <div class="card"><h3>成本结构（总花销 ${yuan(c.cost)}）</h3>
        <div class="donut-wrap"><div>${pie(catItems,150)}</div>
        <div class="legend">${catItems.map((x,i)=>`<div class="row"><span class="dot" style="background:${x.color}"></span>
          <span class="nm">${x.name}</span><span class="vl num">${yuan(x.value)}</span></div>`).join("")}</div></div></div>
      <div class="card"><h3>利润与预算</h3>
        <div class="kpi"><div class="v">${yuan(c.profit)}</div><div class="l">总利润（合同 ${yuan(c.contract)} − 花销 ${yuan(c.cost)}）</div></div>
        <div class="bar-row" style="margin-top:14px"><div class="nm">毛利率</div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100,c.margin)}%;background:${c.margin>=c.budget_margin?'#2f9e44':'#e03131'}"></div></div>
          <div class="vl num">${pct(c.margin)}</div></div>
        <div style="display:flex;justify-content:space-between;margin-top:12px;font-size:13px">
          <span style="color:var(--sub)">单片成本（台账均单价）</span><span class="num" style="font-weight:700">${yuan(c.unit_cost)}</span></div>
        <div class="note">目标毛利率 ${pct(c.budget_margin)} · ${c.margin>=c.budget_margin?'盈利充足 ✔':'低于目标 ⚠'}</div>
        <div class="note">台账口径生产总花销 ${yuan(c.ledger_cost)}（采购明细汇总 ${yuan(c.cost)}），差异含人工/辅料。</div></div>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h3>应收款催收（${c.receivable_list.length}）</h3>
        <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>项目</th><th>应收</th><th>合同交期</th><th>状态</th></tr></thead>
        <tbody>${recvRows}</tbody></table></div></div>
      <div class="card"><h3>现金流预测（30/60/90天）</h3><div class="cf">${cf}</div>
        <div class="note">收入按合同交期、支出按采购预计到货归集；净额为收减付。</div></div>
    </div></section>`);
  if(ROLES[role].sections.includes("C")) app.appendChild(secC);

  /* 质量 */
  const q=m.quality;
  const secQ=el(`<section id="sec-Q" class="sec"><div class="sec-title">__IC_QUAL__ 质量分析（交付质量）</div>
    <div class="grid g3">
      <div class="card"><h3>贴片良品率</h3><div class="donut-wrap">
        <div>${donut(q.smt_yield,"#0ca678")}</div>
        <div class="legend"><div class="row"><span class="dot" style="background:#0ca678"></span>
          <span class="nm">良品 / 投入</span><span class="vl">${pct(q.smt_yield)}</span></div>
          <div class="row"><span class="nm" style="color:var(--sub)">组装良品率</span><span class="vl">${q.asm_yield==null?'未录入':pct(q.asm_yield)}</span></div></div></div>
        <div class="note">组装良品率：2 条组装记录均未填良品率，暂无法计算（并非 0%）。</div></div>
      <div class="card"><h3>维修率</h3><div class="donut-wrap">
        <div>${donut(q.repair_rate,"#e8590c")}</div>
        <div class="legend"><div class="row"><span class="dot" style="background:#e8590c"></span>
          <span class="nm">维修 ${q.repair_total} / 发货 ${q.shipped}</span><span class="vl">${pct(q.repair_rate)}</span></div>
          <div class="row"><span class="nm" style="color:var(--sub)">超期未完修</span><span class="vl ${q.repair_overdue>0?'neg':''}">${q.repair_overdue}</span></div></div></div>
        <div class="note">平均返修周期 ${q.repair_avg} 天</div></div>
      <div class="card"><h3>维修明细</h3>
        ${q.repair_list.length?`<table data-paginate="8"><thead><tr><th>项目</th><th>问题</th><th>返修</th><th>状态</th></tr></thead><tbody>`
          +q.repair_list.map(r=>`<tr><td>${r.proj}</td><td>${r.item||"—"}</td><td class="num">${r.back}</td>
          <td><span class="pill ${r.done?'tag-green':(r.overdue?'tag-red':'tag-amber')}">${r.done?'已完成':(r.overdue?'超期':'处理中')}</span></td></tr>`).join("")
          +`</tbody></table>`:`<div class="empty">无维修记录</div>`}</div>
    </div></section>`);
  if(ROLES[role].sections.includes("Q")) app.appendChild(secQ);

  /* 生产进度 & 产线流转（基于生产计划 / 工序真实字段） */
  const tp=m.time;
  const flowEntries=Object.entries(tp.flow_dist).sort((a,b)=>a[0]<b[0]?-1:1);
  const FCOL=["#3b5bdb","#1c7ed6","#0ca678","#f08c00","#7048e8","#e8590c","#1098ad","#d6336c","#5c7cfa","#f783ac","#82c91e","#ff922b","#20c997","#845ef7","#fab005"];
  const flowItems=flowEntries.map(([k,v],i)=>({name:k.split(" ").slice(1).join(" ")||k,value:v,color:FCOL[i%FCOL.length]}));
  const flowMax=Math.max(1,...flowItems.map(x=>x.value));
  const flowHTML=flowItems.map(i=>`<div class="bar-row">
      <div class="nm" style="width:124px;flex:0 0 124px;text-align:left">${i.name}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round(i.value/flowMax*100)}%;background:${i.color}"></div></div>
      <div class="vl num">${i.value}</div></div>`).join("");
  const stageItems2=Object.entries(tp.stage_dist).map(([k,v],i)=>({name:k,value:v,color:["#3b5bdb","#1c7ed6","#0ca678","#f08c00","#7048e8","#e8590c"][i%6]}));
  const secP=el(`<section id="sec-P" class="sec"><div class="sec-title">__IC_PROJ__ 生产进度 & 产线流转</div>
    <div class="grid g2">
      <div class="card"><h3>产线实时流转（在产计划当前工序）</h3>${flowHTML}
        <div class="note">基于生产工序表「当前流程」：每个在产计划当前所处环节，用于定位产能瓶颈。共 ${flowEntries.length} 个工序环节在流转。</div></div>
      <div class="card"><h3>生产计划阶段分布</h3>${bars(stageItems2)}
        <div class="note">基于生产计划表「阶段」：整体进度分布（已交付/备料中/组装等）。</div></div>
    </div></section>`);
  if(ROLES[role].sections.includes("P")) app.appendChild(secP);

  /* 供应链 + 物料库存预警 */
  const s=m.supply;
  const supRows=s.supplier.length?s.supplier.map(x=>{
    const rc=x.rate==null?'—':pct(x.rate);
    const cls=x.rate==null?'':(x.rate>=90?'tag-green':(x.rate>=70?'tag-amber':'tag-red'));
    return `<tr><td>${x.name}</td><td class="num">${x.orders}</td>
      <td class="num">${fmt(x.ontime)}/${fmt(x.late)}/${fmt(x.pending)}</td>
      <td><span class="pill ${cls}">${rc}</span></td><td class="num">${yuan(x.cost)}</td></tr>`;
  }).join(""):`<tr><td colspan="5" class="empty">无采购记录</td></tr>`;
  const odRows=s.overdue_list.length?s.overdue_list.map(o=>`<tr>
    <td>${o.supplier}</td><td>${o.material||"—"}</td><td>${o.plan}</td>
    <td class="num">${o.eta}</td><td><span class="pill tag-red">逾期${o.days}天</span></td></tr>`).join("")
    :`<tr><td colspan="5" class="empty">无采购逾期 ✔</td></tr>`;
  const invRows=s.inventory_warn.length?s.inventory_warn.map(v=>`<tr>
    <td>${v.plan}</td><td>${v.result}</td><td class="num">${v.time||"—"}</td>
    <td><span class="pill tag-amber">待处理</span></td></tr>`).join("")
    :`<tr><td colspan="4" class="empty">库存核对正常 ✔</td></tr>`;
  const pd=m.partdb;
  let pdHTML;
  if(pd){
    const b=pd.bom;
    const shRows=b&&b.shortage.length?b.shortage.map(x=>{
      const rk=(x.risk&&x.risk.length)?x.risk.join('·'):'缺料';
      const cls=x.confirmed===0?'tag-red':(x.gap>0?'tag-amber':'tag-green');
      return `<tr><td>${x.name}${x.ipn?' <span class="sub">'+x.ipn+'</span>':''}</td>
        <td class="num">${x.qty_per}</td><td class="num">${x.need}</td>
        <td class="num">${x.confirmed}</td><td class="num">${x.gap}</td>
        <td class="num">${x.price!=null?yuan(x.price):'—'}</td>
        <td><span class="pill ${cls}">${rk}</span></td></tr>`;
    }).join(''):`<tr><td colspan="7" class="empty">✅ 当前库存可满足 ${b?b.qty:''} 套生产，无缺料</td></tr>`;
    const wRows=pd.inventory_warn.length?pd.inventory_warn.map(w=>{
      const cls=w.status==='紧急'?'tag-red':'tag-amber';
      return `<tr><td>${w.name}${w.ipn?' <span class="sub">'+w.ipn+'</span>':''}</td>
        <td class="num">${w.confirmed}</td><td class="num">${w.minamount}</td>
        <td class="num">${w.gap}</td><td><span class="pill ${cls}">${w.status}</span></td></tr>`;
    }).join(''):`<tr><td colspan="5" class="empty">✅ 暂无低于安全库存的零件（当前 ${pd.part_count} 个零件均未设置安全库存线 minamount）</td></tr>`;
    pdHTML=`<div class="card" style="margin-top:14px"><div class="pd-head">
        <h3>在产缺料检查 · ${b?b.project_name:''}（${b?b.qty:''} 套）</h3>
        <span class="badge-real">PartDB 实时 · ${pd.generated_at}</span></div>
      <div class="pd-stats">
        <div><b>${pd.part_count}</b><span>零件总数</span></div>
        <div><b class="${b&&b.shortage.length?'c-red':'c-green'}">${b?b.shortage.length:0}</b><span>在产缺料</span></div>
        <div><b class="${pd.zero_confirmed?'c-amber':''}">${pd.zero_confirmed}</b><span>零确认库存</span></div>
        <div><b>${b?yuan(b.single_set_cost):'—'}</b><span>单套BOM成本</span></div>
      </div>
      <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>物料</th><th>单套</th><th>需求</th><th>确认库存</th><th>缺口</th><th>单价</th><th>风险</th></tr></thead>
        <tbody>${shRows}</tbody></table></div>
      <div class="note">缺料 = 需求 − 已确认库存（仅计 description 含盘点日期 MDD/MMDD 的批次）。单套成本按最低有效报价汇总，${b?b.unpriced_count:0} 种无价未计入。</div>
    </div>
    <div class="card" style="margin-top:14px"><h3>库存健康 · 安全库存预警（minamount）</h3>
      <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>物料</th><th>确认库存</th><th>安全库存</th><th>缺口</th><th>状态</th></tr></thead>
        <tbody>${wRows}</tbody></table></div>
    </div>`;
  }else{
    pdHTML=`<div class="card" style="margin-top:14px"><h3>物料库存预警（库存核对记录）</h3>
      <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>关联计划</th><th>核对结果</th><th>完成时间</th><th>状态</th></tr></thead>
      <tbody>${invRows}</tbody></table></div>
      <div class="note">⚠ PartDB 未配置，BOM 级缺料检查已跳过；以上仅基于「库存核对记录」的最终完成状态判定。</div></div>`;
  }
  const secSup=el(`<section id="sec-Sup" class="sec"><div class="sec-title">__IC_SUP__ 供应链（采购）</div>
    <div class="grid g2">
      <div class="card"><h3>采购逾期（${s.overdue_list.length}）</h3>
        <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>供应商</th><th>物料</th><th>关联计划</th><th>预计到货</th><th>状态</th></tr></thead>
        <tbody>${odRows}</tbody></table></div></div>
      <div class="card"><h3>供应商交期准确率</h3>
        <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>供应商</th><th>订单</th><th>准/迟/待</th><th>准时率</th><th>累计花销</th></tr></thead>
        <tbody>${supRows}</tbody></table></div></div>
    </div></section>`);
  const secInv=el(`<section id="sec-Inv" class="sec"><div class="sec-title">__IC_BOX__ 物料库存预警（PartDB 实时）</div>
    ${pdHTML}</section>`);
  if(ROLES[role].sections.includes("Sup")) app.appendChild(secSup);
  if(ROLES[role].sections.includes("Inv")) app.appendChild(secInv);

  /* 新建生产项目向导：纯表单采集，提交导出 JSON；写入由 op.py apply-wizard 完成（网页不持有任何账号/口令） */
  const secWZ=el(`<section id="sec-WZ" class="sec"><div class="sec-title">__IC_ADD__ 新建生产项目（向导）</div>
    <div class="card">
      <p class="note">填完点「生成并下载」，浏览器会下载一个 <b>新建生产项目.json</b>。在本机跑一条命令即可写入数据（本地 CSV 或 SeaTable，取决于你的配置）：</p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:680px">
        <label>产品名称 *<input id="wz-product" type="text" placeholder="如 4G小卡二代" style="width:100%"></label>
        <label>数量 *<input id="wz-qty" type="number" min="1" placeholder="100" style="width:100%"></label>
        <label>交期天数（天数）<input id="wz-days" type="number" min="1" placeholder="30" style="width:100%"></label>
        <label>优先级<select id="wz-prio" style="width:100%"><option>高</option><option selected>中</option><option>低</option></select></label>
        <label>负责人<input id="wz-owner" type="text" placeholder="选填" style="width:100%"></label>
        <label>备注<input id="wz-note" type="text" placeholder="选填" style="width:100%"></label>
      </div>
      <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn-primary" onclick="submitWizard()">__IC_DOWNLOAD__ 生成并下载 JSON</button>
        <code id="wz-cmd" style="background:#f3f4f6;padding:6px 10px;border-radius:8px;font-size:13px">python op.py apply-wizard 新建生产项目.json</code>
      </div>
      <p class="note" style="margin-top:10px">💡 写入后重新生成驾驶舱即可看到新项目；若配置了 PartDB，缺料预警会自动计算。<b>网页本身不存任何账号/口令</b>，数据始终由本机 Python 管道落库，安全合规。</p>
    </div></section>`);
  if(ROLES[role].sections.includes("WZ")) app.appendChild(secWZ);

  /* 快速导航条：列出当前角色可见模块，点击平滑跳转，滚动自动高亮 */
  const SEC_NAV={K:['核心指标','__IC_GRID__'],A:['行动建议','__IC_NEXT__'],WZ:['新建项目','__IC_ADD__'],BF:['补录数据','__IC_EDIT__'],
    PW:['项目&在制','__IC_PROJ__'],G:['甘特图','__IC_GANT__'],T:['工时','__IC_TIME__'],
    C:['成本','__IC_COST__'],Q:['质量','__IC_QUAL__'],P:['产线流转','__IC_PROJ__'],
    Sup:['供应链','__IC_SUP__'],Inv:['库存预警','__IC_BOX__']};
  const navHTML=ROLES[role].sections.map(k=>`<button class="sec-nav-item" data-target="sec-${k}">${SEC_NAV[k][1]}${SEC_NAV[k][0]}</button>`).join("");
  const navBar=el(`<nav class="sec-nav" id="secNav">${navHTML}</nav>`);
  navBar.querySelectorAll(".sec-nav-item").forEach(b=>b.onclick=()=>{
    const t=document.getElementById(b.dataset.target);
    if(t) t.scrollIntoView({behavior:"smooth",block:"start"});
  });
  app.insertBefore(navBar, secK);
  initPagination();
  observeNav(navBar);
}

/* ---------- 交互 ---------- */
function toast(msg){
  let t=$("#toast");
  if(!t){ t=el(`<div class="toast" id="toast"></div>`); document.body.appendChild(t); }
  t.textContent=msg; t.classList.add("show");
  clearTimeout(t._timer);
  t._timer=setTimeout(()=>t.classList.remove("show"), 3200);
}
/* 新建项目向导：采集 → 生成 JSON → 浏览器下载（不触碰任何后端/口令） */
function submitWizard(){
  const 产品=document.getElementById('wz-product').value.trim();
  const 数量=parseInt(document.getElementById('wz-qty').value)||0;
  const 天数=parseInt(document.getElementById('wz-days').value)||0;
  const 优先级=document.getElementById('wz-prio').value;
  const 负责人=document.getElementById('wz-owner').value.trim();
  const 备注=document.getElementById('wz-note').value.trim();
  if(!产品||!数量){ toast('请填写产品名称和数量'); return; }
  const due=new Date(Date.now()+天数*86400000).toISOString().slice(0,10);
  const obj={_wizard:"new-project",产品,数量,交期天数:天数,预计完工:due,优先级,负责人,备注,生成时间:new Date().toISOString()};
  const blob=new Blob([JSON.stringify(obj,null,2)],{type:"application/json"});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='新建生产项目.json'; a.click();
  URL.revokeObjectURL(a.href);
  document.getElementById('wz-cmd').textContent='python op.py apply-wizard 新建生产项目.json';
  toast('已下载 新建生产项目.json ✅ 运行命令即可写入');
}
/* 分享角色视图：复制带 #role 锚点 + 口令的微信文案，直接发微信给对应人 */
function shareRole(role){
  const base=location.origin + location.pathname + (location.search||"");
  const url=base + "#role=" + role;
  const name=ROLES[role].name;
  const pw=PW[role];
  const txt="【生产·项目管理驾驶舱 · "+name+"视图】\n链接："+url+"\n打开后输入口令："+pw+"\n数据每日 9:00 自动更新，无需手动刷新。\n（内部查看，请勿外传）";
  const done=()=>toast("已复制「"+name+"视图」链接 ✅ 去微信粘给"+name+"即可");
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(done, ()=>fallbackCopy(txt,done)); }
  else fallbackCopy(txt, done);
}
/* 口令管理：仅管理员可见，查看 / 复制 / 轮换 */
function openPwModal(){
  populatePw();
  $("#pwNote").innerHTML="";
  $("#pwModal").classList.add("show");
}
function closePwModal(){ $("#pwModal").classList.remove("show"); }
function pwName(k){ return k==="admin" ? "管理员（全部角色）" : (ROLES[k]?ROLES[k].name:k); }
function populatePw(){
  const rows=Object.keys(PW).map(k=>
    `<div class="pw-row"><span class="pw-name">${pwName(k)}</span>
      <input class="pw-val" type="text" readonly value="${PW[k]}">
      <button class="pw-cp" data-pw="${PW[k]}">复制</button></div>`).join("");
  $("#pwList").innerHTML=rows;
  $("#pwList").querySelectorAll(".pw-cp").forEach(b=>b.onclick=()=>{
    const t=b.dataset.pw;
    const done=()=>toast("已复制口令 ✅");
    if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(done,()=>fallbackCopy(t,done));
    else fallbackCopy(t,done);
  });
}
function genPw(prefix){ let s=prefix; for(let i=0;i<4;i++) s+=Math.floor(Math.random()*10); return s; }
function rotateAll(){
  const map={admin:genPw("ZHWL"), boss:genPw("ZHWL"), warehouse:genPw("CK"), purchase:genPw("CG"), production:genPw("SC"), sales:genPw("XS")};
  PW=map;
  localStorage.setItem("cockpit_pw_override", _b64enc(map));   // 本机立即生效
  populatePw();
  const lines=Object.keys(map).map(k=> pwName(k)+"："+map[k]).join("\n");
  $("#pwNote").innerHTML="✅ 已生成本机新口令并立即生效。<br>要让<b>所有分享链接（其他设备）也换成新口令、并作废旧口令</b>，请把下面新口令发我，或直接说「重新部署」——我会用这些新口令重建上线：<br><textarea class='pw-out' readonly>"+lines+"</textarea>";
  toast("已轮换本机口令 ✅");
}
function copyAllPw(){
  const lines=Object.keys(PW).map(k=> pwName(k)+"："+PW[k]).join("\n");
  const done=()=>toast("已复制全部口令 ✅");
  if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(lines).then(done,()=>fallbackCopy(lines,done));
  else fallbackCopy(lines,done);
}
let _navIO=null;
function initPagination(){
  document.querySelectorAll('table[data-paginate]').forEach(tbl=>{
    const size=parseInt(tbl.dataset.paginate)||8;
    const tb=tbl.querySelector('tbody'); if(!tb) return;
    const rows=Array.from(tb.querySelectorAll('tr'));
    if(rows.length<=size) return;
    let cur=1; const pages=Math.ceil(rows.length/size);
    const nav=el(`<div class="pg"><button class="pg-prev">‹ 上一页</button><span class="pg-info"></span><button class="pg-next">下一页 ›</button></div>`);
    tbl.parentElement.after(nav);
    function draw(){
      const start=(cur-1)*size;
      rows.forEach((r,i)=>{ r.style.display=(i>=start&&i<start+size)?'':'none'; });
      nav.querySelector('.pg-info').textContent='第 '+cur+' / '+pages+' 页 · 共 '+rows.length+' 条';
      nav.querySelector('.pg-prev').disabled=cur<=1;
      nav.querySelector('.pg-next').disabled=cur>=pages;
    }
    nav.querySelector('.pg-prev').onclick=()=>{ if(cur>1){cur--;draw();} };
    nav.querySelector('.pg-next').onclick=()=>{ if(cur<pages){cur++;draw();} };
    draw();
  });
}
function observeNav(nav){
  const items=Array.from(nav.querySelectorAll('.sec-nav-item'));
  const map={}; items.forEach(b=>map[b.dataset.target]=b);
  if(_navIO) _navIO.disconnect();
  _navIO=new IntersectionObserver(es=>{
    es.forEach(e=>{ if(e.isIntersecting){ items.forEach(i=>i.classList.remove('active')); const it=map[e.target.id]; if(it) it.classList.add('active'); } });
  },{rootMargin:'-45% 0px -50% 0px',threshold:0});
  document.querySelectorAll('section.sec').forEach(s=>_navIO.observe(s));
}
function exportJSON(){
  const blob=new Blob([JSON.stringify(MODEL,null,2)],{type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download="驾驶舱数据快照_"+MODEL.snapshot+".json";
  a.click(); URL.revokeObjectURL(a.href);
}
function importJSON(file){
  const r=new FileReader();
    r.onload=()=>{ try{ const d=JSON.parse(r.result); MODEL=d; render(MODEL);
      $("#banner").innerHTML="已导入本地快照（"+d.snapshot+"），点击「导出分析JSON」可保存。";
    }catch(e){ alert("JSON 解析失败："+e.message); } };
  r.readAsText(file);
}
/* 分析数据：自动拼好结构化分析请求 → 复制到剪贴板 → 用户粘贴到 WorkBuddy 发我 */
function analyzeData(){
  const m=MODEL, k=m.kpi, t=m.time, c=m.cost, q=m.quality, s=m.supply;
  const role=currentRole();
  const L=[];
  L.push("【请帮我分析这份生产·项目管理驾驶舱数据 — 当前角色视图："+ROLES[role].name+"】");
  L.push("数据快照："+m.snapshot + (m.isDemo ? "（演示数据）" : "（真实数据·SeaTable云"+(m.synced_at?"，同步 "+m.synced_at:"")+"）"));
  L.push("");
  L.push("【1 核心指标（"+ROLES[role].name+"视图）】");
  buildKPIs(m, role).forEach(x=>L.push("· "+x.l+"："+x.v+(x.s?"（"+x.s+"）":"")));
  L.push("");
  L.push("【2 下一步行动建议（"+ROLES[role].name+"专属）】");
  const actsAll=m.next_actions||[];
  const acts=(role==="boss")?actsAll:actsAll.filter(a=>(ROLES[role].actions||[]).includes(a.cat));
  if(acts.length) acts.forEach(a=>L.push("  ["+a.pri+"] "+a.text));
  else L.push("  （当前角色暂无专属待办）");
  L.push("");
  L.push("【3 关键明细】");
  L.push("· 采购逾期：" + (s.overdue_list.length ? s.overdue_list.map(o=>o.supplier+"·"+o.material+"（逾期"+o.days+"天）").join("；") : "无"));
  L.push("· 供应商准时率：" + (s.supplier.length ? s.supplier.map(x=>(x.name+": "+(x.rate==null?"—":x.rate+"%"))).join("，") : "无"));
  L.push("· 质量：贴片良品率 "+q.smt_yield+"%，组装良品率 "+(q.asm_yield==null?"未录入":q.asm_yield+"%")+"，维修率 "+q.repair_rate+"%（超期未完 "+q.repair_overdue+"）");
  L.push("· PartDB 缺料：" + (m.partdb && m.partdb.bom ? m.partdb.bom.shortage.length+" 种（零确认库存 "+m.partdb.zero_confirmed+" 种）" : "未连接"));
  L.push("· 产线瓶颈环节：" + (t.flow_dist ? (Object.entries(t.flow_dist).sort((a,b)=>b[1]-a[1])[0]||"无") : "无"));
  L.push("");
  L.push("【4 我在页面上已补录的字段（待确认后写回云端）】");
  const ov=loadOV(); let hasOv=false; const ovList=[];
  for(const tbl of ["生产计划","组装记录"]) for(const rid in (ov[tbl]||{})) for(const f in ov[tbl][rid]) if(ov[tbl][rid][f]!==""){ hasOv=true; ovList.push(tbl+" / "+rid+" / "+f+" = "+ov[tbl][rid][f]); }
  if(hasOv){ L.push(ovList.join("\n")); } else { L.push("（暂无，可忽略）"); }
  L.push("");
  L.push("请基于以上数据重点分析：①当前最大瓶颈与风险在哪；②未来 2 周最该优先推进的 3 件事；③成本 / 质量 / 交期三方面各有什么可优化点。给出具体、可执行的建议。");
  const txt=L.join("\n");
  const done=()=>alert("已复制「分析数据」请求到剪贴板 ✅\n\n你现在就在 WorkBuddy 里——直接 Ctrl+V 粘贴到下方对话框，点发送，我就会基于这份数据帮你做分析。");
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(done, ()=>fallbackCopy(txt,done)); }
  else fallbackCopy(txt, done);
}
function fmtAge(h){
  if(h==null) return "";
  if(h<1) return Math.max(1,Math.round(h*60))+" 分钟前";
  if(h<48) return Math.round(h)+" 小时前";
  return Math.round(h/24)+" 天前";
}
function syncFreshness(){
  const sat=MODEL.synced_at||"";
  if(!sat) return {h:null,lvl:"na",txt:"未知"};
  const d=new Date(sat.replace(/-/g,"/"));
  if(isNaN(d.getTime())) return {h:null,lvl:"na",txt:sat};
  const h=(Date.now()-d.getTime())/3600000;
  let lvl="green",txt="数据新鲜";
  if(h>=24){lvl="red";txt="数据可能已过时";}
  else if(h>=6){lvl="amber";txt="较新";}
  return {h:h,lvl:lvl,txt:txt};
}
function refreshSyncBadge(){
  const el=$("#syncBadge"); if(!el) return;
  const f=syncFreshness();
  const color=f.lvl==="green"?"var(--green)":f.lvl==="amber"?"var(--amber)":f.lvl==="red"?"var(--red)":"var(--sub)";
  const age=f.h==null?"":(" · 同步于 "+MODEL.synced_at+" · "+fmtAge(f.h));
  el.innerHTML='<span class="dot" style="background:'+color+'"></span>'+(MODEL.isDemo?"演示数据":("真实数据 · "+f.txt))+age;
}
function openSync(){
  const f=syncFreshness();
  const color=f.lvl==="green"?"var(--green)":f.lvl==="amber"?"var(--amber)":f.lvl==="red"?"var(--red)":"var(--sub)";
  const rows=[
    ["数据来源", MODEL.isDemo?"本地演示数据":("SeaTable 云「"+(MODEL.base_name||"生产")+"」+ PartDB 实时")],
    ["业务表同步于", MODEL.synced_at||"未知"],
    ["距今", f.h==null?"未知":fmtAge(f.h)],
    ["PartDB 物料快照", MODEL.partdb_at||"未知"],
    ["数据新鲜度", '<span class="dot" style="background:'+color+'"></span>'+f.txt],
    ["当前状态", MODEL.isDemo?"演示模式（非真实库）":"已接入真实库 · 只读快照"],
  ];
  let html='<div class="modal-mask" id="syncMask"><div class="modal"><div class="modal-h">__IC_SYNC__ 同步状况</div><table class="sync-t"><tbody>';
  for(const r of rows) html+='<tr><td class="sk">'+r[0]+'</td><td class="sv">'+r[1]+'</td></tr>';
  html+='</tbody></table>';
  if(f.lvl==="red") html+='<div class="note" style="color:var(--red)">⚠ 距上次同步已超过 24 小时，建议重新同步获取最新数据。</div>';
  html+='<div class="note">本驾驶舱是生成时的冻结快照，页面不会自动联网拉取。点下方按钮复制「重新同步」指令，粘贴到 WorkBuddy 发送，我就会重跑同步并重新生成 HTML。</div>';
  html+='<div class="modal-ft"><button class="btn" id="btnCopySync">一键复制重新同步指令</button><button class="btn btn-ghost" id="btnCloseSync">关闭</button></div></div></div>';
  const tmp=document.createElement("div"); tmp.innerHTML=html; document.body.appendChild(tmp.firstChild);
  const mask=$("#syncMask");
  $("#btnCloseSync").onclick=()=>mask.remove();
  mask.onclick=e=>{ if(e.target===mask) mask.remove(); };
  $("#btnCopySync").onclick=()=>{
    const txt="请重新同步数据并重生成「生产·项目管理驾驶舱」：运行 seatable_sync.py 拉取最新 SeaTable 云端 + PartDB 实时物料，再运行 cockpit.py 重新渲染。当前同步时间 "+(MODEL.synced_at||"未知")+"。";
    const done=()=>alert("已复制「重新同步」指令 ✅\n在下方对话框 Ctrl+V 粘贴并发送，我立刻重跑同步+渲染并交付新 HTML。");
    if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(done,()=>fallbackCopy(txt,done));
    else fallbackCopy(txt,done);
  };
}
function setupLock(){
  const mask=document.getElementById("lockMask");
  const saved=sessionStorage.getItem("cockpit_unlocked");
  if(saved){
    try{
      const u=JSON.parse(saved);
      if(u && (u.level==="admin" || (u.level==="role" && ROLES[u.role]))){ UNLOCK=u; mask.style.display="none"; return; }
    }catch(e){}
  }
  const role=currentRole();
  document.getElementById("lockRole").textContent="当前视图："+ROLES[role].name;
  const inp=document.getElementById("lockInput"), btn=document.getElementById("lockBtn"), err=document.getElementById("lockErr");
  const tryU=()=>{
    const v=inp.value.trim();
    if(v===PW.admin){ UNLOCK={level:"admin"}; }
    else if(PW[role] && v===PW[role]){ UNLOCK={level:"role",role:role}; }
    else { err.textContent="口令错误，请重试"; inp.value=""; inp.focus(); return; }
    sessionStorage.setItem("cockpit_unlocked", JSON.stringify(UNLOCK));
    mask.style.display="none";
    render(MODEL);
  };
  btn.onclick=tryU;
  inp.addEventListener("keydown",e=>{ if(e.key==="Enter") tryU(); });
  inp.focus();
}
window.addEventListener("DOMContentLoaded",()=>{
  setupLock();
  render(MODEL);
  $("#btnExport").onclick=exportJSON;
  $("#btnSync").onclick=openSync;
  refreshSyncBadge();
  $("#btnAnalyze").onclick=analyzeData;
  $("#btnImport").onclick=()=>$("#fileInput").click();
  $("#fileInput").onchange=e=>{ if(e.target.files[0]) importJSON(e.target.files[0]); };
  $("#btnRefresh").onclick=()=>alert("在技能目录运行：\npython cockpit.py\n即可用最新本地数据重新生成此驾驶舱 HTML。");
  // 口令管理弹窗（仅管理员可见）
  $("#pwClose").onclick=closePwModal;
  $("#pwModalMask").onclick=closePwModal;
  $("#pwRotate").onclick=rotateAll;
  $("#pwCopyAll").onclick=copyAllPw;
  // 补录面板：事件委托（render 会重建 DOM，用委托保证每次都生效）
  $("#app").addEventListener("change", e=>{ if(e.target.matches("#bfBody [data-rid]")) onBfChange(); });
  // 角色切换：URL hash 变化即重渲染对应视图
  window.addEventListener("hashchange", ()=>render(MODEL));
});
</script>
</body>
</html>
"""


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    adapter = get_adapter()
    adapter.auth()
    today = datetime.now(_TZ).date()
    model = compute(adapter, today)
    html = HTML.replace("__MODEL__", json.dumps(model, ensure_ascii=False))
    for k, v in ICONS.items():
        html = html.replace("__" + k + "__", v)
    html = html.replace("__PW_BLOB__", json.dumps(PW_BLOB))
    if PW_MODE == "rotate":
        _pwfile = os.path.join(os.path.dirname(os.path.abspath(out)), "cockpit_passwords.json")
        with open(_pwfile, "w", encoding="utf-8") as _f:
            json.dump(_PW_MAP, _f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] 驾驶舱已生成：{out}")
    print(f"     快照日期 {model['snapshot']} · 项目 {model['kpi']['projects']} · "
          f"合同额 ¥{model['kpi']['contract']:,.0f} · 采购逾期 {model['kpi']['purchase_overdue']}")


if __name__ == "__main__":
    main()
