#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""project_brain.py — 单项目「第二大脑」CLI（第二期）。

设计原则与第一期一致：**先预演，再授权，才执行**。

    # 0) 造一份授权（人亲手做的一步，等价于第一期 --grant-file）
    python project_brain.py grant --actor 老板 --hours 8 --out data/approvals/GRT-demo.json

    # 1) 预演：不写任何东西
    python project_brain.py ingest --file docs/contracts/examples/project-brain-sample.json
    python project_brain.py context --project-id PRJ-20260926-0001

    # 2) 真实写入：必须带授权
    python project_brain.py ingest --file ... --mode apply --grant-file data/approvals/GRT-demo.json

    # 3) 查询五问
    python project_brain.py query --project-id PRJ-... --question today

不带 ``--mode apply`` 一律是 preview：所有写入退化成 candidate，一行都不落盘。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from application import authorization as AUTH          # noqa: E402
from application import contracts as C                 # noqa: E402
from application.project_brain import schema as S      # noqa: E402
from application.project_brain.service import ProjectBrain, get_project_context  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.join(HERE, "data", "project_brain")


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif isinstance(data, dict):
        for k, v in data.items():
            print("%s：%s" % (k, v if not isinstance(v, (dict, list))
                              else json.dumps(v, ensure_ascii=False)))
    else:
        print(data)


def _brain(args) -> ProjectBrain:
    return ProjectBrain(root=getattr(args, "root", "") or DEFAULT_ROOT)


def _grant(args):
    path = getattr(args, "grant_file", "") or ""
    if not path:
        return None
    g = AUTH.GrantStore.load_file(path)
    if g is None:
        print("[错误] 授权文件读不到或格式不正确：%s" % path)
        raise SystemExit(2)
    return g


def _mode(args) -> str:
    return getattr(args, "mode", C.MODE_PREVIEW)


# ────────────────────────────────────────────────────────────────
def cmd_grant(args) -> int:
    """造一份人工授权文件（对应第一期 --grant-file 的格式）。"""
    tables = tuple(args.tables) if args.tables else tuple(S.TABLE_NAMES)
    actions = tuple(args.actions) if args.actions else (S.ACTION_APPEND, S.ACTION_UPDATE)
    expires = ""
    if args.hours:
        import datetime as _dt
        expires = (_dt.datetime.now() + _dt.timedelta(hours=args.hours)
                   ).isoformat(timespec="seconds")
    g = AUTH.manual_grant(tables=tables, actions=actions, actor=args.actor,
                          reason=args.reason, expires_at=expires,
                          max_uses=args.max_uses)
    out = args.out or os.path.join(HERE, "data", "approvals", "%s.json" % g.grant_id)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(g.to_dict(), f, ensure_ascii=False, indent=2)
    print("已生成授权：%s" % out)
    print("  编号 %s｜授权人 %s｜表 %s｜动作 %s｜过期 %s｜次数 %s"
          % (g.grant_id, g.actor, "、".join(tables), "、".join(actions),
             expires or "不过期", g.max_uses or "不限"))
    return 0


def cmd_doctor(args) -> int:
    from application.project_brain.store import LocalMemoryAdapter
    failures = []
    b = _brain(args)
    ad = b.store
    if not isinstance(ad, LocalMemoryAdapter):
        failures.append("适配器不是 LocalMemoryAdapter")
    try:
        ad.list_rows(S.TB_MEMORY)
    except Exception as e:                       # noqa: BLE001
        failures.append("记忆库不可读：%s" % e)
    if not os.access(os.path.dirname(b.root) or ".", os.W_OK):
        failures.append("数据目录不可写：%s" % b.root)
    print("root=%s" % b.root)
    print("契约版本=%s｜本地表=%d｜记忆类型=%d｜行动状态=%d"
          % (S.BRAIN_CONTRACT_VERSION, len(S.TABLE_NAMES),
             len(S.MEMORY_KINDS), len(S.ACTION_STATES)))
    print("结果：%s" % ("通过" if not failures else "失败 —— " + "；".join(failures)))
    return 0 if not failures else 1


def cmd_ingest(args) -> int:
    with open(args.file, encoding="utf-8") as f:
        payload = json.load(f)
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    if not messages:
        print("[错误] %s 里没有 messages" % args.file)
        return 2
    b = _brain(args)
    rep = b.ingest(messages, mode=_mode(args), grant=_grant(args),
                   actor=args.actor, run_id=args.run_id or "")
    print("模式=%s｜消息 %d（去重 %d / 落库 %d）｜新记忆 %d｜新行动 %d"
          % (_mode(args), rep["messages"], rep["duplicates"],
             rep["stored_messages"], rep["memories"], rep["actions"]))
    for r in rep["results"]:
        for kind, status, ref in r.get("results", []):
            print("   %-8s %-14s %s" % (kind, status, ref))
    return 0


