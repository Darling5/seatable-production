# -*- coding: utf-8 -*-
"""application/exec_plane/service.py — 执行平面门面（`L_exec` 的唯一写入口）。

## 授权：不新造一套

与 `project_brain/service.py` 严格同构 —— 本模块**不实现任何授权判定**，
全部委托给：

  · `application.authorization.authorize_write` / `WriteGrant` ——「人同意这次写入吗」
  · `application.dataservice.DataService.write` —— 统一写入路径
    （preview 出候选 / apply 无授权则 blocked / 幂等键去重 / 写完读回验证 / 落台账）

于是「**没授权就什么都没发生**」在这一层同样是**结构上不可能违反**的：
本模块没有接收「置信度」「自动执行」之类参数的入口。

## 为什么复用 `LocalMemoryAdapter`

`project_brain/store.py::LocalMemoryAdapter` 暴露的
`list_rows / append_row / update_row` 与 SeaTable 适配器**同名同义**，
所以第一期的 `DataService` 能原样复用，不必给 L_exec 再写一套写入闸门。
`domain-model.md` §7 说 L_exec 的落点建议是 ERP/MES、「若无，先用 SeaTable 表结构承载」——
当前既无 ERP，本批也没有改云表的授权，故先落本地 JSONL；
将来换成真 ERP 或 SeaTable 表时，只换适配器，本模块与上层代码都不用改。

## 业务校验挂在哪

`G21`（BOM 结构）、`G23`（付款前置）在**写入前**校验，
`G22`（齐套开工）在**工单下达前**校验 —— 校验全部是 `bom.py` /
`procurement.py` / `work_order.py` 里的**纯函数**，本模块只负责
「校验不通过就一个字都不写」。
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Mapping, Optional, Sequence

from .. import authorization as AUTH
from .. import contracts as C
from ..dataservice import DataService, WriteRequest, WriteResult
from ..project_brain import schema as PBS
from ..project_brain.store import StaleVersionError
from . import bom as BOM
from . import procurement as PROC
from . import schema as S
from . import work_order as WO
from .store import ExecTableStore

R_DUPLICATE = "duplicate"
R_STALE = "stale_version"
R_ILLEGAL = "illegal_transition"
R_NOT_FOUND = "not_found"
R_REJECTED = "rejected"
# 本层特有：业务前置不满足 —— 明确与「没授权」区分开
R_PRECONDITION = "precondition_failed"


class ExecPlane:
    """执行平面门面：L_exec 实体的唯一写入口。

    ``root`` 默认 `data/exec_plane`（与 `data/project_brain` 平级，互不干扰）。
    """

    @staticmethod
    def default_root(skill_dir: str = "") -> str:
        """默认落点 `<仓库根>/data/exec_plane`（与 `data/project_brain` 平级，互不干扰）。

        单独抽出来是为了**可被纯计算地断言** ——
        否则「默认根目录对不对」只能靠真的建一个目录去试，
        测试就会在仓库里留下垃圾目录。
        """
        base = skill_dir or os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "data", "exec_plane")

    def __init__(self, root: str = "", *, adapter: Optional[Any] = None,
                 ds: Optional[DataService] = None, skill_dir: str = ""):
        self.root = os.path.abspath(root or self.default_root(skill_dir))
        os.makedirs(self.root, exist_ok=True)
        self.store = adapter or ExecTableStore(self.root)
        self._ds = ds or DataService(adapter_factory=lambda _route: self.store,
                                     data_dir=self.root)

    @property
    def ds(self) -> DataService:
        return self._ds

    # ════════════════════════════════════════════════════════════════
    # 读取
    # ════════════════════════════════════════════════════════════════
    def rows(self, table: str) -> list[dict]:
        return self.store.list_rows(table)

    def items(self) -> list[dict]:
        return self.rows(S.TB_ITEM)

    def bom_versions(self) -> list[dict]:
        return self.rows(S.TB_BOM_VERSION)

    def bom_lines(self) -> list[dict]:
        return self.rows(S.TB_BOM_LINE)

    def goods_receipts(self) -> list[dict]:
        return self.rows(S.TB_GR)

    def payments(self) -> list[dict]:
        return self.rows(S.TB_PAYMENT)

    def work_orders(self) -> list[dict]:
        return self.rows(S.TB_WORK_ORDER)

    # ════════════════════════════════════════════════════════════════
    # 写入闸门（唯一出口；与二期同构）
    # ════════════════════════════════════════════════════════════════
    def _write(self, table: str, row: dict, *, action: str, mode: str,
               grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
               reason: str = "", idem_key: str = "", row_id: str = "",
               verify_fields: Sequence[str] = (),
               expected_version: Optional[int] = None) -> WriteResult:
        """统一写入：①授权闸门 → ②乐观锁 → ③DataService。

        顺序是刻意的 —— **无授权时连版本都不查**，直接 blocked，
        确保「没授权就什么都没发生」。

        ``action`` **只允许** `S.ACTION_APPEND` / `S.ACTION_UPDATE`
        （= 二期的同一套动作词表）：`WriteGrant.covers` 与 `DataService.write`
        都硬校验这两个取值，业务动作名会被判「不支持的写入动作」并静默 blocked。
        业务语义由**表名 + 行状态**表达，授权粒度就是「表 × 动作」，与二期一致。
        """
        if action not in (S.ACTION_APPEND, S.ACTION_UPDATE):
            raise ValueError("非规范写入动作：%r（只能用 %s / %s）"
                             % (action, S.ACTION_APPEND, S.ACTION_UPDATE))
        if mode == C.MODE_APPLY:
            ok, why = AUTH.authorize_write(grant, table, action)
            if not ok:
                return WriteResult("blocked", message=why,
                                   evidence={"table": table, "action": action})
        if mode == C.MODE_APPLY and row_id and expected_version is not None:
            actual = self.store.version_of(table, row_id)
            if actual is not None and int(actual) != int(expected_version):
                return WriteResult(R_STALE, row_id=row_id,
                                   message="版本冲突：%s 期望 v%s，实际 v%s（本次未写入）"
                                           % (row_id, expected_version, actual),
                                   evidence={"table": table, "action": action})
        payload = dict(row)
        if row_id:
            payload[PBS.F_ROW_ID] = row_id
            if expected_version is not None:
                payload[PBS.F_EXPECTED_VERSION] = int(expected_version)
        elif PBS.F_ROW_ID not in payload:
            raise ValueError("写入 %s 必须提供主键（__row_id__ / row_id）" % table)
        req = WriteRequest(table=table, row=payload, route=S.WRITE_ROUTE,
                           action=action, row_id=row_id or "", idem_key=idem_key,
                           actor=actor, reason=reason,
                           verify_fields=tuple(verify_fields), grant=grant)
        return self.ds.write(req, mode=mode)

    @staticmethod
    def _key(row: Mapping[str, Any], *names: str) -> str:
        for n in names:
            v = str(row.get(n) or "").strip()
            if v:
                return v
        raise ValueError("缺少主键字段：%s" % "/".join(names))

    @staticmethod
    def _carry_verdict(res: WriteResult, code: str, verdict: Mapping[str, Any]) -> WriteResult:
        """把业务判定结论挂到结果上，让调用方**看得见**。

        `WriteResult` 只有 `evidence` 这一个扩展位，所以用 **双下划线键**
        （`__verdict__`）—— 与仓库里「`__` 开头即内部字段」的约定一致，
        不会与任何业务字段撞名（`project_brain.schema.is_internal` 同理）。

        ★ 为什么非挂不可：`G23` 的数量覆盖判定有**第三态「判不了」**
          （付款单没填数量 / GR 没填已验收数量 → `checked=False` 且**不阻断**）。
          这个状态如果只活在函数返回值里、不带到写入结果上，
          上层拿到的就是一个干净的成功结果 ——
          **「判不了」被静默折叠成了「通过」，正是最不该发生的那种假指标。**
        """
        ev = dict(res.evidence or {})
        slot = dict(ev.get("__verdict__") or {})
        slot[code] = dict(verdict)
        ev["__verdict__"] = slot
        res.evidence = ev
        return res

    # ════════════════════════════════════════════════════════════════
    # G21：BOM
    # ════════════════════════════════════════════════════════════════
    def add_item(self, row: dict, *, mode: str = C.MODE_PREVIEW,
                 grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                 reason: str = "", **kw) -> WriteResult:
        return self._write(S.TB_ITEM, row, action=S.ACTION_APPEND, mode=mode,
                           grant=grant, actor=actor, reason=reason,
                           row_id=self._key(row, "item_id"), **kw)

    def put_bom(self, version: dict, lines: Sequence[Mapping[str, Any]], *,
                mode: str = C.MODE_PREVIEW,
                grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                reason: str = "") -> WriteResult:
        """写入一版 BOM（版本 + 若干行），**先过 G21 结构校验**。

        校验范围是「已落库的全部 BOM + 本次要写的」——
        只看本次的话，新版本与历史版本之间的环永远查不出来。
        """
        versions = self.bom_versions() + [dict(version)]
        rows = self.bom_lines() + [dict(l) for l in (lines or [])]
        verdict = BOM.validate_bom(versions, rows)
        if not verdict["ok"]:
            return WriteResult(R_PRECONDITION, message=verdict["summary"],
                               evidence={"code": "G21", "issues": verdict["issues"]})
        dup = [str(l.get("line_id") or "") for l in rows
               if str(l.get("line_id") or "")]
        if len(dup) != len(set(dup)):
            return WriteResult(R_PRECONDITION,
                               message="BOM 行 ID 重复 —— 同一行不得重复写入",
                               evidence={"code": "G21"})
        res = self._write(S.TB_BOM_VERSION, version, action=S.ACTION_APPEND,
                          mode=mode, grant=grant, actor=actor, reason=reason,
                          row_id=self._key(version, "bom_id"))
        if not res.ok or mode != C.MODE_APPLY:
            return res
        for ln in (lines or []):
            r = self._write(S.TB_BOM_LINE, dict(ln), action=S.ACTION_APPEND,
                            mode=mode, grant=grant, actor=actor, reason=reason,
                            row_id=self._key(ln, "line_id"))
            if not r.ok:
                return r
        return res

    def validate_bom(self) -> dict:
        return BOM.validate_bom(self.bom_versions(), self.bom_lines())

    def explode(self, parent_item_id: str, *, quantity: float = 1.0,
                version: str = "") -> dict:
        return BOM.explode(self.bom_versions(), self.bom_lines(), parent_item_id,
                           quantity=quantity, version=version)

    # ════════════════════════════════════════════════════════════════
    # G23：到货验收 + 付款
    # ════════════════════════════════════════════════════════════════
    def add_goods_receipt(self, row: dict, *, mode: str = C.MODE_PREVIEW,
                          grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                          reason: str = "", **kw) -> WriteResult:
        return self._write(S.TB_GR, row, action=S.ACTION_APPEND, mode=mode,
                           grant=grant, actor=actor, reason=reason,
                           row_id=self._key(row, "gr_id"), **kw)

    def add_payment(self, row: dict, *, mode: str = C.MODE_PREVIEW,
                    grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                    reason: str = "", enforce: bool = True,
                    **kw) -> WriteResult:
        """写入付款单 —— **默认强制 G23 前置校验**（`enforce=True`）。

        `enforce=False` 只用于历史数据导入；生产路径不得关掉它。
        校验不通过则**一个字都不写**，并返回 `precondition_failed`。

        无论 `enforce` 与否，判定结论都会挂在结果的 `evidence["__verdict__"]["G23"]`
        上 —— 特别是数量覆盖的第三态「判不了」（`amount.checked=False`）：
        **它不阻断写入，但必须被看见**。
        """
        verdict = self.can_pay(row)
        if enforce and not verdict["allowed"]:
            return WriteResult(R_PRECONDITION, message=verdict["message"],
                               evidence={"code": "G23", "detail": verdict})
        res = self._write(S.TB_PAYMENT, row, action=S.ACTION_APPEND, mode=mode,
                          grant=grant, actor=actor, reason=reason,
                          row_id=self._key(row, "payment_id"), **kw)
        return self._carry_verdict(res, "G23", verdict)

    def can_pay(self, payment: Mapping[str, Any]) -> dict:
        return PROC.can_pay(payment, self.goods_receipts())

    # ════════════════════════════════════════════════════════════════
    # G22：工单
    # ════════════════════════════════════════════════════════════════
    def add_work_order(self, row: dict, *, mode: str = C.MODE_PREVIEW,
                       grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                       reason: str = "", **kw) -> WriteResult:
        return self._write(S.TB_WORK_ORDER, row, action=S.ACTION_APPEND, mode=mode,
                           grant=grant, actor=actor, reason=reason,
                           row_id=self._key(row, "wo_id"), **kw)

    def release_work_order(self, wo_id: str, *, mode: str = C.MODE_PREVIEW,
                           grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                           reason: str = "", approval: Optional[Mapping[str, Any]] = None,
                           enforce: bool = True) -> WriteResult:
        """下达工单（开工）。**默认强制 G22 齐套前置校验**。

        校验通过才写（状态 → `released`，缺料时把授权留痕一并写回工单）；
        不通过则一个字都不写。
        """
        current = self.store.get_row(S.TB_WORK_ORDER, wo_id)
        if current is None:
            return WriteResult(R_NOT_FOUND, row_id=wo_id,
                               message="工单不存在：%s" % wo_id)
        cur_state = str(current.get("state") or "")
        if S.WO_RELEASED not in S.WO_TRANSITIONS.get(cur_state, set()):
            return WriteResult(R_ILLEGAL, row_id=wo_id,
                               message="非法状态迁移：%s(%s) → %s"
                                       % (S.WO_STATE_CN.get(cur_state, cur_state),
                                          cur_state, S.WO_STATE_CN[S.WO_RELEASED]))
        outcome = WO.release(current, bom_versions=self.bom_versions(),
                             bom_lines=self.bom_lines(),
                             gr_rows=self.goods_receipts(),
                             approval=approval, actor=actor)
        if enforce and not outcome["allowed"]:
            return WriteResult(R_PRECONDITION, row_id=wo_id,
                               message=outcome["message"],
                               evidence={"code": "G22", "detail": outcome})
        patch = dict(outcome["patch"])
        if not enforce and not outcome["allowed"]:
            # 强行放行（**只给历史数据导入用**）：必须把「为什么被拒」原样留下。
            # 否则 `shortages=[]` 会被读成「物料齐套」—— 而它可能是「BOM 有环，
            # 压根算不出需求」。宁可字段冗余，也不要一个看起来正常的空列表。
            patch = {"state": S.WO_RELEASED, "state_cn": S.WO_STATE_CN[S.WO_RELEASED],
                     "shortages": outcome["shortages"],
                     "satisfied_by_claim": outcome.get("satisfied_by_claim") or [],
                     "release_shortage_override": {},
                     "released_by": actor,
                     "released_at": _dt.datetime.now().isoformat(timespec="seconds"),
                     "release_forced": True,
                     "release_forced_code": outcome["code"],
                     "release_forced_message": outcome["message"]}
        if reason:
            patch["release_reason"] = reason
        res = self._write(S.TB_WORK_ORDER, patch, action=S.ACTION_UPDATE,
                          mode=mode, grant=grant, actor=actor, reason=reason,
                          row_id=wo_id,
                          expected_version=int(current.get(PBS.F_VERSION) or 0))
        # 判定结论（含缺料清单与放行授权三态）一并带出，别只留在局部变量里
        return self._carry_verdict(res, "G22", {
            k: v for k, v in outcome.items() if k != "patch"})


__all__ = ["ExecPlane", "R_PRECONDITION", "R_DUPLICATE", "R_STALE",
           "R_ILLEGAL", "R_NOT_FOUND", "R_REJECTED"]
