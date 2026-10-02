# -*- coding: utf-8 -*-
"""application/contracts.py — 替身闭环 v2 执行框架契约（Phase 0）。

定位：纯数据结构 + 常量，**零 I/O、零依赖现有模块**。Phase 1 的 runner、
workflows 与 CLI 都以此为准；在 runner 落地前，本文件被 import 不产生任何副作用。

设计依据：docs/avatar-loop-v2.md（2026-09-12 决策稿）。
"""
from __future__ import annotations

import datetime as _dt
import re
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

# 运行级终态（只用于 RunResult.status，**不是**步骤状态）
# 语义：链路完整跑完，但存在**非阻断**步骤失败 —— 数据降级、发布放行但必须可见。
# 2026-10-02 新增：此前「有降级但仍可发布」没有任何合法表达，
# 只能靠人工改 final.json 绕过门禁（见 data/runs/patch_final_*.py），
# 直接把 failed 改写成 skipped/纯 success，导致驾驶舱看不到真实故障。
STATUS_DEGRADED = "degraded"


def run_status(steps: Sequence[Mapping[str, Any]]) -> str:
    """由步骤记录推导**运行级**终态。这是全仓库唯一一份实现。

    | 情形                                   | 结果                |
    |----------------------------------------|---------------------|
    | 无步骤（DAG 为空 = 配置错误，fail-closed） | ``failed``        |
    | 存在**阻断型**失败（blocking 缺省为真）    | ``failed``        |
    | 只有非阻断失败（降级）                    | ``degraded``      |
    | 其余                                   | ``success``       |

    为什么必须是唯一一份（2026-10-02 修 FIX-6）：
    原先这段判定在 **三处**各自实现过 —— ``runner.py`` 收尾、``gates.py`` 白名单、
    ``workflows/workflow.py::cmd_note``。三处口径不一致，于是：
      · runner 判出的 degraded 会被后跑的 ``cmd_note``（旧口径：任一非 success/skipped
        即 failed）**覆盖回 failed** —— evening 自动化 Prompt 连调三次 ``note``，
        等于每次都在拆掉刚修好的降级；
      · 而 gates 只认 success/skipped，degraded 一出现就被拦。
    收敛到一个函数后，改语义只需改这一处，不会再出现三方打架。
    """
    steps = list(steps or ())
    if not steps:
        return STATUS_FAILED
    bad = [s for s in steps
           if s.get("status") not in (STATUS_SUCCESS, STATUS_SKIPPED)]
    if not bad:
        return STATUS_SUCCESS
    if any(s.get("blocking", True) for s in bad):
        return STATUS_FAILED
    return STATUS_DEGRADED


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
    # 该步失败是否应阻断依赖它的步骤与对外发布。默认 True（失败即阻断）；
    # 只有「数据源偶发不可用、旧快照仍可用」这类步骤才显式声明 False，
    # 让降级成为**真实生效**的降级，而不是嘴上说 continue、实际被门禁拦住。
    blocking: bool = True
    # 本步应当产出的文件（相对 skill_dir 或绝对路径）。runner 会核对存在性并
    # 记录 sha256 到账本，供发布门禁把「要上传的文件」钉死到某一次运行。
    expect_artifacts: Sequence[str] = ()

    def allowed_in(self, ctx: RunContext) -> tuple[bool, str]:
        """运行前统一裁决：mode / approval / destructive 三道闸。"""
        if self.side_effect == SIDE_DESTRUCTIVE and not ctx.yes:
            return False, "destructive 步骤需要显式 --yes"
        if self.side_effect in (SIDE_ONLINE_WRITE, SIDE_DESTRUCTIVE) and not ctx.is_apply:
            return False, "写入/删除类步骤在 preview 模式下被拦截（用 --mode apply）"
        if self.side_effect == SIDE_PUBLISH and not ctx.is_apply:
            return False, "发布类步骤在 preview 模式下被拦截（用 --mode apply）"
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

    def refresh_status(self) -> "RunResult":
        """按 ``run_status()`` 重算整次状态（全仓库唯一口径），返回自身。

        任何**改写 steps 之后**的代码（runner 收尾、``workflow.py note`` 补记 AI 步骤）
        都必须调它来同步 status，禁止再各写一份 ``all(...)`` 判定。
        """
        self.status = run_status(self.steps)
        return self

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "workflow": self.workflow,
                "status": self.status, "steps": self.steps,
                "approvals": self.approvals, "summary": self.summary,
                "started_at": self.started_at, "finished_at": self.finished_at}


