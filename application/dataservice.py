# -*- coding: utf-8 -*-
"""统一写入口：显式授权、持久化预占、读回验证及审计台账。

有幂等键的请求在调用 adapter 前记录 sending。响应丢失、进程中断、空
row_id 都属于 outcome_unknown，绝不盲目重发；已知 row_id 只读回核验。
SQLite 负责跨进程串行化，CSV 是人工可读台账，不再充当成功判据。
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import json
import math
import os
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Optional

from . import contracts as C
from . import authorization as AUTH

LEDGER_FILE_NAME = "write_ledger.csv"
LEDGER_FIELDS = ["时间", "路由", "动作", "表", "row_id", "幂等键",
                 "读回验证", "内容摘要", "执行者", "备注"]


@dataclass
class WriteRequest:
    table: str
    row: Mapping[str, Any]
    route: str = "production"
    action: str = "append"
    row_id: str = ""
    idem_key: str = ""  # 显式留空表示独立操作；自动化调用方必须提供稳定键
    actor: str = ""
    reason: str = ""
    verify_fields: tuple = ()
    grant: Optional[AUTH.WriteGrant] = None


@dataclass
class WriteResult:
    status: str
    row_id: str = ""
    verified: bool = False
    message: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "candidate" or (
            self.status in ("written", "skipped_reuse") and self.verified)


class DataService:
    def __init__(self, adapter_factory: Optional[Callable[[str], Any]] = None,
                 data_dir: str = ""):
        self._factory = adapter_factory
        self.data_dir = os.path.abspath(data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))

    def write(self, req: WriteRequest, mode: str = C.MODE_PREVIEW) -> WriteResult:
        if req.route not in C.ROUTE_POLICIES:
            return WriteResult("blocked", message="未知路由 %r" % req.route)
        if mode not in (C.MODE_PREVIEW, C.MODE_APPLY):
            return WriteResult("blocked", message="未知运行模式 %r" % mode)
        if req.action not in ("append", "update"):
            return WriteResult("blocked", message="不支持的写入动作 %r" % req.action)
        if not isinstance(req.table, str) or not req.table.strip():
            return WriteResult("blocked", message="写入目标表不能为空")
        if not isinstance(req.row, Mapping) or not req.row:
            return WriteResult("blocked", message="写入载荷必须是非空对象")
        if req.action == "update" and not req.row_id:
            return WriteResult("blocked", message="update 需要 row_id")
        if req.verify_fields and any(k not in req.row for k in req.verify_fields):
            return WriteResult("blocked", message="verify_fields 包含载荷中不存在的字段")
        try:
            fingerprint = AUTH.payload_hash({"row": dict(req.row), "row_id": req.row_id,
                                             "verify_fields": list(req.verify_fields)})
        except (ValueError, TypeError):
            return WriteResult("blocked", message="载荷不是有效的 JSON 数据")
        if mode != C.MODE_APPLY:
            return WriteResult("candidate", message="preview：候选（未写）", evidence=dict(req.row))
        approval_required = C.ROUTE_POLICIES[req.route] == C.WRITE_APPROVAL_REQUIRED
        if approval_required:
            # 复用只读结果不消耗新次数；真正发送前由 reserve 重读并强校验。
            ok, why = AUTH.authorize_write(req.grant, req.table, req.action,
                                          route=req.route, row_id=req.row_id,
                                          row=req.row, check_uses=False)
            if not ok:
                return WriteResult("blocked", message=why)
        try:
            adapter = self._get_adapter(req.route)
        except Exception as exc:
            return WriteResult("blocked", message="初始化目标后端失败（未发送写入）：%s" % exc)
        target = self._target_identity(adapter, req.route)
        operation_key = AUTH.payload_hash({"target": target, "route": req.route,
                                           "table": req.table, "action": req.action,
                                           "key": req.idem_key or uuid.uuid4().hex})
        db = None
        try:
            db = self._connect()
            # 从查重到完成记账保持同一排他事务，防止并发读回/CSV追加互相踩踏。
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operations WHERE operation_key=?",
                             (operation_key,)).fetchone()
            if old is not None:
                if old["request_hash"] != fingerprint:
                    return WriteResult("blocked", row_id=old["row_id"],
                                       message="同一幂等键对应不同载荷，拒绝复用或重写")
                return self._reconcile(db, old, req, adapter)
            # 旧版本只有 CSV，没有请求摘要；保守地核验其 row_id，不自动重发。
            legacy = self._legacy_entry(req)
            if legacy is not None:
                rid = legacy.get("row_id") or ""
                state = "verify_failed" if rid else "outcome_unknown"
                db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)",
                           (operation_key, fingerprint, state, rid, "旧台账待核验", 1))
                db.commit()
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT * FROM operations WHERE operation_key=?",
                                 (operation_key,)).fetchone()
                return self._reconcile(db, old, req, adapter)
            if approval_required:
                try:
                    reserved = AUTH.GrantStore(os.path.join(
                        self.data_dir, AUTH.GRANTS_DIR_NAME)).reserve(
                            req.grant, table=req.table, action=req.action,
                            route=req.route, row_id=req.row_id, row=req.row)
                    req = replace(req, grant=reserved)
                except (OSError, sqlite3.Error, PermissionError, ValueError) as exc:
                    return WriteResult("blocked", message="授权预占失败（未写）：%s" % exc)
            # 先持久化 sending，再发请求。进程在任何后续位置中断也不会重复 append。
            db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)",
                       (operation_key, fingerprint, "sending", req.row_id if req.action == "update" else "",
                        "写入结果尚未确认", 0))
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            try:
                if req.action == "update":
                    adapter.update_row(req.table, req.row_id, dict(req.row))
                    rid = req.row_id
                else:
                    rid = adapter.append_row(req.table, dict(req.row))
                if not rid:
                    return self._finish(db, operation_key, req, "outcome_unknown", "",
                                        "写入返回空 row_id；禁止自动重发，需人工核对")
            except Exception as exc:
                return self._finish(db, operation_key, req, "outcome_unknown",
                                    req.row_id if req.action == "update" else "",
                                    "写入响应未知，禁止自动重发：%s" % exc)
            # 先保存已知 row_id，读回/CSV失败后仍能定位原行。
            db.execute("UPDATE operations SET state=?, row_id=? WHERE operation_key=?",
                       ("verify_failed", str(rid), operation_key))
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            verified, detail = self.verify_readback(adapter, req, str(rid))
            return self._finish(db, operation_key, req,
                                "written" if verified else "verify_failed", str(rid), detail)
        except (OSError, sqlite3.Error, ValueError) as exc:
            # 持久化失败可能发生在写后，绝不能声称未写或让上层视为成功。
            return WriteResult("outcome_unknown", message="写入状态无法可靠落盘，需核对：%s" % exc)
        finally:
            if db is not None:
                db.close()  # 未提交的事务回滚，已提交的 sending 保留

    def _connect(self):
        os.makedirs(self.data_dir, exist_ok=True)
        db = sqlite3.connect(os.path.join(self.data_dir, "write_operations.sqlite3"), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS operations (operation_key TEXT PRIMARY KEY, "
                   "request_hash TEXT NOT NULL, state TEXT NOT NULL, row_id TEXT NOT NULL, "
                   "detail TEXT NOT NULL, accounted INTEGER NOT NULL)")
        db.commit()
        return db

    def _reconcile(self, db, old, req, adapter) -> WriteResult:
        rid = old["row_id"]
        if not rid:
            return WriteResult("outcome_unknown", message=(
                "上次请求结果未知且没有 row_id；禁止重发，需人工核对远端后处置"),
                evidence={"idem_key": req.idem_key, "state": old["state"]})
        verified, detail = self.verify_readback(adapter, req, rid)
        if verified:
            if not old["accounted"] or old["state"] != "written":
                result = self._finish(db, old["operation_key"], req, "written", rid, detail)
                if not result.ok:
                    return result
            return WriteResult("skipped_reuse", row_id=rid, verified=True,
                               message="复用原 row_id，重新读回验证通过（未重写）")
        return self._finish(db, old["operation_key"], req, "verify_failed", rid, detail)

    def _finish(self, db, key, req, state: str, rid: str, detail: str) -> WriteResult:
        verified = state == "written"
        try:
            self._ledger(req, rid, verified, detail)
            accounted = 1
        except OSError as exc:
            state, verified, accounted = "outcome_unknown", False, 0
            detail = "台账落盘失败；已有 row_id 只可核验，不得重写：%s" % exc
        db.execute("UPDATE operations SET state=?, row_id=?, detail=?, accounted=? "
                   "WHERE operation_key=?", (state, rid, detail, accounted, key))
        db.commit()
        prefix = "已写入并读回验证通过：" if verified else (
            "读回验证失败：" if state == "verify_failed" else "")
        return WriteResult(state, row_id=rid, verified=verified, message=prefix + detail,
                           evidence={"row_id": rid, "idem_key": req.idem_key})

    @staticmethod
    def _target_identity(adapter: Any, route: str) -> str:
        # 不包含 token；目标变化不允许借用另一 Base/本地库的历史成功结果。
        identity = [type(adapter).__module__, type(adapter).__qualname__, route]
        for name in ("server", "uuid", "base_name", "root", "data_dir"):
            value = getattr(adapter, name, "")
            if value:
                identity.append("%s=%s" % (name, value))
        return hashlib.sha256("|".join(identity).encode("utf-8")).hexdigest()

    def verify_readback(self, adapter: Any, req: WriteRequest, rid: str) -> tuple[bool, str]:
        rows = self._safe_list_rows(adapter, req.table)
        if isinstance(rows, str):
            return False, rows
        target = next((r for r in rows if str(r.get("__row_id__", "")) == str(rid)), None)
        if target is None:
            return False, "row_id=%s 在表「%s」读回后不存在" % (rid, req.table)
        # 控制字段不是业务列；结构化值必须比对，不能用行存在代替写入正确。
        fields = req.verify_fields or tuple(k for k in req.row if k != "__expected_version__")
        mismatch = []
        for key in fields:
            want = req.row[key]
            got = target.get(key)
            if not self._value_equal(want, got):
                mismatch.append("%s: 期望 %r 实得 %r" % (key, want, got))
        if mismatch:
            return False, "字段不一致（%s）" % "; ".join(mismatch[:3])
        if not fields:
            return False, "没有可验证的业务字段"
        return True, "字段一致（%d 项）" % len(fields)

    @staticmethod
    def _value_equal(want, got) -> bool:
        if want is None or want == "":
            return got is None or got == ""
        if isinstance(want, bool):
            return (type(got) is bool and got is want) or (
                isinstance(got, str) and got.strip().lower() == str(want).lower())
        if isinstance(want, (int, float)):
            if isinstance(got, bool) or got is None or got == "":
                return False
            try:
                return math.isfinite(float(got)) and float(want) == float(got)
            except (TypeError, ValueError, OverflowError):
                return False
        if isinstance(want, (list, tuple)):
            # 多选/链接列：云端会归一化顺序，所以按集合比较（多一个少一个仍要抓）；
            # 但绝不「只要行存在就算过」——那正是静默丢列能被放行的原因。
            if not isinstance(got, (list, tuple)):
                return False
            try:
                dump = lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True)
                return sorted(dump(x) for x in want) == sorted(dump(x) for x in got)
            except (TypeError, ValueError):
                return False
        if isinstance(want, Mapping):
            try:
                if not isinstance(got, Mapping):
                    return False
                return (json.dumps(dict(want), ensure_ascii=False, sort_keys=True)
                        == json.dumps(dict(got), ensure_ascii=False, sort_keys=True))
            except (TypeError, ValueError):
                return False
        return str(want) == str(got) if got is not None else False

    @staticmethod
    def write_verified(adapter: Any, table: str, row: Mapping[str, Any],
                       action: str = "append", row_id: str = "") -> tuple[str, bool, str]:
        """低层兼容原语，不提供授权/幂等；业务自动化应使用 write。"""
        if action not in ("append", "update"):
            return "", False, "不支持的写入动作"
        if action == "update":
            if not row_id:
                return "", False, "update 需要 row_id"
            adapter.update_row(table, row_id, dict(row))
            rid = row_id
        else:
            rid = adapter.append_row(table, dict(row))
            if not rid:
                return "", False, "写入返回空 row_id，结果未知"
        ds = DataService.__new__(DataService)
        verified, detail = ds.verify_readback(adapter, WriteRequest(
            table=table, row=row, action=action, row_id=rid), rid)
        return rid, verified, detail

    @staticmethod
    def _safe_list_rows(adapter: Any, table: str):
        try:
            rows = adapter.list_rows(table)
            if not isinstance(rows, (list, tuple)) or any(not isinstance(r, Mapping) for r in rows):
                return "读回 list_rows 返回了非法结构"
            return rows
        except Exception as exc:
            return "读回 list_rows 异常：%s" % exc

    def _get_adapter(self, route: str) -> Any:
        if self._factory is None:
            from adapters.factory import load_config, get_adapter
            cfg = load_config()
            adapter = get_adapter(cfg, base_name=route, strict=True)
            adapter.auth()
            return adapter
        return self._factory(route)

    def _ledger_path(self) -> str:
        return os.path.join(self.data_dir, LEDGER_FILE_NAME)

    def _legacy_entry(self, req: WriteRequest):
        if not req.idem_key or not os.path.exists(self._ledger_path()):
            return None
        with open(self._ledger_path(), encoding="utf-8-sig", newline="") as f:
            matches = [r for r in csv.DictReader(f)
                       if r.get("幂等键") == req.idem_key and r.get("路由") == req.route
                       and r.get("表") == req.table and r.get("动作") == req.action]
        return matches[-1] if matches else None

    def _ledger(self, req: WriteRequest, rid: str, verified: bool, detail: str) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        path = self._ledger_path()
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(LEDGER_FIELDS)
            summary = "; ".join("%s=%s" % kv for kv in list(req.row.items())[:3])
            grant_tag = "[%s] " % req.grant.grant_id if req.grant else "[自动策略] "
            writer.writerow([_dt.datetime.now().isoformat(timespec="seconds"),
                             req.route, req.action, req.table, rid, req.idem_key,
                             "通过" if verified else "失败", summary[:120], req.actor,
                             (grant_tag + detail + ("; " + req.reason if req.reason else ""))[:240]])
            f.flush()
            os.fsync(f.fileno())
