# -*- coding: utf-8 -*-
"""application/project_brain/schema.py — 单项目第二大脑 v1 契约常量。

定位：**纯常量 + 纯函数，零 I/O、零业务依赖**。契约文档
``docs/contracts/project-brain-v1.md`` 与第三期读的合成样例都以此为准。

设计铁律（与第一期可信执行层一致）：
  · ID 是主键，**名称只是展示**；来源系统/Base/表/行 ID 一律原样保留；
  · 记忆按类型分家（观察 ≠ 已核实事实），不允许把「听说」混进「事实」；
  · 所有记录 append-only，纠正靠追加新记录 + supersedes 指针，永不覆盖历史。
"""
from __future__ import annotations

# ────────────────────────────────────────────────────────────────────
# 契约版本
# ────────────────────────────────────────────────────────────────────
BRAIN_CONTRACT_VERSION = "project-brain-v1"

# 统一字段（八个，第三期按这八个对齐）
UNIFIED_FIELDS = (
    "project_id", "plan_id", "action_id", "evidence_id",
    "decision_id", "run_id", "snapshot_id", "version",
)

# 来源溯源字段：不以名称做主键，但必须能回到来源那一行
PROVENANCE_FIELDS = (
    "source_system",    # wechat / seatable / manual / partdb / mpp / doc
    "source_base",      # production / tasks / crm / local
    "source_table",     # 表名（仅作展示与定位用）
    "source_row_id",    # 行 ID —— 真正的主键
    "source_message_id",  # 来源消息唯一键，用于采集去重
)

# 记忆采集/发生时间：两个时间必须分开记，否则「相对日期」无从还原
TIME_FIELDS = (
    "occurred_at",   # 事情发生时间（消息里说的时间）
    "captured_at",   # 系统采集时间（我们拿到它的时间）
)

# ────────────────────────────────────────────────────────────────────
# 记忆类型（五类，各自独立存储，不允许混为一个 "notes" 字段）
# ────────────────────────────────────────────────────────────────────
KIND_OBSERVATION = "observation"    # 观察：未经核实的感知，可能是错的
KIND_FACT = "fact"                  # 已核实事实：有证据并经核实
KIND_COMMITMENT = "commitment"      # 承诺：谁答应在何时做什么
KIND_DECISION = "decision"          # 决策：含理由（为什么这么定）
KIND_PREDICTION = "prediction"      # 预测：对未来的判断，含依据与置信度

MEMORY_KINDS = (KIND_OBSERVATION, KIND_FACT, KIND_COMMITMENT,
                KIND_DECISION, KIND_PREDICTION)

MEMORY_KIND_CN = {
    KIND_OBSERVATION: "观察",
    KIND_FACT: "已核实事实",
    KIND_COMMITMENT: "承诺",
    KIND_DECISION: "决策",
    KIND_PREDICTION: "预测",
}

# 只有 fact 才允许被当作「事实」对外播报；其余四类必须带类型前缀
FACTUAL_KINDS = (KIND_FACT,)

# 记忆生命周期状态（只增不删：superseded 的旧版本仍留在事件流里）
MS_ACTIVE = "active"          # 生效中
MS_CONFIRMED = "confirmed"    # 已核实（观察转事实后置为此态）
MS_SUPERSEDED = "superseded"  # 已被纠正/取代

MEMORY_STATUSES = (MS_ACTIVE, MS_CONFIRMED, MS_SUPERSEDED)

# ────────────────────────────────────────────────────────────────────
# 证据
# ────────────────────────────────────────────────────────────────────
EVIDENCE_KINDS = (
    "message",      # 聊天消息原话
    "document",     # 合同/送货单/截图
    "system",       # 业务系统里的行（SeaTable/PartDB/Mpp）
    "shipment",     # 发货记录
    "arrival",      # 到货/收货记录
    "acceptance",   # 验收记录
    "inspection",   # 检验/质检
    "payment",      # 付款/回款
)

