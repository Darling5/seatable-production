# -*- coding: utf-8 -*-
"""
生产域静态结构定义（本地模式 / 初始化用）。

这部分把 SKILL.md 里的「表级规则」结构化成数据，供 local 适配器在零配置时
也能正确建表、套默认值、建立双向关联。SeaTable 模式下的真实结构来自 Base 的
metadata，这里的定义是本地模式的“兜底真相”。

注意：SELECT 选项（如 IC 供应商 14 选项）的「显示中文标签」规则仍由 SKILL.md
约束，模型负责把原始值翻译成中文标签；本地模式里存的就是文本，无需翻译。
"""
from .base import Unsupported

# ══════════════════════════════════════════════════════════════════
# 中立列类型词表
#
# 上层（workflows / domain / 建表调用方）描述表结构时**只用这套词**，
# 不认识任何后端私有类型名 —— SeaTable 的 "single-select"、飞书的数字类型码、
# 简道云的 widget 类型、金蝶的元数据标识，都不该出现在上层代码里。
# 由后端自己翻译成原生类型（见 backend_type）。
# ══════════════════════════════════════════════════════════════════
NEUTRAL_TYPES = (
    "text",         # 短文本
    "longtext",     # 长文本
    "number",       # 数值
    "date",         # 日期（不含时间）
    "datetime",     # 日期 + 时间
    "bool",         # 勾选
    "select",       # 单选
    "multiselect",  # 多选
    "attachment",   # 附件
)

#: 中立类型 → 各后端原生类型名。
#:   · local：CSV 一切都存成文本，这是如实声明而非偷懒。
#:   · seatable：取自只读实测的列 type 取值（GET /metadata/ 的 columns[].type）。
BACKEND_TYPES = {
    "local": {t: "text" for t in NEUTRAL_TYPES},
    "seatable": {
        "text": "text",
        "longtext": "long-text",
        "number": "number",
        "date": "date",
        "datetime": "date",      # SeaTable 的 date 列自带 format，用 formatter 区分
        "bool": "checkbox",
        "select": "single-select",
        "multiselect": "multiple-select",
        "attachment": "file",
    },
}

#: 这些类型**不能**通过建表接口创建，只能人工预建：
#:   · link     —— SeaTable 建关联列需要同时指定对方表和展示列，语义远超「一个类型」；
#:                 飞书/简道云同理。建表时静默降级成 text 会造出一个**假关联列**，
#:                 比直接报错危险得多。
#:   · formula  —— 计算列由公式定义，也不允许客户端写。
NON_CREATABLE_TYPES = ("link", "formula", "lookup", "rollup", "aggregation")


def backend_type(backend: str, neutral: str) -> str:
    """把中立类型翻译成某后端的原生类型名。

    容错两处，都是为了让「换后端」不必改上层代码：
      · 传进来的已经是该后端原生类型名 → 原样放行（增量迁移期的旧调用方仍能用）；
      · 未知类型 → 抛错，**不**静默降级成 text。静默降级会造出类型错误的列，
        而且要到很久以后（数据写进去读不出来时）才暴露。
    """
    t = str(neutral or "text").strip().lower()
    if t in NON_CREATABLE_TYPES:
        raise Unsupported(
            "类型「%s」不能通过建表接口创建（需人工预建）：关联列/计算列都要额外"
            "指定对方表或公式，不是「一个类型」能表达的" % t)
    table = BACKEND_TYPES.get(str(backend or "").lower())
    if table is None:
        raise Unsupported("未知后端「%s」，无法翻译列类型" % backend)
    if t in table:
        return table[t]
    if t in table.values():
        return t  # 已经是该后端原生名，放行
    raise ValueError("未知列类型「%s」（中立类型：%s）" % (neutral, "/".join(NEUTRAL_TYPES)))

