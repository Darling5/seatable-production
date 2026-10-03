# -*- coding: utf-8 -*-
"""application/exec_plane/quality.py — G24 质量闸（**纯函数**，不碰存储）。

## 不变量

`G24`：**OQC 不合格 ⟹ 不得入库、不得发货。**

## 为什么闸门要分方向

「这批货能不能**进**」和「这批货能不能**出**」问的是两个问题，对应两种检验：

| 闸门方向 | 认哪种检验 | 理由 |
|---|---|---|
| `inbound` 入库 | **IQC**（来料检验） | 进门那一刻判来料合不合格 |
| `outbound` 出库/发货 | **OQC**（出货检验） | 出门那一刻判成品合不合格 |

**IPQC（过程检验）两个方向都不作数** —— 它是车间内部的中间控制手段。
把三者混成一句「有检验合格就能动」，会让过程检验的合格结论被拿去当来料合格用，
那正是 `G24` 要堵的洞。对应关系只由 `schema.GATE_SOURCE_TYPES` 一处表达。

## 三态，不是两态

判定结果有三种，**都不许被折叠**：

  · `allowed=True,  checked=True`   —— 放行
  · `allowed=False, checked=True`   —— 明确不允许，且 `code` 告诉调用方**下一步干什么**
  · `checked=False`                 —— **判不了**

第三态折叠成「通过」是本仓库最不该发生的假指标（批次 1 的 `G23` 就栽在这上面）；
折叠成「没依据的拒绝」也不行 —— 那会把「数据没填全」伪装成「货不合格」。

★ 但 `checked=False` **不等于一律拒绝**，要看判不了的是**主判据**还是**附加判据**：

| 判不了的东西 | 能不能确认「不合格」？ | 处置 |
|---|---|---|
| 连物品都没给（`R_TARGET_UNKNOWN`） | 不能，连对象都定位不到 | **拒绝**（fail-closed） |
| 结论取值不在词表内（`R_VERDICT_INVALID`） | 不能，它可能本就是「不合格」 | **拒绝**（fail-closed） |
| 合格但没填抽检数量（`R_COVERAGE_UNKNOWN`） | **能** —— 它明确是「合格」 | 放行 + 标注 |

最后一行是刻意的，理由有两条，都不是「宽松一点比较好」：

  1. **`G24` 只禁止在「不合格」时放行**（「OQC 不合格 ⟹ 不得入库、不得发货」）。
     拿「没填抽检数」去拒绝，实现的是**比不变量更严的规则**。
  2. **更严的规则在这里有个坏下场**：拒了以后，人为了让货动起来最省事的做法
     就是随手填个 `1` —— 于是我们亲手制造了一个假数据。
     而 `1` 与真实的抽检数在判据里**完全等价**（下面第 ⑤ 段的检查只问「有没有」）。
     ★ 而且出货检验本来就是**抽检**：1000 件的批抽 8 件是正常合规的，
       所以抽检数**不能**拿去和被移动的数量比 ——「抽检 5 件放行 1000 件」不是漏洞，
       是标准做法。既然它证明不了覆盖面，就不要拿它当门禁。

  所以这一条的正确形态是**放行 + 把「覆盖面判不了」显式带出去**
  （`checked=False` + `R_COVERAGE_UNKNOWN`，由 `service._carry_verdict` 挂到写入结果上），
  而不是让调用方从一个布尔值里猜。

## 与 `work_order.py` 的关系

同款形态：判定是纯函数、原因码逐个可达、`as_of` 由门面（`service.ExecPlane`）填。
本模块**不 import 存储层**，所以可以脱离任何数据源单测。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import schema as S


# ────────────────────────────────────────────────────────────────────
# 小工具：把「取不到」与「取到 0」分开
# ────────────────────────────────────────────────────────────────────
def _s(v: Any) -> str:
    return str(v if v is not None else "").strip()


def _num(v: Any):
    """转数量；**取不到返回 `None`**（不要退化成 0 —— 二者含义相反）。

    `None` = 「没填」；`0` = 「填了零」（明确的覆盖面为零）。
    二者的**处置相同**（都算覆盖面判不了），但**提示必须说清是哪一种** ——
    「没填」要人去补数，「填了零」要人去查这张单子是不是空壳。
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def source_types(direction: str) -> tuple:
    """闸门方向 → 作数的检验类型。

    ★ 方向非法直接抛异常，**不静默返回空元组**：返回空会让后续逻辑
      一路走到「没有可用检验 → 拒绝」，把「参数写错了」伪装成
      「货物没检验」，是最难查的一类错。
    """
    d = _s(direction)
    if d not in S.GATE_SOURCE_TYPES:
        raise ValueError("未知闸门方向：%r（合法：%s）"
                         % (direction, "|".join(S.GATE_DIRECTIONS)))
    return S.GATE_SOURCE_TYPES[d]


