# -*- coding: utf-8 -*-
"""流水线公共层：配置、PartDB/SeaTable 客户端、工件读写。

凭证只从技能根目录 config.yaml 读，脚本内不写死任何 token/UUID/IP。
所有中间产物落 pipeline/out/<run_id>/，每一步可单独重跑、可人工改后续跑。
"""
import json
import os
import re
import sys
from datetime import date

import requests
import yaml

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE_DIR = os.path.join(SKILL_DIR, "pipeline")
OUT_DIR = os.path.join(PIPE_DIR, "out")


# ── 配置 ────────────────────────────────────────────
def load_cfg():
    p = os.path.join(SKILL_DIR, "config.yaml")
    if not os.path.exists(p):
        die("找不到 config.yaml，请先配置 partdb 与 seatable 凭证")
    return yaml.safe_load(open(p, encoding="utf-8")) or {}


def load_rules():
    p = os.path.join(PIPE_DIR, "rules.yaml")
    return yaml.safe_load(open(p, encoding="utf-8")) or {}


def die(msg, code=1):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


# ── 运行目录 / 工件 ──────────────────────────────────
def run_dir(run_id, create=True):
    d = os.path.join(OUT_DIR, run_id)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def save(run_id, name, obj):
    d = run_dir(run_id)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return p


def load(run_id, name):
    p = os.path.join(run_dir(run_id, create=False), name)
    if not os.path.exists(p):
        die(f"缺少上一步产物 {name}（路径 {p}），请先跑对应步骤")
    return json.load(open(p, encoding="utf-8"))


def save_csv(run_id, name, rows, cols):
    import csv
    p = os.path.join(run_dir(run_id), name)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return p


# ── PartDB ─────────────────────────────────────────
class PartDB:
    """Hydra(JSON-LD) API。批量列表不可靠，故库存以逐个零件详情为准。"""

    def __init__(self, cfg):
        p = (cfg or {}).get("partdb") or {}
        if not p.get("enabled") or not p.get("url") or not p.get("token"):
            die("config.yaml 未启用 partdb 或缺 url/token，无法做库存核对")
        self.url = p["url"].rstrip("/")
        self.h = {"Authorization": f"Token {p['token']}"}

    def _get(self, path, **params):
        r = requests.get(f"{self.url}{path}", params=params or None,
                         headers=self.h, timeout=40)
        r.raise_for_status()
        return r.json()

    def all_parts(self):
        """全量零件。必须翻页到 member 为空，不能信 hydra:totalItems（会截断）。"""
        out, page = [], 1
        while True:
            d = self._get("/parts", limit=100, page=page)
            batch = d.get("hydra:member", [])
            if not batch:
                break
            out.extend(batch)
            page += 1
            if page > 100:  # 防御性上限
                break
        return out

    def get_part(self, pid):
        return self._get(f"/parts/{pid}")

    def part_lots(self, pid):
        """零件的库存批次。优先用详情内嵌，缺失则回查 part_lots。"""
        d = self.get_part(pid)
        lots = d.get("partLots") or d.get("part_lots") or []
        if lots and isinstance(lots[0], str):  # 只给了 IRI，需逐个取
            real = []
            for iri in lots:
                try:
                    real.append(self._get(iri.replace("/api", "", 1)
                                          if iri.startswith("/api") else iri))
                except Exception:
                    pass
            lots = real
        return d, lots


# 批次「已确认」判定：description 含 MDD / MMDD 形式的确认日期。
# 实库示例为 603、512、0603，不带字母 M；需校验月/日，避免把普通数字误认日期。
CONFIRM_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")


def lot_confirmed(lot):
    desc = str(lot.get("description") or "")
    for token in CONFIRM_RE.findall(desc):
        digits = token.zfill(4)
        month, day = int(digits[:2]), int(digits[2:])
        if 1 <= month <= 12 and 1 <= day <= 31:
            return True
    return False


