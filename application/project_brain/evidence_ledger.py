# -*- coding: utf-8 -*-
"""application/project_brain/evidence_ledger.py — 证据条目构造与口径判定。

证据是「第二大脑」里唯一有资格支撑结论的东西。一条证据必须回答三件事：
  1. 它**断言了什么**（claim_type：已发货 / 在途 / 已到货 / 部分到货 / 验收通过…）；
  2. 它**从哪来**（source_system / base / table / row_id / message_id）；
  3. 它**什么时候发生、什么时候被采集**（occurred_at / captured_at）。

业主点名两个坑都在这里用类型钉死：

  · 「已发货」不能当作「已到货」—— claim=shipped 属于发运动作，
    与 claim=arrived/partial/full 是不同口径，绝不可互换；
  · 「部分到货」不能关闭齐套行动 —— claim=partial 明确不是齐套。

本模块只做构造与判定，不做 I/O，可离线单测。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from typing import Any, Mapping, Optional, Sequence

from . import ids
from . import schema as S


def evidence_dedupe_key(*, claim_type: str, source_message_id: str = "",
                        source_row_id: str = "", subject: str = "",
                        project_id: str = "", occurred_at: str = "") -> str:
    """同一来源 + 同一口径 ⇒ 同一条证据（重复采集不重复建）。"""
    if source_message_id:
        basis = "msg|%s|%s|%s" % (source_message_id, claim_type, project_id)
    elif source_row_id:
        basis = "row|%s|%s|%s" % (source_row_id, claim_type, project_id)
    else:
        basis = "sub|%s|%s|%s|%s" % (project_id, claim_type, subject, occurred_at)
    return "EK-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]


def build_evidence_row(
    *,
    project_id: str,
    kind: str,
    claim_type: str,
    summary: str = "",
    plan_id: str = "",
    action_id: str = "",
    decision_id: str = "",
    run_id: str = "",
    snapshot_id: str = "",
    subject: str = "",
    quantity: Any = "",
    quantity_total: Any = "",
    unit: str = "",
    occurred_at: str = "",
    captured_at: str = "",
    source_system: str = "",
    source_base: str = "",
    source_table: str = "",
    source_row_id: str = "",
    source_message_id: str = "",
    verified: bool = False,
    verified_by: str = "",
    verified_at: str = "",
    criteria_met: Any = "",
    confidence: Any = "",
    now: Optional[_dt.datetime] = None,
    seq: int = 0,
) -> dict:
    """构造一条证据行（未落盘）。"""
    if kind not in S.EVIDENCE_KINDS:
        raise ValueError("未知证据类型：%r（合法：%s）"
                         % (kind, "|".join(S.EVIDENCE_KINDS)))
    if claim_type not in S.CLAIM_TYPES:
        raise ValueError("未知断言口径：%r（合法：%s）"
                         % (claim_type, "|".join(S.CLAIM_TYPES)))
    now = now or _dt.datetime.now()
    return {
        # ── 统一字段 ──
        "project_id": project_id,
        "plan_id": plan_id,
        "action_id": action_id,
        "evidence_id": ids.new_id("evidence", now=now, seq=seq),
        "decision_id": decision_id,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "version": 1,
        # ── 断言 ──
        "kind": kind,
        "claim_type": claim_type,
        "claim_cn": S.CLAIM_CN.get(claim_type, claim_type),
        "summary": str(summary or "").strip(),
        "subject": subject,
        "quantity": quantity,
        "quantity_total": quantity_total,
        "unit": unit,
        # ── 时间 ──
        "occurred_at": occurred_at or "",
        "captured_at": captured_at or now.isoformat(timespec="seconds"),
        # ── 来源 ──
        "source_system": source_system,
        "source_base": source_base,
        "source_table": source_table,
        "source_row_id": str(source_row_id or ""),
        "source_message_id": str(source_message_id or ""),
        # ── 核实 ──
        "verified": bool(verified),
        "verified_by": verified_by,
        "verified_at": verified_at or (now.isoformat(timespec="seconds") if verified else ""),
        "criteria_met": criteria_met,
        "confidence": confidence,
        "status": "active",
        "dedupe_key": evidence_dedupe_key(
            claim_type=claim_type, source_message_id=source_message_id,
            source_row_id=str(source_row_id or ""), subject=subject,
            project_id=project_id, occurred_at=occurred_at),
    }


# ────────────────────────────────────────────────────────────────────
# 口径判定
# ────────────────────────────────────────────────────────────────────
def is_arrival_evidence(row: Mapping[str, Any]) -> bool:
    """这条证据是否证明「货真的到了」。发货 ≠ 到货。"""
    return str(row.get("claim_type") or "") in S.ARRIVAL_CLAIMS


def is_shipped_only(row: Mapping[str, Any]) -> bool:
    """只证明「已发出/在途」，不证明到货。"""
    return str(row.get("claim_type") or "") in (S.CLAIM_SHIPPED, S.CLAIM_IN_TRANSIT)


def is_acceptance_evidence(row: Mapping[str, Any]) -> bool:
    """是否是验收结论（通过/不通过）。"""
    return (str(row.get("kind") or "") == "acceptance"
            or str(row.get("claim_type") or "") in (S.CLAIM_ACCEPTED, S.CLAIM_REJECTED))


def is_full_arrival(row: Mapping[str, Any]) -> bool:
    """齐套到货：claim=full，或 claim=arrived 且数量未缺。"""
    claim = str(row.get("claim_type") or "")
    if claim == S.CLAIM_FULL:
        return True
    if claim == S.CLAIM_ARRIVED and _qty_complete(row):
        return True
    return False


def _qty_complete(row: Mapping[str, Any]) -> bool:
    """数量口径：有总量且实收 >= 总量 ⇒ 齐套；取不到总量则不判定为齐套。"""
    try:
        got = float(row.get("quantity"))
        total = float(row.get("quantity_total"))
    except (TypeError, ValueError):
        return False
    return total > 0 and got >= total


def evidence_ids_of(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(r.get("evidence_id")) for r in rows if r.get("evidence_id")]


__all__ = ["build_evidence_row", "evidence_dedupe_key", "is_arrival_evidence",
           "is_shipped_only", "is_acceptance_evidence", "is_full_arrival",
           "evidence_ids_of"]
