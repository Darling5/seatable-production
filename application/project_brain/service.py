# -*- coding: utf-8 -*-
"""application/project_brain/service.py — 单项目第二大脑门面（本期唯一写入口）。

━━ 授权：不新造一套，直接复用第一期 ━━
本模块**不**实现任何授权判定，全部委托给：

  · ``application.authorization.authorize_write`` / ``WriteGrant``
    —— 「人同意这次写入吗」；
  · ``application.dataservice.DataService.write``
    —— 统一写入路径：preview 出候选 / apply 无授权则 blocked /
       幂等键去重 / 写完读回验证 / 落 ``write_ledger.csv`` 台账。

于是「高置信不等于授权」「消息和文件内容不能作为执行指令」这两条，
在本期是**结构上不可能违反**的：本模块压根没有接收「置信度」这个参数的入口。

━━ 四类授权语义（契约要求分开定义）━━
  fact_verification  事实核实  —— 把观察升级为已核实事实（需要证据 + 核实人）
  action_business    行动业务  —— 行动的建立与状态迁移
  execution_grant    执行授权  —— 即 WriteGrant 本身（本期唯一放行来源）
  system_execution   系统执行  —— 由 run_id / 账本反映，不由人授权

前两类在写台账时以 ``[fact_verification]`` / ``[action_business]`` 前缀记入
「备注」列，审计时可 grep —— 沿用第一期 ``[GRT-xxxxxx]`` 前缀的既有惯例。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
from typing import Any, Iterable, Mapping, Optional, Sequence

from .. import authorization as AUTH
from .. import contracts as C
from ..dataservice import DataService, WriteRequest, WriteResult
from . import actions as ACT
from . import context as CTX
from . import evidence_ledger as EV
from . import ids
from . import memory as MEM
from . import schema as S
from . import timeparse as TP
from .store import LocalMemoryAdapter, StaleVersionError

AUTH_CLASS_FACT = "fact_verification"
AUTH_CLASS_ACTION = "action_business"
AUTH_CLASS_EXECUTION = "execution_grant"
AUTH_CLASS_SYSTEM = "system_execution"

# 写结果状态（扩展第一期的词表；语义见契约文档 §5）
R_DUPLICATE = "duplicate"
R_STALE = "stale_version"
R_ILLEGAL = "illegal_transition"
R_NOT_FOUND = "not_found"
R_REJECTED = "rejected"


def _sha(text: str, n: int = 16) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:n]


def message_row_id(message_id: str, text: str = "", message_time: str = "") -> str:
    """来源消息的稳定主键：同一消息重复采集 ⇒ 同一行 ⇒ 天然去重。"""
    basis = str(message_id or "") or ("%s|%s" % (text, message_time))
    return "MSG-" + _sha(basis)


def reminder_row_id(reminder_key: str) -> str:
    return "RMD-" + _sha(reminder_key)


class ProjectBrain:
    """单项目第二大脑。所有写入走 DataService；所有查询走折叠后的只读视图。"""

    def __init__(self, root: str = "", *, data_dir: str = "",
                 adapter: Optional[Any] = None,
                 ds: Optional[DataService] = None,
                 skill_dir: str = ""):
        if not root:
            base = skill_dir or os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
            root = os.path.join(base, "data", "project_brain")
        self.root = os.path.abspath(root)
        self.data_dir = data_dir or self.root
        os.makedirs(self.root, exist_ok=True)
        self.store = adapter or LocalMemoryAdapter(self.root)
        self._ds = ds or DataService(
            adapter_factory=lambda _route: self.store,
            data_dir=self.data_dir)

    @property
    def ds(self) -> DataService:
        return self._ds

    # ════════════════════════════════════════════════════════════════
    # 写入闸门（唯一出口）
    # ════════════════════════════════════════════════════════════════
    def _write(self, table: str, row: dict, *, action: str,
               mode: str, grant: Optional[AUTH.WriteGrant] = None,
               actor: str = "", reason: str = "", idem_key: str = "",
               row_id: str = "", verify_fields: Sequence[str] = (),
               expected_version: Optional[int] = None) -> WriteResult:
        """统一写入：先过授权闸门，再过乐观锁，最后交给 DataService。

        顺序是刻意的 —— 无授权时**连版本都不查**，直接 blocked，
        确保「没授权就什么都没发生」。
        """
        # ① 授权闸门（与第一期同一函数，不复制逻辑）
        if mode == C.MODE_APPLY:
            ok, why = AUTH.authorize_write(grant, table, action)
            if not ok:
                return WriteResult("blocked", message=why,
                                   evidence={"table": table, "action": action})

        # ② 乐观锁：旧版本不许覆盖新版本
        if mode == C.MODE_APPLY and row_id and expected_version is not None:
            actual = self.store.version_of(table, row_id)
            if actual is not None and int(actual) != int(expected_version):
                return WriteResult(R_STALE, row_id=row_id,
                                   message="版本冲突：%s 期望 v%s，实际 v%s（可能有另一个窗口"
                                           "刚写过，本次未写入）"
                                           % (row_id, expected_version, actual),
                                   evidence={"table": table, "action": action,
                                             "expected_version": expected_version,
                                             "actual_version": actual})

        payload = dict(row)
        if row_id:
            payload[S.F_ROW_ID] = row_id
            if expected_version is not None:
                payload[S.F_EXPECTED_VERSION] = int(expected_version)
        elif S.F_ROW_ID not in payload:
            raise ValueError("写入 %s 必须提供主键（__row_id__ / row_id）" % table)

        req = WriteRequest(
            table=table, row=payload, route=S.WRITE_ROUTE, action=action,
            row_id=row_id or "", idem_key=idem_key, actor=actor,
            reason=reason, verify_fields=tuple(verify_fields), grant=grant)
        return self.ds.write(req, mode=mode)

    # ════════════════════════════════════════════════════════════════
    # 1) 采集：来源消息 + 观察/承诺/决策/预测
    # ════════════════════════════════════════════════════════════════
    def ingest(self, messages: Sequence[Mapping[str, Any]], *,
               mode: str = C.MODE_PREVIEW,
               grant: Optional[AUTH.WriteGrant] = None,
               actor: str = "automation", run_id: str = "",
               snapshot_id: str = "") -> dict:
        """采集一批来源消息。

        每条消息形如::

            {"message_id": "...", "text": "...", "message_time": "2026-09-26 09:10",
             "source_system": "wechat", "source_base": "", "source_table": "群聊",
             "sender": "老王",
             "items": [ {"kind": "observation", "text": "...", "aspect_key": "...",
                         "value": "...", "relative_date": "本周五",
                         "action": {"title": "...", "kind": "kitting",
                                    "owner": "老王", "due_relative": "本周五",
                                    "acceptance_criteria": "到货齐套并抽检合格"}} ]}

        ``items`` 由上层 AI 抽取（本模块不做中文 NLP，避免用正则猜人话）。
        没有 ``items`` 的消息只做原话存证，不产生记忆与行动。
        """
        report = {"messages": len(messages), "duplicates": 0, "memories": 0,
                  "actions": 0, "stored_messages": 0, "blocked": 0,
                  "results": []}
        for msg in messages:
            res = self._ingest_one(dict(msg), mode=mode, grant=grant,
                                   actor=actor, run_id=run_id,
                                   snapshot_id=snapshot_id)
            report["results"].append(res)
            if res.get("duplicate"):
                report["duplicates"] += 1
            if res.get("message_stored"):
                report["stored_messages"] += 1
            report["memories"] += len(res.get("memory_ids") or [])
            report["actions"] += len(res.get("action_ids") or [])
            report["blocked"] += 1 if res.get("blocked") else 0
        return report

    def _ingest_one(self, msg: dict, *, mode: str, grant, actor: str,
                    run_id: str, snapshot_id: str) -> dict:
        mid = str(msg.get("message_id") or "")
        text = str(msg.get("text") or "")
        mtime_raw = msg.get("message_time") or msg.get("occurred_at") or ""
        mtime = TP.parse_message_time(mtime_raw) or _dt.datetime.now()
        captured = str(msg.get("captured_at") or _dt.datetime.now().isoformat(timespec="seconds"))
        rid = message_row_id(mid, text, str(mtime_raw))
        out = {"message_row_id": rid, "message_id": mid, "duplicate": False,
               "message_stored": False, "memory_ids": [], "action_ids": [],
               "blocked": False, "results": []}

        existing = self.store.get_row(S.TB_MESSAGE, rid)
        if existing is not None:
            out["duplicate"] = True
            out["message_stored"] = False
            return out

        src = {
            "source_system": str(msg.get("source_system") or ""),
            "source_base": str(msg.get("source_base") or ""),
            "source_table": str(msg.get("source_table") or ""),
            "source_message_id": mid or rid,
        }
        mrow = {
            "project_id": str(msg.get("project_id") or ""),
            "plan_id": str(msg.get("plan_id") or ""),
            "action_id": "", "evidence_id": "", "decision_id": "",
            "run_id": run_id, "snapshot_id": snapshot_id, "version": 1,
            "message_id": mid or rid,
            "text": text,
            "sender": str(msg.get("sender") or ""),
            "occurred_at": mtime.isoformat(timespec="seconds"),
            "captured_at": captured,
            "dedupe_key": rid,
            **src,
        }
        res = self._write(S.TB_MESSAGE, mrow, action=S.ACTION_APPEND, mode=mode,
                          grant=grant, actor=actor,
                          reason="[%s] 采集来源消息" % AUTH_CLASS_SYSTEM,
                          idem_key="msg:%s" % rid, row_id=rid,
                          verify_fields=("message_id", "text", "occurred_at"))
        out["results"].append(("message", res.status, res.message))
        if res.status == "blocked":
            out["blocked"] = True
            return out
        if res.status in ("written", "candidate"):
            out["message_stored"] = True
        else:
            return out

        for item in (msg.get("items") or []):
            self._ingest_item(dict(item), msg=msg, mtime=mtime, captured=captured,
                              mid=src["source_message_id"], mode=mode, grant=grant,
                              actor=actor, run_id=run_id, snapshot_id=snapshot_id,
                              out=out)
        return out

    def _ingest_item(self, item: dict, *, msg: dict, mtime: _dt.datetime,
                     captured: str, mid: str, mode: str, grant, actor: str,
                     run_id: str, snapshot_id: str, out: dict) -> None:
        kind = str(item.get("kind") or S.KIND_OBSERVATION)
        project_id = str(msg.get("project_id") or "")
        text = str(item.get("text") or "").strip()
        relative = item.get("relative_date") or item.get("due_relative") or ""
        # 关键：相对日期按**消息时间**解析，不是按采集时间
        occurred = TP.resolve_date(str(relative), mtime) or str(item.get("occurred_at") or "")
        dkey = MEM.dedupe_key(kind, source_message_id=mid, text=text,
                              project_id=project_id,
                              aspect_key=str(item.get("aspect_key") or ""))
        dup = self._find_by(S.TB_MEMORY, "dedupe_key", dkey)
        if dup is not None:
            out["results"].append(("memory", R_DUPLICATE, dup.get("memory_id", "")))
            return
        row = MEM.build_memory_row(
            kind=kind, project_id=project_id, text=text,
            plan_id=str(msg.get("plan_id") or ""),
            action_id=str(item.get("action_id") or ""),
            evidence_id=str(item.get("evidence_id") or ""),
            run_id=run_id, snapshot_id=snapshot_id,
            occurred_at=occurred, captured_at=captured,
            source_system=str(msg.get("source_system") or ""),
            source_base=str(msg.get("source_base") or ""),
            source_table=str(msg.get("source_table") or ""),
            source_row_id=str(msg.get("source_row_id") or ""),
            source_message_id=mid,
            actor=str(item.get("actor") or msg.get("sender") or ""),
            aspect_key=str(item.get("aspect_key") or ""),
            value=str(item.get("value") or ""),
            confidence=item.get("confidence", ""),
            rationale=str(item.get("rationale") or ""),
            due_date=occurred if item.get("is_due") else "",
        )
        res = self._write(S.TB_MEMORY, row, action=S.ACTION_APPEND, mode=mode,
                          grant=grant, actor=actor,
                          reason="[%s] 采集%s" % (AUTH_CLASS_FACT, S.MEMORY_KIND_CN[kind]),
                          idem_key="mem:%s" % dkey,
                          row_id=row["memory_id"],
                          verify_fields=("kind", "text", "project_id"))
        out["results"].append(("memory", res.status, row["memory_id"]))
        if res.status == "blocked":
            out["blocked"] = True
            return
        if res.status in ("written", "candidate"):
            out["memory_ids"].append(row["memory_id"])

        act_spec = item.get("action")
        if act_spec:
            self._propose_action_from(act_spec, msg=msg, mtime=mtime,
                                      captured=captured, mid=mid, memory_id=row["memory_id"],
                                      mode=mode, grant=grant, actor=actor,
                                      run_id=run_id, snapshot_id=snapshot_id, out=out)

    def _propose_action_from(self, spec: dict, *, msg: dict, mtime: _dt.datetime,
                             captured: str, mid: str, memory_id: str, mode: str,
                             grant, actor: str, run_id: str, snapshot_id: str,
                             out: dict) -> None:
        project_id = str(msg.get("project_id") or "")
        title = str(spec.get("title") or "").strip()
        if not title:
            return
        ikey = ACT.action_idem_key(project_id, title)
        dup = self._find_by(S.TB_ACTION, "idem_key", ikey)
        if dup is not None:
            out["results"].append(("action", R_DUPLICATE, dup.get("action_id", "")))
            return
        due = TP.resolve_date(str(spec.get("due_relative") or spec.get("due") or ""),
                              mtime) or ""
        row = ACT.build_action_row(
            project_id=project_id, title=title,
            kind=str(spec.get("kind") or S.AK_GENERAL),
            owner=str(spec.get("owner") or ""),
            due_date=due,
            acceptance_criteria=spec.get("acceptance_criteria") or "",
            plan_id=str(msg.get("plan_id") or ""),
            evidence_id=str(spec.get("evidence_id") or ""),
            run_id=run_id, snapshot_id=snapshot_id,
            status=S.ST_CANDIDATE)
        row["created_from"] = memory_id
        row["created_at"] = captured
        res = self._write(S.TB_ACTION, row, action=S.ACTION_APPEND, mode=mode,
                          grant=grant, actor=actor,
                          reason="[%s] 建立候选行动" % AUTH_CLASS_ACTION,
                          idem_key="act:%s" % ikey, row_id=row["action_id"],
                          verify_fields=("title", "status", "due_date"))
        out["results"].append(("action", res.status, row["action_id"]))
        if res.status == "blocked":
            out["blocked"] = True
            return
        if res.status in ("written", "candidate"):
            out["action_ids"].append(row["action_id"])

    # ════════════════════════════════════════════════════════════════
    # 2) 事实核实 / 纠正
    # ════════════════════════════════════════════════════════════════
    def verify_observation(self, observation_id: str, *, evidence_id: str,
                           verified_by: str, mode: str,
                           grant: Optional[AUTH.WriteGrant] = None,
                           actor: str = "automation", run_id: str = "",
                           snapshot_id: str = "",
                           rationale: str = "") -> dict:
        """把一条**观察**升级为**已核实事实**（需要证据 + 核实人）。

        旧观察不删除，只追加一个新版本把它标成 ``confirmed``；
        事实是新的一条记录，指向同一个 evidence —— 历史完整保留。
        """
        obs = self.store.get_row(S.TB_MEMORY, observation_id)
        if obs is None:
            return {"status": R_NOT_FOUND, "message": "找不到观察 %s" % observation_id}
        if not str(evidence_id or "").strip():
            return {"status": R_REJECTED, "message": "事实核实必须提供证据，禁止无证据升格"}
        if not str(verified_by or "").strip():
            return {"status": R_REJECTED, "message": "事实核实必须署名核实人"}
        ev = self.store.get_row(S.TB_EVIDENCE, evidence_id)
        if ev is None:
            return {"status": R_NOT_FOUND, "message": "找不到证据 %s" % evidence_id}

        fact = MEM.build_memory_row(
            kind=S.KIND_FACT, project_id=str(obs.get("project_id") or ""),
            text=str(obs.get("text") or ""), plan_id=str(obs.get("plan_id") or ""),
            action_id=str(obs.get("action_id") or ""), evidence_id=evidence_id,
            run_id=run_id, snapshot_id=snapshot_id,
            occurred_at=str(obs.get("occurred_at") or ""),
            captured_at=_dt.datetime.now().isoformat(timespec="seconds"),
            source_system=str(obs.get("source_system") or ""),
            source_base=str(obs.get("source_base") or ""),
            source_table=str(obs.get("source_table") or ""),
            source_row_id=str(obs.get("source_row_id") or ""),
            source_message_id=str(obs.get("source_message_id") or ""),
            actor=verified_by, aspect_key=str(obs.get("aspect_key") or ""),
            value=str(obs.get("value") or ""), rationale=rationale,
            corrects_id=observation_id)
        res = self._write(S.TB_MEMORY, fact, action=S.ACTION_APPEND, mode=mode,
                          grant=grant, actor=actor,
                          reason="[%s] %s 核实 %s" % (AUTH_CLASS_FACT, verified_by, observation_id),
                          idem_key="fact:%s:%s" % (observation_id, evidence_id),
                          row_id=fact["memory_id"],
                          verify_fields=("kind", "text", "evidence_id"))
        if mode != C.MODE_APPLY:
            return {"status": res.status, "message": res.message, "fact_id": fact["memory_id"]}
        if res.status not in ("written",):
            return {"status": res.status, "message": res.message,
                    "fact_id": fact["memory_id"]}

        self._patch_memory(observation_id, {"status": "confirmed",
                                            "superseded_by": fact["memory_id"]},
                           mode=mode, grant=grant, actor=actor,
                           reason="[%s] 观察已核实，转事实" % AUTH_CLASS_FACT)
        return {"status": "written", "message": "已升级为事实", "fact_id": fact["memory_id"]}

    def correct_memory(self, memory_id: str, *, new_text: str = "",
                       new_value: str = "", reason: str = "", mode: str,
                       grant: Optional[AUTH.WriteGrant] = None,
                       actor: str = "automation", evidence_id: str = "",
                       run_id: str = "", snapshot_id: str = "") -> dict:
        """追加纠正：新写一条记忆，旧的标 superseded —— **历史不被覆盖**。"""
        old = self.store.get_row(S.TB_MEMORY, memory_id)
        if old is None:
            return {"status": R_NOT_FOUND, "message": "找不到记忆 %s" % memory_id}
        now = _dt.datetime.now()
        row = MEM.build_memory_row(
            kind=str(old.get("kind") or S.KIND_OBSERVATION),
            project_id=str(old.get("project_id") or ""),
            text=new_text or str(old.get("text") or ""),
            plan_id=str(old.get("plan_id") or ""),
            action_id=str(old.get("action_id") or ""),
            evidence_id=evidence_id or str(old.get("evidence_id") or ""),
            run_id=run_id, snapshot_id=snapshot_id,
            occurred_at=str(old.get("occurred_at") or ""),
            captured_at=now.isoformat(timespec="seconds"),
            source_system=str(old.get("source_system") or ""),
            source_base=str(old.get("source_base") or ""),
            source_table=str(old.get("source_table") or ""),
            source_row_id=str(old.get("source_row_id") or ""),
            source_message_id=str(old.get("source_message_id") or ""),
            actor=actor or str(old.get("actor") or ""),
            aspect_key=str(old.get("aspect_key") or ""),
            value=new_value or str(old.get("value") or ""),
            rationale=reason)
        res = self._write(S.TB_MEMORY, row, action=S.ACTION_APPEND, mode=mode,
                          grant=grant, actor=actor,
                          reason="[%s] 纠正 %s：%s" % (AUTH_CLASS_FACT, memory_id, reason),
                          idem_key="fix:%s:%s" % (memory_id, _sha(new_value + new_text)),
                          row_id=row["memory_id"],
                          verify_fields=("kind", "text", "value"))
        if mode != C.MODE_APPLY or res.status not in ("written",):
            return {"status": res.status, "message": res.message,
                    "memory_id": row["memory_id"]}
        self._patch_memory(memory_id, {"status": "superseded",
                                       "superseded_by": row["memory_id"]},
                           mode=mode, grant=grant, actor=actor,
                           reason="[%s] 已被 %s 纠正" % (AUTH_CLASS_FACT, row["memory_id"]))
        return {"status": "written", "message": "已追加纠正，旧记录保留为 superseded",
                "memory_id": row["memory_id"], "supersedes": memory_id}

    # ════════════════════════════════════════════════════════════════
    # 3) 证据
    # ════════════════════════════════════════════════════════════════
    def add_evidence(self, *, project_id: str, kind: str, claim_type: str,
                     mode: str, grant: Optional[AUTH.WriteGrant] = None,
                     action_id: str = "", summary: str = "", subject: str = "",
                     quantity: Any = "", quantity_total: Any = "",
                     occurred_at: str = "", captured_at: str = "",
                     source_system: str = "", source_base: str = "",
                     source_table: str = "", source_row_id: str = "",
                     source_message_id: str = "", verified: bool = False,
                     verified_by: str = "", criteria_met: Any = "",
                     actor: str = "automation", run_id: str = "",
                     snapshot_id: str = "") -> dict:
        row = EV.build_evidence_row(
            project_id=project_id, kind=kind, claim_type=claim_type,
            summary=summary, action_id=action_id, subject=subject,
            quantity=quantity, quantity_total=quantity_total,
            occurred_at=occurred_at, captured_at=captured_at,
            source_system=source_system, source_base=source_base,
            source_table=source_table, source_row_id=source_row_id,
            source_message_id=source_message_id, verified=verified,
            verified_by=verified_by, criteria_met=criteria_met,
            run_id=run_id, snapshot_id=snapshot_id)
        res = self._write(S.TB_EVIDENCE, row, action=S.ACTION_APPEND, mode=mode,
                          grant=grant, actor=actor,
                          reason="[%s] 登记证据 %s" % (AUTH_CLASS_FACT, claim_type),
                          idem_key="evd:%s" % row["dedupe_key"],
                          row_id=row["evidence_id"],
                          verify_fields=("kind", "claim_type", "summary"))
        return {"status": res.status, "message": res.message,
                "evidence_id": row["evidence_id"], "row": row}

    def _link_evidence(self, action_id: str, evidence_id: str, *, mode: str,
                       grant, actor: str) -> None:
        act = self.store.get_row(S.TB_ACTION, action_id)
        if act is None:
            return
        ids_ = list(act.get("evidence_ids") or [])
        if evidence_id in ids_:
            return
        ids_.append(evidence_id)
        self._patch_action(action_id, {"evidence_ids": ids_}, mode=mode, grant=grant,
                           actor=actor, reason="[%s] 关联证据" % AUTH_CLASS_ACTION)

    # ════════════════════════════════════════════════════════════════
    # 4) 行动闭环
    # ════════════════════════════════════════════════════════════════
    def propose_action(self, *, project_id: str, title: str, mode: str,
                       grant: Optional[AUTH.WriteGrant] = None,
                       kind: str = S.AK_GENERAL, owner: str = "",
                       due_date: str = "", acceptance_criteria: Any = "",
                       plan_id: str = "", evidence_id: str = "",
                       actor: str = "automation", run_id: str = "",
                       snapshot_id: str = "") -> dict:
        ikey = ACT.action_idem_key(project_id, title)
        dup = self._find_by(S.TB_ACTION, "idem_key", ikey)
        if dup is not None:
            return {"status": R_DUPLICATE, "action_id": dup.get("action_id"),
                    "message": "同一项目下已存在同名行动，未重复建立"}
        row = ACT.build_action_row(
            project_id=project_id, title=title, kind=kind, owner=owner,
            due_date=due_date, acceptance_criteria=acceptance_criteria,
            plan_id=plan_id, evidence_id=evidence_id, run_id=run_id,
            snapshot_id=snapshot_id)
        res = self._write(S.TB_ACTION, row, action=S.ACTION_APPEND, mode=mode,
                          grant=grant, actor=actor,
                          reason="[%s] 建立候选行动" % AUTH_CLASS_ACTION,
                          idem_key="act:%s" % ikey, row_id=row["action_id"],
                          verify_fields=("title", "status", "due_date"))
        return {"status": res.status, "message": res.message,
                "action_id": row["action_id"]}

    def transition(self, action_id: str, to_state: str, *, reason: str, mode: str,
                   grant: Optional[AUTH.WriteGrant] = None,
                   actor: str = "automation", evidence_id: str = "",
                   blocked_reason: str = "", blocked_owner: str = "",
                   run_id: str = "", snapshot_id: str = "",
                   expected_version: Optional[int] = None) -> dict:
        """行动状态迁移。关闭（closed）会先跑四条硬规则校验。"""
        act = self.store.get_row(S.TB_ACTION, action_id)
        if act is None:
            return {"status": R_NOT_FOUND, "message": "找不到行动 %s" % action_id}
        cur = str(act.get("status") or "")
        ev = [e for e in self._rows_of(S.TB_EVIDENCE, action_id)
              if str(e.get("action_id") or "") == action_id]

        # ① 先判「这一步走不走得通」——非法迁移不必再看业务规则
        if not ACT.can_transition(cur, to_state):
            return {"status": R_ILLEGAL,
                    "message": ACT.transition_error(cur, to_state)}
        # ② 再判「这一步允不允许」——关闭必须通过验收（四条硬规则）
        if to_state == S.ST_CLOSED:
            allowed, code, why = ACT.check_close(act, ev)
            if not allowed:
                return {"status": R_REJECTED, "reason_code": code,
                        "message": "拒绝关闭：%s" % why}
        if to_state == S.ST_BLOCKED and not str(blocked_reason or "").strip():
            return {"status": R_REJECTED,
                    "message": "登记阻塞必须写明原因（否则没人知道在等什么）"}

        now = _dt.datetime.now()
        payload: dict[str, Any] = {
            "status": to_state, "status_cn": S.ACTION_STATE_CN[to_state],
            "prev_state": cur,
            "updated_at": now.isoformat(timespec="seconds"),
            "last_transition_at": now.isoformat(timespec="seconds"),
        }
        if to_state == S.ST_BLOCKED:
            payload.update({"blocked_reason": blocked_reason,
                            "blocked_owner": blocked_owner,
                            "blocked_since": now.isoformat(timespec="seconds")})
        else:
            payload.update({"blocked_reason": "", "blocked_owner": "",
                            "blocked_since": ""})
        if cur == S.ST_CLOSED or cur == S.ST_CANCELLED:
            payload["reopen_count"] = int(act.get("reopen_count") or 0) + 1
        if to_state == S.ST_CANCELLED:
            payload["cancel_reason"] = reason

        if evidence_id:
            linked = list(act.get("evidence_ids") or [])
            if evidence_id not in linked:
                linked.append(evidence_id)
            payload["evidence_ids"] = linked

        res = self._patch_action(action_id, payload, mode=mode, grant=grant,
                                 actor=actor,
                                 reason="[%s] %s → %s：%s" % (AUTH_CLASS_ACTION, cur, to_state, reason),
                                 expected_version=expected_version)
        if res.status in ("written", "candidate"):
            self._record_event(act, cur, to_state, reason=reason, actor=actor,
                               evidence_id=evidence_id, mode=mode, grant=grant,
                               run_id=run_id, snapshot_id=snapshot_id)
        return {"status": res.status, "message": res.message or "状态已迁移",
                "action_id": action_id, "from": cur, "to": to_state}

    def close_action(self, action_id: str, *, mode: str,
                     grant: Optional[AUTH.WriteGrant] = None,
                     actor: str = "automation", acceptance_evidence_id: str = "",
                     reason: str = "") -> dict:
        """关闭行动：验收证据是**必须**的，没有就不关。"""
        if not acceptance_evidence_id:
            return {"status": R_REJECTED, "reason_code": ACT.R_NEED_ACCEPTANCE,
                    "message": "拒绝关闭：%s" % ACT.REASON_CN[ACT.R_NEED_ACCEPTANCE]}
        ev = self.store.get_row(S.TB_EVIDENCE, acceptance_evidence_id)
        if ev is None:
            return {"status": R_NOT_FOUND,
                    "message": "找不到验收证据 %s" % acceptance_evidence_id}
        self._link_evidence(action_id, acceptance_evidence_id, mode=mode,
                            grant=grant, actor=actor)
        return self.transition(action_id, S.ST_CLOSED,
                               reason=reason or "验收通过，关闭",
                               mode=mode, grant=grant, actor=actor,
                               evidence_id=acceptance_evidence_id)

    def rollover(self, as_of: str, *, mode: str,
                 grant: Optional[AUTH.WriteGrant] = None,
                 actor: str = "automation", project_id: str = "") -> dict:
        """跨天滚动：未完成事项保留原期限，只累计逾期与结转次数。"""
        acted: list[dict] = []
        for a in self.store.list_rows(S.TB_ACTION):
            if project_id and str(a.get("project_id") or "") != project_id:
                continue
            payload = ACT.rollover_payload(a, as_of)
            if not payload:
                continue
            res = self._patch_action(str(a.get("action_id")), payload, mode=mode,
                                     grant=grant, actor=actor,
                                     reason="[%s] 跨天滚动，原期限不变" % AUTH_CLASS_SYSTEM)
            acted.append({"action_id": a.get("action_id"), "status": res.status,
                          "due_date": a.get("due_date"),
                          "due_date_original": payload.get("due_date_original",
                                                           a.get("due_date_original")),
                          "overdue_days": payload.get("overdue_days")})
        return {"as_of": as_of, "rolled": acted, "count": len(acted)}

    # ════════════════════════════════════════════════════════════════
    # 5) 提醒
    # ════════════════════════════════════════════════════════════════
    def reminders(self, as_of: str, *, mode: str,
                  grant: Optional[AUTH.WriteGrant] = None,
                  actor: str = "automation", project_id: str = "",
                  run_id: str = "", snapshot_id: str = "") -> dict:
        """算出当前该发的提醒；同一 reminder_key 只建一条（去重）。"""
        created: list[dict] = []
        existing: list[str] = []
        for a in self.store.list_rows(S.TB_ACTION):
            if project_id and str(a.get("project_id") or "") != project_id:
                continue
            for cand in ACT.reminder_candidates(a, as_of):
                rid = reminder_row_id(cand["reminder_key"])
                if self.store.get_row(S.TB_REMINDER, rid) is not None:
                    existing.append(cand["reminder_key"])
                    continue
                row = {
                    "project_id": cand["project_id"],
                    "plan_id": str(a.get("plan_id") or ""),
                    "action_id": cand["action_id"], "evidence_id": "",
                    "decision_id": "", "run_id": run_id, "snapshot_id": snapshot_id,
                    "version": 1,
                    "reminder_key": cand["reminder_key"],
                    "kind": cand["kind"], "kind_cn": cand["kind_cn"],
                    "title": cand["title"], "owner": cand["owner"],
                    "due_date": cand["due_date"], "as_of": as_of,
                    "status": ACT.RS_PENDING, "attempts": 0, "last_error": "",
                    "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
                    "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
                }
                res = self._write(S.TB_REMINDER, row, action=S.ACTION_APPEND,
                                  mode=mode, grant=grant, actor=actor,
                                  reason="[%s] 生成提醒" % AUTH_CLASS_SYSTEM,
                                  idem_key="rmd:%s" % cand["reminder_key"],
                                  row_id=rid,
                                  verify_fields=("reminder_key", "kind", "status"))
                if res.status in ("written", "candidate"):
                    created.append({"reminder_id": rid, "status": res.status,
                                    **{k: cand[k] for k in
                                       ("reminder_key", "kind", "action_id",
                                        "owner", "title", "due_date")}})
        return {"as_of": as_of, "created": created, "skipped_duplicate": existing,
                "created_count": len(created), "skipped_count": len(existing)}

    def mark_reminder(self, reminder_id: str, *, ok: bool, mode: str,
                      grant: Optional[AUTH.WriteGrant] = None,
                      error: str = "", actor: str = "automation") -> dict:
        """标记提醒投递结果。失败不丢：保留 attempts 与原因，可原样重试。"""
        row = self.store.get_row(S.TB_REMINDER, reminder_id)
        if row is None:
            return {"status": R_NOT_FOUND, "message": "找不到提醒 %s" % reminder_id}
        payload = {
            "status": ACT.RS_SENT if ok else ACT.RS_FAILED,
            "attempts": int(row.get("attempts") or 0) + 1,
            "last_error": "" if ok else str(error or "未说明"),
            "updated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        res = self._write(S.TB_REMINDER, payload, action=S.ACTION_UPDATE, mode=mode,
                          grant=grant, actor=actor,
                          reason="[%s] 提醒投递%s" % (AUTH_CLASS_SYSTEM,
                                                     "成功" if ok else "失败"),
                          row_id=reminder_id,
                          expected_version=int(row.get(S.F_VERSION) or 1),
                          verify_fields=("status", "attempts"))
        return {"status": res.status, "message": res.message,
                "reminder_id": reminder_id, "attempts": payload["attempts"]}

    def failed_reminders(self) -> list[dict]:
        """待重试的失败提醒（失败恢复入口）。"""
        return [r for r in self.store.list_rows(S.TB_REMINDER)
                if str(r.get("status")) == ACT.RS_FAILED]

    # ════════════════════════════════════════════════════════════════
    # 6) 查询
    # ════════════════════════════════════════════════════════════════
    def context(self, project_id: str, snapshot_id: str = "", *,
                as_of: Any = None,
                stale_after_hours: int = S.DEFAULT_STALE_AFTER_HOURS) -> dict:
        """等价于契约接口 ``get_project_context(project_id, snapshot_id)``。

        给了 snapshot_id 就**只读快照**：同一 id 反复调用结果完全一致，
        不受此后任何写入影响（第三期因此可以对着固定版本复核）。
        """
        if snapshot_id:
            snap = CTX.read_snapshot(self.root, snapshot_id)
            if snap is None:
                return {"contract_version": S.BRAIN_CONTRACT_VERSION,
                        "project_id": project_id, "snapshot_id": snapshot_id,
                        "error": "snapshot_not_found",
                        "message": "找不到快照 %s" % snapshot_id}
            return snap
        return CTX.build_context(adapter=self.store, project_id=project_id,
                                 as_of=as_of,
                                 stale_after_hours=stale_after_hours)

    def snapshot(self, project_id: str, *, mode: str,
                 grant: Optional[AUTH.WriteGrant] = None,
                 snapshot_id: str = "", as_of: Any = None,
                 actor: str = "automation") -> dict:
        """固化一份快照。快照是本地产物（local_append），preview 不写盘。"""
        sid = snapshot_id or ids.new_id("snapshot")
        ctx = CTX.build_context(adapter=self.store, project_id=project_id, as_of=as_of)
        if mode != C.MODE_APPLY:
            return {"status": "candidate", "snapshot_id": sid,
                    "message": "preview：快照未落盘", "context": ctx}
        path = CTX.write_snapshot(self.root, ctx, sid)
        return {"status": "written", "snapshot_id": sid, "path": path,
                "version": ctx.get("version")}

    def query(self, project_id: str, question: str = "status", *,
              snapshot_id: str = "", as_of: Any = None) -> dict:
        ctx = self.context(project_id, snapshot_id, as_of=as_of)
        answers = ctx.get("answers") or {}
        key = {"status": "status", "当前状态": "status",
               "blockers": "blockers", "阻塞": "blockers",
               "today": "today", "今日重点": "today",
               "decisions": "decisions", "决策": "decisions",
               "gaps": "gaps", "缺口": "gaps"}.get(question, question)
        if key not in answers:
            return {"error": "unknown_question", "question": question,
                    "available": sorted(answers)}
        ans = dict(answers[key])
        ans["question"] = key
        ans["project_id"] = project_id
        ans["snapshot_id"] = ctx.get("snapshot_id", "")
        ans["context_version"] = ctx.get("version")
        return ans

    # ════════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════════
    def _patch_memory(self, memory_id: str, payload: dict, *, mode: str, grant,
                      actor: str, reason: str) -> WriteResult:
        row = self.store.get_row(S.TB_MEMORY, memory_id)
        ver = int(row.get(S.F_VERSION) or 1) if row else None
        return self._write(S.TB_MEMORY, payload, action=S.ACTION_UPDATE,
                           mode=mode, grant=grant, actor=actor, reason=reason,
                           row_id=memory_id, expected_version=ver,
                           verify_fields=tuple(payload.keys()))

    def _patch_action(self, action_id: str, payload: dict, *, mode: str, grant,
                      actor: str, reason: str,
                      expected_version: Optional[int] = None) -> WriteResult:
        row = self.store.get_row(S.TB_ACTION, action_id)
        if row is None:
            return WriteResult(R_NOT_FOUND, message="找不到行动 %s" % action_id)
        ver = expected_version if expected_version is not None else int(row.get(S.F_VERSION) or 1)
        return self._write(S.TB_ACTION, payload, action=S.ACTION_UPDATE,
                           mode=mode, grant=grant, actor=actor, reason=reason,
                           row_id=action_id, expected_version=ver,
                           verify_fields=tuple(k for k in payload
                                               if not S.is_internal(k)
                                               and isinstance(payload[k], (str, int, float, bool))))

    def _record_event(self, action: Mapping[str, Any], from_state: str, to_state: str,
                      *, reason: str, actor: str, evidence_id: str, mode: str,
                      grant, run_id: str, snapshot_id: str) -> None:
        row = ACT.build_action_event(
            action=action, from_state=from_state, to_state=to_state,
            reason=reason, actor=actor, evidence_id=evidence_id,
            run_id=run_id, snapshot_id=snapshot_id)
        self._write(S.TB_ACTION_EVENT, row, action=S.ACTION_APPEND, mode=mode,
                    grant=grant, actor=actor,
                    reason="[%s] 行动留痕" % AUTH_CLASS_SYSTEM,
                    idem_key="evt:%s" % row["event_id"], row_id=row["event_id"],
                    verify_fields=("action_id", "from_state", "to_state"))

    def _rows_of(self, table: str, _key: str = "") -> list[dict]:
        return self.store.list_rows(table)

    def _find_by(self, table: str, field: str, value: str) -> Optional[dict]:
        for r in self.store.list_rows(table):
            if str(r.get(field) or "") == str(value):
                return r
        return None

    def evidence_of_action(self, action_id: str) -> list[dict]:
        return [e for e in self.store.list_rows(S.TB_EVIDENCE)
                if str(e.get("action_id") or "") == str(action_id)]


def get_project_context(project_id: str, snapshot_id: str = "",
                        *, root: str = "", as_of: Any = None) -> dict:
    """契约接口：``get_project_context(project_id, snapshot_id)``。

    第三期只需 import 这一个函数即可拿到全部事实/承诺/行动/决策/阻塞/
    证据引用/缺失信息/冲突/来源时效。
    """
    return ProjectBrain(root=root).context(project_id, snapshot_id, as_of=as_of)


__all__ = ["ProjectBrain", "get_project_context", "message_row_id",
           "reminder_row_id", "AUTH_CLASS_FACT", "AUTH_CLASS_ACTION",
           "AUTH_CLASS_EXECUTION", "AUTH_CLASS_SYSTEM",
           "R_DUPLICATE", "R_STALE", "R_ILLEGAL", "R_NOT_FOUND", "R_REJECTED",
           "StaleVersionError"]
