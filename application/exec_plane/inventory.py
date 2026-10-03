# -*- coding: utf-8 -*-
"""application/exec_plane/inventory.py — G25 库存守恒（**纯函数**，不碰存储）。

## 不变量

`G25`：**Σ入库 − Σ出库 = 结存。**

## 这个不变量为什么值得单独一层

流水是「过程」，结存是「结果」。二者由**不同来源**产生：
流水来自本层的逐笔登记，结存来自**外部快照**（如 PartDB 的 `total_instock`）。
所以 `G25` 不是一个算式，而是**一次对账**：

  · 只算流水内部自洽（`Σin − Σout` 与自己比）是**废话** —— 恒等式；
  · 真正有信息量的是**与外部快照比**，差异才说明「有一边漏了」。

因此本模块的输入天然是两份数据，**没有快照就什么都判不了**（见下）。

## 三态，不是两态

沿用批次 1 的 `G23`/`G24` 范式：

  · `balanced=True,  checked=True`   —— 守恒
  · `balanced=False, checked=True`   —— 明确**不**守恒（账实不符，有确凿差异）
  · `balanced=False, checked=False`  —— **判不了**（fail-closed：不认证守恒，但必须看得见）

★ 「判不了」**绝不是**「不守恒」的同义词，二者要修的东西完全不同；
  它也**绝不是**「通过」—— 那是最该避免的假指标。

## 原因码怎么划分（比 G24 更明确的一条规则）

**一个码对应一个「下一步动作」，而不是对应一个 `if` 分支。**

  · 方向不认识 → 下一步「去把方向字段改对」 → 单独一码 `R_UNKNOWN_DIRECTION`
  · 数量没填   → 下一步「去把数量补上」     → 单独一码 `R_QTY_UNKNOWN`
    二者同在「求和算不全」这一族，但**要修的不是同一个字段**，所以拆开。
    ⚠️ 把「缺数量」折进 `R_UNKNOWN_DIRECTION` 会让提示**说谎**
       （方向明明是对的，却说「方向不是 in/out」）—— 说谎的提示比多一个码更糟。
  · 快照缺行 / 快照行有但结存为空 → 下一步都是「去把结存补上」 → **共用**一码
    `R_SNAPSHOT_INCOMPLETE`（此处刻意不拆，正因为下一步动作相同）。

## 列名口径：归一化在适配器，本层只有一个口径

本模块只认一套规范列名，**不接受别名**：

| 数据 | 规范列 |
|---|---|
| 流水 | `item_id` / `direction` / `qty` / `txn_id` / `txn_at` |
| 快照 | `item_id` / `qty_on_hand` / `as_of` |

PartDB 那边叫 `total_instock`，**由 `adapters/partdb.py` 映射成 `qty_on_hand`**。
若这里也认 `total_instock`，同一个字段就有了两个能用的写法 ——
那正是 `G30`「防双口径」要拦的东西（一旦两处口径漂移，谁也说不出哪个对）。

## 与 `quality.py` 的关系

同款形态：判定是纯函数、原因码逐个可达、`as_of` 由门面填。本模块**不 import 存储层**。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import schema as S

# 浮点余量。★ 它**不是**「允许的差异额度」：数量是整数件，
# 这里只是吸收 `0.1+0.2` 这类二进制浮点误差，量级取 1e-6 已经极宽。
TOL = 1e-6


# ────────────────────────────────────────────────────────────────────
# 小工具：把「取不到」与「取到 0」分开（与 `quality.py` 同款语义）
# ────────────────────────────────────────────────────────────────────
def _s(v: Any) -> str:
    return str(v if v is not None else "").strip()


def _num(v: Any):
    """转数量；**取不到返回 `None`**（不要退化成 0 —— 二者含义相反）。

    `None` = 「没填」（判不了）；`0` = 「填了零」（明确的一笔零数量）。
    `R_QTY_UNKNOWN` 与「快照结存为空」两条判不了的分支，全靠这个区分成立。
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sorted(rows: Sequence[Mapping[str, Any]], *keys: str) -> list:
    """按给定键（末尾自动补 `__row_id__`）升序 —— 口径稳定、可重跑。

    只用一列排序会在该列取值相同的多行上产生**不确定结果**（取决于输入顺序），
    于是「同一份数据两次调用结论不一致」。把顺序钉死是结论可复现的前提。
    """
    ks = tuple(keys) + ("__row_id__",)
    return sorted((dict(r) for r in (rows or ())),
                  key=lambda r: tuple(_s(r.get(k)) for k in ks))


