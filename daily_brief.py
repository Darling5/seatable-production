#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_brief.py — 每日站会摘要（第二大脑的嘴：一条消息说清今天要管什么）。

合成四路数据成一段可直接发群的摘要：
  1) 异常检测 alerts.py 的结果（高/中分级）
  2) 微信情报待确认事件（wechat_intake 事件表）
  3) 业务面：进行中项目/计划数、昨日发货、待收总额
  4) 行情异动（涨跌/NRND/EOL）

命令：
  python daily_brief.py [--push] [--date YYYY-MM-DD]

输出：
  data/daily_brief.md           完整版（人读）
  data/daily_brief_short.md     精简版（200字内，直接发群）
  --push 时同时写通知发件箱（notify.py 消费）
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
INTAKE_DIR = os.path.join(DATA, "wechat_intake")
EVENTS_PATH = os.path.join(DATA, "微信事件.csv")

sys_path = HERE
import sys
sys.path.insert(0, sys_path)

import alerts as alerts_mod  # noqa: E402
import notify as notify_mod  # noqa: E402


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


def _crm_brief(target_date=None):
    """CRM 自动写入摘要（v1.9 承诺：播报点名，供人工核对）。

    读 data/crm_dispatch_ledger.csv，统计「今天」的自动写入：
      created_leads / add_follow / reused
    返回 (计数 dict, 明细行 list)。台账缺失或当日无写入时计数全 0。
    """
    today = target_date or datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(DATA, "crm_dispatch_ledger.csv")
    counts = {"create_lead": 0, "reused": 0, "add_follow": 0, "other": 0}
    lines = []
    if not os.path.exists(path):
        return counts, lines
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            ts = (r.get("时间") or "")[:10]
            if ts != today:
                continue
            action = (r.get("动作") or "").strip()
            if action == "create_lead":
                counts["create_lead"] += 1
            elif action == "reused" or "reused" in action:
                counts["reused"] += 1
            elif action == "add_follow":
                counts["add_follow"] += 1
            else:
                counts["other"] += 1
            summary = (r.get("内容摘要") or "").strip()
            lines.append("%s %s：%s" % (action, r.get("客户名称") or "—", summary[:60]))
    return counts, lines


