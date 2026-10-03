# -*- coding: utf-8 -*-
"""application/decision_support/alignment.py — 二期 / 三期契约对齐层（不是又一套契约）。

存在的原因很直白：二期 `project-brain-v1` 是**公共契约的所有者**，三期
`decision-support-v1` 是**消费方**。两个窗口同时开工时，最容易出的不是「没接口」，
而是**两套看起来一样、其实不一样的东西**：

  · 同一个词两期含义不同（「预测」）→ 复盘时把算法输出当成人的承诺；
  · 同一个字段一边没产出（`plan_id`）→ 联表静默断链，谁都没报错；
  · 同一个时间两种写法（`2026-09-24 09:35:00` 与 `2026-09-26T10:59:03`）
    → 比较字符串的代码看起来永远相等，其实永远不等；
  · 同一个「决策」两期都在写 → 二期决策库被算法候选方案污染。

本模块把上面四类问题变成**可执行检查**（`check_alignment`）与**可断言性质**
（`check_no_authored_decision_id`），并只做三件事：

  1. **回填**统一字段（`make_unified`）—— 术语跟着二期，三期不改名；
  2. **校验**两期输入是否真的指向同一个项目 / 同一个快照 / 同一个契约版本；
  3. **分离**「人做的预测」与「模型算的预测」（`human_prediction_records` /
     `split_prediction_records`）。

明确不做：不新建项目 ID 映射，不复制事实，不建第二套审批器。
执行授权只有一个出口：第一期 `application.authorization`。

对齐结论的权威文档是 ``docs/contracts/alignment-p2-p3-v1.md``；
机器可读的字段映射是 ``docs/contracts/examples/alignment-field-map.json``
（由本模块的 ``FIELD_MAP`` 生成，测试断言二者一致 —— 防止文档与代码各说各话）。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import uuid
from typing import Any

from . import schema as S

# 本层自身的版本。改了字段映射或判定规则就 +1，便于两边对账。
ALIGN_VERSION = "p2p3-align-v1"

# ────────────────────────── 统一字段（术语跟着二期）──────────────────────────
# 顺序与二期 docs/contracts/project-brain-v1.md §1 一致，不重排（下游可能按位读）。
UNIFIED_FIELDS = (
    "project_id", "plan_id", "action_id", "evidence_id",
    "decision_id", "run_id", "snapshot_id", "version",
)

SOURCE_FIELDS = (
    "source_system", "source_base", "source_table",
    "source_row_id", "source_message_id",
)

# ID 形态：与二期 application/project_brain/ids.py 完全一致
# ★ 2026-10-03（批次 1 / G29 前置）：规范形态一律 **3 个字母**。
#   实测缺口：本表原先只登记了 `ids.KIND_PREFIX` 的 15 个前缀，而
#   `application/contracts.py::_ID_PREFIX` 的 12 类业务对象里 **11 类被本校验器拒绝**
#   —— 3 类因前缀只有 2 字母（MO/PO/AS，被 `[A-Z]{3}` 挡掉）、
#   8 类因根本没登记（CTR/CUS/LED/OPP/QUO/REQ/SHP/SOL）。
#   批次 1 的 `PO → GR → 付款单` 这条线要求 PO 的 ID 跨层合法，故一并收口。
#   ⚠️ 本模块**刻意不 import 二期**（见文件头：二期不可用时本层仍须可用），
#   所以这张表是硬编码的并集 —— 靠 `tests/test_cross_layer_ids.py` 的
#   交叉一致性测试防它与那两张表漂移（同 G30 的「防双口径」做法）。
ID_RE = re.compile(r"^(?P<prefix>[A-Z]{3})-(?P<date>\d{8})-(?P<tail>[0-9A-Za-z]{4})$")
# 历史两字母形态：只读兼容，不再用于生成（名单有限且显式，由测试锁定）
LEGACY_ID_RE = re.compile(r"^(?P<prefix>[A-Z]{2})-(?P<date>\d{8})-(?P<tail>[0-9A-Za-z]{4})$")

# 规范前缀 = `project_brain.ids.KIND_PREFIX` ∪ `contracts._ID_PREFIX`
#            ∪ `exec_plane.schema.KIND_PREFIX`（去重）
KNOWN_PREFIXES = ("PRJ", "PLN", "ACT", "EVD", "DEC", "MEM", "OBS", "FCT",
                  "CMT", "PRD", "MSG", "RMD", "SNP", "RUN", "EVT",
                  # ↓ contracts 侧业务对象（2026-10-03 补登记）
                  "CUS", "LED", "OPP", "REQ", "SOL", "QUO", "CTR",
                  "MFO", "PUR", "SHP", "AFS",
                  # ↓ exec_plane 侧执行平面实体（2026-10-03 批次 1 补登记）
                  #   ★ 实测教训：这 7 个前缀是 `exec_plane` 新建实体时加的，
                  #     第一版**忘了登记到这里**，于是执行平面所有 ID 被跨层校验
                  #     全数拒绝 —— 与本次修的 G29 缺口**症状完全一样**。
                  #     抓到它的是 `tests/test_exec_plane.py` 的并集一致性断言，
                  #     不是人记得同步。这正是覆盖矩阵里那句
                  #     「随 G14–G28 逐层建立，每层接入时补跨层 ID 一致性用例」的落地。
                  "ITM", "BOM", "BLN", "PRQ", "GRN", "PAY", "WKO",
                  # ↓ exec_plane 侧批次 2（G24 质量闸 / G25 库存守恒）
                  #   ★ 这次是**先跑测试、看它报红、再补登记**：
                  #     `test_前缀并集恒等于跨层登记表` 当场吐出
                  #     「仅在业务层有：['INS','IVT','MRB']」；同文件的
                  #     `test_本层新生成_ID_全部跨层合法` 同步报
                  #     「inspection 生成 INS-20261003-0007 过不了跨层校验」。
                  #     两个断言**各管一段**：前者管「表全不全」，
                  #     后者管「真生成的 ID 能不能过」—— 只有前者会漏掉
                  #     「表登记了但生成路径没走这张表」这类错位。
                  "INS", "MRB", "IVT", "IVS")
# 历史前缀（两字母）：与 `contracts.LEGACY_ID_PREFIX` 一一对应
LEGACY_PREFIXES = ("MO", "PO", "AS")

# ────────────────────────── 语义分歧裁决表 ──────────────────────────
# 这不是「注意事项」，是对齐结论：同名字段归谁、谁不得写谁。
# 测试逐条断言；改这里必须同步改 alignment-p2-p3-v1.md。
SEMANTIC_SPLITS = [
    {
        "term": "预测",
        "brain_side": "predictions（kind=prediction）：**人**对未来的判断，"
                      "带 actor / rationale / confidence / aspect_key",
        "ds_side": "forecast_date：**算法**由工序/依赖/日历算出的日期，"
                   "带 rules_version / forecast_status",
        "ruling": "两者都保留，复盘时必须按 prediction_source 分开统计；"
                  "禁止把人说的预测与模型算的预测合并成一个「准确率」。",
        "guard": "records 必须带 prediction_source ∈ {model, human}；"
                 "human 记录必须带 source_ref 指回二期 memory_id。",
    },
    {
        "term": "决策",
        "brain_side": "decisions（kind=decision）：**人**做的决策记录，"
                      "decision_id 即其 memory_id，含 rationale",
        "ds_side": "方案比较只产出候选与所需批准（required_approvals），"
                   "**不产出 decision_id**",
        "ruling": "方案选择 ≠ 决策已定 ≠ 执行授权。候选方案被人采纳后，"
                  "由二期经第一期闸门写入决策库；三期永不写决策。",
        "guard": "check_no_authored_decision_id()：三期产出里出现的 "
                 "decision_id 必须全部来自二期上下文，否则报错。",
    },
    {
        "term": "状态",
        "brain_side": "action.status ∈ {candidate, pending_confirm, ready, "
                      "in_progress, blocked, pending_acceptance, closed, cancelled}",
        "ds_side": "step.status ∈ {planned, in_progress, done}；"
                   "forecast_status ∈ {determined, conditional, undetermined}",
        "ruling": "三套词表各管一段：行动归二期，工序与预测确定性归三期。"
                  "不得互相赋值（例如把 conditional 写成 pending_confirm）。",
        "guard": "字段名不同名（status 分属不同层级对象），测试断言互不赋值。",
    },
    {
        "term": "as_of / data_as_of",
        "brain_side": "data_as_of：上下文里**最晚一条记录**的采集时间",
        "ds_side": "as_of：计划快照的时点；data_as_of 回填二期值",
        "ruling": "两者都指「这批数据截至什么时候」，但口径不同，"
                  "因此并列存放、不互相覆盖；不一致时报 as_of_mismatch。",
        "guard": "check_alignment 比较两者，差异超过阈值给 warning。",
    },
    {
        "term": "version",
        "brain_side": "顶层 version = 所有记录 version 之和（单调增，粗粒度护栏）；"
                      "单条记录 version = 乐观锁依据",
        "ds_side": "只回填、只读比对；执行前必须重查版本",
        "ruling": "三期的任何产出都必须带 context_version（当时的顶层 version）。"
                  "未来执行前比对：不一致即拒绝，不用旧版本结论去写库。",
        "guard": "make_unified 强制带 version；compare_versions() 给一致性判定。",
    },
]

# ────────────────────────── 字段映射（文档与代码的单一来源）──────────────────────────
# direction: r = 三期只读二期；w = 三期产出；rw = 两边都有、语义一致
FIELD_MAP = [
    {"field": "contract_version", "brain_path": "contract_version",
     "ds_path": "contract_version", "direction": "r", "required": True,
     "note": "固定 project-brain-v1；不一致即 context_version_mismatch"},
    {"field": "project_id", "brain_path": "project_id", "ds_path": "unified.project_id",
     "direction": "rw", "required": True,
     "note": "PRJ-YYYYMMDD-XXXX；两期必须指向同一个项目"},
    {"field": "plan_id", "brain_path": "*.plan_id（记忆/行动/证据行）",
     "ds_path": "unified.plan_id / steps[*].plan_id",
     "direction": "rw", "required": True,
     "note": "PLN-YYYYMMDD-XXXX；三期快照的 plan_id 必须在二期上下文里被引用过"},
    {"field": "action_id", "brain_path": "actions[].action_id",
     "ds_path": "evidence.action_ids（引用）", "direction": "r", "required": False,
     "note": "三期只引用，不新增行动"},
    {"field": "evidence_id", "brain_path": "evidence_refs[].evidence_id",
     "ds_path": "evidence.evidence_ids", "direction": "r", "required": False,
     "note": "只引用；事实本体在二期，三期不复制"},
    {"field": "decision_id", "brain_path": "decisions[].decision_id",
     "ds_path": "evidence.decision_ids（只读引用）", "direction": "r", "required": False,
     "note": "三期永不产出 decision_id（见 SEMANTIC_SPLITS）"},
    {"field": "run_id", "brain_path": "actions[].run_id / 记忆行 run_id",
     "ds_path": "unified.run_id", "direction": "w", "required": True,
     "note": "RUN-YYYYMMDD-XXXX；三期自己的运行编号，供二期按 run_id 追溯算法运行"},
    {"field": "snapshot_id", "brain_path": "snapshot_id", "ds_path": "unified.snapshot_id",
     "direction": "rw", "required": True,
     "note": "SNP-YYYYMMDD-XXXX；两期必须指向同一个快照，否则结论不可复现"},
    {"field": "version", "brain_path": "version（顶层）", "ds_path": "unified.version",
     "direction": "r", "required": True,
     "note": "回填当时的二期顶层 version，作为「执行前重查版本」的比对基准"},
    {"field": "source_system", "brain_path": "*.source_system",
     "ds_path": "（透传，不参与算法）", "direction": "r", "required": False,
     "note": "仅用于溯源展示；名称类字段一律不作关联键"},
    {"field": "source_table", "brain_path": "*.source_table",
     "ds_path": "（透传，不参与算法）", "direction": "r", "required": False,
     "note": "同上；关联一律用 source_row_id"},
    {"field": "data_as_of", "brain_path": "data_as_of", "ds_path": "data_as_of",
     "direction": "r", "required": False,
     "note": "结论的数据截至时间，不是脚本运行时间"},
    {"field": "source_freshness", "brain_path": "source_freshness[]",
     "ds_path": "caveats[]（转成 GAP_STALE_SNAPSHOT）", "direction": "r",
     "required": False, "note": "stale=true 的来源必须转成「结论待复核」"},
    {"field": "missing_info", "brain_path": "missing_info[]",
     "ds_path": "evidence.missing_info", "direction": "r", "required": False,
     "note": "结构 {code, message, ref, subject, as_of}"},
    {"field": "conflicts", "brain_path": "conflicts[]",
     "ds_path": "evidence.unresolved_conflicts + conditions_from_context()",
     "direction": "r", "required": False,
     "note": "未解决冲突 → 条件按 unknown 处理，不放行；程序不替人裁决"},
    {"field": "predictions", "brain_path": "predictions[]",
     "ds_path": "human_prediction_records() → 复盘记录（prediction_source=human）",
     "direction": "r", "required": False,
     "note": "与人无关的算法预测走 prediction_source=model，两者不合并"},
]

# 授权与边界：写成常量而不是注释，便于测试直接断言
EXECUTION_GATE = {
    "gate_module": "application.authorization",
    "rule": "方案选择不等于执行授权；任何落库都必须先过第一期 WriteGrant。",
    "pre_execution": "执行前用 compare_versions() 重查二期顶层 version，"
                     "不一致即拒绝（旧版本结论不得写库）。",
    "ds_writes_business_tables": False,
    "forbidden_tables": ("项目", "来源消息", "证据", "项目记忆",
                         "行动项", "行动事件", "提醒"),
}

# 时间格式容差：二期存储的是来源原始格式，两种都真实存在
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
)
# 纯日期按当天几点算 —— 两边必须一致，否则同一条约束会差一天
DATE_ONLY_HOUR = 9


# ────────────────────────── ID 与统一字段 ──────────────────────────
def new_run_id(now: _dt.datetime = None, seq: int = 0) -> str:
    """生成与二期同格式的运行编号 ``RUN-YYYYMMDD-XXXX``。

    不 import 二期模块是为了让本层在二期不可用时也能工作（离线算法）；
    格式一致性由 ``is_valid_id`` 与跨期测试双向校验。
    """
    now = now or _dt.datetime.now()
    tail = "%04d" % int(seq) if seq else uuid.uuid4().hex[:4]
    return "RUN-%s-%s" % (now.strftime("%Y%m%d"), tail)


def is_valid_id(value: str, prefix: str = "") -> bool:
    """校验 ``PREFIX-YYYYMMDD-XXXX``；给了 prefix 就要求前缀一致。

    默认**同时接受规范（3 字母）与历史（2 字母）形态** ——
    改前缀不该让历史数据变成「非法数据」，否则跨层校验会把真行判成脏行。
    需要严格判定新写入时用 ``is_canonical_id``。
    """
    return is_canonical_id(value, prefix) or is_legacy_id(value, prefix)


def is_canonical_id(value: str, prefix: str = "") -> bool:
    """**严格**规范判定：3 字母前缀且已在 ``KNOWN_PREFIXES`` 登记。新写入用这个。"""
    m = ID_RE.match(str(value or ""))
    if not m:
        return False
    if prefix and m.group("prefix") != prefix.upper():
        return False
    return m.group("prefix") in KNOWN_PREFIXES


def is_legacy_id(value: str, prefix: str = "") -> bool:
    """**只读**兼容判定：2 字母历史前缀且已在 ``LEGACY_PREFIXES`` 登记。"""
    m = LEGACY_ID_RE.match(str(value or ""))
    if not m:
        return False
    if prefix and m.group("prefix") != prefix.upper():
        return False
    return m.group("prefix") in LEGACY_PREFIXES


def make_unified(*, project_id: str, run_id: str = "", snapshot_id: str = "",
                 plan_id: str = "", action_id: str = "", evidence_id: str = "",
                 decision_id: str = "", version: int = 0) -> dict:
    """回填八个统一字段。**键集合恒等于 UNIFIED_FIELDS**，不多不少。

    run_id 缺省时现场生成 —— 三期的每一次运行都必须能被追溯，
    否则「这次结论是什么时候、按哪个版本算出来的」无从回答。
    """
    out = {
        "project_id": project_id or "",
        "plan_id": plan_id or "",
        "action_id": action_id or "",
        "evidence_id": evidence_id or "",
        "decision_id": decision_id or "",
        "run_id": run_id or new_run_id(),
        "snapshot_id": snapshot_id or "",
        "version": int(version or 0),
    }
    assert tuple(out) == UNIFIED_FIELDS, "统一字段顺序/集合被改动了"
    return out


def compare_versions(context_version: int, current_version: int) -> dict:
    """执行前版本重查：结论基于旧版本 → 拒绝执行，而不是「凑合用」。"""
    cv, nv = int(context_version or 0), int(current_version or 0)
    if cv == nv:
        return {"ok": True, "code": "version_match",
                "message": "版本一致（%d）" % cv}
    return {"ok": False, "code": "version_stale",
            "message": "结论基于版本 %d，当前已是 %d —— 必须重跑后再执行"
                       % (cv, nv), "context_version": cv, "current_version": nv}


# ────────────────────────── 时间：两种写法都要吃 ──────────────────────────
def parse_time(value) -> _dt.datetime | None:
    """解析二期产出的时间。取不到就返回 None —— **绝不猜**。

    纯日期字符串按 ``DATE_ONLY_HOUR``（09:00）算：Python 3.11+ 的
    ``fromisoformat("2026-09-30")`` 会返回 00:00，用 00:00 去比「早于某天 09:00」
    会算早一天，进而把条件放行时间提前一整个工作日。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 10:                       # 纯日期
        try:
            d = _dt.date.fromisoformat(text)
        except ValueError:
            return None
        return _dt.datetime.combine(d, _dt.time(DATE_ONLY_HOUR, 0))
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in _TIME_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_time(value) -> str:
    """归一成 ``YYYY-MM-DDTHH:MM:SS``；解析不了返回空串（不返回原文，免得下游以为能用）。"""
    dt = parse_time(value)
    return dt.isoformat() if dt else ""


