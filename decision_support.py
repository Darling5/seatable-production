#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""decision_support.py — 第三期「项目决策辅助」命令行入口。

只读、离线。四条命令对应四个问题：

  python decision_support.py analyze   --snapshot <file>      这活什么时候完？卡在哪？
  python decision_support.py compare   --snapshot <file>      换个条件能不能好一点？要谁点头？
  python decision_support.py delay     --snapshot <file> --step <id> --hours 8
                                                              A 晚 8 小时，会连累谁？
  python decision_support.py explain   --snapshot <file> --step <id>
                                                              凭什么算成这天？
  python decision_support.py backtest  --records <file>       以前预测准不准？
  python decision_support.py align                            两期契约对得上吗？
  python decision_support.py demo                             用内置合成样例跑一遍全链路

约定：
  · 所有命令都不写任何业务表；compare 会明确列出「执行意图（未授权、未执行）」；
  · --json 输出机器可读结果，默认输出人读摘要；
  · 退出码：0 正常；1 输入/参数错误；2 排程不可用（图或日历有硬问题）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from application.decision_support import schema as S                    # noqa: E402
from application.decision_support import alignment as ALIGN             # noqa: E402
from application.decision_support.backtest import backtest              # noqa: E402
from application.decision_support.service import DecisionSupport        # noqa: E402

EXAMPLES = os.path.join(HERE, "docs", "contracts", "examples")
DEFAULT_SNAPSHOT = os.path.join(EXAMPLES, "decision-support-planning-sample.json")
DEFAULT_CONTEXT = os.path.join(EXAMPLES, "decision-support-context-mock.json")


# ────────────────────────── 载入 ──────────────────────────
def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_ds(args) -> DecisionSupport:
    snap = _load_json(args.snapshot or DEFAULT_SNAPSHOT)
    ctx = {}
    cpath = getattr(args, "context", "") or DEFAULT_CONTEXT
    if cpath and os.path.exists(cpath):
        ctx = _load_json(cpath)
    return DecisionSupport(snap, context=ctx)


# ────────────────────────── 打印 ──────────────────────────
def print_analyze(res: dict) -> None:
    print("=" * 74)
    print("交付依赖分析 decision_support analyze · 契约 %s · 规则 %s"
          % (res["contract_version"], res["rules_version"]))
    print("=" * 74)
    print("项目 %s · 快照 %s · 数据截至 %s"
          % (res["project_id"], res["snapshot_id"], res["data_as_of"]))
    cal = res["calendar"]
    print("日历：%s（每天 %.1f 小时）" % (cal["name"], cal["hours_per_day"]))
    print("\n【三种日期】合同 / 内部计划 / 预测 —— 分开放，谁也不覆盖谁")
    for pid, d in sorted(res["dates"].items()):
        fc = d.get("resource_feasible_date") or d.get("forecast_date")
        st = d.get("resource_feasible_status") or d.get("forecast_status")
        tag = {"determined": "确定", "conditional": "有条件", "undetermined": "不可确定"}.get(st, st)
        print("  %-20s %s" % (pid, d.get("name") or ""))
        print("     合同日期 %-12s 内部计划 %-12s 预测 %-12s [%s]"
              % (d.get("contract_date") or "—", d.get("internal_plan_date") or "—",
                 fc or "—", tag))
        if d.get("delay_vs_contract_days") is not None:
            print("     预测 vs 合同：%+d 天" % d["delay_vs_contract_days"])
        for r in d.get("undetermined_reasons") or []:
            print("     ⚠ %s" % r.get("message"))

    cpm = res["cpm"]
    print("\n【关键路径】口径：仅前置关系（不含资源约束）")
    print("  项目完成（仅前置口径）：%s" % (cpm.get("project_finish") or "不可确定"))
    print("  关键路径：%s" % (" → ".join(cpm.get("critical_path") or []) or "无"))
    rf = res["resource_feasible"]
    print("\n【资源可行排程】方法：%s" % (rf.get("method_cn") or ""))
    for p in rf.get("plans") or []:
        print("  %-20s 资源可行完成 %s" % (p["plan_id"], p.get("forecast_date") or "不可确定"))
    for c in rf.get("conflicts_resolved") or []:
        print("  ↷ %s 原可 %s 开始，%s → %s"
              % (c["step_id"], c["shifted_from"], c["why"], c["start"]))
    for p in rf.get("protected_steps") or []:
        print("  🔒 %s %s（%s）" % (p["step_id"], p["status_cn"], p["say"]))

    if res.get("gaps"):
        print("\n【缺口 / 不可确定的原因】")
        for g in res["gaps"]:
            print("  · [%s] %s" % (g.get("code"), g.get("message")))
    if res.get("caveats"):
        print("\n【保留意见（来自二期上下文的来源时效）】")
        for c in res["caveats"]:
            print("  · %s" % c.get("message"))
    print("\n声明：%s" % S.SCHEDULE_DISCLAIMER)
    print("%s\n" % S.AUTH_NOTE)


