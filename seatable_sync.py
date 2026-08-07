# -*- coding: utf-8 -*-
"""seatable_sync.py — 把 SeaTable 云端「生产」库的真实业务数据同步到本地 data/*.csv。

同步后 cockpit.py 无需改动即可基于真实数据渲染驾驶舱（本地 adapter 直接读 CSV）。

凭证来源：本技能 config.yaml 的 [seatable] 段（来自用户提供的 config-example.env）。
仅存于本地技能目录，勿提交到公开仓库。

SeaTable 云网关路径（已对照官方 seatable-api-python 源码核实）：
  app-access-token : GET {server}/api/v2.1/dtable/app-access-token/  (Bearer <api_token>)
  metadata         : GET {gateway}/api/v2/dtables/{uuid}/metadata/    (Bearer <access_token>)
  rows             : GET {gateway}/api/v2/dtables/{uuid}/rows/?table_name=X&limit=&offset=
其中 gateway = app-access-token 返回的 dtable_server（形如 https://cloud.seatable.cn/api-gateway/）。

用法：
  python seatable_sync.py                 # 全量同步
  python seatable_sync.py --dry-run       # 只打印表与行数，不写文件
  python seatable_sync.py --tables 项目,生产计划   # 只同步指定表
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
META_PATH = os.path.join(DATA, "_sync_meta.json")

# 云端列名 -> cockpit 期望列名 的别名（让 cockpit 无需改动即可读真实数据）
COLUMN_ALIAS = {
    "交期（天）": "交期",
}

# 需要丢弃的 SeaTable 内部字段（row 里以下划线开头，不在 metadata 列中）
INTERNAL_PREFIX = ("_",)


def _load_cfg():
    """极简 YAML 解析：只取 [seatable] 段的 key: value。"""
    path = os.path.join(HERE, "config.yaml")
    cfg = {}
    in_seat = False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            if raw.startswith("seatable:") and not raw.startswith(" "):
                in_seat = True
                continue
            if in_seat:
                if raw.startswith(" ") and ":" in raw:
                    k, _, v = raw.strip().partition(":")
                    cfg[k.strip()] = v.strip()
                else:
                    # 遇到新的顶层段，结束
                    if not raw.startswith(" "):
                        break
    return cfg


def _http_json(url, auth, timeout=60):
    req = urllib.request.Request(url, headers={
        "Authorization": auth,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def auth(server, api_token):
    url = server.rstrip("/") + "/api/v2.1/dtable/app-access-token/"
    return _http_json(url, "Bearer " + api_token)


def _flatten(col_type, v, select_map=None):
    """把 SeaTable 单元格值扁平化为可写入 CSV 的字符串。

    select_map: 单选/多选列的选项 id->name 映射（行值存的是选项 id）。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, list):
        if not v:
            return ""
        out = []
        for item in v:
            if isinstance(item, dict):
                out.append(str(item.get("display_value")
                               or item.get("name")
                               or item.get("url")
                               or ""))
            else:
                # 多选列：item 是选项 id
                out.append(str(select_map.get(item, item) if select_map else item))
        return ",".join(x for x in out if x)
    if isinstance(v, dict):
        return str(v.get("display_value") or v.get("name") or v.get("url") or "")
    # 单选列：行值是选项 id，翻译成显示名
    if select_map and isinstance(v, str) and v in select_map:
        return select_map[v]
    # 日期类列：ISO 字符串 "2026-03-31T00:00:00+08:00" -> "2026-03-31"
    if isinstance(v, str) and col_type in ("date", "ctime", "mtime", "datetime") and "T" in v:
        return v[:10]
    return str(v)


def fetch_select_options(gateway, uuid, access_token, table_name):
    """拉取某表的列定义，返回 {列key: {选项id: 选项名}}（仅 single/multiple-select 有值）。"""
    tn = urllib.parse.quote(table_name)
    url = gateway.rstrip("/") + f"/api/v2/dtables/{uuid}/columns/?table_name={tn}"
    data = _http_json(url, "Bearer " + access_token)
    result = {}
    for c in data.get("columns", []):
        if c.get("type") in ("single-select", "multiple-select"):
            opts = (c.get("data") or {}).get("options") or []
            result[c.get("key")] = {o.get("id"): o.get("name") for o in opts if o.get("id")}
    return result


