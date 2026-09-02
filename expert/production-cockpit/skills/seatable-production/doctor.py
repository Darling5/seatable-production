#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doctor.py — 开局体检：把「你还缺什么」一次性说清楚。

为什么需要它：
  新用户装上这套东西、连上自己的空库，跑一下驾驶舱 —— 不报错，
  生成一个全 0 的空壳。这是最糟的形态：用户以为装好了，
  对着空仪表盘发呆，不知道下一步干嘛。

  「不报错」不等于「能用」。doctor 的职责就是把这中间的差距讲明白，
  并且每一条都附上「那我现在该敲什么命令」。

分级：
  BLOCK  致命，核心能力直接失效（例：PartDB 没配 → 缺料推算全废）
  WARN   能跑但结果会失真（例：项目表缺「合同交期」→ 倒排算不出来）
  INFO   建议优化，不影响正确性
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters import schema  # noqa: E402

BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"
_ORDER = {BLOCK: 0, WARN: 1, INFO: 2}
_LABEL = {BLOCK: "严重", WARN: "警告", INFO: "建议"}

# 缺了它们，对应能力直接不可用
CRITICAL_COLS = {
    "项目": ["合同交期", "合同总价"],
    "生产计划": ["生产产品", "数量", "合同交期"],
}


class Finding(object):
    def __init__(self, level, title, detail="", fix=""):
        self.level = level
        self.title = title
        self.detail = detail
        self.fix = fix


def _safe_rows(adapter, table):
    """读一张表；表不存在/读失败都当空表，不让体检本身崩掉。"""
    try:
        return adapter.list_rows(table) or []
    except Exception:
        return []


def check_tables(adapter):
    """表是否齐全。缺表 = 对应模块整块消失。"""
    out = []
    missing = []
    for t in schema.TABLES:
        try:
            adapter.list_rows(t)
        except Exception:
            missing.append(t)
    if missing:
        out.append(Finding(
            WARN, "缺 %d 张表：%s" % (len(missing), "、".join(missing[:6])),
            "缺失的表对应的模块不会出现在驾驶舱里。",
            "本地模式下写入第一条数据即自动建表；SeaTable 模式需先在 Base 里建表。"))
    return out


def check_columns(adapter):
    """关键列是否存在。有表无列，算出来的数会静默失真 —— 比缺表更危险。"""
    out = []
    for table, cols in CRITICAL_COLS.items():
        rows = _safe_rows(adapter, table)
        if not rows:
            continue
        present = set()
        for r in rows[:50]:
            present |= set(r.keys())
        lack = [c for c in cols if c not in present]
        if lack:
            out.append(Finding(
                WARN, "「%s」表缺关键列：%s" % (table, "、".join(lack)),
                "缺「合同交期」→ 倒排算不出最晚下单日；缺「数量」→ BOM 展开无法乘。",
                "在表里补上这些列，或用 op.py update 给已有记录补值。"))
    return out


def check_data(adapter):
    """有没有数据。空库不是错误，但必须告诉用户「现在还看不到东西」。"""
    out = []
    projects = _safe_rows(adapter, "项目")
    plans = _safe_rows(adapter, "生产计划")

    if not projects and not plans:
        out.append(Finding(
            BLOCK, "数据库是空的，驾驶舱会显示全 0",
            "没有任何项目和生产计划，所有分析、建议、倒排都无从算起。",
            "想先看效果：python seed_demo.py（灌演示数据）\n"
            "        正式使用：把合同发给我，我帮你提取并录入"))
        return out

    if not projects:
        out.append(Finding(
            WARN, "「项目」表为空", "没有合同信息，算不出金额、回款、交付节点。",
            "把客户合同发我，我提取「合同总价/交期/付款条件」后列给你确认"))
    if not plans:
        out.append(Finding(
            WARN, "「生产计划」表为空", "没有在制计划，产线流转和工时分析是空的。",
            "python op.py apply-wizard  或直接用自然语言说「给 XX 建生产计划」"))

    # 交期缺失：倒排的直接输入
    if projects:
        no_due = [p for p in projects if not (p.get("合同交期") or "").strip()]
        if no_due:
            out.append(Finding(
                WARN, "%d/%d 个项目没有合同交期" % (len(no_due), len(projects)),
                "交期是倒排的起点，缺了就算不出「今天必须下单」。",
                "补录：python op.py update 项目 <row_id> --set 合同交期=2026-09-30"))
    return out


