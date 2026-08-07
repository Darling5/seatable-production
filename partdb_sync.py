# -*- coding: utf-8 -*-
"""
partdb_sync.py — 从真实 PartDB 拉取库存 / BOM，生成 data/partdb_snapshot.json，
供 cockpit.py 渲染真实的「物料库存预警 + 缺料检查」。

用法:
  python partdb_sync.py                 # 默认：project 22（4G小卡V4.0），生产 10 套
  python partdb_sync.py --project 22 --qty 10
  python partdb_sync.py --no-bom        # 只刷库存预警，不做 BOM 缺料
  python partdb_sync.py --workers 24

配置优先级: 命令行参数 > 环境变量(PARTDB_URL/PARTDB_TOKEN) > config.yaml 的 partdb 段。
"""
import os
import sys
import json
import re
import argparse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)

try:
    from adapters.factory import load_config
    _cfg = load_config().get("partdb") or {}
except Exception:
    _cfg = {}

DEFAULT_URL = os.environ.get("PARTDB_URL") or _cfg.get("url") or ""
DEFAULT_TOKEN = os.environ.get("PARTDB_TOKEN") or _cfg.get("token") or ""


# ---------- PartLot 确认日期解析（铁律：MDD/MMDD = 已盘点确认）----------
def parse_lot_date(desc):
    if desc is None or not str(desc).strip():
        return None
    d = str(desc).strip()
    if re.match(r"^\d{3,4}$", d):
        m, day = (int(d[0]), int(d[1:])) if len(d) == 3 else (int(d[:2]), int(d[2:]))
        if 1 <= m <= 12 and 1 <= day <= 31:
            return f"{m}月{day}日"
    return None


