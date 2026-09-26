# -*- coding: utf-8 -*-
"""application/project_brain/scenario.py — 合成项目回放器（离线验收用）。

把一份「合成项目 JSON」按顺序喂给 ``ProjectBrain``，产出每一步的结果日志。
用途有两个，都很实在：

  1. **先跑通合成项目再碰真数据**（业主明确要求的上线顺序）；
  2. 让契约样例**可执行** —— 样例不只是「长这样的 JSON」，而是「跑起来
     必须得到这样的结果」，第三期照着回放即可复现全部结论。

引用解析刻意做成**确定性**的：
  · 行动  → 按 ``project_id + title`` 算出 idem_key 反查（与入库时的键同源）；
  · 记忆  → 按 ``kind + text`` 精确匹配（样例里文本唯一）；
  · 证据  → 用样例里的 ``ref`` 名映射到运行时生成的 evidence_id。
因此回放不依赖随机 ID，重复回放结果一致。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from .. import contracts as C
from . import actions as ACT
from . import memory as MEM
from . import schema as S
from .service import ProjectBrain


def _resolve_action(brain: ProjectBrain, project_id: str, title: str) -> Optional[dict]:
    ikey = ACT.action_idem_key(project_id, title)
    for a in brain.store.list_rows(S.TB_ACTION):
        if str(a.get("idem_key") or "") == ikey:
            return a
    return None


def _resolve_memory(brain: ProjectBrain, kind: str, text: str) -> Optional[dict]:
    for m in brain.store.list_rows(S.TB_MEMORY):
        if str(m.get("kind")) == kind and str(m.get("text")) == text \
                and MEM.is_active(m):
            return m
    return None


def replay(brain: ProjectBrain, scenario: Mapping[str, Any], *,
           mode: str = C.MODE_PREVIEW, grant: Any = None,
           actor: str = "scenario", run_id: str = "") -> dict:
    """按步骤回放一份合成项目；返回 ``{"log": [...], "refs": {...}}``。"""
    log: list[dict] = []
    refs: dict[str, str] = {}
    project_id = str((scenario.get("project") or {}).get("project_id") or "")
    snapshot_ids: list[str] = []

    for step in (scenario.get("steps") or []):
        op = str(step.get("op") or "")
        pid = str(step.get("project_id") or project_id)
        try:
            entry = _run_step(brain, step, op=op, pid=pid, refs=refs,
                              mode=mode, grant=grant, actor=actor, run_id=run_id,
                              snapshot_ids=snapshot_ids)
        except Exception as e:                      # noqa: BLE001
            entry = {"op": op, "status": "error", "detail": "%s: %s" % (type(e).__name__, e)}
        log.append(entry)

    return {"project_id": project_id, "mode": mode, "log": log,
            "refs": refs, "snapshot_ids": snapshot_ids,
            "ok": all(e.get("status") not in ("error", "failed") for e in log)}


def _run_step(brain: ProjectBrain, step: dict, *, op: str, pid: str, refs: dict,
              mode: str, grant, actor: str, run_id: str,
              snapshot_ids: list) -> dict:
    if op == "ingest":
        msg = dict(step.get("message") or {})
        msg.setdefault("project_id", pid)
        rep = brain.ingest([msg], mode=mode, grant=grant, actor=actor, run_id=run_id)
        r0 = (rep.get("results") or [{}])[0]
        return {"op": op, "message_id": r0.get("message_id"),
                "status": "blocked" if r0.get("blocked") else
                          ("duplicate" if r0.get("duplicate") else "ok"),
                "detail": "记忆 %d 条｜行动 %d 条"
                          % (rep.get("memories", 0), rep.get("actions", 0)),
                "memory_ids": r0.get("memory_ids"), "action_ids": r0.get("action_ids")}

    if op == "evidence":
        ev_kw = ("kind", "claim_type", "summary", "subject", "quantity",
                 "quantity_total", "occurred_at", "captured_at", "source_system",
                 "source_base", "source_table", "source_row_id",
                 "source_message_id", "verified", "verified_by", "criteria_met",
                 "plan_id")
        kw = {k: step[k] for k in ev_kw if k in step}
        title = str(step.get("action_title") or "")
        if title:
            a = _resolve_action(brain, pid, title)
            if a is None:
                return {"op": op, "status": "error",
                        "detail": "证据指向的行动不存在：%s" % title}
            kw["action_id"] = a.get("action_id")
            # 没显式给 plan_id 时，跟所指向的行动保持一致 —— 否则证据与计划断链
            if not kw.get("plan_id"):
                kw["plan_id"] = str(a.get("plan_id") or "")
        r = brain.add_evidence(project_id=pid, mode=mode, grant=grant,
                               actor=actor, **kw)
        if step.get("ref"):
            refs[str(step["ref"])] = r.get("evidence_id", "")
        return {"op": op, "ref": step.get("ref"), "status": r.get("status"),
                "detail": r.get("evidence_id")}

    if op == "verify":
        m = _resolve_memory(brain, str(step.get("memory_kind") or S.KIND_OBSERVATION),
                            str(step.get("memory_text") or ""))
        if m is None:
            return {"op": op, "status": "error", "detail": "找不到待核实的观察"}
        ev_id = refs.get(str(step.get("evidence_ref") or ""), "")
        r = brain.verify_observation(str(m.get("memory_id")), evidence_id=ev_id,
                                     verified_by=str(step.get("verified_by") or "核实人"),
                                     mode=mode, grant=grant, actor=actor,
                                     rationale=str(step.get("reason") or ""))
        return {"op": op, "status": r.get("status"),
                "detail": r.get("message") or r.get("fact_id")}

    if op == "transition":
        a = _resolve_action(brain, pid, str(step.get("action_title") or ""))
        if a is None:
            return {"op": op, "status": "error", "detail": "找不到行动"}
        r = brain.transition(str(a.get("action_id")), str(step.get("to") or ""),
                             reason=str(step.get("reason") or ""), mode=mode,
                             grant=grant, actor=actor,
                             evidence_id=refs.get(str(step.get("evidence_ref") or ""), ""),
                             blocked_reason=str(step.get("blocked_reason") or ""),
                             blocked_owner=str(step.get("blocked_owner") or ""))
        return {"op": op, "action_id": a.get("action_id"), "to": step.get("to"),
                "status": r.get("status"), "reason_code": r.get("reason_code", ""),
                "detail": r.get("message")}

    if op == "close":
        a = _resolve_action(brain, pid, str(step.get("action_title") or ""))
        if a is None:
            return {"op": op, "status": "error", "detail": "找不到行动"}
        r = brain.close_action(str(a.get("action_id")), mode=mode, grant=grant,
                               actor=actor,
                               acceptance_evidence_id=refs.get(
                                   str(step.get("evidence_ref") or ""), ""),
                               reason=str(step.get("reason") or ""))
        return {"op": op, "action_id": a.get("action_id"), "status": r.get("status"),
                "reason_code": r.get("reason_code", ""), "detail": r.get("message")}

    if op == "rollover":
        r = brain.rollover(str(step.get("as_of") or ""), mode=mode, grant=grant,
                           actor=actor, project_id=pid)
        return {"op": op, "status": "ok", "detail": "滚动 %d 条" % r["count"],
                "rolled": r["rolled"]}

    if op == "reminders":
        r = brain.reminders(str(step.get("as_of") or ""), mode=mode, grant=grant,
                            actor=actor, project_id=pid, run_id=run_id)
        for c in r["created"]:
            refs.setdefault(str(c["reminder_key"]), c["reminder_id"])
        return {"op": op, "status": "ok",
                "detail": "新建 %d｜去重 %d" % (r["created_count"], r["skipped_count"]),
                "created": r["created"]}

    if op == "mark_reminder":
        rid = refs.get(str(step.get("reminder_key") or ""), "")
        if not rid:
            return {"op": op, "status": "error", "detail": "找不到提醒"}
        r = brain.mark_reminder(rid, ok=bool(step.get("ok", True)), mode=mode,
                                grant=grant, error=str(step.get("error") or ""),
                                actor=actor)
        return {"op": op, "status": r.get("status"), "detail": r.get("message")}

    if op == "correct":
        m = _resolve_memory(brain, str(step.get("memory_kind") or S.KIND_OBSERVATION),
                            str(step.get("memory_text") or ""))
        if m is None:
            return {"op": op, "status": "error", "detail": "找不到待纠正的记忆"}
        r = brain.correct_memory(str(m.get("memory_id")),
                                 new_text=str(step.get("new_text") or ""),
                                 new_value=str(step.get("new_value") or ""),
                                 reason=str(step.get("reason") or ""),
                                 mode=mode, grant=grant, actor=actor)
        return {"op": op, "status": r.get("status"),
                "detail": r.get("message"), "memory_id": r.get("memory_id")}

    if op == "snapshot":
        sid = str(step.get("snapshot_id") or "")
        r = brain.snapshot(pid, mode=mode, grant=grant, snapshot_id=sid, actor=actor)
        if r.get("status") == "written":
            snapshot_ids.append(r["snapshot_id"])
        return {"op": op, "status": r.get("status"),
                "detail": r.get("snapshot_id"), "path": r.get("path")}

    if op == "context":
        ctx = brain.context(pid, str(step.get("snapshot_id") or ""))
        return {"op": op, "status": "ok",
                "detail": "在办 %d｜阻塞 %d｜冲突 %d"
                          % (ctx["counts"]["actions_open"], ctx["counts"]["blockers"],
                             ctx["counts"]["conflicts"])}

    return {"op": op, "status": "skipped", "detail": "未知步骤类型"}


__all__ = ["replay"]
