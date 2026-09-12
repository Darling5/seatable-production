#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""won_deal.py — 赢单转化引擎（v2.0 闭环：线索 → 商机落地 → 转生产立项）。

打通 avatar-loop-v2 设计的 §5 ⑤⑥ 两步：赢单确认后，一条命令把 CRM 线索
转成 production Base 的「客户档案 + 合同信息 + 项目立项」，完成
「来单→报价→跟进→赢单→立项」的前半段闭环。

流程（两段式，延续「列出方案你核对」宗旨）：
  plan    只读：读 CRM 线索现状 + 本地快照查重，生成三表写入方案（完整展示）
  apply   写入：必须 --yes；逐表写入→读回验证→写台账，任一步失败即停

写入规则（继承 crm_dispatch 2026-09-11 实操沉淀）：
  - 行写入用中文列名（列 key 会 HTTP 200 静默失败）
  - 查重：客户档案表按客户名称；项目按「项目」名——已存在则复用不重建
  - 写入后读回验证，读回值≠写入值视为失败
  - 全程台账 data/won_deal_ledger.csv，可事后核对

用法：
  python won_deal.py plan --customer "云南亚雄科技" \
      --product "天然气重大危险源人员定位" --amount 464250 \
      --delivery-days 90 --payment "50%,30%,20%"
  python won_deal.py apply --yes ...（同参数）
  python won_deal.py ledger
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LEDGER_FILE = os.path.join(HERE, "data", "won_deal_ledger.csv")
LEDGER_FIELDS = ["时间", "动作", "客户名称", "row_id", "表格", "内容摘要", "备注"]

CRM_LEAD_TABLE = "销售线索表"
PROFILE_TABLE = "客户档案表"
CONTRACT_TABLE = "合同信息表"
PROJECT_TABLE = "项目"

# 可写字段（超集；写入时按 --data 补充，未知字段拒绝）
PROFILE_FIELDS = {"客户名称", "客户类型", "联系人", "联系方式", "客户地址",
                  "发票抬头", "纳税人识别号", "发票地址", "电话",
                  "开户名称", "开户银行", "银行账号"}
CONTRACT_FIELDS = {"合同编号", "合同上传时间", "合同上传日期", "关联销售线索",
                   "合同金额", "回款金额", "回款占比", "是否具备开票条件", "已开票金额"}
