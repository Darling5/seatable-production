#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户到售后闭环控制平面 CLI（离线、本地、可预演）。"""
from __future__ import annotations
import argparse, json, os, tempfile
from application import contracts as C
from domain.order_to_cash import Service, OBJECT_SPECS, build_full_scenario, handoff_suggestions

HERE=os.path.dirname(os.path.abspath(__file__))
def out(data, as_json=False):
    if as_json: print(json.dumps(data, ensure_ascii=False, indent=2))
    elif isinstance(data, dict):
        for k,v in data.items(): print(f"{k}：{v if not isinstance(v,(dict,list)) else json.dumps(v,ensure_ascii=False)}")
    else: print(data)
def svc(args): return Service(args.data_dir)

def cmd_doctor(args):
    failures=[]
    if set(OBJECT_SPECS)!=set(C._ID_PREFIX): failures.append("12 类对象规格与 ID 前缀不一致")
    if not all(C.new_object_id(k) for k in OBJECT_SPECS): failures.append("ID 生成失败")
    d=args.data_dir; parent=d if os.path.isdir(d) else os.path.dirname(d) or "."
    if not os.access(parent,os.W_OK): failures.append("数据目录不可写")
    result={"passed":not failures,"objects":len(OBJECT_SPECS),"data_dir":d,"failures":failures}
    out(result,args.json); return 0 if not failures else 1

def cmd_scenario(args): out(build_full_scenario(args.customer,args.product,args.owner),args.json); return 0

def cmd_start(args):
    r=svc(args).start_case(args.customer,args.product,args.owner,args.source_event,args.contact,args.phone,args.amount,args.due_date,args.actor,args.yes)
    out(r,args.json); return 0

def cmd_advance(args):
    try:r=svc(args).advance(args.root_id,args.to,args.reason,args.actor,args.source_event,args.yes)
    except Exception as e: out({"status":"error","error":str(e)},args.json); return 1
    out(r,args.json); return 0 if r.get("status") not in ("illegal","error") else 1

def cmd_revise(args):
    try:r=svc(args).revise(args.root_id,args.kind,args.summary,args.reason,args.actor,args.source_event,args.yes)
    except Exception as e: out({"status":"error","error":str(e)},args.json); return 1
    out(r,args.json); return 0

def cmd_status(args): out(svc(args).status(args.root_id),args.json); return 0

def cmd_cases(args):
    s=svc(args); objects=s.store.read("objects")
    roots=sorted({r["root_id"] for r in objects if r.get("root_id") and r.get("object_id")==r.get("root_id")})
    cases=[]
    for rid in roots:
        v=s.verify(rid); st=s.status(rid)
        cases.append({"root_id":rid,"state":st.get("state",""),"next_action":st.get("next_action",""),
                      "objects":len(st["objects"]),"coverage":v["coverage"],"passed":v["passed"],
                      "failures":v["failures"][:3],"customer":next((o.get("summary","") for o in st["objects"] if o.get("object_type")=="customer"), "")})
    out({"cases":cases,"total":len(cases)},args.json); return 0

def cmd_report(args):
    s=svc(args); objects=s.store.read("objects")
    roots=sorted({r["root_id"] for r in objects if r.get("root_id") and r.get("object_id")==r.get("root_id")})
    rows=[]
    for rid in roots:
        v=s.verify(rid); st=s.status(rid)
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%d%%</td><td>%s</td></tr>" % (
            rid, st.get("state",""), (st.get("next_action","") or "—")[:40], len(st["objects"]),
            v["coverage"], "通过" if v["passed"] else "；".join(v["failures"][:2])))
    html=["<!DOCTYPE html><html><head><meta charset='utf-8'><title>业务闭环看板</title>",
          "<style>body{font-family:sans-serif;margin:24px}table{border-collapse:collapse;width:100%}",
          "td,th{border:1px solid #ccc;padding:6px 10px;font-size:13px}th{background:#f5f5f5}</style></head><body>",
          "<h2>客户到售后业务闭环看板</h2><p>案件 %d 个（本地控制平面，正式写入仍需人工批准）</p>" % len(roots),
          "<table><tr><th>案件 root_id</th><th>当前状态</th><th>下一步</th><th>对象数</th><th>闭环覆盖</th><th>验收</th></tr>",
          *rows, "</table></body></html>"]
    path=os.path.join(args.data_dir,"business_loop_report.html")
    os.makedirs(args.data_dir,exist_ok=True)
    with open(path,"w",encoding="utf-8") as f: f.write("\n".join(html))
    print("报告已生成：%s（案件 %d 个）" % (path,len(roots))); return 0

def cmd_verify(args):
    r=svc(args).verify(args.root_id);out(r,args.json);return 0 if r.get("passed") else 1

def common(p):
    p.add_argument("--data-dir",default=os.path.join(HERE,"data","business_loop"));p.add_argument("--json",action="store_true")
def main(argv=None):
    ap=argparse.ArgumentParser(description="客户到售后闭环控制平面")
    sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("doctor");common(p);p.set_defaults(func=cmd_doctor)
    p=sub.add_parser("scenario");p.add_argument("--customer",required=True);p.add_argument("--product",required=True);p.add_argument("--owner",default="项目经理");p.add_argument("--json",action="store_true");p.set_defaults(func=cmd_scenario)
    p=sub.add_parser("start");common(p);p.add_argument("--customer",required=True);p.add_argument("--product",required=True);p.add_argument("--owner",default="项目经理");p.add_argument("--source-event",default="");p.add_argument("--contact",default="");p.add_argument("--phone",default="");p.add_argument("--amount",type=float,default=0);p.add_argument("--due-date",default="");p.add_argument("--actor",default="automation");p.add_argument("--yes",action="store_true");p.set_defaults(func=cmd_start)
    p=sub.add_parser("advance");common(p);p.add_argument("--root-id",required=True);p.add_argument("--to",required=True,choices=C.PROJECT_STATES);p.add_argument("--reason",required=True);p.add_argument("--actor",default="automation");p.add_argument("--source-event",default="");p.add_argument("--yes",action="store_true");p.set_defaults(func=cmd_advance)
    p=sub.add_parser("revise");common(p);p.add_argument("--root-id",required=True);p.add_argument("--kind",required=True,choices=["requirement","solution","quote"]);p.add_argument("--summary",required=True);p.add_argument("--reason",required=True);p.add_argument("--actor",default="automation");p.add_argument("--source-event",default="");p.add_argument("--yes",action="store_true");p.set_defaults(func=cmd_revise)
    p=sub.add_parser("status");common(p);p.add_argument("--root-id",required=True);p.set_defaults(func=cmd_status)
    p=sub.add_parser("cases");common(p);p.set_defaults(func=cmd_cases)
    p=sub.add_parser("report");common(p);p.set_defaults(func=cmd_report)
    p=sub.add_parser("verify");common(p);p.add_argument("--root-id",required=True);p.set_defaults(func=cmd_verify)
    args=ap.parse_args(argv);return args.func(args)
if __name__=="__main__":raise SystemExit(main())
