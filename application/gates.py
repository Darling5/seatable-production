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

2026-10-02 修订（全链降级 / Plan B 专项，四处）：
  · **FIX-2**：``_NON_STEP_JSON`` 统一「非步骤 JSON」口径（``final.json`` + ``context.json``），
    ``load_steps`` 与 ``_has_ledger`` 共用，并收紧 step_id 推导 —— 拿不到就跳过，
    不再「造一个 key」，否则下游仍会判「账本不可信」而误杀发布；
  · **FIX-1**：整次状态白名单扩为 ``{success, skipped, degraded}``。此前与
    ``runner.py`` 的整次判定语义矛盾（runner 不看 blocking 就判 failed），
    导致 ``blocking=False`` 声明的降级**永远到不了发布**；
  · **FIX-5**：``find_run_dir("latest")`` 改为**按时间**取最新，不再是目录名字典序
    （``daily-`` < ``evening-`` 会让 latest 永远命中 evening）；
  · **FIX-3**：关键阶段按账本的 ``workflow`` 字段分开取（``CRITICAL_BY_WORKFLOW``），
    不再用「只有 evening 的 id」的全局常量。

设计约束：纯读 + 纯计算，不 import 任何业务模块，可离线单测。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from . import contracts as C

# 账本目录里**不是步骤**的 JSON（运行元数据）。
# 2026-10-02 修 FIX-2：原先只排除 final.json，于是 context.json（RunContext 的序列化，
# 含 run_id/workflow/mode/skill_dir…，**没有 step_id 字段**）被当成步骤读，
# 触发「存在缺少 step_id 的步骤记录 —— 账本不可信」→ fail-closed 误杀发布。
# 实测：evening 的 publish 天天被拒（「本次未上传任何内容」），对外驾驶舱长期没更新。
# 注意：本文件下面的 _has_ledger 早已排除 context.json —— 属同文件两套口径的漏改。
_NON_STEP_JSON = ("final.json", "context.json")

# 步骤文件的命名契约（runner._step_file / workflow.py cmd_note 都按 "%02d_%s.json" 写）。
# 2026-10-02 加（修 FIX-10）：run 目录里**只有**匹配它的 JSON 才算步骤文件，
# 其余 .json（人工 dump、旁路产物）一律跳过 —— 否则一份顶层是数组的杂项 JSON
# 就能把整次运行的账本判成「不可信」，连 final.json 都读不到、发布永久被拒。
_STEP_FILE_RE = re.compile(r"^\d+_.+\.json$")

# 允许发布的运行级状态。degraded = 链路完整跑完但有**非阻断**失败（降级可见、发布放行）。
_RELEASABLE_RUN_STATUSES = (C.STATUS_SUCCESS, C.STATUS_SKIPPED, C.STATUS_DEGRADED)
# 历史人工补丁造过的运行状态值（见 data/runs/patch_final_*.py）：按降级处理但强制告警
_LEGACY_DEGRADED_RUN_STATUSES = ("completed_with_errors",)

# 各工作流的「关键阶段」：这几步一旦没跑成，看板就不该发出去。
# 2026-10-02 修 FIX-3：原先是全局常量且四个 id 全是 evening 的，而 daily 用的是
# wechat_pull / wechat_summary → daily 侧的关键步骤检查一次都不命中，保护形同虚设。
CRITICAL_BY_WORKFLOW = {
    "evening": ("wechat_collect", "wxmedia_ocr", "wxmatch_scan", "evening_write"),
    "daily": ("seatable_sync", "cockpit"),
}
# 兜底：workflow 未知时用并集（fail-closed，宁可严）
CRITICAL_STEP_IDS = tuple(sorted({s for v in CRITICAL_BY_WORKFLOW.values() for s in v}))


def _critical_for(final: Optional[Mapping[str, Any]],
                  critical: Optional[Sequence[str]] = None) -> tuple:
    """按账本的 workflow 字段取该链路的关键阶段；显式传入的 critical 优先。"""
    if critical is not None:
        return tuple(critical)
    wf = str((final or {}).get("workflow") or "")
    return CRITICAL_BY_WORKFLOW.get(wf, CRITICAL_STEP_IDS)


_RUN_NAME_RE = re.compile(r"-(\d{8})-(\d{4})")


