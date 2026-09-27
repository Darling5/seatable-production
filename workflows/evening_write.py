#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evening_write.py — 晚间链路的「授权写入及读回」步骤（可信执行层 v1）。

分工原则：**机器只负责执行，授权永远由人给。**
本步骤不产生授权，只消费授权：

  1. 在 data/approvals/ 找一份当前未过期、未用尽的授权；
  2. 找不到 → 打印 [skip]，退出码 3（skipped，不是失败）；
     当晚照常出快照 / 风险 / 看板，只是不写业务表；
  3. 找到   → 调 wxmatch.cmd_apply(grant_file=...)，逐条经 DataService
              写入 + 读回验证 + 落 data/write_ledger.csv 台账。

退出码：
  0  写入全部通过（或本次没有待写入项）
  1  有写入未通过（读回验证失败 / 授权不覆盖该表 / 结果未知）→ 上游发布门禁拦住发布
  3  没有有效授权 → 跳过

⚠️ 退出码只看 **cmd_apply 返回的结构化摘要**（审计 2026-09-27 修）：
   旧实现忽略 cmd_apply 的返回值，改用「读台账新增行里有没有『失败』」来判成败
   —— 于是「授权不覆盖该表」「业务后端起不来」这类**根本没走到写库**的失败，
   一条台账都不会新增，脚本照样退 0，门禁也就照样放行。
   现在一律以 write() 的结构化 status 为准；台账只用于打印与人工核对。

⚠️ 本脚本**不会**自己造授权。想让它写库，必须有人先把授权文件放进
   data/approvals/。授权文件长这样（manual 来源）：

       {
         "grant_id": "GRT-20260926-a1b2c3",
         "tables": ["IC采购记录", "组装料采购记录"],
         "actions": ["update"],
         "actor": "老板",
         "reason": "9/26 群消息核对无误，同意回填到货状态",
         "issued_at": "2026-09-26T19:05:00",
         "expires_at": "2026-09-27T00:00:00",
         "max_uses": 5
       }

   留空 tables/actions 表示不限（**只应在人工确认时这么写**）；
   max_uses=0 表示不限次，建议一次性任务写个有限次数，用完即失效。
   授权一旦撤销（删除该文件），已构造的授权对象也不能再用。
"""
from __future__ import annotations

import csv
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

LEDGER_NAME = "write_ledger.csv"


def _ledger_rows(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return [r for r in csv.DictReader(f)]
    except OSError:
        return []


def main() -> int:
    from application import authorization as AUTH

    ledger = os.path.join(HERE, "data", LEDGER_NAME)

    store = AUTH.GrantStore()
    lives = store.live_grants()
    if not lives:
        print("[skip] data/approvals/ 下没有有效授权 —— 今晚不写业务表（这是正常状态）。")
        print("       要让晚间链路写库：人工核对无误后放入授权文件，")
        print("       文件格式见 evening_write.py 顶部注释。")
        return 3

    grant = lives[-1]
    print("[ok] 使用授权 %s（授权人 %s｜%s）"
          % (grant.grant_id, grant.actor or "未署名", grant.reason or "无说明"))
    if grant.tables:
        print("     范围：表 %s / 动作 %s"
              % ("、".join(grant.tables), "、".join(grant.actions) or "不限"))

    from wx import wxmatch as wm  # noqa: E402 — wxmatch 已并入 wx/ 包
    summary = wm.cmd_apply(grant_file=store.path(grant.grant_id))

    new_rows = _ledger_rows(ledger)
    reason = summary.get("reason") or ""
    if summary.get("wrote_nothing"):
        if reason == "no_todo":
            print("[ok] 本次没有待授权写入的核对项（台账共 %d 条）" % len(new_rows))
            return 0
        # no_grant / bad_grant：授权文件读不到或非法 → 等同「没有授权」，跳过而非失败
        print("[skip] 未执行写入（%s）—— 当晚不写业务表" % (reason or "缺少可用授权"))
        return 3

    unknown = int(summary.get("unknown") or 0)
    blocked = int(summary.get("blocked") or 0)
    failed = int(summary.get("failed") or 0)
    if unknown:
        print("[fail] 有 %d 条写入**结果未知**（响应丢失/落盘失败）：" % unknown)
        for no, items in (summary.get("results") or {}).items():
            for it in items:
                if it.get("stage") == "unknown":
                    print("       %s %s" % (no, (it.get("msg") or "")[:120]))
        print("       已按「不可重发」处置（避免重复写）。请人工核对远端后决定下一步。")
        return 1
    if failed or blocked:
        print("[fail] 授权写入未通过：成功 %d / 未通过 %d（其中授权阻断 %d 条）"
              % (summary.get("ok", 0), failed, blocked))
        for no, items in (summary.get("results") or {}).items():
            if items and all(i.get("ok") for i in items):
                continue
            print("       %s %s" % (no, "；".join(i.get("msg", "") for i in items)[:150]))
        return 1
    print("[ok] 授权写入及读回完成：成功 %d 条，读回失败 0 条（台账共 %d 条）"
          % (summary.get("ok", 0), len(new_rows)))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