# ────────────────────────────────────────────────────────────────────
# 流水汇总
# ────────────────────────────────────────────────────────────────────
def aggregate_txns(txns: Sequence[Mapping[str, Any]], *, item_id: str = "") -> dict:
    """把流水按物料汇总。

    返回 `{"by_item": {...}, "unknown_direction": [...], "missing_qty": [...], "count": n}`。

    ★ `unknown_direction` / `missing_qty` 返回**原始行**而非计数：
      报「有 2 笔数量没填」没法修，报出是哪两笔才能修。
      两处都保留完整行（含 `__row_id__`），调用方可直接回写。
    """
    want = _s(item_id)
    rows = _sorted((txns or ()), "txn_at", "txn_id")
    if want:
        rows = [r for r in rows if _s(r.get("item_id")) == want]

    by_item: dict = {}
    unknown, missing = [], []
    for r in rows:
        item = _s(r.get("item_id"))
        d = _s(r.get("direction"))
        if d not in S.TXN_DIRECTIONS:
            unknown.append(r)
            continue
        q = _num(r.get("qty"))
        if q is None:
            missing.append(r)
            continue
        # 没有物品号的流水汇不到任何物料上 —— 也算「求和算不全」，
        # 归入 missing_qty 一族（下一步同样是「把这一笔补对」）。
        if not item:
            missing.append(r)
            continue
        slot = by_item.setdefault(item, {"item_id": item, S.TXN_IN: 0.0,
                                         S.TXN_OUT: 0.0, "net": 0.0, "count": 0})
        slot[d] += q
        slot["count"] += 1

    for slot in by_item.values():
        slot["net"] = slot[S.TXN_IN] - slot[S.TXN_OUT]

    return {"by_item": by_item, "unknown_direction": unknown,
            "missing_qty": missing, "count": len(rows)}


def _snapshot_map(snapshots: Sequence[Mapping[str, Any]], *, item_id: str = "") -> dict:
    """结存快照 → `{item_id: qty}`；无法使用的行单列。

    返回 `{"qty": {...}, "unreadable": [...], "unkeyed": [...], "rows": n, "as_of": s}`。

    · `unreadable` —— 有物品号但结存为空 / 非数（下一步：补结存）
    · `unkeyed`    —— 连物品号都没有，匹配不到任何物料（无法作为对账基准）
    · `as_of`      —— 各行 `as_of` 的**最大值**，作为本次对账的时间戳
    """
    want = _s(item_id)
    rows = _sorted((snapshots or ()), "as_of", "item_id")
    if want:
        rows = [r for r in rows if _s(r.get("item_id")) == want]

    qty, unreadable, unkeyed, as_of = {}, [], [], ""
    for r in rows:
        item = _s(r.get("item_id"))
        a = _s(r.get("as_of"))
        if a > as_of:
            as_of = a
        if not item:
            unkeyed.append(r)
            continue
        q = _num(r.get("qty_on_hand"))
        if q is None:
            unreadable.append(r)
            continue
        # 同一物料多条快照：取 `as_of` 最新的那条（`_sorted` 已定序，末条即最新）
        qty[item] = q
    return {"qty": qty, "unreadable": unreadable, "unkeyed": unkeyed,
            "rows": len(rows), "as_of": as_of}


