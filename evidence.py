#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信图片证据的本地元数据与清理工具。

此模块刻意与微信读取引擎、SeaTable 写入链路解耦：它只处理证据元数据，
不会上传图片，也不会主动连接线上服务。图片登记时默认保留原文件，优先
记录可供检索的文字摘要、哈希和来源信息。

命令示例::
    python evidence.py register-image photo.jpg --summary "快递单号..."
    python evidence.py scan --root data/wechat_intake
    python evidence.py prune --root data/wechat_intake --yes

``scan`` 永远只输出候选；``prune`` 没有 ``--yes`` 时也只输出候选。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

DEFAULT_RETENTION_DAYS = 90
DEFAULT_INDEX_NAME = "evidence_index.json"
CLOSED_STATUSES = frozenset({
    "已闭环", "已完成", "已解决", "已结案", "已确认", "已忽略",
    "closed", "complete", "completed", "resolved", "done", "approved", "ignored",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "长期", "长期保留"}


def is_closed(record: Mapping[str, Any]) -> bool:
    """判断证据是否已闭环；未知/空状态一律按未闭环处理。"""
    if _parse_time(record.get("closed_at") or record.get("closedAt")):
        return True
    status = str(record.get("status") or record.get("状态") or "").strip().lower()
    return status in {s.lower() for s in CLOSED_STATUSES}


def is_long_term(record: Mapping[str, Any]) -> bool:
    return any(_bool(record.get(k)) for k in (
        "long_term_retention", "long_term", "retain_forever", "长期保留", "永久保留",
    ))


def _record_time(record: Mapping[str, Any], fallback: Optional[Path] = None) -> Optional[datetime]:
    for key in ("created_at", "captured_at", "created", "日期", "date"):
        dt = _parse_time(record.get(key))
        if dt:
            return dt
    if fallback is not None:
        try:
            return datetime.fromtimestamp(fallback.stat().st_mtime, timezone.utc)
        except OSError:
            pass
    return None


@dataclass
class EvidenceRecord:
    evidence_id: str
    kind: str = "image"
    path: str = ""
    created_at: str = ""
    summary: str = ""
    event_id: str = ""
    status: str = "待处理"
    long_term_retention: bool = False
    source: str = "wechat"
    sha256: str = ""
    size: int = 0
    mime_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def image_metadata(
    image_path: os.PathLike[str] | str,
    *,
    summary: str = "",
    event_id: str = "",
    status: str = "待处理",
    long_term_retention: bool = False,
    source: str = "wechat",
    evidence_id: str = "",
    created_at: Optional[str] = None,
) -> dict[str, Any]:
    """读取图片基本属性并生成元数据；不复制、不上传、不修改图片。"""
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    digest, size = _sha256(path)
    eid = evidence_id or "img-" + digest[:16]
    # 没有 OCR/视觉摘要时仍提供可检索文字元数据，而不是把图片内容上传。
    text = (summary or "").strip() or "图片证据：%s（sha256:%s）" % (path.name, digest[:16])
    return EvidenceRecord(
        evidence_id=eid,
        path=str(path),
        created_at=created_at or _utc_now().isoformat(),
        summary=text,
        event_id=event_id,
        status=status,
        long_term_retention=bool(long_term_retention),
        source=source,
        sha256=digest,
        size=size,
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    ).to_dict()


def _index_path(root: os.PathLike[str] | str) -> Path:
    p = Path(root)
    return p if p.suffix.lower() == ".json" else p / DEFAULT_INDEX_NAME


def read_index(root: os.PathLike[str] | str) -> list[dict[str, Any]]:
    path = _index_path(root)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("records") or payload.get("evidence") or []
    return [dict(x) for x in payload if isinstance(x, Mapping)] if isinstance(payload, list) else []


def write_index(root: os.PathLike[str] | str, records: Sequence[Mapping[str, Any]]) -> Path:
    path = _index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "records": list(records)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def register_image(image_path: os.PathLike[str] | str, root: os.PathLike[str] | str, **kwargs: Any) -> dict[str, Any]:
    record = image_metadata(image_path, **kwargs)
    records = read_index(root)
    records = [r for r in records if r.get("evidence_id") != record["evidence_id"]]
    records.append(record)
    write_index(root, records)
    return record


def _iter_metadata(root: Path) -> Iterable[tuple[dict[str, Any], Optional[Path]]]:
    if root.is_file():
        files = [root]
    else:
        files = list(root.rglob("*.json")) if root.exists() else []
    for path in files:
        if path.name == DEFAULT_INDEX_NAME:
            for rec in read_index(path):
                yield rec, path
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidates = payload.get("records", []) if isinstance(payload, Mapping) else payload
        if isinstance(payload, Mapping) and (payload.get("evidence_id") or payload.get("证据编号")):
            candidates = [payload]
        if isinstance(candidates, Mapping):
            candidates = [candidates]
        if isinstance(candidates, list):
            for rec in candidates:
                if isinstance(rec, Mapping) and (rec.get("evidence_id") or rec.get("证据编号") or rec.get("kind") == "image"):
                    yield dict(rec), path


def scan_candidates(records: Iterable[Mapping[str, Any]], *, now: Optional[datetime] = None, retention_days: int = DEFAULT_RETENTION_DAYS, metadata_paths: Optional[Iterable[Optional[Path]]] = None) -> list[dict[str, Any]]:
    """只生成清理候选，不执行任何删除。"""
    now = (now or _utc_now()).astimezone(timezone.utc)
    cutoff = now - timedelta(days=int(retention_days))
    paths = list(metadata_paths or [])
    out = []
    for i, raw in enumerate(records):
        rec = dict(raw)
        created = _record_time(rec, paths[i] if i < len(paths) else None)
        if not created or created >= cutoff or not is_closed(rec) or is_long_term(rec):
            continue
        rec["candidate_reason"] = "超过%d天、已闭环且非长期保留" % retention_days
        rec["age_days"] = (now - created).days
        out.append(rec)
    return out


def scan(root: os.PathLike[str] | str, *, now: Optional[datetime] = None, retention_days: int = DEFAULT_RETENTION_DAYS) -> list[dict[str, Any]]:
    pairs = list(_iter_metadata(Path(root)))
    return scan_candidates((r for r, _ in pairs), now=now, retention_days=retention_days, metadata_paths=(p for _, p in pairs))


def scan_adapter(adapter: Any, table: str, *, now: Optional[datetime] = None, retention_days: int = DEFAULT_RETENTION_DAYS) -> list[dict[str, Any]]:
    """只读扫描 SeaTable/本地适配器中的证据元数据表。

    ``adapter`` 只需实现 ``list_rows(table)``；不会调用 ``auth`` 或任何写入方法。
    这样线上扫描可由上层显式注入已授权适配器，同时测试完全离线。
    """
    records = adapter.list_rows(table)
    return scan_candidates(records, now=now, retention_days=retention_days)


def prune_adapter(adapter: Any, table: str, *, yes: bool = False, now: Optional[datetime] = None, retention_days: int = DEFAULT_RETENTION_DAYS) -> list[dict[str, Any]]:
    """扫描适配器候选；只有 ``yes=True`` 才调用删除接口。"""
    records = list(adapter.list_rows(table))
    candidates = scan_candidates(records, now=now, retention_days=retention_days)
    if yes and candidates:
        ids = [r.get("__row_id__") or r.get("row_id") for r in candidates]
        ids = [x for x in ids if x]
        if ids:
            adapter.delete_rows(table, ids)
    return candidates


def prune(root: os.PathLike[str] | str, *, yes: bool = False, now: Optional[datetime] = None, retention_days: int = DEFAULT_RETENTION_DAYS, delete_files: bool = True) -> list[dict[str, Any]]:
    """删除候选图片及其索引记录。没有 ``yes`` 时绝不删除。"""
    candidates = scan(root, now=now, retention_days=retention_days)
    if not yes or not candidates:
        return candidates
    index = _index_path(root)
    records = read_index(root)
    ids = {r.get("evidence_id") for r in candidates}
    for rec in candidates:
        if delete_files and rec.get("path"):
            try:
                Path(str(rec["path"])).unlink(missing_ok=True)
            except OSError:
                pass
    if index.is_file():
        write_index(root, [r for r in records if r.get("evidence_id") not in ids])
    return candidates


def _print(records: Sequence[Mapping[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(list(records), ensure_ascii=False, indent=2))
    elif records:
        for r in records:
            print("%s | %s | %s | %s" % (r.get("evidence_id", ""), r.get("created_at", ""), r.get("status", ""), r.get("path", "")))
        print("共 %d 条候选" % len(records))
    else:
        print("没有符合清理条件的证据候选。")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="微信图片证据元数据与90天清理（离线）")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("register-image", help="登记图片元数据，不上传图片")
    p.add_argument("image")
    p.add_argument("--root", default=os.path.join("data", "wechat_intake"))
    p.add_argument("--summary", default="")
    p.add_argument("--event-id", default="")
    p.add_argument("--status", default="待处理")
    p.add_argument("--long-term", action="store_true")
    p.add_argument("--json", action="store_true")
    for name in ("scan", "prune"):
        p = sub.add_parser(name, help="扫描候选" if name == "scan" else "扫描并按确认删除候选")
        p.add_argument("--root", default=os.path.join("data", "wechat_intake"))
        p.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
        p.add_argument("--yes", action="store_true", help="确认删除；缺少此参数只扫描不删除")
        p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.command == "register-image":
        rec = register_image(args.image, args.root, summary=args.summary, event_id=args.event_id, status=args.status, long_term_retention=args.long_term)
        _print([rec], args.json)
        return 0
    candidates = scan(args.root, retention_days=args.days)
    if args.command == "prune" and args.yes:
        candidates = prune(args.root, yes=True, retention_days=args.days)
        print("已删除 %d 条证据（本地文件及元数据索引）。" % len(candidates), file=sys.stderr)
    _print(candidates, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
