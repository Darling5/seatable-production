# -*- coding: utf-8 -*-
"""workflows/daily_refresh.py — 每日 9 点主流程（Phase 1：subprocess 包装旧脚本）。

把 automations/README.md 每日 Prompt 的 13 步收进可恢复 DAG。
分工边界（avatar-loop-v2 §4）：
  - 代码确定性步骤（本模块）：同步、取数、核对、预测、摘要、驾驶舱；
  - AI 语义步骤（留给自动化 Prompt）：微信候选分流、AI 总结、CRM 识别写入、播报。
    Prompt 只准「启动本工作流 + 读 final.json 播报」，不再自行编排脚本顺序。

用法（经根目录 workflow.py）：
  python workflows/workflow.py daily --mode preview  # 只读检查（online_write 全被拦）
  python workflows/workflow.py daily --mode apply  # 真实执行（每日自动化用这个）
  python workflows/workflow.py daily --mode apply --resume <run_id>  # 断点续跑
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import contracts as C          # noqa: E402
from application.runner import make_step        # noqa: E402

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PY = sys.executable or "python"

# 重组后（v2.0.0）的脚本真实落点。改目录结构时这里会立刻报错，
# 而不是把「脚本不存在」记成每一步都失败的夜间事故。
SCRIPTS = {
    "seatable_sync": "sync/seatable_sync.py",
    "partdb_sync": "sync/partdb_sync.py",
    "wechat_intake": "wx/wechat_intake.py",
    "wxmatch": "wx/wxmatch.py",
    "alerts": "workflows/alerts.py",
    "foresee": "domain/foresee.py",
    "loop_sync": "workflows/loop_sync.py",
    "loop_trigger": "workflows/loop_trigger.py",
    "daily_brief": "workflows/daily_brief.py",
    "cockpit": "cockpit/cockpit.py",
}

# 驾驶舱产物（相对技能目录）：供发布门禁把文件钉到某次运行
COCKPIT_ARTIFACT = "项目管理驾驶舱.html"


def _script(key: str) -> str:
    rel = SCRIPTS[key]
    path = os.path.join(_HERE, rel)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "工作流引用的脚本不存在：%s（%s）—— 仓库目录已变动，"
            "请更新 workflows/daily_refresh.py 的 SCRIPTS" % (rel, key))
    return path


def build_steps() -> list[C.StepSpec]:
    """每日刷新 DAG。副作用声明严格对应旧脚本真实行为。"""
    def s(step_id, name, key, args=(), **kw):
        return make_step([_PY, _script(key)] + list(args), step_id, name, **kw)

    return [
        # ── 1. 数据同步 ─────────────────────────────────────────
        # seatable_sync 失败即中止：业务表是所有计算的主数据源，没有它一切皆空。
        # partdb_sync 失败则是**降级**，不是中止 —— 对齐 evening_full.py:109-114 的
        # 实测结论（2026-09-26 partdb 502 曾 abort 全链）。库存源偶发 502 时，
        # 快照沿用上一份、链条照跑，门禁只告警不阻断。
        # 但降级必须**可见**：final.status=degraded + 播报点名「数据可能有多旧」。
        s("seatable_sync", "同步生产业务 Base", "seatable_sync",
          side_effect=C.SIDE_LOCAL_APPEND,
          failure_policy="abort", retry=1),
        s("partdb_sync", "同步 PartDB 库存", "partdb_sync",
          side_effect=C.SIDE_LOCAL_APPEND,
          failure_policy="continue", retry=1, blocking=False),

        # ── 2. 微信情报（pull 增量 + summary 回溯；AI 总结与分流留给 Prompt）──
        s("wechat_pull", "微信事件增量拉取", "wechat_intake", ["pull"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync",), failure_policy="continue", retry=1),
        s("wechat_summary", "微信 24h 摘要取数", "wechat_intake",
          ["summary", "--hours", "24",
           "--out", os.path.join("data", "wechat_intake", "summary_24h.md")],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("wechat_pull",), failure_policy="continue"),
        # 注意：ai_summary（AI 写）与 wx_dispatch 分流不在此列——那是语义步骤，
        # 由自动化 AI 读 summary_24h.md 完成，产物落 ai_summary_24h.md。

        # ── 3. 核对与预测（只读核对 + 本地快照）──
        s("wxmatch_scan", "消息↔业务表核对（只读）", "wxmatch", ["scan"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("wechat_pull",), failure_policy="continue"),
        s("alerts", "异常检测", "alerts", ["run"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync", "partdb_sync"), failure_policy="continue"),
        s("foresee", "风险预测重算", "foresee",
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync", "partdb_sync"), failure_policy="continue"),
        s("foresee_review", "预测复盘（台账对照）", "foresee", ["review"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("foresee",), failure_policy="continue"),

        # ── 3.5 控制平面：来单建案 → CRM 云端镜像 ──────────────
        # **顺序不可颠倒**：先 loop_trigger 建案件，loop_sync 才能把它们镜像到 CRM。
        # 若 loop_sync 在前，当日新建的案件要等次日才出现在云端。
        # loop_trigger 只写本地 data/business_loop/*.csv（不碰 SeaTable / CRM，
        # 见 loop_trigger.py:10「单向触发」），所以副作用是 SIDE_LOCAL_APPEND。
        # 它失败不阻断发布：控制平面台账不是驾驶舱的展示必要项。
        s("loop_trigger", "来单线索 → 控制平面案件", "loop_trigger", ["--yes"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync",), failure_policy="continue",
          retry=1, blocking=False),
        # loop_sync 是**叶子步骤**（全 DAG 无任何步骤 depends_on 它），
        # 且 CRM 镜像不是驾驶舱的展示必要项 —— 一个云端后端挂掉不该带走整链。
        # 故显式声明非阻断：它失败时整次运行判 degraded（可发布 + 强制告警），
        # 而不是 failed（拒绝发布）。判据与 partdb_sync / loop_trigger 一致。
        s("loop_sync", "业务闭环台账镜像 CRM", "loop_sync", ["--yes"],
          side_effect=C.SIDE_ONLINE_WRITE,
          depends_on=("seatable_sync", "loop_trigger"),
          failure_policy="continue", retry=1, blocking=False),

        # ── 4. 摘要与驾驶舱（生成）──
        s("daily_brief", "站会摘要 + 发件箱", "daily_brief", ["--push"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("alerts", "wechat_pull"), failure_policy="continue"),
        s("cockpit", "驾驶舱重新生成", "cockpit",
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_sync", "partdb_sync", "wechat_pull",
                      "wxmatch_scan", "foresee"),
          failure_policy="continue", expect_artifacts=(COCKPIT_ARTIFACT,)),
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
