# -*- coding: utf-8 -*-
"""
生产域静态结构定义（本地模式 / 初始化用）。

这部分把 SKILL.md 里的「表级规则」结构化成数据，供 local 适配器在零配置时
也能正确建表、套默认值、建立双向关联。SeaTable 模式下的真实结构来自 Base 的
metadata，这里的定义是本地模式的“兜底真相”。

注意：SELECT 选项（如 IC 供应商 14 选项）的「显示中文标签」规则仍由 SKILL.md
约束，模型负责把原始值翻译成中文标签；本地模式里存的就是文本，无需翻译。
"""

# 18 张表（顺序即业务主从关系）
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


# 17 条语义关联（本地模式用内部 link_id；SeaTable 模式由 metadata 解析真实 link_id）
# id: 语义关联标识；table/other: 两张表；table_col/other_col: 各自关联列名
LINKS = [
    {"id": "1T1Q", "table": "生产计划", "table_col": "贴片生产记录", "other": "贴片生产记录", "other_col": "生产计划"},
    {"id": "320W", "table": "生产计划", "table_col": "成品采购记录", "other": "成品采购记录", "other_col": "生产计划"},
    {"id": "3Fld", "table": "生产计划", "table_col": "项目", "other": "项目", "other_col": "生产计划"},
    {"id": "3p4C", "table": "发货清单", "table_col": "维修记录", "other": "维修记录", "other_col": "发货清单"},
    {"id": "46T9", "table": "生产计划", "table_col": "IC采购记录", "other": "IC采购记录", "other_col": "生产计划"},
    {"id": "B70w", "table": "生产计划", "table_col": "组装记录", "other": "组装记录", "other_col": "生产计划"},
    {"id": "PBdY", "table": "生产计划", "table_col": "PCB下单记录", "other": "PCB下单记录", "other_col": "生产计划"},
    {"id": "PEgd", "table": "项目", "table_col": "发货清单", "other": "发货清单", "other_col": "相关项目"},
    {"id": "Qp9v", "table": "生产计划", "table_col": "库存核对记录", "other": "库存核对记录", "other_col": "链接其他记录"},
    {"id": "UHZV", "table": "生产计划", "table_col": "组装料采购记录", "other": "组装料采购记录", "other_col": "生产计划"},
    {"id": "j4du", "table": "项目", "table_col": "维修记录", "other": "维修记录", "other_col": "相关项目"},
    {"id": "l90Q", "table": "生产计划", "table_col": "外壳采购记录", "other": "外壳采购记录", "other_col": "生产计划"},
    {"id": "wana", "table": "生产计划", "table_col": "项目", "other": "项目", "other_col": "生产计划"},
    {"id": "zPf4", "table": "生产计划", "table_col": "PCBA半成品采购记录", "other": "PCBA半成品采购记录", "other_col": "生产计划"},
    {"id": "zwwS", "table": "生产计划", "table_col": "生产工序", "other": "生产工序", "other_col": "生产计划"},
    # 资源域关联
    {"id": "RsAl", "table": "资源", "table_col": "资源分配", "other": "资源分配", "other_col": "资源"},
    {"id": "PlAl", "table": "生产计划", "table_col": "资源分配", "other": "资源分配", "other_col": "生产计划"},
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
    """根据两张表名反查语义 link_id（本地模式用）。"""
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