def is_iso_t(value) -> bool:
    """判断是否是标准 ISO-T 写法（``2026-09-26T10:59:03``）。

    二期的存储层保留来源原始格式，两种写法都真实存在：
    消息自带的 ``2026-09-24 09:35:00``（空格）与系统生成的 ``2026-09-26T10:59:03``。
    空格写法**能解析但非标准**，会被记成 info 级提示 —— 不阻断，但要看得见。
    """
    text = str(value or "").strip()
    if not text:
        return True
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}T", text))


# ────────────────────────── 读上下文（唯一入口）──────────────────────────
def _slim_conflict(c: dict) -> dict:
    """把二期冲突压成**引用形态**：留 id、留两侧取值、留成员 memory_id，不带原文。

    冲突成员里的 `text` 是记忆原文 —— 原文的家在二期。三期只需要知道
    「哪个方面有两个说法、分别来自哪条记忆」，需要看原话时按 memory_id 回二期取。
    这样三期产出里不会出现二期的记忆正文，也就不会有人误把三期当第二份事实库。
    """
    members = list(c.get("members") or [])
    return {
        "conflict_id": c.get("conflict_id") or "",
        "project_id": c.get("project_id") or "",
        "aspect_key": c.get("aspect_key") or "",
        "values": list(c.get("values") or []),
        "member_ids": [m.get("memory_id") for m in members if m.get("memory_id")],
        "members": [{"memory_id": m.get("memory_id"), "kind": m.get("kind"),
                     "value": m.get("value"), "plan_id": m.get("plan_id") or "",
                     "captured_at": m.get("captured_at") or ""}
                    for m in members],
        "resolved": bool(c.get("resolved")),
        "hint": c.get("hint") or "",
    }


