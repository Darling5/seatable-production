# -*- coding: utf-8 -*-
"""application/project_brain/memory.py — 项目记忆：五类分家 + 去重 + 冲突 + 纠正。

━━ 五类为什么必须分开 ━━
「老王说下周到货」和「发货清单显示 9/25 已到货」在数据库里都是「一句话」，
但它们的可信度差着一整个量级。把两者塞进同一个 notes 字段，等于把
「听说」洗白成「事实」——这正是业主说的「不能把已发货当已到货」
在记忆层的同一个病根。

所以：

  observation  观察     —— 未经核实，可能是错的；播报时必须带「(未核实)」
  fact         已核实事实 —— 有证据链接 + 有核实人；才允许当事实用
  commitment   承诺     —— 谁答应在何时做什么；逾期即成为「谁没做到」的依据
  decision     决策     —— 带理由（rationale），回答「当时为什么这么定」
  prediction   预测     —— 对未来的判断，带依据与置信度；错了要能被复盘

━━ 三条硬约束 ━━
  1. **来源消息去重**：同一来源消息 + 同一断言 ⇒ 同一条记忆，不重复建；
  2. **冲突展示**：同一 aspect_key 出现互斥取值 ⇒ 两条都留，标成冲突，
     让人来判断，程序不替人裁决；
  3. **追加纠正**：纠正 = 新写一条 supersedes 旧条，旧条标 superseded，
     旧值原样留在事件流里 —— 任何时候都能回答「当初记的是什么」。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from typing import Any, Iterable, Mapping, Optional

from . import ids
from . import schema as S

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[，。！？、；：\"'（）()\[\]【】…~·\-—]+")


def normalize_text(text: Any) -> str:
    """归一化文本用于去重比对：去空白、去常见标点、统一小写。

    只用于**判定重复**，绝不用于展示（展示必须原话，原话是第一等资产）。
    """
    t = str(text or "")
    t = _PUNCT.sub("", t)
    t = _WS.sub("", t)
    return t.strip().lower()


def dedupe_key(kind: str, *, source_message_id: str = "",
               source_row_id: str = "", text: str = "",
               project_id: str = "", aspect_key: str = "") -> str:
    """记忆去重键。

    优先用**来源标识**（同一消息/同一行 ⇒ 同一条记忆）；来源缺失时退化到
    内容指纹。键里带 project_id / aspect_key，避免不同项目的同名事项互吞。
    """
    if source_message_id:
        basis = "msg|%s|%s|%s|%s" % (source_message_id, project_id, kind, aspect_key)
    elif source_row_id:
        basis = "row|%s|%s|%s|%s" % (source_row_id, project_id, kind, aspect_key)
    else:
        basis = "txt|%s|%s|%s|%s" % (project_id, kind, aspect_key, normalize_text(text))
    return "MK-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]


def is_active(row: Mapping[str, Any]) -> bool:
    """记忆是否仍生效（未被纠正/未被合并）。"""
    return str(row.get("status") or "active") == "active"


def build_memory_row(
    *,
    kind: str,
    project_id: str,
    text: str,
    plan_id: str = "",
    action_id: str = "",
    evidence_id: str = "",
    decision_id: str = "",
    run_id: str = "",
    snapshot_id: str = "",
    occurred_at: str = "",
    captured_at: str = "",
    source_system: str = "",
    source_base: str = "",
    source_table: str = "",
    source_row_id: str = "",
    source_message_id: str = "",
    actor: str = "",
    aspect_key: str = "",
    value: str = "",
    confidence: Any = "",
    rationale: str = "",
    due_date: str = "",
    corrects_id: str = "",
    now: Optional[_dt.datetime] = None,
    seq: int = 0,
) -> dict:
    """构造一条记忆行（未落盘）。返回的 dict 已含统一字段。"""
    if kind not in S.MEMORY_KINDS:
        raise ValueError("未知记忆类型：%r（合法：%s）"
                         % (kind, "|".join(S.MEMORY_KINDS)))
    now = now or _dt.datetime.now()
    mid = ids.memory_id(kind, now=now, seq=seq)
    row = {
        # ── 统一字段 ──
        "project_id": project_id,
        "plan_id": plan_id,
        "action_id": action_id,
        "evidence_id": evidence_id,
        "decision_id": decision_id or (mid if kind == S.KIND_DECISION else ""),
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "version": 1,
        # ── 身份 ──
        "memory_id": mid,
        "kind": kind,
        "kind_cn": S.MEMORY_KIND_CN[kind],
        "text": str(text or "").strip(),
        # ── 时间：发生 ≠ 采集 ──
        "occurred_at": occurred_at or "",
        "captured_at": captured_at or now.isoformat(timespec="seconds"),
        # ── 来源溯源（保留系统/Base/表/行 ID）──
        "source_system": source_system,
        "source_base": source_base,
        "source_table": source_table,
        "source_row_id": str(source_row_id or ""),
        "source_message_id": str(source_message_id or ""),
        # ── 业务 ──
        "actor": actor,
        "aspect_key": aspect_key,
        "value": value,
        "confidence": confidence,
        "rationale": rationale,
        "due_date": due_date,
        "status": "active",
        "corrects_id": corrects_id,
        "dedupe_key": dedupe_key(kind, source_message_id=source_message_id,
                                 source_row_id=str(source_row_id or ""),
                                 text=text, project_id=project_id,
                                 aspect_key=aspect_key),
    }
    return row


# ────────────────────────────────────────────────────────────────────
# 冲突检测
# ────────────────────────────────────────────────────────────────────
def find_conflicts(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """找出同一 aspect_key 下的互斥取值。

    只对带 ``aspect_key`` + ``value`` 的记忆生效；同一 key 只要出现 **两个
    以上不同 value** 就是冲突 —— 无论它们是观察还是事实。

    设计取舍：**不自动取最新值**。冲突往往意味着「有人改口」或者「有两条
    数据源打架」，两者的处置方式完全不同，只有人能分清。
    """
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        if not is_active(r):
            continue
        key = str(r.get("aspect_key") or "").strip()
        val = str(r.get("value") or "").strip()
        if not key or not val:
            continue
        buckets.setdefault("%s::%s" % (r.get("project_id", ""), key), []).append(dict(r))

    out: list[dict] = []
    for gkey, items in buckets.items():
        values = {str(i.get("value")) for i in items}
        if len(values) < 2:
            continue
        items.sort(key=lambda r: str(r.get("occurred_at") or r.get("captured_at") or ""))
        out.append({
            "conflict_id": "CFL-" + hashlib.sha1(gkey.encode("utf-8")).hexdigest()[:8],
            "project_id": items[0].get("project_id", ""),
            "aspect_key": items[0].get("aspect_key", ""),
            "values": sorted(values),
            "members": [
                {"memory_id": i.get("memory_id"), "kind": i.get("kind"),
                 "kind_cn": i.get("kind_cn"), "value": i.get("value"),
                 "text": i.get("text"), "occurred_at": i.get("occurred_at"),
                 "captured_at": i.get("captured_at"),
                 "source_system": i.get("source_system"),
                 "source_table": i.get("source_table"),
                 "source_row_id": i.get("source_row_id"),
                 "evidence_id": i.get("evidence_id")}
                for i in items
            ],
            "resolved": False,
            "hint": "两条来源说法不一致，请人工确认哪条为准（程序不替人裁决）",
        })
    return out


def supersede(old_row: Mapping[str, Any]) -> dict:
    """把被纠正的旧记忆标为 superseded 的**更新载荷**（不改历史，只追加新版本）。"""
    return {
        "status": "superseded",
        "superseded_by": "",
    }


__all__ = ["normalize_text", "dedupe_key", "is_active", "build_memory_row",
           "find_conflicts", "supersede", "S"]