PROJECT_FIELDS = {"项目编号", "项目", "产品需求", "合同", "创建者", "状态", "阶段",
                  "合同总价", "签订日期", "下单付", "收货付", "验收付", "实收", "待收",
                  "合同交期"}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ledger_append(action: str, customer: str, row_id: str, table: str,
                   summary: str, note: str = "") -> None:
    os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
    new_file = not os.path.exists(LEDGER_FILE)
    with open(LEDGER_FILE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(LEDGER_FIELDS)
        w.writerow([_now(), action, customer, row_id, table, summary[:200], note])


def _get_adapter(base: str):
    from adapters.factory import load_config, get_adapter
    cfg = load_config()
    a = get_adapter(cfg, base_name=base)
    a.auth()
    return a


def _clean(data: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    unknown = {k for k in (data or {}) if k not in allowed}
    if unknown:
        raise ValueError("字段不属于「%s」可写范围：%s" %
                         (", ".join(sorted(allowed & set())) or "?", "、".join(sorted(unknown))))
    return {k: v for k, v in (data or {}).items() if v not in (None, "")}


def _parse_payments(spec: str, amount: float) -> dict[str, Any]:
    """'50%,30%,20%' → 下单付/收货付/验收付 + 待收。也接受 '232125,139275,92850'。"""
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if len(parts) != 3:
        raise ValueError("--payment 需三段（下单,收货,验收），如 '50%,30%,20%'")
    vals = []
    for p in parts:
        vals.append(round(amount * (float(p.rstrip("%")) / 100.0), 2)
                    if p.endswith("%") else float(p))
    total = round(sum(vals), 2)
    if total != float(amount):
        raise ValueError("付款分段合计 %s ≠ 合同总价 %s" % (total, amount))
    return {"下单付": vals[0], "收货付": vals[1], "验收付": vals[2],
            "实收": 0, "待收": float(amount)}


def _find_by(table: str, rows: list[dict], key: str, value: str):
    for r in rows:
        if str(r.get(key, "")).strip() == str(value).strip():
            return r
    return None


def _next_project_no(rows: list[dict]) -> str:
    """项目编号 YYYYMMDD-NNN：同日已有的最大序号 +1。"""
    today = dt.date.today().strftime("%Y%m%d")
    n = 0
    for r in rows:
        no = str(r.get("项目编号", "") or "")
        if no.startswith(today + "-"):
            try:
                n = max(n, int(no.rsplit("-", 1)[1]))
            except (ValueError, IndexError):
                pass
    return "%s-%03d" % (today, n + 1)


def _next_contract_no(rows: list[dict]) -> str:
    """合同编号 HTBH-XXXX：既有最大序号 +1（本地快照推断，写入后读回校准）。"""
    n = 0
    for r in rows:
        no = str(r.get("合同编号", "") or "")
        if no.startswith("HTBH-"):
            try:
                n = max(n, int(no[5:]))
            except ValueError:
                pass
    return "HTBH-%04d" % (n + 1)


def build_plan(customer: str, product: str, amount: float, delivery_days: int,
               payment: str, contact: str = "", phone: str = "",
               extra_profile: Mapping[str, Any] | None = None,
               sign_date: str = "") -> dict[str, Any]:
    """纯函数：生成三表写入方案（不碰任何 adapter，可离线测试）。"""
    today = dt.date.today()
    sign = sign_date or today.isoformat()
    due = (today + dt.timedelta(days=delivery_days)).isoformat()
    pays = _parse_payments(payment, amount)

    profile = {"客户名称": customer, "客户类型": "企业"}
    if contact:
        profile["联系人"] = contact
    if phone:
        profile["联系方式"] = phone
    if extra_profile:
        profile.update(_clean(extra_profile, PROFILE_FIELDS))

    contract = {
        "合同编号": None,  # apply 时按云端序号生成
        "合同上传时间": _now(),
        "合同上传日期": sign,
        "关联销售线索": customer,
        "合同金额": amount,
        "回款金额": 0,
        "回款占比": 0,
        "是否具备开票条件": "否",
    }

    project = {
        "项目编号": None,  # apply 时按云端序号生成
        "项目": "%s-%s" % (customer, product),
        "创建者": "替身系统",
        "状态": "计划中",
        "阶段": "立项",
        "合同总价": amount,
        "签订日期": sign,
        "合同交期": due,
    }
    project.update(pays)
    return {"profile": profile, "contract": contract, "project": project}


def render_plan(plan: dict[str, Any], reuse: dict[str, Any]) -> str:
    """给人看的确认清单。"""
    L = ["【赢单转化写入方案】（apply --yes 前不会写入任何一条）", ""]
    if reuse.get("profile"):
        L.append("· 客户档案：已存在（%s），复用不重建" % reuse["profile"].get("客户名称"))
    else:
        L.append("· 客户档案表 新建：%s" % json.dumps(
            {k: v for k, v in plan["profile"].items()}, ensure_ascii=False))
    if reuse.get("project"):
        L.append("· 项目：已存在（%s），复用不重建" % reuse["project"].get("项目"))
    else:
        L.append("· 合同信息表 新建：金额 %s｜%s" % (
            plan["contract"]["合同金额"], plan["contract"]["合同上传日期"]))
        L.append("· 项目表 新建：%s｜总价 %s｜交期 %s｜待收 %s" % (
            plan["project"]["项目"], plan["project"]["合同总价"],
            plan["project"]["合同交期"], plan["project"]["待收"]))
        L.append("  付款：下单 %s / 收货 %s / 验收 %s" % (
            plan["project"]["下单付"], plan["project"]["收货付"], plan["project"]["验收付"]))
    L.append("")
    L.append("确认无误后执行：python won_deal.py apply --yes ...（同参数）")
    return "\n".join(L)


def _verify_write(a, table: str, row_id: str, data: Mapping[str, Any],
                  customer: str) -> None:
    """写入读回验证（SKILL.md §12 教训：HTTP 200 ≠ 落库）。"""
    row = None
    for r in a.list_rows(table):
        if str(r.get("__row_id__")) == str(row_id):
            row = r
            break
    if row is None:
        raise RuntimeError("读回验证失败：「%s」%s 找不到刚写入的行" % (table, row_id))
    for k, v in data.items():
        got = str(row.get(k, "")).strip()
        want = str(v).strip()
        if got != want:
            raise RuntimeError("读回验证失败：「%s」%s 列 %s：写入 %r 读回 %r"
                               % (table, row_id, k, want, got))


def execute(a, plan: dict[str, Any], customer: str) -> list[dict[str, Any]]:
    """执行写入：查重 → 建档案 → 建合同 → 建项目 → 读回验证 → 台账。"""
    results = []

    # 1) 客户档案（查重：客户名称）
    profiles = a.list_rows(PROFILE_TABLE)
    hit = _find_by(PROFILE_TABLE, profiles, "客户名称", customer)
    if hit:
        results.append({"table": PROFILE_TABLE, "action": "reused", "row_id": hit.get("__row_id__"),
                        "note": "客户档案已存在，复用"})
    else:
        rid = a.append_row(PROFILE_TABLE, plan["profile"])
        _verify_write(a, PROFILE_TABLE, rid, plan["profile"], customer)
        _ledger_append("create_profile", customer, rid, PROFILE_TABLE,
                       json.dumps(plan["profile"], ensure_ascii=False))
        results.append({"table": PROFILE_TABLE, "action": "created", "row_id": rid})

    # 2) 合同信息（编号按云端行序生成，查重：关联销售线索 + 合同金额）
    contracts = a.list_rows(CONTRACT_TABLE)
    dup = _find_by(CONTRACT_TABLE, contracts, "关联销售线索", customer)
    if dup and str(dup.get("合同金额", "")).strip() == str(plan["contract"]["合同金额"]):
        results.append({"table": CONTRACT_TABLE, "action": "reused", "row_id": dup.get("__row_id__"),
                        "note": "同客户同金额合同已存在，复用"})
    else:
        cdata = dict(plan["contract"])
        cdata["合同编号"] = _next_contract_no(contracts)
        rid = a.append_row(CONTRACT_TABLE, cdata)
        _verify_write(a, CONTRACT_TABLE, rid, cdata, customer)
        _ledger_append("create_contract", customer, rid, CONTRACT_TABLE,
                       "%s 金额 %s" % (cdata["合同编号"], cdata["合同金额"]))
        results.append({"table": CONTRACT_TABLE, "action": "created", "row_id": rid,
                        "合同编号": cdata["合同编号"]})

    # 3) 项目立项（查重：项目名）
    projects = a.list_rows(PROJECT_TABLE)
    pname = plan["project"]["项目"]
    hit = _find_by(PROJECT_TABLE, projects, "项目", pname)
    if hit:
        results.append({"table": PROJECT_TABLE, "action": "reused", "row_id": hit.get("__row_id__"),
                        "note": "同名项目已存在，复用"})
    else:
        pdata = dict(plan["project"])
        pdata["项目编号"] = _next_project_no(projects)
        rid = a.append_row(PROJECT_TABLE, pdata)
        _verify_write(a, PROJECT_TABLE, rid, pdata, customer)
        _ledger_append("create_project", customer, rid, PROJECT_TABLE,
                       "%s 总价 %s 交期 %s" % (pdata["项目编号"], pdata["合同总价"], pdata["合同交期"]))
        results.append({"table": PROJECT_TABLE, "action": "created", "row_id": rid,
                        "项目编号": pdata["项目编号"]})
    return results


def cmd_plan(args) -> int:
    plan = build_plan(args.customer, args.product, args.amount, args.delivery_days,
                      args.payment, args.contact, args.phone,
                      json.loads(args.profile) if args.profile else None,
                      args.sign_date)
    reuse: dict[str, Any] = {}
    if not args.offline:
        a = _get_adapter("production")
        reuse["profile"] = _find_by(PROFILE_TABLE, a.list_rows(PROFILE_TABLE),
                                    "客户名称", args.customer)
        reuse["project"] = _find_by(PROJECT_TABLE, a.list_rows(PROJECT_TABLE),
                                    "项目", plan["project"]["项目"])
    print(render_plan(plan, reuse))
    if args.json:
        print(json.dumps({"plan": plan, "reuse": {k: bool(v) for k, v in reuse.items()}},
                         ensure_ascii=False, indent=2))
    return 0


def cmd_apply(args) -> int:
    if not args.yes:
        print("[拒绝] apply 必须带 --yes（先把 plan 的方案给人核对）")
        return 1
    plan = build_plan(args.customer, args.product, args.amount, args.delivery_days,
                      args.payment, args.contact, args.phone,
                      json.loads(args.profile) if args.profile else None,
                      args.sign_date)
    a = _get_adapter("production")
    # 先查重展示，再执行
    profiles = a.list_rows(PROFILE_TABLE)
    reuse = {"profile": _find_by(PROFILE_TABLE, profiles, "客户名称", args.customer),
             "project": _find_by(PROJECT_TABLE, a.list_rows(PROJECT_TABLE),
                                 "项目", plan["project"]["项目"])}
    print(render_plan(plan, reuse))
    results = execute(a, plan, args.customer)
    print("\n【执行结果】")
    for r in results:
        print("  · %-6s %-12s %s" % (r["action"], r["table"], r.get("row_id", "")))
    _ledger_append("won_deal_run", args.customer, "", "",
                   "apply 完成：%s" % json.dumps(
                       [{k: str(v) for k, v in r.items()} for r in results], ensure_ascii=False))
    return 0


def cmd_ledger(_args) -> int:
    if not os.path.exists(LEDGER_FILE):
        print("（台账为空）")
        return 0
    with open(LEDGER_FILE, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    print("赢单转化台账（%d 条）：" % (len(rows) - 1))
    for r in rows:
        print(" | ".join(c[:40] for c in r))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="赢单转化引擎（CRM 线索 → 客户档案+合同+项目立项）")
    sub = ap.add_subparsers(dest="command", required=True)

    for name, help_, func in (
            ("plan", "生成写入方案（只读）", cmd_plan),
            ("apply", "执行写入（必须 --yes）", cmd_apply)):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--customer", required=True, help="客户名称（CRM 线索同名）")
        p.add_argument("--product", required=True, help="产品/项目简称，如「天然气人员定位」")
        p.add_argument("--amount", required=True, type=float, help="合同总价")
        p.add_argument("--delivery-days", required=True, type=int, help="交期天数（自然日）")
        p.add_argument("--payment", required=True,
                       help="付款分段 '50%,30%,20%' 或绝对值 '232125,139275,92850'")
        p.add_argument("--contact", default="", help="联系人（默认从 CRM 线索取）")
        p.add_argument("--phone", default="", help="联系方式（默认从 CRM 线索取）")
        p.add_argument("--profile", default=None,
                       help='补充客户档案 JSON，如 \'{"客户地址":"...","发票抬头":"..."}\'')
        p.add_argument("--sign-date", default="", help="签订日期 YYYY-MM-DD（默认今天）")
        if name == "plan":
            p.add_argument("--offline", action="store_true",
                           help="不连云端，纯本地生成方案（跳过查重）")
            p.add_argument("--json", action="store_true", help="附加输出 JSON")
        if name == "apply":
            p.add_argument("--yes", action="store_true", help="显式确认执行")
        p.set_defaults(func=func)

    p = sub.add_parser("ledger", help="查看写入台账")
    p.set_defaults(func=cmd_ledger)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
