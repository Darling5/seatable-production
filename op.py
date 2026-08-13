#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生产交付协同助手 — 统一数据操作 CLI（后端无关）。

所有增删改查都走这里，SKILL.md 不直接碰任何存储细节，也不出现任何凭证。
用法示例：
  python3 op.py list 生产计划
  python3 op.py list 生产计划 --where 状态=进行中
  python3 op.py append 生产计划 '{"生产产品":"4G小卡","数量":100,"关联项目":"项目A"}'
  python3 op.py update 生产计划 row_3 '{"状态":"已完成"}'
  python3 op.py delete 生产计划 row_3
  python3 op.py link 生产计划 PCB下单记录 row_3 row_7
  python3 op.py linked 生产计划 row_3
  python3 op.py meta 生产计划
  python3 op.py resolve-link 生产计划 PCB下单记录
  python3 op.py export-excel 生产数据.xlsx
  python3 op.py partdb-search 电容 10
  python3 op.py partdb-shortage 22 100
  python3 op.py res-add 张三 --stage 贴片 --capacity 1 --rate 400
  python3 op.py alloc-add 张三 4G小卡二代 --stage 贴片 --qty 5 --days 5
  python3 op.py res-load
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters.factory import load_config, get_adapter, get_partdb  # noqa: E402
from adapters import schema  # noqa: E402


def _print_rows(rows):
    if not rows:
        print("(空)")
        return
    # 打印为表格
    cols = []
    for r in rows:
        for k in r.keys():
            if k != "__row_id__" and k not in cols:
                cols.append(k)
    header = ["__row_id__"] + cols
    print("\t".join(header))
    for r in rows:
        print("\t".join(str(r.get(c, "")) for c in header))


def create_production_plan_row(data):
    """把向导/文本解析出的结构化数据转成「生产计划」表的写入行（项目经理建计划）。"""
    import datetime as _dt
    prod = str(data.get("产品", "")).strip()
    qty = int(data.get("数量", 0) or 0)
    if not prod or not qty:
        raise ValueError("产品名称或数量缺失，无法写入")
    days = int(data.get("交期天数", 0) or 0)
    due = str(data.get("预计完工") or "").strip() or (
        (_dt.date.today() + _dt.timedelta(days=days)).isoformat())
    today = _dt.date.today().isoformat()
    return {
        "生产产品": prod,
        "数量": qty,
        "关联项目": prod + " 项目",
        "状态": "进行中",
        "阶段": "库存核对",
        "立项日期": today,
        "合同交期": due,
        "完货日期": "",
        "负责人": str(data.get("负责人", "") or ""),
        "优先级": str(data.get("优先级", "中") or "中"),
        "备注": str(data.get("备注", "") or ""),
    }


def create_project_record(data):
    """把销售立项向导/文本解析出的数据转成「项目」表的写入行（对应销售立项表单）。"""
    import datetime as _dt
    name = str(data.get("产品", "")).strip()
    if not name:
        raise ValueError("项目名称缺失，无法写入")
    days = int(data.get("交期天数", 0) or 0)
    due = str(data.get("预计完工") or "").strip() or (
        (_dt.date.today() + _dt.timedelta(days=days)).isoformat())
    return {
        "项目": name,
        "状态": "计划中",
        "阶段": "立项",
        "合同交期": due,
        "花费天数": days,
        "创建者": str(data.get("负责人", "") or ""),
        "产品需求": str(data.get("备注", "") or ""),
    }


def _build_target_row(data):
    """按 _target 选择写入表与目标行；默认「生产计划」（向后兼容旧下载 JSON）。"""
    target = str(data.get("_target") or "生产计划").strip()
    if target == "项目":
        return "项目", create_project_record(data)
    return "生产计划", create_production_plan_row(data)


def _apply_and_refresh(adapter, args, table, row):
    rid = adapter.append_row(table, row)
    print(f"OK 已写入「{table}」 row_id={rid}")
    try:
        import subprocess
        cockpit = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cockpit.py")
        out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "项目管理驾驶舱.html")
        subprocess.run([sys.executable, cockpit, out], check=False)
        print(f"OK 驾驶舱已刷新：{out}")
    except Exception as e:
        print(f"[warn] 驾驶舱自动刷新失败（手动跑 python cockpit.py 即可）：{e}")


