#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workflow.py — 替身闭环 v2 统一工作流入口（Phase 1）。

每日 9 点自动化从「Prompt 记 13 步」改为「跑一条命令」：

  python workflow.py daily --mode preview   # 演练：online_write 全被拦截
  python workflow.py daily --mode apply     # 真实执行
  python workflow.py daily --resume <id>    # 断点续跑（成功步骤跳过）
  python workflow.py status <run_id>        # 查看历史运行结果

输出：data/runs/<run_id>/final.json —— AI 播报与人工核对只读这个文件。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from application import contracts as C   # noqa: E402
from workflows.daily_refresh import WORKFLOWS, run_workflow  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "data", "runs")


def _ctx(args) -> C.RunContext:
    return C.RunContext(
        run_id=args.run_id or C.new_run_id(args.workflow),
        workflow=args.workflow,
        actor=args.actor,
        mode=args.mode,
        yes=args.yes,
        skill_dir=HERE,
        resume_of=args.resume,
    )


def cmd_run(args):
    ctx = _ctx(args)
    print("▶ 运行 %s  run_id=%s  mode=%s%s" % (
        ctx.workflow, ctx.run_id, ctx.mode,
        "  (resume of %s)" % ctx.resume_of if ctx.resume_of else ""))
    rr = run_workflow(args.workflow, ctx)
    final_path = os.path.join(RUNS_DIR, ctx.run_id, "final.json")
    print("\n═══ 运行结果 ═══")
    for st in rr.steps:
        mark = {"success": "✓", "skipped": "⊘", "failed": "✗", "blocked": "⛔"}.get(
            st["status"], "?")
        print("  %s %-16s %s" % (mark, st["step_id"], st.get("error") or ""))
    print("  合计：%s" % json.dumps(rr.summary, ensure_ascii=False))
    print("  结果文件：%s" % final_path)
    return 0 if rr.status == C.STATUS_SUCCESS else 1


def cmd_status(args):
    path = os.path.join(RUNS_DIR, args.run_id, "final.json")
    if not os.path.exists(path):
        print("（找不到运行记录 %s）" % args.run_id)
        return 1
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    print("run_id=%s workflow=%s status=%s" % (data["run_id"], data["workflow"], data["status"]))
    print("时间：%s → %s" % (data.get("started_at"), data.get("finished_at")))
    for st in data["steps"]:
        print("  %-16s %-8s %s" % (st["step_id"], st["status"], st.get("error") or ""))
    print("合计：%s" % json.dumps(data.get("summary", {}), ensure_ascii=False))
    return 0


