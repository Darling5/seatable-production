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
    # ── 批次 2（G24 质量闸 / G25 库存守恒）──
    "inspection": "INS",               # 检验单（IQC / IPQC / OQC）
    "mrb_disposition": "MRB",          # MRB 处置（材料审查委员会裁定）
    "inventory_txn": "IVT",            # 库存流水（入库 / 出库逐笔）
    "inventory_snapshot": "IVS",       # 库存结存快照（外部来源的镜像）
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
# ── 批次 2（G24 / G25）──
TB_INSPECTION = "检验单"
TB_MRB = "MRB处置"
TB_INVENTORY_TXN = "库存流水"
# 结存快照：**外部来源的本地镜像**（当前是 PartDB 的 `total_instock`）。
# ★ 它是 G25 的**对账基准**，不是本层自己算出来的数 —— 若它由本层流水反推，
#   那「Σ入库 − Σ出库 = 结存」就退化成恒等式，什么都验不出来。
#   这正是 `G25` 与「自己跟自己比」的区别，也是这张表必须来自外部的理由。
TB_INVENTORY_SNAPSHOT = "库存结存快照"

TABLE_FILES = {
    TB_ITEM: "items.jsonl",
    TB_BOM_VERSION: "bom_versions.jsonl",
    TB_BOM_LINE: "bom_lines.jsonl",
    TB_PR: "purchase_requisitions.jsonl",
    TB_GR: "goods_receipts.jsonl",
    TB_PAYMENT: "payments.jsonl",
    TB_WORK_ORDER: "work_orders.jsonl",
    TB_INSPECTION: "inspections.jsonl",
    TB_MRB: "mrb_dispositions.jsonl",
    TB_INVENTORY_TXN: "inventory_txns.jsonl",
    TB_INVENTORY_SNAPSHOT: "inventory_snapshots.jsonl",
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

# ────────────────────────────────────────────────────────────────────
# G24 质检词表（批次 2）
#
# `G24`：OQC 不合格 ⟹ 不得入库、不得发货。判据在 `quality.py`（纯函数），
# 本段只定词表。
#
# ⚠️ 「检验类型」与「闸门方向」是**两件事**，不要合并成一个字段：
#      · 类型（IQC/IPQC/OQC）答「这是哪一阶段的检验」；
#      · 方向（inbound/outbound）答「这批货要走哪条路」。
#    `GATE_SOURCE_TYPES` 才表达二者的对应关系。把 IPQC 也算进入库闸门，
#    会让「过程检验合格」被当成「来料合格」—— 那正是 G24 要防的事。
#
# ⚠️ 检验单与 MRB 处置都是 **append-only**（没有状态迁移表）：
#    重新检验 = 写一条新记录，MRB 改判 = 写一条新裁定。这样「当时凭什么放行」
#    永远可回溯 —— 就地改状态会把证据改掉。
# ────────────────────────────────────────────────────────────────────
QC_IQC = "IQC"                     # 来料检验（incoming）
QC_IPQC = "IPQC"                   # 过程检验（in-process）
QC_OQC = "OQC"                     # 出货检验（outgoing）
QC_TYPES = (QC_IQC, QC_IPQC, QC_OQC)
QC_TYPE_CN = {QC_IQC: "来料检验", QC_IPQC: "过程检验", QC_OQC: "出货检验"}

QC_PENDING = "pending"             # 待检
QC_PASS = "pass"                   # 合格
QC_FAIL = "fail"                   # 不合格
QC_CONCESSION = "concession"       # 让步接收（检验环节自己判的）
QC_VERDICTS = (QC_PENDING, QC_PASS, QC_FAIL, QC_CONCESSION)
QC_VERDICT_CN = {QC_PENDING: "待检", QC_PASS: "合格",
                 QC_FAIL: "不合格", QC_CONCESSION: "让步接收"}
# 能直接放行下游的检验结论
QC_RELEASING_VERDICTS = (QC_PASS, QC_CONCESSION)

GATE_INBOUND = "inbound"           # 入库闸门
GATE_OUTBOUND = "outbound"         # 出库 / 发货闸门
GATE_DIRECTIONS = (GATE_INBOUND, GATE_OUTBOUND)
GATE_DIRECTION_CN = {GATE_INBOUND: "入库", GATE_OUTBOUND: "出库/发货"}
# 方向 → 哪种检验才作数（★ 唯一表达这层对应关系的地方）
GATE_SOURCE_TYPES = {
    GATE_INBOUND: (QC_IQC,),
    GATE_OUTBOUND: (QC_OQC,),
}

MRB_RETURN = "return"
MRB_REWORK = "rework"
MRB_SCRAP = "scrap"
MRB_CONCESSION = "concession"
MRB_USE_AS_IS = "use_as_is"
MRB_DECISIONS = (MRB_RETURN, MRB_REWORK, MRB_SCRAP, MRB_CONCESSION, MRB_USE_AS_IS)
MRB_DECISION_CN = {
    MRB_RETURN: "退货", MRB_REWORK: "返工", MRB_SCRAP: "报废",
    MRB_CONCESSION: "让步接收", MRB_USE_AS_IS: "直接使用",
}
# ★ 只有这两条能放行下游：`rework` 必须**返工后重新检验**，
#   拿它当放行依据等于「说好了要返工就直接用」—— 那是 G24 最典型的破口。
MRB_RELEASING = (MRB_CONCESSION, MRB_USE_AS_IS)

# G24 原因码 —— ★ 每一个都必须**真的会被返回**（逐个用例断言），
# 否则就是死分支：「`__all__` 里有一个取不到的码」本身就是假指标。
# 下面 9 个码与 `quality.check_gate` 的分支**一一对应**。
# 注意「检验不合格」这条路**没有**单独的码：它必然落到
# `R_MRB_MISSING` / `R_MRB_INVALID` / `R_MRB_NOT_RELEASING` 三者之一
# （不合格之后的下一步动作才是调用方需要的答案），故不设汇总码。
R_NO_INSPECTION = "no_inspection"
R_INSPECTION_PENDING = "inspection_pending"
R_INSPECTION_TYPE_MISMATCH = "inspection_type_not_applicable"
R_COVERAGE_UNKNOWN = "inspection_coverage_unknown"
R_VERDICT_INVALID = "inspection_verdict_invalid"
R_MRB_MISSING = "mrb_disposition_missing"
R_MRB_INVALID = "mrb_disposition_invalid"
R_MRB_NOT_RELEASING = "mrb_disposition_not_releasing"
R_TARGET_UNKNOWN = "inspection_target_unknown"
G24_REASON_CN = {
    R_NO_INSPECTION: "该方向要求的检验尚未登记",
    R_INSPECTION_PENDING: "检验结论还是「待检」",
    R_INSPECTION_TYPE_MISMATCH: "有检验记录，但检验类型与闸门方向不对应",
    R_COVERAGE_UNKNOWN: "检验单没填检验数量，无法确认覆盖面",
    R_VERDICT_INVALID: "检验结论取值不在词表内",
    R_MRB_MISSING: "检验不合格，但没有 MRB 裁定",
    R_MRB_INVALID: "MRB 裁定取值不在词表内",
    R_MRB_NOT_RELEASING: "MRB 裁定不是放行类（退货/返工/报废）",
    R_TARGET_UNKNOWN: "没给物品，无法定位检验对象",
}

# ────────────────────────────────────────────────────────────────────
# G25 库存流水词表（批次 2）
#
# `G25`：`Σ入库 − Σ出库 = 结存`。判据在 `inventory.py`（纯函数）。
# 「结存」来自**外部快照**（如 PartDB 的 `total_instock`）——
# 没有快照时是**判不了**（第三态），必须显式可见，不得当成「守恒通过」。
# ────────────────────────────────────────────────────────────────────
TXN_IN = "in"                      # 入库
TXN_OUT = "out"                    # 出库
TXN_DIRECTIONS = (TXN_IN, TXN_OUT)
TXN_DIRECTION_CN = {TXN_IN: "入库", TXN_OUT: "出库"}

# ★ 与 G24 同一条纪律：每个码都必须在 `inventory.check_balance` 里
#   **有且只有一条分支能把它取出来**，否则就是死分支（假指标）。
R_NO_SNAPSHOT = "no_inventory_snapshot"
R_UNKNOWN_DIRECTION = "unknown_txn_direction"
# ⚠️ 「数量没填」与「方向不认识」是**两件事**，故分设两码。
#    把缺数量折进 `R_UNKNOWN_DIRECTION` 会让返回的提示**说谎**
#    （明明方向是对的，却说「方向不是 in/out」）—— 说谎的提示比多一个码更糟。
#    二者都属「求和算不全 ⇒ 判不了」，但**下一步要修的东西不同**。
R_QTY_UNKNOWN = "txn_qty_unknown"
R_SNAPSHOT_INCOMPLETE = "snapshot_missing_items"
R_BALANCE_MISMATCH = "balance_mismatch"
G25_REASON_CN = {
    R_NO_SNAPSHOT: "没有任何结存快照，无法对账",
    R_UNKNOWN_DIRECTION: "流水方向不是 in/out，求和算不全",
    R_QTY_UNKNOWN: "流水没填数量，求和算不全",
    R_SNAPSHOT_INCOMPLETE: "部分物料在快照里没有结存，这些物料的守恒判不了",
    R_BALANCE_MISMATCH: "Σ入库 − Σ出库 与结存不一致",
}

__all__ = ["CONTRACT_VERSION", "KIND_PREFIX", "ID_RE", "new_id", "validate_id",
           "TB_ITEM", "TB_BOM_VERSION", "TB_BOM_LINE", "TB_PR", "TB_GR",
           "TB_PAYMENT", "TB_WORK_ORDER", "TB_INSPECTION", "TB_MRB",
           "TB_INVENTORY_TXN", "TB_INVENTORY_SNAPSHOT", "TABLE_FILES", "TABLE_NAMES",
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
           "WO_STATES", "WO_STATE_CN", "WO_TRANSITIONS",
           # 批次 2：G24 质检
           "QC_IQC", "QC_IPQC", "QC_OQC", "QC_TYPES", "QC_TYPE_CN",
           "QC_PENDING", "QC_PASS", "QC_FAIL", "QC_CONCESSION",
           "QC_VERDICTS", "QC_VERDICT_CN", "QC_RELEASING_VERDICTS",
           "GATE_INBOUND", "GATE_OUTBOUND", "GATE_DIRECTIONS",
           "GATE_DIRECTION_CN", "GATE_SOURCE_TYPES",
           "MRB_RETURN", "MRB_REWORK", "MRB_SCRAP", "MRB_CONCESSION",
           "MRB_USE_AS_IS", "MRB_DECISIONS", "MRB_DECISION_CN", "MRB_RELEASING",
           "R_NO_INSPECTION", "R_INSPECTION_PENDING",
           "R_INSPECTION_TYPE_MISMATCH", "R_COVERAGE_UNKNOWN",
           "R_VERDICT_INVALID", "R_MRB_MISSING", "R_MRB_INVALID",
           "R_MRB_NOT_RELEASING", "R_TARGET_UNKNOWN", "G24_REASON_CN",
           # 批次 2：G25 库存守恒
           "TXN_IN", "TXN_OUT", "TXN_DIRECTIONS", "TXN_DIRECTION_CN",
           "R_NO_SNAPSHOT", "R_UNKNOWN_DIRECTION", "R_QTY_UNKNOWN",
           "R_SNAPSHOT_INCOMPLETE", "R_BALANCE_MISMATCH", "G25_REASON_CN"]
