#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信事项双 Base 分流的离线预览器。

本模块只把已经标准化的微信事件/意图转换为候选 JSON，不读取或写入
SeaTable，也不会上传证据图片。路由必须由 ``target`` 或 ``base`` 明确给出；
没有明确路由的意图会进入 ``unrouted``，绝不会默认为 production。

可接受的输入示例::

    {"event_id": "WX-1", "text": "...", "intents": [
      {"target": "production", "op": "update", "table": "项目",
       "data": {"合同交期": "2026-10-01"}},
      {"base": "tasks", "op": "append", "data": {
       "名称": "跟进交期", "负责人": "张三", "截止日期": "2026-09-20"}}
    ]}

``python wx_dispatch.py preview event.json`` 只打印候选 JSON。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

BASES = ("production", "tasks")
TASK_FIELDS = (
    "名称", "负责人", "部门", "优先级", "详情", "执行时间", "计划完成时间", "状态"
)

# 允许输入方使用英文或常见中文别名，但输出始终采用任务 Base 的日常字段。
_TASK_ALIASES = {
    "名称": ("名称", "任务名称", "任务", "name", "title", "task_name"),
    "负责人": ("负责人", "执行人", "经办人", "assignee", "owner", "负责人姓名"),
    "部门": ("部门", "所属部门", "department", "dept"),
    "优先级": ("优先级", "priority", "级别"),
    "详情": ("详情", "详细内容", "描述", "说明", "备注", "detail", "description", "body"),
    "执行时间": ("执行时间", "开始时间", "执行日期", "execution_time", "start_time", "start_date"),
    "计划完成时间": ("计划完成时间", "截止日期", "截止时间", "计划截止日期", "due_date", "deadline", "planned_finish_time"),
    "状态": ("状态", "任务状态", "status"),
}


