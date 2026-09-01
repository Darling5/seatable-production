#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alerts.py — 异常检测引擎（第二大脑的神经：让数据主动喊，而不是等你来看）。

规则（纯查询+阈值，不搞 AI 玄学）：
  A1 交期逼近未完货   进行中项目，合同交期距今 ≤ 7 天且完货日期为空
  A2 项目已超期       进行中项目，合同交期已过且完货日期为空（超期天数）
  A3 逾期应收         已交付/进行中项目，待收 > 0 且合同交期已过
  A4 采购下单未到货   已下单的采购记录（IC/外壳/PCBA/组装料/成品/PCB）逐条列出
  A5 计划停滞         生产计划「计划中」且剩余时间 < -14（两周以上无推进）
  A6 行情异动         物料行情记录中涨跌幅超阈值（默认 ±10%）或生命周期 NRND/EOL
  A7 数据体检         状态为空的项目/计划、#VALUE! 脏值（提示补录）

命令：
  python alerts.py run [--json]     # 跑全部规则，打印报告并写 data/alerts.json
  python alerts.py show             # 只读上次结果
  python alerts.py rules            # 列出规则清单

输出 data/alerts.json 供 daily_brief.py / 驾驶舱 / 自动化消费。
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, date

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "alerts.json")

sys.path.insert(0, HERE)
from adapters.factory import load_config  # noqa: E402

PURCHASE_TABLES = ["IC采购记录", "外壳采购记录", "PCBA半成品采购记录",
                   "组装料采购记录", "成品采购记录", "PCB下单记录"]


def _read(name):
    p = os.path.join(DATA, name + ".csv")
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return None


def _days(s):
    """'2026-08-21' → 距今天数（正=未来，负=已过去）。"""
    if not s:
        return None
    try:
        d = datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
        return (d - date.today()).days
    except ValueError:
        return None


def _is_active(status):
    return (status or "").strip() not in ("已交付", "已终止", "已取消")


def _rule_deadline(projects):
    hits = []
    for r in projects:
        if not _is_active(r.get("状态")):
            continue
        if r.get("完货日期"):
            continue
        dd = _days(r.get("合同交期"))
        if dd is None:
            continue
        if dd < 0:
            hits.append({"level": "高", "rule": "A2 项目已超期",
                         "target": "%s %s" % (r.get("项目编号"), r.get("项目", "")),
                         "detail": "合同交期 %s 已超 %d 天，仍未完货" % (r.get("合同交期"), -dd),
                         "action": "确认延期沟通结果，更新合同交期或推动交付"})
        elif dd <= 7:
            hits.append({"level": "中", "rule": "A1 交期逼近",
                         "target": "%s %s" % (r.get("项目编号"), r.get("项目", "")),
                         "detail": "合同交期 %s 仅剩 %d 天，未完货" % (r.get("合同交期"), dd),
                         "action": "核对生产计划阶段，确认能否按期交付"})
    return hits


def _rule_receivable(projects):
    hits = []
    for r in projects:
        due = _num(r.get("待收"))
        if not due or due <= 0:
            continue
        dd = _days(r.get("合同交期"))
        overdue = dd is not None and dd < 0
        status = (r.get("状态") or "").strip()
        if overdue:
            hits.append({"level": "高" if status == "已交付" else "中", "rule": "A3 逾期应收",
                         "target": "%s %s" % (r.get("项目编号"), r.get("项目", "")),
                         "detail": "待收 ¥%s，交期 %s 已过 %d 天（状态：%s）"
                                   % (f"{due:,.0f}", r.get("合同交期"), -dd, status or "未填"),
                         "action": "催收尾款；已交付逾期货款优先处理"})
    return hits


def _rule_purchase_pending():
    hits = []
    for t in PURCHASE_TABLES:
        for r in _read(t):
            status = (r.get("状态") or "").strip()
            if not status or "到货" in status or status in ("已取消", "未下单"):
                continue
            plan = (r.get("生产计划") or "").strip()
            code = r.get(t.replace("记录", "").replace("下单", "下单") + "编号") \
                or r.get("__row_id__", "")[:8]
            hits.append({"level": "中", "rule": "A4 采购在途",
                         "target": "%s %s" % (t, code),
                         "detail": "状态「%s」交期%s，关联：%s"
                                   % (status, r.get("交期") or "未填", plan or "（未关联计划）"),
                         "action": "跟踪到货；长期未到货要催供应商"})
    # 按计划聚合视图在 brief 里做；这里逐条
    return hits


