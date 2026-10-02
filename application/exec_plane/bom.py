# -*- coding: utf-8 -*-
"""application/exec_plane/bom.py — 物料清单结构约束（`G21`）。

`G21` 命题（`docs/domain-model.md` §5）：

> **BOM 结构：无环、版本内父件唯一、用量守恒、替代料显式受控**

覆盖矩阵里它是**空白**项 —— 全仓 `BOM` 零命中（唯一命中是 `.venv` 里第三方包的
字节序标记，与本议题无关）。本模块把四条性质落成**可执行检查**：

  | # | 性质 | 落点 | 反例（必须报红） |
  |---|---|---|---|
  | ① | **无环** | `find_cycles()`（复用 `decision_support.graph.DependencyGraph` 的成环检测范式） | A→B→C→A；父件把自己当子件 |
  | ② | **版本内父件唯一** | `check_structure()` | 同一 (父件, 版本号) 两条 BOM；同版本同一子件出现两次 |
  | ③ | **用量守恒** | `check_structure()` | 用量 ≤ 0 / 非数字；损耗率不在 `[0, 1)` |
  | ④ | **替代料显式受控** | `check_structure()` | 替代组无主料 / 多个主料 / 跨父件混组 |

**为什么无环要跨版本看**：单个 BOM 版本只有「一个父件 + 若干子件」，本身构不成环；
环是**物料之间**的关系（A 用 B、B 用 A），所以必须把所有版本的行拼成
一张物料级有向图再判 —— 这也正是 `G21` 不能靠「逐版本自检」实现的原因，
不能靠人记得全量扫。

## 复用而不是重写

成环检测**不自己写 DFS**：`application/decision_support/graph.py::DependencyGraph`
已经有 Kahn 拓扑 + DFS 取环路路径 + 自环/重复边识别，且被测过。
这里只做「BOM 行 → 物料级有向边」的翻译层。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..decision_support import schema as DSS
from ..decision_support.graph import DependencyGraph
from . import schema as S

# ────────────────────────────────────────────────────────────────────
# 问题码
# ────────────────────────────────────────────────────────────────────
BOM_CYCLE = "bom_item_cycle"
BOM_SELF_REFERENCE = "bom_self_reference"
BOM_DUP_PARENT_VERSION = "bom_duplicate_parent_version"
BOM_DUP_CHILD = "bom_duplicate_child_in_version"
BOM_QTY_INVALID = "bom_quantity_invalid"
BOM_SCRAP_INVALID = "bom_scrap_rate_invalid"
BOM_SUBSTITUTE_NO_PRIMARY = "bom_substitute_no_primary"
BOM_SUBSTITUTE_MULTI_PRIMARY = "bom_substitute_multiple_primary"
BOM_SUBSTITUTE_CROSS_PARENT = "bom_substitute_cross_parent"
BOM_LINE_UNKNOWN_BOM = "bom_line_unknown_bom_version"
BOM_VERSION_STATUS_INVALID = "bom_version_status_invalid"

ISSUE_CN = {
    BOM_CYCLE: "物料清单存在循环引用（A 用 B、B 又用 A）",
    BOM_SELF_REFERENCE: "父件把自己声明为子件",
    BOM_DUP_PARENT_VERSION: "同一父件在同一版本号下声明了多条 BOM",
    BOM_DUP_CHILD: "同一版本内同一子件被重复声明",
    BOM_QTY_INVALID: "单机用量必须是大于 0 的数字",
    BOM_SCRAP_INVALID: "损耗率必须在 [0, 1) 之间",
    BOM_SUBSTITUTE_NO_PRIMARY: "替代组里没有指定主料",
    BOM_SUBSTITUTE_MULTI_PRIMARY: "替代组里指定了多个主料",
    BOM_SUBSTITUTE_CROSS_PARENT: "同一替代组横跨了不同父件/版本",
    BOM_LINE_UNKNOWN_BOM: "BOM 行指向了不存在的 BOM 版本",
    BOM_VERSION_STATUS_INVALID: "BOM 版本状态非法",
}

# BOM 版本状态
BOM_DRAFT = "draft"
BOM_RELEASED = "released"
BOM_OBSOLETE = "obsolete"
BOM_STATES = (BOM_DRAFT, BOM_RELEASED, BOM_OBSOLETE)
BOM_STATE_CN = {BOM_DRAFT: "草稿", BOM_RELEASED: "已发布", BOM_OBSOLETE: "已作废"}

# 物料类型
ITEM_RAW = "raw"                # 原材料
ITEM_SEMI = "semi"              # 半成品
ITEM_FINISHED = "finished"      # 成品
ITEM_KIND = (ITEM_RAW, ITEM_SEMI, ITEM_FINISHED)
ITEM_KIND_CN = {ITEM_RAW: "原材料", ITEM_SEMI: "半成品", ITEM_FINISHED: "成品"}


def _issue(code: str, ref: str = "", detail: Optional[dict] = None) -> dict:
    return {"code": code, "message": ISSUE_CN.get(code, code), "ref": ref,
            "severity": "error", "detail": detail or {}}


def _now() -> _dt.datetime:
    return _dt.datetime.now()


# ────────────────────────────────────────────────────────────────────
# 实体构造
# ────────────────────────────────────────────────────────────────────
def build_item_row(*, item_no: str, name: str = "", unit: str = "个",
                   item_kind: str = ITEM_RAW, spec: str = "",
                   preferred_supplier: str = "", note: str = "",
                   now: Optional[_dt.datetime] = None, seq: int = 0) -> dict:
    """构造一条物料。``item_no`` 是**唯一料号**（业务键），ID 另生成。"""
    if item_kind not in ITEM_KIND:
        raise ValueError("未知物料类型：%r（合法：%s）"
                         % (item_kind, "|".join(ITEM_KIND)))
    ts = now or _now()
    return {
        "item_id": S.new_id("item", now=ts, seq=seq),
        "item_no": str(item_no or "").strip(),
        "name": name, "spec": spec, "unit": unit,
        "item_kind": item_kind, "item_kind_cn": ITEM_KIND_CN[item_kind],
        "preferred_supplier": preferred_supplier, "note": note,
        "created_at": ts.isoformat(timespec="seconds"),
    }


def build_bom_version_row(*, parent_item_id: str, version: str = "A",
                          status: str = BOM_DRAFT, effective_from: str = "",
                          project_id: str = "", note: str = "",
                          now: Optional[_dt.datetime] = None, seq: int = 0) -> dict:
    """构造一个 BOM 版本（一个父件 + 一个版本号）。"""
    if status not in BOM_STATES:
        raise ValueError("未知 BOM 版本状态：%r（合法：%s）"
                         % (status, "|".join(BOM_STATES)))
    ts = now or _now()
    return {
        "bom_id": S.new_id("bom_version", now=ts, seq=seq),
        "parent_item_id": parent_item_id, "version": str(version or "").strip(),
        "status": status, "status_cn": BOM_STATE_CN[status],
        "effective_from": effective_from, "project_id": project_id, "note": note,
        "created_at": ts.isoformat(timespec="seconds"),
    }


def build_bom_line_row(*, bom_id: str, child_item_id: str, quantity_per: Any,
                       unit: str = "个", scrap_rate: Any = 0,
                       substitute_group: str = "", is_primary: bool = True,
                       position: str = "", note: str = "",
                       now: Optional[_dt.datetime] = None, seq: int = 0) -> dict:
    """构造一条 BOM 行。

    ``substitute_group`` 非空即表示「这一行属于某个替代组」——
    **替代关系必须显式登记**，不得靠料号相近去猜（`G21` 第 ④ 条）。
    """
    ts = now or _now()
    return {
        "line_id": S.new_id("bom_line", now=ts, seq=seq),
        "bom_id": bom_id, "child_item_id": child_item_id,
        "quantity_per": quantity_per, "unit": unit, "scrap_rate": scrap_rate,
        "substitute_group": str(substitute_group or ""),
        "is_primary": bool(is_primary), "position": position, "note": note,
        "created_at": ts.isoformat(timespec="seconds"),
    }


# ────────────────────────────────────────────────────────────────────
# ① 无环
# ────────────────────────────────────────────────────────────────────
def find_cycles(bom_versions: Sequence[Mapping[str, Any]],
                lines: Sequence[Mapping[str, Any]]) -> list[dict]:
    """把所有 BOM 行拼成**物料级**有向图（父件 → 子件），检测环。

    复用 `DependencyGraph`：它已实现 Kahn 拓扑、DFS 取环路、自环与重复边识别，
    本函数只负责「BOM 行 → 边」的翻译与结果格式转换。
    """
    bom_of = {str(b.get("bom_id") or ""): b for b in (bom_versions or [])}
    g = DependencyGraph()
    for ln in (lines or []):
        bom = bom_of.get(str(ln.get("bom_id") or ""))
        if not bom:
            continue
        parent = str(bom.get("parent_item_id") or "")
        child = str(ln.get("child_item_id") or "")
        if not parent or not child:
            continue
        g.add_edge(parent, child, source=str(ln.get("line_id") or ""))

    out: list[dict] = []
    for it in g.validate():
        if it.code == DSS.GAP_CYCLE:
            out.append(_issue(BOM_CYCLE, ref=it.ref,
                              detail={"cycle": (it.detail or {}).get("cycle", []),
                                      "path": it.message}))
        elif it.code == DSS.GAP_SELF_LOOP:
            out.append(_issue(BOM_SELF_REFERENCE, ref=it.ref,
                              detail=dict(it.detail or {})))
    return out


# ────────────────────────────────────────────────────────────────────
# ②③④ 结构检查
# ────────────────────────────────────────────────────────────────────
def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_structure(bom_versions: Sequence[Mapping[str, Any]],
                    lines: Sequence[Mapping[str, Any]]) -> list[dict]:
    """逐条检查 ②版本内父件唯一 ③用量守恒 ④替代料显式受控。

    只做**单版本/单行**能判的事；跨版本的环由 `find_cycles()` 负责。
    """
    issues: list[dict] = []
    versions = list(bom_versions or [])
    rows = list(lines or [])
    bom_of = {str(b.get("bom_id") or ""): b for b in versions}

    # ② (父件, 版本号) 唯一
    seen_pv: dict[tuple, str] = {}
    for bom in versions:
        if str(bom.get("status") or "") not in BOM_STATES:
            issues.append(_issue(BOM_VERSION_STATUS_INVALID,
                                 ref=str(bom.get("bom_id") or "")))
        key = (str(bom.get("parent_item_id") or ""), str(bom.get("version") or ""))
        first = seen_pv.get(key)
        if first is not None:
            issues.append(_issue(BOM_DUP_PARENT_VERSION, ref=str(bom.get("bom_id") or ""),
                                 detail={"parent_item_id": key[0], "version": key[1],
                                         "conflicts_with": first}))
        else:
            seen_pv[key] = str(bom.get("bom_id") or "")

    # ② 同一版本内同一子件重复声明
    seen_child: dict[tuple, str] = {}
    for ln in rows:
        bom_id = str(ln.get("bom_id") or "")
        if bom_id not in bom_of:
            issues.append(_issue(BOM_LINE_UNKNOWN_BOM, ref=str(ln.get("line_id") or ""),
                                 detail={"bom_id": bom_id}))
            continue
        child = str(ln.get("child_item_id") or "")
        key = (bom_id, child)
        first = seen_child.get(key)
        if first is not None:
            issues.append(_issue(BOM_DUP_CHILD, ref=str(ln.get("line_id") or ""),
                                 detail={"child_item_id": child, "bom_id": bom_id,
                                         "conflicts_with": first}))
        else:
            seen_child[key] = str(ln.get("line_id") or "")

    # ③ 用量守恒 / 损耗率区间
    for ln in rows:
        ref = str(ln.get("line_id") or "")
        qty = _num(ln.get("quantity_per"))
        if qty is None or qty <= 0:
            issues.append(_issue(BOM_QTY_INVALID, ref=ref,
                                 detail={"quantity_per": ln.get("quantity_per")}))
        scrap = _num(ln.get("scrap_rate"))
        if scrap is None:
            # 空值视为 0（无损耗），但**非数字**必须报错
            if str(ln.get("scrap_rate") or "").strip() not in ("", "0", "0.0"):
                issues.append(_issue(BOM_SCRAP_INVALID, ref=ref,
                                     detail={"scrap_rate": ln.get("scrap_rate")}))
        elif not (0.0 <= scrap < 1.0):
            issues.append(_issue(BOM_SCRAP_INVALID, ref=ref,
                                 detail={"scrap_rate": ln.get("scrap_rate")}))

    # ④ 替代料显式受控
    groups: dict[str, list[dict]] = {}
    for ln in rows:
        gname = str(ln.get("substitute_group") or "").strip()
        if gname:
            groups.setdefault(gname, []).append(ln)
    for gname, members in sorted(groups.items()):
        primaries = [m for m in members if m.get("is_primary")]
        if not primaries:
            issues.append(_issue(BOM_SUBSTITUTE_NO_PRIMARY, ref=gname,
                                 detail={"group": gname,
                                         "lines": [str(m.get("line_id") or "") for m in members]}))
        elif len(primaries) > 1:
            issues.append(_issue(BOM_SUBSTITUTE_MULTI_PRIMARY, ref=gname,
                                 detail={"group": gname,
                                         "lines": [str(m.get("line_id") or "") for m in primaries]}))
        boms = {str(m.get("bom_id") or "") for m in members}
        if len(boms) > 1:
            issues.append(_issue(BOM_SUBSTITUTE_CROSS_PARENT, ref=gname,
                                 detail={"group": gname, "bom_ids": sorted(boms)}))
    return issues


def validate_bom(bom_versions: Sequence[Mapping[str, Any]],
                 lines: Sequence[Mapping[str, Any]]) -> dict:
    """`G21` 总入口：返回 ``{ok, issues, errors, n_by_code}``。

    ``ok=False`` 表示存在 error 级问题 —— **不得据此发布 BOM / 下推工单**。
    """
    issues = check_structure(bom_versions, lines) + find_cycles(bom_versions, lines)
    errors = [i for i in issues if i.get("severity") == "error"]
    by_code: dict[str, int] = {}
    for i in issues:
        by_code[i["code"]] = by_code.get(i["code"], 0) + 1
    return {"ok": not errors, "issues": issues, "errors": errors,
            "n_by_code": dict(sorted(by_code.items())),
            "summary": ("BOM 结构合规（%d 版本 / %d 行）"
                        % (len(list(bom_versions or [])), len(list(lines or [])))
                        if not errors else
                        "BOM 结构有 %d 处问题：%s"
                        % (len(errors),
                           "、".join("%s×%d" % (k, v) for k, v in sorted(by_code.items()))))}


def explode(bom_versions: Sequence[Mapping[str, Any]],
            lines: Sequence[Mapping[str, Any]], parent_item_id: str,
            *, quantity: float = 1.0, version: str = "",
            max_depth: int = 32) -> dict:
    """按 BOM 展开某父件的用量（单层/多层递归）。

    遇到环**立即停止并如实报错**，绝不进入无限递归，**也绝不返回一个看似完整的
    展开表**：有环时 ``requirements`` 恒为 ``{}``，算到一半的结果只放在
    ``partial_requirements`` 里并明确标注「不可用」。

    这条是刻意的 —— 有环时算出来的用量是**错的**（重复累加环上的节点），
    如果把它放在 `requirements` 里返回，调用方一个 `exp["requirements"]` 就拿到了
    污染数据，而 `ok=False` 很容易被忽略。宁可让正确的调用方多写一个键名。
    """
    versions = list(bom_versions or [])
    by_parent: dict[str, list[dict]] = {}
    for bom in versions:
        if version and str(bom.get("version") or "") != version:
            continue
        if str(bom.get("status") or "") == BOM_OBSOLETE:
            continue
        by_parent.setdefault(str(bom.get("parent_item_id") or ""), []).append(bom)
    rows: dict[str, list[dict]] = {}
    for ln in (lines or []):
        rows.setdefault(str(ln.get("bom_id") or ""), []).append(dict(ln))

    need: dict[str, float] = {}
    cycles: list[str] = []

    def walk(item: str, qty: float, depth: int, path: list[str]) -> None:
        if depth > max_depth:
            cycles.append(" → ".join(path + [item]) + "（超过最大深度）")
            return
        if item in path:
            cycles.append(" → ".join(path[path.index(item):] + [item]))
            return
        for bom in by_parent.get(item, []):
            for ln in rows.get(str(bom.get("bom_id") or ""), []):
                child = str(ln.get("child_item_id") or "")
                per = _num(ln.get("quantity_per")) or 0.0
                scrap = _num(ln.get("scrap_rate")) or 0.0
                need[child] = need.get(child, 0.0) + qty * per * (1.0 + scrap)
                walk(child, qty * per * (1.0 + scrap), depth + 1, path + [item])

    walk(str(parent_item_id or ""), float(quantity), 0, [])
    polluted = bool(cycles)
    return {"parent_item_id": parent_item_id, "quantity": quantity,
            "version": version,
            # 有环 ⇒ requirements 恒空（不是「部分可用」，是「不可用」）
            "requirements": {} if polluted else dict(sorted(need.items())),
            "partial_requirements": dict(sorted(need.items())) if polluted else {},
            "cycles": cycles,
            "ok": not polluted,
            "message": ("展开完成，共 %d 种物料" % len(need) if not polluted
                        else "展开中止：发现循环引用 %s（算到一半的用量不可用）"
                             % "；".join(cycles[:3]))}


__all__ = [
    "BOM_CYCLE", "BOM_SELF_REFERENCE", "BOM_DUP_PARENT_VERSION", "BOM_DUP_CHILD",
    "BOM_QTY_INVALID", "BOM_SCRAP_INVALID", "BOM_SUBSTITUTE_NO_PRIMARY",
    "BOM_SUBSTITUTE_MULTI_PRIMARY", "BOM_SUBSTITUTE_CROSS_PARENT",
    "BOM_LINE_UNKNOWN_BOM", "BOM_VERSION_STATUS_INVALID", "ISSUE_CN",
    "BOM_DRAFT", "BOM_RELEASED", "BOM_OBSOLETE", "BOM_STATES", "BOM_STATE_CN",
    "ITEM_RAW", "ITEM_SEMI", "ITEM_FINISHED", "ITEM_KIND", "ITEM_KIND_CN",
    "build_item_row", "build_bom_version_row", "build_bom_line_row",
    "find_cycles", "check_structure", "validate_bom", "explode",
]