def read_context(ctx: dict) -> dict:
    """把二期 ProjectContext 读成一个**规范化视图**。

    这里不复制事实内容：只留索引（evidence_id / decision_id / memory_id）。
    事实的家永远在二期 —— 三期复制一份，就会出现「两处都能改」的问题。
    """
    ctx = dict(ctx or {})
    memories = {}
    for key, kind in (("facts", "fact"), ("observations", "observation"),
                      ("commitments", "commitment"), ("decisions", "decision"),
                      ("predictions", "prediction")):
        memories[key] = list(ctx.get(key) or [])

    plan_ids: set[str] = set()
    time_notes: list[dict] = []
    for bucket in list(memories.values()) + [list(ctx.get("actions") or []),
                                            list(ctx.get("evidence_refs") or [])]:
        for row in bucket:
            pid = str(row.get("plan_id") or "").strip()
            if pid:
                plan_ids.add(pid)
            raw = row.get("captured_at") or row.get("occurred_at") or ""
            if raw and not is_iso_t(raw):
                time_notes.append({"ref": row.get("memory_id") or row.get("action_id")
                                          or row.get("evidence_id") or "",
                                   "field": "captured_at" if row.get("captured_at")
                                            else "occurred_at",
                                   "value": str(raw)})

    conflicts = [_slim_conflict(c) for c in (ctx.get("conflicts") or [])]
    freshness = list(ctx.get("source_freshness") or [])
    return {
        "present": bool(ctx),
        "align_version": ALIGN_VERSION,
        "contract_version": str(ctx.get("contract_version") or ""),
        "project_id": str(ctx.get("project_id") or ""),
        "snapshot_id": str(ctx.get("snapshot_id") or ""),
        "version": int(ctx.get("version") or 0),
        "data_as_of": str(ctx.get("data_as_of") or ""),
        "plan_ids": sorted(plan_ids),
        "plan_id_absent": not plan_ids,
        "memories": memories,
        "actions": list(ctx.get("actions") or []),
        "blockers": list(ctx.get("blockers") or []),
        "evidence_ids": [str(e.get("evidence_id")) for e in (ctx.get("evidence_refs") or [])
                         if e.get("evidence_id")],
        "decision_ids": [str(d.get("decision_id")) for d in memories["decisions"]
                         if d.get("decision_id")],
        "action_ids": [str(a.get("action_id")) for a in (ctx.get("actions") or [])
                       if a.get("action_id")],
        "conflicts": conflicts,
        "unresolved_conflicts": [c for c in conflicts if not c.get("resolved")],
        "missing_info": list(ctx.get("missing_info") or []),
        "source_freshness": freshness,
        "stale_sources": [s for s in freshness if s.get("stale")],
        "n_observations_unverified": len(memories["observations"]),
        "time_format_notes": time_notes,
        "answers": dict(ctx.get("answers") or {}),
        "fields_missing_on_records": _missing_unified_fields(ctx),
    }


