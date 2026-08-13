# -*- coding: utf-8 -*-
"""本地 CSV 适配器（默认后端，零配置、离线、无需任何账号）。

每张表存成 data/<表名>.csv，关联存 data/__links__.json，计数器存
data/__counters__.json。仅依赖 Python 标准库，任何装了 Python 的机器都能跑。
对外暴露与 SeaTable 适配器完全一致的接口（见 base.BaseAdapter）。
"""
import csv
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from .base import BaseAdapter
from . import schema

_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class LocalAdapter(BaseAdapter):
    def __init__(self, data_dir: str, config: dict = None):
        self.data_dir = data_dir
        self.config = config or {}
        os.makedirs(self.data_dir, exist_ok=True)
        self._links_path = os.path.join(self.data_dir, "__links__.json")
        self._counters_path = os.path.join(self.data_dir, "__counters__.json")
        self._links = _load_json(self._links_path, {})
        self._counters = _load_json(self._counters_path, {})

    # ── 内部工具 ───────────────────────────────────────
    def _table_path(self, table: str) -> str:
        safe = table.replace("/", "_").replace("\\", "_")
        return os.path.join(self.data_dir, f"{safe}.csv")

    def _read_all(self, table: str) -> List[Dict[str, Any]]:
        """返回含 __row_id__ 的全部行。"""
        path = self._table_path(table)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = []
            for row in reader:
                d = {k: v for k, v in row.items() if k not in (None, "")}
                d["__row_id__"] = d.get("__row_id__") or ""
                rows.append(d)
        return rows

    def _write_all(self, table: str, rows: List[Dict[str, Any]]) -> None:
        cols: List[str] = []
        for r in rows:
            for k in r.keys():
                if k not in cols:
                    cols.append(k)
        # __row_id__ 永远第一列
        if "__row_id__" in cols:
            cols.remove("__row_id__")
        header = ["__row_id__"] + cols
        path = self._table_path(table)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in header})

    def _patch_row(self, table: str, row_id: str, updates: Dict[str, Any]) -> None:
        rows = self._read_all(table)
        for r in rows:
            if r.get("__row_id__") == row_id:
                r.update(updates)
                break
        self._write_all(table, rows)

    def _next_row_id(self, table: str) -> str:
        key = f"row_{table}"
        self._counters[key] = self._counters.get(key, 0) + 1
        _save_json(self._counters_path, self._counters)
        return f"row_{self._counters[key]}"

    def _next_auto(self, table: str, col: str) -> str:
        key = f"auto_{table}_{col}"
        self._counters[key] = self._counters.get(key, 0) + 1
        _save_json(self._counters_path, self._counters)
        return str(self._counters[key])

    # ── 生命周期 ───────────────────────────────────────
    def auth(self) -> None:
        pass  # 本地无需认证

    # ── 读 ─────────────────────────────────────────────
    def list_rows(self, table: str) -> List[Dict[str, Any]]:
        return self._read_all(table)

    def get_metadata(self, table: str) -> Dict[str, Any]:
        rows = self._read_all(table)
        cols = []
        for r in rows:
            for k in r.keys():
                if k != "__row_id__" and k not in cols:
                    cols.append(k)
        return {
            "table_name": table,
            "columns": [{"name": c, "type": "text"} for c in cols],
        }

    # ── 写 ─────────────────────────────────────────────
    def append_row(self, table: str, data: Dict[str, Any]) -> str:
        defaults = schema.merged_defaults(table, self.config)
        merged: Dict[str, Any] = {}
        for k, v in defaults.items():
            merged[k] = _today() if v == "__TODAY__" else v
        merged.update(data)
        # 自动编号列（列名含“编号”且为空 → 递增填充）
        for k in list(merged.keys()):
            if schema.AUTO_NUMBER_HINT in k and merged.get(k) in ("", None):
                merged[k] = self._next_auto(table, k)
        rid = self._next_row_id(table)
        merged["__row_id__"] = rid
        rows = self._read_all(table)
        rows.append(merged)
        self._write_all(table, rows)
        return rid

    def update_row(self, table: str, row_id: str, data: Dict[str, Any]) -> None:
        self._patch_row(table, row_id, {k: v for k, v in data.items() if k != "__row_id__"})

    def delete_rows(self, table: str, row_ids: List[str]) -> None:
        rows = self._read_all(table)
        keep = [r for r in rows if r.get("__row_id__") not in set(row_ids)]
        self._write_all(table, keep)

    # ── 关联 ───────────────────────────────────────────
    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: List[str]) -> None:
        other_row_ids = list(other_row_ids or [])
        # 1) 写入关联 JSON（双向）
        self._links.setdefault(link_id, {}).setdefault(table, {})[row_id] = other_row_ids
        for oid in other_row_ids:
            rev = self._links[link_id].setdefault(other_table, {})
            rev.setdefault(oid, [])
            if row_id not in rev[oid]:
                rev[oid].append(row_id)
        _save_json(self._links_path, self._links)
        # 2) 镜像进 CSV 关联列，方便直接打开查看
        col_t = schema.link_col_of(table, link_id)
        col_o = schema.link_col_of(other_table, link_id)
        if col_t:
            self._patch_row(table, row_id, {col_t: ",".join(other_row_ids)})
        if col_o:
            for oid in other_row_ids:
                cur = self._read_all(other_table)
                for r in cur:
                    if r.get("__row_id__") == oid:
                        exist = [x for x in str(r.get(col_o, "")).split(",") if x]
                        if row_id not in exist:
                            exist.append(row_id)
                        r[col_o] = ",".join(exist)
                self._write_all(other_table, cur)

    def list_linked(self, table: str, row_id: str, link_id: str) -> List[str]:
        return list(self._links.get(link_id, {}).get(table, {}).get(row_id, []))