def print_compare(res: dict) -> None:
    print("=" * 74)
    print("方案比较 decision_support compare · 基准 %s" % res.get("baseline_id"))
    print("=" * 74)
    for sc in res["scenarios"]:
        print("\n━━ %s %s%s" % (sc["scenario_id"], sc["name"],
                                "（基准）" if sc["is_baseline"] else ""))
        for m in sc["mutations"]:
            print("   变更：%s" % m.get("say"))
        print("   完成时间：%s" % sc["completion_text"])
        dc = sc["delta_cost"]
        print("   增量成本：%.2f 元（%s）%s"
              % (dc["amount"], "合成假设" if dc["is_synthetic"] else "实际",
                 "" if dc["complete"] else "  ← 不完整，有项目待核实"))
        if sc["affected_projects"]:
            print("   影响项目：%s" % "；".join(
                "%s %+d 天（%s → %s）" % (a["plan_id"], a["delta_days"], a["from"], a["to"])
                for a in sc["affected_projects"]))
        else:
            print("   影响项目：无（相对基准没有计划日期变化）")
        if sc["open_conditions"]:
            print("   待核实条件：")
            for c in sc["open_conditions"]:
                print("     ? %s" % c.get("message"))
        if sc["required_approvals"]:
            print("   所需批准：")
            for a in sc["required_approvals"]:
                print("     ✍ %s（%s）：%s" % (a["what"], a["who"], a["why"]))
    led = res["simulation_ledger"]
    print("\n【模拟台账】执行意图 %d 条，其中未授权 %d 条，已执行 %d 条；写入端被调用 %d 次"
          % (led["intents_total"], led["rejected_total"], led["executed_total"],
             led["writer_calls"]))
    print("  %s" % S.SCHEDULE_DISCLAIMER)
    print("  %s\n" % res["authorization_note"])


def print_delay(res: dict) -> None:
    if not res.get("ok"):
        print("延期模拟不可用：%s" % res.get("reason"))
        for i in res.get("issues") or []:
            print("  · %s" % i.get("message"))
        return
    print("=" * 74)
    print("延期传播：%s 延 %s 小时" % (res["step_id"], res["extra_hours"]))
    print("=" * 74)
    print("依据：%s\n" % res["basis"])
    if not res["affected_steps"]:
        print("没有任何工序被推后（下游松弛足够吸收）。")
    for a in res["affected_steps"]:
        mark = "（被松弛吸收，完成日未变）" if a["absorbed_by_slack"] else ""
        print("  %-14s %-16s 开始 %+d 天 · 完成 %+d 天 %s"
              % (a["step_id"], a["name"], a["delta_start_days"] or 0,
                 a["delta_finish_days"] or 0, mark))
    print("\n受影响计划：")
    for p in res["affected_plans"]:
        print("  %-20s %s → %s（%+d 天）"
              % (p["plan_id"], p["old_forecast"], p["new_forecast"], p["delta_days"]))
    print()


def print_explain(res: dict) -> None:
    if not res.get("ok"):
        print("无法解释：%s" % res.get("message"))
        for i in res.get("issues") or []:
            print("  · %s" % i.get("message"))
        return
    for e in res["explained"]:
        print("=" * 74)
        print("%s %s（%s）" % (e["step_id"], e["name"], e["status"]))
        print("=" * 74)
        d = e["dates"]
        print("  合同 %s · 内部计划 %s · 预测 %s [%s]"
              % (d["contract_date"] or "—", d["internal_plan_date"] or "—",
                 d["forecast_date"] or "—", d["forecast_status"]))
        print("  推理链：")
        for r in e["reasoning"]:
            print("    · %s：%s" % (r["what"], r["why"]))
        print()


