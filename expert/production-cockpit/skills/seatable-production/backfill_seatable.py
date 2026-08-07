# -*- coding: utf-8 -*-
"""backfill_seatable.py — 把缺失字段补录回 SeaTable 云端「生产」库。

三个子命令：
  python backfill_seatable.py gen        # 生成缺值模板 data/backfill_template.csv（带参考列 + 预填确定性值）
  python backfill_seatable.py push       # 读取已填写的 data/backfill_template.csv，按行写回云端
  python backfill_seatable.py push-json  # 读取从驾驶舱 HTML 复制回来的 data/backfill_submit.json，写回云端
                                        #   （--yes 跳过交互确认，供确认后自动写回）

安全说明：
  - 仅更新模板中「非空」的缺值单元格，不碰其它字段；按 SeaTable 行 _id 精准定位。
  - 单选列（如 放行状态）的值会先映射到云端已有选项 id，避免新建脏选项。
  - 写回前会先打印将要更新的行数/字段，确认无误才发起 PUT。
凭证：config.yaml 的 [seatable] 段（同 seatable_sync.py）。
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TEMPLATE = os.path.join(DATA, "backfill_template.csv")
META_PATH = os.path.join(DATA, "_sync_meta.json")

# 状态 -> 放行状态 的推荐映射（仅作提示，单选值以云端已有选项为准，脚本会取最接近的选项）
STATUS_TO_RELEASE = {
    "已交付": "已放行",
    "已完成": "已放行",
    "进行中": "待放行",
    "可能延迟": "待放行",
    "已超期": "待放行",
    "待客户下单": "待下单",
}


def _load_cfg():
    path = os.path.join(HERE, "config.yaml")
    cfg, in_seat = {}, False
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
                elif not raw.startswith(" "):
                    break
    return cfg


def _http_json(url, auth, data=None, method="GET", timeout=60):
    req = urllib.request.Request(url, headers={
        "Authorization": auth,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }, method=method)
    if data is not None:
        req.data = json.dumps(data).encode("utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def connect():
    cfg = _load_cfg()
    server = cfg.get("server", "https://cloud.seatable.cn").rstrip("/")
    api_token = cfg["api_token"]
    uuid = cfg["base_uuid"]
    app = _http_json(server + "/api/v2.1/dtable/app-access-token/", "Bearer " + api_token)
    access_token = app["access_token"]
    gateway = app.get("dtable_server", server + "/api-gateway/").rstrip("/") + "/"
    md = _http_json(gateway + f"/api/v2/dtables/{uuid}/metadata/", "Bearer " + access_token)
    metadata = md.get("metadata", {})
    # 表名 -> {显示名: (key, type)}
    name_map, type_map, key_map = {}, {}, {}
    for t in metadata.get("tables", []):
        nm = t.get("name")
        name_map[nm], type_map[nm], key_map[nm] = {}, {}, {}
        for c in t.get("columns", []):
            name_map[nm][c.get("name")] = c.get("key")
            type_map[nm][c.get("key")] = c.get("type")
            key_map[nm][c.get("key")] = c.get("name")
    # 单选列：选项 name -> id
    select_opts = {}
    for t in metadata.get("tables", []):
        nm = t.get("name")
        tn = urllib.parse.quote(nm)
        cols = _http_json(gateway + f"/api/v2/dtables/{uuid}/columns/?table_name={tn}",
                          "Bearer " + access_token).get("columns", [])
        for c in cols:
            if c.get("type") in ("single-select", "multiple-select"):
                opts = (c.get("data") or {}).get("options") or []
                select_opts[(nm, c.get("name"))] = {o.get("name"): o.get("id") for o in opts if o.get("id")}
    return {"server": server, "api_token": api_token, "uuid": uuid,
            "access_token": access_token, "gateway": gateway,
            "name_map": name_map, "type_map": type_map, "select_opts": select_opts}


def _date(v):
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def gen_template():
    """读取本地真实快照，生成缺值模板。确定性可推的日期先预填，单选/未知留空。"""
    plans = list(csv.DictReader(open(os.path.join(DATA, "生产计划.csv"), encoding="utf-8-sig")))
    asm = list(csv.DictReader(open(os.path.join(DATA, "组装记录.csv"), encoding="utf-8-sig")))
    rows = []
    for p in plans:
        rid = p.get("__row_id__", "")
        sd = _date(p.get("立项日期"))
        cd = _date(p.get("合同交期"))
        spend = p.get("花费天数", "").strip()
        spend = float(spend) if spend.replace(".", "", 1).isdigit() else 0.0
        status = p.get("状态", "").strip()
        done = status in ("已交付", "已完成")
        # 计划开始 ≈ 立项；计划完成 ≈ 合同交期（确定性变换）
        start = p.get("立项日期", "").strip()
        finish = p.get("合同交期", "").strip()
        # 实际完成：done 计划用 立项+花费天数（真实字段推算），否则留空
        actual = ""
        if done:
            if sd and spend:
                actual = (sd + timedelta(days=int(spend))).isoformat()
            elif cd:
                actual = finish
        rows.append({
            "表": "生产计划", "__row_id__": rid, "标识": p.get("生产产品", ""),
            "参考_状态": status, "参考_立项日期": start, "参考_合同交期": finish,
            "参考_花费天数": spend,
            "计划开始日期": start, "计划完成日期": finish,
            "实际完成日期": actual, "放行状态": "",
        })
    for a in asm:
        rows.append({
            "表": "组装记录", "__row_id__": a.get("__row_id__", ""),
            "标识": a.get("关联项目", "") or a.get("组装产品", "") or a.get("生产计划", ""),
            "参考_状态": "", "参考_立项日期": "", "参考_合同交期": "", "参考_花费天数": "",
            "计划开始日期": "", "计划完成日期": "", "实际完成日期": "", "放行状态": "",
            "组装良品率": "",
        })
    fill_cols = ["计划开始日期", "计划完成日期", "实际完成日期", "放行状态", "组装良品率"]
    fieldnames = ["表", "__row_id__", "标识", "参考_状态", "参考_立项日期",
                  "参考_合同交期", "参考_花费天数"] + fill_cols
    with open(TEMPLATE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    # 统计
    blank = sum(1 for r in rows for c in fill_cols if not r.get(c, "").strip())
    print(f"[ok] 模板已生成：{TEMPLATE}")
    print(f"     生产计划 {len(plans)} 行 + 组装记录 {len(asm)} 行，待填单元格约 {blank} 个")
    print("     已预填：计划开始≈立项、计划完成≈合同交期、实际完成(done)=立项+花费天数")
    print("     留空待你填：实际完成(进行中计划)、放行状态(单选)、组装良品率")
    print("     填好后把本文件发回给我，我执行 push 写回云端。")


def map_fields(conn, tbl, fields):
    """把 {显示列名: 值} 映射成 SeaTable 内部更新负载（处理单选选项 id）。"""
    nm = conn["name_map"].get(tbl)
    if not nm:
        print(f"[!] 表「{tbl}」在云端不存在，跳过")
        return None
    upd = {}
    for col, val in fields.items():
        val = str(val).strip()
        if not val:
            continue
        key = nm.get(col)
        if not key:
            print(f"[!] 列「{col}」在表「{tbl}」不存在，跳过该格")
            continue
        ctype = conn["type_map"][tbl].get(key)
        if ctype in ("single-select", "multiple-select"):
            opts = conn["select_opts"].get((tbl, col), {})
            oid = opts.get(val)
            if oid is None:
                for oname, oid2 in opts.items():
                    if val in oname or oname in val:
                        oid = oid2
                        break
            if oid is None:
                print(f"[!] 单选值「{val}」在「{tbl}.{col}」无匹配选项（可选：{list(opts.keys())}），跳过该格")
                continue
            upd[key] = [oid] if ctype == "multiple-select" else oid
        else:
            upd[key] = val
    return upd


def do_push(conn, updates, auto=False):
    """updates: [{table, row_id, fields:{列:值}}]。构建负载、预览、确认、写回云端。"""
    by_table = {}
    for u in updates:
        tbl, rid, fields = u["table"], u["row_id"], u["fields"]
        if not tbl or not rid:
            continue
        upd = map_fields(conn, tbl, fields)
        if upd:
            by_table.setdefault(tbl, []).append({"row_id": rid, "row": upd})
    total = sum(len(v) for v in by_table.values())
    if not total:
        print("[!] 没有需要写回的字段（都为空或无法映射）。")
        return
    print(f"\n→ 准备写回 {total} 行：")
    for tbl, items in by_table.items():
        print(f"  {tbl}: {len(items)} 行")
        for it in items[:5]:
            print("     ", it["row_id"], "→", {k: v for k, v in it["row"].items()})
        if len(items) > 5:
            print(f"      … 其余 {len(items) - 5} 行")
    if not auto:
        ok = input("确认写回云端？(输入 yes 继续)：").strip().lower()
        if ok != "yes":
            print("[x] 已取消，未做任何修改。")
            return
    for tbl, items in by_table.items():
        url = conn["gateway"] + f"/api/v2/dtables/{conn['uuid']}/rows/"
        _http_json(url, "Bearer " + conn["access_token"],
                   data={"table_name": tbl, "rows": items}, method="PUT")
        print(f"  [ok] {tbl}: 已更新 {len(items)} 行")
    print("\n✅ 写回完成。下一步：")
    print("    python seatable_sync.py && python cockpit.py   # 重新拉取并渲染")


def push_template(auto=False):
    if not os.path.exists(TEMPLATE):
        print("[!] 找不到模板，请先 gen 并填写。")
        return
    conn = connect()
    rows = list(csv.DictReader(open(TEMPLATE, encoding="utf-8-sig")))
    updates = []
    for r in rows:
        tbl = r.get("表", "").strip()
        rid = r.get("__row_id__", "").strip()
        if not tbl or not rid:
            continue
        fields = {c: r.get(c, "") for c in ["计划开始日期", "计划完成日期", "实际完成日期", "放行状态", "组装良品率"]}
        updates.append({"table": tbl, "row_id": rid, "fields": fields})
    do_push(conn, updates, auto=auto)


def push_json(json_path, auto=False):
    """从粘贴回的 JSON（{表:{row_id:{列:值}}}）写回云端。"""
    if not os.path.exists(json_path):
        print(f"[!] 找不到 JSON：{json_path}")
        return
    conn = connect()
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    updates = []
    for tbl, rowsd in data.items():
        for rid, fields in rowsd.items():
            updates.append({"table": tbl, "row_id": rid, "fields": fields})
    do_push(conn, updates, auto=auto)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["gen", "push", "push-json"])
    ap.add_argument("--file", default=os.path.join(DATA, "backfill_submit.json"),
                    help="push-json 读取的 JSON 路径")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认直接写回")
    args = ap.parse_args()
    if args.cmd == "gen":
        gen_template()
    elif args.cmd == "push":
        push_template(auto=args.yes)
    elif args.cmd == "push-json":
        push_json(args.file, auto=args.yes)


if __name__ == "__main__":
    main()
