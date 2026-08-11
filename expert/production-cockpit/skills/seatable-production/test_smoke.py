#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_smoke.py — 零依赖冒烟测试（不需要 pytest，直接 python test_smoke.py）。

覆盖：
  1. 所有模块可导入、HTML 模板占位符可完整替换
  2. 资源域计算：负载率 / 超载 / 闲置 / 排程冲突 / 人工成本
  3. 演示数据能稳定产出「超载 + 闲置 + 冲突」三种信号（否则驾驶舱首次打开是死页面）
  4. 口令从 config 读取，且明文不出现在产物 HTML 里
  5. 每条 next_action 的 cat 都能在驾驶舱里找到落点（否则「去处理」按钮消失）

强制在临时目录跑，绝不读写真实 data/。
"""
import datetime
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_FAIL = []
_PASS = [0]


def check(cond, msg):
    if cond:
        _PASS[0] += 1
    else:
        _FAIL.append(msg)
        print("  FAIL: " + msg)


def main():
    tmp = tempfile.mkdtemp(prefix="cockpit_test_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        import adapters.factory as F
        from adapters.local import LocalAdapter
        from adapters import schema

        real = F.get_adapter
        F.get_adapter = lambda config=None: LocalAdapter(data)
        try:
            import seed_demo
            seed_demo.get_adapter = F.get_adapter
            seed_demo.main()

            ad = LocalAdapter(data)
            ad.auth()
            today = datetime.date.today()
            plans = ad.list_rows("生产计划")

            print("\n[1] 表结构")
            check(len(schema.TABLES) == 16, "TABLES 应为 16 张，实际 %d" % len(schema.TABLES))
            check("资源" in schema.TABLES and "资源分配" in schema.TABLES, "资源域表未注册")
            check(schema.link_id_for("资源", "资源分配") == "RsAl", "资源↔资源分配 link_id 解析失败")
            check(schema.columns_of("资源") is not None, "columns_of('资源') 返回 None")
            check(len(schema.validate_enum("资源", {"类型": "外星人"})) == 1, "枚举软校验未生效")
            check(len(schema.validate_enum("资源", {"类型": "人员"})) == 0, "合法枚举被误报")

            print("[2] 资源域计算")
            import cockpit
            res = cockpit.compute_resources(ad, today, plans)
            check(res is not None, "compute_resources 返回 None（演示资源数据未被读到）")
            if res:
                check(res["week"]["workdays"] == 5, "本周工作日应为 5，实际 %s" % res["week"]["workdays"])
                check(len(res["over"]) >= 1, "演示数据未产生「超载」信号")
                check(len(res["idle"]) >= 1, "演示数据未产生「闲置」信号")
                check(len(res["conflicts"]) >= 1, "演示数据未产生「排程冲突」信号")
                check(res["labor_cost"] > 0, "人工成本为 0")
                check(len(res["plan_cost"]) > 0, "未按生产计划归集人工成本")
                over = res["over"][0]
                check(over["load"] > 100, "超载资源负载率应 >100%%，实际 %s" % over["load"])
                # 已完成的分配计成本但不占本周负载
                idle = res["idle"][0]
                check(idle["week_alloc"] == 0, "闲置资源本周投入应为 0")

            print("[3] 空资源时模块应隐藏")
            empty_dir = os.path.join(tmp, "empty")
            os.makedirs(empty_dir)
            ad2 = LocalAdapter(empty_dir)
            ad2.auth()
            check(cockpit.compute_resources(ad2, today, []) is None,
                  "无资源数据时应返回 None（隐藏模块），而不是空表")

            print("[4] 模型与行动建议")
            model = cockpit.compute(ad, today)
            check(model.get("resource") is not None, "model 缺少 resource")
            for k in ("res_load", "res_over", "res_conflict", "labor_cost"):
                check(k in model["kpi"], "KPI 缺少 %s" % k)
            acts = model["next_actions"]
            check(any(a.get("cat") == "resource" for a in acts), "未生成资源类行动建议")
            # 每条建议都要有 pri / cat / text，且 cat 在驾驶舱有映射
            KNOWN = {"purchase", "warehouse", "delivery", "production", "sales", "boss", "resource"}
            for a in acts:
                check(a.get("pri") in ("高", "中", "提示"), "行动建议优先级非法: %r" % a.get("pri"))
                check(a.get("cat") in KNOWN, "行动建议 cat 未在 CAT_SEC 中登记: %r" % a.get("cat"))
                check(bool(a.get("text")), "行动建议缺少文案")

            print("[5] HTML 产物")
            html = cockpit.HTML.replace("__MODEL__", json.dumps(model, ensure_ascii=False))
            for k, v in cockpit.ICONS.items():
                html = html.replace("__" + k + "__", v)
            html = html.replace("__PW_BLOB__", json.dumps(cockpit.PW_BLOB))
            import re
            left = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]{2,}__", html)))
            check(not left, "HTML 中存在未替换占位符: %s" % left)
            check("sec-Rs" in html, "HTML 中缺少资源区")
            check(".rs-track" in html, "缺少资源进度条样式（数字会被填充条盖住）")
            # 口令明文不得出现
            check(cockpit.ADMIN_PASSWORD not in html,
                  "管理员口令明文出现在 HTML 中（应为 base64 混淆）")
            for pw in cockpit.ROLE_PASSWORDS.values():
                check(pw not in html, "角色口令明文出现在 HTML 中")
            # 每个 CAT_SEC 已登记的类别都能在 JS 里找到
            check('resource:["Rs"' in html.replace(" ", "").replace("\n", "") or
                  'resource:["Rs","G","T"]' in html, "CAT_SEC 未登记 resource 类别")

            print("[6] 口令生成幂等")
            a1, r1 = cockpit._load_pw()
            a2, r2 = cockpit._load_pw()
            check(a1 == a2 and r1 == r2, "两次读取口令不一致（每次生成会导致分享出去的口令失效）")
        finally:
            F.get_adapter = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if _FAIL:
        print("FAILED %d / %d" % (len(_FAIL), len(_FAIL) + _PASS[0]))
        for f in _FAIL:
            print("  · " + f)
        return 1
    print("ALL %d CHECKS PASSED" % _PASS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
