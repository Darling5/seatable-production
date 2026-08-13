#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
intake.py — 自然语言进度录入的「意图 + 风险分级」层。

定位：这是「第二个大脑」的入口。你早上说一段话，它拆成若干条待写入意图，
按风险分级——低风险直接写，高风险列出来等你确认。

    低风险（AUTO）：补录既有记录的空字段、写工作日志、报进度
    高风险（CONFIRM）：改交期/金额、新建记录、下采购单、阶段跳步、任何删除

设计原则（来自用户明确要求）：
  「重要数据必须列出审核确认后再执行，不然把数据搞乱了」
  → 分级只决定「要不要停下来问」，不决定「要不要给你看」。
    所有写入无论风险高低，执行后都会落一条工作日志，可回溯。

本模块只做**判定与编排**，不做 NLP 抽取——抽取由上层的 AI（读 SKILL.md 的模型）
完成，它把理解结果以 Intent 列表传进来。这样避免在 Python 里写死正则去猜人话，
那条路在中文口语上必然失败。
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters import schema  # noqa: E402

# 风险等级
AUTO = "auto"          # 直接写，事后可在工作日志里看到
CONFIRM = "confirm"    # 必须列出来等人点头

# 高风险字段：碰这些一律要确认，不管在哪张表
SENSITIVE_COLS = {
    "合同交期", "合同总价", "实收", "下单付", "收货付", "验收付",
    "采购花销", "打板价格", "单价", "金额", "数量", "交期",
}

# 高风险表：写这些表等于花钱或对外承诺
SENSITIVE_TABLES = {
    "项目", "PCB下单记录", "外壳采购记录", "IC采购记录",
    "PCBA半成品采购记录", "组装料采购记录", "成品采购记录",
}

# 低风险字段：补录这些不会造成业务后果
SAFE_COLS = {
    "备注", "问题描述", "说明", "良品率", "实际完成", "完成时间",
    "核对结果", "维修清单", "记录人", "原话", "提取结果",
}


class Intent(object):
    """一条待执行的写入意图。

    op      : append / update / stage / log
    table   : 目标表
    data    : 待写入字段
    row_id  : update 时的目标行
    reason  : 为什么要这么写（展示给人看的依据，来自原话）
    """

    def __init__(self, op, table, data, row_id=None, reason=""):
        self.op = op
        self.table = table
        self.data = data or {}
        self.row_id = row_id
        self.reason = reason
        self.risk = AUTO
        self.warnings = []

    def to_dict(self):
        return {"op": self.op, "table": self.table, "data": self.data,
                "row_id": self.row_id, "reason": self.reason,
                "risk": self.risk, "warnings": self.warnings}


def assess(intent, adapter=None):
    """给一条意图定风险级别 + 收集告警。就地修改并返回该意图。"""
    intent.warnings = list(schema.validate_enum(intent.table, intent.data))

    # 1. 删除永远要确认（本模块不产出 delete，留作防御）
    if intent.op == "delete":
        intent.risk = CONFIRM
        intent.warnings.append("删除不可恢复")
        return intent

    # 2. 工作日志 / 阶段轨迹是纯追加的记忆，写错了也只是多一条记录
    if intent.table in ("工作日志", "阶段轨迹"):
        intent.risk = AUTO
        return intent

    # 3. 阶段推进：跳步或回退要确认，正常推进放行
    if intent.op == "stage":
        w = schema.stage_jump_warning(intent.data.get("原阶段"), intent.data.get("新阶段"))
        if w:
            intent.warnings.append(w)
            intent.risk = CONFIRM
        else:
            intent.risk = AUTO
        return intent

    # 4. 新建记录：建在敏感表上要确认（等于花钱/对外承诺）
    if intent.op == "append":
        if intent.table in SENSITIVE_TABLES:
            intent.risk = CONFIRM
            intent.warnings.append("在「%s」新建记录涉及金额或对外承诺" % intent.table)
        else:
            # 新建非敏感记录（如维修单、库存核对）：字段里有敏感列才拦
            hit = SENSITIVE_COLS & set(intent.data.keys())
            intent.risk = CONFIRM if hit else AUTO
            if hit:
                intent.warnings.append("涉及关键字段：%s" % "/".join(sorted(hit)))
        return intent

    # 5. 更新：改敏感字段要确认；只补空的安全字段可自动
    if intent.op == "update":
        hit = SENSITIVE_COLS & set(intent.data.keys())
        if hit:
            intent.risk = CONFIRM
            intent.warnings.append("修改关键字段：%s" % "/".join(sorted(hit)))
            # 把「改动前的值」查出来一并展示，避免盲改
            if adapter and intent.row_id:
                old = _find_row(adapter, intent.table, intent.row_id)
                if old:
                    for c in sorted(hit):
                        intent.warnings.append(
                            "  %s：%s → %s" % (c, old.get(c, "(空)") or "(空)", intent.data[c]))
            return intent
        unsafe = set(intent.data.keys()) - SAFE_COLS
        if unsafe:
            # 改的不是白名单字段，但也不是敏感字段：看是不是在覆盖已有值
            if adapter and intent.row_id:
                old = _find_row(adapter, intent.table, intent.row_id) or {}
                overwriting = [c for c in unsafe if (old.get(c) or "").strip()]
                if overwriting:
                    intent.risk = CONFIRM
                    intent.warnings.append("将覆盖已有值：%s" % "/".join(sorted(overwriting)))
                    return intent
        intent.risk = AUTO
        return intent

    intent.risk = CONFIRM
    return intent


