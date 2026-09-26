# -*- coding: utf-8 -*-
"""application/gates.py — 发布门禁（可信执行层 v1，2026-09-26）。

一条原则：**账本里没证明过的，不许对外发布。**

驾驶舱是一次「信息释放」——发出去就有人按它做决定。所以发布前必须回看
这次运行的账本（``data/runs/<run_id>/final.json``）：

  1. 整次运行不能有 failed 步骤；
  2. OCR / 核对 / 授权写入这几类**关键阶段**不能 failed 或 blocked；
  3. 关键阶段若报出自带计数 ``verify_failed``，同样视为关键失败 ——
     那意味着「以为写进去了，其实没写」，看板与真实数据已经脱节。

``skipped`` 不算失败：数据源不可用（没微信消息、OCR 环境缺失、当晚没给
授权）本来就是正常状态，不该连坐发布。

设计约束：纯读 + 纯计算，不 import 任何业务模块，可离线单测。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import contracts as C

# 晚间链路的「关键阶段」：这几步一旦没跑成，看板就不该发出去
CRITICAL_STEP_IDS = ("wechat_collect", "wxmedia_ocr", "wxmatch_scan", "evening_write")


@dataclass
class GateResult:
    """发布门禁结论。allowed=False 时 reasons 逐条说明为什么。"""
    run_id: str = ""
    allowed: bool = False
    reasons: list = field(default_factory=list)
    checked: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def render(self) -> str:
        if self.allowed:
            return "发布门禁通过（%s）" % (self.run_id or "?")
        lines = ["发布门禁未通过（%s），共 %d 条问题：" % (self.run_id or "?", len(self.reasons))]
        lines += ["  · %s" % r for r in self.reasons]
        return "\n".join(lines)


def default_runs_dir(skill_dir: str = "") -> str:
    base = skill_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "runs")


def find_run_dir(run_id: str, runs_dir: str = "") -> str:
    """解析 run_id（支持 latest / 空）→ 运行目录绝对路径。"""
    rd = runs_dir or default_runs_dir()
    if run_id in ("", "latest"):
        best = ""
        if os.path.isdir(rd):
            for name in sorted(os.listdir(rd)):
                if os.path.exists(os.path.join(rd, name, "final.json")):
                    best = name
        return os.path.join(rd, best) if best else ""
    return os.path.join(rd, run_id)


def evaluate_dict(data: Mapping[str, Any],
                  critical: Sequence[str] = CRITICAL_STEP_IDS) -> GateResult:
    """对一份已解析的 final.json 做门禁判定（离线可测）。"""
    steps = list(data.get("steps") or [])
    by_id = {s.get("step_id"): s for s in steps}
    reasons: list[str] = []

    # 1. 整次运行不允许有 failed 步骤
    for s in steps:
        if s.get("status") == C.STATUS_FAILED:
            reasons.append("步骤 %s 失败：%s"
                           % (s.get("step_id"), (s.get("error") or "")[:140]))

    # 2. 关键阶段必须跑过且不是 failed / blocked
    for sid in critical:
        s = by_id.get(sid)
        if s is None:
            reasons.append("关键步骤 %s 不在账本里 —— 该阶段没跑，不能发布" % sid)
            continue
        status = s.get("status")
        if status in (C.STATUS_FAILED, C.STATUS_BLOCKED):
            reasons.append("关键步骤 %s 未成功（%s）：%s"
                           % (sid, status, (s.get("error") or "")[:140]))
        counts = s.get("counts") or {}
        n_bad = int(counts.get("verify_failed") or 0)
        if n_bad:
            reasons.append("关键步骤 %s 有 %d 条读回验证失败 —— 台账与真实数据已脱节"
                           % (sid, n_bad))

    return GateResult(
        run_id=str(data.get("run_id") or ""),
        allowed=not reasons,
        reasons=reasons,
        checked={s.get("step_id"): s.get("status") for s in steps},
    )


def evaluate(run_id: str = "latest", runs_dir: str = "",
             critical: Sequence[str] = CRITICAL_STEP_IDS) -> GateResult:
    """按 run_id 判定发布门禁。找不到账本 → 直接阻断。"""
    run_dir = find_run_dir(run_id, runs_dir)
    if not run_dir:
        return GateResult(run_id=run_id, allowed=False,
                          reasons=["找不到任何运行账本 —— 没有账本就不发布"])
    final = os.path.join(run_dir, "final.json")
    if not os.path.exists(final):
        return GateResult(run_id=os.path.basename(run_dir), allowed=False,
                          reasons=["找不到运行账本 %s —— 没有账本就不发布" % final])
    try:
        with open(final, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return GateResult(run_id=os.path.basename(run_dir), allowed=False,
                          reasons=["运行账本无法解析：%s" % e])
    return evaluate_dict(data, critical)


__all__ = ["CRITICAL_STEP_IDS", "GateResult", "default_runs_dir", "find_run_dir",
           "evaluate", "evaluate_dict"]