def _parse_wizard_text(text):
    """解析【新建项目】/【新建生产计划】纯文本为结构化 dict（兼容中文/英文冒号）。"""
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("【") or line.startswith("（") or line.startswith("("):
            continue
        if "：" in line:
            k, _, v = line.partition("：")
            data[k.strip()] = v.strip()
        elif ":" in line:
            k, _, v = line.partition(":")
            data[k.strip()] = v.strip()
    # 识别目标表：销售立项表单写到「项目」，其余写到「生产计划」
    if "【新建项目】" in text:
        data["_target"] = "项目"
    return data


# ══════════════════════════════════════════════════════════════════
# 资源域：录入 / 查询 / 负载分析（对标 MS Project 资源工作表）
# ══════════════════════════════════════════════════════════════════

def _warn_enum(table, row):
    """写入前软校验枚举值，仅提示不阻断。"""
    for w in schema.validate_enum(table, row):
        print(f"[warn] {w}")


def _res_exists(adapter, name):
    """按姓名查资源，返回 (row_id, row) 或 (None, None)。"""
    for r in adapter.list_rows("资源"):
        if (r.get("姓名") or "").strip() == name.strip():
            return r.get("__row_id__"), r
    return None, None


def cmd_res_add(adapter, args):
    """新增/更新一条资源档案。同名视为更新，避免重复建档。"""
    row = {
        "姓名": args.name.strip(),
        "类型": args.type,
        "所属工序": args.stage or "",
        "日产能": args.capacity,
        "单位": args.unit,
        "日费率": args.rate,
        "在岗状态": args.status,
        "备注": args.note or "",
    }
    _warn_enum("资源", row)
    rid, old = _res_exists(adapter, args.name)
    if rid:
        adapter.update_row("资源", rid, row)
        print(f"OK 已更新资源「{args.name}」 row_id={rid}（同名档案已存在，执行覆盖更新）")
    else:
        rid = adapter.append_row("资源", row)
        print(f"OK 已建档资源「{args.name}」 row_id={rid}")
    print(f"   类型={args.type} 工序={args.stage or '—'} 日产能={args.capacity}{args.unit} "
          f"日费率=¥{args.rate} 状态={args.status}")


def cmd_alloc_add(adapter, args):
    """把资源分配到某个生产计划的某道工序上（MS Project 的「资源分配」）。"""
    import datetime as _dt
    start = args.start or _dt.date.today().isoformat()
    if args.days and not args.end:
        end = (_dt.date.fromisoformat(start) + _dt.timedelta(days=int(args.days) - 1)).isoformat()
    else:
        end = args.end or start
    if end < start:
        print(f"[error] 结束日期 {end} 早于开始日期 {start}，已拒绝写入。"); sys.exit(1)

    rid_res, res = _res_exists(adapter, args.resource)
    if not rid_res:
        _hint = 'python3 op.py res-add "%s" --stage <工序> --rate <日费率>' % args.resource
        print("[warn] 资源表中没有「%s」的档案，仍会写入分配记录，"
              "但驾驶舱会标为「未登记」。建议先执行：\n       %s" % (args.resource, _hint))

    # 排程冲突预检：同一资源、活动分配、日期区间相交
    conflicts = []
    for a in adapter.list_rows("资源分配"):
        if (a.get("资源") or "").strip() != args.resource.strip():
            continue
        if (a.get("状态") or "计划中").strip() not in ("计划中", "进行中"):
            continue
        s2, e2 = str(a.get("开始日期") or "")[:10], str(a.get("结束日期") or "")[:10]
        if s2 and e2 and not (end < s2 or start > e2):
            conflicts.append((a.get("生产计划") or "?", s2, e2))

    row = {
        "资源": args.resource.strip(),
        "生产计划": args.plan.strip(),
        "工序": args.stage or "",
        "投入量": args.qty,
        "单位": args.unit,
        "开始日期": start,
        "结束日期": end,
        "状态": args.status,
        "备注": args.note or "",
    }
    _warn_enum("资源分配", row)
    rid = adapter.append_row("资源分配", row)
    print(f"OK 已分配：{args.resource} → 「{args.plan}」{args.stage or ''} "
          f"{start}~{end} 投入 {args.qty}{args.unit}  row_id={rid}")
    if conflicts:
        print(f"[warn] 检测到 {len(conflicts)} 处排程冲突，该资源同期已被占用：")
        for pl, s2, e2 in conflicts:
            print(f"       · 「{pl}」{s2}~{e2}")
        print("       请改期、拆分投入量，或换人。驾驶舱「资源负载」页会持续提醒。")