def split_stock(lots):
    """返回 (已确认库存, 未确认库存, 位置列表)。未确认不得用于生产承诺。"""
    ok = unk = 0.0
    locs = []
    for lot in lots:
        try:
            amt = float(lot.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        loc = lot.get("storage_location")
        if isinstance(loc, dict):
            locs.append(loc.get("name") or "")
        if lot_confirmed(lot):
            ok += amt
        else:
            unk += amt
    return ok, unk, [x for x in locs if x]


def part_price(part):
    """取该零件最低有效单价及供应商。price_per_unit 是派生字段，只读取不写。"""
    best = None
    for od in part.get("orderdetails") or []:
        sup = od.get("supplier") or {}
        sname = sup.get("name") if isinstance(sup, dict) else None
        for pd in od.get("pricedetails") or []:
            try:
                v = float(pd.get("price_per_unit") or 0)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
            if best is None or v < best[0]:
                best = (v, sname, od.get("supplierpartnr"))
    if not best:
        return None, None, None
    return best


# ── SeaTable ───────────────────────────────────────
class SeaTable:
    """写入一律用中文列名：GET 返回的列 key（如 AK7C）不能用于写入。"""

    def __init__(self, cfg):
        s = (cfg or {}).get("seatable") or {}
        if not s.get("api_token") or not s.get("base_uuid"):
            die("config.yaml 缺 seatable.api_token / base_uuid")
        self.server = (s.get("server") or "https://cloud.seatable.cn").rstrip("/")
        self.token = s["api_token"]
        self.uuid = s["base_uuid"]
        r = requests.get(f"{self.server}/api/v2.1/dtable/app-access-token/",
                         headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
        r.raise_for_status()
        d = r.json()
        self.access = d["access_token"]
        self.dts = (d.get("dtable_server") or f"{self.server}/api-gateway").rstrip("/")
        self._meta = None

    @property
    def h(self):
        return {"Authorization": f"Bearer {self.access}"}

    def base(self):
        return f"{self.dts}/api/v2/dtables/{self.uuid}"

    def meta(self):
        # metadata 嵌在 metadata 键下，直接取顶层 tables 会拿到 None
        if self._meta is None:
            r = requests.get(self.base() + "/metadata/", headers=self.h, timeout=30)
            r.raise_for_status()
            self._meta = r.json().get("metadata", {})
        return self._meta

    def tables(self):
        return self.meta().get("tables", [])

    def table(self, name):
        t = next((x for x in self.tables() if x["name"] == name), None)
        if t is None:
            die(f"SeaTable 中无此表：{name}")
        return t

    def select_options(self, table, col):
        t = self.table(table)
        c = next((x for x in t["columns"] if x["name"] == col), None)
        if not c:
            return []
        return [o.get("name") for o in (c.get("data") or {}).get("options", [])]

    def list_rows(self, table):
        t = self.table(table)
        k2n = {c["key"]: c["name"] for c in t["columns"]}
        rows, limit, off = [], 1000, 0
        while True:
            r = requests.get(self.base() + "/rows/", headers=self.h,
                             params={"table_name": table, "limit": limit, "offset": off},
                             timeout=40)
            r.raise_for_status()
            batch = r.json().get("rows", [])
            for row in batch:
                d = {k2n.get(k, k): v for k, v in row.items()
                     if k not in ("_ctime", "_mtime")}
                d["__row_id__"] = row.get("_id")
                rows.append(d)
            if len(batch) < limit:
                break
            off += limit
        return rows

    def append(self, table, data):
        payload = {"table_name": table,
                   "rows": [{k: v for k, v in data.items() if k != "__row_id__"}]}
        r = requests.post(self.base() + "/rows/",
                          headers={**self.h, "Content-Type": "application/json"},
                          json=payload, timeout=40)
        r.raise_for_status()
        rows = r.json().get("rows") or []
        return rows[0].get("_id") if rows else None

    def update(self, table, row_id, data):
        # 必须用 updates 数组；row_id+row 平铺格式会报 missing updates field
        r = requests.put(self.base() + "/rows/",
                         headers={**self.h, "Content-Type": "application/json"},
                         json={"table_name": table,
                               "updates": [{"row_id": row_id, "row": data}]}, timeout=40)
        r.raise_for_status()

    def link_id(self, t1, t2):
        """从 link 列 data.link_id 解析；兼容少数版本的顶层 links。"""
        a, b = self.table(t1)["_id"], self.table(t2)["_id"]
        for table_name, other_id in ((t1, b), (t2, a)):
            for col in self.table(table_name).get("columns", []):
                if col.get("type") != "link":
                    continue
                data = col.get("data") or {}
                ids = {data.get("table_id"), data.get("other_table_id")}
                if other_id in ids and data.get("link_id"):
                    return data["link_id"]
        for ln in self.meta().get("links", []):
            if {ln.get("table1_id"), ln.get("table2_id")} == {a, b}:
                return ln["link_id"]
        die(f"未找到 {t1} ↔ {t2} 的关联列")

    def link(self, t1, t2, row_id, other_ids, lid=None):
        """关联必须走 PUT /links/，update_row 写 link 列无效。双向各调一次。"""
        lid = lid or self.link_id(t1, t2)
        payloads = [
            {"link_id": lid, "table_name": t1, "other_table_name": t2,
             "row_id_list": [row_id], "other_rows_ids_map": {row_id: list(other_ids)}},
            {"link_id": lid, "table_name": t2, "other_table_name": t1,
             "row_id_list": list(other_ids),
             "other_rows_ids_map": {o: [row_id] for o in other_ids}},
        ]
        for pl in payloads:
            r = requests.put(self.base() + "/links/",
                             headers={**self.h, "Content-Type": "application/json"},
                             json=pl, timeout=40)
            r.raise_for_status()


# ── 杂项 ────────────────────────────────────────────
def today():
    return date.today().isoformat()


def money(v):
    return f"{float(v):,.2f}"


def cn_amount(num):
    """金额转中文大写（采购订单必备）。"""
    digits = "零壹贰叁肆伍陆柒捌玖"
    units = ["", "拾", "佰", "仟"]
    groups = ["", "万", "亿", "万亿"]
    num = round(float(num) + 1e-9, 2)
    yuan = int(num)
    jiao = int(round((num - yuan) * 100))
    if yuan == 0:
        s = "零圆"
    else:
        parts, gi = [], 0
        while yuan > 0:
            g = yuan % 10000
            if g:
                seg, zero = "", False
                for i in range(3, -1, -1):
                    d = (g // (10 ** i)) % 10
                    if d:
                        if zero:
                            seg += digits[0]
                            zero = False
                        seg += digits[d] + units[i]
                    elif seg:
                        zero = True
                parts.insert(0, seg + groups[gi])
            elif parts:
                parts.insert(0, "")
            yuan //= 10000
            gi += 1
        s = "".join(parts) + "圆"
    if jiao == 0:
        return s + "整"
    tail = ""
    if jiao // 10:
        tail += digits[jiao // 10] + "角"
    if jiao % 10:
        tail += digits[jiao % 10] + "分"
    return s + tail