def build_key_map(metadata):
    """返回 {表名: {列key: (显示名, 类型)}}。"""
    km = {}
    for t in metadata.get("tables", []):
        name = t.get("name")
        cols = {}
        for c in t.get("columns", []):
            cols[c.get("key")] = (c.get("name"), c.get("type"))
        km[name] = cols
    return km


def fetch_rows(gateway, uuid, access_token, table_name, limit=1000):
    """分页拉取某张表全部行。"""
    rows = []
    offset = 0
    tn = urllib.parse.quote(table_name)
    while True:
        url = (gateway.rstrip("/") + f"/api/v2/dtables/{uuid}/rows/"
               f"?table_name={tn}&limit={limit}&offset={offset}")
        data = _http_json(url, "Bearer " + access_token)
        batch = data.get("rows", [])
        rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        if offset > 50000:  # 安全阀
            break
    return rows


def sync(dry_run=False, only_tables=None):
    cfg = _load_cfg()
    if not cfg.get("api_token") or not cfg.get("base_uuid"):
        print("[!] config.yaml 缺少 [seatable] 段的 api_token / base_uuid，无法同步。")
        return None
    server = cfg.get("server", "https://cloud.seatable.cn").rstrip("/")
    api_token = cfg["api_token"]
    uuid = cfg["base_uuid"]

    print("→ 获取 app-access-token ...")
    app = auth(server, api_token)
    access_token = app["access_token"]
    gateway = app.get("dtable_server", server + "/api-gateway/").rstrip("/") + "/"
    print(f"  库名：{app.get('dtable_name')} · 网关：{gateway}")

    print("→ 拉取 metadata ...")
    md = _http_json(gateway + f"/api/v2/dtables/{uuid}/metadata/", "Bearer " + access_token)
    metadata = md.get("metadata", {})
    key_map = build_key_map(metadata)
    print(f"  云端表数：{len(key_map)}")

    os.makedirs(DATA, exist_ok=True)
    counts = {}
    for table_name, cols in key_map.items():
        if only_tables and table_name not in only_tables:
            continue
        rows = fetch_rows(gateway, uuid, access_token, table_name)
        counts[table_name] = len(rows)
        if dry_run:
            print(f"  [dry] {table_name}: {len(rows)} 行, {len(cols)} 列")
            continue
        # 单选/多选列：行值存的是选项 id，需要 id->name 映射
        select_maps = fetch_select_options(gateway, uuid, access_token, table_name)
        # 写 CSV：__row_id__ 第一列，其余按 metadata 列顺序（套用别名）
        seen = set()
        fieldnames = ["__row_id__"]
        for (_k, (name, _t)) in cols.items():
            out_name = COLUMN_ALIAS.get(name, name)
            if out_name in seen:
                continue
            seen.add(out_name)
            fieldnames.append(out_name)
        out_path = os.path.join(DATA, table_name.replace("/", "_").replace("\\", "_") + ".csv")
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                rid = r.get("_id", "")
                rec = {"__row_id__": rid}
                for key, (name, ctype) in cols.items():
                    val = r.get(key)
                    out_name = COLUMN_ALIAS.get(name, name)
                    rec[out_name] = _flatten(ctype, val, select_maps.get(key))
                w.writerow(rec)
        print(f"  [ok] {table_name}: {len(rows)} 行 -> {os.path.basename(out_path)}")

    if not dry_run:
        meta = {
            "source": "seatable-cloud",
            "base_name": app.get("dtable_name"),
            "base_uuid": uuid,
            "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "row_counts": counts,
        }
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 同步完成，写入 {META_PATH}")
        print("   下一步：python cockpit.py  →  重生成真实驾驶舱")
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--tables", help="只同步指定表，逗号分隔，如 项目,生产计划")
    args = ap.parse_args()
    only = set(t.strip() for t in args.tables.split(",")) if args.tables else None
    sync(dry_run=args.dry_run, only_tables=only)


if __name__ == "__main__":
    main()