# 22 张表（顺序即业务主从关系）。本文件自 1.8 起新增了资源域 / 记忆域 /
# 客户到售后控制平面的表，此前注释里的「18 张」是过时数字（test_smoke 断言 22）。
TABLES = [
    "项目",
    "生产计划",
    "发货清单",
    "维修记录",
    "生产工序",
    "库存核对记录",
    "PCB下单记录",
    "外壳采购记录",
    "IC采购记录",
    "贴片生产记录",
    "PCBA半成品采购记录",
    "组装料采购记录",
    "组装记录",
    "成品采购记录",
    # ── 资源域（对标 MS Project 的资源表 / 资源分配表）──────────
    "资源",       # 人 / 设备 / 外协：谁可用、每天有多少产能、按什么费率计价
    "资源分配",   # 谁在哪个生产计划的哪道工序上、投入多少、什么时间段
    # ── 记忆域（「第二大脑」的本体：原话 + 决策 + 阶段轨迹）────────
    "工作日志",   # 你说的每句话原文 + 提取结果，纯追加不修改
    "阶段轨迹",   # 谁在哪天把哪个项目推进到哪个阶段，用于算阶段停留时长
    # ── 客户到售后控制平面（本地 CSV；正式 production 写入仍走候选/交接）──
    "业务对象台账",
    "状态轨迹",
    "证据链",
    "审批记录",
]

# 资源域列定义（本地模式建表用；SeaTable 模式以 Base metadata 为准）
RESOURCE_COLUMNS = [
    "资源编号", "姓名", "类型", "所属工序", "日产能", "单位",
    "日费率", "在岗状态", "备注",
]
ALLOCATION_COLUMNS = [
    "分配编号", "资源", "生产计划", "工序", "投入量", "单位",
    "开始日期", "结束日期", "状态", "备注",
]

# 记忆域列定义
# 工作日志：纯追加，永不修改。原话是第一等资产——结构化提取会出错，原话不会。
LOG_COLUMNS = [
    "日志编号", "日期", "原话", "关联项目", "提取结果", "写入表", "类型", "记录人",
]
# 阶段轨迹：每次阶段变更追加一条，用于算「卡在打样 47 天」
STAGE_LOG_COLUMNS = [
    "轨迹编号", "日期", "项目", "原阶段", "新阶段", "停留天数", "说明", "异常",
]
BUSINESS_OBJECT_COLUMNS = [
    "object_id", "object_type", "root_id", "parent_id", "customer_id", "project_id",
    "version", "state", "owner", "next_action", "due_date", "summary", "evidence_ids",
    "idempotency_key", "created_at", "updated_at",
]
STATE_TRANSITION_COLUMNS = [
    "transition_id", "root_id", "object_id", "from_state", "to_state", "severity",
    "trigger_event_id", "actor", "reason", "approval_id", "created_at",
]
EVIDENCE_CHAIN_COLUMNS = [
    "event_id", "intent_id", "candidate_id", "write_id", "related_object_type",
    "related_object_id", "source", "source_ref", "summary", "created_at",
]
APPROVAL_RECORD_COLUMNS = [
    "approval_id", "action", "object_id", "before_json", "after_json", "reason",
    "approver", "status", "source_event_ids", "created_at",
]

LOG_TYPES = ["进度", "决策", "问题", "变更", "其他"]

# 资源类型 / 状态取值（写入时做软校验，非法值仅告警不阻断）
RESOURCE_TYPES = ["人员", "设备", "外协"]
RESOURCE_STATUS = ["在岗", "请假", "离职", "维护中", "停用"]
ALLOCATION_STATUS = ["计划中", "进行中", "已完成", "已取消"]

# ══════════════════════════════════════════════════════════════════
# 阶段生命周期（项目从想法到量产交付的全链路）
#
# 重要：「阶段」与「工序」是两件事，此前被混在同一个字段里。
#   · 阶段 = 产品生命周期位置（立项 → 手板 → 试产 → 量产 → 交付）
#   · 工序 = 板子在产线上的物理路径（备料 → SMT → 组装 → 测试 → 出货）
# 一个处于「小批量」阶段的项目，其在制批次同时在走「SMT贴片」工序。
# ══════════════════════════════════════════════════════════════════
STAGES = [
    "立项",      # 需求确认、报价、合同
    "研发",      # 原理图、PCB Layout、软件开发
    "手板",      # 第一版实物，验证功能可行性
    "打样",      # 小量试制，验证工艺可行性
    "试产",      # 产线试跑，验证可量产性
    "小批量",    # 首批交付客户验证
    "批量",      # 稳定重复生产
    "量产",      # 满负荷生产
    "交付",      # 发货、验收、回款
    "维修",      # 售后返修（可与其他阶段并存）
]

