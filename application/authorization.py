# -*- coding: utf-8 -*-
"""application/authorization.py — 写库授权闸门（可信执行层 v1，2026-09-26）。

━━ 这条规则为什么存在 ━━
「高置信」是**匹配算法的自评**：金额差在 2% 以内、供应商名唯一命中。
它回答的是「这条消息像不像真的」，**不是**「人同意这次写入吗」。
一旦把置信度当授权用，算法的一次误判就会直接改掉真实业务表，
而且全程没有任何人过目 —— 这正是本期改造要拆掉的东西。

所以本模块把授权从置信度里**彻底摘出来**：
  - 任何业务表（production / tasks）写入，必须携带一份 WriteGrant；
  - WriteGrant 只有两个来源：
      1. 人工审批通过的 ApprovalRequest（source=approval）；
      2. 人亲手写下的授权文件（source=manual，经 --grant-file 传入）；
  - 本模块**刻意不接收、不读取任何置信度字段** —— 高/中/低一视同仁。
    置信度只允许影响「哪些条目进入待授权清单」，不允许影响「是否放行」。

设计约束：纯数据结构 + 局部文件 I/O，不 import 任何线上适配器，可离线单测。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, Optional, Sequence

GRANTS_DIR_NAME = "approvals"

# 授权来源
SOURCE_MANUAL = "manual"        # 人亲手写的授权文件
SOURCE_APPROVAL = "approval"    # 人工审批通过的 ApprovalRequest

# 动作通配符
ACTION_ANY = "any"


def new_grant_id(now: Optional[_dt.datetime] = None) -> str:
    """GRT-YYYYMMDD-xxxxxx 形式的授权编号。"""
    now = now or _dt.datetime.now()
    return "GRT-%s-%s" % (now.strftime("%Y%m%d"), uuid.uuid4().hex[:6])


def grants_dir(skill_dir: str) -> str:
    return os.path.join(skill_dir, "data", GRANTS_DIR_NAME)


def _parse_dt(value: str) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class WriteGrant:
    """一次写库的**显式**授权凭证。没有它，再高的置信度也写不进去。

    tables / actions 为空元组表示「不限」——不限表只应在人工授权文件里
    明确写出；程序化生成的授权（如审批转化）会强制限定范围。
    """
    grant_id: str
    tables: tuple = ()          # 允许写入的表名；空 = 不限表
    actions: tuple = ()         # 允许的动作（append / update）；空 = 不限
    actor: str = ""             # 授权人（谁点的头）
    reason: str = ""            # 为什么批准
    issued_at: str = ""
    expires_at: str = ""        # 空 = 不过期
    max_uses: int = 0           # 0 = 不限次
    used: int = 0               # 已消耗次数
    source: str = SOURCE_MANUAL
    approval_id: str = ""       # source=approval 时对应的审批编号
    note: str = ""

    # ── 判定 ────────────────────────────────────────────────
    def covers(self, table: str, action: str,
               now: Optional[_dt.datetime] = None) -> tuple[bool, str]:
        """这份授权是否覆盖 (table, action)。返回 (是否放行, 原因)。"""
        now = now or _dt.datetime.now()
        if self.expires_at:
            exp = _parse_dt(self.expires_at)
            if exp is None:
                return False, "授权 %s 的 expires_at 无法解析：%r" % (
                    self.grant_id, self.expires_at)
            if now > exp:
                return False, "授权 %s 已于 %s 过期" % (self.grant_id, self.expires_at)
        if self.max_uses and self.used >= self.max_uses:
            return False, "授权 %s 已用尽（%d/%d 次）" % (
                self.grant_id, self.used, self.max_uses)
        if self.tables and table not in self.tables:
            return False, "授权 %s 不含表「%s」（仅限：%s）" % (
                self.grant_id, table, "、".join(self.tables))
        acts = tuple(a for a in self.actions if a)
        if acts and ACTION_ANY not in acts and action not in acts:
            return False, "授权 %s 不含动作「%s」（仅限：%s）" % (
                self.grant_id, action, "、".join(acts))
        return True, ""

    def consumed(self) -> "WriteGrant":
        """用掉一次授权，返回新凭证（不落盘，由 GrantStore 负责回写）。"""
        return replace(self, used=self.used + 1)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tables"] = list(self.tables)
        d["actions"] = list(self.actions)
        return d

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WriteGrant":
        known = set(cls.__dataclass_fields__)
        kw = {k: v for k, v in dict(data).items() if k in known}
        kw["tables"] = tuple(kw.get("tables") or ())
        kw["actions"] = tuple(kw.get("actions") or ())
        kw["used"] = int(kw.get("used") or 0)
        kw["max_uses"] = int(kw.get("max_uses") or 0)
        kw.setdefault("grant_id", new_grant_id())
        if not kw.get("issued_at"):
            kw["issued_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        return cls(**kw)


# ────────────────────────────────────────────────────────────
# 便捷构造
# ────────────────────────────────────────────────────────────
def manual_grant(tables: Sequence[str] = (), actions: Sequence[str] = (),
                 actor: str = "", reason: str = "", expires_at: str = "",
                 max_uses: int = 0, note: str = "") -> WriteGrant:
    """构造一份人工授权（对应 --grant-file 里的内容）。"""
    return WriteGrant(
        grant_id=new_grant_id(),
        tables=tuple(t for t in tables if t),
        actions=tuple(a for a in actions if a),
        actor=actor, reason=reason,
        issued_at=_dt.datetime.now().isoformat(timespec="seconds"),
        expires_at=expires_at, max_uses=int(max_uses or 0),
        source=SOURCE_MANUAL, note=note,
    )


def grant_from_approval(approval: Any, tables: Sequence[str] = ()) -> WriteGrant:
    """把一条**已通过**的审批请求转成写库授权。

    未通过的审批直接抛 PermissionError —— 这是唯一的用法错误，
    必须让调用方立刻发现，而不是静默降级成「无授权」。
    """
    status = str(getattr(approval, "status", "") or "")
    if status != "approved":
        raise PermissionError(
            "审批 %s 未通过（status=%r），不能转为写库授权"
            % (getattr(approval, "approval_id", "?"), status))
    return WriteGrant(
        grant_id=new_grant_id(),
        tables=tuple(t for t in tables if t),
        actions=(ACTION_ANY,),
        actor=getattr(approval, "approver", "") or "unknown",
        reason=getattr(approval, "reason", "") or "",
        issued_at=_dt.datetime.now().isoformat(timespec="seconds"),
        source=SOURCE_APPROVAL,
        approval_id=getattr(approval, "approval_id", ""),
    )


# ────────────────────────────────────────────────────────────
# 授权仓库
# ────────────────────────────────────────────────────────────
class GrantStore:
    """授权凭证的本地仓库：data/approvals/<grant_id>.json。

    刻意做成「一个授权一个文件」——便于人在文件管理器里肉眼核对、
    单独撤销（删文件即失效），也天然留下时间戳证据。
    """

    def __init__(self, data_dir: str = ""):
        self.dir = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", GRANTS_DIR_NAME)

    def path(self, grant_id: str) -> str:
        return os.path.join(self.dir, "%s.json" % grant_id)

    def save(self, grant: WriteGrant) -> str:
        os.makedirs(self.dir, exist_ok=True)
        p = self.path(grant.grant_id)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(grant.to_dict(), f, ensure_ascii=False, indent=2)
        return p

    def load(self, grant_id: str) -> Optional[WriteGrant]:
        return self.load_file(self.path(grant_id))

    @staticmethod
    def load_file(path: str) -> Optional[WriteGrant]:
        """从人指定路径读授权文件（--grant-file）。读不到/格式错返回 None。"""
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return WriteGrant.from_dict(json.load(f))
        except (OSError, ValueError):
            return None

    def list_all(self) -> list[WriteGrant]:
        if not os.path.isdir(self.dir):
            return []
        out = []
        for fn in sorted(os.listdir(self.dir)):
            if fn.endswith(".json"):
                g = self.load(fn[:-5])
                if g is not None:
                    out.append(g)
        return out

    def find_valid(self, table: str, action: str,
                   now: Optional[_dt.datetime] = None) -> Optional[WriteGrant]:
        """找一份当前有效的授权（用于晚间自动化「有授权才写」）。"""
        for g in self.list_all():
            ok, _ = g.covers(table, action, now=now)
            if ok:
                return g
        return None

    def live_grants(self, now: Optional[_dt.datetime] = None) -> list[WriteGrant]:
        """列出当前未过期、未用尽的授权（**不校验表/动作**）。

        晚间链路用这个判断「今晚有没有给人批过的写入授权」；
        具体某条写入能不能落到某张表，仍由 DataService 按表逐条裁决。
        """
        now = now or _dt.datetime.now()
        out = []
        for g in self.list_all():
            if g.max_uses and g.used >= g.max_uses:
                continue
            if g.expires_at:
                exp = _parse_dt(g.expires_at)
                if exp is None or now > exp:
                    continue
            out.append(g)
        return out

    def consume(self, grant: WriteGrant) -> WriteGrant:
        """消耗一次并回写（文件不存在则创建，方便手动造的授权被正确计数）。"""
        nxt = grant.consumed()
        self.save(nxt)
        return nxt


# ────────────────────────────────────────────────────────────
# 供 DataService 调用的统一裁决
# ────────────────────────────────────────────────────────────
def authorize_write(grant: Optional[WriteGrant], table: str, action: str,
                    now: Optional[_dt.datetime] = None) -> tuple[bool, str]:
    """统一裁决：没有授权一律拒绝，且原因里要说清「置信度不算数」。"""
    if grant is None:
        return False, ("写库需要显式授权（高置信度不构成授权）："
                       "请走人工确认，或用 --grant-file 提供授权文件")
    return grant.covers(table, action, now=now)


__all__ = [
    "GRANTS_DIR_NAME", "SOURCE_MANUAL", "SOURCE_APPROVAL", "ACTION_ANY",
    "new_grant_id", "grants_dir", "WriteGrant", "GrantStore",
    "manual_grant", "grant_from_approval", "authorize_write",
]