def _jsonable(value: Any) -> Any:
    """把输入转换成可稳定序列化的简单结构。"""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(value[k]) for k in sorted(value, key=lambda x: str(x))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(x) for x in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_key(value: Any) -> str:
    return "wx-" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:24]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _normalise_base(value: Any) -> str | None:
    """只接受明确的 production/tasks（及不含歧义的英文大小写）。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    aliases = {
        "production": "production", "prod": "production",
        "生产": "production", "生产业务": "production",
        "tasks": "tasks", "task": "tasks", "待办": "tasks", "任务": "tasks",
    }
    return aliases.get(text)


def _explicit_targets(item: Mapping[str, Any], event: Mapping[str, Any] | None = None) -> tuple[list[str], str | None]:
    """返回 (目标列表, 错误原因)。任何冲突/未知值都不静默纠正。"""
    vals: list[Any] = []
    has_explicit = False
    for key in ("target", "base"):
        if key in item and item[key] not in (None, ""):
            has_explicit = True
            vals.extend(_as_list(item[key]))
    if not vals and event is not None:
        # targets 是显式的双路写法；target/base 也允许为数组。
        for key in ("targets", "target", "base"):
            if key in event and event[key] not in (None, ""):
                has_explicit = True
                vals.extend(_as_list(event[key]))
    if not vals:
        return [], "缺少明确 target/base"
    bases = [_normalise_base(v) for v in vals]
    if any(v is None for v in bases):
        return [], "target/base 必须是 production 或 tasks"
    out = list(dict.fromkeys(v for v in bases if v))
    return out, None


def map_task_fields(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """将任务意图字段映射为任务 Base 的八个日常字段。

    未提及的字段显式保留为空，不擅自填状态/日期；未知字段不丢失，而由
    候选的 ``intent`` 保留，方便人工确认时查看原始意图。
    """
    src = data or {}
    result: dict[str, Any] = {field: "" for field in TASK_FIELDS}
    for field, aliases in _TASK_ALIASES.items():
        for alias in aliases:
            if alias in src and src[alias] not in (None, ""):
                result[field] = src[alias]
                break
    return result


def _evidence_items(event: Mapping[str, Any], intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = intent.get("evidence")
    if raw in (None, ""):
        raw = event.get("evidence")
    if raw in (None, ""):
        raw = event.get("evidence_images") or event.get("image_metadata")
    result: list[dict[str, Any]] = []
    for item in _as_list(raw):
        if isinstance(item, Mapping):
            rec = copy.deepcopy(dict(item))
        else:
            rec = {"path": str(item)}
        # 明确标记候选阶段绝不上传；仅引用/保留元数据。
        rec.setdefault("source", "wechat")
        rec["upload"] = False
        rec["upload_status"] = "not_uploaded"
        result.append(rec)
    return result


def _candidate(event: Mapping[str, Any], intent: Mapping[str, Any], base: str, index: int, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    data = intent.get("data")
    if not isinstance(data, Mapping):
        data = {}
    # production 有意保留原始 intent/data，不在此模块猜测目标表和业务字段。
    original = copy.deepcopy(dict(intent))
    original.pop("target", None)
    original.pop("base", None)
    original.pop("evidence", None)
    body = {
        "event_id": event.get("event_id") or event.get("事件编号") or "",
        "source": event.get("source") or event.get("来源") or event.get("来源群") or "wechat",
        "base": base,
        "intent_index": index,
        "intent": original,
        "evidence": evidence,
    }
    candidate: dict[str, Any] = {
        "candidate_type": "wechat_intent",
        "event_id": body["event_id"],
        "target": base,
        "base": base,
        "dedupe_key": _hash_key(body),
        "status": "待确认",
        "preview_only": True,
        "write_online": False,
        "intent": original,
        "evidence": evidence,
    }
    if base == "tasks":
        mapped = map_task_fields(data)
        candidate["table"] = intent.get("table") or ""
        candidate["fields"] = mapped
        candidate["data"] = mapped
        candidate["field_mapping"] = {"source": dict(data), "target_fields": list(TASK_FIELDS)}
    else:
        # table 只有上层明确给出时才展示；不从分类、文本或关键词推断。
        if "table" in intent:
            candidate["table"] = intent.get("table")
        candidate["data"] = copy.deepcopy(dict(data))
        candidate["fields"] = copy.deepcopy(dict(data))
        candidate["field_mapping"] = {"preserved_intent": True}
    return candidate


def _expand_intents(payload: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    raw = payload.get("intents")
    if raw is None:
        raw = payload.get("intent")
    if raw is None:
        # 也接受 {production: {...}, tasks: {...}} 这种明确双路结构。
        out: list[tuple[int, Mapping[str, Any]]] = []
        idx = 0
        for base in BASES:
            if isinstance(payload.get(base), Mapping):
                item = dict(payload[base])
                item.setdefault("target", base)
                out.append((idx, item)); idx += 1
        return out
    items = raw if isinstance(raw, list) else [raw]
    return [(i, x) for i, x in enumerate(items) if isinstance(x, Mapping)]


def build_preview(payload: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """生成离线双 Base 预览，不执行任何写入。"""
    events = list(payload) if isinstance(payload, list) else [payload]
    candidates: list[dict[str, Any]] = []
    unrouted: list[dict[str, Any]] = []
    for event_no, raw_event in enumerate(events):
        if not isinstance(raw_event, Mapping):
            unrouted.append({"index": event_no, "reason": "事件必须是 JSON 对象", "intent": raw_event})
            continue
        event = dict(raw_event)
        for idx, raw_intent in _expand_intents(event):
            intent = dict(raw_intent)
            targets, error = _explicit_targets(intent, event)
            if error:
                unrouted.append({
                    "event_id": event.get("event_id") or event.get("事件编号") or "",
                    "intent_index": idx, "reason": error, "intent": copy.deepcopy(intent),
                })
                continue
            evidence = _evidence_items(event, intent)
            for base in targets:  # 一个意图显式 target=[production,tasks] 即生成双路候选
                candidates.append(_candidate(event, intent, base, idx, evidence))
    return {
        "version": 1,
        "preview_only": True,
        "write_online": False,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "unrouted": unrouted,
    }


def dispatch_event(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    """便捷 API：只返回已路由候选列表。"""
    return build_preview(event)["candidates"]


def dispatch_intents(intents: Sequence[Mapping[str, Any]], *, event: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """便捷 API：给定 intents，返回完整预览（含未路由项）。"""
    base_event = dict(event or {})
    base_event["intents"] = list(intents)
    return build_preview(base_event)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="微信事项双 Base 分流（仅生成离线预览）")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("preview", help="读取标准化事件 JSON，输出候选 JSON")
    p.add_argument("input", help="输入 JSON 文件路径；使用 - 从 stdin 读取")
    p.add_argument("--out", help="可选：把预览 JSON 写到本地文件（仍不会写线上）")
    p.add_argument("--compact", action="store_true", help="压缩 JSON 输出")
    args = ap.parse_args(argv)
    if args.command != "preview":
        return 2
    try:
        text = sys.stdin.read() if args.input == "-" else open(args.input, "r", encoding="utf-8-sig").read()
        payload = json.loads(text)
        result = build_preview(payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print("[错误] 无法生成预览：%s" % exc, file=sys.stderr)
        return 2
    output = json.dumps(result, ensure_ascii=False, indent=None if args.compact else 2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