# 阶段推进的合法路径。用于自然语言录入时校验「跳阶段」，
# 例如从「立项」直接跳到「量产」必然是漏记了中间过程，需提醒。
# 允许回退（打回上一阶段返工），回退不视为异常但会记录。
STAGE_NEXT = {
    "立项": ["研发"],
    "研发": ["手板"],
    "手板": ["打样", "研发"],        # 手板不过 → 打回研发
    "打样": ["试产", "手板"],
    "试产": ["小批量", "打样"],      # 试产不过 → 回打样
    "小批量": ["批量", "试产"],
    "批量": ["量产", "交付"],
    "量产": ["交付"],
    "交付": ["维修"],
    "维修": [],
}

# 「维修」是并行阶段，任何已交付项目都可能进入，不参与顺序校验
STAGE_PARALLEL = ["维修"]

# 工序：板子在产线上的物理路径。这是「生产计划.阶段」列的实际取值域，
# 与上面的生命周期 STAGES 是两套正交的东西，切勿用 STAGES 去校验它。
# （历史遗留：该列名叫「阶段」但存的是工序，改名会破坏既有数据，故保留列名、
#   仅在此处把语义钉死，避免后来者再次混淆。）
PROCESS_STEPS = [
    "库存核对", "备料", "贴片", "组装", "测试", "出货", "已交付",
]


def stage_index(stage):
    """返回阶段在生命周期中的序号；未知阶段返回 -1。"""
    try:
        return STAGES.index((stage or "").strip())
    except ValueError:
        return -1


def stage_jump_warning(old, new):
    """校验阶段推进是否跳步/非法。返回告警文案，正常推进返回 None。

    只告警不阻断——现实中确实存在「手板直接转小批量」的加急项目，
    但必须让人看见这个决定，而不是悄悄发生。
    """
    old_s, new_s = (old or "").strip(), (new or "").strip()
    if not new_s:
        return None
    if new_s not in STAGES:
        return "阶段「%s」不在标准生命周期内，建议用：%s" % (new_s, " / ".join(STAGES))
    if new_s in STAGE_PARALLEL or not old_s:
        return None
    if old_s not in STAGES:
        return None
    if new_s in STAGE_NEXT.get(old_s, []):
        return None
    oi, ni = stage_index(old_s), stage_index(new_s)
    if ni < oi:
        return "阶段回退：%s → %s（打回返工？请确认这是有意为之）" % (old_s, new_s)
    skipped = STAGES[oi + 1:ni]
    if skipped:
        return "阶段跳步：%s → %s，跳过了 %s。是加急，还是漏记了中间进度？" % (
            old_s, new_s, "/".join(skipped))
    return None


