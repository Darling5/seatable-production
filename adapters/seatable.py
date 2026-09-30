# -*- coding: utf-8 -*-
"""SeaTable 适配器（配置驱动，绝不写死 token / UUID）。

所有凭证来自 config.yaml。列名用中文（避开裸 API 的列 key 坑）。
仅依赖 requests（绝大多数环境自带；若缺则用 pip install requests）。

本文件的关键实现全部经过**只读实测**验证，不依赖记忆中的 API 文档
（探测脚本与结论见 2026-09-30 会话记录）：
  · ``metadata`` 顶层只有 ``format_version`` / ``tables`` / ``version``，
    **没有 ``links`` 键** —— 所以「按表名反查 link_id」不能读它，必须扫
    ``columns[].data.link_id``（旧实现在这里读了 ``meta["links"]``，恒为空列表，
    于是 ``_resolve_link_id`` 永远抛 KeyError，是一条从未被走通的死路）。
  · ``GET /links/`` → **405**，读不到关联。
  · ``GET /rows/{row_id}/?table_name=`` → **200**，返回**裸行对象**（不带 ``rows``
    包装、含 ``_id``），是单行读取的正确入口；比 ``GET /rows/`` 拉全表便宜得多。
  · 关联列在行数据里回传为 ``[{"row_id": ..., "display_value": ...}]`` ——
    这是**读关联的唯一可用入口**，``list_linked`` 就建在它上面。
  · ``POST /links/`` 存在（空 body 回 400 ``require table_id or table_name``，
    不是 405）；但它「追加还是覆盖」的语义无法在不写入的前提下确证，见
    ``link_append`` 的说明。
"""
import time

import requests

from . import schema
from .base import (CAP_BATCH_WRITE, CAP_DELETE, CAP_LINK, CAP_LINK_READ, CAP_READ,
                   CAP_SCHEMA_MANAGE, CAP_SERVER_ROW_ID, CAP_UPDATE, CAP_WRITE,
                   BaseAdapter)

#: 单次 ``POST /rows/`` 的最大行数。官方上限 1000，这里取 500 留余量 ——
#: 批量接口是「全成功或全失败」语义，批越小、失败时要重放的范围越小。
_BATCH_LIMIT = 500

#: 行级内部键：``GET /rows/`` 会混在行里回传，不映射成业务列。
#: 注意 ``_ctime`` / ``_mtime`` 是**例外** —— 它们既可能是内部时间戳、也可能是
#: 用户真的建了「创建时间/修改时间」类型的列，需按列定义判断，见 ``_translate``。
_ROW_META_KEYS = {"_id", "_creator", "_last_modifier", "_archived", "_locked", "_locked_by"}