def cmd_res_load(adapter, args):
    """终端版资源负载报表：负载率 / 超载 / 闲置 / 冲突 / 人工成本。"""
    import datetime as _dt
    import cockpit as _ck
    today = _dt.date.today()
    plans = adapter.list_rows("生产计划")
    res = _ck.compute_resources(adapter, today, plans)
    if not res:
        print("尚未录入任何资源或分配记录。先执行：\n"
              "  python3 op.py res-add 张三 --stage 贴片 --capacity 1 --rate 400\n"
              "  python3 op.py alloc-add 张三 4G小卡二代 --stage 贴片 --qty 5 --days 5")
        return
    w = res["week"]
    print(f"本周窗口 {w['start']} ~ {w['end']}（{w['workdays']} 个工作日）")
    print(f"在岗 {res['on_duty']}/{res['total']}  平均负载 {res['avg_load']}%  "
          f"超载 {len(res['over'])}  闲置 {len(res['idle'])}  冲突 {len(res['conflicts'])}  "
          f"人工成本 ¥{res['labor_cost']:,.0f}")
    print("-" * 78)
    print(f"{'资源':<10}{'类型':<6}{'工序':<10}{'状态':<8}{'负载':>8}{'已排/产能':>14}{'成本':>12}")
    for r in res["rows"]:
        flag = "！超载" if r["over"] else ("·闲置" if r["idle"] else "")
        used = "%s/%s" % (r["week_alloc"], r["capacity"])
        cost = "¥%s" % format(r["cost"], ",.0f")
        load = "%s%%" % r["load"]
        print("%-10s%-6s%-10s%-8s%8s%14s%12s  %s" % (
            r["name"], r["type"], r["stage"] or "—", r["status"], load, used, cost, flag))
    if res["conflicts"]:
        print("-" * 78)
        print("排程冲突：")
        for c in res["conflicts"]:
            print(f"  · {c['name']}：「{c['a']}」与「{c['b']}」重叠 {c['days']} 天")
    if res["plan_cost"]:
        print("-" * 78)
        print("各生产计划人工投入：")
        for p in res["plan_cost"]:
            print(f"  · {p['plan']:<24} {p['people']} 人  投入 {p['qty']}  ¥{p['cost']:,.0f}")


def cmd_doctor(adapter, args):
    """开局体检：把「你还缺什么」一次说清，每条附下一步命令。"""
    import doctor as _doc
    cfg = load_config(args.config)
    findings = _doc.run(adapter, cfg)
    print(_doc.render(findings))
    if _doc.has_blocker(findings) and getattr(args, "strict", False):
        sys.exit(2)


def cmd_intake(adapter, args):
    """执行一份意图清单（JSON）。低风险自动写，高风险需 --yes 或逐条确认。

    意图 JSON 由上层 AI 依据 SKILL.md 从自然语言提取，格式见 intake.py。
    """
    import intake
    with open(args.file, encoding="utf-8") as f:
        payload = json.load(f)
    raw = payload.get("原话") or payload.get("raw") or ""
    actor = payload.get("记录人") or payload.get("actor") or ""
    items = payload.get("intents") or payload.get("意图") or []
    if not items:
        print("意图清单为空，未做任何写入。")
        return

    intents = []
    for it in items:
        i = intake.Intent(it.get("op"), it.get("table"), it.get("data"),
                          it.get("row_id"), it.get("reason", ""))
        intents.append(intake.assess(i, adapter))

    print(intake.render_plan(intents))
    auto, confirm = intake.split(intents)

    if confirm and not args.yes:
        print("")
        print("以上 %d 条高风险改动**尚未执行**。" % len(confirm))
        print("确认无误后，重新执行并加 --yes；或修改 JSON 后重跑。")
        if auto and not args.only_confirmed:
            print("（%d 条低风险项也一并暂停了，避免数据半写半不写）" % len(auto))
        return

    todo = intents if args.yes else auto
    if not todo:
        print("没有可执行的条目。")
        return
    print("")
    print("── 开始执行 %d 条 ──" % len(todo))
    for ok, msg in intake.execute(adapter, todo, actor=actor, raw_text=raw):
        print(("OK " if ok else "!! ") + msg)
    _refresh_cockpit(args)