def run_sort_key(run_dir: str) -> tuple:
    """run 目录的「时间」排序键。

    2026-10-02 修 FIX-5：原 ``find_run_dir("latest")`` 用 ``sorted(os.listdir())`` 取末个
    —— 那是**目录名字典序**，而 ``daily-`` < ``evening-``，只要存在任何 evening 目录，
    latest 就永远命中 evening，无视当天更晚生成的 daily。
    直接后果：daily 的发布命令不带 ``--gate``（走默认 latest）→ 拿到 evening 的账本
    → 待发布文件的哈希与它绑定不上 → **结构性必然拒绝发布**（实测 2026-09-29 起）。
    规范命名的目录按不可变的内嵌时间戳排；不规范的用 mtime 兜底且排在后面。
    """
    name = os.path.basename(os.path.normpath(run_dir))
    m = _RUN_NAME_RE.search(name)
    if m:
        return (1, m.group(1) + m.group(2), 0.0)
    try:
        mt = os.path.getmtime(run_dir)
    except OSError:
        mt = 0.0
    return (0, "", mt)

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
    return any(fn.endswith(".json") and fn not in _NON_STEP_JSON
               for fn in os.listdir(run_dir))


def find_run_dir(run_id: str, runs_dir: str = "") -> str:
    """解析 run_id（支持 latest / 空）→ 运行目录绝对路径。

    ``latest`` = **时间上最新**的一次运行（2026-10-02 修 FIX-5），不是目录名字典序。
    """
    rd = runs_dir or default_runs_dir()
    if run_id in ("", "latest"):
        cands: list[str] = []
        if os.path.isdir(rd):
            for name in os.listdir(rd):
                d = os.path.join(rd, name)
                if _has_ledger(d):
                    cands.append(d)
        return max(cands, key=run_sort_key) if cands else ""
    return os.path.join(rd, run_id)