def print_backtest(res: dict) -> None:
    print("=" * 74)
    print("预测复盘 decision_support backtest · 契约 %s" % res["contract_version"])
    print("=" * 74)
    print("口径来源：%s" % res["verdict_source"])
    print("防未来信息：%s" % ("通过（无泄漏）" if res["no_lookahead_ok"]
                              else "发现 %d 处泄漏" % len(res["no_lookahead_violations"])))
    for v in res["no_lookahead_violations"]:
        print("  ⚠ %s" % v.get("message"))
    s = res["summary"]
    print("\n记录 %d 条：可复盘 %d / 待实际结果 %d / 不可确定 %d"
          % (s["n_records"], s["n_reviewed"], s["n_pending"], s["n_undetermined"]))
    print("%s" % s["message"])
    if s["accuracy_status"] == "ok":
        print("  MAE %.2f 天 · 偏差 %+.2f 天（正=偏乐观）· 命中率 %.1f%%（±%d 天）"
              % (s["mae_days"], s["bias_days"], s["hit_rate"] * 100, s["hit_tolerance_days"]))
    b = res["baseline"]
    print("\n历史中位工期基线：%s 小时（样本 %s 条，%s）"
          % (b.get("value_hours"), b.get("n_samples"), b.get("status")))
    if res["foresee_alignment"]:
        print("与 foresee 口径对齐：%s" % res["foresee_alignment"])
    print()


# ────────────────────────── 命令 ──────────────────────────
def cmd_analyze(args) -> int:
    ds = _make_ds(args)
    res = ds.analyze()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_analyze(res)
    return 0 if res["cpm"].get("critical_path") or res["cpm"].get("project_finish") else 2


def cmd_compare(args) -> int:
    ds = _make_ds(args)
    cost_model = _load_json(args.cost_model) if getattr(args, "cost_model", "") else None
    if cost_model is None:
        cost_model = (ds.snapshot.get("cost_model") or None)
    res = ds.compare(cost_model=cost_model)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_compare(res)
    return 0


def cmd_delay(args) -> int:
    ds = _make_ds(args)
    res = ds.delay_impact(args.step, float(args.hours))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_delay(res)
    return 0 if res.get("ok") else 2


def cmd_explain(args) -> int:
    ds = _make_ds(args)
    res = ds.explain(step_id=args.step or "", plan_id=args.plan or "")
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_explain(res)
    return 0 if res.get("ok") else 1


def cmd_backtest(args) -> int:
    data = _load_json(args.records)
    records = data.get("predictions") if isinstance(data, dict) else data
    samples = data.get("samples") if isinstance(data, dict) else []
    res = backtest(records, samples, samples, as_of=args.as_of or None)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_backtest(res)
    return 0


def print_align(res: dict) -> None:
    print("=" * 74)
    print("二期/三期契约对齐 %s · 二期契约 %s"
          % (res["align_version"], S.BRAIN_CONTRACT_VERSION))
    print("=" * 74)
    print("输入来源：%s" % res["context_source"])
    print("二期模块定位：%s（in_tree=同一棵树；isolated=别名包隔离装载；missing=不可用）"
          % res["brain_root_kind"])
    s = res["summary"]
    print("项目 %s · 快照 %s · 上下文版本 %s"
          % (s.get("project_id") or "—", s.get("snapshot_id") or "—",
             s.get("context_version")))
    print("结论：%s（error %d / warning %d）"
          % ("对齐" if res["ok"] else "**未对齐**", s["n_errors"], s["n_warnings"]))
    print("\n【逐项】")
    icon = {"error": "✗", "warning": "!", "info": "·"}
    for i in res["items"]:
        print("  %s [%s] %s" % (icon.get(i["severity"], "·"), i["code"], i["message"]))
        if i["severity"] in ("error", "warning") and i.get("how_to_fix"):
            print("        处置：%s" % i["how_to_fix"])
    print("\n【语义分歧裁决】同名词两期含义不同，必须分开统计")
    for sp in ALIGN.SEMANTIC_SPLITS:
        print("  · 「%s」" % sp["term"])
        print("      二期：%s" % sp["brain_side"])
        print("      三期：%s" % sp["ds_side"])
        print("      裁决：%s" % sp["ruling"])
    print("\n【八个统一字段】%s" % " · ".join(ALIGN.UNIFIED_FIELDS))
    print("【执行闸门】%s" % ALIGN.EXECUTION_GATE["rule"])
    print("            %s\n" % ALIGN.EXECUTION_GATE["pre_execution"])