def _missing_unified_fields(ctx: dict) -> dict:
    """检查每条记录是否真的带了八个统一字段（缺哪个、缺几条）。

    这是对「二期契约 §1」的实测，而不是读文档相信它 ——
    早期版本就把 ``plan_id`` 漏在投影层了，文档是对的、代码是错的。
    """
    buckets = {"observations": ctx.get("observations") or [],
               "facts": ctx.get("facts") or [],
               "commitments": ctx.get("commitments") or [],
               "decisions": ctx.get("decisions") or [],
               "predictions": ctx.get("predictions") or [],
               "actions": ctx.get("actions") or [],
               "evidence_refs": ctx.get("evidence_refs") or []}
    out: dict[str, dict] = {}
    for name, rows in buckets.items():
        counter: dict[str, int] = {}
        for r in rows:
            for f in UNIFIED_FIELDS:
                if f not in r:
                    counter[f] = counter.get(f, 0) + 1
        if counter:
            out[name] = counter
    return out


# ────────────────────────── 对齐检查 ──────────────────────────
SEV_ERROR = "error"
SEV_WARN = "warning"
SEV_INFO = "info"


def _item(code: str, severity: str, message: str, ref: str = "",
          how_to_fix: str = "") -> dict:
    return {"code": code, "severity": severity, "message": message,
            "ref": ref, "how_to_fix": how_to_fix}


