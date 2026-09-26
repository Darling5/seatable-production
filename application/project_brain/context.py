# -*- coding: utf-8 -*-
"""application/project_brain/context.py — 项目查询与统一上下文接口。

回答业主点名的五个问题：

  1. 当前状态               —— ``answers["status"]``
  2. 阻塞与负责人           —— ``answers["blockers"]``
  3. 今日重点               —— ``answers["today"]``
  4. 历史决策理由           —— ``answers["decisions"]``
  5. 信息缺口               —— ``answers["gaps"]``

一条不许破的规矩：**每条结论都必须带证据引用和数据截至时间**。
所以 ``answers`` 里每一项都形如::

    {"answer": "...", "evidence_ids": [...], "as_of": "2026-09-26T10:00:00",
     "items": [...], "caveats": [...]}

``as_of`` 是「这条结论依赖的最新一条证据/记忆的采集时间」，不是「我们现在
几点」。两者的差别就是「数据截至 9/25 18:00」和「现在是 9/26 10:00」的
差别 —— 前者才是有信息量的那个。

另：``get_project_context(project_id, snapshot_id)`` 是给第三期的正式接口，
带 snapshot_id 时**只读快照**，结果可复现（同 id 同结果），不受后续写入影响。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import actions as ACT
from . import evidence_ledger as EV
from . import memory as MEM
from . import schema as S
from . import timeparse as TP

SNAPSHOT_DIR = "snapshots"

# 信息缺口原因码
G_ACTION_NO_OWNER = "action_no_owner"
G_ACTION_NO_DUE = "action_no_due"
G_ACTION_NO_CRITERIA = "action_no_criteria"
G_ACTION_NO_EVIDENCE = "action_no_evidence"
G_FACT_NO_EVIDENCE = "fact_no_evidence"
G_STALE_SOURCE = "stale_source"
G_UNRESOLVED_CONFLICT = "unresolved_conflict"

GAP_CN = {
    G_ACTION_NO_OWNER: "行动没有负责人 —— 没人认领就不会有人做",
    G_ACTION_NO_DUE: "行动没有截止时间 —— 没有期限就没有优先级",
    G_ACTION_NO_CRITERIA: "行动没有验收标准 —— 无法判断是否真的做完了",
    G_ACTION_NO_EVIDENCE: "行动没有任何证据支撑",
    G_FACT_NO_EVIDENCE: "事实缺少证据引用 —— 无法回溯来源",
    G_STALE_SOURCE: "来源数据超过时效阈值未更新",
    G_UNRESOLVED_CONFLICT: "存在未解决的来源冲突",
}


def _rows(adapter: Any, table: str, project_id: str = "") -> list[dict]:
    try:
        rows = adapter.list_rows(table)
    except Exception:
        return []
    out = [dict(r) for r in rows]
    if project_id:
        out = [r for r in out if str(r.get("project_id") or "") == str(project_id)]
    return out


def _max_ts(values: Iterable[Any]) -> str:
    best = ""
    for v in values:
        t = str(v or "")
        if t and t > best:
            best = t
    return best


def _as_datetime(value: Any) -> Optional[_dt.datetime]:
    if isinstance(value, _dt.datetime):
        return value
    return TP.parse_message_time(value)


def source_freshness(*, adapter: Any, project_id: str = "",
                     as_of: Any = None,
                     stale_after_hours: int = S.DEFAULT_STALE_AFTER_HOURS) -> list[dict]:
    """按 (来源系统, Base, 表) 聚合时效，标注是否过期。

    只统计**真正从外部读回来的原始记录**（来源消息 + 证据）——
    由它们派生出来的记忆/事实是我们自己的加工产物，拿它们充当「来源最近
    更新过」会把一个四天没动静的来源粉饰成新鲜的。
    """
    now = _as_datetime(as_of) or _dt.datetime.now()
    buckets: dict[tuple, dict] = {}
    for table in (S.TB_MESSAGE, S.TB_EVIDENCE):
        for r in _rows(adapter, table, project_id):
            if not str(r.get("source_system") or "").strip():
                continue
            if not (str(r.get("source_message_id") or "").strip()
                    or str(r.get("source_row_id") or "").strip()
                    or str(r.get("source_table") or "").strip()):
                continue
            key = (str(r.get("source_system")),
                   str(r.get("source_base") or ""),
                   str(r.get("source_table") or table))
            b = buckets.setdefault(key, {"hours": [], "count": 0,
                                         "oldest": None, "newest": None})
            b["count"] += 1
            age = TP.age_hours(r.get("captured_at"), now)
            if age is not None:
                b["hours"].append(age)
                b["newest"] = age if b["newest"] is None else min(b["newest"], age)
                b["oldest"] = age if b["oldest"] is None else max(b["oldest"], age)
    out: list[dict] = []
    for (sysname, base, table), b in sorted(buckets.items()):
        age = b["newest"]
        out.append({
            "source_system": sysname,
            "source_base": base,
            "source_table": table,
            "records": b["count"],
            "age_hours": age,
            "oldest_age_hours": b["oldest"],
            "stale": bool(age is not None and age > stale_after_hours),
            "stale_after_hours": stale_after_hours,
        })
    return out


def build_context(*, adapter: Any, project_id: str,
                  snapshot_id: str = "",
                  as_of: Any = None,
                  stale_after_hours: int = S.DEFAULT_STALE_AFTER_HOURS) -> dict:
    """把该项目所有记忆折叠成一份统一上下文（纯读，无副作用）。"""
    now = _as_datetime(as_of) or _dt.datetime.now()
    as_of_iso = now.isoformat(timespec="seconds")

    project_rows = _rows(adapter, S.TB_PROJECT, project_id)
    project = project_rows[0] if project_rows else {}

    memory_rows = _rows(adapter, S.TB_MEMORY, project_id)
    evidence_rows = _rows(adapter, S.TB_EVIDENCE, project_id)
    action_rows = _rows(adapter, S.TB_ACTION, project_id)
    event_rows = _rows(adapter, S.TB_ACTION_EVENT, project_id)
    message_rows = _rows(adapter, S.TB_MESSAGE, project_id)

    active_mem = [m for m in memory_rows if MEM.is_active(m)]
    by_kind: dict[str, list[dict]] = {k: [] for k in S.MEMORY_KINDS}
    for m in active_mem:
        by_kind.setdefault(str(m.get("kind")), []).append(m)

    ev_by_action: dict[str, list[dict]] = {}
    for e in evidence_rows:
        ev_by_action.setdefault(str(e.get("action_id") or ""), []).append(e)

    actions_out: list[dict] = []
    blockers: list[dict] = []
    focus: list[dict] = []
    gaps: list[dict] = []

    for a in sorted(action_rows, key=lambda r: (str(r.get("due_date") or "9999"), str(r.get("action_id")))):
        aid = str(a.get("action_id") or "")
        ev = ev_by_action.get(aid, [])
        as_of_a = _max_ts([e.get("captured_at") for e in ev]
                          + [a.get("updated_at"), a.get("created_at")])
        item = {
            "action_id": aid,
            "title": a.get("title", ""),
            "kind": a.get("kind", ""),
            "kind_cn": a.get("kind_cn", ""),
            "status": a.get("status", ""),
            "status_cn": a.get("status_cn", ""),
            "owner": a.get("owner", ""),
            "due_date": a.get("due_date", ""),
            "due_date_original": a.get("due_date_original", ""),
            "overdue_days": a.get("overdue_days", 0),
            "carry_over_count": a.get("carry_over_count", 0),
            "acceptance_criteria": a.get("acceptance_criteria", ""),
            "blocked_reason": a.get("blocked_reason", ""),
            "blocked_owner": a.get("blocked_owner", ""),
            "blocked_since": a.get("blocked_since", ""),
            "evidence_ids": [str(e.get("evidence_id")) for e in ev],
            "as_of": as_of_a or as_of_iso,
            "version": a.get(S.F_VERSION, 1),
        }
        actions_out.append(item)

        if str(a.get("status")) == S.ST_BLOCKED:
            blockers.append({
                "action_id": aid, "title": a.get("title", ""),
                "owner": a.get("owner", ""),
                "blocked_owner": a.get("blocked_owner", ""),
                "reason": a.get("blocked_reason", ""),
                "since": a.get("blocked_since", ""),
                "due_date": a.get("due_date", ""),
                "evidence_ids": item["evidence_ids"],
                "as_of": item["as_of"],
            })

        if ACT.is_open(a):
            _collect_gaps(a, ev, gaps, as_of_iso)
            if _in_focus(a, now):
                focus.append({
                    "action_id": aid, "title": a.get("title", ""),
                    "owner": a.get("owner", ""),
                    "due_date": a.get("due_date", ""),
                    "status": a.get("status", ""), "status_cn": a.get("status_cn", ""),
                    "overdue_days": a.get("overdue_days", 0),
                    "reason": _focus_reason(a, now),
                    "evidence_ids": item["evidence_ids"],
                    "as_of": item["as_of"],
                })

    for m in active_mem:
        if str(m.get("kind")) == S.KIND_FACT and not str(m.get("evidence_id") or "").strip():
            gaps.append(_gap(G_FACT_NO_EVIDENCE, ref=str(m.get("memory_id")),
                             subject=m.get("text", ""), as_of=as_of_iso))

    conflicts = MEM.find_conflicts(active_mem)
    for c in conflicts:
        gaps.append(_gap(G_UNRESOLVED_CONFLICT, ref=str(c.get("conflict_id")),
                         subject=str(c.get("aspect_key")), as_of=as_of_iso))

    freshness = source_freshness(adapter=adapter, project_id=project_id,
                                 as_of=now, stale_after_hours=stale_after_hours)
    for f in freshness:
        if f["stale"]:
            gaps.append(_gap(
                G_STALE_SOURCE,
                ref="%s/%s/%s" % (f["source_system"], f["source_base"], f["source_table"]),
                subject="%s（%.1f 小时未更新）" % (f["source_table"], f["age_hours"] or 0),
                as_of=as_of_iso))

    version = sum(int(r.get(S.F_VERSION) or 1)
                  for r in (memory_rows + evidence_rows + action_rows
                            + event_rows + message_rows)) or 1

    ctx = {
        "contract_version": S.BRAIN_CONTRACT_VERSION,
        "project_id": project_id,
        "project": {k: v for k, v in project.items() if not S.is_internal(k)},
        "snapshot_id": snapshot_id,
        "version": version,
        "generated_at": as_of_iso,
        "data_as_of": _max_ts(
            [r.get("captured_at") for r in evidence_rows + memory_rows + message_rows]
            + [a.get("updated_at") for a in action_rows]) or as_of_iso,
        "source_freshness": freshness,
        "facts": _brief(by_kind.get(S.KIND_FACT, [])),
        "observations": _brief(by_kind.get(S.KIND_OBSERVATION, [])),
        "commitments": _brief(by_kind.get(S.KIND_COMMITMENT, [])),
        "decisions": _brief(by_kind.get(S.KIND_DECISION, [])),
        "predictions": _brief(by_kind.get(S.KIND_PREDICTION, [])),
        "actions": actions_out,
        "action_events": [dict(e) for e in event_rows],
        "blockers": blockers,
        "today_focus": focus,
        "evidence_refs": [dict(e) for e in evidence_rows],
        "missing_info": gaps,
        "conflicts": conflicts,
        "counts": {
            "actions_open": sum(1 for a in action_rows if ACT.is_open(a)),
            "actions_closed": sum(1 for a in action_rows if str(a.get("status")) == S.ST_CLOSED),
            "blockers": len(blockers),
            "conflicts": len(conflicts),
            "gaps": len(gaps),
        },
    }
    ctx["answers"] = {
        "status": answer_status(ctx),
        "blockers": answer_blockers(ctx),
        "today": answer_today(ctx),
        "decisions": answer_decisions(ctx),
        "gaps": answer_gaps(ctx),
    }
    return ctx


def _brief(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    out = []
    for m in rows:
        out.append({
            "memory_id": m.get("memory_id"),
            "kind": m.get("kind"),
            "kind_cn": m.get("kind_cn"),
            "text": m.get("text"),
            "rationale": m.get("rationale", ""),
            "actor": m.get("actor", ""),
            "value": m.get("value", ""),
            "aspect_key": m.get("aspect_key", ""),
            "due_date": m.get("due_date", ""),
            "confidence": m.get("confidence", ""),
            "evidence_id": m.get("evidence_id", ""),
            "decision_id": m.get("decision_id", ""),
            "occurred_at": m.get("occurred_at", ""),
            "captured_at": m.get("captured_at", ""),
            "source_system": m.get("source_system", ""),
            "source_base": m.get("source_base", ""),
            "source_table": m.get("source_table", ""),
            "source_row_id": m.get("source_row_id", ""),
            "source_message_id": m.get("source_message_id", ""),
        })
    return out


def _gap(code: str, *, ref: str, subject: str, as_of: str) -> dict:
    return {"code": code, "message": GAP_CN.get(code, code),
            "ref": ref, "subject": subject, "as_of": as_of}


def _collect_gaps(action: Mapping[str, Any], ev: Sequence[Mapping[str, Any]],
                  gaps: list[dict], as_of: str) -> None:
    aid = str(action.get("action_id") or "")
    if not str(action.get("owner") or "").strip():
        gaps.append(_gap(G_ACTION_NO_OWNER, ref=aid, subject=action.get("title", ""), as_of=as_of))
    if not str(action.get("due_date") or "").strip():
        gaps.append(_gap(G_ACTION_NO_DUE, ref=aid, subject=action.get("title", ""), as_of=as_of))
    if not ACT.has_criteria(action.get("acceptance_criteria")):
        gaps.append(_gap(G_ACTION_NO_CRITERIA, ref=aid, subject=action.get("title", ""), as_of=as_of))
    if not ev:
        gaps.append(_gap(G_ACTION_NO_EVIDENCE, ref=aid, subject=action.get("title", ""), as_of=as_of))


def _in_focus(action: Mapping[str, Any], now: _dt.datetime) -> bool:
    status = str(action.get("status") or "")
    if not ACT.is_open(action):
        return False
    if status in (S.ST_BLOCKED, S.ST_PENDING_ACCEPTANCE):
        return True
    due = ACT.parse_date(action.get("due_date"))
    if due is None:
        return status == S.ST_IN_PROGRESS
    return due <= now.date()


def _focus_reason(action: Mapping[str, Any], now: _dt.datetime) -> str:
    status = str(action.get("status") or "")
    if status == S.ST_BLOCKED:
        return "被阻塞：%s" % (action.get("blocked_reason") or "未说明原因")
    if status == S.ST_PENDING_ACCEPTANCE:
        return "等待验收"
    due = ACT.parse_date(action.get("due_date"))
    if due is not None and due < now.date():
        return "已逾期 %d 天（原期限 %s）" % ((now.date() - due).days,
                                              action.get("due_date_original") or action.get("due_date"))
    if due is not None and due == now.date():
        return "今日到期"
    return "进行中"


# ────────────────────────────────────────────────────────────────────
# 五个问题的答案（每条都带证据 + 数据截至时间）
# ────────────────────────────────────────────────────────────────────
def answer_status(ctx: Mapping[str, Any]) -> dict:
    counts = ctx.get("counts", {})
    lines = ["项目 %s：在办行动 %d 个，已关闭 %d 个，阻塞 %d 个，来源冲突 %d 处。"
             % (ctx.get("project_id"), counts.get("actions_open", 0),
                counts.get("actions_closed", 0), counts.get("blockers", 0),
                counts.get("conflicts", 0))]
    conf = ctx.get("project") or {}
    if conf.get("stage") or conf.get("阶段"):
        lines.append("当前阶段：%s。" % (conf.get("stage") or conf.get("阶段")))
    return {"answer": " ".join(lines),
            "items": ctx.get("actions", []),
            "evidence_ids": _ev_of(ctx.get("actions", [])),
            "as_of": ctx.get("data_as_of"),
            "caveats": _status_caveats(ctx)}


def answer_blockers(ctx: Mapping[str, Any]) -> dict:
    bl = ctx.get("blockers", [])
    if not bl:
        text = "当前没有登记在案的阻塞。"
    else:
        parts = []
        for b in bl:
            parts.append("%s（负责人 %s，被 %s 卡住：%s，自 %s 起）"
                         % (b.get("title") or b.get("action_id"),
                            b.get("owner") or "未指派",
                            b.get("blocked_owner") or "未指明",
                            b.get("reason") or "未说明", b.get("since") or "—"))
        text = "共 %d 项阻塞：%s" % (len(bl), "；".join(parts))
    return {"answer": text, "items": bl,
            "evidence_ids": _ev_of(bl),
            "as_of": _max_ts([b.get("as_of") for b in bl]) or ctx.get("data_as_of"),
            "caveats": []}


def answer_today(ctx: Mapping[str, Any]) -> dict:
    focus = ctx.get("today_focus") or _focus(ctx)
    if not focus:
        text = "今天没有到期或被阻塞的事项。"
    else:
        text = "今日重点 %d 项：%s" % (
            len(focus),
            "；".join("%s（%s，%s）" % (f.get("title"), f.get("owner") or "未指派", f.get("reason"))
                      for f in focus))
    return {"answer": text, "items": focus,
            "evidence_ids": _ev_of(focus),
            "as_of": _max_ts([f.get("as_of") for f in focus]) or ctx.get("data_as_of"),
            "caveats": []}


def answer_decisions(ctx: Mapping[str, Any]) -> dict:
    dec = ctx.get("decisions", [])
    if not dec:
        text = "还没有登记过决策记录。"
    else:
        text = "共 %d 条决策：" % len(dec) + "；".join(
            "%s（%s，理由是 %s）" % (d.get("text"), d.get("actor") or "未署名",
                                    d.get("rationale") or "未记录理由")
            for d in dec)
    return {"answer": text, "items": dec,
            "evidence_ids": [d.get("evidence_id") for d in dec if d.get("evidence_id")],
            "as_of": _max_ts([d.get("captured_at") for d in dec]) or ctx.get("data_as_of"),
            "caveats": ([] if all(d.get("rationale") for d in dec) and dec
                        else (["部分决策未记录理由，无法回答「当时为什么这么定」"] if dec else []))}


def answer_gaps(ctx: Mapping[str, Any]) -> dict:
    gaps = ctx.get("missing_info", [])
    if not gaps:
        text = "没有发现信息缺口。"
    else:
        by_code: dict[str, int] = {}
        for g in gaps:
            by_code[g["code"]] = by_code.get(g["code"], 0) + 1
        text = "共 %d 处信息缺口：%s" % (
            len(gaps), "；".join("%s×%d" % (GAP_CN.get(c, c), n)
                                for c, n in sorted(by_code.items())))
    return {"answer": text, "items": gaps, "evidence_ids": [],
            "as_of": ctx.get("data_as_of"), "caveats": []}


def _focus(ctx: Mapping[str, Any]) -> list[dict]:
    return [a for a in ctx.get("actions", []) if a.get("status") in S.OPEN_STATES]


def _status_caveats(ctx: Mapping[str, Any]) -> list[str]:
    out = []
    obs = ctx.get("observations", [])
    if obs:
        out.append("其中 %d 条为**未核实观察**，不得当作事实使用" % len(obs))
    if ctx.get("conflicts"):
        out.append("存在 %d 处来源冲突，结论可能随后续核实变化" % len(ctx["conflicts"]))
    return out


def _ev_of(items: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for i in items:
        for e in (i.get("evidence_ids") or []):
            if e and e not in out:
                out.append(e)
        if i.get("evidence_id") and i["evidence_id"] not in out:
            out.append(i["evidence_id"])
    return out


# ────────────────────────────────────────────────────────────────────
# 快照
# ────────────────────────────────────────────────────────────────────
def snapshot_path(root: str, snapshot_id: str) -> str:
    return os.path.join(root, SNAPSHOT_DIR, "%s.json" % snapshot_id)


def write_snapshot(root: str, ctx: Mapping[str, Any], snapshot_id: str) -> str:
    path = snapshot_path(root, snapshot_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(ctx)
    payload["snapshot_id"] = snapshot_id
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def read_snapshot(root: str, snapshot_id: str) -> Optional[dict]:
    path = snapshot_path(root, snapshot_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


__all__ = ["build_context", "answer_status", "answer_blockers", "answer_today",
           "answer_decisions", "answer_gaps", "source_freshness",
           "write_snapshot", "read_snapshot", "snapshot_path", "SNAPSHOT_DIR",
           "GAP_CN"]