# 语义关联表
# id: 语义关联标识；table/other: 两张表；table_col/other_col: 各自关联列名
#
# ⚠️ 关于 id 字段（2026-09-30 校正，勿再当成契约）
#   id 里存的是**历史上从某个 SeaTable Base 读到的 link_id 字面量**。但 link_id
#   是**后端私有产物**，不是跨后端的东西：飞书用双向关联字段、简道云用关联查询、
#   本地模式用自造键。所以：
#     · 上层**不应**把 id 当 SeaTable link_id 用（正确做法是让后端自己按表名解析，
#       见 SeaTableAdapter._resolve_link_id）；
#     · id 现在的真实职责是**稳定的语义键**（本地模式拿它当 __links__.json 的键）。
#
#   2026-09-30 对生产 Base 做了只读探测（GET /metadata/ 的 columns[].data.link_id），
#   逐条核对了下面 17 条与真实 link 列的对应关系，修正了 5 处列名/归属错误，
#   并标注了 2 条在真实 Base 里**不存在**的条目。真实 Base 有 18 个 link_id。
LINKS = [
    {"id": "1T1Q", "table": "生产计划", "table_col": "贴片生产记录", "other": "贴片生产记录", "other_col": "生产计划"},
    # 修正（2026-09-30）：生产计划侧的列名真实是「成品采购」，不是「成品采购记录」
    {"id": "320W", "table": "生产计划", "table_col": "成品采购", "other": "成品采购记录", "other_col": "生产计划"},
    # 修正（2026-09-30）：生产计划侧的列名真实是「关联项目」，不是「项目」
    {"id": "3Fld", "table": "生产计划", "table_col": "关联项目", "other": "项目", "other_col": "生产计划"},
    {"id": "3p4C", "table": "发货清单", "table_col": "维修记录", "other": "维修记录", "other_col": "发货清单"},
    {"id": "46T9", "table": "生产计划", "table_col": "IC采购记录", "other": "IC采购记录", "other_col": "生产计划"},
    {"id": "B70w", "table": "生产计划", "table_col": "组装记录", "other": "组装记录", "other_col": "生产计划"},
    {"id": "PBdY", "table": "生产计划", "table_col": "PCB下单记录", "other": "PCB下单记录", "other_col": "生产计划"},
    {"id": "PEgd", "table": "项目", "table_col": "发货清单", "other": "发货清单", "other_col": "相关项目"},
    {"id": "Qp9v", "table": "生产计划", "table_col": "库存核对记录", "other": "库存核对记录", "other_col": "生产计划"},
    {"id": "UHZV", "table": "生产计划", "table_col": "组装料采购记录", "other": "组装料采购记录", "other_col": "生产计划"},
    {"id": "j4du", "table": "项目", "table_col": "维修记录", "other": "维修记录", "other_col": "相关项目"},
    {"id": "l90Q", "table": "生产计划", "table_col": "外壳采购记录", "other": "外壳采购记录", "other_col": "生产计划"},
    # 修正（2026-09-30）：wana 与 3Fld 是**两条不同的**关联，方向相反 ——
    #   · 3Fld：生产计划.关联项目  ↔ 项目.生产计划
    #   · wana：项目.阶段        ↔ 生产计划.项目      ← 项目侧那列的列名叫「阶段」
    # 旧记录把 wana 写成「生产计划.table_col=项目 / 项目.other_col=生产计划」，两处都错，
    # 于是 link_id_for 用无序集合比较时与 3Fld 互相命中，永远只返回先出现的 3Fld。
    {"id": "wana", "table": "项目", "table_col": "阶段", "other": "生产计划", "other_col": "项目"},
    # 修正（2026-09-30）：生产计划侧的列名真实是「PCBA采购」，不是「PCBA半成品采购记录」
    {"id": "zPf4", "table": "生产计划", "table_col": "PCBA采购", "other": "PCBA半成品采购记录", "other_col": "生产计划"},
    # 修正（2026-09-30）：生产计划侧的列名真实是「工序」，不是「生产工序」
    {"id": "zwwS", "table": "生产计划", "table_col": "工序", "other": "生产工序", "other_col": "生产计划"},
    # ── 资源域关联 ────────────────────────────────────────────────
    # ⚠️ 以下两条 id 在 2026-09-30 的只读探测中**未在真实生产 Base 里找到**
    #    （真实 Base 只有 18 个 link_id，包含 JV66 / Ya8b / s7AE 但不含 RsAl / PlAl）。
    #    保留它们是因为本地模式把它们当语义键用（test_smoke.py 也断言了 RsAl）；
    #    但**不要**把它们当真实 SeaTable link_id 传给适配器 ——
    #    SeaTableAdapter.link() 会显式报「link_id 不存在」，而不是拿它去撞一个
    #    语焉不详的服务端错误。资源域若要在 SeaTable 上写关联，需先在两表间建好关联列。
    {"id": "RsAl", "table": "资源", "table_col": "资源分配", "other": "资源分配", "other_col": "资源"},
    {"id": "PlAl", "table": "生产计划", "table_col": "资源分配", "other": "资源分配", "other_col": "生产计划"},
    # ── 2026-09-30 补登记：真实 Base 里存在、但此前漏登记的 3 条 ────────
    # 「项目阶段」表尚未登记进上面的 TABLES，故此三条暂只作联通性记录。
    {"id": "JV66", "table": "项目阶段", "table_col": "关联生产计划", "other": "生产计划", "other_col": "关联项目阶段"},
    {"id": "Ya8b", "table": "项目阶段", "table_col": "关联项目", "other": "项目", "other_col": "项目阶段"},
    {"id": "s7AE", "table": "项目阶段", "table_col": "前置阶段", "other": "项目阶段", "other_col": "项目阶段"},
]

# 各表的默认值（模型未提取到时由适配器自动套用）
# "__TODAY__" 占位符在写入时解析为当天 YYYY-MM-DD（Asia/Shanghai）
TABLE_DEFAULTS = {
    "生产计划":   {"状态": "计划中", "阶段": "库存核对", "立项日期": "__TODAY__"},
    "项目":       {"状态": "计划中"},
    "外壳采购记录": {"采购时间": "__TODAY__"},
    "IC采购记录":  {"状态": "未下单"},
    "贴片生产记录": {"状态": "待送料"},
    "PCBA半成品采购记录": {"状态": "未下单"},
    "组装料采购记录": {"状态": "谈判中"},
    "组装记录":   {},
    "成品采购记录": {"状态": "谈判中", "下单时间": "__TODAY__"},
    "资源":       {"类型": "人员", "在岗状态": "在岗", "单位": "人日", "日产能": 1},
    "资源分配":   {"状态": "计划中", "单位": "人日", "开始日期": "__TODAY__"},
    "工作日志":   {"日期": "__TODAY__", "类型": "进度"},
    "阶段轨迹":   {"日期": "__TODAY__"},
}

