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

批次 2 的两条同理：

  · `G24`（质量闸）在**每笔库存流水写入前**校验（`add_inventory_txn`）——
    闸门挂「货物移动」而不是挂 `GR`，理由见该方法 docstring。
  · `G25`（库存守恒）是**对账**，不拦截任何写入（`inventory_balance`）——
    它是读数之间的比对，没有「该不该写」的问题。
    把它也做成写入门禁会逼出一堆「为了写进去而凑数」的流水。
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
from . import inventory as INV
from . import procurement as PROC
from . import quality as QC
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

    def inspections(self) -> list[dict]:
        return self.rows(S.TB_INSPECTION)

    def mrb_dispositions(self) -> list[dict]:
        return self.rows(S.TB_MRB)

    def inventory_txns(self) -> list[dict]:
        return self.rows(S.TB_INVENTORY_TXN)

    def inventory_snapshots(self) -> list[dict]:
        return self.rows(S.TB_INVENTORY_SNAPSHOT)

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

    # 结论上要带的时间戳字段（取这些字段里的最大值作为 as_of）
    # ★ 批次 2 补进 `inspected_at` / `decided_at` / `txn_at`：
    #   不补的话，G24/G25 的结论 `as_of` 恒为空串 ——「每步带 as_of」这条
    #   交付判据会在新实体上**静默失守**（不报错，只是永远空着）。
    #   对既有表无影响：它们的行里没有这三个列名。
    _AS_OF_FIELDS = ("created_at", "received_at", "accepted_at", "released_at",
                     "inspected_at", "decided_at", "txn_at")

    @classmethod
    def as_of(cls, *row_groups: Sequence[Mapping[str, Any]]) -> str:
        """结论的 `as_of`：这组行里**最新的那个时间戳**（取不到则空串）。

        `DEV-KICKOFF.md` §7 阶段一的交付判据是「**每步带 ID、带 `as_of`**」——
        `as_of` 是**结论/读取结果**上的字段（G30 上行新鲜度），不是存储行字段，
        所以本层由「读了哪些行」算出它：本层任何一条**判定结论**都带 `as_of`，
        这样「这个结论是按哪一刻的数据算出来的」永远答得上来。
        """
        best = ""
        for rows in row_groups:
            for r in (rows or ()):
                for f in cls._AS_OF_FIELDS:
                    v = str(r.get(f) or "")
                    if v and v > best:
                        best = v
        return best

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
        """全库 BOM 结构校验结论（带 `as_of`）。"""
        versions, lines = self.bom_versions(), self.bom_lines()
        out = BOM.validate_bom(versions, lines)
        out["as_of"] = self.as_of(versions, lines)
        return out

    def explode(self, parent_item_id: str, *, quantity: float = 1.0,
                version: str = "") -> dict:
        versions, lines = self.bom_versions(), self.bom_lines()
        out = BOM.explode(versions, lines, parent_item_id,
                          quantity=quantity, version=version)
        out["as_of"] = self.as_of(versions, lines)
        return out

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
        """这笔付款能不能付的结论（带 `as_of`）。"""
        grs = self.goods_receipts()
        out = PROC.can_pay(payment, grs)
        out["as_of"] = self.as_of(grs)
        return out

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
        verdict = {k: v for k, v in outcome.items() if k != "patch"}
        verdict["as_of"] = self.as_of(self.bom_versions(), self.bom_lines(),
                                      self.goods_receipts())
        return self._carry_verdict(res, "G22", verdict)

    # ════════════════════════════════════════════════════════════════
    # G24：质量闸（检验单 + MRB 处置）
    # ════════════════════════════════════════════════════════════════
    def add_inspection(self, row: dict, *, mode: str = C.MODE_PREVIEW,
                       grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                       reason: str = "", **kw) -> WriteResult:
        """登记一条检验记录（**append-only**：重新检验 = 写新的一条）。

        写入时校验「检验类型」与「结论」在词表内 —— 目的是把 `G24` 判定里
        `R_INSPECTION_TYPE_MISMATCH` / `R_VERDICT_INVALID` 这两条「数据有问题」
        的分支**挡在门口**。★ 但那两个码**依然可达**：它们描述的是**读到**
        坏数据时的行为（历史行、外部导入、绕过本门面的写入），判定侧不能因为
        「写入侧已校验」就假设数据一定干净。

        ★ 刻意**不**要求填 `qty_inspected`：强制填等于宣布「判不了」这个第三态
          不存在 —— 而它恰恰是最需要被看见的状态（见 `quality.R_COVERAGE_UNKNOWN`）。
        """
        t = str(row.get("inspection_type") or "").strip()
        if t not in S.QC_TYPES:
            return WriteResult(R_PRECONDITION,
                               message="检验类型非法：%r（合法：%s）"
                                       % (row.get("inspection_type"),
                                          "|".join(S.QC_TYPES)),
                               evidence={"code": "G24", "field": "inspection_type"})
        v = str(row.get("verdict") or "").strip()
        if v not in S.QC_VERDICTS:
            return WriteResult(R_PRECONDITION,
                               message="检验结论非法：%r（合法：%s）"
                                       % (row.get("verdict"), "|".join(S.QC_VERDICTS)),
                               evidence={"code": "G24", "field": "verdict"})
        return self._write(S.TB_INSPECTION, row, action=S.ACTION_APPEND, mode=mode,
                           grant=grant, actor=actor, reason=reason,
                           row_id=self._key(row, "inspection_id"), **kw)

    def add_mrb(self, row: dict, *, mode: str = C.MODE_PREVIEW,
                grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                reason: str = "", enforce: bool = True, **kw) -> WriteResult:
        """登记一条 MRB 裁定（**append-only**：改判 = 写新的一条裁定）。

        前置：① `decision` 在词表内；② 挂了 `inspection_id` 时，那张检验单
        **必须已经存在**。②不是形式主义 —— `quality._for_inspection` 只按
        `inspection_id` 找裁定，一个写错号的 MRB 是**永远不会被找到**的，
        于是闸门一直报 `R_MRB_MISSING`（「没有裁定」），而人手里明明有一条。
        把这种错**挡在写入那一刻**，比让人对着一个说不通的拒绝信息排查便宜得多。

        `enforce=False` 只给历史数据导入（与 `add_payment` 同款出口）。
        """
        d = str(row.get("decision") or "").strip()
        if d not in S.MRB_DECISIONS:
            return WriteResult(R_PRECONDITION,
                               message="MRB 裁定非法：%r（合法：%s）"
                                       % (row.get("decision"), "|".join(S.MRB_DECISIONS)),
                               evidence={"code": "G24", "field": "decision"})
        iid = str(row.get("inspection_id") or "").strip()
        if enforce and not iid and not str(row.get("item_id") or "").strip():
            return WriteResult(R_PRECONDITION,
                               message="MRB 必须挂到检验单（inspection_id）或至少给物品（item_id）"
                                       "—— 两只都不给就无从判定它裁的是哪批货",
                               evidence={"code": "G24"})
        if enforce and iid:
            row_iid = self.store.get_row(S.TB_INSPECTION, iid)
            if row_iid is None:
                return WriteResult(R_PRECONDITION,
                                   message="MRB 引用的检验单不存在：%s" % iid,
                                   evidence={"code": "G24", "field": "inspection_id"})
        return self._write(S.TB_MRB, row, action=S.ACTION_APPEND, mode=mode,
                           grant=grant, actor=actor, reason=reason,
                           row_id=self._key(row, "mrb_id"), **kw)

    def check_movement(self, item_id: str, direction: str, *,
                       ref_id: str = "") -> dict:
        """按**流水方向**选闸门：`in → IQC`、`out → OQC`。

        ★ 方向非法**直接抛异常**（与 `quality.source_types` 同款理由）：
          静默返回一个「没通过」的结论会把**参数写错**伪装成**货物没检验**，
          是最难查的一类错。写入路径（`add_inventory_txn`）会先自己校验方向
          并返回 `precondition_failed`，所以正常调用不会撞到这个异常。
        """
        insp, mrbs = self.inspections(), self.mrb_dispositions()
        if direction == S.TXN_IN:
            out = QC.check_inbound(item_id, insp, mrbs, ref_id=ref_id)
        elif direction == S.TXN_OUT:
            out = QC.check_outbound(item_id, insp, mrbs, ref_id=ref_id)
        else:
            raise ValueError("未知流水方向：%r（合法：%s）"
                             % (direction, "|".join(S.TXN_DIRECTIONS)))
        out["as_of"] = self.as_of(insp, mrbs)
        return out

    def check_inbound(self, item_id: str, *, ref_id: str = "") -> dict:
        """入库闸门（认 IQC）结论，带 `as_of`。"""
        return self.check_movement(item_id, S.TXN_IN, ref_id=ref_id)

    def check_outbound(self, item_id: str, *, ref_id: str = "") -> dict:
        """出库 / 发货闸门（认 OQC）结论，带 `as_of`。"""
        return self.check_movement(item_id, S.TXN_OUT, ref_id=ref_id)

    # ════════════════════════════════════════════════════════════════
    # G25：库存守恒（库存流水 + 结存快照对账）
    # ════════════════════════════════════════════════════════════════
    def add_inventory_txn(self, row: dict, *, mode: str = C.MODE_PREVIEW,
                          grant: Optional[AUTH.WriteGrant] = None, actor: str = "",
                          reason: str = "", enforce: bool = True,
                          **kw) -> WriteResult:
        """登记一笔库存流水 —— **默认强制 G24 质量闸**（按方向选 IQC / OQC）。

        ## 为什么 `G24` 的落点在这里，而不是 `GR`

        `G24` 说「OQC 不合格 ⟹ **不得入库**、**不得发货**」——
        它约束的是**货物移动**这个动作，所以闸门挂在**流水**上：
        每一笔 `in` 要过入库闸门（IQC），每一笔 `out` 要过出库闸门（OQC）。

        而 `add_goods_receipt`（到货验收）**刻意不加 G24 闸** ——
        到货验收回答的是「东西到了没有 / 收不收」（`G23`），
        质检回答的是「这东西合格不合格」（`G24`）。把两者合并，
        就会让「验收通过」被当成「质量合格」，那正是 `G24` 要堵的洞。

        ## 方向非法：用既有机制拒绝，不自造码

        方向不在 `in/out` 里 → 返回 `precondition_failed` 并说清合法值。
        这里**不新增原因码**：自造一个只有本函数能产出的码，等于给
        `schema.__all__` 塞一条没有判定层对应分支的孤码（死分支）。
        """
        direction = str(row.get("direction") or "").strip()
        if direction not in S.TXN_DIRECTIONS:
            return WriteResult(R_PRECONDITION,
                               message="库存流水方向非法：%r（合法：%s）"
                                       % (row.get("direction"),
                                          "|".join(S.TXN_DIRECTIONS)),
                               evidence={"code": "G24", "field": "direction"})
        verdict = self.check_movement(str(row.get("item_id") or ""), direction,
                                      ref_id=str(row.get("ref_id") or ""))
        if enforce and not verdict["allowed"]:
            return WriteResult(R_PRECONDITION, message=verdict["message"],
                               evidence={"code": "G24", "detail": verdict})
        res = self._write(S.TB_INVENTORY_TXN, row, action=S.ACTION_APPEND,
                          mode=mode, grant=grant, actor=actor, reason=reason,
                          row_id=self._key(row, "txn_id"), **kw)
        return self._carry_verdict(res, "G24", verdict)

    def add_inventory_snapshot(self, row: dict, *, mode: str = C.MODE_PREVIEW,
                               grant: Optional[AUTH.WriteGrant] = None,
                               actor: str = "", reason: str = "",
                               **kw) -> WriteResult:
        """写入一条结存快照（外部来源的镜像，如 PartDB 的 `total_instock`）。

        这里换名归一到 `qty_on_hand`（判据层只认这一套口径，见 `inventory.py`）
        —— 归一化发生在**入口**，不在判定里，否则同一个字段会有两种写法可用。
        """
        payload = dict(row)
        if "qty_on_hand" not in payload and "total_instock" in payload:
            payload["qty_on_hand"] = payload.pop("total_instock")
        return self._write(S.TB_INVENTORY_SNAPSHOT, payload, action=S.ACTION_APPEND,
                           mode=mode, grant=grant, actor=actor, reason=reason,
                           row_id=self._key(payload, "snapshot_id"), **kw)

    @staticmethod
    def _max_ts(rows: Sequence[Mapping[str, Any]], field: str) -> str:
        return max([str(r.get(field) or "") for r in (rows or ())] + [""])

    def inventory_balance(self, *, item_id: str = "") -> dict:
        """`G25` 对账结论：Σ入库 − Σ出库 是否等于结存（带 `as_of`）。

        `as_of` 优先取**对账基准**（快照）的日期；快照没标日期时退回流水时间；
        两处都取不到就**留空** —— 不编一个「现在」冒充数据时刻。
        """
        txns, snaps = self.inventory_txns(), self.inventory_snapshots()
        out = INV.check_balance(txns, snaps, item_id=item_id)
        out["as_of"] = out.get("as_of") or self._max_ts(txns, "txn_at")
        return out


__all__ = ["ExecPlane", "R_PRECONDITION", "R_DUPLICATE", "R_STALE",
           "R_ILLEGAL", "R_NOT_FOUND", "R_REJECTED"]