# 断言口径：区分「发货」与「到货」，禁止混用（业主点名的坑）
CLAIM_SHIPPED = "shipped"          # 已发货
CLAIM_IN_TRANSIT = "in_transit"    # 在途
CLAIM_ARRIVED = "arrived"          # 已到货
CLAIM_PARTIAL = "partial"          # 部分到货
CLAIM_FULL = "full"                # 齐套到货
CLAIM_ACCEPTED = "accepted"        # 验收通过
CLAIM_REJECTED = "rejected"        # 验收不通过
CLAIM_UNKNOWN = "unknown"

CLAIM_TYPES = (CLAIM_SHIPPED, CLAIM_IN_TRANSIT, CLAIM_ARRIVED,
               CLAIM_PARTIAL, CLAIM_FULL, CLAIM_ACCEPTED,
               CLAIM_REJECTED, CLAIM_UNKNOWN)

CLAIM_CN = {
    CLAIM_SHIPPED: "已发货", CLAIM_IN_TRANSIT: "在途",
    CLAIM_ARRIVED: "已到货", CLAIM_PARTIAL: "部分到货",
    CLAIM_FULL: "齐套到货", CLAIM_ACCEPTED: "验收通过",
    CLAIM_REJECTED: "验收不通过", CLAIM_UNKNOWN: "未知",
}

# 只有这些断言才算「真正到货」
ARRIVAL_CLAIMS = (CLAIM_ARRIVED, CLAIM_PARTIAL, CLAIM_FULL)

# ────────────────────────────────────────────────────────────────────
# 行动闭环状态
# ────────────────────────────────────────────────────────────────────
ST_CANDIDATE = "candidate"                    # 候选
ST_PENDING_CONFIRM = "pending_confirm"        # 待确认
ST_READY = "ready"                            # 待执行
ST_IN_PROGRESS = "in_progress"                # 进行中
ST_BLOCKED = "blocked"                        # 阻塞
ST_PENDING_ACCEPTANCE = "pending_acceptance"  # 待验收
ST_CLOSED = "closed"                          # 已关闭
ST_CANCELLED = "cancelled"                    # 已取消

ACTION_STATES = (ST_CANDIDATE, ST_PENDING_CONFIRM, ST_READY, ST_IN_PROGRESS,
                 ST_BLOCKED, ST_PENDING_ACCEPTANCE, ST_CLOSED, ST_CANCELLED)

ACTION_STATE_CN = {
    ST_CANDIDATE: "候选", ST_PENDING_CONFIRM: "待确认", ST_READY: "待执行",
    ST_IN_PROGRESS: "进行中", ST_BLOCKED: "阻塞", ST_PENDING_ACCEPTANCE: "待验收",
    ST_CLOSED: "已关闭", ST_CANCELLED: "已取消",
}

# 合法迁移。closed / cancelled 是「可以重开」的终态，不是死胡同。
ACTION_TRANSITIONS: dict[str, set[str]] = {
    ST_CANDIDATE: {ST_PENDING_CONFIRM, ST_CANCELLED},
    ST_PENDING_CONFIRM: {ST_READY, ST_CANDIDATE, ST_CANCELLED},
    ST_READY: {ST_IN_PROGRESS, ST_BLOCKED, ST_CANCELLED},
    ST_IN_PROGRESS: {ST_BLOCKED, ST_PENDING_ACCEPTANCE, ST_CANCELLED, ST_READY},
    ST_BLOCKED: {ST_IN_PROGRESS, ST_READY, ST_CANCELLED},
    ST_PENDING_ACCEPTANCE: {ST_CLOSED, ST_IN_PROGRESS, ST_BLOCKED},
    ST_CLOSED: {ST_IN_PROGRESS},        # 重开
    ST_CANCELLED: {ST_PENDING_CONFIRM, ST_IN_PROGRESS},   # 重开
}

# 未完结状态（跨天保留、计入今日重点的判定用）
OPEN_STATES = (ST_CANDIDATE, ST_PENDING_CONFIRM, ST_READY,
               ST_IN_PROGRESS, ST_BLOCKED, ST_PENDING_ACCEPTANCE)