def build(target_date=None):
    today = target_date or datetime.now().strftime("%Y-%m-%d")
    # 1) 异常
    alerts_mod.run()  # 现跑一次，保证新鲜
    ar = json.load(open(os.path.join(DATA, "alerts.json"), encoding="utf-8"))
    c = ar["counts"]
    high = [h for h in ar["alerts"] if h["level"] == "高"]
    mid = [h for h in ar["alerts"] if h["level"] == "中"]

    # 2) 微信待确认事件
    events = _read("微信事件")
    pending = [e for e in events if e.get("状态") == "待确认"]

    # 3) 业务面
    projects = _read("项目")
    plans = _read("生产计划")
    active_p = [p for p in projects if (p.get("状态") or "").strip() not in
                ("已交付", "已终止", "已取消", "")]
    active_plans = [p for p in plans if (p.get("状态") or "").strip() not in
                    ("已交付", "已终止", "已取消")]
    # 昨日发货
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    ships = [s for s in _read("发货清单") if (s.get("发货日期") or "").startswith(yday)]
    # 待收总额
    receivable = sum(v for v in (_num(p.get("待收")) for p in projects) if v)

    # 4) 行情异动
    market = [h for h in ar["alerts"] if h["rule"].startswith("A6")]

    # 5) CRM 自动写入（当日台账，供人工核对）
    crm_counts, crm_lines = _crm_brief(today)

    # ── 组装精简版（目标 200 字）──
    L = []
    L.append("📋 生产站会 %s" % today)
    L.append("")
    L.append("🔴 要紧事（高 %d）" % c["高"])
    for h in high[:5]:
        L.append("· %s：%s" % (h["target"][:20], h["detail"][:44]))
    if len(high) > 5:
        L.append("· …另有 %d 条见驾驶舱" % (len(high) - 5))
    if market:
        L.append("")
        L.append("📉 行情：%s" % "；".join(m["detail"][:30] for m in market[:3]))
    if pending:
        L.append("")
        L.append("💬 微信情报待确认 %d 条（说「确认 WX…」处理）" % len(pending))
        for e in pending[:3]:
            L.append("· [%s] %s" % (e.get("分类", ""), (e.get("原文") or "")[:36]))
    if any(crm_counts.values()):
        L.append("")
        L.append("🤝 CRM 自动写入：新建线索 %d · 复用 %d · 跟进 %d（台账待核对）"
                 % (crm_counts["create_lead"], crm_counts["reused"], crm_counts["add_follow"]))
        for c in crm_lines[:3]:
            L.append("· %s" % c[:56])
    L.append("")
    L.append("📊 面：%d 项目在产 · %d 计划执行 · 待收 ¥%s · 昨发 %d 批"
             % (len(active_p), len(active_plans), f"{receivable:,.0f}", len(ships)))
    if mid:
        L.append("🟡 观察（中 %d）：%s" % (c["中"],
                    "；".join(h["target"][:14] for h in mid[:4]) + ("…" if len(mid) > 4 else "")))
    short = "\n".join(L)

    # ── 完整版 ──
    F = ["# 生产站会摘要 %s\n" % today]
    F.append("## 🔴 高优先级（%d）\n" % c["高"])
    for h in high:
        F.append("- **%s**（%s）\n  %s\n  → %s" % (h["target"], h["rule"], h["detail"], h["action"]))
    F.append("\n## 🟡 观察项（%d）\n" % c["中"])
    for h in mid:
        F.append("- %s：%s" % (h["target"], h["detail"]))
    F.append("\n## 💬 微信情报待确认（%d）\n" % len(pending))
    for e in pending:
        F.append("- %s [%s] %s（%s %s）" % (e.get("事件编号", ""), e.get("分类", ""),
                                            (e.get("原文") or "")[:80],
                                            e.get("来源群", "")[:16], e.get("发送人", "")))
    F.append("\n## 📊 业务面\n")
    F.append("- 在产项目：%d · 执行中计划：%d" % (len(active_p), len(active_plans)))
    F.append("- 待收总额：¥%s" % f"{receivable:,.0f}")
    F.append("- 昨日发货：%d 批" % len(ships))
    if ships:
        for s in ships[:5]:
            F.append("  · %s %s（%s）" % (s.get("发货序号", ""), (s.get("发货内容") or "")[:40],
                                           s.get("类型", "")))
    # CRM 自动写入摘要（完整版：逐条点名，方便人工核对与撤销）
    crm_total = sum(crm_counts.values())
    F.append("\n## 🤝 CRM 自动写入（%d 条，当日台账）\n" % crm_total)
    if crm_total:
        F.append("- 新建线索：%d · 复用：%d · 跟进记录：%d"
                 % (crm_counts["create_lead"], crm_counts["reused"], crm_counts["add_follow"]))
        for c in crm_lines:
            F.append("  · %s" % c)
        F.append("- 核对：`python crm_dispatch.py ledger`；不对的单条告诉我撤销")
    else:
        F.append("- 今日暂无自动写入（周末无来单属正常）")
    low = [h for h in ar["alerts"] if h["level"] == "低"]
    if low:
        F.append("\n## 🔧 数据体检（%d，低）\n" % len(low))
        for h in low:
            F.append("- %s：%s" % (h["target"], h["detail"]))
    full = "\n".join(F)

    with open(os.path.join(DATA, "daily_brief_short.md"), "w", encoding="utf-8") as f:
        f.write(short)
    with open(os.path.join(DATA, "daily_brief.md"), "w", encoding="utf-8") as f:
        f.write(full)
    return short, full


def main():
    ap = argparse.ArgumentParser(description="每日站会摘要")
    ap.add_argument("--push", action="store_true", help="写入通知发件箱")
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    short, full = build(a.date)
    print(short)
    print("\n---\n完整版：data/daily_brief.md · 精简版：data/daily_brief_short.md")
    if a.push:
        i = notify_mod.enqueue(
            subject="📋 生产站会摘要 %s" % (a.date or datetime.now().strftime("%Y-%m-%d")),
            body=short, level="info")
        print("[ok] 已写发件箱 #%d（AI 会话经 agent-mail 发送）" % i)


if __name__ == "__main__":
    main()
