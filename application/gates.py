# -*- coding: utf-8 -*-
"""application/gates.py — 发布门禁（可信执行层 v1，2026-09-26）。

一条原则：**账本里没证明过的，不许对外发布。**

驾驶舱是一次「信息释放」——发出去就有人按它做决定。所以发布前必须回看
这次运行的账本：

  1. 整次运行不能有（阻断型）failed 步骤；
  2. OCR / 核对 / 授权写入这几类**关键阶段**不能 failed 或 blocked；
  3. 关键阶段若报出自带计数 ``verify_failed``，同样视为关键失败 ——
     那意味着「以为写进去了，其实没写」，看板与真实数据已经脱节。

``skipped`` 不算失败：数据源不可用（没微信消息、OCR 环境缺失、当晚没给
授权）本来就是正常状态，不该连坐发布。

2026-09-27 收紧（审计 A05 / A06 / A10）：
  · **账本按步骤文件读，不再只认 final.json**。原先门禁只读
    ``<run>/final.json``，而 final.json 是在**所有步骤（含发布）跑完后**才写的
    —— 于是全新一晚的发布步骤执行时，账本根本还不存在，门禁必然拒绝发布。
    现在改为读 ``<run>/NN_<step>.json``（runner 每步落盘），发布时前面各步
    的账本已在盘上，门禁可用；final.json 存在时作为权威覆盖。
  · **格式不合规一律拒绝**：未知/缺失状态、重复 step_id、非表结构 —— 全部
    阻断，而不是「没看见 failed 就放行」。
  · **产物绑定**：``evaluate_artifact()`` 要求待上传文件在本run账本里被记过
    sha256 且哈希一致，改过的 HTML 不能用旧账本蒙混过关。

设计约束：纯读 + 纯计算，不 import 任何业务模块，可离线单测。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from . import contracts as C

# 晚间链路的「关键阶段」：这几步一旦没跑成，看板就不该发出去
CRITICAL_STEP_IDS = ("wechat_collect", "wxmedia_ocr", "wxmatch_scan", "evening_write")

# 账本里允许出现的步骤终态。其余一律视为账本不可信。
_KNOWN_STATUSES = (C.STATUS_SUCCESS, C.STATUS_SKIPPED, C.STATUS_FAILED, C.STATUS_BLOCKED)


@dataclass
class GateResult:
    """发布门禁结论。allowed=False 时 reasons 逐条说明为什么。"""
    run_id: str = ""
    allowed: bool = False
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    checked: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def render(self) -> str:
        if self.allowed:
            lines = ["发布门禁通过（%s）" % (self.run_id or "?")]
        else:
            lines = ["发布门禁未通过（%s），共 %d 条问题："
                     % (self.run_id or "?", len(self.reasons))]
            lines += ["  · %s" % r for r in self.reasons]
        lines += ["  ! %s" % w for w in self.warnings]
        return "\n".join(lines)


def default_runs_dir(skill_dir: str = "") -> str:
    base = skill_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "runs")


def _has_ledger(run_dir: str) -> bool:
    if not os.path.isdir(run_dir):
        return False
    if os.path.exists(os.path.join(run_dir, "final.json")):
        return True
    return any(fn.endswith(".json") and fn != "context.json"
               for fn in os.listdir(run_dir))


def find_run_dir(run_id: str, runs_dir: str = "") -> str:
    """解析 run_id（支持 latest / 空）→ 运行目录绝对路径。"""
    rd = runs_dir or default_runs_dir()
    if run_id in ("", "latest"):
        best = ""
        if os.path.isdir(rd):
            for name in sorted(os.listdir(rd)):
                if _has_ledger(os.path.join(rd, name)):
                    best = name
        return os.path.join(rd, best) if best else ""
    return os.path.join(rd, run_id)


def load_steps(run_dir: str) -> tuple[Optional[dict], list, str]:
    """读取一次运行的步骤账本。

    返回 ``(final, steps, error)``：``final`` 为 final.json（可能为 None），
    ``steps`` 为步骤记录列表（final.json 里的记录优先，因为它是汇总后的权威
    版本），``error`` 非空表示账本不可信、必须阻断。
    """
    if not os.path.isdir(run_dir):
        return None, [], "运行目录不存在：%s" % run_dir
    merged: dict[str, Any] = {}
    order: list[str] = []
    for fn in sorted(os.listdir(run_dir)):
        if not fn.endswith(".json") or fn == "final.json":
            continue
        path = os.path.join(run_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return None, [], "步骤账本 %s 无法解析：%s" % (fn, e)
        if not isinstance(data, Mapping):
            return None, [], "步骤账本 %s 不是表结构" % fn
        sid = data.get("step_id") or fn[:-5].split("_", 1)[-1]
        if str(sid) not in merged:
            order.append(str(sid))
        merged[str(sid)] = dict(data)
    final = None
    final_path = os.path.join(run_dir, "final.json")
    if os.path.exists(final_path):
        try:
            with open(final_path, encoding="utf-8") as f:
                final = json.load(f)
        except (OSError, ValueError) as e:
            return None, [], "final.json 无法解析：%s" % e
        if not isinstance(final, Mapping):
            return None, [], "final.json 不是表结构"
        for s in (final.get("steps") or []):
            if not isinstance(s, Mapping):
                return final, [], "final.json 的步骤记录不是表结构"
            sid = str(s.get("step_id") or "")
            if sid and sid not in merged:
                order.append(sid)
            if sid:
                merged[sid] = dict(s)
    if not merged:
        return final, [], "运行目录 %s 里没有任何步骤账本" % run_dir
    return final, [merged[k] for k in order], ""


def evaluate_dict(data: Mapping[str, Any],
                  critical: Sequence[str] = CRITICAL_STEP_IDS) -> GateResult:
    """对一份已解析的 final.json 做门禁判定（离线可测）。

    直接转交 ``evaluate_steps`` —— 唯一一套判定逻辑，避免「门禁有两套、
    其中一套比较松」这种自己给自己开后门的结构。
    """
    if not isinstance(data, Mapping):
        return GateResult(allowed=False, reasons=["账本不是表结构 —— 不可信，不发布"])
    steps = data.get("steps") or []
    if not isinstance(steps, (list, tuple)):
        return GateResult(run_id=str(data.get("run_id") or ""), allowed=False,
                          reasons=["账本 steps 不是列表 —— 不可信，不发布"])
    return evaluate_steps(str(data.get("run_id") or ""), data, list(steps), critical)


def evaluate_steps(run_id: str, final: Optional[Mapping[str, Any]], steps: Sequence[Mapping[str, Any]],
                   critical: Sequence[str] = CRITICAL_STEP_IDS) -> GateResult:
    """对「按步骤文件合并出来的账本」做门禁判定（fail-closed 校验）。"""
    reasons: list[str] = []
    warnings: list[str] = []
    checked: dict[str, Any] = {}

    if final is not None:
        f_status = str(final.get("status") or "")
        if f_status and f_status not in (C.STATUS_SUCCESS, C.STATUS_SKIPPED):
            reasons.append("整次运行状态为 %s，不能发布" % f_status)
        f_run = str(final.get("run_id") or "")
        if not f_run:
            reasons.append("final.json 缺少 run_id —— 账本与本次运行无法对应")
        elif run_id and f_run != run_id:
            reasons.append("账本 run_id=%s 与本次运行的 %s 不一致" % (f_run, run_id))

    seen = set()
    for s in steps:
        sid = str(s.get("step_id") or "")
        if not sid:
            reasons.append("存在缺少 step_id 的步骤记录 —— 账本不可信")
            continue
        if sid in seen:
            reasons.append("步骤 %s 在账本里重复出现 —— 状态有歧义" % sid)
            continue
        seen.add(sid)
        status = str(s.get("status") or "")
        checked[sid] = status
        if status not in _KNOWN_STATUSES:
            reasons.append("步骤 %s 的状态 %r 不在已知终态内 —— 账本不可信" % (sid, status))
            continue
        counts = s.get("counts")
        if counts is not None and not isinstance(counts, Mapping):
            reasons.append("步骤 %s 的 counts 不是表结构 —— 账本不可信" % sid)
            continue
        n_bad = (counts or {}).get("verify_failed")
        if n_bad is not None:
            if isinstance(n_bad, bool) or not isinstance(n_bad, int) or n_bad < 0:
                reasons.append("步骤 %s 的 verify_failed 非法：%r" % (sid, n_bad))
                continue
        # 阻断型失败 → 阻断；非阻断失败 → 只告警（真实降级，但必须可见）
        if status == C.STATUS_FAILED:
            if s.get("blocking", True):
                reasons.append("步骤 %s 失败：%s" % (sid, (s.get("error") or "")[:140]))
            else:
                warnings.append("步骤 %s 失败但不阻断发布（%s）：%s"
                                % (sid, s.get("side_effect") or "非关键",
                                   (s.get("error") or "")[:100]))
        for crit in critical:
            if sid == crit:
                if status in (C.STATUS_FAILED, C.STATUS_BLOCKED):
                    reasons.append("关键步骤 %s 未成功（%s）：%s"
                                   % (sid, status, (s.get("error") or "")[:140]))
                if int((counts or {}).get("verify_failed") or 0):
                    reasons.append("关键步骤 %s 有 %s 条读回验证失败 —— 台账与真实数据已脱节"
                                   % (sid, (counts or {}).get("verify_failed")))

    for crit in critical:
        if crit not in seen:
            reasons.append("关键步骤 %s 不在账本里 —— 该阶段没跑，不能发布" % crit)

    return GateResult(run_id=run_id, allowed=not reasons, reasons=reasons,
                      warnings=warnings, checked=checked)


def artifact_hashes(steps: Sequence[Mapping[str, Any]]) -> dict:
    """汇总账本里所有已记录的产物哈希（绝对路径 → sha256）。"""
    out: dict[str, str] = {}
    for s in steps:
        hashes = s.get("artifact_hashes") or {}
        if isinstance(hashes, Mapping):
            for path, digest in hashes.items():
                if isinstance(path, str) and isinstance(digest, str):
                    out[os.path.abspath(path)] = digest
    return out


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def evaluate(run_id: str = "latest", runs_dir: str = "",
             critical: Sequence[str] = CRITICAL_STEP_IDS,
             artifact: str = "") -> GateResult:
    """按 run_id 判定发布门禁。找不到/读不懂账本 → 直接阻断。

    ``artifact`` 给定时，额外要求该文件与本次运行的账本**绑定**（见
    ``evaluate_artifact``）——这样「用旧账本放行新文件」会被挡住。
    """
    run_dir = find_run_dir(run_id, runs_dir)
    if not run_dir or not _has_ledger(run_dir):
        return GateResult(run_id=run_id, allowed=False,
                          reasons=["找不到任何运行账本 —— 没有账本就不发布"])
    final, steps, err = load_steps(run_dir)
    if err:
        return GateResult(run_id=os.path.basename(run_dir), allowed=False,
                          reasons=[err + " —— 账本不可信，不发布"])
    g = evaluate_steps(os.path.basename(run_dir), final, steps, critical)
    if artifact and g.allowed:
        bind = bind_artifact(steps, artifact, g.run_id)
        if bind is not None:
            g.allowed = False
            g.reasons.append(bind)
    return g


def bind_artifact(steps: Sequence[Mapping[str, Any]], artifact: str,
                  run_id: str = "") -> Optional[str]:
    """校验待发布文件确由这次运行产出。返回 None 表示通过，否则返回拒绝原因。"""
    target = os.path.abspath(artifact)
    if not os.path.isfile(target):
        return "待发布文件不存在：%s" % target
    recorded = artifact_hashes(steps)
    if target not in recorded:
        return ("本次运行（%s）的账本里没有记录产物 %s —— 无法证明它来自这次运行，"
                "拒绝发布（先跑工作流生成，再由发布步骤上传）"
                % (run_id or "?", os.path.basename(target)))
    try:
        actual = sha256_file(target)
    except OSError as e:
        return "待发布文件无法读取：%s" % e
    if actual != recorded[target]:
        return ("待发布文件 %s 的内容与账本记录不一致（账本 %s… / 实际 %s…）—— "
                "文件在账本生成后被改过，拒绝发布"
                % (os.path.basename(target), recorded[target][:12], actual[:12]))
    return None


__all__ = ["CRITICAL_STEP_IDS", "GateResult", "default_runs_dir", "find_run_dir",
           "evaluate", "evaluate_dict", "evaluate_steps", "load_steps",
           "artifact_hashes", "bind_artifact", "sha256_file"]
