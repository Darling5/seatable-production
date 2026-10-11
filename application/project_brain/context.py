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
G_SOURCE_UNREADABLE = "source_unreadable"
G_TIME_FALLBACK = "time_basis_fallback"

GAP_CN = {
    G_ACTION_NO_OWNER: "行动没有负责人 —— 没人认领就不会有人做",
    G_ACTION_NO_DUE: "行动没有截止时间 —— 没有期限就没有优先级",
    G_ACTION_NO_CRITERIA: "行动没有验收标准 —— 无法判断是否真的做完了",
    G_ACTION_NO_EVIDENCE: "行动没有任何证据支撑",
    G_FACT_NO_EVIDENCE: "事实缺少证据引用 —— 无法回溯来源",
    G_STALE_SOURCE: "来源数据超过时效阈值未更新",
    G_UNRESOLVED_CONFLICT: "存在未解决的来源冲突",
    G_SOURCE_UNREADABLE: "来源表读不到 —— 「读不到」不等于「表里没有数据」",
    G_TIME_FALLBACK: "时间基准降级 —— 消息时间缺失，改用采集时刻解析相对日期",
}


class SourceUnreadableError(RuntimeError):
    """读来源表失败且**没有接收方可以留痕**时抛出。

    与 ``store`` 层「账本读不出就拒绝」同一条规矩：拿不到数据时，
    宁可让调用方炸掉，也不能把「读不到」降级成「没有」。
    """

    def __init__(self, failures: Sequence[Mapping[str, str]]) -> None:
        self.failures = [dict(f) for f in failures]
        super().__init__(
            "来源表读不到且无留痕通道：%s"
            % "；".join("%s（%s）" % (f.get("table"), f.get("error"))
                        for f in self.failures))


class _Reader:
    """带「读失败留痕」的只读表读取器。

    ⚠️ 这里以前是一个裸的 try/except：

        try:
            rows = adapter.list_rows(table)
        except Exception:
            return []

    于是存储挂了、表名不对、读权限不够、适配器自己抛异常 —— 一律表现成
    「这张表里没有数据」。后果是 ``answers["gaps"]`` 会心安理得地回答
    **「没有发现信息缺口。」**、``source_freshness`` 会回答 ``[]``
    （＝所有来源都很新鲜）。**「读不到」被洗成了「没有」**，
    而且账面上完全看不出来 —— 这是最坏的一类错：结论的确定性凭空升高。

    现在失败必须落进 ``failures``，由调用方转成
    ``G_SOURCE_UNREADABLE`` 缺口与显式 caveat。同一张表只记首次失败
    （``build_context`` 与 ``source_freshness`` 会各读一次消息/证据表，
    去重后不会重复报告）。
    """

    def __init__(self) -> None:
        self._failures: dict[str, dict] = {}

    def rows(self, adapter: Any, table: str, project_id: str = "") -> list[dict]:
        try:
            rows = adapter.list_rows(table)
        except Exception as exc:      # noqa: BLE001 —— 必须接住并留痕，不能吞
            self._failures.setdefault(str(table), {
                "table": str(table),
                "error": "%s: %s" % (type(exc).__name__, exc),
            })
            return []
        out = [dict(r) for r in rows]
        if project_id:
            out = [r for r in out if str(r.get("project_id") or "") == str(project_id)]
        return out

    @property
    def failures(self) -> list[dict]:
        return [dict(v) for v in self._failures.values()]

    def gaps(self, as_of: str) -> list[dict]:
        return [_gap(G_SOURCE_UNREADABLE, ref=f["table"],
                     subject=f["error"], as_of=as_of)
                for f in self.failures]

    def caveats(self) -> list[str]:
        if not self._failures:
            return []
        names = "、".join(sorted(self._failures))
        return ["有 %d 张来源表读不到（%s）—— 下面的结论可能建立在残缺数据上；"
                "「读不到」不等于「这些表里没有数据」"
                % (len(self._failures), names)]