def cmd_context(args) -> int:
    ctx = _brain(args).context(args.project_id, args.snapshot_id or "",
                               stale_after_hours=args.stale_hours)
    if args.json:
        _print(ctx, True)
        return 0
    print("═══ 项目上下文 %s ═══" % ctx.get("project_id"))
    print("契约 %s｜上下文版本 %s｜数据截至 %s｜快照 %s"
          % (ctx.get("contract_version"), ctx.get("version"),
             ctx.get("data_as_of"), ctx.get("snapshot_id") or "(最新)"))
    for key in ("status", "blockers", "today", "decisions", "gaps"):
        ans = (ctx.get("answers") or {}).get(key) or {}
        print("\n[%s]" % key)
        print("  %s" % ans.get("answer"))
        if ans.get("evidence_ids"):
            print("  证据：%s" % "、".join(ans["evidence_ids"]))
        print("  数据截至：%s" % ans.get("as_of"))
        for c in (ans.get("caveats") or []):
            print("  ! %s" % c)
    print("\n来源时效：")
    for f in ctx.get("source_freshness", []):
        print("  %-10s %-10s %-10s %s 条  %.1f 小时%s"
              % (f["source_system"], f["source_base"], f["source_table"],
                 f["records"], f["age_hours"] or 0,
                 "  ← 已过期" if f["stale"] else ""))
    return 0


def cmd_query(args) -> int:
    ans = _brain(args).query(args.project_id, args.question,
                             snapshot_id=args.snapshot_id or "")
    if args.json:
        _print(ans, True)
        return 0
    print(ans.get("answer") or ans.get("message") or ans)
    if ans.get("evidence_ids"):
        print("证据：%s" % "、".join(ans["evidence_ids"]))
    print("数据截至：%s" % ans.get("as_of"))
    return 0


def cmd_snapshot(args) -> int:
    r = _brain(args).snapshot(args.project_id, mode=_mode(args),
                              grant=_grant(args),
                              snapshot_id=args.snapshot_id or "")
    print("快照 %s：%s %s" % (r.get("snapshot_id"), r.get("status"),
                              r.get("path") or r.get("message") or ""))
    return 0 if r.get("status") in ("written", "candidate") else 1


def cmd_actions(args) -> int:
    b = _brain(args)
    rows = [a for a in b.store.list_rows(S.TB_ACTION)
            if not args.project_id or a.get("project_id") == args.project_id]
    if args.json:
        _print(rows, True)
        return 0
    print("%-22s %-10s %-10s %-8s %-12s %s" % ("action_id", "状态", "类型", "负责人", "截止", "标题"))
    print("-" * 96)
    for a in sorted(rows, key=lambda r: str(r.get("due_date") or "9999")):
        print("%-22s %-10s %-10s %-8s %-12s %s"
              % (a.get("action_id"), a.get("status_cn"), a.get("kind_cn"),
                 a.get("owner") or "-", a.get("due_date") or "-",
                 str(a.get("title"))[:34]))
    print("\n共 %d 条（其中在办 %d）"
          % (len(rows), sum(1 for a in rows if a.get("status") in S.OPEN_STATES)))
    return 0


def cmd_transition(args) -> int:
    b = _brain(args)
    r = b.transition(args.action_id, args.to, reason=args.reason,
                     mode=_mode(args), grant=_grant(args), actor=args.actor,
                     evidence_id=args.evidence_id or "",
                     blocked_reason=args.blocked_reason or "",
                     blocked_owner=args.blocked_owner or "",
                     expected_version=args.expected_version)
    print("状态=%s %s" % (r.get("status"), r.get("message") or ""))
    if r.get("reason_code"):
        print("原因码=%s" % r["reason_code"])
    return 0 if r.get("status") in ("written", "candidate") else 1


def cmd_close(args) -> int:
    b = _brain(args)
    r = b.close_action(args.action_id, mode=_mode(args), grant=_grant(args),
                       actor=args.actor,
                       acceptance_evidence_id=args.evidence_id or "",
                       reason=args.reason or "")
    print("状态=%s %s" % (r.get("status"), r.get("message") or ""))
    if r.get("reason_code"):
        print("原因码=%s" % r["reason_code"])
    return 0 if r.get("status") in ("written", "candidate") else 1