def check_alignment(ctx: dict, planning: dict, *,
                    expected_contract: str = S.BRAIN_CONTRACT_VERSION,
                    as_of_tolerance_hours: int = 24,
                    require_context: bool = True) -> dict:
    """两期输入是否真的对得上。返回 ``{ok, items, summary}``。

    ``ok=False`` 只表示「有 error 级问题」；warning 不阻断算法，
    但必须显示在结论里 —— 沉默地带着错前提算出漂亮数字，比报错更危险。
    """
    view = read_context(ctx)
    planning = dict(planning or {})
    items: list[dict] = []

    if not view["present"]:
        items.append(_item(
            "context_absent", SEV_ERROR if require_context else SEV_WARN,
            "没有取到 %s 的 ProjectContext，本次结论未绑定二期的证据与缺口"
            % expected_contract,
            how_to_fix="传入 context_port / mock_context_path，或确认二期 brain_root 可导入"))
        return _summarize(items, view, planning)

    # 1) 契约版本
    if view["contract_version"] != expected_contract:
        items.append(_item(
            "contract_version_mismatch", SEV_ERROR,
            "上下文契约版本为 %r，本期对齐的是 %r"
            % (view["contract_version"], expected_contract),
            how_to_fix="确认二期分支是否已合并；版本不一致时不得按现有字段直读"))

    # 2) 项目一致性
    p_pid = str(planning.get("project_id") or "")
    if p_pid and view["project_id"] and p_pid != view["project_id"]:
        items.append(_item(
            "project_id_mismatch", SEV_ERROR,
            "计划快照属于 %s，二期上下文属于 %s —— 不是同一个项目"
            % (p_pid, view["project_id"]), ref=p_pid,
            how_to_fix="两者必须同项目；跨项目取数会让结论张冠李戴"))

    # 3) 快照一致性（结论可复现的前提）
    p_sid = str(planning.get("snapshot_id") or "")
    if p_sid and view["snapshot_id"] and p_sid != view["snapshot_id"]:
        items.append(_item(
            "snapshot_id_mismatch", SEV_ERROR,
            "计划快照 id 为 %s，二期上下文快照 id 为 %s —— 两边不是同一时刻的数据"
            % (p_sid, view["snapshot_id"]), ref=p_sid,
            how_to_fix="让两期引用同一个 snapshot_id，否则结论无法复现"))

    # 4) as_of 差异
    a1, a2 = parse_time(planning.get("as_of")), parse_time(view["data_as_of"])
    if a1 and a2:
        gap = abs((a1 - a2).total_seconds()) / 3600.0
        if gap > as_of_tolerance_hours:
            items.append(_item(
                "as_of_mismatch", SEV_WARN,
                "计划快照时点 %s 与二期数据截至时间 %s 相差 %.1f 小时（阈值 %d）"
                % (a1.isoformat(), a2.isoformat(), gap, as_of_tolerance_hours),
                how_to_fix="差异大说明一边数据较旧，结论要标注「基于过期数据」"))

    # 5) plan_id 形态与联表可验证性
    plan_ids = [str(s.get("plan_id") or "").strip()
                for s in (planning.get("steps") or [])]
    plan_ids = sorted({p for p in plan_ids if p})
    bad = [p for p in plan_ids if not is_valid_id(p, "PLN")]
    if bad:
        items.append(_item(
            "plan_id_invalid_format", SEV_ERROR,
            "计划 id 不符合二期规范 PLN-YYYYMMDD-XXXX：%s" % "、".join(bad),
            ref=bad[0],
            how_to_fix="改成 4 位尾码（如 PLN-20260928-A001）；"
                       "否则二期 validate_id 拒绝，联表键形同虚设"))

    if plan_ids and view["plan_id_absent"]:
        items.append(_item(
            "plan_id_absent_upstream", SEV_WARN,
            "二期上下文里**没有任何记录**带 plan_id，无法验证「同一份计划」",
            how_to_fix="二期补齐记录上的 plan_id（合同 §1）"))
    elif plan_ids and not view["plan_id_absent"]:
        unlinked = [p for p in plan_ids if p not in set(view["plan_ids"])]
        if unlinked:
            items.append(_item(
                "plan_link_unverified", SEV_WARN,
                "计划 %s 在二期上下文里查不到任何引用（记忆/行动/证据都没有）"
                % "、".join(unlinked), ref=unlinked[0],
                how_to_fix="可能是新计划、或二期尚未记录；本结论的「计划↔行动」关联未经证实"))
        else:
            items.append(_item("plan_link_ok", SEV_INFO,
                               "计划 %s 均能在二期上下文里找到引用" % "、".join(plan_ids)))

    # 6) 时间写法
    if view["time_format_notes"]:
        sample = view["time_format_notes"][0]
        items.append(_item(
            "time_format_non_iso", SEV_INFO,
            "%d 条记录的时间是空格写法（如 %s）而非 ISO-T；已按容差解析"
            % (len(view["time_format_notes"]), sample["value"]), ref=sample["ref"],
            how_to_fix="两边新增字段时统一用 ISO-T；读取端必须容忍两种"))

    # 7) 契约 §1 落实度
    miss = view["fields_missing_on_records"]
    if miss:
        detail = "；".join("%s 缺 %s" % (t, "、".join(sorted(f)))
                          for t, f in sorted(miss.items()))
        items.append(_item(
            "unified_fields_incomplete", SEV_WARN,
            "部分记录未携带八个统一字段：%s" % detail,
            how_to_fix="二期投影层补齐（合同 §1）；缺 plan_id 会直接导致联表断链"))

    if not any(i["severity"] in (SEV_ERROR, SEV_WARN) for i in items):
        items.append(_item("align_ok", SEV_INFO,
                           "两期输入对齐（契约版本、项目、快照一致，plan_id 可联表）"))
    return _summarize(items, view, planning)