def _time_fallback_rows(tables: Sequence[tuple]) -> list[dict]:
    """哪些表里有「时间基准降级」的记录（subject 见 `schema.TIME_SOURCES`）。

    只报条数不报行号：这里的问题是**口径**（这批日期是怎么算出来的），
    不是某一行写错了。逐行列出来会把一处口径问题淹没在几十行噪音里。
    """
    out = []
    for table, rows, id_key in tables:
        hits = [r for r in rows
                if str(r.get("occurred_at_source") or "") == S.SRC_FALLBACK_CAPTURE_TIME]
        if hits:
            out.append({"table": str(table), "count": len(hits),
                        "sample_id": str(hits[0].get(id_key) or "")})
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
                     stale_after_hours: int = S.DEFAULT_STALE_AFTER_HOURS,
                     reader: Optional["_Reader"] = None) -> list[dict]:
    """按 (来源系统, Base, 表) 聚合时效，标注是否过期。

    只统计**真正从外部读回来的原始记录**（来源消息 + 证据）——
    由它们派生出来的记忆/事实是我们自己的加工产物，拿它们充当「来源最近
    更新过」会把一个四天没动静的来源粉饰成新鲜的。

    ``reader``：读失败的去处。``build_context`` 会把自己的读取器传进来，
    失败统一转成缺口；**独立调用时不给 reader，则读失败直接抛
    ``SourceUnreadableError``** —— 本函数的返回结构里没有一个字段能表达
    「这张表我没读到」，与其返回一个看起来一切正常的 ``[]``，
    不如拒绝回答。
    """
    own = reader is None
    rdr = reader if reader is not None else _Reader()
    now = _as_datetime(as_of) or _dt.datetime.now()
    buckets: dict[tuple, dict] = {}
    for table in (S.TB_MESSAGE, S.TB_EVIDENCE):
        for r in rdr.rows(adapter, table, project_id):
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
        oldest = b["oldest"]
        bucket = {
            "source_system": sysname,
            "source_base": base,
            "source_table": table,
            "records": b["count"],
            "age_hours": age,
            "oldest_age_hours": oldest,
            # 三个字段回答三个不同的问题，**只有一个可以据以分支**：
            #   · `stale`         ＝「这个来源**还在动**吗」—— 看**最新**一条。诊断用。
            #   · `stale_oldest`  ＝「它里面**最老**的一条有多老」—— 看最老一条。测量用。
            #   · `stale_source`  ＝★**判据结论**：这个来源算不算落后。**唯一可依据的字段**。
            # ⚠️ 只报 `stale` 会**挑最好看的样本**：一个来源今天来过一条新消息，
            # `stale` 立刻变 False，可它手里还有 8 条四天没动的记录 ——
            # 「落后」被一个刚到的样本盖住了。判断「这批数据可不可信」
            # 必须用保守的那个，所以结论**在这里算好、盖章进字段**
            # （`is_stale_source`）：下游只读结论，不自己挑字段 ——
            # 挑字段这件事**曾经在上游和下游各做了一次，做出两个相反的答案**。
            "stale": bool(age is not None and age > stale_after_hours),
            "stale_oldest": bool(oldest is not None and oldest > stale_after_hours),
            "stale_after_hours": stale_after_hours,
        }
        bucket["stale_source"] = is_stale_source(bucket)
        out.append(bucket)
    if own and rdr.failures:
        # 没有留痕通道 → 拒绝回答（见 docstring）
        raise SourceUnreadableError(rdr.failures)
    return out