def cmd_rollover(args) -> int:
    r = _brain(args).rollover(args.as_of, mode=_mode(args), grant=_grant(args),
                              actor=args.actor, project_id=args.project_id or "")
    print("跨天滚动（截至 %s）：%d 条" % (r["as_of"], r["count"]))
    for x in r["rolled"]:
        print("  %-22s %-8s 原期限 %s｜逾期 %s 天"
              % (x["action_id"], x["status"], x["due_date_original"],
                 x["overdue_days"]))
    return 0


def cmd_reminders(args) -> int:
    b = _brain(args)
    if args.mark:
        rid, _, state = args.mark.partition("=")
        r = b.mark_reminder(rid.strip(), ok=(state.strip() != "failed"),
                            mode=_mode(args), grant=_grant(args),
                            error=args.error or "", actor=args.actor)
        print("提醒 %s → %s（第 %s 次）" % (rid.strip(), r.get("status"),
                                            r.get("attempts")))
        return 0 if r.get("status") in ("written", "candidate") else 1
    r = b.reminders(args.as_of, mode=_mode(args), grant=_grant(args),
                    actor=args.actor, project_id=args.project_id or "")
    print("截至 %s：新建提醒 %d 条，去重跳过 %d 条"
          % (r["as_of"], r["created_count"], r["skipped_count"]))
    for c in r["created"]:
        print("  %-12s %-8s %-22s %s" % (c["reminder_id"], c["kind_cn"],
                                         c["action_id"], c["title"][:30]))
    failed = b.failed_reminders()
    if failed:
        print("\n待重试的失败提醒 %d 条：" % len(failed))
        for f in failed:
            print("  %-12s 第 %s 次失败：%s" % (f.get(S.F_ROW_ID),
                                                f.get("attempts"), f.get("last_error")))
    return 0


def cmd_evidence(args) -> int:
    b = _brain(args)
    r = b.add_evidence(project_id=args.project_id, kind=args.kind,
                       claim_type=args.claim_type, mode=_mode(args),
                       grant=_grant(args), action_id=args.action_id or "",
                       summary=args.summary or "", subject=args.subject or "",
                       quantity=args.quantity or "", quantity_total=args.total or "",
                       occurred_at=args.occurred_at or "",
                       source_system=args.source_system or "manual",
                       source_table=args.source_table or "",
                       source_row_id=args.source_row_id or "",
                       source_message_id=args.source_message_id or "",
                       verified=args.verified, verified_by=args.verified_by or "",
                       criteria_met=True if args.criteria_met else "",
                       actor=args.actor)
    print("证据 %s：%s %s" % (r["evidence_id"], r["status"], r.get("message") or ""))
    return 0 if r["status"] in ("written", "candidate") else 1


def cmd_verify_obs(args) -> int:
    b = _brain(args)
    r = b.verify_observation(args.observation_id, evidence_id=args.evidence_id,
                             verified_by=args.verified_by, mode=_mode(args),
                             grant=_grant(args), actor=args.actor,
                             rationale=args.reason or "")
    print("%s %s" % (r.get("status"), r.get("message") or ""))
    if r.get("fact_id"):
        print("事实编号=%s" % r["fact_id"])
    return 0 if r.get("status") in ("written", "candidate") else 1


def cmd_correct(args) -> int:
    b = _brain(args)
    r = b.correct_memory(args.memory_id, new_text=args.text or "",
                         new_value=args.value or "", reason=args.reason or "",
                         mode=_mode(args), grant=_grant(args), actor=args.actor)
    print("%s %s" % (r.get("status"), r.get("message") or ""))
    if r.get("memory_id"):
        print("新记忆=%s" % r["memory_id"])
    return 0 if r.get("status") in ("written", "candidate") else 1


def cmd_replay(args) -> int:
    """回放一份合成项目（离线验收用；样例见 docs/contracts/examples/）。"""
    from application.project_brain.scenario import replay
    with open(args.file, encoding="utf-8") as f:
        scenario = json.load(f)
    b = _brain(args)
    rep = replay(b, scenario, mode=_mode(args), grant=_grant(args),
                 actor=args.actor, run_id=args.run_id or "")
    print("合成项目回放：%s  mode=%s" % (rep["project_id"], rep["mode"]))
    for e in rep["log"]:
        flag = {"ok": "✓", "written": "✓", "candidate": "⊘", "duplicate": "⊘",
                "blocked": "⛔", "rejected": "⛔", "error": "✗",
                "failed": "✗"}.get(e.get("status"), "·")
        print("  %s %-16s %-12s %s" % (flag, e.get("op"), e.get("status", ""),
                                       str(e.get("detail"))[:70]))
    print("结果：%s" % ("全部步骤通过" if rep["ok"] else "存在失败步骤"))
    return 0 if rep["ok"] else 1


