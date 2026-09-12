# -*- coding: utf-8 -*-
"""application/runner.py — 工作流执行器（Phase 1）。

消费 contracts.StepSpec 列表，按 depends_on 组 DAG 拓扑执行：
  - 运行前三道闸裁决（destructive/--yes、online_write/apply、approval/人工）；
  - 每步结果落 data/runs/<run_id>/<序号>_<step_id>.json；
  - final.json 汇总，AI 播报只读这里；
  - --resume <run_id> 时已 success 的步骤跳过，failed/blocked 重试。

设计依据：docs/avatar-loop-v2.md §4。本模块不 import 任何业务模块，
步骤实现（含 subprocess 包装旧脚本）由 workflows/ 提供。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Callable, Optional, Sequence

from application import contracts as C

RUNS_DIR_NAME = "runs"


class WorkflowRunner:
    def __init__(self, ctx: C.RunContext, steps: Sequence[C.StepSpec],
                 runs_dir: Optional[str] = None):
        self.ctx = ctx
        self.steps = list(steps)
        self.runs_dir = runs_dir or os.path.join(
            ctx.skill_dir or os.getcwd(), "data", RUNS_DIR_NAME)
        self._by_id = {s.id: s for s in self.steps}
        self._results: dict[str, dict] = {}
        self._order: list[C.StepSpec] = []
        self._validate()

    # ── 准备 ─────────────────────────────────────────────
    def _validate(self) -> None:
        seen = set()
        for s in self.steps:
            if s.id in seen:
                raise ValueError("步骤 ID 重复：%s" % s.id)
            seen.add(s.id)
        for s in self.steps:
            for dep in s.depends_on:
                if dep not in self._by_id:
                    raise ValueError("步骤 %s 依赖不存在的 %s" % (s.id, dep))
        # Kahn 拓扑排序（稳定：同层按声明顺序）
        indeg = {s.id: len(set(s.depends_on)) for s in self.steps}
        ready = [s for s in self.steps if indeg[s.id] == 0]
        order: list[C.StepSpec] = []
        while ready:
            s = ready.pop(0)
            order.append(s)
            for t in self.steps:
                if s.id in set(t.depends_on):
                    indeg[t.id] -= 1
                    if indeg[t.id] == 0:
                        ready.append(t)
        if len(order) != len(self.steps):
            raise ValueError("步骤存在循环依赖")
        self._order = order

    # ── 运行 ─────────────────────────────────────────────
    def _step_file(self, step_id: str) -> str:
        idx = next(i for i, s in enumerate(self._order, 1) if s.id == step_id)
        return os.path.join(self._run_dir, "%02d_%s.json" % (idx, step_id))

    @property
    def _run_dir(self) -> str:
        return os.path.join(self.runs_dir, self.ctx.run_id)

    def _load_resume_state(self) -> None:
        """恢复历史 run 的已完成步骤：success → 跳过标记。"""
        if not self.ctx.resume_of:
            return
        old_dir = os.path.join(self.runs_dir, self.ctx.resume_of)
        if not os.path.isdir(old_dir):
            return
        for s in self.steps:
            for fn in sorted(os.listdir(old_dir)):
                if fn.endswith("_%s.json" % s.id):
                    try:
                        with open(os.path.join(old_dir, fn), encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        break
                    if data.get("status") == C.STATUS_SUCCESS:
                        self._results[s.id] = data
                    break

    def run(self) -> C.RunResult:
        os.makedirs(self._run_dir, exist_ok=True)
        self._load_resume_state()
        started = _dt.datetime.now().isoformat(timespec="seconds")
        rr = C.RunResult(run_id=self.ctx.run_id, workflow=self.ctx.workflow,
                         started_at=started)
        aborted = False
        for s in self._order:
            # resume：已成功的直接跳过
            if s.id in self._results:
                rr.steps.append(self._results[s.id])
                continue
            # 前置检查
            dep_fail = [d for d in s.depends_on
                        if self._last_status(d) in (C.STATUS_FAILED, C.STATUS_BLOCKED)]
            res = C.StepResult(step_id=s.id)
            if aborted:
                res.status = C.STATUS_BLOCKED
                res.error = "先前 abort 步骤导致跳过"
            elif dep_fail:
                res.status = C.STATUS_BLOCKED
                res.error = "依赖步骤未成功：%s" % "、".join(dep_fail)
            else:
                ok, why = s.allowed_in(self.ctx)
                if not ok:
                    res.status = C.STATUS_BLOCKED
                    res.error = why
                else:
                    res.started_at = _dt.datetime.now().isoformat(timespec="seconds")
                    attempts = s.retry + 1
                    last_err = None
                    for _ in range(attempts):
                        try:
                            out = s.run(self.ctx)
                            res = out if isinstance(out, C.StepResult) else res
                            if res.status in (C.STATUS_SUCCESS, C.STATUS_SKIPPED):
                                break
                            last_err = res.error or "步骤返回非成功状态"
                        except Exception as e:  # noqa: BLE001 — runner 必须吞掉一切步骤异常
                            last_err = "%s: %s" % (type(e).__name__, e)
                            res.status = C.STATUS_FAILED
                        if _:
                            res.error = last_err
                    res.finished_at = _dt.datetime.now().isoformat(timespec="seconds")
                    if res.status not in (C.STATUS_SUCCESS, C.STATUS_SKIPPED):
                        res.status = C.STATUS_FAILED
                        res.error = res.error or last_err
            d = res.to_dict()
            self._results[s.id] = d
            rr.steps.append(d)
            try:
                with open(self._step_file(s.id), "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
            except OSError:
                pass  # 落盘失败不阻断执行，final.json 仍会尝试
            if res.status == C.STATUS_FAILED and s.failure_policy == "abort":
                aborted = True
        rr.finished_at = _dt.datetime.now().isoformat(timespec="seconds")
        statuses = [s["status"] for s in rr.steps]
        rr.status = (C.STATUS_SUCCESS if statuses and all(
            st in (C.STATUS_SUCCESS, C.STATUS_SKIPPED) for st in statuses)
            else C.STATUS_FAILED)
        try:
            with open(os.path.join(self._run_dir, "final.json"), "w", encoding="utf-8") as f:
                json.dump(rr.to_dict(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return rr

    def _last_status(self, step_id: str) -> str:
        d = self._results.get(step_id)
        return d.get("status", C.STATUS_BLOCKED) if d else C.STATUS_BLOCKED


def make_step(cmd: Sequence[str], step_id: str, name: str, **kw) -> C.StepSpec:
    """把旧脚本包装成 StepSpec：subprocess 执行，退出码即成败。

    这是 Phase 1 的核心兼容手段——不重写业务，先拿到统一编号、
    结构化结果、checkpoint 与失败策略。
    """
    def _run(ctx: C.RunContext) -> C.StepResult:
        import subprocess
        import sys
        res = C.StepResult(step_id=step_id)
        proc = subprocess.run(list(cmd), cwd=ctx.skill_dir or None,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (proc.stdout or "") + (proc.stderr or "")
        res.artifacts = []
        res.warnings = [l for l in out.splitlines() if "[warn]" in l or "[!]" in l][:10]
        if proc.returncode == 0:
            return res.ok()
        return res.fail("exit=%s %s" % (proc.returncode, out.strip()[-400:]))
    return C.StepSpec(id=step_id, name=name, run=_run, **kw)