def build_context(*, adapter: Any, project_id: str,
                  snapshot_id: str = "",
                  as_of: Any = None,
                  stale_after_hours: int = S.DEFAULT_STALE_AFTER_HOURS) -> dict:
    """把该项目所有记忆折叠成一份统一上下文（纯读，无副作用）。

    读表失败**不会**再被吞成「空表」：失败的每一张表都会变成一处
    ``G_SOURCE_UNREADABLE`` 信息缺口，并进入本轮 caveat。
    因此只要有任何一张表读不到，``answers["gaps"]`` 就不可能回答
    「没有发现信息缺口。」—— 那正是「读不到」被洗成「没有」的入口。
    """
    now = _as_datetime(as_of) or _dt.datetime.now()
    as_of_iso = now.isoformat(timespec="seconds")
    rdr = _Reader()

    project_rows = rdr.rows(adapter, S.TB_PROJECT, project_id)
    project = project_rows[0] if project_rows else {}

    memory_rows = rdr.rows(adapter, S.TB_MEMORY, project_id)
    evidence_rows = rdr.rows(adapter, S.TB_EVIDENCE, project_id)
    action_rows = rdr.rows(adapter, S.TB_ACTION, project_id)
    event_rows = rdr.rows(adapter, S.TB_ACTION_EVENT, project_id)
    message_rows = rdr.rows(adapter, S.TB_MESSAGE, project_id)

    # 有效记忆 = active（生效中）+ confirmed（观察已核实升格）——
    # 只有 superseded 算「死」。见 memory.is_active 的说明：把 confirmed
    # 归进「死」会让核实过的记忆从所有出口消失（既不在 observations，
    # 也不在 facts），整条核实链在上下文里表现为「这条记忆不见了」。
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
        # as_of = 「这条行动的结论依赖的最新一条**证据采集**时间」。
        #
        # ⚠️ 这里**不许**算 `action.updated_at`：那是**系统写入时刻**，谁动过
        # 这行它就跟到「现在」，于是「这条行动的数据有多旧」在契约层永远
        # 看不出来。同一个坑 `data_as_of` 已经踩过并修过一次
        # （见 tests/test_project_brain.py 里那段「四条一条都测不出来」的注释），
        # 当时只修了全局那一个，每条行动自己这个漏了 —— 现在两处同一口径。
        #
        # 没有证据时退化到 `created_at`（行动的登记时间），再没有才用行的
        # 写入时刻；行的写入时刻同时单独放在 `row_updated_at` 里 ——
        # 「数据多旧」和「这行什么时候被系统动过」两个事实各占一个位子，
        # 谁也不许顶替谁。
        as_of_a = _max_ts([e.get("captured_at") for e in ev]) or str(a.get("created_at") or "")
        item = {
            "action_id": aid,
            # 八个统一字段（合同 §1）—— 行动必须能直接联到生产计划与产生它的运行
            "project_id": a.get("project_id", ""),
            "plan_id": a.get("plan_id", ""),
            "evidence_id": a.get("evidence_id", ""),
            "decision_id": a.get("decision_id", ""),
            "run_id": a.get("run_id", ""),
            "snapshot_id": a.get("snapshot_id", ""),
            "version": a.get(S.F_VERSION, a.get("version", 1)),
            "title": a.get("title", ""),
            "kind": a.get("kind", ""),
            "kind_cn": a.get("kind_cn", ""),
            "status": a.get("status", ""),
            "status_cn": a.get("status_cn", ""),
            "owner": a.get("owner", ""),
            "due_date": a.get("due_date", ""),
            "due_date_original": a.get("due_date_original", ""),
            # due_date 是拿哪个时刻当基准解析的（相对期限会整体漂移）
            "occurred_at_source": a.get("occurred_at_source", S.SRC_MESSAGE_TIME),
            "overdue_days": a.get("overdue_days", 0),
            "carry_over_count": a.get("carry_over_count", 0),
            "acceptance_criteria": a.get("acceptance_criteria", ""),
            "blocked_reason": a.get("blocked_reason", ""),
            "blocked_owner": a.get("blocked_owner", ""),
            "blocked_since": a.get("blocked_since", ""),
            "evidence_ids": [str(e.get("evidence_id")) for e in ev],
            "as_of": as_of_a or as_of_iso,
            # 系统写入时刻（只读诊断用，**不是** as_of）：想知道「这条行动
            # 最近一次被程序改动是什么时候」看这个，想知道「它的数据有多旧」看 as_of。
            "row_updated_at": a.get("updated_at", ""),
        }
        actions_out.append(item)

        if str(a.get("status")) == S.ST_BLOCKED:
            blockers.append({
                "action_id": aid, "title": a.get("title", ""),
                "project_id": a.get("project_id", ""),
                "plan_id": a.get("plan_id", ""),
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
                                 as_of=now, stale_after_hours=stale_after_hours,
                                 reader=rdr)
    for f in freshness:
        # 缺口只认**保守**判据（最老一条过期就报）。文案由 `stale_gap`
        # 唯一产出，「算不算过期」由 `is_stale_source` 唯一判定 ——
        # alignment.read_context 与 CLI 的 `sources` 子命令调同一个函数，
        # 免得同一份上下文里出现「过期」和「不过期」两个答案。
        if is_stale_source(f):
            gaps.append(stale_gap(f, as_of=as_of_iso))

    # ⚠️ 必须放在最后：读表失败要作为缺口进 ``missing_info``，
    # 否则「读不到」会以「这张表没数据」的身份混进所有结论里。
    gaps.extend(rdr.gaps(as_of_iso))

    # 时间基准降级：`occurred_at` 是拿采集时刻而不是消息时间解析出来的。
    # 这类记录的**截止日期可能整体漂移**，而且漂移量随采集延迟变化 ——
    # 不报出来，「下周五」到底是哪一天就没人知道是怎么算的。
    time_fallback = _time_fallback_rows(
        ((S.TB_MEMORY, memory_rows, "memory_id"),
         (S.TB_ACTION, action_rows, "action_id"),
         (S.TB_MESSAGE, message_rows, "message_id")))
    for tf in time_fallback:
        gaps.append(_gap(
            G_TIME_FALLBACK, ref=tf["table"],
            subject="%s 张表里有 %d 条记录的 occurred_at 是拿**采集时刻**"
                    "解析的（消息时间缺失）—— 相对日期（「下周五」）可能整体"
                    "漂移，漂移量随采集延迟变化" % (tf["table"], tf["count"]),
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
        # data_as_of = **数据截至时间**，口径是「最晚一条**采集**时间」——
        # 也就是「这批数据到此为止，之后的事我们还不知道」。
        #   · 不用 generated_at（那是「现在几点」）；
        #   · 不用 action.updated_at（系统写入时刻，会把 data_as_of 顶成「现在」，
        #     于是「数据有多旧」在契约层看不出来，三期对齐计划快照时会误判成一样新）；
        #   · 不用 occurred_at（那可能是**将来**的业务日期，例如「下周三到货」，
        #     会把 data_as_of 顶到未来，直接破坏「截至时间」的含义）。
        "data_as_of": _max_ts(
            [r.get("captured_at") for r in evidence_rows + memory_rows
             + message_rows]) or as_of_iso,
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
        # 内部字段（双下划线 = 不进契约、不落业务列）：本轮读表失败明细。
        # 放在这里是为了让每个 answer 都能说清「这份结论缺了哪些表」，
        # 而不是让调用方自己去数 missing_info 里的 source_unreadable。
        "__source_unreadable": rdr.failures,
        # 同上：时间基准降级的记录（见 _time_fallback_rows）
        "__time_fallback": time_fallback,
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
    """记忆行投影。

    八个统一字段**一个都不能少**（合同第 1 节）：早期版本只投影了
    `project_id` / `action_id` / `evidence_id` / `decision_id`，
    把 `plan_id` / `run_id` / `snapshot_id` / `version` 丢了 ——
    结果是「记忆/行动与生产计划联表」在契约层就断链，
    下游只能拿名称去猜（而合同第 1.1 节明令禁止用名称做键）。

    另两个字段是 2026-10-11 补的，都是为了让**状态和来历**不再隐形：

    · `status` —— 不投影它，下游就没法区分「未核实观察」和「已核实的」，
      而 ``answers[*].caveats`` 里那句「其中 N 条为未核实观察」正是靠它算的
      （缺这个字段时按**未核实**兜底，宁可说重也不说轻）；
    · `corrects_id` —— 一条事实是从哪条观察升格来的。不投影它，
      「观察 → 事实」这条链在上下文里就断了：事实不知道自己从哪来，
      而被升格的那条观察已经换了身份（status=confirmed）。
    """
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
            "status": m.get("status", S.MS_ACTIVE),
            "corrects_id": m.get("corrects_id", ""),
            # ── 八个统一字段（合同 §1，缺一不可）──
            "project_id": m.get("project_id", ""),
            "plan_id": m.get("plan_id", ""),
            "action_id": m.get("action_id", ""),
            "evidence_id": m.get("evidence_id", ""),
            "decision_id": m.get("decision_id", ""),
            "run_id": m.get("run_id", ""),
            "snapshot_id": m.get("snapshot_id", ""),
            "version": m.get("version", m.get(S.F_VERSION, 1)),
            # ── 时间与来源 ──
            "occurred_at": m.get("occurred_at", ""),
            # 这个 occurred_at 是拿哪个时刻当基准解析的（见 schema.TIME_SOURCES）
            "occurred_at_source": m.get("occurred_at_source", S.SRC_MESSAGE_TIME),
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


def is_stale_source(f: Mapping[str, Any]) -> bool:
    """「这个来源算不算过期」—— **全系统唯一判据**。

    只认**保守**的那个（``stale_oldest``：最老一条过期就算落后）。
    一个来源今天来过一条新消息，不能因此把手里那批四天没动的记录
    说成「新鲜」—— 那是「挑最好看的样本」。

    为什么必须收成一处：同一个问题**曾经有两个答案**
    （`build_context` 按保守判据产缺口，`alignment.read_context` 和
    `project_brain.py` 的 `sources` 子命令按 ``stale``（最新一条）过滤）。
    于是一条「刚来过一条新消息、但 8 条记录四天没动」的来源会同时得到
    「过期」（缺口里）与「不过期」（提示里）两个结论 —— **同一份上下文，
    两个答案**。判据在这里定义一次，三处调用同一个函数。

    ``stale`` 只在完全没有 ``stale_oldest`` 时兜底（老契约文档），
    因为 ``oldest ≥ newest``，正常情况下 ``stale ⟹ stale_oldest``。
    """
    if f.get("stale_oldest") is not None:
        return bool(f.get("stale_oldest"))
    return bool(f.get("stale"))


def stale_gap(f: Mapping[str, Any], *, as_of: str) -> dict:
    """把「来源时效」桶转成一处信息缺口。

    判据用**保守**的那个（``stale_oldest``：最老一条过期就算落后）——
    一个来源今天来过一条新消息，不能因此把手里那批四天没动的记录
    说成「新鲜」。文案里同时给出最老小时数与记录条数：
    只说「未更新」看不出规模，是 1 条还是 800 条，处置完全不同。
    """
    return _gap(
        G_STALE_SOURCE,
        ref="%s/%s/%s" % (f.get("source_system"), f.get("source_base"),
                          f.get("source_table")),
        subject="%s（最老一条 %.1f 小时未更新，共 %d 条；最新一条 %.1f 小时）"
                % (f.get("source_table"), f.get("oldest_age_hours") or 0,
                   f.get("records") or 0, f.get("age_hours") or 0),
        as_of=as_of)


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
            "caveats": _answer_caveats(ctx)}


def answer_today(ctx: Mapping[str, Any]) -> dict:
    """今日重点。

    ``today_focus`` 是「今天该看什么」的**唯一判据**，这里必须区分两件事：

    * **键不存在** —— 调用方交来的是残缺上下文（早期契约样例、外部拼装），
      此时才允许退化，且必须把「已退化」写进 caveats；
    * **值等于空列表** —— 这是一个**有效结论**：「今天确实没有到期或被阻塞
      的事项」，绝不能当成「取不到」再去兜底。

    旧写法 ``ctx.get("today_focus") or _focus(ctx)`` 把空列表当假值，于是
    **空档时反而把全部在办行动端上来当「今日重点」** —— 实测连 `due_date`
    是 2099 年的行动都会被写进「今日重点 N 项」，结论的字面意思与它自己
    引用的证据相反（驾驶舱/决策支持都会照抄这句话）。
    """
    if "today_focus" in ctx:
        focus = list(ctx.get("today_focus") or [])
        degraded = False
    else:
        focus = _focus(ctx)
        degraded = True
    caveats: list[str] = []
    if degraded:
        caveats.append(
            "上下文未提供 today_focus，已退化为「全部在办行动」（%d 项）—— "
            "这不是「今日到期」的结论，仅供参考" % len(focus))
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
            "caveats": _answer_caveats(ctx, caveats)}


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
            "caveats": _answer_caveats(
                ctx,
                ([] if all(d.get("rationale") for d in dec) and dec
                 else (["部分决策未记录理由，无法回答「当时为什么这么定」"] if dec else [])))}


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
    # 读表失败时，缺口清单本身也是残缺的，必须说出来 ——
    # 只报「共 N 处」会让人以为已经数全了。
    return {"answer": text, "items": gaps, "evidence_ids": [],
            "as_of": ctx.get("data_as_of"), "caveats": _answer_caveats(ctx)}