# ────────────────────────────────────────────────────────────────────
# 业务对象 ID（§1：ID 是主键，名称只是展示）
#
# ★ 2026-10-03（批次 1 / G29 前置）：**前缀一律 3 个字母**。
#   实测缺口：原先 `production_order` / `purchase_order` / `after_sales` 用的是
#   **2 字母**前缀（MO / PO / AS），而跨层校验器
#   `application/decision_support/alignment.py::is_valid_id` 的 `ID_RE` 要求
#   `[A-Z]{3}` → 这三类业务 ID **被跨层校验直接拒绝**。
#   实测（12 类对象）：仅 `project`(PRJ) 通过，其余 11 类全被拒 ——
#   3 类因形态不符（MO/PO/AS）、8 类因未登记进 `KNOWN_PREFIXES`。
#   批次 1 要建 `PO → GR → 付款单` 这条线，PO 的 ID 必须先跨层合法，故在此收口。
#
#   向后兼容：旧前缀保留在 `LEGACY_ID_PREFIX` 里继续**可校验**（历史数据不失效），
#   但不再用于生成新 ID。legacy 名单是显式且有限的，由测试锁定。
# ────────────────────────────────────────────────────────────────────
_ID_PREFIX = {
    "customer": "CUS", "lead": "LED", "opportunity": "OPP", "requirement": "REQ",
    "solution": "SOL", "quote": "QUO", "contract": "CTR", "project": "PRJ",
    "production_order": "MFO", "purchase_order": "PUR", "shipment": "SHP",
    "after_sales": "AFS",
}

# 历史两字母前缀：只用于**读**旧数据，不再用于生成。
# 例：`MO-20260913-ab12` 是合法历史 ID，改前缀后仍须能通过 validate_object_id，
# 否则「改名把历史数据变成非法数据」。
LEGACY_ID_PREFIX = {
    "production_order": "MO",
    "purchase_order": "PO",
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


def is_canonical_object_id(kind: str, value: str) -> bool:
    """**严格**判定：只认 `_ID_PREFIX` 里的规范前缀，**不接受**历史前缀。

    与 `decision_support.alignment.is_canonical_id` 对称 ——
    两层各有一个「只认规范形态」的入口，给**新写入**用：

        validate_object_id       = 规范 ∪ 历史   → 读旧数据
        is_canonical_object_id   = 规范           → 写新数据

    为什么要有严格版：跨层引用的校验（如 `G23` 要求付款单挂的 PO 号
    必须能与 `L_proj` 对上账）如果连历史形态都放行，
    `PO-…` 这种已废弃形态就会被判成「合法」，于是
    「形态非法」那条原因码**永远取不到** —— 一个写在 `__all__` 里的死分支。
    """
    prefix = _ID_PREFIX.get(str(kind).lower())
    if not prefix or not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"%s-\d{8}-[0-9A-Za-z]{4}" % re.escape(prefix), value))


def validate_object_id(kind: str, value: str) -> bool:
    """校验业务 ID 是否匹配对象类型及 ``PREFIX-YYYYMMDD-XXXX`` 格式。

    接受**规范化前缀**与 ``LEGACY_ID_PREFIX`` 里的历史前缀（只读兼容）。
    需要严格判定（新写入、跨层引用）时用 ``is_canonical_object_id``。
    """
    return (is_canonical_object_id(kind, value)
            or _validate_legacy_object_id(kind, value))


def _validate_legacy_object_id(kind: str, value: str) -> bool:
    """只读兼容：仅认 ``LEGACY_ID_PREFIX`` 里的历史前缀。"""
    prefix = LEGACY_ID_PREFIX.get(str(kind).lower())
    if not prefix or not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"%s-\d{8}-[0-9A-Za-z]{4}" % re.escape(prefix), value))


@dataclass(frozen=True)
class EvidenceLink:
    event_id: str
    intent_id: str = ""
    candidate_id: str = ""
    write_id: str = ""
    related_object_type: str = ""
    related_object_id: str = ""
    source: str = ""
    source_ref: str = ""


@dataclass(frozen=True)
class StateTransition:
    object_type: str
    object_id: str
    from_state: str
    to_state: str
    trigger_event_id: str = ""
    actor: str = ""
    reason: str = ""
    created_at: str = ""

    @property
    def severity(self) -> str:
        return transition_severity(self.from_state, self.to_state)


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
    "STATUS_DEGRADED",
    "run_status",
    "new_run_id", "RunContext", "StepResult", "StepSpec", "ApprovalRequest", "RunResult",
    "new_object_id", "validate_object_id", "is_canonical_object_id",
    "EvidenceLink", "StateTransition",
    # 注：`_ID_PREFIX` 是私有实现细节（下划线开头），**不进公开 API**，
    # 需要它的测试直接按属性名访问。这里只公开历史前缀表。
    "LEGACY_ID_PREFIX",
    "PROJECT_STATES", "can_transition", "transition_severity",
]