def _summarize(items: list[dict], view: dict, planning: dict) -> dict:
    errors = [i for i in items if i["severity"] == SEV_ERROR]
    warns = [i for i in items if i["severity"] == SEV_WARN]
    return {
        "align_version": ALIGN_VERSION,
        "ok": not errors,
        "items": items,
        "errors": errors,
        "warnings": warns,
        "summary": {
            "n_errors": len(errors), "n_warnings": len(warns),
            "brain_contract": view["contract_version"],
            "expected_contract": S.BRAIN_CONTRACT_VERSION,
            "project_id": view["project_id"],
            "snapshot_id": view["snapshot_id"] or str(planning.get("snapshot_id") or ""),
            "context_version": view["version"],
            "plan_ids_linked": sorted(set(view["plan_ids"]) &
                                      {str(s.get("plan_id") or "")
                                       for s in (planning.get("steps") or [])}),
        },
        "context_version": view["version"],
    }


def check_no_authored_decision_id(payload, allowed: set) -> dict:
    """三期**不得创作** decision_id：产出里出现的每一个都必须来自二期。

    这是「方案选择不等于执行授权」的结构性保证 —— 不是靠开发者记得别写。
    """
    allowed = {str(x) for x in (allowed or set()) if x}
    found: list[dict] = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                here = "%s.%s" % (path, k) if path else str(k)
                if k == "decision_id" and isinstance(v, str) and v.strip():
                    found.append({"path": here, "value": v})
                walk(v, here)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))

    walk(payload)
    alien = [f for f in found if f["value"] not in allowed]
    return {
        "ok": not alien,
        "referenced": sorted({f["value"] for f in found} - {a["value"] for a in alien}),
        "authored": alien,
        "message": ("产出中的 decision_id 全部来自二期" if not alien
                    else "发现三期自己造的 decision_id：%s —— 决策只能由二期经闸门写入"
                         % "、".join(a["value"] for a in alien)),
    }