def _rule_stalled_plans():
    hits = []
    for r in _read("生产计划"):
        if not _is_active(r.get("状态")):
            continue
        left = _num(r.get("剩余时间"))
        if left is None or left >= -14:
            continue
        hits.append({"level": "中", "rule": "A5 计划停滞",
                     "target": "%s %s" % (r.get("生产计划编号"), r.get("生产产品", "")),
                     "detail": "剩余 %d 天（已超 2 周无推进），阶段「%s」"
                               % (int(left), r.get("阶段") or "未填"),
                     "action": "确认卡点（缺料/等确认/等客户），更新阶段或调整计划"})
    return hits


def _rule_market():
    hits = []
    cfg = (load_config() or {}).get("market") or {}
    th = float(cfg.get("alert_threshold") or 10)
    for r in _read("物料行情记录"):
        model = (r.get("物料型号") or "").strip()
        chg = _num(r.get("涨跌幅"))
        life = (r.get("生命周期") or "").strip()
        if chg is not None and abs(chg) >= th:
            hits.append({"level": "高" if abs(chg) >= th * 2 else "中", "rule": "A6 行情异动",
                         "target": model,
                         "detail": "%s %s%%（%s，%s）" % (
                             "涨" if chg > 0 else "跌", chg, r.get("渠道") or "渠道未填",
                             r.get("日期", "")),
                         "action": "评估在途/未来采购是否锁价或提前下单"})
        elif life in ("NRND", "EOL"):
            hits.append({"level": "高", "rule": "A6 停产风险",
                         "target": model,
                         "detail": "生命周期 %s（%s）" % (life, r.get("来源链接") or "无来源链接"),
                         "action": "确认最后一次采购量是否覆盖项目剩余需求，考虑替代料"})
    return hits


def _rule_data_health():
    hits = []
    for r in _read("项目"):
        if not (r.get("状态") or "").strip() and _days(r.get("签订日期")) is not None:
            hits.append({"level": "低", "rule": "A7 数据体检",
                         "target": "项目 %s" % r.get("项目编号", ""),
                         "detail": "状态为空", "action": "补录状态"})
    for r in _read("生产计划"):
        if "#VALUE!" in (r.get("剩余时间") or ""):
            hits.append({"level": "低", "rule": "A7 数据体检",
                         "target": "生产计划 %s" % r.get("生产计划编号", ""),
                         "detail": "剩余时间公式错误（#VALUE!），交货时间未填",
                         "action": "补填交货时间"})
    return hits


RULES = [
    ("A1 交期逼近未完货", _rule_deadline),
    ("A3 逾期应收", _rule_receivable),
    ("A4 采购在途", _rule_purchase_pending),
    ("A5 计划停滞", _rule_stalled_plans),
    ("A6 行情/停产", _rule_market),
    ("A7 数据体检", _rule_data_health),
]


def run(as_json=False):
    projects = _read("项目")
    hits = []
    for name, fn in RULES:
        try:
            r = fn(projects) if name != "A4 采购在途" and name != "A6 行情/停产" and name != "A5 计划停滞" and name != "A7 数据体检" else fn()
            hits.extend(r)
        except Exception as e:
            hits.append({"level": "低", "rule": "A0 引擎异常", "target": name,
                         "detail": str(e)[:120], "action": "检查数据格式"})
    order = {"高": 0, "中": 1, "低": 2}
    hits.sort(key=lambda h: order.get(h["level"], 3))
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {"高": sum(1 for h in hits if h["level"] == "高"),
                   "中": sum(1 for h in hits if h["level"] == "中"),
                   "低": sum(1 for h in hits if h["level"] == "低")},
        "alerts": hits,
    }
    os.makedirs(DATA, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return result
    c = result["counts"]
    print("== 异常检测报告 %s ==" % result["generated_at"])
    print("    高 %d · 中 %d · 低 %d（已写 data/alerts.json）"
          % (c["高"], c["中"], c["低"]))
    for h in hits:
        print("  [%s] %s · %s" % (h["level"], h["rule"], h["target"]))
        print("        %s" % h["detail"])
    return result


def show():
    if not os.path.exists(OUT):
        print("（还没有结果，先跑 python alerts.py run）")
        return
    r = json.load(open(OUT, encoding="utf-8"))
    c = r["counts"]
    print("== 异常检测报告 %s ==" % r["generated_at"])
    print("    高 %d · 中 %d · 低 %d" % (c["高"], c["中"], c["低"]))
    for h in r["alerts"]:
        print("  [%s] %s · %s" % (h["level"], h["rule"], h["target"]))
        print("        %s → %s" % (h["detail"], h["action"]))


def main():
    ap = argparse.ArgumentParser(description="异常检测引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--json", action="store_true")
    sub.add_parser("show")
    sub.add_parser("rules")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a.json)
    elif a.cmd == "show":
        show()
    else:
        for name, _ in RULES:
            print(" ·", name)


if __name__ == "__main__":
    main()
