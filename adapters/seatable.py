# -*- coding: utf-8 -*-
"""SeaTable 适配器（配置驱动，绝不写死 token / UUID）。

所有凭证来自 config.yaml。列名用中文（避开裸 API 的列 key 坑）。
link_id 在写关联时按「两张表名」从 Base metadata 动态解析，不依赖写死的 link_id。
仅依赖 requests（绝大多数环境自带；若缺则用 pip install requests）。
"""
import requests
from .base import BaseAdapter


class SeaTableAdapter(BaseAdapter):
    def __init__(self, api_token: str, server: str, base_uuid: str):
        self.token = api_token
        self.server = server.rstrip("/")
        self.uuid = base_uuid
        self._access = None
        self._server = None
        self._meta = None

    # ── 生命周期 ──────────────────────────────────
    def auth(self) -> None:
        r = requests.get(
            f"{self.server}/api/v2.1/dtable/app-access-token/",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()
        self._access = d["access_token"]
        self._server = (d.get("dtable_server") or f"{self.server}/api-gateway").rstrip("/")

    @property
    def _h(self):
        return {"Authorization": f"Bearer {self._access}"}

    def _base(self):
        return f"{self._server}/api/v2/dtables/{self.uuid}"

    def _ensure_meta(self):
        if self._meta is None:
            r = requests.get(self._base() + "/metadata/", headers=self._h, timeout=30)
            r.raise_for_status()
            self._meta = r.json()["metadata"]

    def _table(self, name):
        self._ensure_meta()
        t = next((x for x in self._meta["tables"] if x["name"] == name), None)
        if t is None:
            raise KeyError(f"表不存在：{name}")
        return t

    # ── 读 ───────────────────────────────────────
    def get_metadata(self, table: str):
        t = self._table(table)
        return {
            "table_name": table,
            "columns": [{"name": c["name"], "type": c["type"], "key": c["key"]} for c in t["columns"]],
        }

    def list_rows(self, table: str):
        self._table(table)
        key2name = {c["key"]: c["name"] for c in self._table(table)["columns"]}
        rows, limit, offset = [], 1000, 0
        while True:
            r = requests.get(self._base() + "/rows/", headers=self._h,
                             params={"table_name": table, "limit": limit, "offset": offset}, timeout=30)
            r.raise_for_status()
            batch = r.json().get("rows", [])
            for row in batch:
                rid = row.get("_id")
                d = {key2name.get(k, k): v for k, v in row.items() if k not in ("_id", "_ctime", "_mtime")}
                d["__row_id__"] = rid
                rows.append(d)
            if len(batch) < limit:
                break
            offset += limit
        return rows

    # ── 写 ───────────────────────────────────────
    def append_row(self, table: str, data: dict):
        payload = {"table_name": table, "rows": [{k: v for k, v in data.items() if k != "__row_id__"}]}
        r = requests.post(self._base() + "/rows/", headers={**self._h, "Content-Type": "application/json"},
                         json=payload, timeout=30)
        r.raise_for_status()
        resp = r.json()
        new = resp.get("rows") or []
        return (new[0].get("_id") if new else None)

    def update_row(self, table: str, row_id: str, data: dict):
        # SeaTable 更新单行接口字段名为 updates（非 row）
        payload = {"table_name": table, "row_id": row_id,
                   "updates": {k: v for k, v in data.items() if k != "__row_id__"}}
        r = requests.put(self._base() + "/rows/", headers={**self._h, "Content-Type": "application/json"},
                        json=payload, timeout=30)
        r.raise_for_status()

    def delete_rows(self, table: str, row_ids: list):
        r = requests.delete(self._base() + "/rows/", headers={**self._h, "Content-Type": "application/json"},
                           json={"table_name": table, "row_ids": list(row_ids)}, timeout=30)
        r.raise_for_status()

    # ── 关联 ─────────────────────────────────────
    def _resolve_link_id(self, table: str, other: str):
        self._ensure_meta()
        t1 = self._table(table)
        t2 = self._table(other)
        for ln in self._meta.get("links", []):
            if {ln.get("table1_id"), ln.get("table2_id")} == {t1["_id"], t2["_id"]}:
                return ln["link_id"]
        raise KeyError(f"未在 Base 中找到 {table} ↔ {other} 的关联列，请先在两表间建立 link")

    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: list) -> None:
        # 优先用调用方传入的 link_id；为空则按表名解析
        lid = link_id or self._resolve_link_id(table, other_table)
        for direction in ((table, other_table), (other_table, table)):
            src, dst = direction
            r = requests.put(self._base() + "/links/", headers={**self._h, "Content-Type": "application/json"},
                             json={"link_id": lid, "table_name": src, "other_table_name": dst,
                                   "row_id_list": [row_id if src == table else o for o in
                                                  ([row_id] if src == table else other_row_ids)],
                                   "other_rows_ids_map": {row_id: list(other_row_ids)} if src == table
                                   else {o: [row_id] for o in other_row_ids}},
                             timeout=30)
            r.raise_for_status()

    def list_linked(self, table: str, row_id: str, link_id: str):
        # SeaTable 读关联需回查对方表，这里用 query 近似：从对方表反向找
        # 简化实现：返回空（如需精确，可在 SKILL 流程里用 list_rows + 过滤替代）
        return []