# ────────────────────────────────────────────────────────────────────
# 行动类型（决定关闭时用哪套硬规则）
# ────────────────────────────────────────────────────────────────────
AK_GENERAL = "general"        # 一般事项
AK_KITTING = "kitting"        # 齐套（必须齐套到货才能关）
AK_SHIPMENT = "shipment"      # 发货
AK_DELIVERY = "delivery"      # 到货/交付
AK_ACCEPTANCE = "acceptance"  # 验收
AK_PURCHASE = "purchase"      # 采购
AK_PRODUCTION = "production"  # 生产

ACTION_KINDS = (AK_GENERAL, AK_KITTING, AK_SHIPMENT, AK_DELIVERY,
                AK_ACCEPTANCE, AK_PURCHASE, AK_PRODUCTION)

ACTION_KIND_CN = {
    AK_GENERAL: "一般事项", AK_KITTING: "齐套", AK_SHIPMENT: "发货",
    AK_DELIVERY: "到货", AK_ACCEPTANCE: "验收", AK_PURCHASE: "采购",
    AK_PRODUCTION: "生产",
}

# 需要「真到货」才能关闭的行动类型：发货 ≠ 到货
NEEDS_ARRIVAL_KINDS = (AK_KITTING, AK_DELIVERY, AK_ACCEPTANCE)

# ────────────────────────────────────────────────────────────────────
# 本地记忆表（本模块的持久化载体；**不是**真实业务表）
#
# 本轮明确不写真实业务表、不改云表结构。这些表只是一组本地 JSONL 文件名，
# 经 LocalMemoryAdapter 暴露成和 SeaTable 一样的 list_rows/append_row 接口，
# 从而让第一期的 DataService（授权 + 幂等 + 读回验证 + 台账）**原样复用**。
# ────────────────────────────────────────────────────────────────────
TB_PROJECT = "项目"
TB_MESSAGE = "来源消息"
TB_EVIDENCE = "证据"
TB_MEMORY = "项目记忆"
TB_ACTION = "行动项"
TB_ACTION_EVENT = "行动事件"
TB_REMINDER = "提醒"

TABLE_FILES = {
    TB_PROJECT: "projects.jsonl",
    TB_MESSAGE: "messages.jsonl",
    TB_EVIDENCE: "evidence.jsonl",
    TB_MEMORY: "memory.jsonl",
    TB_ACTION: "actions.jsonl",
    TB_ACTION_EVENT: "action_events.jsonl",
    TB_REMINDER: "reminders.jsonl",
}

TABLE_NAMES = tuple(TABLE_FILES)

# 内部字段前缀：不参与读回验证，也不进台账摘要
INTERNAL_PREFIX = "__"
F_VERSION = "__version__"
F_EXPECTED_VERSION = "__expected_version__"
F_ROW_ID = "__row_id__"
F_OP = "__op__"
F_SEQ = "__seq__"
F_AT = "__at__"
F_REASON = "__reason__"
F_ACTOR = "__actor__"

# 授权：本地记忆写入走 tasks 路由（approval_required）——与第一期同一套策略
WRITE_ROUTE = "tasks"

# 默认来源时效阈值（小时）：超过就标 stale，并计入信息缺口
DEFAULT_STALE_AFTER_HOURS = 24

# 允许被授权覆盖的动作（与 WriteGrant.actions 对齐）
ACTION_APPEND = "append"
ACTION_UPDATE = "update"


def is_internal(key: str) -> bool:
    """内部字段判定：双下划线开头一律不落业务列。"""
    return isinstance(key, str) and key.startswith(INTERNAL_PREFIX)


def public_fields(row: dict) -> dict:
    """剥掉内部字段，返回纯业务字段。"""
    return {k: v for k, v in to_items(row) if not is_internal(k)}


def to_items(row):
    if isinstance(row, dict):
        return list(row.items())
    return list(dict(row or {}).items())


__all__ = [name for name in dir() if not name.startswith("_")]
