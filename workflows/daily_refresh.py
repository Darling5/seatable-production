# -*- coding: utf-8 -*-
"""workflows/daily_refresh.py — 每日 9 点主流程（Phase 1：subprocess 包装旧脚本）。

把 automations/README.md 每日 Prompt 的 13 步收进可恢复 DAG。
分工边界（avatar-loop-v2 §4）：
  - 代码确定性步骤（本模块）：同步、取数、核对、预测、摘要、驾驶舱；
  - AI 语义步骤（留给自动化 Prompt）：微信候选分流、AI 总结、CRM 识别写入、播报。
    Prompt 只准「启动本工作流 + 读 final.json 播报」，不再自行编排脚本顺序。

用法（经根目录 workflow.py）：
  python workflow.py daily --mode preview   # 只读检查（online_write 全被拦）
  python workflow.py daily --mode apply     # 真实执行（每日自动化用这个）
  python workflow.py daily --mode apply --resume <run_id>   # 断点续跑
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import contracts as C          # noqa: E402
from application.runner import make_step        # noqa: E402

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PY = sys.executable or "python"


def build_steps() -> list[C.StepSpec]:
    """每日刷新 DAG。副作用声明严格对应旧脚本真实行为。"""
    def s(step_id, name, script_args, **kw):
        return make_step([_PY, os.path.join(_HERE, script_args[0])] + list(script_args[1:]),
                         step_id, name, **kw)

    return [
        # ── 1. 数据同步（失败即中止：后面的计算全是旧数据，跑了也白跑）──
        s("seatable_sync", "同步生产业务 Base",
          ["seatable_sync.py"], side_effect=C.SIDE_LOCAL_APPEND,
          failure_policy="abort", retry=1),
        s("partdb_sync", "同步 PartDB 库存",
          ["partdb_sync.py"], side_effect=C.SIDE_LOCAL_APPEND,
          failure_policy="abort", retry=1),

        # ── 2. 微信情报（pull 增量 + summary 回溯；AI 总结与分流留给 Prompt）──
        s("wechat_pull", "微信事件增量拉取",
          ["wechat_intake.py", "pull"], side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync",), failure_policy="continue", retry=1),
        s("wechat_summary", "微信 24h 摘要取数",
          ["wechat_intake.py", "summary", "--hours", "24",
           "--out", os.path.join("data", "wechat_intake", "summary_24h.md")],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("wechat_pull",), failure_policy="continue"),
        # 注意：ai_summary（AI 写）与 wx_dispatch 分流不在此列——那是语义步骤，
        # 由自动化 AI 读 summary_24h.md 完成，产物落 ai_summary_24h.md。

        # ── 3. 核对与预测（只读核对 + 本地快照）──
        s("wxmatch_scan", "消息↔业务表核对（只读）",
          ["wxmatch.py", "scan"], side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("wechat_pull",), failure_policy="continue"),
        s("alerts", "异常检测",
          ["alerts.py", "run"], side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync", "partdb_sync"), failure_policy="continue"),
        s("foresee", "风险预测重算",
          ["foresee.py"], side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync", "partdb_sync"), failure_policy="continue"),
        s("foresee_review", "预测复盘（台账对照）",
          ["foresee.py", "review"], side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("foresee",), failure_policy="continue"),

        # ── 4. 摘要与驾驶舱（生成）──
        s("daily_brief", "站会摘要 + 发件箱",
          ["daily_brief.py", "--push"], side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("alerts", "wechat_pull"), failure_policy="continue"),
        s("cockpit", "驾驶舱重新生成",
          ["cockpit.py"], side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync", "partdb_sync", "wechat_pull",
                      "wxmatch_scan", "foresee"),
          failure_policy="continue"),
        # 注意：发布（publish）不在本 DAG——对外链接更新属于 SIDE_PUBLISH，
        # 由自动化 AI 单独确认后执行，避免数据刷新失败牵连发布。
    ]


WORKFLOWS = {
    "daily": build_steps,
}


def run_workflow(name: str, ctx: C.RunContext) -> C.RunResult:
    from application.runner import WorkflowRunner
    if name not in WORKFLOWS:
        raise SystemExit("[错误] 未知工作流：%r（可选：%s）" % (name, "|".join(WORKFLOWS)))
    return WorkflowRunner(ctx, WORKFLOWS[name]()).run()
