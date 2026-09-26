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
  1  有写入未通过（读回验证失败 / 授权不覆盖该表）→ 上游发布门禁拦住发布
  3  没有有效授权 → 跳过

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
    before = len(_ledger_rows(ledger))

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
    wm.cmd_apply(grant_file=store.path(grant.grant_id))

    new_rows = _ledger_rows(ledger)[before:]
    failed = [r for r in new_rows if (r.get("读回验证") or "").strip() == "失败"]
    if failed:
        print("[fail] 本次授权写入有 %d 条读回验证失败：" % len(failed))
        for r in failed[:5]:
            print("       %s %s  %s" % (r.get("表"), r.get("row_id"),
                                        (r.get("备注") or "")[:60]))
        return 1
    print("[ok] 授权写入及读回完成：新增台账 %d 条，读回失败 0 条" % len(new_rows))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