def _sorted(rows: Sequence[Mapping[str, Any]]) -> list:
    """按 (检验时间, 记录 ID) 升序 —— 取「最新那条」时口径稳定。

    只用时间排序会在同一时刻的多条记录上产生**不确定结果**（取决于输入顺序），
    所以加第二键把顺序钉死；这样「同一份数据两次调用结论一致」是有保证的。
    """
    return sorted((dict(r) for r in (rows or ())),
                  key=lambda r: (_s(r.get("inspected_at")),
                                 _s(r.get("inspection_id")),
                                 _s(r.get("__row_id__"))))


def inspections_of(item_id: str, inspections: Sequence[Mapping[str, Any]],
                   *, ref_id: str = "") -> list:
    """该物品的全部检验记录（**不按类型过滤**），按时间升序。

    单独留出「不按类型过滤」这一版，是为了让
    `R_INSPECTION_TYPE_MISMATCH`（有记录但类型不对）能被**明确识别** ——
    如果这里就按类型过滤掉，那种情况会退化成「没有检验记录」，
    调用方会去补一张已有的单子。
    """
    item, ref = _s(item_id), _s(ref_id)
    out = []
    for r in (inspections or ()):
        if _s(r.get("item_id")) != item:
            continue
        # ref 收窄：检验单没挂 ref 时**仍计入**（老数据常见），
        # 挂了 ref 则必须与本次一致 —— 否则等于拿别批货的检验结论放行。
        r_ref = _s(r.get("ref_id"))
        if ref and r_ref and r_ref != ref:
            continue
        out.append(r)
    return _sorted(out)


def _for_inspection(inspection_id: str, item_id: str,
                    mrbs: Sequence[Mapping[str, Any]]) -> list:
    """该检验单的 MRB 裁定（按 `inspection_id` 找；没挂则回落到同物品的裁定）。"""
    iid, item = _s(inspection_id), _s(item_id)
    hit = [m for m in (mrbs or ()) if _s(m.get("inspection_id")) == iid and iid]
    if not hit:
        hit = [m for m in (mrbs or ()) if _s(m.get("item_id")) == item
               and not _s(m.get("inspection_id"))]
    return _sorted(hit)


# ────────────────────────────────────────────────────────────────────
# 结论构造：只有这三个出口，保证字段齐整（不会有半成品结论流出去）
# ────────────────────────────────────────────────────────────────────
def _base(direction: str, item_id: str, ref_id: str) -> dict:
    return {
        "gate": _s(direction),
        "item_id": _s(item_id),
        "ref_id": _s(ref_id),
        "source_types": source_types(direction),
        "allowed": False,
        "checked": True,
        "code": "",
        "message": "",
        "inspection_id": "",
        "inspection_type": "",
        "inspection_verdict": "",
        "mrb_id": "",
        "mrb_decision": "",
        "as_of": "",          # 由门面按「读了哪些行」填
    }


def _block(gate: dict, code: str, *, checked: bool = True, extra: str = "") -> dict:
    """拒绝。`checked=False` 表示**判不了**（fail-closed，同样不放行）。

    状态词（拒绝 / 判不了）由消息**前缀**统一给出，而不是塞进每个原因码的文案里 ——
    否则同一句话会既在码表里说一遍、又在拼消息时重复一遍。
    """
    gate["allowed"] = False
    gate["checked"] = bool(checked)
    gate["code"] = code
    why = S.G24_REASON_CN.get(code, code)
    head = "拒绝" if checked else "判不了（fail-closed，不放行）"
    gate["message"] = "%s%s：%s%s" % (S.GATE_DIRECTION_CN.get(gate["gate"], gate["gate"]),
                                      head, why,
                                      ("（%s）" % extra) if extra else "")
    return gate


def _allow(gate: dict, why: str) -> dict:
    gate["allowed"] = True
    gate["checked"] = True
    gate["code"] = ""
    gate["message"] = "%s放行：%s" % (S.GATE_DIRECTION_CN.get(gate["gate"], gate["gate"]), why)
    return gate


def _allow_unchecked(gate: dict, code: str, why: str) -> dict:
    """放行，但把「这一条判据判不了」原样带出去。

    ★ 唯一用法是 `R_COVERAGE_UNKNOWN`（合格但抽检数没填）—— 它不是主判据，
      主判据「合不合格」已经答了。为什么不拒绝，见模块 docstring 那张表。
      `allowed=True` 与 `checked=False` 同时出现是刻意的组合，**不是漏写**：
      「这个结论不完全可认证」和「这个结论不允许」是两件事。
    """
    gate["allowed"] = True
    gate["checked"] = False
    gate["code"] = code
    gate["message"] = "%s放行（但此项判不了）：%s%s" % (
        S.GATE_DIRECTION_CN.get(gate["gate"], gate["gate"]),
        S.G24_REASON_CN.get(code, code),
        (" —— %s" % why) if why else "")
    return gate