def _get(url, token, timeout=20):
    req = urllib.request.Request(url, headers={"Authorization": "Token " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_part_detail(base, token, pid):
    """拉单个零件详情，返回规整字典；失败返回 None。"""
    try:
        d = _get(base + "/parts/%s" % pid, token)
    except Exception:
        return None
    lots = d.get("partLots") or []
    confirmed = 0
    unconfirmed = 0
    locs = []
    for l in lots:
        amt = l.get("amount") or 0
        sl = l.get("storage_location")
        loc = sl.get("name", "") if isinstance(sl, dict) else (sl or "")
        cd = parse_lot_date(l.get("description"))
        if cd is not None:
            confirmed += amt
            if loc:
                locs.append("%s:%s(%s)" % (loc, amt, cd))
        else:
            unconfirmed += amt
    price = None
    for od in d.get("orderdetails") or []:
        for pd in od.get("pricedetails") or []:
            pp = pd.get("price_per_unit")
            if pp is not None:
                try:
                    pp = float(pp)
                except (TypeError, ValueError):
                    continue
                if price is None or pp < price:
                    price = pp
    fp = d.get("footprint")
    cat = d.get("category")
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "ipn": d.get("ipn") or "",
        "footprint": fp.get("name", "") if isinstance(fp, dict) else (fp or ""),
        "category": cat.get("name", "") if isinstance(cat, dict) else (cat or ""),
        "minamount": d.get("minamount") or 0,
        "total_instock": d.get("total_instock") or 0,
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "locs": locs,
        "price": price,
    }


def fetch_all_parts(base, token):
    """分页拉全部零件列表（仅基础字段）。"""
    out = []
    page = 1
    while True:
        data = _get("%s/parts?limit=200&page=%d" % (base, page), token)
        out.extend(data.get("hydra:member", []))
        if not data.get("hydra:view", {}).get("hydra:next"):
            break
        page += 1
    return out


def fetch_project(base, token, pid):
    try:
        return _get(base + "/projects/%s" % pid, token)
    except Exception:
        return {"id": pid, "name": "项目%d" % pid}


def fetch_bom(base, token, pid):
    items = []
    page = 1
    while True:
        data = _get("%s/projects/%s/bom?limit=100&page=%d" % (base, pid, page), token)
        items.extend(data.get("hydra:member", []))
        if not data.get("hydra:view", {}).get("hydra:next"):
            break
        page += 1
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, default=22, help="做缺料检查的 PartDB 项目 ID（默认 22 = 4G小卡V4.0）")
    ap.add_argument("--qty", type=int, default=10, help="生产套数（默认 10）")
    ap.add_argument("--no-bom", action="store_true", help="跳过 BOM 缺料检查，只刷库存预警")
    ap.add_argument("--workers", type=int, default=20, help="并发拉取零件详情的线程数")
    args = ap.parse_args()

    if not (DEFAULT_URL and DEFAULT_TOKEN):
        print("[错误] 未找到 PartDB 配置：请设置环境变量 PARTDB_URL/PARTDB_TOKEN，或写 config.yaml 的 partdb 段。")
        sys.exit(1)

    base = DEFAULT_URL.rstrip("/")
    token = DEFAULT_TOKEN
    print("[1/4] 拉取全部零件列表 ...")
    parts_list = fetch_all_parts(base, token)
    print("      共 %d 个零件" % len(parts_list))

    print("[2/4] 并发拉取零件详情（确认库存 + 单价）...")
    cache = {}
    ids = [p.get("id") for p in parts_list if p.get("id") is not None]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_part_detail, base, token, i): i for i in ids}
        done = 0
        for f in as_completed(futs):
            res = f.result()
            done += 1
            if res:
                cache[res["id"]] = res
            if done % 50 == 0:
                print("      已处理 %d/%d" % (done, len(ids)))
    print("      成功 %d/%d" % (len(cache), len(ids)))

    # ---- 库存预警：minamount>0 且 确认库存 < 安全库存 ----
    warn = []
    zero_confirmed = 0
    for p in cache.values():
        if p["confirmed"] == 0 and p["total_instock"] == 0:
            zero_confirmed += 1
        if p["minamount"] and p["minamount"] > 0 and p["confirmed"] < p["minamount"]:
            gap = p["minamount"] - p["confirmed"]
            warn.append({
                "name": p["name"],
                "ipn": p["ipn"],
                "footprint": p["footprint"],
                "confirmed": p["confirmed"],
                "minamount": p["minamount"],
                "gap": gap,
                "status": "紧急" if p["confirmed"] == 0 else "警告",
                "locs": p["locs"],
                "price": p["price"],
            })
    warn.sort(key=lambda x: (0 if x["status"] == "紧急" else 1, -x["gap"]))

    snapshot = {
        "source": "PartDB",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "part_count": len(cache),
        "zero_confirmed": zero_confirmed,
        "inventory_warn": warn,
        "bom": None,
    }

    # ---- BOM 缺料检查 ----
    if not args.no_bom:
        print("[3/4] 拉取项目 %d 的 BOM 并做缺料检查（%d 套）..." % (args.project, args.qty))
        proj = fetch_project(base, token, args.project)
        bom_items = fetch_bom(base, token, args.project)
        shortage = []
        single_set_cost = 0.0
        priced_count = 0
        for it in bom_items:
            q = it.get("quantity") or 0
            part_ref = it.get("part") or {}
            pid = part_ref.get("id")
            det = cache.get(pid)
            name = part_ref.get("name") or (det.get("name") if det else "?")
            ipn = det.get("ipn") if det else ""
            fp = det.get("footprint") if det else ""
            confirmed = det.get("confirmed", 0) if det else 0
            price = det.get("price") if det else None
            needed = q * args.qty
            gap = max(needed - confirmed, 0)
            risks = []
            if price is None:
                risks.append("无价")
            else:
                single_set_cost += q * price
                priced_count += 1
            if confirmed == 0 and needed > 0:
                risks.append("零库存")
            elif 0 < confirmed < needed:
                risks.append("库存不足")
            shortage.append({
                "name": name,
                "ipn": ipn,
                "footprint": fp,
                "qty_per": q,
                "need": needed,
                "confirmed": confirmed,
                "gap": gap,
                "price": price,
                "amount": round(q * price, 2) if price else None,
                "risk": risks,
            })
        shortage.sort(key=lambda x: (-x["gap"], x["name"]))
        snapshot["bom"] = {
            "project_id": args.project,
            "project_name": proj.get("name", "项目%d" % args.project),
            "qty": args.qty,
            "bom_count": len(bom_items),
            "shortage": [s for s in shortage if s["gap"] > 0],
            "single_set_cost": round(single_set_cost, 2),
            "priced_count": priced_count,
            "unpriced_count": len(bom_items) - priced_count,
        }
        print("      缺料物料 %d 种 / 单套成本 ¥%.2f（%d 种有价）" % (
            len(snapshot["bom"]["shortage"]), single_set_cost, priced_count))
    else:
        print("[3/4] 跳过 BOM 缺料检查（--no-bom）")

    out_path = os.path.join(SKILL_DIR, "data", "partdb_snapshot.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print("[4/4] 已写出 %s" % out_path)
    print("      库存预警 %d 条，零确认库存零件 %d 个" % (len(warn), zero_confirmed))


if __name__ == "__main__":
    main()
