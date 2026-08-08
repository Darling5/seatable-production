# -*- coding: utf-8 -*-
"""后端适配器抽象接口。

SKILL.md 只依赖这个接口，不关心底层是本地文件还是 SeaTable。
所有方法返回/接受的都是「中文列名 → 值」的字典，行标识统一叫 row_id。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseAdapter(ABC):
    # ── 生命周期 ──────────────────────────────────────────
    @abstractmethod
    def auth(self) -> None:
        """建立/刷新连接（local 模式为空操作）。"""

    # ── 读 ────────────────────────────────────────────────
    @abstractmethod
    def list_rows(self, table: str) -> List[Dict[str, Any]]:
        """返回某表全部行，每行是 {中文列名: 值, '__row_id__': row_id}。"""

    @abstractmethod
    def get_metadata(self, table: str) -> Dict[str, Any]:
        """返回表结构：{"table_name":..., "columns":[{"name":..., "type":...}]}。"""

    # ── 写 ────────────────────────────────────────────────
    @abstractmethod
    def append_row(self, table: str, data: Dict[str, Any]) -> str:
        """新增一行，返回新行 row_id（已自动套默认值/跳过自动列）。"""

    @abstractmethod
    def update_row(self, table: str, row_id: str, data: Dict[str, Any]) -> None:
        """更新指定行（只传要改的字段）。"""

    @abstractmethod
    def delete_rows(self, table: str, row_ids: List[str]) -> None:
        """删除多行。"""

    # ── 关联 ──────────────────────────────────────────────
    @abstractmethod
    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: List[str]) -> None:
        """建立双向关联（同一 link_id，两张表各记一次）。"""

    @abstractmethod
    def list_linked(self, table: str, row_id: str, link_id: str) -> List[str]:
        """返回某行在某 link_id 上关联到的对方 row_id 列表。"""

    # ── 便捷查询 ──────────────────────────────────────────
    def query(self, table: str, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """简单等值/包含过滤，filters={列名: 值}。"""
        rows = self.list_rows(table)
        if not filters:
            return rows
        out = []
        for r in rows:
            ok = True
            for k, v in filters.items():
                cur = r.get(k, "")
                if v is None:
                    if cur not in ("", None, []):
                        ok = False
                elif isinstance(v, str) and v.startswith("*"):
                    if v[1:] not in str(cur):
                        ok = False
                elif str(cur) != str(v):
                    ok = False
            if ok:
                out.append(r)
        return out
