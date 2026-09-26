# -*- coding: utf-8 -*-
"""application/project_brain — 单项目「第二大脑」（第二期）。

公开入口：

    from application.project_brain import get_project_context, ProjectBrain

    ctx = get_project_context("PRJ-20260926-0001")            # 不带快照：读最新
    ctx = get_project_context("PRJ-20260926-0001", "SNP-...") # 带快照：可复现

契约文档：``docs/contracts/project-brain-v1.md``
合成样例：``docs/contracts/examples/project-brain-sample.json``

模块划分：
    schema           契约常量（统一字段、五类记忆、行动状态、口径枚举）
    ids              统一 ID 生成/校验（PREFIX-YYYYMMDD-XXXX）
    timeparse        相对日期解析（**基准是消息时间**）
    store            本地 append-only 记忆库（暴露成 adapter 接口）
    memory           五类记忆、去重键、冲突检测、追加纠正
    evidence_ledger  证据构造与口径判定（已发货 ≠ 已到货）
    actions          行动状态机 + 四条业务硬规则 + 提醒/滚动
    context          查询与统一上下文（get_project_context 的实现）
    service          门面：唯一写入口，复用第一期授权闸门 + DataService
"""
from __future__ import annotations

from . import actions, context, evidence_ledger, ids, memory, schema, scenario, timeparse
from .service import (ProjectBrain, get_project_context, message_row_id,
                      reminder_row_id)
from .scenario import replay
from .store import LocalMemoryAdapter, StaleVersionError

__all__ = [
    "ProjectBrain", "get_project_context", "message_row_id", "reminder_row_id",
    "replay", "LocalMemoryAdapter", "StaleVersionError",
    "actions", "context", "evidence_ledger", "ids", "memory", "scenario",
    "schema", "timeparse",
]