def check_inventory(config):
    """采购库存可来自 PartDB、ERP API/MCP 或客户维护的 Excel/CSV。"""
    out = []
    inventory = (config or {}).get("inventory") or {}
    source = str(inventory.get("source") or "").strip().lower()
    partdb = (config or {}).get("partdb") or {}
    if not source and partdb.get("enabled"):
        source = "partdb"

    if source == "partdb":
        if not partdb.get("url") or not partdb.get("token"):
            out.append(Finding(
                BLOCK, "PartDB 库存源缺 URL 或 Token",
                "已选择 PartDB 作为库存源，但连接信息不完整。",
                "填写 config.yaml 的 partdb.url / partdb.token，或改用 inventory.source: file。"))
        return out

    if source in ("file", "excel", "csv", "erp-export", "kingdee-export",
                  "jiandaoyun-export", "zentao-export"):
        file_cfg = inventory.get("file") or {}
        if not file_cfg.get("path"):
            out.append(Finding(
                BLOCK, "文件库存源未配置导出文件路径",
                "采购缺口无法计算，直到客户提供 Excel/CSV 库存导出。",
                "运行 python pipeline/run.py init，把导出文件放进 pipeline/customer/inventory/，再填写 inventory.file.path。"))
        return out

    if source in ("api", "http", "rest", "kingdee-api", "jiandaoyun-api", "zentao-api"):
        api = inventory.get("api") or {}
        if not api.get("base_url") or not api.get("list_path"):
            out.append(Finding(
                BLOCK, "HTTP API 库存源缺少接口地址",
                "已有 ERP 的 API 接入需要基础地址和库存列表接口路径。",
                "填写 inventory.api.base_url / list_path，并按 ERP 文档配置 auth、data_path、pagination 和 fields。"))
        return out

    if source in ("mcp", "mcp-tool"):
        mcp = inventory.get("mcp") or {}
        if not mcp.get("command") or not mcp.get("tool"):
            out.append(Finding(
                BLOCK, "MCP 库存源缺少服务命令或工具名",
                "MCP 接入需要客户本地 MCP 服务命令和库存查询工具名。",
                "填写 inventory.mcp.command / tool，并按工具返回结构配置 data_path 和 fields。"))
        return out

    out.append(Finding(
        BLOCK, "未配置采购库存源",
        "合同采购量无法与库存核对，不能安全地产生缺口。",
        "在 config.yaml 设置 inventory.source: partdb、api、mcp 或 file；已有 ERP 优先配置 API/MCP，文件导出作为兜底。"))
    return out


# 保留旧函数名，兼容外部脚本调用。
def check_partdb(config):
    return check_inventory(config)


def check_localization(config):
    """写死的业务常量 —— 直接决定这套东西能不能给别家用。"""
    out = []
    hard = []
    for table, defaults in schema.TABLE_DEFAULTS.items():
        for col, val in defaults.items():
            if col in ("供应商", "组装厂") and isinstance(val, str) and val:
                hard.append("%s.%s=%s" % (table, col, val))
    if hard:
        out.append(Finding(
            INFO, "检测到内置的供应商默认值（来自原开发者的公司）",
            "这些值会被当成你的默认供应商填进记录里：\n      " + "；".join(hard),
            "在 config.yaml 里加 defaults 段覆盖，或录入时显式指定供应商"))
    return out


def run(adapter, config):
    findings = []
    findings += check_inventory(config)
    findings += check_tables(adapter)
    findings += check_columns(adapter)
    findings += check_data(adapter)
    findings += check_localization(config)
    findings.sort(key=lambda f: _ORDER.get(f.level, 9))
    return findings


def render(findings):
    if not findings:
        return "体检通过：表结构完整、数据齐全、库存源已配置。\n可以直接跑 python cockpit.py 看驾驶舱。"

    lines = []
    n_block = sum(1 for f in findings if f.level == BLOCK)
    n_warn = sum(1 for f in findings if f.level == WARN)

    lines.append("=" * 64)
    lines.append("开局体检：%d 项严重 · %d 项警告 · 共 %d 条"
                 % (n_block, n_warn, len(findings)))
    lines.append("=" * 64)
    for i, f in enumerate(findings, 1):
        lines.append("")
        lines.append("[%s] %d. %s" % (_LABEL.get(f.level, f.level), i, f.title))
        if f.detail:
            lines.append("      " + f.detail)
        if f.fix:
            lines.append("  怎么办：" + f.fix)

    lines.append("")
    lines.append("-" * 64)
    if n_block:
        lines.append("有 %d 项严重问题，核心能力（库存核对 / 下一步建议）目前不可用。" % n_block)
        lines.append("建议先解决它们，再看驾驶舱 —— 否则看到的数是不完整的。")
    else:
        lines.append("没有致命问题，可以开始用了：python cockpit.py")
    return "\n".join(lines)


def has_blocker(findings):
    return any(f.level == BLOCK for f in findings)