class SeaTableAdapter(BaseAdapter):
    backend = "seatable"

    #: 能力声明（声明式，不联网）：
    #:   · batch_write     —— POST /rows/ 原生批量
    #:   · schema_manage   —— POST /tables/ 能建表
    #:   · link / link_read —— link() 与 list_linked() 都是真实现（不再是空桩）
    #:   · server_row_id   —— row_id 是服务端生成的 UUID，客户端不可指定
    #:   · 无 idempotent：没有幂等键，重试即重复写（写入路径必须靠 DataService 的
    #:     读回验证 + operation_key 兜）
    #:   · 无 optimistic_lock：没有行版本号 → version_of() 返回 None
    #:   · 无 query_pushdown：query() 仍是拉全表后内存过滤
    CAPS = frozenset({
        CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE,
        CAP_LINK, CAP_LINK_READ, CAP_BATCH_WRITE, CAP_SCHEMA_MANAGE, CAP_SERVER_ROW_ID,
    })

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
        self._cellmeta = None
        self._linkidx = None
        self._linkmap = None

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

    def close(self) -> None:
        """SeaTable 用的是 access token，无服务端会话要关；只清本地缓存。"""
        self._access = None
        self._meta = None
        self._cellmeta = None
        self._linkidx = None
        self._linkmap = None

    @staticmethod
    def _raise_on_body_error(resp, what: str) -> dict:
        """SeaTable 存在「HTTP 200 但 body 里带错误」的响应，必须显式识别。

        为什么：本仓库实打实踩过 ``PUT /rows/`` 传列 key 时返回
        ``{"success": true}`` HTTP 200 却**不落库**的静默失败，写入路径因此
        才加了「读回验证」。新写的接口一律在 raise_for_status 之后再查一遍 body。
        """
        try:
            body = resp.json()
        except Exception:
            return {}
        if isinstance(body, dict):
            err = body.get("error_message") or body.get("error")
            if err:
                raise RuntimeError("%s 被服务端拒绝：%s" % (what, err))
            if body.get("success") is False:
                raise RuntimeError("%s 返回 success=false：%s" % (what, body))
        return body if isinstance(body, dict) else {}

    # ── 读 ───────────────────────────────────────
    def get_metadata(self, table: str):
        t = self._table(table)
        return {
            "table_name": table,
            "columns": [{"name": c["name"], "type": c["type"], "key": c["key"]} for c in t["columns"]],
        }

    def table_exists(self, table: str) -> bool:
        """按 metadata 判断，不抛异常（表不存在时返回 False）。"""
        try:
            self._ensure_meta()
        except Exception:
            return False
        return any(x.get("name") == table for x in self._meta.get("tables", []))

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
        if self._cellmeta is None:
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
                    # 大小写不敏感 —— 同 `_link_index()` 的理由：真实 Base 的类型字符串混用大小写。
                    # 实测（只读）`工时记录.开始时间/结束时间` 是 `DATE`、还有 `LONG_TEXT`/`NUMBER`。
                    if str(c.get("type") or "").lower() in ("single-select", "multiple-select"):
                        opts = (c.get("data") or {}).get("options") or []
                        sel[nm] = {o.get("id"): o.get("name") for o in opts if o.get("id")}
            except Exception:
                pass  # 拿不到列定义就退回原始值，绝不因此让整表读取失败
            self._cellmeta[table] = (sel, types)
        return self._cellmeta[table]

    @staticmethod
    def _flat_cell(v, ctype, selmap):
        """按列类型扁平化单元格：选项 id→选项名、ISO 日期→YYYY-MM-DD。

        ⚠️ 类型比较一律**大小写不敏感**：真实 Base 里类型字符串混用大小写（只读实测：
        `link` 36 列但 `LINK` 4 列；`date` 40 列但 **`DATE` 2 列** ——
        `工时记录.开始时间` / `结束时间`，另有 `LONG_TEXT` 2 列、`NUMBER` 1 列）。
        精确比较会让这些列**静默漏过扁平化**：日期留着完整 ISO
        （`2026-05-07T00:00:00+08:00`），而下游是按 `YYYY-MM-DD` 写的 ——
        正是本文件 `_cell_meta` docstring 里记的那类静默错误。
        """
        if isinstance(v, list) and selmap:
            return [selmap.get(x, x) if isinstance(x, str) else x for x in v]
        if isinstance(v, str) and selmap and v in selmap:
            return selmap[v]
        if (isinstance(v, str) and "T" in v
                and str(ctype or "").lower() in ("date", "ctime", "mtime", "datetime")):
            return v[:10]
        return v

    def _translate(self, table: str, row: dict, key2name: dict, sel: dict, types: dict) -> dict:
        """把一行的「列 key → 原始值」翻译成「中文列名 → 已扁平化值」+ ``__row_id__``。"""
        d = {}
        for k, v in row.items():
            if k == "_id":
                continue
            # ⚠️ SeaTable 的「创建时间 / 修改时间」类列（type = ctime / mtime）**不按列 key 回传**，
            #    而是作为**行级** `_ctime` / `_mtime` 给出（这类列的 key 恰好就是 `_ctime` / `_mtime`）。
            #    生产计划表的「交货时间（自动记录）」正是一条 **mtime 列** ——
            #    `foresee.py` 算历史工期分位（→ 风险雷达）就靠它。
            #    早先这里无条件 skip 掉 `_ctime`/`_mtime`，于是**直连路径永远读不到这两列**
            #    （CSV 路径有，因为导出接口会带上真实列名），foresee 的历史分位会**静默全空**。
            #    修法：只要该 key 在列表定义里对应一个业务列，就照常映射回列名；
            #    没定义才当纯内部时间戳丢弃。
            if k in ("_ctime", "_mtime") and k not in key2name:
                continue
            if k in _ROW_META_KEYS:
                continue
            nm = key2name.get(k, k)
            d[nm] = self._flat_cell(v, types.get(nm), sel.get(nm))
        d["__row_id__"] = row.get("_id")
        return d

    def list_rows(self, table: str):
        t = self._table(table)
        key2name = {c["key"]: c["name"] for c in t["columns"]}
        sel, types = self._cell_meta(table)
        rows, limit, offset = [], 1000, 0
        while True:
            r = requests.get(self._base() + "/rows/", headers=self._h,
                             params={"table_name": table, "limit": limit, "offset": offset}, timeout=30)
            r.raise_for_status()
            batch = r.json().get("rows", [])
            for row in batch:
                rows.append(self._translate(table, row, key2name, sel, types))
            if len(batch) < limit:
                break
            offset += limit
        return rows

    def _raw_row(self, table: str, row_id: str) -> dict:
        """单行原始读取（已验证：``GET /rows/{row_id}/`` 返回裸行对象）。

        行不存在时抛 ``KeyError``（调用方按契约转成 None）。先 ``_table()`` 是为了
        把「表不存在」和「行不存在」区分开 —— 两者都回 404，混在一起会让人查错方向。
        """
        self._table(table)
        r = requests.get(self._base() + f"/rows/{row_id}/", headers=self._h,
                         params={"table_name": table}, timeout=30)
        if r.status_code == 404:
            raise KeyError(f"行不存在：表「{table}」row_id={row_id}")
        r.raise_for_status()
        return r.json()

    def get_row(self, table: str, row_id: str):
        """覆盖基类默认实现：走单行接口，不再拉全表。

        契约与基类一致：**找不到返回 None，不抛异常**。
        """
        t = self._table(table)
        key2name = {c["key"]: c["name"] for c in t["columns"]}
        sel, types = self._cell_meta(table)
        try:
            raw = self._raw_row(table, row_id)
        except KeyError:
            return None
        return self._translate(table, raw, key2name, sel, types)

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

    def append_rows(self, table: str, rows: list):
        """服务端批量新增（覆盖基类的「逐条循环」兜底）。

        返回与入参**同序等长**的 row_id 列表；服务端没回每个 id 的位置填 ``None``
        （宁可为 None 也不要错位 —— 错位会让调用方把 A 的 id 记到 B 头上）。
        """
        self._table(table)
        rows = [{k: v for k, v in (r or {}).items() if k != "__row_id__"} for r in (rows or [])]
        ids = []
        for i in range(0, len(rows), _BATCH_LIMIT):
            chunk = rows[i:i + _BATCH_LIMIT]
            resp = requests.post(
                self._base() + "/rows/",
                headers={**self._h, "Content-Type": "application/json"},
                json={"table_name": table, "rows": chunk}, timeout=60)
            resp.raise_for_status()
            body = self._raise_on_body_error(resp, "批量新增（表 %s）" % table)
            got = [x.get("_id") for x in (body.get("row_ids") or [])]
            ids.extend(got if len(got) == len(chunk) else [None] * len(chunk))
        return ids

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

    # ── 表结构 ───────────────────────────────────
    def ensure_table(self, table: str, columns=None) -> str:
        """确保表存在；返回 ``"created"`` / ``"exists"``。

        ``columns`` 用中立形态 ``{"name":..., "type":...}``；类型经
        ``schema.backend_type("seatable", ...)`` 翻译成 SeaTable 原生类型。
        兼容直接传 ``{"column_name":..., "column_type":...}`` 的旧形态
        （``workflows/loop_sync.py`` 历史上就是这么拼的）。
        """
        if self.table_exists(table):
            return "exists"
        cols = []
        for c in (columns or []):
            if isinstance(c, dict):
                nm = c.get("name") or c.get("column_name")
                ct = c.get("type") or c.get("column_type") or "text"
            else:
                nm, ct = c, "text"
            if not nm:
                continue
            cols.append({"column_name": nm,
                         "column_type": schema.backend_type(self.backend, ct)})
        r = requests.post(self._base() + "/tables/",
                          headers={**self._h, "Content-Type": "application/json"},
                          json={"table_name": table, "columns": cols}, timeout=30)
        r.raise_for_status()
        self._raise_on_body_error(r, "建表 %s" % table)
        # 建表会改 metadata → 四份缓存全部失效，下次用时重拉
        self._meta = None
        self._cellmeta = None
        self._linkidx = None
        self._linkmap = None
        return "created"

    # ── 关联 ─────────────────────────────────────
    def _link_index(self) -> dict:
        """``{表名: [{column, key, link_id, other, multiple}, ...]}``，按 metadata 缓存。

        ⚠️ 这是**唯一**能拿到 link_id 的地方。``metadata`` 顶层没有 ``links`` 键
        （只读实测：顶层仅 ``format_version`` / ``tables`` / ``version``），
        link_id 藏在每个 link 列的 ``data.link_id`` 里。
        """
        if self._linkidx is None:
            self._ensure_meta()
            id2name = {t["_id"]: t["name"] for t in self._meta["tables"]}
            idx = {}
            for t in self._meta["tables"]:
                items = []
                for c in t.get("columns", []):
                    # ⚠️ 必须**大小写不敏感**比较。真实 Base 里 link 列的类型字符串**混用大小写**：
                    #    只读实测 production Base —— `type == "link"` 36 列，`type == "LINK"` 4 列
                    #    （`生产计划.工时记录` / `生产工序.工时记录` / `工时记录.关联生产计划` /
                    #     `工时记录.关联工序`，link_id = W1Q1 / Hh3j），另有 9 个 `link-formula`
                    #    （**没有 link_id**，不是可真写的关联列，必须继续排除）。
                    #    早先这里是精确比较 `!= "link"` → 那 4 个大写列被**静默跳过**，后果：
                    #      · `link_columns("工时记录")` 返回 **[]**（真表有 2 个关联列）
                    #      · `known_link_ids()` 只有 18 个（真实 20 个）
                    #      · 最糟的是 `_assert_link_id_known()` 会**谎报** W1Q1 / Hh3j
                    #        「在本 Base 中不存在」—— 一个守卫反过来拦截合法 link_id
                    #    这与 `list_linked` 空桩是同一类「静默失败」，故一并修掉。
                    if str(c.get("type") or "").lower() != "link":
                        continue
                    dd = c.get("data") or {}
                    items.append({
                        "column": c.get("name"),
                        "key": c.get("key"),
                        "link_id": dd.get("link_id"),
                        "other": id2name.get(dd.get("other_table_id")),
                        "multiple": bool(dd.get("is_multiple")),
                    })
                idx[t["name"]] = items
            self._linkidx = idx
        return self._linkidx

    def link_map(self) -> dict:
        """``{link_id: {表名: 该表上的列名}}``。

        为什么按「表名集合」而不是 ``data.other_table_id`` 判定：SeaTable 的双向关联
        在**两张表上各建一个同 link_id 的列**（只读实测：``3Fld`` 同时出现在
        ``生产计划.关联项目`` 和 ``项目.生产计划``）。而反向列的 ``data.other_table_id``
        实测并不稳定地指向对方表 —— 拿它判定会漏掉候选，进而把「两条关联」误判成
        「唯一一条」，悄悄关联到另一条业务线上。用「这个 link_id 覆盖了哪两张表」
        判定就与响应细节无关了。
        """
        if self._linkmap is None:
            m = {}
            for tname, items in self._link_index().items():
                for c in items:
                    if c.get("link_id"):
                        m.setdefault(c["link_id"], {})[tname] = c["column"]
            self._linkmap = m
        return self._linkmap

    def link_columns(self, table: str) -> list:
        """某表上的全部关联列。表不存在时抛 ``KeyError``。"""
        self._table(table)          # 先确认表存在（拿真实报错，而不是空列表）
        return list(self._link_index().get(table, []))

    def known_link_ids(self) -> set:
        return set(self.link_map())

    def _resolve_link_id(self, table: str, other: str, column: str = None) -> str:
        """按「两张表名」反查真实 link_id。

        同两张表之间可能存在**多条**关联（实测：项目 ↔ 生产计划 有两条 ——
        ``3Fld`` 与 ``wana``，且两条在两张表上各有列）。此时不静默取第一条，
        而是要求 ``column`` 指定「该表上的列名」；不给就抛错并列出候选，
        避免「关联到了另一条业务线上」这种最难查的错。
        """
        self._table(table)
        self._table(other)
        m = self.link_map()
        cands = [lid for lid, per_table in m.items() if table in per_table and other in per_table]
        if not cands:
            raise KeyError(
                "没有连接「%s」与「%s」的关联列；「%s」上的关联列有：%s"
                % (table, other, table,
                   ["%s(→%s)" % (c["column"], c["link_id"]) for c in self.link_columns(table)] or "无"))
        if column:
            hit = [lid for lid in cands if m[lid].get(table) == column]
            if not hit:
                raise KeyError("表「%s」上没有名为「%s」的关联列（候选：%s）"
                               % (table, column,
                                  ["%s(%s)" % (m[lid][table], lid) for lid in cands]))
            return hit[0]
        if len(cands) == 1:
            return cands[0]
        raise KeyError(
            "表「%s」与「%s」之间有 %d 条关联列（%s），无法自动判定 —— "
            "请用 column= 指定「%s」侧的列名，或直接传明确的 link_id"
            % (table, other, len(cands),
               ", ".join("%s.→%s" % (m[lid].get(table), lid) for lid in cands), table))

    def _assert_link_id_known(self, link_id: str) -> None:
        """调用方传进来的 link_id 必须在本 Base 真实存在。

        为什么要显式校验：本仓库 ``adapters/schema.py`` 的 LINKS 表里写着
        **硬编码的 SeaTable link_id 字面量**，其中 ``PlAl`` / ``RsAl`` 在当前
        Base 里**并不存在**（只读实测：真实 link_id 只有 18 个）。拿不存在的
        link_id 去 PUT /links/ 会返回一个语焉不详的错误；显式抛错能直接指出
        「是调用方传了个过期的 id」，而不是让人去猜是权限还是表结构问题。
        """
        if link_id in self.known_link_ids():
            return
        raise KeyError(
            "link_id「%s」在本 Base 中不存在（现有：%s）—— "
            "多半是调用方用了过期的硬编码值，请改为按表名解析或更新配置"
            % (link_id, ",".join(sorted(self.known_link_ids()))))

    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: list) -> None:
        """**双向整体替换**关联（两表各写一次）。

        优先用调用方传入的 link_id（校验必须真实存在）；为空则按表名解析。
        """
        lid = link_id or self._resolve_link_id(table, other_table)
        self._assert_link_id_known(lid)
        self._link_put(lid, table, other_table, row_id, list(other_row_ids or []),
                       bidirectional=True)

    def link_one_way(self, table: str, other_table: str, link_id: str,
                     row_id: str, other_row_ids: list) -> None:
        """只写 ``table`` 侧的关联列，不碰对方表。

        覆盖基类的 ``Unsupported``：``domain/crm_dispatch.py`` 需要「给新建的
        跟进记录挂上它的客户线索，但**不许**动线索那侧的关联列表」—— 线索侧的
        关联列表一旦被整体替换，历史跟进记录就全断了（2026-09-11 实测踩坑）。
        """
        lid = link_id or self._resolve_link_id(table, other_table)
        self._assert_link_id_known(lid)
        self._link_put(lid, table, other_table, row_id, list(other_row_ids or []),
                       bidirectional=False)

    def _link_put(self, link_id: str, table: str, other_table: str,
                  row_id: str, other_row_ids: list, bidirectional: bool) -> None:
        directions = [(table, other_table)]
        if bidirectional:
            directions.append((other_table, table))
        for src, dst in directions:
            if src == table:
                row_id_list = [row_id]
                id_map = {row_id: other_row_ids}
            else:
                row_id_list = list(other_row_ids)
                id_map = {o: [row_id] for o in other_row_ids}
            r = requests.put(self._base() + "/links/",
                             headers={**self._h, "Content-Type": "application/json"},
                             json={"link_id": link_id, "table_name": src, "other_table_name": dst,
                                   "row_id_list": row_id_list, "other_rows_ids_map": id_map},
                             timeout=30)
            r.raise_for_status()
            self._raise_on_body_error(r, "写关联（%s ↔ %s）" % (src, dst))

    def list_linked(self, table: str, row_id: str, link_id: str) -> list:
        """真实现（替换掉历史上的 ``return []`` 空桩）。

        实现依据（只读实测）：关联列在行数据里回传为
        ``[{"row_id": ..., "display_value": ...}]``，所以「读关联」= 读该行 +
        按 link_id 找到列 + 抽取 row_id。``GET /links/`` 是 405，走不通。

        ``link_id`` 传空字符串 → 返回该行在**所有**关联列上的对方 row_id 并集。
        表/行/link_id 任一不存在都显式抛错 —— 空列表**只**代表「确实没有关联」。
        """
        cols = self.link_columns(table)
        if link_id:
            picked = [c for c in cols if c.get("link_id") == link_id]
            if not picked:
                raise KeyError("表「%s」上没有 link_id=%s 的关联列（现有：%s）"
                               % (table, link_id, [c["link_id"] for c in cols] or "无"))
        else:
            picked = cols
        if not picked:
            return []
        raw = self._raw_row(table, row_id)
        out = []
        for c in picked:
            for rid in _row_ids_of(raw.get(c["key"])):
                if rid not in out:
                    out.append(rid)
        return out

    def link_append(self, table: str, other_table: str, link_id: str,
                    row_id: str, other_row_ids: list) -> list:
        """**追加式**关联：保留历史，在读回验证通过后返回完整列表。

        实现方式（读-改-写）与为什么不直接用 ``POST /links/``：

        ``POST /links/`` 这条路确实存在（空 body 回 400
        ``require table_id or table_name``，不是 405），字段名 ``link_id`` /
        ``table_name`` / ``other_table_name`` 也已探明（伪 link_id 得到
        ``link for ZZZZ not found``，同时证明该探测零副作用）。**但它到底是
        「追加」还是「把该行关联设置成这一个」，无法在不向 Base 真实写入的前提下
        确证**。猜错的后果是：逐条 POST 之后只留下最后一条 —— 既有的历史关联被
        静默冲掉，正是本方法要消灭的那类 bug。所以这里选择**已验证的读 + 已验证
        的写**（``list_linked`` + ``link``），并加读回验证。

        ⚠️ 已知局限：读-改-写**不是并发安全的**。两个进程同时给同一行追加关联时，
        后写的一方会覆盖先写的一方（丢失更新）。SeaTable 没有行版本号、也没有
        条件写，这一点无法在适配器层消除；调用方若有并发写场景需自行串行化。
        这也是基类默认实现直接抛 ``Unsupported`` 而不是偷偷 RMW 的原因。
        """
        lid = link_id or self._resolve_link_id(table, other_table)
        self._assert_link_id_known(lid)
        current = self.list_linked(table, row_id, lid)
        merged = current + [x for x in (other_row_ids or []) if x not in current]
        if merged == current:
            return current
        self.link(table, other_table, lid, row_id, merged)
        # 读回验证：写入后必须能读到完整并集。SeaTable 是强一致读，
        # 但保留一次重试以吸收偶发抖动；仍不一致就抛错，绝不静默返回。
        for attempt in range(2):
            back = self.list_linked(table, row_id, lid)
            missing = [x for x in merged if x not in back]
            if not missing:
                return back
            if attempt == 0:
                time.sleep(0.3)
        raise RuntimeError(
            "link_append 读回验证失败：期望关联 %d 条，实际读到 %d 条，缺失 %s"
            % (len(merged), len(back), missing))


def _row_ids_of(value) -> list:
    """从关联列的取值里抽出 row_id 列表。

    容忍三种回传形态：``[{"row_id":...}]``（实测）、``["rowid"]``、``"rowid"``，
    以及 None/空。没有这一层容错，SeaTable 换个响应形态就会让读关联静默变空。
    """
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        return [value]
    out = []
    for x in value:
        if isinstance(x, dict):
            rid = x.get("row_id") or x.get("_id")
        else:
            rid = x
        if rid:
            out.append(rid)
    return out
