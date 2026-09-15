# -*- coding: utf-8 -*-
"""SeaTable 适配器（配置驱动，绝不写死 token / UUID）。

所有凭证来自 config.yaml。列名用中文（避开裸 API 的列 key 坑）。
link_id 在写关联时按「两张表名」从 Base metadata 动态解析，不依赖写死的 link_id。
仅依赖 requests（绝大多数环境自带；若缺则用 pip install requests）。
"""
import requests
from .base import BaseAdapter


class SeaTableAdapter(BaseAdapter):
    def __init__(self, api_token: str, server: str, base_uuid: str, base_name: str = None):
        self.token = api_token
        self.server = server.rstrip("/")
        self.uuid = base_uuid
        # Human-readable name is useful for routing/diagnostics and is kept
        # separate from the UUID.  Every adapter owns its own auth + metadata
        # cache, so multiple Bases can safely coexist in one process.
        self.base_name = base_name or "default"
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

    def _cell_meta(self, table: str):
        """返回 (单选/多选列的 {列名: {选项id: 选项名}}, {列名: 列类型})，按表缓存。

        为什么必须有：SeaTable `GET /rows/` 对单选/多选列返回的是**选项 id**
        （如状态 "58668"），日期列返回完整 ISO（"2026-05-07T00:00:00+08:00"）。
        而下游 cockpit.py 是按「本地 CSV 习惯」写的——状态是中文名、日期是
        YYYY-MM-DD。若不翻译就会出现两类静默错误：
          · 状态列显示 "58668"、KPI 在产/计划/已交付计数恒为 0
          · _date() 解析失败 → 甘特图为空、剩余天数为 null、交期达成率 N/A
        seatable_sync.py 走 CSV 路径时已做同样翻译（见其 _flatten），此处补上直连路径。
        """
        if getattr(self, "_cellmeta", None) is None:
            self._cellmeta = {}
        if table not in self._cellmeta:
            sel, types = {}, {}
            try:
                r = requests.get(self._base() + "/columns/", headers=self._h,
                                 params={"table_name": table}, timeout=30)
                r.raise_for_status()
                for c in r.json().get("columns", []):
                    nm = c.get("name")
                    if not nm:
                        continue
                    types[nm] = c.get("type")
                    if c.get("type") in ("single-select", "multiple-select"):
                        opts = (c.get("data") or {}).get("options") or []
                        sel[nm] = {o.get("id"): o.get("name") for o in opts if o.get("id")}
            except Exception:
                pass  # 拿不到列定义就退回原始值，绝不因此让整表读取失败
            self._cellmeta[table] = (sel, types)
        return self._cellmeta[table]

    @staticmethod
    def _flat_cell(v, ctype, selmap):
        """按列类型扁平化单元格：选项 id→选项名、ISO 日期→YYYY-MM-DD。"""
        if isinstance(v, list) and selmap:
            return [selmap.get(x, x) if isinstance(x, str) else x for x in v]
        if isinstance(v, str) and selmap and v in selmap:
            return selmap[v]
        if isinstance(v, str) and ctype in ("date", "ctime", "mtime", "datetime") and "T" in v:
            return v[:10]
        return v

    def list_rows(self, table: str):
        self._table(table)
        key2name = {c["key"]: c["name"] for c in self._table(table)["columns"]}
        sel, types = self._cell_meta(table)
        rows, limit, offset = [], 1000, 0
        while True:
            r = requests.get(self._base() + "/rows/", headers=self._h,
                             params={"table_name": table, "limit": limit, "offset": offset}, timeout=30)
            r.raise_for_status()
            batch = r.json().get("rows", [])
            for row in batch:
                rid = row.get("_id")
                d = {}
                for k, v in row.items():
                    if k in ("_id", "_ctime", "_mtime"):
                        continue
                    nm = key2name.get(k, k)
                    d[nm] = self._flat_cell(v, types.get(nm), sel.get(nm))
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
        # SeaTable add_row 响应：{"inserted_row_count":1,"row_ids":[{"_id":...}],"first_row":{...}}
        if resp.get("row_ids"):
            return resp["row_ids"][0].get("_id")
        if resp.get("first_row"):
            return resp["first_row"].get("_id")
        new = resp.get("rows") or []
        return (new[0].get("_id") if new else None)

    def _row_with_keys(self, table: str, data: dict) -> dict:
        """把写入 dict 的中文列名解析为 SeaTable 列 key。

        ⚠️ 2026-09-01 实测结论：**update 不能用这个方法**。
        PUT /rows/ 传列 key 会返回 `{"success":true}`（HTTP 200）但数据不落库，
        静默失败，极易误以为写入成功。实测传中文列名才生效。
        保留此方法仅供需要列 key 的特殊场景（如未来的批量接口），update_row 已不再调用。
        """
        cols = {c["name"]: c["key"] for c in self._table(table)["columns"]}
        return {cols.get(k, k): v for k, v in data.items() if k != "__row_id__"}

    def update_row(self, table: str, row_id: str, data: dict):
        # SeaTable 更新单行接口：updates 必须是 [{row_id, row:{...}}] 的数组结构。
        # ⚠️ row 必须用**中文列名**：实测传列 key 会返回 success:true 但不落库（静默失败）。
        #    append_row 同理按列名匹配。两个写接口都不要转 key。
        row = {k: v for k, v in data.items() if k != "__row_id__"}
        payload = {
            "table_name": table,
            "updates": [
                {"row_id": row_id, "row": row}
            ],
        }
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
