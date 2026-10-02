# -*- coding: utf-8 -*-
"""application/runner.py — 工作流执行器（Phase 1）。

消费 contracts.StepSpec 列表，按 depends_on 组 DAG 拓扑执行：
  - 运行前三道闸裁决（destructive/--yes、online_write/apply、approval/人工）；
  - 每步结果落 data/runs/<run_id>/<序号>_<step_id>.json；
  - final.json 汇总，AI 播报只读这里；
  - --resume <run_id> 时已 success 的步骤跳过，failed/blocked 重试。

安全性要点（2026-09-27 收紧）：
  1. **续跑必须同上下文**：复用某一步的「成功」前，先比对该步骤所在的运行
     上下文指纹（workflow / mode / Base / 目录 / 步骤定义）。指纹不一致 → 一律
     重跑，绝不拿另一次运行、另一种模式的结果冒充本次成功。
  2. **产物必须存在**：步骤用 ``expect_artifacts`` 声明应当产出的文件；声明了却
     没产出 → 该步判失败（否则门禁会为一份根本不存在的产物放行）。
  3. **blocking=False 才算降级**：依赖步骤失败时，只有该依赖显式声明非阻断，
     下游才继续跑；否则照旧 blocked。

设计依据：docs/avatar-loop-v2.md §4。本模块不 import 任何业务模块，
步骤实现（含 subprocess 包装旧脚本）由 workflows/ 提供。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from typing import Callable, Optional, Sequence

from application import contracts as C

RUNS_DIR_NAME = "runs"
CONTEXT_FILE_NAME = "context.json"

# 参与上下文指纹的字段：换了其中任何一个，「上次成功」都不再适用于本次。
_FINGERPRINT_FIELDS = ("workflow", "mode", "base_name", "skill_dir", "config_path",
                       "data_dir")


def context_fingerprint(ctx: C.RunContext) -> str:
    basis = {k: str(getattr(ctx, k, "") or "") for k in _FINGERPRINT_FIELDS}
    return hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


def _step_fingerprint(ctx: C.RunContext, spec: C.StepSpec) -> str:
    basis = context_fingerprint(ctx) + "|" + "|".join([
        spec.id, spec.side_effect, str(spec.write_mode or ""),
        ",".join(spec.depends_on), "1" if spec.blocking else "0"])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


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
        self._resume_ok = False
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
        """恢复历史 run：仅当上下文与步骤定义完全一致时才复用 success。"""
        if not self.ctx.resume_of:
            return
        old_dir = os.path.join(self.runs_dir, self.ctx.resume_of)
        if not os.path.isdir(old_dir):
            print("[warn] 找不到要续跑的运行 %s，本次按全新运行执行" % self.ctx.resume_of)
            return
        old_ctx = self._read_json(os.path.join(old_dir, CONTEXT_FILE_NAME))
        want = context_fingerprint(self.ctx)
        if not isinstance(old_ctx, dict) or old_ctx.get("context_fingerprint") != want:
            print("[warn] 续跑上下文与 %s 不一致（工作流/模式/目录/Base 有变），"
                  "本次不复用任何历史步骤结果，全部重跑" % self.ctx.resume_of)
            self._resume_ok = False
            return
        self._resume_ok = True
        reused = 0
        for s in self.steps:
            data = self._read_json(self._step_file_in(old_dir, s.id))
            if not isinstance(data, dict):
                continue
            if data.get("status") != C.STATUS_SUCCESS:
                continue
            fp = data.get("ctx_fingerprint") or ""
            if fp != _step_fingerprint(self.ctx, s):
                print("[warn] 步骤 %s 的定义已变化，本次不沿用历史成功结果" % s.id)
                continue
            data["reused_from"] = self.ctx.resume_of
            self._results[s.id] = data
            reused += 1
        print("[resume] 上下文一致，复用 %d 个已成功步骤（其余重跑）" % reused)

    def _step_file_in(self, run_dir: str, step_id: str):
        if not os.path.isdir(run_dir):
            return ""
        for fn in sorted(os.listdir(run_dir)):
            if fn.endswith("_%s.json" % step_id):
                return os.path.join(run_dir, fn)
        return ""

    @staticmethod
    def _read_json(path: str):
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _sha256(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _resolve_artifacts(self, spec: C.StepSpec) -> list[str]:
        out = []
        for raw in spec.expect_artifacts or ():
            p = str(raw)
            if not os.path.isabs(p):
                p = os.path.join(self.ctx.skill_dir or "", p)
            out.append(os.path.abspath(p))
        return out

    def run(self) -> C.RunResult:
        os.makedirs(self._run_dir, exist_ok=True)
        with open(os.path.join(self._run_dir, CONTEXT_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump({**self.ctx.to_dict(),
                       "context_fingerprint": context_fingerprint(self.ctx)},
                      f, ensure_ascii=False, indent=2)
        self._load_resume_state()
        started = _dt.datetime.now().isoformat(timespec="seconds")
        rr = C.RunResult(run_id=self.ctx.run_id, workflow=self.ctx.workflow,
                         started_at=started)
        aborted = False
        for s in self._order:
            # resume：上下文与步骤定义都一致时才跳过
            if s.id in self._results:
                rr.steps.append(self._results[s.id])
                continue
            # 前置检查：只有**阻断型**依赖失败才拦下游（非阻断＝真实降级）
            dep_fail = [d for d in s.depends_on
                        if self._last_status(d) in (C.STATUS_FAILED, C.STATUS_BLOCKED)
                        and getattr(self._by_id.get(d), "blocking", True)]
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
                    else:
                        # 声明的产物必须真实存在，否则「成功」是空的
                        want = self._resolve_artifacts(s)
                        missing = [p for p in want if not os.path.isfile(p)]
                        if missing and res.status == C.STATUS_SUCCESS:
                            res.fail("声明产物未生成：%s" % "、".join(
                                os.path.basename(p) for p in missing))
                        res.artifacts = list(dict.fromkeys(
                            [str(a) for a in (res.artifacts or [])] + want))
            d = res.to_dict()
            d["blocking"] = bool(s.blocking)
            d["ctx_fingerprint"] = _step_fingerprint(self.ctx, s)
            d["artifact_hashes"] = {}
            for p in d.get("artifacts") or []:
                try:
                    if os.path.isfile(p):
                        d["artifact_hashes"][os.path.abspath(p)] = self._sha256(p)
                except OSError:
                    pass
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
        # 整次状态三档（2026-10-02 修 G1/G6）。
        # 旧实现：`all(st in (success, skipped))` —— **完全不看 blocking**，
        # 于是 evening 里声明 blocking=False 的 partdb_snap 一失败，整次就判 failed；
        # 而发布门禁（gates.py）要求整次 ∈ {success, skipped}，一拦到底 ——
        # 「非阻断失败只告警」那条善意逻辑**永远到不了**，只能靠人工改 final.json 绕过。
        # 现在判定收敛到 `contracts.run_status()`（全仓库唯一一份）：
        # 只有**阻断型**失败判 failed；纯非阻断失败判 degraded（降级可见、发布放行）。
        # 不在此处再写一遍条件 —— 见 contracts.run_status 的 docstring（G6）。
        rr.status = C.run_status(rr.steps)
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

    退出码约定：
      0        → success
      3        → skipped（数据源不可用/开关关闭，产物不刷新是正常结果，
                  不是失败也不是成功——验收器不查其产物新鲜度）
      其他非 0 → failed（retry 生效）

    参数里可用占位符 ``{run_id}`` / ``{skill_dir}``（运行时替换），
    供「发布门禁」这类需要知道本次运行编号的步骤使用。
    """
    def _run(ctx: C.RunContext) -> C.StepResult:
        import subprocess
        import sys
        res = C.StepResult(step_id=step_id)
        argv = [str(a).replace("{run_id}", ctx.run_id)
                     .replace("{skill_dir}", ctx.skill_dir or "")
                for a in cmd]
        proc = subprocess.run(argv, cwd=ctx.skill_dir or None,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        out = (proc.stdout or "") + (proc.stderr or "")
        res.artifacts = []
        res.warnings = [l for l in out.splitlines() if "[warn]" in l or "[!]" in l or "[skip]" in l][:10]
        if proc.returncode == 0:
            return res.ok()
        if proc.returncode == 3:
            return _skipped(res, out)
        return res.fail("exit=%s %s" % (proc.returncode, out.strip()[-400:]))
    return C.StepSpec(id=step_id, name=name, run=_run, **kw)


def _skipped(res: C.StepResult, out: str) -> C.StepResult:
    res.status = C.STATUS_SKIPPED
    res.error = ""
    skip_line = next((l for l in out.splitlines() if "[skip]" in l or "[!]" in l), "")
    res.counts = {"skipped_reason": skip_line[:120]} if skip_line else {}
    return res
