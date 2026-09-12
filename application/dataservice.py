# -*- coding: utf-8 -*-
"""application/dataservice.py — 统一写入服务（Phase 0 / P0-4）。

职责（对应 docs/avatar-loop-v2.md 的「执行层收口」）：
  1. **统一入口**：所有 SeaTable 写入走 DataService.write()，不再各自调 adapter；
  2. **路由策略**：按 contracts.ROUTE_POLICIES 决定真写还是出候选
     （production/tasks → 候选制，必须人工 approve；crm → 自动写 + 台账）；
  3. **读回验证**：写完立刻读回来核对关键字段（中文列名），不一致 = 写入失败
     （2026-09-11 教训：中文列名写错会 HTTP 200 + 0 行更新，静默丢数据）；
  4. **幂等键**：同键重写 → 复用，台账可追溯；
  5. **台账**：每次自动写都记 data/write_ledger.csv。

设计约束：本模块可离线单测 —— adapter 通过构造注入（惰性工厂），
不在 import 时做任何 I/O。preview 模式只产草稿不落任何写入。
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from . import contracts as C

# ────────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────────
LEDGER_FILE_NAME = "write_ledger.csv"
LEDGER_FIELDS = ["时间", "路由", "动作", "表", "row_id", "幂等键",
                 "读回验证", "内容摘要", "执行者", "备注"]


@dataclass
class WriteRequest:
    """一次写入请求。业务代码只管填这个，不碰 adapter。"""
    table: str                          # 中文表名（写入目标）
    row: Mapping[str, Any]              # 中文列名 -> 值
    route: str = "production"           # production / tasks / crm（决定写策略）
    action: str = "append"              # append / update（update 需带 row_id）
    row_id: str = ""                    # update 目标
    idem_key: str = ""                  # 幂等键；空 = 不查重（旧行为）
    actor: str = ""                     # 谁发起（automation / 人名）
    reason: str = ""                    # 业务依据（微信原话/合同号…）
    verify_fields: tuple = ()           # 读回验证的字段（默认验证全部标量列）


@dataclass
class WriteResult:
    """写入结果。status 是唯一判定依据。"""
    status: str                         # written / candidate / skipped_reuse / verify_failed / blocked
    row_id: str = ""
    verified: bool = False
    message: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("written", "candidate", "skipped_reuse")


# ────────────────────────────────────────────────────────────────────
# DataService
# ────────────────────────────────────────────────────────────────────
class DataService:
    """统一写入服务。adapter_factory(base_name) -> adapter，惰性调用。

    用法（apply 模式，crm 路由）：
        ds = DataService(lambda name: get_adapter(load_config(), base_name=name))
        res = ds.write(WriteRequest(table="销售线索表", row={...}, route="crm",
                                    idem_key="crm-lead:...|客户A"))
        assert res.status == "written" and res.verified
    """

    def __init__(self, adapter_factory: Optional[Callable[[str], Any]] = None,
                 data_dir: str = ""):
        self._factory = adapter_factory
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

    # ── 对外主入口 ─────────────────────────────────────
    def write(self, req: WriteRequest, mode: str = C.MODE_PREVIEW) -> WriteResult:
        policy = C.ROUTE_POLICIES.get(req.route)
        if policy is None:
            return WriteResult("blocked", message="未知路由 %r（合法：production/tasks/crm）" % req.route)

        # 候选制路由（production/tasks）：任何模式下都不直接写，产出候选供人工确认
        if policy == C.WRITE_APPROVAL_REQUIRED:
            if mode != C.MODE_APPLY:
                # preview 连候选都可以直接返回（上层渲染用）
                return WriteResult("candidate", message="preview：候选（未写）",
                                   evidence=dict(req.row))
            return WriteResult("candidate", message="候选制路由：等待人工 approve 后由 intake.py 执行",
                               evidence=dict(req.row))

        # crm 路由：preview 不写；apply 才写
        if mode != C.MODE_APPLY:
            return WriteResult("candidate", message="preview：草稿（未写）",
                               evidence=dict(req.row))

        # 幂等检查
        if req.idem_key and self._key_used(req.idem_key):
            return WriteResult("skipped_reuse", message="幂等键已写入过，跳过",
                               evidence={"idem_key": req.idem_key})

        adapter = self._get_adapter(req.route)
        try:
            if req.action == "update":
                if not req.row_id:
                    return WriteResult("blocked", message="update 需要 row_id")
                adapter.update_row(req.table, req.row_id, dict(req.row))
                rid = req.row_id
            else:
                rid = adapter.append_row(req.table, dict(req.row))
                if not rid:
                    return WriteResult("verify_failed", verified=False,
                                       message="写入返回空 row_id")
        except Exception as e:  # 写入异常统一收口，不向上炸
            return WriteResult("verify_failed", verified=False,
                               message="写入异常：%s" % e)

        # 读回验证：中文列名写错时 SeaTable 会 HTTP 200 静默丢列，必须核对
        verified, detail = self.verify_readback(adapter, req, rid)
        self._ledger(req, rid, verified, detail)

        if not verified:
            return WriteResult("verify_failed", row_id=rid, verified=False,
                               message="读回验证失败：%s" % detail,
                               evidence={"row_id": rid})
        return WriteResult("written", row_id=rid, verified=True,
                           message="已写入并读回验证通过", evidence={"row_id": rid})

    # ── 读回验证 ───────────────────────────────────────
    def verify_readback(self, adapter: Any, req: WriteRequest,
                        rid: str) -> tuple[bool, str]:
        """重新拉表核对 row_id 存在且关键字段一致。

        标量字段（str/int/float/bool）逐一比对；list/dict（链接列、多选）
        跳过精确比对只查存在性 —— 云端会归一化这类值，硬比必误报。
        """
        rows = self._safe_list_rows(adapter, req.table)
        if isinstance(rows, str):     # 异常说明
            return False, rows
        target = None
        for r in rows:
            if r.get("__row_id__") == rid:
                target = r
                break
        if target is None:
            return False, "row_id=%s 在表「%s」读回后不存在" % (rid, req.table)

        fields = req.verify_fields or tuple(
            k for k, v in (req.row or {}).items()
            if isinstance(v, (str, int, float, bool)))
        mismatch = []
        for k in fields:
            want = req.row.get(k)
            got = target.get(k)
            # SeaTable 空列常返回 None / ""，等价处理
            if (want or "") != ("" if got is None else str(got) if isinstance(want, str) else got):
                if str(want or "") != str("" if got is None else got):
                    mismatch.append("%s: 期望 %r 实得 %r" % (k, want, got))
        if mismatch:
            return False, "字段不一致（%s）" % "; ".join(mismatch[:3])
        return True, "字段一致（%d 项）" % len(fields)

    # ── 写入原语（供写链复用；不含策略/幂等/台账）──────
    @staticmethod
    def write_verified(adapter: Any, table: str, row: Mapping[str, Any],
                       action: str = "append", row_id: str = "") -> tuple[str, bool, str]:
        """append/update + 读回验证一步完成。返回 (row_id, verified, detail)。

        语义约定：row_id 返回值有意义当且仅当写入请求本身被云端接受
        （append 拿到 rid / update 的目标 rid）；verified=False 说明字段
        静默丢失（HTTP 200 但列没进表），调用方按写入失败处理。
        """
        if action == "update":
            if not row_id:
                return "", False, "update 需要 row_id"
            adapter.update_row(table, row_id, dict(row))
            rid = row_id
        else:
            rid = adapter.append_row(table, dict(row))
            if not rid:
                return "", False, "写入返回空 row_id"
        # 读回验证（就地构造轻量 req，复用比对逻辑）
        ds = DataService.__new__(DataService)   # 不走 __init__（避免无谓 I/O）
        verified, detail = ds.verify_readback(adapter, WriteRequest(
            table=table, row=row, action=action, row_id=rid), rid)
        return rid, verified, detail

    # ── 内部 ───────────────────────────────────────────
    @staticmethod
    def _safe_list_rows(adapter: Any, table: str):
        """list_rows 异常统一收口为字符串说明，不向上炸。"""
        try:
            return adapter.list_rows(table)
        except Exception as e:
            return "读回 list_rows 异常：%s" % e

    def _get_adapter(self, route: str) -> Any:
        if self._factory is None:
            from adapters.factory import load_config, get_adapter
            cfg = load_config()
            a = get_adapter(cfg, base_name=route)
            a.auth()
            return a
        return self._factory(route)

    def _ledger_path(self) -> str:
        return os.path.join(self.data_dir, LEDGER_FILE_NAME)

    def _key_used(self, key: str) -> bool:
        path = self._ledger_path()
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if (r.get("幂等键") or "").strip() == key:
                    return True
        return False

    def _ledger(self, req: WriteRequest, rid: str, verified: bool, detail: str) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        path = self._ledger_path()
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(LEDGER_FIELDS)
            summary = "; ".join("%s=%s" % kv for kv in list(req.row.items())[:3])
            w.writerow([_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        req.route, req.action, req.table, rid, req.idem_key,
                        "通过" if verified else "失败", summary[:120],
                        req.actor, (req.reason or detail)[:80]])
