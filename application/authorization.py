# -*- coding: utf-8 -*-
"""显式写库授权。文件决定授权范围，持久化预占决定剩余次数。

置信度不构成授权。授权必须在发送写请求前预占；超时、读回失败和进程
中断均不退还次数，避免用重试扩大一次性授权。SQLite 串行化跨进程预占，
JSON 仍可人工核对/撤销（删除授权文件即撤销，旧对象不能复活它）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping, Optional, Sequence

GRANTS_DIR_NAME = "approvals"
SOURCE_MANUAL = "manual"
SOURCE_APPROVAL = "approval"
ACTION_ANY = "any"
_ACTIONS = {"append", "update", ACTION_ANY}
_ROUTES = {"production", "tasks", "crm"}


def new_grant_id(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    return "GRT-%s-%s" % (now.strftime("%Y%m%d"), uuid.uuid4().hex[:12])


def grants_dir(skill_dir: str) -> str:
    return os.path.join(skill_dir, "data", GRANTS_DIR_NAME)


def _parse_dt(value: str) -> Optional[_dt.datetime]:
    try:
        value = _dt.datetime.fromisoformat(str(value).strip())
        # 老文件的无时区时间按本机时区解释，与旧版 now() 的语义一致。
        return value.astimezone(_dt.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def payload_hash(row: Mapping[str, Any]) -> str:
    """保留类型的规范化摘要；不把 False、0、空串等混为一谈。"""
    raw = json.dumps(dict(row), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WriteGrant:
    grant_id: str
    tables: tuple = ()
    actions: tuple = ()
    actor: str = ""
    reason: str = ""
    issued_at: str = ""
    expires_at: str = ""
    max_uses: int = 0
    used: int = 0
    source: str = SOURCE_MANUAL
    approval_id: str = ""
    note: str = ""
    routes: tuple = ()
    row_id: str = ""
    payload_hash: str = ""
    # 只在可信文件加载入口赋值，不接受 JSON 注入，也不写回授权内容。
    _source_path: str = field(default="", repr=False, compare=False)

    def validate(self) -> tuple[bool, str]:
        if not isinstance(self.grant_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.grant_id):
            return False, "授权 grant_id 为空或包含非法路径字符"
        for name in ("tables", "actions", "routes"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(
                    not isinstance(x, str) or not x.strip() for x in values):
                return False, "授权 %s 必须是非空字符串的列表（不限范围请显式写 []）" % name
        if any(x not in _ACTIONS for x in self.actions):
            return False, "授权包含不支持的动作"
        if any(x not in _ROUTES for x in self.routes):
            return False, "授权包含未知路由"
        for name in ("max_uses", "used"):
            n = getattr(self, name)
            if type(n) is not int or n < 0:
                return False, "授权 %s 必须是非负整数" % name
        if self.source not in (SOURCE_MANUAL, SOURCE_APPROVAL):
            return False, "授权来源非法"
        if not self.issued_at or _parse_dt(self.issued_at) is None:
            return False, "授权 issued_at 无法解析"
        if self.expires_at and _parse_dt(self.expires_at) is None:
            return False, "授权 expires_at 无法解析"
        if self.payload_hash and not re.fullmatch(r"[0-9a-f]{64}", self.payload_hash):
            return False, "授权载荷摘要非法"
        if self.source == SOURCE_APPROVAL and not (
                self.approval_id and self.actor and len(self.tables) == 1
                and len(self.routes) == 1 and len(self.actions) == 1
                and self.actions[0] in ("append", "update") and self.payload_hash
                and self.max_uses == 1
                and (self.actions[0] != "update" or self.row_id)):
            return False, "审批授权必须绑定单一路由、表、动作、目标与载荷，且仅限一次"
        return True, ""

    def covers(self, table: str, action: str,
               now: Optional[_dt.datetime] = None, *, route: str = "",
               check_uses: bool = True) -> tuple[bool, str]:
        valid, why = self.validate()
        if not valid:
            return False, why
        if action not in ("append", "update"):
            return False, "不支持的写入动作：%s" % action
        now = (now or _dt.datetime.now().astimezone()).astimezone(_dt.timezone.utc)
        if now < _parse_dt(self.issued_at):
            return False, "授权尚未生效"
        if self.expires_at and now >= _parse_dt(self.expires_at):
            return False, "授权 %s 已于 %s 过期" % (self.grant_id, self.expires_at)
        if check_uses and self.max_uses and self.used >= self.max_uses:
            return False, "授权 %s 已用尽（%d/%d 次）" % (
                self.grant_id, self.used, self.max_uses)
        if self.tables and table not in self.tables:
            return False, "授权 %s 不含表「%s」" % (self.grant_id, table)
        if self.actions and ACTION_ANY not in self.actions and action not in self.actions:
            return False, "授权 %s 不含动作「%s」" % (self.grant_id, action)
        if route and self.routes and route not in self.routes:
            return False, "授权 %s 不含路由「%s」" % (self.grant_id, route)
        return True, ""

    def consumed(self) -> "WriteGrant":
        return replace(self, used=self.used + 1)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("_source_path", None)
        for name in ("tables", "actions", "routes"):
            data[name] = list(data[name])
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WriteGrant":
        required = {"grant_id", "tables", "actions", "issued_at", "expires_at", "max_uses"}
        if not isinstance(data, Mapping) or not required.issubset(data):
            raise ValueError("授权文件缺少显式编号、范围、有效期或次数；拒绝默认无限授权")
        known = set(cls.__dataclass_fields__) - {"_source_path"}
        kw = {k: v for k, v in data.items() if k in known}
        for name in ("tables", "actions", "routes"):
            values = kw.get(name, ())
            if not isinstance(values, (list, tuple)):
                raise ValueError("授权 %s 必须是列表" % name)
            kw[name] = tuple(values)
        grant = cls(**kw)
        valid, why = grant.validate()
        if not valid:
            raise ValueError(why)
        return grant


def manual_grant(tables: Sequence[str] = (), actions: Sequence[str] = (),
                 actor: str = "", reason: str = "", expires_at: str = "",
                 max_uses: int = 0, note: str = "", *,
                 routes: Sequence[str] = ()) -> WriteGrant:
    grant = WriteGrant(
        grant_id=new_grant_id(), tables=tuple(tables), actions=tuple(actions),
        actor=actor, reason=reason,
        issued_at=_dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        expires_at=expires_at, max_uses=max_uses, source=SOURCE_MANUAL,
        note=note, routes=tuple(routes))
    valid, why = grant.validate()
    if not valid:
        raise ValueError(why)
    return grant


def grant_from_approval(approval: Any, tables: Sequence[str] = (), *,
                        route: str = "production", action: str = "update") -> WriteGrant:
    """审批不再转成不限动作/次数的通行证，必须绑定批准的 after 载荷。"""
    if getattr(approval, "status", "") != "approved":
        raise PermissionError("审批未通过，不能转为写库授权")
    target_tables = tuple(tables) or (getattr(approval, "object_type", ""),)
    after = getattr(approval, "after", None)
    if not isinstance(after, Mapping) or not after:
        raise PermissionError("审批未包含明确的 after 载荷")
    grant = WriteGrant(
        grant_id=new_grant_id(), tables=target_tables, actions=(action,),
        routes=(route,), row_id=str(getattr(approval, "object_id", "") or ""),
        payload_hash=payload_hash(after), max_uses=1,
        actor=str(getattr(approval, "approver", "") or ""),
        reason=str(getattr(approval, "reason", "") or ""),
        issued_at=_dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        source=SOURCE_APPROVAL,
        approval_id=str(getattr(approval, "approval_id", "") or ""))
    valid, why = grant.validate()
    if not valid:
        raise PermissionError(why)
    return grant


class GrantStore:
    def __init__(self, data_dir: str = ""):
        self.dir = os.path.abspath(data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", GRANTS_DIR_NAME))

    def path(self, grant_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", str(grant_id)):
            raise ValueError("非法授权编号")
        return os.path.join(self.dir, "%s.json" % grant_id)

    def _connect(self):
        os.makedirs(self.dir, exist_ok=True)
        db = sqlite3.connect(os.path.join(self.dir, ".usage.sqlite3"), timeout=15)
        db.execute("CREATE TABLE IF NOT EXISTS grants "
                   "(grant_id TEXT PRIMARY KEY, path TEXT NOT NULL, used INTEGER NOT NULL)")
        db.commit()
        return db

    @staticmethod
    def _save_at(grant: WriteGrant, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".grant-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(grant.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def save(self, grant: WriteGrant) -> str:
        valid, why = grant.validate()
        if not valid:
            raise ValueError(why)
        path = self.path(grant.grant_id)
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT path, used FROM grants WHERE grant_id=?",
                                 (grant.grant_id,)).fetchone()
                if old and os.path.normcase(old[0]) != os.path.normcase(path):
                    raise PermissionError("同一授权编号不可更换来源路径")
                used = max(grant.used, old[1] if old else 0)
                self._save_at(replace(grant, used=used), path)
                db.execute("INSERT OR REPLACE INTO grants VALUES (?, ?, ?)",
                           (grant.grant_id, path, used))
            return path
        finally:
            db.close()

    def load(self, grant_id: str) -> Optional[WriteGrant]:
        grant = self.load_file(self.path(grant_id))
        return grant if grant is not None and grant.grant_id == grant_id else None

    @staticmethod
    def load_file(path: str) -> Optional[WriteGrant]:
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as f:
                grant = WriteGrant.from_dict(json.load(f))
            return replace(grant, _source_path=os.path.abspath(path))
        except (OSError, ValueError, TypeError):
            return None

    def list_all(self) -> list[WriteGrant]:
        if not os.path.isdir(self.dir):
            return []
        return [g for fn in sorted(os.listdir(self.dir))
                if fn.endswith(".json") and not fn.startswith(".")
                for g in [self.load_file(os.path.join(self.dir, fn))] if g is not None]

    def find_valid(self, table: str, action: str,
                   now: Optional[_dt.datetime] = None) -> Optional[WriteGrant]:
        return next((g for g in self.list_all() if g.covers(table, action, now)[0]), None)

    def live_grants(self, now: Optional[_dt.datetime] = None) -> list[WriteGrant]:
        now = (now or _dt.datetime.now().astimezone()).astimezone(_dt.timezone.utc)
        return [g for g in self.list_all()
                if (not g.max_uses or g.used < g.max_uses)
                and _parse_dt(g.issued_at) <= now
                and (not g.expires_at or now < _parse_dt(g.expires_at))]

    def reserve(self, grant: WriteGrant, *, table: str = "", action: str = "",
                route: str = "", row_id: str = "",
                row: Optional[Mapping[str, Any]] = None) -> WriteGrant:
        """原子地重读授权并预占一次；任何持久化错误都在业务写入前抛出。"""
        valid, why = grant.validate()
        if not valid:
            raise PermissionError(why)
        source = os.path.abspath(grant._source_path) if grant._source_path else self.path(grant.grant_id)
        # 同一外部授权在不同 DataService/data_dir 使用，也共享同一个次数仓库。
        if os.path.normcase(os.path.dirname(source)) != os.path.normcase(self.dir):
            return GrantStore(os.path.dirname(source)).reserve(
                grant, table=table, action=action, route=route, row_id=row_id, row=row)
        db = self._connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT path, used FROM grants WHERE grant_id=?",
                                      (grant.grant_id,)).fetchone()
                if previous and os.path.normcase(previous[0]) != os.path.normcase(source):
                    raise PermissionError("授权编号的来源路径不一致")
                current = self.load_file(source)
                if current is None:
                    if grant._source_path or previous or os.path.exists(source):
                        raise PermissionError("授权文件已撤销、损坏或不可读")
                    current = grant  # 可信调用方显式构造的首次内存授权
                if current.grant_id != grant.grant_id:
                    raise PermissionError("授权编号与来源文件不一致")
                current = replace(current, used=max(current.used, previous[1] if previous else 0))
                if table or action:
                    ok, why = authorize_write(current, table, action, route=route,
                                              row_id=row_id, row=row)
                    if not ok:
                        raise PermissionError(why)
                elif current.max_uses and current.used >= current.max_uses:
                    raise PermissionError("授权已用尽")
                reserved = replace(current, used=current.used + 1, _source_path=source)
                # 文件先 fsync；即使随后的数据库提交失败，次数也不会倒退。
                self._save_at(reserved, source)
                db.execute("INSERT OR REPLACE INTO grants VALUES (?, ?, ?)",
                           (grant.grant_id, source, reserved.used))
            return reserved
        finally:
            db.close()

    def consume(self, grant: WriteGrant) -> WriteGrant:
        """兼容旧调用；写链必须在发送前调用 reserve 并传完整请求范围。"""
        return self.reserve(grant)


def authorize_write(grant: Optional[WriteGrant], table: str, action: str,
                    now: Optional[_dt.datetime] = None, *, route: str = "",
                    row_id: str = "", row: Optional[Mapping[str, Any]] = None,
                    check_uses: bool = True) -> tuple[bool, str]:
    if grant is None:
        return False, ("写库需要显式授权（高置信度不构成授权）："
                       "请走人工确认，或用 --grant-file 提供授权文件")
    ok, why = grant.covers(table, action, now, route=route, check_uses=check_uses)
    if not ok:
        return False, why
    if grant.routes and route not in grant.routes:
        return False, "授权未覆盖该路由"
    if grant.row_id and row_id != grant.row_id:
        return False, "授权未覆盖该目标行"
    if grant.payload_hash:
        try:
            same = row is not None and payload_hash(row) == grant.payload_hash
        except (TypeError, ValueError):
            same = False
        if not same:
            return False, "实际载荷与人工批准的内容不一致"
    return True, ""


__all__ = ["GRANTS_DIR_NAME", "SOURCE_MANUAL", "SOURCE_APPROVAL", "ACTION_ANY",
           "new_grant_id", "grants_dir", "WriteGrant", "GrantStore", "manual_grant",
           "grant_from_approval", "authorize_write", "payload_hash"]