def _find_row(adapter, table, row_id):
    try:
        for r in adapter.list_rows(table):
            if str(r.get("__row_id__")) == str(row_id):
                return r
    except Exception:
        pass
    return None


def split(intents):
    """按风险分成 (自动执行, 待确认) 两组。"""
    return ([i for i in intents if i.risk == AUTO],
            [i for i in intents if i.risk == CONFIRM])


def render_plan(intents):
    """把意图列表渲染成给人看的确认清单。"""
    auto, confirm = split(intents)
    out = []
    if auto:
        out.append("【将自动写入】%d 条（低风险，已记入工作日志可回溯）" % len(auto))
        for i in auto:
            out.append("  · %s" % _one_line(i))
            for w in i.warnings:
                out.append("      ! %s" % w)
    if confirm:
        out.append("")
        out.append("【需要你确认】%d 条 —— 确认前不会写入任何一条" % len(confirm))
        for n, i in enumerate(confirm, 1):
            out.append("  %d) %s" % (n, _one_line(i)))
            if i.reason:
                out.append("      依据：%s" % i.reason)
            for w in i.warnings:
                out.append("      ! %s" % w)
    if not intents:
        out.append("（没有解析出可写入的内容）")
    return "\n".join(out)


def _one_line(i):
    if i.op == "stage":
        return "阶段推进：%s  %s → %s" % (
            i.data.get("项目", "?"), i.data.get("原阶段") or "(未开始)", i.data.get("新阶段"))
    if i.op == "log":
        return "工作日志：%s" % (i.data.get("原话", "")[:40])
    verb = {"append": "新建", "update": "更新", "delete": "删除"}.get(i.op, i.op)
    body = ", ".join("%s=%s" % (k, v) for k, v in list(i.data.items())[:5])
    tail = " …" if len(i.data) > 5 else ""
    rid = ("[%s] " % i.row_id) if i.row_id else ""
    return "%s「%s」%s%s%s" % (verb, i.table, rid, body, tail)


def execute(adapter, intents, actor="", raw_text=""):
    """执行意图。调用方须保证 CONFIRM 项已获批准。

    每条写入都会追加一条工作日志——这是「第二大脑」的记忆本体，
    也是出问题时唯一能回溯「当时为什么这么写」的依据。
    """
    today = datetime.date.today().isoformat()
    results = []
    for i in intents:
        try:
            if i.op == "append":
                rid = adapter.append_row(i.table, i.data)
                results.append((True, "已写入「%s」row_id=%s" % (i.table, rid)))
            elif i.op == "update":
                adapter.update_row(i.table, i.row_id, i.data)
                results.append((True, "已更新「%s」%s" % (i.table, i.row_id)))
            elif i.op == "stage":
                r = _apply_stage(adapter, i, today, actor)
                results.append((True, r))
            elif i.op == "log":
                adapter.append_row("工作日志", i.data)
                results.append((True, "已记入工作日志"))
            else:
                results.append((False, "未知操作：%s" % i.op))
        except Exception as e:
            results.append((False, "失败（%s）：%s" % (i.table, e)))

    # 原话存档：结构化提取会出错，原话不会
    if raw_text:
        try:
            adapter.append_row("工作日志", {
                "日期": today, "原话": raw_text,
                "关联项目": ", ".join(sorted({i.data.get("项目") or i.data.get("生产计划") or ""
                                             for i in intents} - {""})),
                "提取结果": json.dumps([i.to_dict() for i in intents], ensure_ascii=False),
                "写入表": ", ".join(sorted({i.table for i in intents})),
                "类型": "进度", "记录人": actor,
            })
        except Exception as e:
            results.append((False, "原话存档失败：%s" % e))
    return results


def _apply_stage(adapter, intent, today, actor):
    """推进阶段：更新主表 + 追加一条阶段轨迹（用于算停留时长）。"""
    proj = intent.data.get("项目")
    new = intent.data.get("新阶段")
    old = intent.data.get("原阶段") or ""
    # 生命周期阶段挂在「项目」表；「生产计划.阶段」存的是工序，不在这里推进
    table = intent.data.get("_table") or "项目"
    key = "生产产品" if table == "生产计划" else "项目"

    rid = None
    for r in adapter.list_rows(table):
        if (r.get(key) or "").strip() == (proj or "").strip():
            rid = r.get("__row_id__")
            old = old or (r.get("阶段") or "")
            break
    if rid:
        adapter.update_row(table, rid, {"阶段": new})

    # 算在上个阶段待了多久
    stay = ""
    try:
        last = None
        for t in adapter.list_rows("阶段轨迹"):
            if (t.get("项目") or "").strip() == (proj or "").strip():
                last = t
        if last and last.get("日期"):
            d0 = datetime.date.fromisoformat(str(last["日期"])[:10])
            stay = (datetime.date.fromisoformat(today) - d0).days
    except Exception:
        pass

    adapter.append_row("阶段轨迹", {
        "日期": today, "项目": proj, "原阶段": old, "新阶段": new,
        "停留天数": stay, "说明": intent.reason, "记录人": actor,
        "异常": "; ".join(intent.warnings),
    })
    return "「%s」阶段 %s → %s%s" % (proj, old or "(未开始)", new,
                                    ("，上阶段停留 %s 天" % stay) if stay != "" else "")
