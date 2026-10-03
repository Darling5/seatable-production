# -*- coding: utf-8 -*-
"""application/exec_plane — 执行平面（`L_exec`）：BOM → 采购 → 到货验收 → 付款 → 工单。

## 为什么要有这一层

`docs/domain-model.md` 把业务分成三层，而 `L_exec` **此前完全空白**：
「BOM → 采购 → 到货 → 工单 → 质检 → 库存」这条制造主线**没有任何实体承载**，
所以 `G21`（BOM 结构）/ `G22`（齐套开工）/ `G23`（付款前置）/ `G24` / `G25` / `G28`
这些不变量**无处落地**（`docs/invariant-coverage.md` §D）。

结论写得很直白：**「阻碍两者的不是能力，而是实体。」**
本包就是给它们造实体。

## 公开入口

    from application.exec_plane import ExecPlane

    ep = ExecPlane()                                  # 默认落 <repo>/data/exec_plane
    ep.add_payment(row, mode="apply", grant=g)        # G23 前置不过 ⇒ 一个字都不写
    ep.release_work_order(wo_id, mode="apply", grant=g)

## 模块划分

    schema       契约常量（ID 前缀 / 表名映射 / 状态机 / 口径别名）
    store        本地表适配器（复用二期 JSONL 形态，只换「表名 → 文件名」）
    bom          `G21` BOM 结构：无环 / 版本内父件唯一 / 用量守恒 / 替代料显式受控
    procurement  `G23` 采购执行链：PR → PO → GR → 付款（付款 ⟹ 有到货验收记录）
    work_order   `G22` 齐套开工：开工 ⟹ 齐套 = 100% ∨ 显式缺料放行授权
    quality      `G24` 质量闸：OQC 不合格 ⟹ 不得入库、不得发货（分方向：in→IQC / out→OQC）
    inventory    `G25` 库存守恒：Σ入库 − Σ出库 = 结存（与外部快照对账）
    service      门面：唯一写入口，复用第一期授权闸门 + DataService

## 三条不许自己另立一套的东西（违反就是双口径漂移的起点）

  1. **到货 / 验收口径** —— 直接复用 `project_brain.schema` 的 `CLAIM_*` 与
     `project_brain.evidence_ledger` 的 `is_arrival_evidence` / `is_full_arrival`。
     「已发货 ≠ 已到货」「部分到货不算齐套」这两条不在本层重新定义。
  2. **授权闸门 / 写入路由 / 写入动作** —— 全部取第一、二期的**同一个值**：
     路由只从 `contracts.ROUTE_POLICIES` 里选，动作只用 `append` / `update`。
     ★ 自造取值会被共享注册表**静默拒绝**（见 `schema.WRITE_ROUTE` 处的实测记录）。
  3. **ID 形态** —— 一律 `PREFIX-YYYYMMDD-XXXX`，前缀 **3 个大写字母**，
     并与 `contracts._ID_PREFIX` / `project_brain.ids.KIND_PREFIX` 的并集保持
     恒等于 `decision_support.alignment.KNOWN_PREFIXES`（`G29`，
     由 `tests/test_cross_layer_ids.py` 强制）。
"""
from __future__ import annotations

from . import bom, inventory, procurement, quality, schema, work_order
from .service import (ExecPlane, R_DUPLICATE, R_ILLEGAL, R_NOT_FOUND,
                      R_PRECONDITION, R_REJECTED, R_STALE)
from .store import ExecTableStore

__all__ = [
    "ExecPlane", "ExecTableStore",
    "R_PRECONDITION", "R_DUPLICATE", "R_STALE", "R_ILLEGAL", "R_NOT_FOUND",
    "R_REJECTED",
    "bom", "inventory", "procurement", "quality", "schema", "work_order",
]
