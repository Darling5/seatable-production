# -*- coding: utf-8 -*-
"""application/project_brain/ids.py — 统一 ID 生成与校验。

格式与第一期 ``application/contracts.py`` 的 ``new_object_id`` 保持一致：
``PREFIX-YYYYMMDD-XXXX``。本模块是它的**超集**（多了 plan/action/evidence/
decision/snapshot/reminder 等前缀），第一期原有前缀全部沿用，互不冲突。

为什么 ID 必须自成一格：名称会改（「客户B」可能被写成「客户B」「ZZYF」），
ID 不会。所有关联、去重、冲突判定都以 ID 为准，名称只用于展示。
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid

KIND_PREFIX = {
    "project": "PRJ",
    "plan": "PLN",
    "action": "ACT",
    "evidence": "EVD",
    "decision": "DEC",
    "memory": "MEM",
    "observation": "OBS",
    "fact": "FCT",
    "commitment": "CMT",
    "prediction": "PRD",
    "message": "MSG",
    "reminder": "RMD",
    "snapshot": "SNP",
    "run": "RUN",
    "event": "EVT",
}

# 记忆类型 → 前缀（观察/事实/承诺/决策/预测各有自己的编号段，便于肉眼分流）
MEMORY_KIND_PREFIX = {
    "observation": "OBS",
    "fact": "FCT",
    "commitment": "CMT",
    "decision": "DEC",
    "prediction": "PRD",
}


def new_id(kind: str, now: _dt.datetime | None = None, seq: int = 0) -> str:
    """生成 ``PREFIX-YYYYMMDD-XXXX`` 形式的稳定 ID。

    ``seq=0`` 时用 4 位随机后缀（并发安全）；指定 seq 时用零填充序号
    （便于人工写样例与测试断言）。
    """
    prefix = KIND_PREFIX.get(str(kind).lower())
    if not prefix:
        raise ValueError("未知 ID 类型：%r（合法：%s）"
                         % (kind, "|".join(sorted(KIND_PREFIX))))
    now = now or _dt.datetime.now()
    tail = "%04d" % int(seq) if seq else uuid.uuid4().hex[:4]
    return "%s-%s-%s" % (prefix, now.strftime("%Y%m%d"), tail)


def memory_id(kind: str, now: _dt.datetime | None = None, seq: int = 0) -> str:
    """按记忆类型生成 ID（决策类同时就是 decision_id）。"""
    prefix = MEMORY_KIND_PREFIX.get(str(kind).lower())
    if not prefix:
        raise ValueError("未知记忆类型：%r（合法：%s）"
                         % (kind, "|".join(sorted(MEMORY_KIND_PREFIX))))
    return new_id(str(kind).lower(), now=now, seq=seq)


def validate_id(kind: str, value: str) -> bool:
    """校验 ``PREFIX-YYYYMMDD-XXXX`` 且前缀匹配该类型。"""
    prefix = KIND_PREFIX.get(str(kind).lower())
    if not prefix or not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"%s-\d{8}-[0-9A-Za-z]{4}" % re.escape(prefix), value))


def prefix_of(value: str) -> str:
    """取出 ID 的前缀（用于「这个 ID 是什么类型」的快速判定）。"""
    return str(value or "").split("-")[0].upper()


def is_kind(kind: str, value: str) -> bool:
    return validate_id(kind, value)


__all__ = ["KIND_PREFIX", "MEMORY_KIND_PREFIX", "new_id", "memory_id",
           "validate_id", "prefix_of", "is_kind"]