# ────────────────────────── 预测：人 vs 模型 ──────────────────────────
PREDICTION_SOURCE_MODEL = "model"
PREDICTION_SOURCE_HUMAN = "human"
PREDICTION_SOURCES = (PREDICTION_SOURCE_MODEL, PREDICTION_SOURCE_HUMAN)


def prediction_source_of(record: dict) -> str:
    src = str((record or {}).get("prediction_source") or PREDICTION_SOURCE_MODEL)
    return src if src in PREDICTION_SOURCES else PREDICTION_SOURCE_MODEL


def human_prediction_records(ctx: dict, *, as_of=None) -> list[dict]:
    """把二期 `predictions[]`（人说的预测）转成**可复盘的记录**。

    三条纪律：
      · `predicted_at` 取 `captured_at`（消息采集时间），**没有就不收** ——
        事后补的时点等于事后编的准确率；
      · 只收 `captured_at <= as_of` 的（防未来信息泄漏，与 backtest.filter_as_of 同口径）；
      · `value` 解析不成日期就 `predicted_date=None`，并用 `undetermined_reasons`
        说明原因，**不填一个猜的日期**。
    """
    view = read_context(ctx)
    cutoff = parse_time(as_of) if as_of else None
    out: list[dict] = []
    for p in view["memories"]["predictions"]:
        predicted_at = parse_time(p.get("captured_at"))
        if predicted_at is None:
            continue
        if cutoff is not None and predicted_at > cutoff:
            continue
        raw = p.get("value") or p.get("due_date") or ""
        d = parse_time(raw)
        rec = {
            "prediction_id": str(p.get("memory_id") or ""),
            "plan_id": str(p.get("plan_id") or ""),
            "prediction_source": PREDICTION_SOURCE_HUMAN,
            "source_ref": str(p.get("memory_id") or ""),
            "predicted_at": predicted_at.isoformat(),
            "predicted_date": d.date().isoformat() if d else None,
            "predicted_status": "determined" if d else "undetermined",
            "actor": str(p.get("actor") or ""),
            "aspect_key": str(p.get("aspect_key") or ""),
            "confidence": p.get("confidence", ""),
            "confidence_source": PREDICTION_SOURCE_HUMAN,
            "rationale": str(p.get("rationale") or ""),
            "text": str(p.get("text") or ""),
            "source_system": str(p.get("source_system") or ""),
            "source_table": str(p.get("source_table") or ""),
            "input_snapshot_id": view["snapshot_id"],
            "rules_version": "",
            "actual": {},
            "undetermined_reasons": ([] if d else
                                     [{"code": "human_prediction_undated",
                                       "message": "人给的预测没有可解析的日期（原文 %r），"
                                                  "不复盘、不补日期" % str(raw)}]),
        }
        out.append(rec)
    return out


def split_prediction_records(records) -> dict:
    """按来源分流。**不合并统计** —— 混合的准确率没有意义。"""
    human = [r for r in (records or []) if prediction_source_of(r) == PREDICTION_SOURCE_HUMAN]
    model = [r for r in (records or []) if prediction_source_of(r) == PREDICTION_SOURCE_MODEL]
    return {"human": human, "model": model,
            "counts": {"human": len(human), "model": len(model),
                       "total": len(human) + len(model)}}


def merge_prediction_records(model_records, human_records) -> dict:
    """合并成一个列表但**保留来源标签**，按预测时点排序。

    同一个计划可以既有人的预测又有模型的预测 —— 那正是要比较的东西，
    所以**不按 plan_id 去重、不互相覆盖**。
    """
    merged: list[dict] = []
    for r in (model_records or []):
        merged.append(dict(r, prediction_source=PREDICTION_SOURCE_MODEL))
    for r in (human_records or []):
        merged.append(dict(r, prediction_source=PREDICTION_SOURCE_HUMAN))
    merged.sort(key=lambda r: (str(r.get("predicted_at") or ""),
                               prediction_source_of(r),
                               str(r.get("prediction_id") or "")))
    return {
        "records": merged,
        "split": split_prediction_records(merged),
        "note": "human（人说的）与 model（算法算的）并列保存，"
                "复盘时分别统计，禁止合并成一个准确率。",
    }


# ────────────────────────── 二期模块定位 ──────────────────────────
# 两期都把模块放在**同一个顶层包名** `application/` 下。分支并行开发时，
# `sys.path` 上只能有一个 `application` —— 先被导入的那个会赢，另一个整包不可见。
# 二期的内部导入全是相对导入（`from . import schema` / `from .. import contracts`），
# 所以可以把它整个 `application` 包**用别名装载**，实现同进程共存。
# 合并成一棵树后这条路不需要走（直接 import 即可），此机制只为合并前验证。
_BRAIN_ALIAS = "_pb_application"