# ────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="单项目第二大脑（第二期）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, grant=True, mode=True):
        p.add_argument("--root", default="", help="记忆库目录（默认 data/project_brain）")
        p.add_argument("--json", action="store_true")
        if mode:
            p.add_argument("--mode", default=C.MODE_PREVIEW,
                           choices=[C.MODE_PREVIEW, C.MODE_APPLY])
        if grant:
            p.add_argument("--grant-file", default="", help="第一期授权文件路径")
        p.add_argument("--actor", default="automation")

    p = sub.add_parser("grant", help="生成授权文件")
    common(p, grant=False, mode=False)
    p.add_argument("--tables", nargs="*", default=[])
    p.add_argument("--actions", nargs="*", default=[])
    p.add_argument("--hours", type=float, default=8)
    p.add_argument("--max-uses", type=int, default=0)
    p.add_argument("--reason", default="第二大脑写入授权")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser("doctor", help="自检")
    common(p, grant=False, mode=False)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("ingest", help="采集来源消息（含 AI 已抽取的条目）")
    common(p)
    p.add_argument("--file", required=True)
    p.add_argument("--run-id", default="")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("replay", help="回放合成项目 JSON（离线验收）")
    common(p)
    p.add_argument("--file", required=True)
    p.add_argument("--run-id", default="")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("context", help="项目统一上下文")
    common(p, grant=False)
    p.add_argument("--project-id", required=True)
    p.add_argument("--snapshot-id", default="")
    p.add_argument("--stale-hours", type=int, default=S.DEFAULT_STALE_AFTER_HOURS)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("query", help="项目五问")
    common(p, grant=False)
    p.add_argument("--project-id", required=True)
    p.add_argument("--question", default="status",
                   choices=["status", "blockers", "today", "decisions", "gaps"])
    p.add_argument("--snapshot-id", default="")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("snapshot", help="固化快照")
    common(p)
    p.add_argument("--project-id", required=True)
    p.add_argument("--snapshot-id", default="")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("actions", help="行动列表")
    common(p, grant=False, mode=False)
    p.add_argument("--project-id", default="")
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("transition", help="行动状态迁移")
    common(p)
    p.add_argument("--action-id", required=True)
    p.add_argument("--to", required=True, choices=list(S.ACTION_STATES))
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence-id", default="")
    p.add_argument("--blocked-reason", default="")
    p.add_argument("--blocked-owner", default="")
    p.add_argument("--expected-version", type=int, default=None)
    p.set_defaults(func=cmd_transition)

    p = sub.add_parser("close", help="关闭行动（必须带验收证据）")
    common(p)
    p.add_argument("--action-id", required=True)
    p.add_argument("--evidence-id", required=True)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("rollover", help="跨天滚动（保留原期限）")
    common(p)
    p.add_argument("--as-of", required=True)
    p.add_argument("--project-id", default="")
    p.set_defaults(func=cmd_rollover)

    p = sub.add_parser("reminders", help="生成/标记提醒")
    common(p)
    p.add_argument("--as-of", required=True)
    p.add_argument("--project-id", default="")
    p.add_argument("--mark", default="", help="<reminder_id>=sent|failed")
    p.add_argument("--error", default="")
    p.set_defaults(func=cmd_reminders)

    p = sub.add_parser("evidence", help="登记证据")
    common(p)
    p.add_argument("--project-id", required=True)
    p.add_argument("--kind", required=True, choices=list(S.EVIDENCE_KINDS))
    p.add_argument("--claim-type", required=True, choices=list(S.CLAIM_TYPES))
    p.add_argument("--action-id", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--subject", default="")
    p.add_argument("--quantity", default="")
    p.add_argument("--total", default="")
    p.add_argument("--occurred-at", default="")
    p.add_argument("--source-system", default="manual")
    p.add_argument("--source-table", default="")
    p.add_argument("--source-row-id", default="")
    p.add_argument("--source-message-id", default="")
    p.add_argument("--verified", action="store_true")
    p.add_argument("--verified-by", default="")
    p.add_argument("--criteria-met", action="store_true")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("verify-observation", help="观察升格为已核实事实")
    common(p)
    p.add_argument("--observation-id", required=True)
    p.add_argument("--evidence-id", required=True)
    p.add_argument("--verified-by", required=True)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_verify_obs)

    p = sub.add_parser("correct", help="追加纠正（不覆盖历史）")
    common(p)
    p.add_argument("--memory-id", required=True)
    p.add_argument("--text", default="")
    p.add_argument("--value", default="")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_correct)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