# ────────────────────────────────────────────────────────────────────
# 主判据
# ────────────────────────────────────────────────────────────────────
def check_gate(direction: str, item_id: str,
               inspections: Sequence[Mapping[str, Any]],
               mrbs: Sequence[Mapping[str, Any]] = (), *, ref_id: str = "") -> dict:
    """`G24` 闸门判定：这批货能不能过这道门。

    返回的 `code` 与 `S.G24_REASON_CN` **一一对应**，
    且每个码都有一条测试专门把它取出来（防死分支）。
    """
    gate = _base(direction, item_id, ref_id)
    types = gate["source_types"]

    # ① 判不了：连对象都没给
    if not gate["item_id"]:
        return _block(gate, S.R_TARGET_UNKNOWN, checked=False)

    rows = inspections_of(gate["item_id"], inspections, ref_id=gate["ref_id"])
    # ② 一条检验记录都没有
    if not rows:
        return _block(gate, S.R_NO_INSPECTION)

    # ③ 有记录，但没有一条的类型对本方向作数
    typed = [r for r in rows if _s(r.get("inspection_type")) in types]
    if not typed:
        last = rows[-1]
        gate["inspection_id"] = _s(last.get("inspection_id"))
        gate["inspection_type"] = _s(last.get("inspection_type"))
        return _block(gate, S.R_INSPECTION_TYPE_MISMATCH,
                      extra="只认 %s，现有的是 %s"
                            % ("/".join(types), gate["inspection_type"] or "（空）"))

    latest = typed[-1]
    gate["inspection_id"] = _s(latest.get("inspection_id"))
    gate["inspection_type"] = _s(latest.get("inspection_type"))
    verdict = _s(latest.get("verdict"))
    gate["inspection_verdict"] = verdict

    # ④ 待检
    if verdict == S.QC_PENDING:
        return _block(gate, S.R_INSPECTION_PENDING)

    # ⑤ 结论放行类
    if verdict in S.QC_RELEASING_VERDICTS:
        qty = _num(latest.get("qty_inspected"))
        if qty is None or qty <= 0:
            # ★ 第三态里唯一**放行**的一条：主判据（合不合格）已答「合格」，
            #   判不了的是附加的覆盖面。拒绝它 = 比 G24 更严 + 逼人填假数，
            #   理由见模块 docstring。这里只负责把「判不了」显式带出去。
            return _allow_unchecked(
                gate, S.R_COVERAGE_UNKNOWN,
                "结论=%s，但 qty_inspected=%s"
                % (S.QC_VERDICT_CN.get(verdict, verdict),
                   "（没填）" if qty is None else "填的是 %g" % qty))
        return _allow(gate, "检验%s（%s，抽检 %g）"
                            % (S.QC_VERDICT_CN.get(verdict, verdict),
                               gate["inspection_type"], qty))

    # ⑥ 结论取值不在词表内 —— 数据有问题，不猜
    if verdict != S.QC_FAIL:
        return _block(gate, S.R_VERDICT_INVALID, checked=False,
                      extra="verdict=%r" % verdict)

    # ⑦ 不合格：看 MRB 裁定。三态分明 —— 没有 / 取值非法 / 非放行类
    judged = _for_inspection(gate["inspection_id"], gate["item_id"], mrbs)
    if not judged:
        return _block(gate, S.R_MRB_MISSING)
    mrb = judged[-1]
    gate["mrb_id"] = _s(mrb.get("mrb_id") or mrb.get("__row_id__"))
    decision = _s(mrb.get("decision"))
    gate["mrb_decision"] = decision
    if decision not in S.MRB_DECISIONS:
        return _block(gate, S.R_MRB_INVALID, extra="decision=%r" % decision)
    if decision in S.MRB_RELEASING:
        return _allow(gate, "检验不合格，但 MRB 裁定「%s」放行（裁定号 %s）"
                            % (S.MRB_DECISION_CN.get(decision, decision),
                               gate["mrb_id"] or "（未编号）"))
    return _block(gate, S.R_MRB_NOT_RELEASING,
                  extra="裁定是「%s」" % S.MRB_DECISION_CN.get(decision, decision))


def check_inbound(item_id: str, inspections, mrbs=(), *, ref_id: str = "") -> dict:
    """入库闸门（认 IQC）。"""
    return check_gate(S.GATE_INBOUND, item_id, inspections, mrbs, ref_id=ref_id)


def check_outbound(item_id: str, inspections, mrbs=(), *, ref_id: str = "") -> dict:
    """出库 / 发货闸门（认 OQC）。"""
    return check_gate(S.GATE_OUTBOUND, item_id, inspections, mrbs, ref_id=ref_id)


__all__ = ["source_types", "inspections_of", "check_gate",
           "check_inbound", "check_outbound"]
