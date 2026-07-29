# -*- coding: utf-8 -*-
"""
生产域静态结构定义（本地模式 / 初始化用）。

这部分把 SKILL.md 里的「表级规则」结构化成数据，供 local 适配器在零配置时
也能正确建表、套默认值、建立双向关联。SeaTable 模式下的真实结构来自 Base 的
metadata，这里的定义是本地模式的“兜底真相”。

注意：SELECT 选项（如 IC 供应商 14 选项）的「显示中文标签」规则仍由 SKILL.md
约束，模型负责把原始值翻译成中文标签；本地模式里存的就是文本，无需翻译。
"""

# 15 张表（顺序即业务主从关系）
TABLES = [
    "项目",
    "项目阶段",
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
]

# 17 条语义关联（本地模式用内部 link_id；SeaTable 模式由 metadata 解析真实 link_id）
# id: 语义关联标识；table/other: 两张表；table_col/other_col: 各自关联列名
LINKS = [
    {"id": "project-stage", "table": "项目", "table_col": "项目阶段", "other": "项目阶段", "other_col": "关联项目"},
    {"id": "stage-production-plan", "table": "项目阶段", "table_col": "关联生产计划", "other": "生产计划", "other_col": "关联项目阶段"},
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
]

# 各表的默认值（模型未提取到时由适配器自动套用）
# "__TODAY__" 占位符在写入时解析为当天 YYYY-MM-DD（Asia/Shanghai）
TABLE_DEFAULTS = {
    "项目阶段":   {"状态": "未开始", "阶段类型": "立项"},
    "生产计划":   {"状态": "计划中", "阶段": "库存核对", "立项日期": "__TODAY__"},
    "项目":       {"状态": "计划中"},
    "外壳采购记录": {"供应商": "华宸振凯", "采购时间": "__TODAY__"},
    "IC采购记录":  {"状态": "未下单"},
    "贴片生产记录": {"状态": "待送料"},
    "PCBA半成品采购记录": {"供应商": "空循环", "状态": "未下单"},
    "组装料采购记录": {"状态": "谈判中"},
    "组装记录":   {"组装厂": "禾平"},
    "成品采购记录": {"状态": "谈判中", "下单时间": "__TODAY__"},
}

# 列名里含这些字样的，视为「自动编号列」，本地模式自动递增填充（模拟 auto-number）
AUTO_NUMBER_HINT = "编号"


# 解耦版的领域结构。LocalAdapter 用它在空表时返回可靠 metadata；
# SeaTable 迁移脚本也以同一结构为准，避免文档、local 与线上 Base 漂移。
TABLE_COLUMNS = {
    "项目阶段": [
        {"name": "阶段名称", "type": "text"},
        {"name": "关联项目", "type": "link", "other_table": "项目"},
        {"name": "阶段类型", "type": "single-select",
         "options": ["立项", "方案设计", "工程样机", "PCB打样", "开模", "试产", "认证", "量产", "结项"]},
        {"name": "阶段轮次", "type": "text"},
        {"name": "状态", "type": "single-select",
         "options": ["未开始", "进行中", "阻塞", "待评审", "已完成", "已取消"]},
        {"name": "计划开始", "type": "date"},
        {"name": "计划完成", "type": "date"},
        {"name": "实际开始", "type": "date"},
        {"name": "实际完成", "type": "date"},
        {"name": "负责人", "type": "collaborator"},
        {"name": "前置阶段", "type": "link", "other_table": "项目阶段"},
        {"name": "关联生产计划", "type": "link", "other_table": "生产计划",
         "managed_by_reciprocal": True},
        {"name": "交付物", "type": "long-text"},
        {"name": "评审结论", "type": "single-select",
         "options": ["未评审", "通过", "有条件通过", "不通过"]},
        {"name": "问题与风险", "type": "long-text"},
        {"name": "下一步动作", "type": "long-text"},
        {"name": "下一步截止日", "type": "date"},
    ],
    "生产计划": [
        {"name": "生产类型", "type": "single-select",
         "options": ["工程样机", "PCB打样", "试产", "小批量", "量产", "返工"]},
        {"name": "试产阶段", "type": "single-select",
         "options": ["EVT", "DVT", "PVT", "首批量产", "不适用"]},
        {"name": "批次号", "type": "text"},
        {"name": "版本号", "type": "text"},
        {"name": "关联项目阶段", "type": "link", "other_table": "项目阶段"},
        {"name": "计划开始日期", "type": "date"},
        {"name": "计划完成日期", "type": "date"},
        {"name": "实际完成日期", "type": "date"},
        {"name": "批次目标", "type": "long-text"},
        {"name": "放行状态", "type": "single-select",
         "options": ["待评审", "允许进入下一阶段", "禁止放行"]},
    ],
}


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