def _refresh_cockpit(args):
    try:
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        cockpit = os.path.join(here, "cockpit.py")
        out = getattr(args, "out", None) or os.path.join(here, "项目管理驾驶舱.html")
        cmd = [sys.executable, cockpit, out]
        # 必须沿用同一个 --config，否则会拿另一个库的数据去刷新驾驶舱，
        # 出现「写了 A 库、看到 B 库」的错觉。
        cfg = getattr(args, "config", None)
        if cfg:
            cmd += ["--config", cfg]
        subprocess.run(cmd, check=False)
        print("OK 驾驶舱已刷新：%s" % out)
    except Exception as e:
        print("[warn] 驾驶舱刷新失败（手动跑 python cockpit.py 即可）：%s" % e)


def cmd_stage(adapter, args):
    """查看/推进项目阶段。不带 --to 时只显示当前阶段与停留天数。"""
    import intake
    from adapters import schema as _s
    table = args.table
    key = "生产产品" if table == "生产计划" else "项目"

    if not args.to:
        print("%-28s %-8s %s" % ("项目", "阶段", "停留"))
        print("-" * 52)
        trace = {}
        try:
            for t in adapter.list_rows("阶段轨迹"):
                trace[(t.get("项目") or "").strip()] = t.get("日期")
        except Exception:
            pass
        import datetime as _dt
        today = _dt.date.today()
        for r in adapter.list_rows(table):
            nm = (r.get(key) or "").strip()
            if not nm:
                continue
            st = (r.get("阶段") or "").strip() or "(未设置)"
            days = ""
            if trace.get(nm):
                try:
                    d0 = _dt.date.fromisoformat(str(trace[nm])[:10])
                    days = "%d 天" % (today - d0).days
                except Exception:
                    pass
            print("%-28s %-8s %s" % (nm[:26], st, days))
        print("")
        print("标准阶段：" + " → ".join(_s.STAGES))
        return

    # 关键：必须先把「当前阶段」查出来，再做风险判定。
    # 否则 stage_jump_warning(None, 目标) 永远返回 None —— 跳步检查会全线失效。
    cur, found = "", False
    for r in adapter.list_rows(table):
        if (r.get(key) or "").strip() == (args.project or "").strip():
            cur, found = (r.get("阶段") or "").strip(), True
            break
    if not found:
        print("未找到「%s」（在表「%s」的「%s」列）。先用 op.py stage 查看可选项目。"
              % (args.project, table, key))
        sys.exit(1)

    i = intake.Intent("stage", table,
                      {"项目": args.project, "原阶段": cur,
                       "新阶段": args.to, "_table": table},
                      reason=args.note or "")
    intake.assess(i, adapter)
    print(intake.render_plan([i]))
    if i.risk == intake.CONFIRM and not args.yes:
        print("\n未执行。确认无误后加 --yes 重跑。")
        return
    for ok, msg in intake.execute(adapter, [i], actor=args.actor or "",
                                  raw_text=args.note or ""):
        print(("OK " if ok else "!! ") + msg)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").add_argument("table")
    sp = sub.add_parser("query"); sp.add_argument("table"); sp.add_argument("--where", action="append", default=[])
    sp = sub.add_parser("append"); sp.add_argument("table"); sp.add_argument("json")
    sp = sub.add_parser("update"); sp.add_argument("table"); sp.add_argument("row_id"); sp.add_argument("json")
    sp = sub.add_parser("delete"); sp.add_argument("table"); sp.add_argument("row_ids", nargs="+")
    sp = sub.add_parser("link"); sp.add_argument("table"); sp.add_argument("other"); sp.add_argument("row_id"); sp.add_argument("other_row_ids", nargs="+")
    sp = sub.add_parser("linked"); sp.add_argument("table"); sp.add_argument("row_id")
    sub.add_parser("meta").add_argument("table")
    sp = sub.add_parser("resolve-link"); sp.add_argument("table"); sp.add_argument("other")
    sp = sub.add_parser("export-excel"); sp.add_argument("out", nargs="?", default="生产数据.xlsx")
    sp = sub.add_parser("partdb-search"); sp.add_argument("keyword"); sp.add_argument("limit", nargs="?", type=int, default=20)
    sp = sub.add_parser("partdb-shortage"); sp.add_argument("project_id", type=int); sp.add_argument("qty", type=int)
    sp = sub.add_parser("apply-wizard"); sp.add_argument("file"); sp.add_argument("--out", nargs="?", default=None)
    sp = sub.add_parser("apply-text"); sp.add_argument("file"); sp.add_argument("--out", nargs="?", default=None)

    # ── 资源域 ──────────────────────────────────────────────
    sp = sub.add_parser("res-add", help="新增/更新资源档案（人/设备/外协）")
    sp.add_argument("name", help="姓名或设备名，同名视为更新")
    sp.add_argument("--type", default="人员", choices=schema.RESOURCE_TYPES)
    sp.add_argument("--stage", default="", help="所属工序，如 贴片/组装/测试")
    sp.add_argument("--capacity", type=float, default=1.0, help="日产能，默认 1")
    sp.add_argument("--unit", default="人日")
    sp.add_argument("--rate", type=float, default=0.0, help="日费率（元）")
    sp.add_argument("--status", default="在岗", choices=schema.RESOURCE_STATUS)
    sp.add_argument("--note", default="")

    sp = sub.add_parser("alloc-add", help="把资源分配到某生产计划的某道工序")
    sp.add_argument("resource", help="资源姓名")
    sp.add_argument("plan", help="生产计划名（生产产品或计划编号）")
    sp.add_argument("--stage", default="", help="工序")
    sp.add_argument("--qty", type=float, default=1.0, help="投入量")
    sp.add_argument("--unit", default="人日")
    sp.add_argument("--start", default=None, help="开始日期 YYYY-MM-DD，默认今天")
    sp.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    sp.add_argument("--days", type=int, default=0, help="持续天数（与 --end 二选一）")
    sp.add_argument("--status", default="计划中", choices=schema.ALLOCATION_STATUS)
    sp.add_argument("--note", default="")

    sub.add_parser("res-load", help="资源负载报表：超载/闲置/冲突/人工成本")

    sp = sub.add_parser("doctor", help="开局体检：检查表/列/数据/PartDB，列出缺什么与怎么办")
    sp.add_argument("--strict", action="store_true",
                    help="存在严重问题时以退出码 2 结束（供 CI / 安装脚本使用）")

    # ── 录入闭环（第二大脑）──────────────────────────────────
    sp = sub.add_parser("intake", help="执行意图清单 JSON（低风险自动写，高风险需 --yes）")
    sp.add_argument("file", help="意图清单 JSON 路径")
    sp.add_argument("--yes", action="store_true", help="批准所有高风险改动并执行")
    sp.add_argument("--only-confirmed", action="store_true",
                    help="有待确认项时，仍先执行低风险项")
    sp.add_argument("--out", default=None)

    sp = sub.add_parser("stage", help="查看/推进项目阶段")
    sp.add_argument("project", nargs="?", default="", help="项目名；省略则列出全部")
    sp.add_argument("--to", default="", help="推进到的目标阶段")
    sp.add_argument("--table", default="项目", choices=["项目", "生产计划"])
    sp.add_argument("--note", default="", help="说明/依据")
    sp.add_argument("--actor", default="", help="记录人")
    sp.add_argument("--yes", action="store_true", help="批准跳步/回退")

    args = p.parse_args()
    cfg = load_config(args.config)
    adapter = get_adapter(cfg)
    adapter.auth()

    if args.cmd == "list":
        _print_rows(adapter.list_rows(args.table))
    elif args.cmd == "query":
        filters = {}
        for w in args.where:
            k, _, v = w.partition("=")
            filters[k] = v
        _print_rows(adapter.query(args.table, filters))
    elif args.cmd == "append":
        data = json.loads(args.json)
        rid = adapter.append_row(args.table, data)
        print(f"OK row_id={rid}")
    elif args.cmd == "update":
        adapter.update_row(args.table, args.row_id, json.loads(args.json))
        print("OK")
    elif args.cmd == "delete":
        adapter.delete_rows(args.table, args.row_ids)
        print("OK")
    elif args.cmd == "link":
        lid = schema.link_id_for(args.table, args.other)
        adapter.link(args.table, args.other, lid, args.row_id, args.other_row_ids)
        print(f"OK linked {args.table}:{args.row_id} <-> {args.other}:{args.other_row_ids}")
    elif args.cmd == "linked":
        print(json.dumps(adapter.list_linked(args.table, args.row_id, ""), ensure_ascii=False))
    elif args.cmd == "meta":
        print(json.dumps(adapter.get_metadata(args.table), ensure_ascii=False, indent=2))
    elif args.cmd == "resolve-link":
        print(schema.link_id_for(args.table, args.other) or "(local 无独立 link_id，写关联时自动解析)")
    elif args.cmd == "export-excel":
        try:
            from openpyxl import Workbook
        except Exception:
            print("导出 Excel 需要 openpyxl：pip install openpyxl", file=sys.stderr); sys.exit(1)
        wb = Workbook(); wb.remove(wb.active)
        for t in schema.TABLES:
            ws = wb.create_sheet(title=t[:31])
            rows = adapter.list_rows(t)
            if not rows:
                ws.append(["(空)"]); continue
            cols = [c for c in rows[0].keys() if c != "__row_id__"]
            ws.append(["__row_id__"] + cols)
            for r in rows:
                ws.append([r.get("__row_id__", "")] + [r.get(c, "") for c in cols])
        out = args.out if os.path.isabs(args.out) else os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
        wb.save(out)
        print(f"OK 导出到 {out}")
    elif args.cmd == "partdb-search":
        pd = get_partdb(cfg)
        if not pd:
            print("PartDB 未启用（config.yaml 中 partdb.enabled=false 或留空）。已跳过缺料检查。")
            return
        for p in pd.search_parts(args.keyword, args.limit):
            print(f"{p.get('name')} | 料号:{p.get('ipn')} | 库存:{p.get('total_instock')}")
    elif args.cmd == "partdb-shortage":
        pd = get_partdb(cfg)
        if not pd:
            print("PartDB 未启用，无法做缺料检查。")
            return
        for s in pd.shortage(args.project_id, args.qty):
            print(f"缺料: {s['name']} 料号:{s['ipn']} 需{s['need']} 现有{s['stock']} 缺口{s['gap']}")
    elif args.cmd == "apply-wizard":
        with open(args.file, encoding="utf-8") as _f:
            data = json.load(_f)
        if data.get("_wizard") != "new-project":
            print("该文件不是向导生成的「新建*.json」（缺少 _wizard 标记），已跳过。")
            sys.exit(1)
        try:
            table, row = _build_target_row(data)
        except ValueError as e:
            print(str(e)); sys.exit(1)
        _apply_and_refresh(adapter, args, table, row)
    elif args.cmd == "apply-text":
        with open(args.file, encoding="utf-8") as _f:
            text = _f.read()
        data = _parse_wizard_text(text)
        try:
            table, row = _build_target_row(data)
        except ValueError as e:
            print(str(e)); sys.exit(1)
        _apply_and_refresh(adapter, args, table, row)
    elif args.cmd == "res-add":
        cmd_res_add(adapter, args)
    elif args.cmd == "alloc-add":
        cmd_alloc_add(adapter, args)
    elif args.cmd == "res-load":
        cmd_res_load(adapter, args)
    elif args.cmd == "intake":
        cmd_intake(adapter, args)
    elif args.cmd == "stage":
        cmd_stage(adapter, args)
    elif args.cmd == "doctor":
        cmd_doctor(adapter, args)


if __name__ == "__main__":
    main()
