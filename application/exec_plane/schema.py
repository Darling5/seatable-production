# -*- coding: utf-8 -*-
"""application/exec_plane/schema.py — 执行平面（`L_exec`）契约常量。

## 这一层是干什么的

`docs/domain-model.md` 把业务分成三层：`L_gov`（治理）/ `L_proj`（项目执行，
即第二期 `project_brain`）/ `L_exec`（实物执行）。**`L_exec` 此前完全空白** ——
「BOM → 采购 → 到货 → 工单 → 质检 → 库存」这条制造主线**没有任何实体承载**，
所以 `G21`/`G22`/`G24`/`G25`/`G28` 这些不变量无处落地（见
`docs/invariant-coverage.md` §D）。

本层就是给它们造实体。

## 落点为什么是本地表，不是 SeaTable

`domain-model.md` §7 写得明确：`L_exec` 建议落点是 **ERP / MES / WMS**，
**「若无，先用 SeaTable 表结构承载」**。当前既没有 ERP，本批也**没有改云表结构的授权**
（业主铁律：未获授权不得写业务库、不得改云表）。所以先按
`project_brain/store.py::LocalMemoryAdapter` 的同款形态落到**本地 append-only JSONL**：
`list_rows / append_row / update_row` 三方法与 SeaTable 适配器同名同义，
将来换成真 ERP 或 SeaTable 表时，上层代码不必改。**这是「先用本地承载」的落地形态，
不是又一套存储。**

## 三条不许自己另立一套的东西

  1. **到货/验收口径** —— 直接 import `project_brain.schema` 的 `CLAIM_*` /
     `ARRIVAL_CLAIMS`，**不在本层重复定义**。同一条业务事实两种口径正是
     `merge-plan.md` E12 那类「双口径漂移」的病根。
  2. **授权闸门** —— 一切落库仍走第一期 `application.authorization`。
     本层不自建第二套审批器。
  3. **统一八字段与 ID 形态** —— 沿用 `PREFIX-YYYYMMDD-XXXX`，
     前缀**一律 3 大写字母**，并由 `tests/test_cross_layer_ids.py` 锁进
     `decision_support.alignment.KNOWN_PREFIXES`（G29）。
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid

from ..project_brain import schema as PBS

CONTRACT_VERSION = "exec-plane-v1"

# ────────────────────────────────────────────────────────────────────
# ID 前缀（规范：3 个大写字母）
#
# ⚠️ 加新 kind 必须同步两处，否则跨层校验会静默拒绝（G29）：
#   ① 本表；② `application/decision_support/alignment.py::KNOWN_PREFIXES`
#   漏了会被 `tests/test_cross_layer_ids.py` 的交叉一致性测试当场抓住。
# ────────────────────────────────────────────────────────────────────
KIND_PREFIX = {
    "item": "ITM",                     # 物料 / 件号
    "bom_version": "BOM",              # BOM 版本
    "bom_line": "BLN",                 # BOM 行
    "purchase_requisition": "PRQ",     # 请购单（PR）
    "goods_receipt": "GRN",            # 到货验收记录（GR）
    "payment": "PAY",                  # 付款单
    "work_order": "WKO",               # 工单（MO / WO）
}

ID_RE = re.compile(r"^(?P<prefix>[A-Z]{3})-(?P<date>\d{8})-(?P<tail>[0-9A-Za-z]{4})$")


def new_id(kind: str, now=None, seq: int = 0) -> str:
    """生成 ``PREFIX-YYYYMMDD-XXXX``。

    形态与 `project_brain.ids.new_id` / `contracts.new_object_id` **完全一致**
    （同一份正则、同一套前缀登记），只是前缀表分属各层 ——
    把 L_exec 的 kind 塞进记忆层那张表会让两层职责混在一起。
    三张表的并集由 `tests/test_cross_layer_ids.py` 强制与
    `decision_support.alignment.KNOWN_PREFIXES` 保持一致（G29）。
    """
    prefix = KIND_PREFIX.get(str(kind).lower())
    if not prefix:
        raise ValueError("未知 L_exec 实体类型：%r（合法：%s）"
                         % (kind, "|".join(sorted(KIND_PREFIX))))
    now = now or _dt.datetime.now()
    tail = "%04d" % int(seq) if seq else uuid.uuid4().hex[:4]
    return "%s-%s-%s" % (prefix, now.strftime("%Y%m%d"), tail)


def validate_id(kind: str, value) -> bool:
    """校验 ``PREFIX-YYYYMMDD-XXXX`` 且前缀匹配该类型。"""
    prefix = KIND_PREFIX.get(str(kind).lower())
    if not prefix or not isinstance(value, str):
        return False
    return bool(re.fullmatch(r"%s-\d{8}-[0-9A-Za-z]{4}" % re.escape(prefix), value))

# ────────────────────────────────────────────────────────────────────
# 本地表（与 project_brain 同款 JSONL 形态；换真 ERP 时只换适配器）
# ────────────────────────────────────────────────────────────────────
TB_ITEM = "物料"
TB_BOM_VERSION = "BOM版本"
TB_BOM_LINE = "BOM行"
TB_PR = "请购单"
TB_GR = "到货验收记录"
TB_PAYMENT = "付款单"
TB_WORK_ORDER = "工单"

TABLE_FILES = {
    TB_ITEM: "items.jsonl",
    TB_BOM_VERSION: "bom_versions.jsonl",
    TB_BOM_LINE: "bom_lines.jsonl",
    TB_PR: "purchase_requisitions.jsonl",
    TB_GR: "goods_receipts.jsonl",
    TB_PAYMENT: "payments.jsonl",
    TB_WORK_ORDER: "work_orders.jsonl",
}
TABLE_NAMES = tuple(TABLE_FILES)

# ────────────────────────────────────────────────────────────────────
# 写入路由与写入动作 —— **直接取二期的取值，不自己造**
#
# ★ 2026-10-03 实测纠正：本模块初版把 `WRITE_ROUTE` 写成 `"local"`、
#   把动作写成 `"item_add"` / `"payment_add"` / `"work_order_release"` 这类业务名。
#   两者都会被**共享注册表**当场拒绝，且是**静默失效**：
#
#     · 路由：`dataservice.DataService.write` 第 1 句就是
#       `if req.route not in C.ROUTE_POLICIES: return WriteResult("blocked", ...)`，
#       而 `contracts.ROUTE_POLICIES` 只有 `production` / `tasks` / `crm` 三条 ——
#       `"local"` 会被判「未知路由」，**每一次写入都 blocked**。
#       （"local" 是**副作用分级**的名字 `SIDE_LOCAL_APPEND`，不是路由名。两者别混。）
#     · 动作：`authorization.WriteGrant.covers` 与 `DataService.write` 都硬校验
#       `action in ("append", "update")` —— 业务动作名会被判「不支持的写入动作」。
#
#   所以这里**取二期的同一个值**而不是重写一遍：同一套授权策略、同一套动作词表。
#   授权粒度是「表 × 动作」（与二期一致），业务语义由**表名 + 行状态**表达，
#   不需要也不允许另造一套动作名。
# ────────────────────────────────────────────────────────────────────
WRITE_ROUTE = PBS.WRITE_ROUTE          # "tasks"：候选制，人工点头才写
ACTION_APPEND = PBS.ACTION_APPEND      # "append"
ACTION_UPDATE = PBS.ACTION_UPDATE      # "update"

# ────────────────────────────────────────────────────────────────────
# 到货 / 验收口径 —— **复用二期**，不重新定义
# ────────────────────────────────────────────────────────────────────
CLAIM_SHIPPED = PBS.CLAIM_SHIPPED
CLAIM_IN_TRANSIT = PBS.CLAIM_IN_TRANSIT
CLAIM_ARRIVED = PBS.CLAIM_ARRIVED
CLAIM_PARTIAL = PBS.CLAIM_PARTIAL
CLAIM_FULL = PBS.CLAIM_FULL
CLAIM_ACCEPTED = PBS.CLAIM_ACCEPTED
CLAIM_REJECTED = PBS.CLAIM_REJECTED
ARRIVAL_CLAIMS = PBS.ARRIVAL_CLAIMS
CLAIM_CN = PBS.CLAIM_CN

# ────────────────────────────────────────────────────────────────────
# 到货验收记录（GR）状态
#
# ⚠️ 下面 GR / PAY / WO 三张**迁移表**的「是否已被强制执行」并不一样，
#    读的人不要一律当成已生效的守卫（本批只做 G21/G22/G23）：
#
#      · `WO_TRANSITIONS`  —— **已强制执行**：`service.release_work_order`
#        在写之前用它拦非法迁移（draft → released）。
#      · `GR_TRANSITIONS`  —— **仅声明**：本批只有 `add_goods_receipt`（=append），
#        没有「改 GR 状态」的写路径，所以没有任何地方会读它。
#      · `PAY_TRANSITIONS` —— **仅声明**：同上（只有 `add_payment`）。
#
#    之所以**留着而不是删掉**：这是设计结论（状态机能去哪、终态在哪），
#    删掉等于把思考结果丢了。为了让「仅声明」不变成一笔糊涂账：
#    `tests/test_exec_plane.py` 会校验三张表**内部自洽**
#    （键集合恒等于状态集、目标必须是已知状态、终态无出边、无自环），
#    并**逐条断言哪些已强制、哪些仅声明** —— 声明与执行的差距写死在测试里，
#    将来接线时那条断言会红，提醒把说明一起改掉。
# ────────────────────────────────────────────────────────────────────
GR_DRAFT = "draft"                 # 已登记，货未到
GR_ARRIVED = "arrived"             # 已到货，待验收
GR_ACCEPTED = "accepted"           # 验收通过
GR_REJECTED = "rejected"           # 验收不通过
GR_CANCELLED = "cancelled"
GR_STATES = (GR_DRAFT, GR_ARRIVED, GR_ACCEPTED, GR_REJECTED, GR_CANCELLED)
GR_STATE_CN = {
    GR_DRAFT: "已登记（货未到）", GR_ARRIVED: "已到货（待验收）",
    GR_ACCEPTED: "验收通过", GR_REJECTED: "验收不通过", GR_CANCELLED: "已取消",
}
GR_TRANSITIONS = {
    GR_DRAFT: {GR_ARRIVED, GR_CANCELLED},
    GR_ARRIVED: {GR_ACCEPTED, GR_REJECTED, GR_CANCELLED},
    GR_ACCEPTED: set(),
    GR_REJECTED: {GR_ARRIVED},     # 拒收后重新到货（换货/补货）
    GR_CANCELLED: set(),
}

# ────────────────────────────────────────────────────────────────────
# 付款单状态
# ────────────────────────────────────────────────────────────────────
PAY_DRAFT = "draft"
PAY_APPROVED = "approved"
PAY_PAID = "paid"
PAY_CANCELLED = "cancelled"
PAY_STATES = (PAY_DRAFT, PAY_APPROVED, PAY_PAID, PAY_CANCELLED)
PAY_STATE_CN = {
    PAY_DRAFT: "草稿", PAY_APPROVED: "已批准", PAY_PAID: "已付款",
    PAY_CANCELLED: "已取消",
}
PAY_TRANSITIONS = {
    PAY_DRAFT: {PAY_APPROVED, PAY_CANCELLED},
    PAY_APPROVED: {PAY_PAID, PAY_CANCELLED},
    PAY_PAID: set(),
    PAY_CANCELLED: set(),
}

# ────────────────────────────────────────────────────────────────────
# 请购单（PR）与工单（WO）状态
# ────────────────────────────────────────────────────────────────────
PR_DRAFT = "draft"
PR_SUBMITTED = "submitted"
PR_APPROVED = "approved"
PR_REJECTED = "rejected"
PR_CONVERTED = "converted"          # 已转成采购订单
PR_CANCELLED = "cancelled"
PR_STATES = (PR_DRAFT, PR_SUBMITTED, PR_APPROVED, PR_REJECTED,
             PR_CONVERTED, PR_CANCELLED)
PR_STATE_CN = {
    PR_DRAFT: "草稿", PR_SUBMITTED: "已提交", PR_APPROVED: "已批准",
    PR_REJECTED: "已驳回", PR_CONVERTED: "已转采购", PR_CANCELLED: "已取消",
}

WO_DRAFT = "draft"
WO_RELEASED = "released"            # 开工（已下达）
WO_IN_PROGRESS = "in_progress"
WO_DONE = "done"
WO_CANCELLED = "cancelled"
WO_STATES = (WO_DRAFT, WO_RELEASED, WO_IN_PROGRESS, WO_DONE, WO_CANCELLED)
WO_STATE_CN = {
    WO_DRAFT: "草稿", WO_RELEASED: "已下达（可开工）", WO_IN_PROGRESS: "生产中",
    WO_DONE: "已完工", WO_CANCELLED: "已取消",
}
WO_TRANSITIONS = {
    WO_DRAFT: {WO_RELEASED, WO_CANCELLED},
    WO_RELEASED: {WO_IN_PROGRESS, WO_CANCELLED},
    WO_IN_PROGRESS: {WO_DONE, WO_CANCELLED},
    WO_DONE: set(),
    WO_CANCELLED: set(),
}

__all__ = ["CONTRACT_VERSION", "KIND_PREFIX", "ID_RE", "new_id", "validate_id",
           "TB_ITEM", "TB_BOM_VERSION", "TB_BOM_LINE", "TB_PR", "TB_GR",
           "TB_PAYMENT", "TB_WORK_ORDER", "TABLE_FILES", "TABLE_NAMES",
           "WRITE_ROUTE", "ACTION_APPEND", "ACTION_UPDATE",
           "CLAIM_SHIPPED", "CLAIM_IN_TRANSIT", "CLAIM_ARRIVED", "CLAIM_PARTIAL",
           "CLAIM_FULL", "CLAIM_ACCEPTED", "CLAIM_REJECTED", "ARRIVAL_CLAIMS",
           "CLAIM_CN",
           "GR_DRAFT", "GR_ARRIVED", "GR_ACCEPTED", "GR_REJECTED", "GR_CANCELLED",
           "GR_STATES", "GR_STATE_CN", "GR_TRANSITIONS",
           "PAY_DRAFT", "PAY_APPROVED", "PAY_PAID", "PAY_CANCELLED",
           "PAY_STATES", "PAY_STATE_CN", "PAY_TRANSITIONS",
           "PR_DRAFT", "PR_SUBMITTED", "PR_APPROVED", "PR_REJECTED",
           "PR_CONVERTED", "PR_CANCELLED", "PR_STATES", "PR_STATE_CN",
           "WO_DRAFT", "WO_RELEASED", "WO_IN_PROGRESS", "WO_DONE", "WO_CANCELLED",
           "WO_STATES", "WO_STATE_CN", "WO_TRANSITIONS"]