def _ctx_has_data(ctx: dict) -> bool:
    """这份 ProjectContext 里**真的有记录**吗？

    空的二期库里 `get_project_context` 也会返回一份结构完整的上下文
    （契约版本、counts 全 0），光看 `contract_version` 会把「读到空库」
    误当成「读到数据」。所以按记录数与版本号判定。
    """
    if not ctx.get("contract_version"):
        return False
    if int(ctx.get("version") or 0) > 1:
        return True
    for k in ("observations", "facts", "commitments", "decisions", "predictions",
              "actions", "evidence_refs"):
        if ctx.get(k):
            return True
    return False


def cmd_align(args) -> int:
    snap = _load_json(args.snapshot or DEFAULT_SNAPSHOT)
    cpath = getattr(args, "context", "") or DEFAULT_CONTEXT
    pid = str(snap.get("project_id") or "")
    sid = str(snap.get("snapshot_id") or "")

    ctx, kind, err = ALIGN.load_context(
        pid, sid, getattr(args, "brain_root", ""),
        getattr(args, "brain_data", ""))
    notes = []
    if not _ctx_has_data(ctx):
        # 真实模块可用但里面没有这个项目 —— 说清楚，然后回退到「捕获样本」
        if ctx or err:
            notes.append("真实模块读到的上下文里没有本项目的数据%s"
                         % ("（%s）" % err if err else ""))
        kind = kind if kind in ("in_tree", "isolated") else "missing"
        ctx = _load_json(cpath) if os.path.exists(cpath) else {}
        source = ("mock 捕获样本（由真实二期回放导出）"
                  if os.path.exists(cpath) else "空")
    else:
        source = "project_brain（%s）" % kind

    res = {"align_version": ALIGN.ALIGN_VERSION, "brain_root_kind": kind,
           "context_source": source, "source_notes": notes}
    res.update(ALIGN.check_alignment(ctx, snap))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_align(res)
    return 0 if res["ok"] else 2


def cmd_demo(args) -> int:
    ds = _make_ds(args)
    print("### 1/2 交付依赖\n")
    r1 = ds.analyze()
    print_analyze(r1)
    print("### 2/2 方案比较\n")
    r2 = ds.compare(cost_model=ds.snapshot.get("cost_model") or None)
    print_compare(r2)
    print("### 3/3 延期传播：A 批组装延 8 小时\n")
    print_delay(ds.delay_impact("STP-A-ASM", 8))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="decision_support",
                                description="第三期「项目决策辅助」（离线只读）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--snapshot", default="", help="计划域快照 JSON")
        sp.add_argument("--context", default="", help="二期 ProjectContext JSON（mock 或真实）")
        sp.add_argument("--json", action="store_true", help="输出 JSON")

    sp = sub.add_parser("analyze", help="交付依赖：三日期 + 关键路径 + 资源可行排程")
    common(sp)
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("compare", help="方案比较（S0 基准 / S1 提前到货 / S2 加设备）")
    common(sp)
    sp.add_argument("--cost-model", default="", help="成本模型 JSON")
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("delay", help="延期向下游传播")
    common(sp)
    sp.add_argument("--step", required=True)
    sp.add_argument("--hours", default="8")
    sp.set_defaults(func=cmd_delay)

    sp = sub.add_parser("explain", help="解释一个工序/计划的日期怎么来的")
    common(sp)
    sp.add_argument("--step", default="")
    sp.add_argument("--plan", default="")
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("backtest", help="预测复盘（含防未来信息审计）")
    common(sp)
    sp.add_argument("--records", default=os.path.join(EXAMPLES,
                                                      "decision-support-predictions-sample.json"))
    sp.add_argument("--as-of", default="")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("align", help="二期/三期契约对齐检查（字段、快照、plan 联表）")
    common(sp)
    sp.add_argument("--brain-root", default="auto",
                    help="二期模块根目录；auto=自动定位（同树优先，其次兄弟 worktree）")
    sp.add_argument("--brain-data", default="",
                    help="二期记忆库数据目录（默认用二期默认值）")
    sp.set_defaults(func=cmd_align)

    sp = sub.add_parser("demo", help="用内置合成样例跑完整链路")
    common(sp)
    sp.set_defaults(func=cmd_demo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print("找不到输入文件：%s" % e, file=sys.stderr)
        return 1
    except KeyError as e:
        print("快照缺字段：%s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