def load_steps(run_dir: str) -> tuple[Optional[dict], list, str]:
    """读取一次运行的步骤账本。

    返回 ``(final, steps, error)``：``final`` 为 final.json（可能为 None），
    ``steps`` 为步骤记录列表（final.json 里的记录优先，因为它是汇总后的权威
    版本），``error`` 非空表示账本不可信、必须阻断。

    **只有 ``NN_<step>.json`` 才算步骤文件**（2026-10-02 修 FIX-10）。
    这是 runner 的落盘契约（``_step_file`` / ``cmd_note`` 都按 ``%02d_%s.json`` 写），
    因此它有资格 fail-closed：拿不到 step_id、或内容不是表结构 → 账本不可信。
    其它任何 .json 一律**跳过**，绝不能让它们把整次运行判死 ——
    实测事故：`data/runs/daily-20260930-1903-6c9e/` 里被手工丢进一份
    `unsent_outbox.json`（顶层是数组），旧实现遍历到它就 `return error`，
    于是该次运行**连 final.json 都没被读到**、11 个完好步骤文件全部作废、
    门禁永久拒绝发布（「本次未上传任何内容」）。这与 FIX-2 是同一类：
    一个非步骤 JSON = 整条发布链被带走。
    """
    if not os.path.isdir(run_dir):
        return None, [], "运行目录不存在：%s" % run_dir
    merged: dict[str, Any] = {}
    order: list[str] = []
    for fn in sorted(os.listdir(run_dir)):
        if not fn.endswith(".json") or fn in _NON_STEP_JSON:
            continue
        if not _STEP_FILE_RE.match(fn):
            continue          # 非步骤 JSON（人工 dump / 旁路产物）→ 跳过，不判死
        path = os.path.join(run_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return None, [], "步骤账本 %s 无法解析：%s" % (fn, e)
        # 到这里文件一定匹配 NN_ —— 内容不合契约就是账本不可信（fail-closed）。
        if not isinstance(data, Mapping):
            return None, [], "步骤账本 %s 不是表结构（NN_ 前缀的文件必须是步骤记录）" % fn
        # step_id：优先取记录里的字段，其次从 ``NN_<step>.json`` 文件名推。
        # 推导出的 id 要**写回记录** —— 否则下游 evaluate_steps 读不到 step_id，
        # 又会判「存在缺少 step_id 的步骤记录」把发布误杀掉（FIX-2 的同一个坑）。
        sid = str(data.get("step_id") or "").strip()
        if not sid:
            sid = fn[:-5].split("_", 1)[-1].strip()
        if not sid:
            return None, [], "步骤账本 %s 既没有 step_id 字段、也无法从文件名推导" % fn
        rec = dict(data)
        rec["step_id"] = sid
        if sid not in merged:
            order.append(sid)
        merged[sid] = rec
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
                  critical: Optional[Sequence[str]] = None) -> GateResult:
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
                   critical: Optional[Sequence[str]] = None) -> GateResult:
    """对「按步骤文件合并出来的账本」做门禁判定（fail-closed 校验）。

    ``critical`` 留空时**按账本的 workflow 字段**取该链路的关键阶段（2026-10-02 修 FIX-3）。
    ⚠️ 默认值必须是 ``None`` 而不是常量 —— Python 的默认参数在**定义时**求值，
    写常量会让「按 workflow 取」永远不生效。
    """
    reasons: list[str] = []
    warnings: list[str] = []
    checked: dict[str, Any] = {}
    critical = _critical_for(final, critical)

    if final is not None:
        f_status = str(final.get("status") or "")
        if f_status in _LEGACY_DEGRADED_RUN_STATUSES:
            warnings.append("整次运行状态为 %s —— 该值不是 runner 产出的（历史人工补丁），"
                            "已按降级处理，账本可信度存疑" % f_status)
        elif f_status == C.STATUS_DEGRADED:
            # 降级必须可见：放行，但要把「有非阻断失败」摆到台面上
            warnings.append("整次运行存在非阻断失败（degraded）—— 发布放行，"
                            "但部分数据可能陈旧，须在播报中点名是哪一步")
        elif f_status and f_status not in _RELEASABLE_RUN_STATUSES:
            reasons.append("整次运行状态为 %s，不能发布" % f_status)
        f_run = str(final.get("run_id") or "")
        if not f_run:
            reasons.append("final.json 缺少 run_id —— 账本与本次运行无法对应")
        elif run_id and f_run != run_id:
            reasons.append("账本 run_id=%s 与本次运行的 %s 不一致" % (f_run, run_id))

        # 账本自洽性（2026-10-02 加 FIX-8）：status 必须等于「按步骤重算」的结果。
        # 实测 2026-10-02 晚的 final.json：11 步里 partdb_snap 为 failed(blocking=False)，
        # 而 status 写的是 success —— 用仓库内三条代码路径（旧 runner / 新 runner /
        # cmd_note）**没有一条**能算出该值；且该文件 mtime 比 finished_at 晚 3.5 分钟
        # （运行早已结束），说明是事后人工/脚本改写。这是 data/runs/patch_final_*.py
        # 同一手法的第 4 例，效果是让「非阻断失败」在驾驶舱里彻底消失（假绿）。
        # 处置：只告警不拦 —— publish 步骤跑在 final.json 落盘**之前**，历史账本也
        # 大量是人工补的，拦会误伤正常发布；但告警会把「人工改写」摆到台面上。
        # 注意：真正危险的「阻断型失败被写成 success」已由上面对每一步的检查拦下，
        # 本项兜住的是「success ← degraded」这一档无声降级。
        want = C.run_status(steps)
        if f_status and f_status != want:
            warnings.append(
                "账本 status=%s 与按步骤重算的 %s 不一致 —— 疑似人工改写，请复核"
                % (f_status, want))
            checked["status_recomputed"] = want

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
                reasons.append("步骤 %s 失败：%s"
                               % (sid, (s.get("error") or "").replace("\n", " ")[:140]))
            else:
                warnings.append("步骤 %s 失败但不阻断发布（%s）：%s"
                                % (sid, s.get("side_effect") or "非关键",
                                   (s.get("error") or "").replace("\n", " ")[:100]))
        for crit in critical:
            if sid == crit:
                if status in (C.STATUS_FAILED, C.STATUS_BLOCKED):
                    reasons.append("关键步骤 %s 未成功（%s）：%s"
                                   % (sid, status,
                                      (s.get("error") or "").replace("\n", " ")[:140]))
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
             critical: Optional[Sequence[str]] = None,
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


__all__ = ["CRITICAL_STEP_IDS", "CRITICAL_BY_WORKFLOW", "GateResult",
           "default_runs_dir", "find_run_dir", "run_sort_key",
           "evaluate", "evaluate_dict", "evaluate_steps", "load_steps",
           "artifact_hashes", "bind_artifact", "sha256_file"]