# 供应商 / 组装厂等「每家公司都不一样」的默认值，不写死在代码里。
# 由 config.yaml 的 defaults 段提供，例如：
#   defaults:
#     外壳采购记录:
#       供应商: 你的外壳厂名
# 没配就留空——留空只是让人填一下，写死别人家的供应商则是错得离谱。
def merged_defaults(table, config=None):
    """合并内置默认值与用户 config 的 defaults；用户的值优先。"""
    out = dict(TABLE_DEFAULTS.get(table, {}))
    user = ((config or {}).get("defaults") or {}).get(table) or {}
    if isinstance(user, dict):
        out.update(user)
    return out

# 列名里含这些字样的，视为「自动编号列」，本地模式自动递增填充（模拟 auto-number）
AUTO_NUMBER_HINT = "编号"


def link_id_for(table: str, other: str):
    """按**方向**反查语义 link_id（本地模式用）。

    方向敏感是必需的，不是洁癖：`生产计划 ↔ 项目` 之间本来就有**两条独立关联** ——
    `3Fld`（生产计划.关联项目 ↔ 项目.生产计划）和 `wana`（项目.阶段 ↔ 生产计划.项目）。
    旧实现用**无序集合** `{ln["table"], ln["other"]} == {table, other}` 比较，两条会
    互相命中，于是永远只返回列表里先出现的那条 —— 「把数据挂到了另一条业务线上」
    这种错误最难排查（不报错、数据看起来也在）。

    匹配顺序：
      ① 方向精确匹配 (table → other)；
      ② 找不到才退回无序匹配 —— 兼容只登记了一侧的条目。
    """
    for ln in LINKS:
        if ln["table"] == table and ln["other"] == other:
            return ln["id"]
    for ln in LINKS:
        if {ln["table"], ln["other"]} == {table, other}:
            return ln["id"]
    return None


def link_col_of(table: str, link_id: str):
    """返回某张表在某个 link_id 上的关联列名。"""
    for ln in LINKS:
        if ln["id"] == link_id and ln["table"] == table:
            return ln["table_col"]
        if ln["id"] == link_id and ln["other"] == table:
            return ln["other_col"]
    return None


def columns_of(table: str):
    """返回预定义列（资源域 + 记忆域；其余表列由首次写入的数据决定）。"""
    return {
        "资源": RESOURCE_COLUMNS,
        "资源分配": ALLOCATION_COLUMNS,
        "工作日志": LOG_COLUMNS,
        "阶段轨迹": STAGE_LOG_COLUMNS,
        "业务对象台账": BUSINESS_OBJECT_COLUMNS,
        "状态轨迹": STATE_TRANSITION_COLUMNS,
        "证据链": EVIDENCE_CHAIN_COLUMNS,
        "审批记录": APPROVAL_RECORD_COLUMNS,
    }.get(table)


def validate_enum(table: str, row: dict):
    """软校验枚举值。返回告警列表（不阻断写入，交由调用方展示）。"""
    checks = {
        ("资源", "类型"): RESOURCE_TYPES,
        ("资源", "在岗状态"): RESOURCE_STATUS,
        ("资源分配", "状态"): ALLOCATION_STATUS,
        ("工作日志", "类型"): LOG_TYPES,
        ("生产计划", "阶段"): PROCESS_STEPS,   # 注意：这列存的是「工序」，不是生命周期阶段
        ("项目", "阶段"): STAGES,              # 生命周期阶段只挂在项目表上
    }
    warns = []
    for (t, col), allowed in checks.items():
        if t != table:
            continue
        v = (row.get(col) or "").strip() if isinstance(row.get(col), str) else row.get(col)
        if v and v not in allowed:
            warns.append("「%s.%s」值 '%s' 不在建议取值 %s 内" % (t, col, v, "/".join(allowed)))
    return warns
