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

from .base import (CAP_BATCH_WRITE, CAP_DELETE, CAP_LINK, CAP_LINK_READ, CAP_READ,
                   CAP_SCHEMA_MANAGE, CAP_UPDATE, CAP_WRITE, BaseAdapter)
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
    backend = "local"

    # 本地 CSV 后端的能力声明（声明式，不做任何 I/O）：
    #   · 建表/加列是隐式的（首次写入即建），并且提供显式 ensure_table → schema_manage
    #   · link / list_linked 都是真实现（data/__links__.json，双向） → link + link_read
    #   · append_rows 是「读一次 + 写一次」，既快又天然原子 → batch_write
    #   · 不具 query_pushdown：query() 永远拉全表在内存里过滤
    #   · 无 row 版本号 → 不声明 optimistic_lock
    CAPS = frozenset({
        CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE,
        CAP_LINK, CAP_LINK_READ, CAP_BATCH_WRITE, CAP_SCHEMA_MANAGE,
    })

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

    def _read_header(self, table: str) -> List[str]:
        """只读表头（零数据行也能拿到列定义）。

        为什么需要：``get_metadata`` 原先是从**数据行**反推列的，于是
        ``ensure_table`` 刚建出来的空表会返回「零列」—— 与「表不存在」的表现
        完全一样，建表等于白建。空表也应当能报出自己的列。
        """
        path = self._table_path(table)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                head = next(csv.reader(f), None) or []
        except Exception:
            return []
        return [h for h in head if h and h != "__row_id__"]

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
        """列定义 = 表头 ∪ 数据行里出现过的列（表头在前的顺序）。

        先取表头再补数据行：空表（刚 ``ensure_table`` 出来）也能报出列定义。
        """
        cols = list(self._read_header(table))
        for r in self._read_all(table):
            for k in r.keys():
                if k != "__row_id__" and k not in cols:
                    cols.append(k)
        return {
            "table_name": table,
            "columns": [{"name": c, "type": "text"} for c in cols],
        }

    def get_row(self, table: str, row_id: str) -> Optional[Dict[str, Any]]:
        """覆盖默认实现：命中即返回，不必先物化整表。

        ⚠️ 与基类默认实现的语义差异：基类默认「找不到返回 None」，这里同样。
        （基类默认实现会用 ``list_rows`` 拉全表；本地表通常不大，但早退仍然更省。）
        """
        target = str(row_id)
        for r in self._read_all(table):
            if str(r.get("__row_id__", "")) == target:
                return r
        return None

    def table_exists(self, table: str) -> bool:
        """以文件是否存在为准。

        必须覆盖基类默认实现：基类的默认探测是「``get_metadata`` 不抛异常就算存在」，
        而本地模式下**读一张不存在的表只会返回空列、不会抛异常**，于是会把
        「表不存在」误判成「表存在但还没数据」，让建表逻辑整个失效。
        """
        return os.path.exists(self._table_path(table))

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

    def append_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[str]:
        """批量新增：读一次 + 写一次。

        比基类默认的「逐条 append_row」快得多，而且**天然原子** ——
        整表只写一次，不存在「前几条已落盘、后几条失败」的中间态。
        默认值/自动编号的套用逻辑逐行走 ``append_row`` 同一套规则，语义保持一致。
        """
        rows = list(rows or [])
        if not rows:
            return []
        existing = self._read_all(table)
        new_ids: List[str] = []
        for data in rows:
            defaults = schema.merged_defaults(table, self.config)
            merged: Dict[str, Any] = {}
            for k, v in defaults.items():
                merged[k] = _today() if v == "__TODAY__" else v
            merged.update(data)
            for k in list(merged.keys()):
                if schema.AUTO_NUMBER_HINT in k and merged.get(k) in ("", None):
                    merged[k] = self._next_auto(table, k)
            rid = self._next_row_id(table)
            merged["__row_id__"] = rid
            existing.append(merged)
            new_ids.append(rid)
        self._write_all(table, existing)
        return new_ids

    def delete_rows(self, table: str, row_ids: List[str]) -> None:
        rows = self._read_all(table)
        keep = [r for r in rows if r.get("__row_id__") not in set(row_ids)]
        self._write_all(table, keep)

    # ── 关联 ───────────────────────────────────────────
    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: List[str]) -> None:
        """整体替换语义：该行最终只关联到 ``other_row_ids``（与 SeaTable 对齐）。"""
        self._link_write(table, other_table, link_id, row_id, list(other_row_ids or []))

    def link_append(self, table: str, other_table: str, link_id: str,
                    row_id: str, other_row_ids: List[str]) -> List[str]:
        """追加式关联：在既有关系之上新增，保留历史。返回追加后的完整列表。

        本地实现读-改-写是安全的：单进程内没有并发写（``__links__.json`` 一次性
        落盘），不存在丢失更新窗口。远端后端不保证这一点，所以基类默认抛
        Unsupported —— 别把这个实现当成「远端也这么做就行」的许可。
        """
        current = list(self._links.get(link_id, {}).get(table, {}).get(row_id, []))
        merged = current + [x for x in (other_row_ids or []) if x not in current]
        self._link_write(table, other_table, link_id, row_id, merged)
        return merged

    def link_one_way(self, table: str, other_table: str, link_id: str,
                     row_id: str, other_row_ids: List[str]) -> None:
        """只写 ``table`` 一侧，不碰对方表**已有的**关联列表。

        语义严格对齐 SeaTable 的真实行为（只读实测）：往源侧的关联列 PUT 时，
        源侧被整体设置，而对方侧由服务端**增量补齐**（不是整体替换）。所以这里：
          · 源侧 ``table.row_id`` → 设置为 ``other_row_ids``（替换）
          · 对方侧 ``other_table.oid`` → **追加** ``row_id``（保留其历史）
        两边都维护，是为了让本地模式与远端「看起来一样」——否则同一套上层代码
        在 local 上读不到反向关联，又变成一种「换后端就崩」。

        之所以需要这个方法：``link()`` 是双向**整体替换**，拿它做「子记录挂到父记录」
        会把父记录侧的历史关联全部冲掉且不报错（``domain/crm_dispatch.py`` 踩过）。
        """
        other_row_ids = list(other_row_ids or [])
        # 1) 源侧：设置为目标集合
        self._links.setdefault(link_id, {}).setdefault(table, {})[row_id] = other_row_ids
        # 2) 对方侧：**增量补齐**，绝不整体替换
        for oid in other_row_ids:
            rev = self._links[link_id].setdefault(other_table, {})
            rev.setdefault(oid, [])
            if row_id not in rev[oid]:
                rev[oid].append(row_id)
        _save_json(self._links_path, self._links)
        # 3) CSV 镜像：源侧设置为全集；对方侧追加
        col_t = schema.link_col_of(table, link_id)
        if col_t:
            self._patch_row(table, row_id, {col_t: ",".join(other_row_ids)})
        col_o = schema.link_col_of(other_table, link_id)
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
        """``link_id`` 为空表示「该行所有关联列的并集」。"""
        if link_id:
            return list(self._links.get(link_id, {}).get(table, {}).get(row_id, []))
        out: List[str] = []
        for per_table in self._links.values():
            for rid in per_table.get(table, {}).get(row_id, []) or []:
                if rid not in out:
                    out.append(rid)
        return out

    def _link_write(self, table: str, other_table: str, link_id: str,
                    row_id: str, other_row_ids: List[str]) -> None:
        """把「该行关联到 other_row_ids」这一事实双向落盘（JSON + CSV 镜像）。"""
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

    # ── 表结构 ─────────────────────────────────────────
    def ensure_table(self, table: str, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        """确保 ``data/<表名>.csv`` 存在；返回 ``"created"`` / ``"exists"``。

        本地 CSV 的「建表」就是写一个只有表头、零数据行的文件 —— 这样
        ``table_exists`` 立刻为真，且后续 ``append_row`` 不会把表当成不存在。
        """
        if self.table_exists(table):
            return "exists"
        cols: List[str] = []
        for c in (columns or []):
            nm = c.get("name") if isinstance(c, dict) else c
            if nm and nm != "__row_id__" and nm not in cols:
                cols.append(nm)
        self._write_header_only(table, cols)
        return "created"

    def _write_header_only(self, table: str, cols: List[str]) -> None:
        """写一个「表头齐全、零数据行」的 CSV。"""
        path = self._table_path(table)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["__row_id__"] + list(cols))
            w.writeheader()
