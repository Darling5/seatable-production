# -*- coding: utf-8 -*-
"""
PartDB 新建物料 + 批次入库。
默认 dry-run 只打印；加 --apply 才真写。
用法见 SKILL.md。
"""
import os
import sys
import json
import argparse
import urllib.request
import urllib.error
import urllib.parse

CFG = os.path.expanduser("~/.qclaw/seatable-cache/config.env")


def load_env():
    env = {}
    for line in open(CFG, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
BASE = ENV["PARTDB_URL"].rstrip("/")
TOKEN = ENV["PARTDB_TOKEN"]


def req(path, method="GET", data=None, merge_patch=False):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method=method)
    r.add_header("Authorization", "Token " + TOKEN)
    r.add_header("Accept", "application/json")
    if body:
        r.add_header("Content-Type",
                     "application/merge-patch+json" if merge_patch else "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:600]
    except Exception as e:
        return -1, str(e)


def get_all(path, max_pages=40):
    out, page = [], 1
    while page <= max_pages:
        st, d = req(f"{path}?page={page}")
        if st != 200 or not isinstance(d, list) or not d:
            break
        out += d
        page += 1
    return out


def resolve(path, name):
    """按名称精确解析实体 id；找不到返回 None。"""
    if name is None:
        return None
    for e in get_all(path):
        if (e.get("name") or "") == name:
            return e["id"]
    return None


def search_dup(keyword):
    hits = []
    for p in get_all("/parts"):
        n = (p.get("name") or "").lower()
        d = (p.get("description") or "").lower()
        if keyword.lower() in n or keyword.lower() in d:
            cat = p.get("category") or {}
            hits.append({
                "id": p.get("id"), "ipn": p.get("ipn"), "name": p.get("name"),
                "desc": p.get("description"),
                "cat": cat.get("name") if isinstance(cat, dict) else cat,
                "lots": [{"amt": l.get("amount"),
                          "loc": (l.get("storage_location") or {}).get("name")
                                  if isinstance(l.get("storage_location"), dict) else None,
                          "date": l.get("description")}
                         for l in (p.get("partLots") or [])],
            })
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--desc", default="")
    ap.add_argument("--category")
    ap.add_argument("--tags", default="")
    ap.add_argument("--amount", type=float, default=0)
    ap.add_argument("--location")
    ap.add_argument("--date", help="盘点日期 MDD/MMDD，如 902 = 9月2日")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    cat_id = resolve("/categories", a.category) if a.category else None
    loc_id = resolve("/storage_locations", a.location) if a.location else None

    if a.category and cat_id is None:
        print(f"[ERR] 未找到分类：{a.category}"); sys.exit(1)
    if a.location and loc_id is None:
        print(f"[ERR] 未找到库位：{a.location}"); sys.exit(1)

    body = {"name": a.name, "description": a.desc}
    if cat_id:
        body["category"] = f"/api/categories/{cat_id}"
    if a.tags:
        body["tags"] = a.tags

    print("=== 搜重 ===")
    for h in search_dup(a.name):
        print(" ", h["ipn"], h["name"], "|", h["desc"], "|", h["cat"], "|", h["lots"])

    print("=== 计划 ===")
    print("POST /parts", json.dumps(body, ensure_ascii=False))
    print(f"PATCH /parts/{{id}} ipn=P{{id:04d}}  (merge-patch)")
    if a.amount or loc_id or a.date:
        print("POST /part_lots", json.dumps({
            "part": "/api/parts/{id}", "amount": a.amount,
            "storage_location": f"/api/storage_locations/{loc_id}" if loc_id else None,
            "description": a.date or ""}, ensure_ascii=False))

    if not a.apply:
        print("\n[DRY] 加 --apply 才会写入"); return

    st, d = req("/parts", "POST", body)
    print("POST /parts ->", st)
    if st >= 400:
        print(str(d)[:500]); sys.exit(1)
    pid = d["id"]
    ipn = "P%04d" % pid

    st2, _ = req(f"/parts/{pid}", "PATCH", {"ipn": ipn}, merge_patch=True)
    print("PATCH ipn ->", st2, ipn)

    if a.amount or loc_id or a.date:
        lot = {"part": f"/api/parts/{pid}", "amount": a.amount, "description": a.date or ""}
        if loc_id:
            lot["storage_location"] = f"/api/storage_locations/{loc_id}"
        st3, d3 = req("/part_lots", "POST", lot)
        print("POST /part_lots ->", st3)
        if st3 >= 400:
            print(str(d3)[:500])

    st4, d4 = req(f"/parts/{pid}")
    if st4 == 200:
        print(json.dumps({
            "id": d4.get("id"), "ipn": d4.get("ipn"), "name": d4.get("name"),
            "description": d4.get("description"), "tags": d4.get("tags"),
            "category": (d4.get("category") or {}).get("name"),
            "total_instock": d4.get("total_instock"),
            "partLots": [{"amount": l.get("amount"),
                          "location": (l.get("storage_location") or {}).get("name"),
                          "desc": l.get("description")} for l in (d4.get("partLots") or [])],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
