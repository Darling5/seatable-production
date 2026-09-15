# -*- coding: utf-8 -*-
"""客户到售后业务闭环控制平面。

本模块只管理本地控制平面（对象、状态、证据、审批），不直接写 SeaTable。
正式 production/tasks 写入仍由上层候选/审批链执行；这样可以先离线演练整条业务链。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from application import contracts

OBJECT_SPECS = {
    "customer": {"name_zh": "客户", "id_prefix": "CUS", "trigger_state": "lead", "required_fields": ["customer", "owner"], "next_action": "完善客户档案"},
    "lead": {"name_zh": "线索", "id_prefix": "LED", "trigger_state": "lead", "required_fields": ["customer", "product", "owner"], "next_action": "评估并转商机"},
    "opportunity": {"name_zh": "商机", "id_prefix": "OPP", "trigger_state": "lead", "required_fields": ["customer", "product", "owner"], "next_action": "推进商机"},
    "requirement": {"name_zh": "需求", "id_prefix": "REQ", "trigger_state": "requirement_confirming", "required_fields": ["summary", "owner"], "next_action": "确认需求"},
    "solution": {"name_zh": "方案", "id_prefix": "SOL", "trigger_state": "solution_confirming", "required_fields": ["summary", "owner"], "next_action": "确认方案"},
    "quote": {"name_zh": "报价", "id_prefix": "QUO", "trigger_state": "quotation_confirming", "required_fields": ["summary", "owner"], "next_action": "确认报价"},
    "contract": {"name_zh": "合同", "id_prefix": "CTR", "trigger_state": "contract_pending", "required_fields": ["customer_id", "owner"], "next_action": "核对并签署合同"},
    "project": {"name_zh": "项目", "id_prefix": "PRJ", "trigger_state": "won_and_funded", "required_fields": ["customer_id", "owner"], "next_action": "生成生产立项候选"},
    "production_order": {"name_zh": "生产订单", "id_prefix": "MO", "trigger_state": "procurement", "required_fields": ["project_id", "owner"], "next_action": "批准生产计划"},
    "purchase_order": {"name_zh": "采购订单", "id_prefix": "PO", "trigger_state": "procurement", "required_fields": ["project_id", "owner"], "next_action": "批准采购候选"},
    "shipment": {"name_zh": "发货", "id_prefix": "SHP", "trigger_state": "ready_to_ship", "required_fields": ["project_id", "owner"], "next_action": "核对发货清单"},
    "after_sales": {"name_zh": "售后", "id_prefix": "AS", "trigger_state": "after_sales", "required_fields": ["project_id", "owner"], "next_action": "跟进售后并结案"},
}
OBJECT_FIELDS = ["object_id", "object_type", "root_id", "parent_id", "customer_id", "project_id", "version", "state", "owner", "next_action", "due_date", "summary", "evidence_ids", "idempotency_key", "created_at", "updated_at"]
TRANSITION_FIELDS = ["transition_id", "root_id", "object_id", "from_state", "to_state", "severity", "trigger_event_id", "actor", "reason", "approval_id", "created_at"]
EVIDENCE_FIELDS = ["event_id", "intent_id", "candidate_id", "write_id", "related_object_type", "related_object_id", "source", "source_ref", "summary", "created_at"]
APPROVAL_FIELDS = ["approval_id", "action", "object_id", "before_json", "after_json", "reason", "approver", "status", "source_event_ids", "created_at"]
STATE_OBJECTS = {
    "requirement_confirming": ("requirement",),
    "solution_confirming": ("solution",),
    "quotation_confirming": ("quote",),
    "contract_pending": ("contract",),
    "won_and_funded": ("project",),
    "procurement": ("production_order", "purchase_order"),
    "ready_to_ship": ("shipment",),
    "after_sales": ("after_sales",),
}
MAIN_CHAIN = list(contracts.PROJECT_STATES[:14])


def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _token(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


class BusinessStore:
    """四张 UTF-8-SIG CSV 表；每次整表写入都使用临时文件替换。"""
    TABLES = {
        "objects": ("objects.csv", OBJECT_FIELDS),
        "transitions": ("transitions.csv", TRANSITION_FIELDS),
        "evidence": ("evidence.csv", EVIDENCE_FIELDS),
        "approvals": ("approvals.csv", APPROVAL_FIELDS),
    }

    def __init__(self, data_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir)

    def path(self, table: str) -> Path:
        return self.data_dir / self.TABLES[table][0]

    def read(self, table: str) -> list[dict[str, str]]:
        path = self.path(table)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def _atomic_write(self, table: str, rows: Iterable[Mapping[str, Any]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path, fields = self.path(table), self.TABLES[table][1]
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: str(row.get(key, "")) for key in fields})
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def append(self, table: str, row: Mapping[str, Any], idempotency_field: str = "") -> tuple[dict[str, str], bool]:
        rows = self.read(table)
        if idempotency_field and row.get(idempotency_field):
            wanted = str(row[idempotency_field])
            hit = next((item for item in rows if item.get(idempotency_field) == wanted), None)
            if hit:
                return hit, True
        normalized = {key: str(row.get(key, "")) for key in self.TABLES[table][1]}
        rows.append(normalized)
        self._atomic_write(table, rows)
        return normalized, False

    def update(self, object_id: str, changes: Mapping[str, Any]) -> dict[str, str]:
        rows = self.read("objects")
        for row in rows:
            if row.get("object_id") == object_id:
                row.update({key: str(value) for key, value in changes.items()})
                self._atomic_write("objects", rows)
                return row
        raise KeyError("对象不存在：%s" % object_id)

    def writable(self) -> bool:
        parent = self.data_dir if self.data_dir.exists() else self.data_dir.parent
        return parent.exists() and os.access(parent, os.W_OK)


class Service:
    def __init__(self, data_dir: str | os.PathLike[str] = "data/business_loop"):
        self.store = BusinessStore(data_dir)

    def _object(self, kind: str, root_id: str, customer_id: str, project_id: str,
                owner: str, state: str, summary: str = "", parent_id: str = "",
                version: int = 1, idem: str = "", due_date: str = "") -> dict[str, str]:
        now = _now()
        return {
            "object_id": contracts.new_object_id(kind), "object_type": kind,
            "root_id": root_id, "parent_id": parent_id, "customer_id": customer_id,
            "project_id": project_id, "version": str(version), "state": state,
            "owner": owner, "next_action": "" if state == "closed" else OBJECT_SPECS[kind]["next_action"],
            "due_date": due_date, "summary": summary, "evidence_ids": "",
            "idempotency_key": idem, "created_at": now, "updated_at": now,
        }

    def _approval(self, action: str, object_id: str, before: Mapping[str, Any],
                  after: Mapping[str, Any], reason: str, events: list[str], status: str) -> dict[str, str]:
        row = {"approval_id": "APR-" + _token(action, object_id, reason), "action": action,
               "object_id": object_id, "before_json": json.dumps(before, ensure_ascii=False),
               "after_json": json.dumps(after, ensure_ascii=False), "reason": reason,
               "approver": "用户" if status == "approved" else "", "status": status,
               "source_event_ids": ",".join(events), "created_at": _now()}
        return self.store.append("approvals", row, "approval_id")[0]

    def _evidence(self, event_id: str, kind: str, object_id: str, summary: str) -> dict[str, str]:
        row = {"event_id": event_id or "EVT-" + _token(kind, object_id, summary),
               "related_object_type": kind, "related_object_id": object_id,
               "source": "business_loop", "source_ref": "local", "summary": summary,
               "created_at": _now()}
        return self.store.append("evidence", row, "event_id")[0]

    def _transition(self, root_id: str, object_id: str, old: str, new: str,
                    reason: str, actor: str, event_id: str, approval: Mapping[str, Any]) -> dict[str, str]:
        # 案件创建时 old==new（lead→lead）是登记动作不是迁移，标 normal
        severity = "normal" if old == new else contracts.transition_severity(old, new)
        row = {"transition_id": "TRN-" + _token(root_id, object_id, old, new, reason),
               "root_id": root_id, "object_id": object_id, "from_state": old,
               "to_state": new, "severity": severity,
               "trigger_event_id": event_id, "actor": actor, "reason": reason,
               "approval_id": approval.get("approval_id", ""), "created_at": _now()}
        return self.store.append("transitions", row, "transition_id")[0]

    def _objects(self, root_id: str) -> list[dict[str, str]]:
        return [r for r in self.store.read("objects") if r.get("root_id") == root_id]

    def start_case(self, customer: str, product: str, owner: str = "项目经理",
                   source_event_id: str = "", contact: str = "", phone: str = "",
                   amount: float = 0, due_date: str = "", actor: str = "automation",
                   approved: bool = False) -> dict[str, Any]:
        idem = "case:" + _token(customer, product, source_event_id or customer)
        hit = next((r for r in self.store.read("objects") if r.get("idempotency_key") == idem), None)
        if hit:
            return {"status": "reused", "root_id": hit["root_id"], "objects": self._objects(hit["root_id"])}
        plan = {"status": "plan", "idempotency_key": idem, "customer": customer,
                "product": product, "owner": owner, "contact": contact, "phone": phone,
                "amount": amount, "due_date": due_date, "next": "确认后创建客户、线索、商机"}
        if not approved:
            return plan
        root_id = contracts.new_object_id("opportunity")
        customer_row = self._object("customer", root_id, "", "", owner, "lead", customer, idem=idem)
        customer_id = customer_row["object_id"]
        lead = self._object("lead", root_id, customer_id, "", owner, "lead", product, idem=idem + ":lead")
        opportunity = self._object("opportunity", root_id, customer_id, "", owner, "lead", product, idem=idem + ":opportunity")
        opportunity["object_id"] = root_id
        for row in (customer_row, lead, opportunity):
            self.store.append("objects", row, "object_id")
        evidence = self._evidence(source_event_id, "opportunity", root_id, "%s：%s" % (customer, product))
        approval = self._approval("start", root_id, {}, opportunity, "启动客户案件", [evidence["event_id"]], "approved")
        transition = self._transition(root_id, root_id, "lead", "lead", "案件创建", actor, evidence["event_id"], approval)
        return {"status": "created", "root_id": root_id, "objects": [customer_row, lead, opportunity],
                "evidence": evidence, "approval": approval, "transition": transition}

    def _materialize(self, root_id: str, state: str, reason: str) -> list[dict[str, str]]:
        current = self._objects(root_id)
        root = next(r for r in current if r.get("object_id") == root_id)
        customer_id = root.get("customer_id", "")
        project_id = root.get("project_id", "")
        made = []
        for kind in STATE_OBJECTS.get(state, ()):
            if any(r.get("object_type") == kind for r in current):
                continue
            row = self._object(kind, root_id, customer_id, project_id, root.get("owner", ""), state, reason, idem="mat:%s:%s" % (root_id, kind))
            self.store.append("objects", row, "idempotency_key")
            made.append(row)
            if kind == "project":
                project_id = row["object_id"]
                self.store.update(root_id, {"project_id": project_id})
        if project_id:
            for row in self._objects(root_id):
                if row.get("project_id") == "" and row.get("object_type") not in ("customer", "lead", "opportunity"):
                    self.store.update(row["object_id"], {"project_id": project_id})
        return made

    def advance(self, root_id: str, to_state: str, reason: str,
                actor: str = "automation", source_event_id: str = "",
                approved: bool = False) -> dict[str, Any]:
        current = self._objects(root_id)
        root = next((r for r in current if r.get("object_id") == root_id), None)
        if root is None:
            raise KeyError("对象不存在：%s" % root_id)
        old = root.get("state", "")
        severity = contracts.transition_severity(old, to_state)
        plan = {"status": "plan", "root_id": root_id, "from_state": old,
                "to_state": to_state, "severity": severity, "reason": reason,
                "next_action": handoff_suggestions(to_state)}
        if severity == "illegal":
            plan["status"] = "illegal"
            return plan
        if not approved:
            return plan
        evidence = self._evidence(source_event_id, "opportunity", root_id, reason)
        approval = self._approval("advance", root_id, {"state": old}, {"state": to_state}, reason, [evidence["event_id"]], "approved")
        self.store.update(root_id, {"state": to_state, "next_action": "" if to_state == "closed" else handoff_suggestions(to_state), "updated_at": _now()})
        transition = self._transition(root_id, root_id, old, to_state, reason, actor, evidence["event_id"], approval)
        made = self._materialize(root_id, to_state, reason)
        return {**plan, "status": "advanced", "objects_created": made, "approval": approval, "evidence": evidence, "transition": transition}

    def revise(self, root_id: str, kind: str, summary: str, reason: str,
               actor: str = "automation", source_event_id: str = "",
               approved: bool = False) -> dict[str, Any]:
        if kind not in ("requirement", "solution", "quote"):
            raise ValueError("kind 必须是 requirement/solution/quote")
        current = [r for r in self._objects(root_id) if r.get("object_type") == kind]
        version = max([int(r.get("version") or 0) for r in current] or [0]) + 1
        plan = {"status": "plan", "root_id": root_id, "kind": kind, "version": version, "summary": summary}
        if not approved:
            return plan
        root = next(r for r in self._objects(root_id) if r.get("object_id") == root_id)
        parent_id = current[-1]["object_id"] if current else ""
        row = self._object(kind, root_id, root.get("customer_id", ""), root.get("project_id", ""), actor, "draft", summary, parent_id, version, "rev:%s:%s:%s" % (root_id, kind, version))
        self.store.append("objects", row, "idempotency_key")
        evidence = self._evidence(source_event_id, kind, row["object_id"], reason)
        approval = self._approval("revise", row["object_id"], {}, row, reason, [evidence["event_id"]], "approved")
        return {**plan, "status": "revised", "object": row, "evidence": evidence, "approval": approval}

    def verify(self, root_id: str) -> dict[str, Any]:
        objects = self._objects(root_id)
        root = next((r for r in objects if r.get("object_id") == root_id), None)
        if root is None:
            return {"passed": False, "checks": [], "failures": ["root 不存在"], "coverage": 0}
        state = root.get("state", "lead")
        expected = ["customer", "lead", "opportunity"]
        if state in MAIN_CHAIN:
            for stage in MAIN_CHAIN[2:MAIN_CHAIN.index(state) + 1]:
                expected.extend(STATE_OBJECTS.get(stage, ()))
        if state == "after_sales":
            expected.append("after_sales")
        failures, checks = [], []
        for kind in dict.fromkeys(expected):
            rows = [r for r in objects if r.get("object_type") == kind]
            checks.append(kind + "对象")
            if not rows:
                failures.append(kind + "缺失")
            for row in rows:
                if not contracts.validate_object_id(kind, row.get("object_id", "")):
                    failures.append(kind + " ID 无效")
                if not row.get("owner"):
                    failures.append(kind + "责任人缺失")
                if state != "closed" and not row.get("next_action"):
                    failures.append(kind + "下一步缺失")
        transitions = [r for r in self.store.read("transitions") if r.get("root_id") == root_id]
        evidence = [r for r in self.store.read("evidence") if r.get("related_object_id") == root_id]
        checks.extend(["状态轨迹", "证据链"])
        if not transitions:
            failures.append("状态轨迹缺失")
        if not evidence:
            failures.append("证据链缺失")
        coverage = round((len(checks) - len(failures)) / len(checks) * 100) if checks else 0
        return {"passed": not failures, "checks": checks, "failures": failures, "coverage": coverage, "state": state}

    def status(self, root_id: str) -> dict[str, Any]:
        objects = self._objects(root_id)
        root = next((r for r in objects if r.get("object_id") == root_id), None)
        return {"root_id": root_id, "state": root.get("state") if root else "",
                "next_action": root.get("next_action") if root else "",
                "objects": objects,
                "transitions": [r for r in self.store.read("transitions") if r.get("root_id") == root_id],
                "evidence": [r for r in self.store.read("evidence") if r.get("related_object_id") == root_id],
                "approvals": [r for r in self.store.read("approvals") if r.get("object_id") == root_id]}


def build_full_scenario(customer: str, product: str, owner: str = "项目经理") -> dict[str, Any]:
    return {"customer": customer, "product": product, "owner": owner,
            "states": [{"state": state, "objects": list(STATE_OBJECTS.get(state, ())),
                         "severity": "normal" if index == 0 else "normal"}
                        for index, state in enumerate(contracts.PROJECT_STATES)],
            "gates": ["CRM 自动写入", "production/tasks 人工批准", "合同/发货/结案人工确认"]}


def handoff_suggestions(state: str, context: Mapping[str, Any] | None = None) -> str:
    return {
        "lead": "微信来单已建线索，评估后 python business_loop.py advance --to opportunity",
        "opportunity": "确认商机意向，推进需求确认（requirement_confirming）",
        "requirement_confirming": "AI 从聊天/PDF 提取需求要点，人工确认后 revise 需求版本",
        "solution_confirming": "基于需求 V1 出方案草稿，人工评审后 revise 方案版本",
        "quotation_confirming": "方案版本 + pipeline BOM 成本 → 报价草稿，人工确认后发出",
        "contract_pending": "wxmatch 已发现合同 PDF；核对条款后签署并登记合同信息表",
        "won_and_funded": "python won_deal.py plan → 人工核对 → apply --yes 完成立项三表写入",
        "procurement": "python pipeline/run.py prepare；foresee.py 看缺料后批准采购候选",
        "in_production": "贴片/组装记录回写，工序列表用 op.py stage 跟进工序进度",
        "quality_check": "核对良品率与测试记录，异常走维修记录；通过后推进 ready_to_ship",
        "ready_to_ship": "按 ID/SIM 格式规则生成发货清单草稿，人工确认后写发货清单 + SHP-ID",
        "delivering": "跟踪物流与客户签收，签收证据回传后推进 acceptance",
        "acceptance": "wxmatch 收款匹配回款记录，验收+回款齐后推进 closed",
        "closed": "项目已结案；售后问题走 after_sales",
        "cancelled": "案件已取消；证据链保留备查",
        "after_sales": "登记维修记录（AS-ID），验证处理结果后回 closed 结案",
    }.get(state, "检查当前对象、责任人和下一步")