def find_brain_root(extra: str = "") -> tuple[str, str]:
    """找一个能 import 二期 `application.project_brain` 的根目录。

    返回 ``(root, kind)``；``kind`` ∈ {in_tree, worktree, missing}。
    合并后两期在同一棵树里 → ``in_tree``；当前分支开发 → 兄弟 worktree。
    """
    try:                                                      # 同树（合并后）
        import application.project_brain as _pb             # noqa: F401
        return "", "in_tree"
    except Exception:                                        # noqa: BLE001
        pass
    if extra and extra != "auto" and os.path.isdir(extra):
        if os.path.isdir(os.path.join(extra, "application", "project_brain")):
            return extra, "worktree"
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        repo = os.path.abspath(os.path.join(here, "..", ".."))
        parent = os.path.dirname(repo)
        for name in ("p2-project-brain", "project-brain", "p2-project_brain",
                     "seatable-production-1.8.0"):
            cand = os.path.join(parent, name)
            if os.path.isdir(os.path.join(cand, "application", "project_brain")):
                return cand, "worktree"
    except Exception:                                        # noqa: BLE001
        pass
    return "", "missing"


def import_brain(root: str = "") -> tuple[Any, str, str]:
    """导入二期模块。返回 ``(module 或 None, kind, error)``。

    ``kind`` ∈ {``in_tree``, ``isolated``, ``missing``}：

      · ``in_tree``   —— 两期已在同一棵树（正式形态），普通 import 即可；
      · ``isolated``  —— 分支并行：把二期整个 `application` 包以别名装载，
                         绕开顶层包名冲突。**仅用于合并前验证**。
    """
    try:
        import application.project_brain as pb
        return pb, "in_tree", ""
    except Exception as e:                                   # noqa: BLE001
        first_err = "%s: %s" % (type(e).__name__, e)

    root2, kind = find_brain_root(root)
    if kind != "worktree" or not root2:
        return None, "missing", first_err

    try:
        import importlib
        import importlib.util
        if _BRAIN_ALIAS in sys.modules:
            return (importlib.import_module(_BRAIN_ALIAS + ".project_brain"),
                    "isolated", "")
        app_dir = os.path.join(root2, "application")
        spec = importlib.util.spec_from_file_location(
            _BRAIN_ALIAS, os.path.join(app_dir, "__init__.py"),
            submodule_search_locations=[app_dir])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_BRAIN_ALIAS] = mod
        spec.loader.exec_module(mod)
        return (importlib.import_module(_BRAIN_ALIAS + ".project_brain"),
                "isolated", "")
    except Exception as e:                                   # noqa: BLE001
        return None, "missing", "%s（并加载失败：%s: %s）" % (
            first_err, type(e).__name__, e)


def load_context(project_id: str, snapshot_id: str = "",
                 brain_root: str = "", data_root: str = "") -> tuple[dict, str, str]:
    """读一份真实的二期 ProjectContext。返回 ``(ctx, kind, error)``。

    ``data_root`` 是二期记忆库的数据目录（默认用二期的默认值）。
    读不到就返回 ``({}, kind, error)`` —— **不编一个空上下文冒充成功**，
    调用方据此回退 mock 并把原因带进结论。
    """
    pb, kind, err = import_brain(brain_root)
    if pb is None:
        return {}, kind, err
    try:
        fn = getattr(pb, "get_project_context", None)
        if fn is None:
            return {}, kind, "二期模块没有 get_project_context"
        return dict(fn(project_id, snapshot_id, root=data_root) or {}), kind, ""
    except Exception as e:                                   # noqa: BLE001
        return {}, kind, "%s: %s" % (type(e).__name__, e)


# ────────────────────────── 样例生成（供文档用）──────────────────────────
def field_map_json() -> str:
    """把 FIELD_MAP 序列化成 JSON —— 文档里的映射表由它生成，防止手写漂移。"""
    return json.dumps({
        "align_version": ALIGN_VERSION,
        "brain_contract": S.BRAIN_CONTRACT_VERSION,
        "ds_contract": S.DS_CONTRACT_VERSION,
        "unified_fields": list(UNIFIED_FIELDS),
        "source_fields": list(SOURCE_FIELDS),
        "semantic_splits": SEMANTIC_SPLITS,
        "field_map": FIELD_MAP,
        "execution_gate": EXECUTION_GATE,
    }, ensure_ascii=False, indent=2)


__all__ = ["ALIGN_VERSION", "UNIFIED_FIELDS", "SOURCE_FIELDS", "SEMANTIC_SPLITS",
           "FIELD_MAP", "EXECUTION_GATE", "DATE_ONLY_HOUR",
           "ID_RE", "LEGACY_ID_RE", "KNOWN_PREFIXES", "LEGACY_PREFIXES",
           "PREDICTION_SOURCE_MODEL", "PREDICTION_SOURCE_HUMAN", "PREDICTION_SOURCES",
           "new_run_id", "is_valid_id", "is_canonical_id", "is_legacy_id",
           "make_unified", "compare_versions",
           "parse_time", "normalize_time", "is_iso_t", "read_context",
           "check_alignment", "check_no_authored_decision_id",
           "prediction_source_of", "human_prediction_records",
           "split_prediction_records", "merge_prediction_records",
           "find_brain_root", "import_brain", "load_context", "field_map_json",
           "SEV_ERROR", "SEV_WARN", "SEV_INFO"]
