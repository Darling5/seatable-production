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

    args = ap.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