def cmd_list(_args):
    if not os.path.isdir(RUNS_DIR):
        print("（暂无运行记录）")
        return 0
    for rid in sorted(os.listdir(RUNS_DIR)):
        path = os.path.join(RUNS_DIR, rid, "final.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        print("%-28s %-8s %s" % (rid, d.get("status"), d.get("summary", {})))
    return 0


# ── verify：运行后验收（周一实战核对用）──────────────────
# 设计原则：验收 = 对照「工作流自己声称的」与「磁盘上真实存在的」。
# 每项检查独立打分，任何一项失败都以非零退出码结束，可接 CI。

VERIFY_ITEMS = {
    # 步骤 -> 该步必须在 data/ 下留下的产物
    "seatable_sync":  ["data/项目.csv", "data/生产计划.csv"],
    "partdb_sync":    [],
    "wechat_pull":    ["data/wechat_intake/latest.json"],
    "wechat_summary": ["data/wechat_intake/summary_24h.md"],
    "alerts":         ["data/alerts.json"],
    "foresee":        ["data/foresee.json"],
    "daily_brief":    ["data/daily_brief_short.md"],
    "cockpit":        ["项目管理驾驶舱.html"],
    # wxmatch_scan -> data/核对结果.csv（有微信消息才有；空也生成）
    "wxmatch_scan":   ["data/核对结果.csv"],
    # foresee_review 可能无新复盘，不强制产物
}


def _fresh_enough(path: str, run_date: str, max_age_days: int = 1) -> bool:
    """产物 mtime 不早于 run 日期前 max_age_days 天（防止拿旧文件充数）。"""
    import datetime
    try:
        mtime = datetime.date.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return False
    return mtime >= (datetime.date.fromisoformat(run_date)
                     - datetime.timedelta(days=max_age_days))


def cmd_verify(args):
    run_id = args.run_id
    if run_id == "latest":
        run_id = _latest_run_id()
        if not run_id:
            print("（暂无运行记录）先跑 workflow.py run daily 再验收。")
            return 1
        print("（最新运行：%s）" % run_id)
    final_path = os.path.join(RUNS_DIR, run_id, "final.json")
    if not os.path.exists(final_path):
        print("（找不到运行记录 %s）先跑 workflow.py run daily 再验收。" % run_id)
        return 1
    with open(final_path, encoding="utf-8") as f:
        data = json.load(f)
    run_date = (data.get("finished_at") or data.get("started_at") or "")[:10]
    if not run_date:   # 极端情况：时间字段缺失，退化为不校验新鲜度
        run_date = "9999-12-31"

    failures = []
    print("═══ 验收 %s（%s）═══" % (run_id, run_date))
    print("\n[1] 步骤状态")
    for st in data.get("steps", []):
        mark = {"success": "✓", "skipped": "⊘", "failed": "✗", "blocked": "⛔"}.get(
            st["status"], "?")
        print("  %s %-16s %s" % (mark, st["step_id"], st.get("error") or ""))
        if st["status"] in ("failed", "blocked"):
            failures.append("步骤 %s: %s" % (st["step_id"], st.get("error") or st["status"]))

    print("\n[2] 产物核对（成功步骤必须有新鲜产物）")
    step_status = {st["step_id"]: st["status"] for st in data.get("steps", [])}
    for step_id, artifacts in VERIFY_ITEMS.items():
        if step_status.get(step_id) != "success":
            continue   # 没跑/没成功的步骤不核产物（failures[1] 已记）
        for rel in artifacts:
            path = os.path.join(HERE, rel)
            if not os.path.exists(path):
                failures.append("产物缺失：%s（步骤 %s 声称成功）" % (rel, step_id))
                print("  ✗ %-16s 缺 %s" % (step_id, rel))
            elif not _fresh_enough(path, run_date):
                failures.append("产物过期：%s（mtime 早于运行日期，疑似未刷新）" % rel)
                print("  ✗ %-16s %s 是旧文件" % (step_id, rel))
            else:
                print("  ✓ %-16s %s" % (step_id, rel))

    print("\n[3] 台账与安全")
    # 3a. CRM 台账幂等键列已迁移（存在且表头含幂等键）
    ledger = os.path.join(HERE, "data", "crm_dispatch_ledger.csv")
    if os.path.exists(ledger):
        with open(ledger, encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if "幂等键" in header:
            print("  ✓ CRM 台账已含幂等键列")
        else:
            # 旧格式还没被任何一次写入迁移 —— 提示不算失败（下次写入自动迁移）
            print("  ⊘ CRM 台账仍是旧格式（下次写入会自动补列）")
    else:
        print("  ⊘ CRM 台账不存在（尚无自动写入，正常）")
    # 3b. final.json 不含口令明文（安全验收：播报源里不允许出现）
    pw_leak = _scan_password_leak(final_path)
    if pw_leak:
        failures.append("final.json 疑似口令泄露：%s" % pw_leak)
        print("  ✗ final.json 出现口令字段：%s" % pw_leak)
    else:
        print("  ✓ final.json 无口令明文")

    print("\n═══ 结论 ═══")
    if failures:
        print("验收未通过，%d 项问题：" % len(failures))
        for f_ in failures:
            print("  · %s" % f_)
        return 1
    print("验收通过：步骤全绿、产物齐全且新鲜、无安全违规。")
    print("下一步：对照播报文本与 final.json 是否一致（人工抽查 2-3 项数字）。")
    return 0


def _scan_password_leak(path: str) -> str:
    """检查 final.json 文本里是否出现口令字段名（值本身在 config.yaml，不在 runs 里）。"""
    import re
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for pat in ("admin_password", "role_passwords", "cockpit_password"):
        if re.search(re.escape(pat), text):
            return pat
    return ""


def _latest_run_id() -> str:
    """按目录名排序取最新一次有 final.json 的 run（含时间戳，字典序即时间序）。"""
    if not os.path.isdir(RUNS_DIR):
        return ""
    best = ""
    for rid in sorted(os.listdir(RUNS_DIR)):
        if os.path.exists(os.path.join(RUNS_DIR, rid, "final.json")):
            best = rid
    return best


def main():
    ap = argparse.ArgumentParser(description="替身闭环 v2 工作流入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="执行工作流")
    p.add_argument("workflow", choices=sorted(WORKFLOWS))
    p.add_argument("--mode", default=C.MODE_PREVIEW, choices=[C.MODE_PREVIEW, C.MODE_APPLY])
    p.add_argument("--yes", action="store_true", help="显式确认（destructive/approval 必需）")
    p.add_argument("--actor", default="automation")
    p.add_argument("--run-id", default=None, help="指定 run_id（默认自动生成）")
    p.add_argument("--resume", default=None, help="从历史 run 续跑（成功步骤跳过）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="查看一次运行的结果")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="列出历史运行")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("verify", help="验收一次运行：步骤状态 + 产物新鲜度 + 安全检查")
    p.add_argument("run_id", help="要验收的 run_id（最新一次可用 latest）")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