def _focus(ctx: Mapping[str, Any]) -> list[dict]:
    return [a for a in ctx.get("actions", []) if a.get("status") in S.OPEN_STATES]


def _answer_caveats(ctx: Mapping[str, Any], extra: Any = ()) -> list[str]:
    """每条结论都带上「已知局限」：**先说自己特有的，再说全局的**。

    以前只有 `answer_status` 挂了 caveat，其余四个答案一律 ``caveats: []``：
    于是「今天要处理什么」可以一边引用**未核实观察**、一边对局限只字不提，
    而同一个上下文里的「当前状态」却老实交代了。一处生效、四处不生效的守卫
    等于没有守卫 —— 谁走另一个出口谁就不受约束（这就是「守卫只护被点名的
    那一处」的标准长相，而且测试全绿）。
    """
    return list(extra) + _status_caveats(ctx)


def _status_caveats(ctx: Mapping[str, Any]) -> list[str]:
    out = []
    unreadable = ctx.get("__source_unreadable") or []
    if unreadable:
        # ⚠️ 必须是**第一条**：读不到表时，「下面的数字」本身就不完整，
        # 得先让人知道这一点，再谈这些数字说明了什么。
        out.append("有 %d 张来源表读不到（%s）—— 本结论基于残缺数据，"
                   "「读不到」不等于「没有」"
                   % (len(unreadable),
                      "、".join(sorted(str(u.get("table")) for u in unreadable))))
    time_fb = ctx.get("__time_fallback") or []
    if time_fb:
        out.append("有 %d 张表存在**时间基准降级**的记录（%s）—— "
                   "那些记录的相对日期是拿采集时刻解析的，"
                   "「下周五」这类期限可能整体漂移"
                   % (len(time_fb), "、".join(sorted(str(t.get("table"))
                                                     for t in time_fb))))
    obs = ctx.get("observations", [])
    # ⚠️ 必须按 `status` 分开数：`is_active` 修好之后，已核实的观察
    # （status=confirmed）也会进 `observations`，这时如果还照旧把整个桶
    # 说成「N 条未核实观察」，caveat 本身就成了新的假话 ——
    # 把一个已经核实过的观察说成没核实，同样是谎报，只是方向相反。
    # 缺 `status` 字段时按**未核实**兜底：宁可说重，不可说轻。
    unverified = [o for o in obs
                  if str(o.get("status") or S.MS_ACTIVE) != S.MS_CONFIRMED]
    if unverified:
        out.append("其中 %d 条为**未核实观察**，不得当作事实使用" % len(unverified))
    n_verified = len(obs) - len(unverified)
    if n_verified:
        out.append("另有 %d 条观察已核实并升格为事实（见 facts，"
                   "用 corrects_id 可回溯到原观察）" % n_verified)
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
           "GAP_CN", "G_SOURCE_UNREADABLE", "G_TIME_FALLBACK",
           "SourceUnreadableError", "stale_gap", "is_stale_source"]
