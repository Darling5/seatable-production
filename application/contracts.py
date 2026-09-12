# -*- coding: utf-8 -*-
"""application/contracts.py — 替身闭环 v2 执行框架契约（Phase 0）。

定位：纯数据结构 + 常量，**零 I/O、零依赖现有模块**。Phase 1 的 runner、
workflows 与 CLI 都以此为准；在 runner 落地前，本文件被 import 不产生任何副作用。

设计依据：docs/avatar-loop-v2.md（2026-09-12 决策稿）。
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

# ────────────────────────────────────────────────────────────────────
# 运行模式
# ────────────────────────────────────────────────────────────────────
MODE_PREVIEW = "preview"   # 只读/草稿：不产生任何 online_write 与 destructive
MODE_APPLY = "apply"       # 真实执行：local_append / online_write / publish 放行
                          #（destructive 永远额外要求显式 --yes，见 ApprovalGate）

# 步骤副作用分级（每个 Step 必须声明其一）
SIDE_READ_ONLY = "read_only"        # 只读，例如 lookup、foresee --json
SIDE_LOCAL_APPEND = "local_append"  # 写本地快照/台账/事件（可重放、可清理）
SIDE_ONLINE_WRITE = "online_write"  # 写 SeaTable / CRM / PartDB
SIDE_DESTRUCTIVE = "destructive"    # 删除文件、清证据（永远 approval_required）
SIDE_PUBLISH = "publish"            # 对外发布驾驶舱 / 稳定链接

# 写入策略（v1.9 原则的升格版，来自用户宗旨 2026-09-11）
WRITE_APPROVAL_REQUIRED = "approval_required"  # production / tasks：候选制，人工点头才写
WRITE_AUTO_WITH_LEDGER = "auto_with_ledger"    # crm：自动写入 + 台账核对 + 可撤销

ROUTE_POLICIES: Mapping[str, str] = {
    "production": WRITE_APPROVAL_REQUIRED,
    "tasks": WRITE_APPROVAL_REQUIRED,
    "crm": WRITE_AUTO_WITH_LEDGER,
}

# 步骤终态
STATUS_SUCCESS = "success"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"   # 前置失败或等待人工确认


# ────────────────────────────────────────────────────────────────────
# 核心数据结构
# ────────────────────────────────────────────────────────────────────
def new_run_id(workflow: str, now: Optional[_dt.datetime] = None) -> str:
    """daily-20260912-0900-ab12 形式：工作流-日期-时间-随机后缀。"""
    now = now or _dt.datetime.now()
    return "%s-%s-%s-%s" % (
        workflow, now.strftime("%Y%m%d"), now.strftime("%H%M"), uuid.uuid4().hex[:4])


@dataclass
class RunContext:
    """一次工作流运行的统一上下文：所有步骤共享，禁止各自推导路径。"""
    run_id: str
    workflow: str
    actor: str = "automation"
    mode: str = MODE_PREVIEW          # preview / apply
    yes: bool = False                 # 显式确认（destructive 必需）
    dry_run: bool = False             # 业务级 dry-run（如 market sync --dry-run）
    base_name: Optional[str] = None   # production / tasks / crm / None
    skill_dir: str = ""
    data_dir: str = ""
    config_path: str = ""
    resume_of: Optional[str] = None   # 续跑的目标 run_id

    @property
    def is_apply(self) -> bool:
        return self.mode == MODE_APPLY

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class StepResult:
    """单步结构化结果：AI 播报只读这里，不再解析终端文本。"""
    step_id: str
    status: str = STATUS_BLOCKED
    started_at: str = ""
    finished_at: str = ""
    counts: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)   # 本步产物路径
    warnings: list = field(default_factory=list)
    error: Optional[str] = None
    writes: list = field(default_factory=list)      # [{table,row_id,action,customer,...}]

    def ok(self, counts: Optional[dict] = None, **kw) -> "StepResult":
        self.status = STATUS_SUCCESS
        if counts:
            self.counts.update(counts)
        for k, v in kw.items():
            setattr(self, k, v)
        return self

    def fail(self, error: str, **kw) -> "StepResult":
        self.status = STATUS_FAILED
        self.error = error
        for k, v in kw.items():
            setattr(self, k, v)
        return self

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class StepSpec:
    """步骤声明：runner 按 depends_on 组 DAG，按 side_effect 管权限。"""
    id: str
    name: str
    run: Callable[[RunContext], StepResult]
    depends_on: Sequence[str] = ()
    side_effect: str = SIDE_READ_ONLY
    write_mode: Optional[str] = None       # production/tasks/crm → ROUTE_POLICIES
    retry: int = 0
    failure_policy: str = "continue"       # continue / abort
    idempotency_key: Optional[str] = None  # 如 "wechat:{date}:{bookmark}"

    def allowed_in(self, ctx: RunContext) -> tuple[bool, str]:
        """运行前统一裁决：mode / approval / destructive 三道闸。"""
        if self.side_effect == SIDE_DESTRUCTIVE and not ctx.yes:
            return False, "destructive 步骤需要显式 --yes"
        if self.side_effect in (SIDE_ONLINE_WRITE, SIDE_DESTRUCTIVE) and not ctx.is_apply:
            return False, "写入/删除类步骤在 preview 模式下被拦截（用 --mode apply）"
        if self.side_effect == SIDE_ONLINE_WRITE and self.write_mode == WRITE_APPROVAL_REQUIRED \
                and not ctx.yes:
            return False, "approval_required 写入需要人工确认（--yes 或改走候选队列）"
        return True, ""


@dataclass
class ApprovalRequest:
    """人工确认闸门：高风险变更的唯一入口（升级自 intake.py 的 CONFIRM）。"""
    approval_id: str
    object_type: str          # 项目 / 报价 / 合同 / 发货 ...
    object_id: str
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    reason: str = ""
    requester: str = "automation"
    approver: str = ""
    status: str = "pending"   # pending / approved / rejected
    approved_at: str = ""
    source_event_ids: list = field(default_factory=list)  # 证据反查

    def render(self) -> str:
        """给人看的确认清单（延续 v1 「列出改动前后，等点头」原则）。"""
        lines = ["【需要你确认】%s %s" % (self.object_type, self.object_id)]
        keys = list(dict.fromkeys(list(self.before) + list(self.after)))
        for k in keys:
            lines.append("  %s：%s → %s" % (k, self.before.get(k, "(空)"), self.after.get(k, "(空)")))
        if self.reason:
            lines.append("  依据：%s" % self.reason)
        return "\n".join(lines)


@dataclass
class RunResult:
    """整次运行的最终结果：落 data/runs/<run_id>/final.json，供 AI 播报。"""
    run_id: str
    workflow: str
    status: str = STATUS_BLOCKED
    steps: list = field(default_factory=list)   # [StepResult.to_dict()]
    approvals: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def summary(self) -> dict:
        ok = [s for s in self.steps if s.get("status") == STATUS_SUCCESS]
        return {"total": len(self.steps), "success": len(ok),
                "failed": sum(1 for s in self.steps if s.get("status") == STATUS_FAILED),
                "skipped": sum(1 for s in self.steps if s.get("status") == STATUS_SKIPPED),
                "blocked": sum(1 for s in self.steps if s.get("status") == STATUS_BLOCKED)}

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "workflow": self.workflow,
                "status": self.status, "steps": self.steps,
                "approvals": self.approvals, "summary": self.summary,
                "started_at": self.started_at, "finished_at": self.finished_at}


# ────────────────────────────────────────────────────────────────────
# 业务对象 ID（§1：ID 是主键，名称只是展示）
# ────────────────────────────────────────────────────────────────────
_ID_PREFIX = {
    "customer": "CUS", "lead": "LED", "opportunity": "OPP", "requirement": "REQ",
    "solution": "SOL", "quote": "QUO", "contract": "CTR", "project": "PRJ",
    "production_order": "MO", "purchase_order": "PO", "shipment": "SHP",
    "after_sales": "AS",
}


def new_object_id(kind: str, seq: int = 0, now: Optional[_dt.datetime] = None) -> str:
    """生成稳定业务 ID：PRJ-20260912-0007（seq=0 时用随机后缀）。"""
    prefix = _ID_PREFIX.get(str(kind).lower())
    if not prefix:
        raise ValueError("未知对象类型：%r（合法：%s）" % (kind, "|".join(sorted(_ID_PREFIX))))
    now = now or _dt.datetime.now()
    tail = "%04d" % seq if seq else uuid.uuid4().hex[:4]
    return "%s-%s-%s" % (prefix, now.strftime("%Y%m%d"), tail)


# 项目主线状态机（§2；与 生产计划.工序 的映射归 domain/stage_policy）
PROJECT_STATES = (
    "lead", "opportunity", "requirement_confirming", "solution_confirming",
    "quotation_confirming", "contract_pending", "won_and_funded",
    "procurement", "in_production", "quality_check", "ready_to_ship",
    "delivering", "acceptance", "closed",
    # 旁路
    "cancelled", "after_sales",
)

_LEGAL_TRANSITIONS: dict[str, set[str]] = {}


def _build_transitions() -> None:
    """主链状态间跳步/回退一律放行（现实确有加急和返工），但：
    - closed 只能进 after_sales；after_sales ↔ closed；
    - cancelled 只能进不能出；
    跳步/回退的「可见性」由 transition_severity() 标注，不靠阻断。
    """
    main = list(PROJECT_STATES[:14])  # lead … closed
    _POST_DELIVERY = {"ready_to_ship", "delivering", "acceptance"}
    for s in main:
        if s == "closed":
            _LEGAL_TRANSITIONS[s] = {"after_sales"}
        else:
            # 主链任意跳转合法（跳步/回退靠 severity 标注，不阻断）
            targets = set(main) - {s, "after_sales"}
            if s != "acceptance":
                targets.discard("closed")      # 结案只能从验收进入
            targets.add("cancelled")
            if s in _POST_DELIVERY:
                targets.add("after_sales")     # 售后只在交付后可达
            _LEGAL_TRANSITIONS[s] = targets
    _LEGAL_TRANSITIONS["after_sales"] = {"closed"}


_build_transitions()


def can_transition(old: str, new: str) -> bool:
    """状态迁移合法性。跳步/回退不阻断（现实中确有加急返工），但必须可见。"""
    return new in _LEGAL_TRANSITIONS.get(old, set())


def transition_severity(old: str, new: str) -> str:
    """normal / skip / rollback / illegal，供播报与审批分级。

    设计原则（承接 intake.py v1）：跳步和回退不被阻断（现实确有加急和返工），
    但必须让人看见——所以合法 ≠ normal，跳步/回退单独标注。
    """
    if not can_transition(old, new):
        return "illegal"
    if old in ("cancelled",):
        return "normal"
    if new == "cancelled" or old == "closed":
        return "normal"
    order = list(PROJECT_STATES[:14])
    i = order.index(old) if old in order else -1
    j = order.index(new) if new in order else -1
    if i < 0 or j < 0:          # after_sales 等旁路
        return "normal"
    if j == i + 1:
        return "normal"
    return "skip" if j > i else "rollback"


__all__ = [
    "MODE_PREVIEW", "MODE_APPLY",
    "SIDE_READ_ONLY", "SIDE_LOCAL_APPEND", "SIDE_ONLINE_WRITE",
    "SIDE_DESTRUCTIVE", "SIDE_PUBLISH",
    "WRITE_APPROVAL_REQUIRED", "WRITE_AUTO_WITH_LEDGER", "ROUTE_POLICIES",
    "STATUS_SUCCESS", "STATUS_SKIPPED", "STATUS_FAILED", "STATUS_BLOCKED",
    "new_run_id", "RunContext", "StepResult", "StepSpec", "ApprovalRequest", "RunResult",
    "new_object_id", "PROJECT_STATES", "can_transition", "transition_severity",
]
