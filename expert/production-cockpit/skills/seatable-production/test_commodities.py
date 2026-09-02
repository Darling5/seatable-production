# -*- coding: utf-8 -*-
"""离线自测：原料行情模块的写库、环比与展示（不联网、不碰真实 CSV）。

重点验证「同口径环比」——连续合约与具体合约的历史不能混比，
否则拼接处会出现假涨跌（与物料行情串渠道踩过的坑同源）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import commodities as cm

# 指向临时文件，绝不污染真实历史
_TMP = os.path.join(tempfile.gettempdir(), "_test_raw_hist.csv")
cm.HIST_PATH = _TMP
if os.path.exists(_TMP):
    os.remove(_TMP)

FAIL = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(name)
    print("   %s %-38s got=%-18r want=%r" % ("✓" if ok else "✗", name, got, want))


def check_num(name, got, want):
    """CSV 读回来一律是字符串，数值断言先转 float 再比。"""
    try:
        g = float(got)
    except (TypeError, ValueError):
        g = None
    ok = g is not None and abs(g - float(want)) < 1e-6
    if not ok:
        FAIL.append(name)
    print("   %s %-38s got=%-18r want=%r" % ("✓" if ok else "✗", name, got, want))


print("=" * 78)
print("测试 1：人工录入 + 同口径环比（add 走现货·人工口径）")
print("=" * 78)
cm.cmd_add("ABS", "11800")
cm.cmd_add("ABS", "12540")          # +6.27%
rows = cm._read_csv(_TMP)
check("录入条数", len(rows), 2)
check_num("第 2 条涨跌幅", rows[1]["涨跌幅"], 6.27)
check("口径标记为人工", rows[0]["口径"], cm.BASIS_MANUAL)

print()
print("=" * 78)
print("测试 2：同口径隔离 —— 连续 vs 具体合约互不当基准")
print("=" * 78)
# 造数据：黄金连续 900，黄金合约au2612 系列 800→880
rows = cm._read_csv(_TMP)
rows.append({"日期": "2026-08-01", "原料": "黄金", "类别": "金属", "单价": "800",
             "单位": "元/克", "涨跌幅": "", "口径": "合约au2612",
             "来源": "东方财富", "备注": ""})
rows.append({"日期": "2026-08-02", "原料": "黄金", "类别": "金属", "单价": "880",
             "单位": "元/克", "涨跌幅": "", "口径": "合约au2612",
             "来源": "东方财富", "备注": ""})
rows.append({"日期": "2026-08-03", "原料": "黄金", "类别": "金属", "单价": "900",
             "单位": "元/克", "涨跌幅": "", "口径": cm.BASIS_CONT,
             "来源": "新浪期货", "备注": ""})
rows.sort(key=lambda r: (r.get("日期") or "", r.get("原料") or ""))
cm._recalc_pct(rows)
cm._write_csv(_TMP, cm.HIST_COLS, rows)
rows = cm._read_csv(_TMP)
au = {r["口径"]: r["涨跌幅"] for r in rows if r["原料"] == "黄金"}
check_num("合约口径第 2 条 = +10%（800→880）", au.get("合约au2612"), 10.0)
# 核心：连续口径这条是同口径首条，涨跌幅必须为空。
# 若代码错误地跨口径取基准（拿合约的 880 比 900），会算出 +2.27% —— 必须为空才对。
check("连续口径未串用合约价作基准（应为空）", au.get(cm.BASIS_CONT), "")

print()
print("=" * 78)
print("测试 3：sparkline 与口径显示")
print("=" * 78)
check("单点不画走势", cm._sparkline([1]), "—")
check("空数据不画走势", cm._sparkline([]), "—")
check("等值序列", cm._sparkline([5, 5, 5]), "▁▁▁")
check("上升趋势首尾不同", cm._sparkline([1, 2, 3]) != cm._sparkline([3, 2, 1]), True)
check("口径显示·连续", cm._basis_label(cm.BASIS_CONT), "连续·实时")
check("口径显示·合约", cm._basis_label("合约au2612"), "au2612·历史")
check("口径显示·人工", cm._basis_label(cm.BASIS_MANUAL), "现货·人工")

print()
print("=" * 78)
print("测试 4：_prev_price 只认同口径（注意：CSV 里存的是名称，不是代码）")
print("=" * 78)
check_num("ABS 人工口径基准", cm._prev_price("ABS 树脂", cm.BASIS_MANUAL, rows), 12540.0)
check("ABS 连续口径无记录", cm._prev_price("ABS 树脂", cm.BASIS_CONT, rows), None)
check("代码 ABS 查不到（存的是名称）", cm._prev_price("ABS", cm.BASIS_MANUAL, rows), None)

print()
print("=" * 78)
print("测试 5：品种表完整性（塑料代理 + 人工现货都要在册）")
print("=" * 78)
for k in ("AU", "AG", "CU", "SN", "PP", "L", "V", "ABS", "PC", "PS"):
    check("品种 %s 已登记" % k, k in cm.BY_KEY, True)
check("ABS 无自动源", cm.BY_KEY["ABS"]["auto"], False)
check("锡有自动源", cm.BY_KEY["SN"]["auto"], True)
check("锡单位为元/吨", cm.BY_KEY["SN"]["unit"], "元/吨")

# 清理
if os.path.exists(_TMP):
    os.remove(_TMP)
print()
print("=" * 78)
print("失败 %d 项 %s" % (len(FAIL), ("—— " + "、".join(FAIL)) if FAIL else "✓ 全部通过"))
print("=" * 78)
sys.exit(1 if FAIL else 0)
