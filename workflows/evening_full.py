# -*- coding: utf-8 -*-
"""workflows/evening_full.py — 19:00 晚间全量链路（可信执行层 v1，2026-09-26）。

业主口径的顺序，一步不省：

    采集 → OCR/提取 → 核对 → 授权写入及读回 → 快照 → 风险 → 生成发布

与 09:00 轻同步（``workflows/daily_refresh.py``）的分工：

    09:00  daily   —— 轻同步：补增量、不重建、不发布（保持原样，未改动）
    19:00  evening —— 全量：重扫 + OCR + 核对 + 授权写入 + 重建 + 门禁发布

用法（经根目录 workflow.py）：

    python workflow.py run evening --mode preview    # 演练：写入/发布全被拦
    python workflow.py run evening --mode apply      # 真实执行（晚间自动化用）
    python workflow.py run evening --mode apply --resume <run_id>

设计要点：
  - 「授权写入」步骤**不自己造授权**：data/approvals/ 里没有有效授权文件时，
    evening_write.py 打印 [skip] 并以退出码 3 结束（skipped，不算失败）；
  - 「发布」步骤带 ``--gate {run_id}``：账本里关键阶段失败或读回失败 →
    发布被拒绝（退出码 1），且**不上传任何内容**；
  - partdb_snap 失败不 abort：库存源偶发 502 不该连坐整晚的看板。
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
    """晚间全量 DAG。副作用声明严格对应各脚本真实行为。"""
    def s(step_id, name, script_args, **kw):
        return make_step([_PY, os.path.join(_HERE, script_args[0])] + list(script_args[1:]),
                         step_id, name, **kw)

    return [
        # ── ① 采集：微信消息增量 ────────────────────────────────
        s("wechat_collect", "微信消息采集（增量）",
          ["wechat_intake.py", "pull"],
          side_effect=C.SIDE_LOCAL_APPEND, failure_policy="abort", retry=1),

        # ── ② OCR / 提取：图片解密 + 识别（产物进 data/我们媒体索引）──
        # 环境不具备（没有微信媒体库/没有 OCR 引擎）时脚本自退 3 = skipped
        s("wxmedia_ocr", "微信图片解密 + OCR 提取",
          ["wxmedia.py", "scan", "--hours", "24", "--ocr"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("wechat_collect",), failure_policy="continue"),

        # ── ③ 核对：消息 ↔ 业务表只读比对 ──────────────────────
        s("wxmatch_scan", "消息↔业务表核对（只读）",
          ["wxmatch.py", "scan"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("wxmedia_ocr",), failure_policy="abort"),

        # ── ④ 授权写入及读回 ──────────────────────────────────
        # 这是整条链路唯一写业务表的地方，也是唯一需要「人给授权」的地方。
        # 不设 write_mode=approval_required：那会要求运行期 --yes，
        # 而本链路的授权凭证是 data/approvals/ 下的授权文件（见
        # application/authorization.py），由 evening_write.py 自己校验。
        s("evening_write", "授权写入及读回（无授权则跳过）",
          ["evening_write.py"],
          side_effect=C.SIDE_ONLINE_WRITE,
          depends_on=("wxmatch_scan",), failure_policy="abort"),

        # ── ⑤ 快照：写入后的真实状态固化 ────────────────────────
        s("seatable_snap", "业务表快照同步",
          ["seatable_sync.py"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("evening_write",), failure_policy="abort", retry=1),
        # 库存源偶发 502 不该让整晚断掉（2026-09-26 实测：partdb 502 曾 abort 全链）
        s("partdb_snap", "PartDB 库存快照",
          ["partdb_sync.py"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_snap",), failure_policy="continue", retry=1),

        # ── ⑥ 风险 ────────────────────────────────────────────
        s("alerts", "异常检测",
          ["alerts.py", "run"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_snap", "partdb_snap"), failure_policy="continue"),
        s("foresee", "风险预测重算",
          ["foresee.py"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_snap", "partdb_snap"), failure_policy="continue"),
        s("foresee_review", "预测复盘（台账对照）",
          ["foresee.py", "review"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("foresee",), failure_policy="continue"),

        # ── ⑦ 生成：驾驶舱 ────────────────────────────────────
        s("cockpit", "驾驶舱重新生成",
          ["cockpit.py"],
          side_effect=C.SIDE_LOCAL_APPEND,
          depends_on=("seatable_snap", "partdb_snap", "wxmatch_scan",
                      "alerts", "foresee"),
          failure_policy="continue"),

        # ── ⑧ 发布：带门禁（账本没证明过的，不许对外发布）──────
        # 无 token / 非 WorkBuddy 环境 → publish.py 自退 3 = skipped
        # 门禁未通过（关键阶段失败 / 读回失败）→ 退出码 1，不上传任何内容
        s("publish", "发布到团队资料库（带发布门禁）",
          ["publish.py", "--gate", "{run_id}"],
          side_effect=C.SIDE_PUBLISH,
          depends_on=("cockpit", "evening_write"), failure_policy="continue"),
    ]


WORKFLOWS = {
    "evening": build_steps,
}


def run_workflow(name: str, ctx: C.RunContext) -> C.RunResult:
    from application.runner import WorkflowRunner
    if name not in WORKFLOWS:
        raise SystemExit("[错误] 未知工作流：%r（可选：%s）" % (name, "|".join(WORKFLOWS)))
    return WorkflowRunner(ctx, WORKFLOWS[name]()).run()