# ────────────────────────────────────────────────────────────────────
# 结论构造
# ────────────────────────────────────────────────────────────────────
def _conclude(out: dict, code: str, *, checked: bool, extra: str = "") -> dict:
    """填结论。`checked=False` 一律表示**判不了**（fail-closed，不认证守恒）。

    状态词由消息**前缀**统一给出（与 `quality._block` 同款）——
    不塞进原因码文案里，免得同一句话在两个地方各说一遍。
    """
    out["code"] = code
    out["checked"] = bool(checked)
    out["balanced"] = False
    why = S.G25_REASON_CN.get(code, code)
    head = "不守恒" if checked else "判不了（fail-closed，不认证守恒）"
    out["message"] = "%s：%s%s" % (head, why, ("（%s）" % extra) if extra else "")
    return out


def _pass(out: dict, extra: str = "") -> dict:
    out["code"] = ""
    out["checked"] = True
    out["balanced"] = True
    out["message"] = "库存守恒：Σ入库 − Σ出库 与结存一致%s" % (("（%s）" % extra) if extra else "")
    return out


def check_balance(txns: Sequence[Mapping[str, Any]],
                  snapshots: Sequence[Mapping[str, Any]],
                  *, item_id: str = "", as_of: str = "") -> dict:
    """`G25` 对账：流水推出的结存，是否等于快照里的结存。

    ## 结论优先级（★ 与代码分支顺序一一对应，改一处必须改另一处）

    1. **整份快照不可用** → `R_NO_SNAPSHOT`（判不了）：连对账基准都没有。
    2. **求和算不全** → `R_UNKNOWN_DIRECTION` / `R_QTY_UNKNOWN`（判不了）：
       连左边的 `Σ` 都算不出来，谈不上比对。
    3. **有确凿差异** → `R_BALANCE_MISMATCH`（`checked=True`）：差异就是差异，
       发现了就必须报，不该被「还有些项判不了」挡住。
    4. **无差异但判不全** → `R_SNAPSHOT_INCOMPLETE`（`checked=False`）：
       不能认证守恒。
    5. 全部对上 → 守恒。

    ★ 3 排在 4 前面是刻意的（不是「先报判不了」）：一条**确凿的**账实不符
      比一堆**判不了**更该被看见。
    ★ 无论落在哪一档，`extra` 都**同时**报出另一档的项数 ——
      否则「A 项不符 + B 项判不了」会只显示前者，后者被静默吞掉。
    ★ `balanced=True` 只在**每一项都有快照、每一项都对上**时返回，
      所以 fail-closed 是成立的：判不了时绝不会返回守恒。

    ## 快照里有、流水里一笔没有的物料

    报 `R_BALANCE_MISMATCH`（期望 0、实际 50 ⇒ 差 50），**不是**判不了：
    「结存 50 但流水一笔没有」不是信息不足，是**账实不符**，判得明明白白。
    """
    out = {
        "item_id": _s(item_id),
        "balanced": False,
        "checked": True,
        "code": "",
        "message": "",
        "items": [],          # 逐物料明细（含未参与总体结论的项，便于人看）
        "as_of": _s(as_of),
    }

    agg = aggregate_txns(txns, item_id=item_id)
    # ★ 快照要算**两份**，这不是冗余：
    #   · `snap_all` 回答「整份快照能不能用」—— 它为空才是 `R_NO_SNAPSHOT`；
    #   · `snap`（按 item_id 过滤）回答「这个物料有没有结存」——
    #     它为空只说明**这个物料**没有结存，属 `R_SNAPSHOT_INCOMPLETE`。
    #   只算过滤后的那一份，会把「问某个物料时它恰好没快照」误报成
    #   「一个快照都没有」，于是调用方去重建整份快照，而该做的只是补这一行。
    snap_all = _snapshot_map(snapshots)
    snap = _snapshot_map(snapshots, item_id=item_id) if _s(item_id) else snap_all

    # ① 整份快照都不可用 —— 没有对账基准，**判不了**
    if not snap_all["qty"]:
        why = []
        if snap_all["rows"]:
            why.append("快照 %d 行，但%s"
                       % (snap_all["rows"],
                          "都没有物品号" if snap_all["unkeyed"] else "结存全为空"))
        return _conclude(out, S.R_NO_SNAPSHOT, checked=False,
                         extra="；".join(why) or "未读到任何快照行")

    # ② 求和算不全之一：方向不认识
    if agg["unknown_direction"]:
        return _conclude(out, S.R_UNKNOWN_DIRECTION, checked=False,
                         extra="%d 笔，合法值 %s；例：%s"
                               % (len(agg["unknown_direction"]),
                                  "|".join(S.TXN_DIRECTIONS),
                                  _brief(agg["unknown_direction"][0])))

    # ③ 求和算不全之二：数量没填
    if agg["missing_qty"]:
        return _conclude(out, S.R_QTY_UNKNOWN, checked=False,
                         extra="%d 笔；例：%s"
                               % (len(agg["missing_qty"]),
                                  _brief(agg["missing_qty"][0])))

    # ④ 逐物料对账
    items, no_snap, mismatch = [], [], []
    for item in sorted(set(agg["by_item"]) | set(snap["qty"])):
        slot = agg["by_item"].get(item, {"item_id": item, S.TXN_IN: 0.0,
                                         S.TXN_OUT: 0.0, "net": 0.0, "count": 0})
        if item not in snap["qty"]:
            # 快照里查不到这个物料 —— 它的守恒判不了
            items.append({"item_id": item, "in": slot[S.TXN_IN], "out": slot[S.TXN_OUT],
                          "expected": slot["net"], "on_hand": None, "diff": None,
                          "status": "no_snapshot", "code": S.R_SNAPSHOT_INCOMPLETE})
            no_snap.append(item)
            continue
        on_hand = snap["qty"][item]
        diff = slot["net"] - on_hand
        if abs(diff) > TOL:
            items.append({"item_id": item, "in": slot[S.TXN_IN], "out": slot[S.TXN_OUT],
                          "expected": slot["net"], "on_hand": on_hand, "diff": diff,
                          "status": "mismatch", "code": S.R_BALANCE_MISMATCH})
            mismatch.append(item)
        else:
            items.append({"item_id": item, "in": slot[S.TXN_IN], "out": slot[S.TXN_OUT],
                          "expected": slot["net"], "on_hand": on_hand, "diff": 0.0,
                          "status": "balanced", "code": ""})
    out["items"] = items
    if not out["as_of"]:
        # 对账基准的日期优先；本次问的那个物料没有快照行时，退回整份快照的日期
        out["as_of"] = snap["as_of"] or snap_all["as_of"]

    # ⑤ 快照行有、结存读不出来的物料（下一步同样是「补结存」，故与 ④ 共用一码）
    if snap["unreadable"]:
        no_snap.extend(_s(r.get("item_id")) or "（无物品号）" for r in snap["unreadable"])

    # ⑥ 优先级：确凿差异 > 判不了 > 守恒（理由见 docstring）
    tail = ""
    if no_snap:
        tail = "另有 %d 项判不了（快照缺结存）：%s" % (len(no_snap), "、".join(no_snap[:3]))
    if mismatch:
        return _conclude(out, S.R_BALANCE_MISMATCH, checked=True,
                         extra="对不上 %d 项：%s%s"
                               % (len(mismatch), "、".join(mismatch[:3]),
                                  ("；" + tail) if tail else ""))
    if no_snap:
        return _conclude(out, S.R_SNAPSHOT_INCOMPLETE, checked=False, extra=tail)
    return _pass(out, "（%d 项，快照 %s）" % (len(items), out["as_of"] or "未标日期"))


def _brief(row: Mapping[str, Any]) -> str:
    """异常行的简短定位串（够人去把这一笔找出来就行）。"""
    return "%s/%s dir=%r qty=%r" % (_s(row.get("txn_id")) or _s(row.get("__row_id__")) or "?",
                                    _s(row.get("item_id")) or "（无物品号）",
                                    row.get("direction"), row.get("qty"))


__all__ = ["TOL", "aggregate_txns", "check_balance"]
