# -*- coding: utf-8 -*-
"""离线自测：用假 offer 跑通 market.cmd_sync 的写库路径（不联网、不碰真实 CSV）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 模块在上一级
import market

# 把数据文件指向临时文件，绝不污染真实历史
market.WATCH_PATH = os.path.join(market.DATA, "_test_watch.csv")
market.HIST_PATH = os.path.join(market.DATA, "_test_hist.csv")
for p in (market.WATCH_PATH, market.HIST_PATH):
    if os.path.exists(p):
        os.remove(p)

market._write_csv(market.WATCH_PATH, market.WATCH_COLS, [
    {"物料型号": "FR8018HD", "物料名称": "蓝牙芯片", "类别": "IC",
     "来源": "IC采购记录", "上次采购价": "3.10", "上次采购日期": "2026-08-01",
     "启用": "1", "备注": ""},
    {"物料型号": "TEST-NRND", "物料名称": "测试停产料", "类别": "IC",
     "来源": "IC采购记录", "上次采购价": "1.00", "上次采购日期": "2026-08-01",
     "启用": "1", "备注": ""},
])


class FakeSP(object):
    """假供应商：模拟两个渠道返回。"""
    SOURCE_CN = {"digikey": "得捷", "mouser": "贸泽"}

    def available_sources(self):
        return ["digikey", "mouser"]

    def lookup_many(self, mpns, sources=None, qty=1):
        out = {}
        for m in mpns:
            rows = [{
                "ok": True, "source": "得捷", "source_key": "digikey", "mpn": m,
                "manufacturer": "FakeChip", "desc": "测试用假数据",
                "price": 0.42, "currency": "USD", "price_cny": 2.982,
                "fx_rate": 7.1, "fx_src": "静态汇率(config)",
                "stock": 12000, "lead_time": "8 Weeks",
                "lifecycle": "在产" if m == "FR8018HD" else "NRND",
                "url": "https://fake/dk", "datasheet": "", "qty": 1, "error": "",
            }, {
                "ok": True, "source": "贸泽", "source_key": "mouser", "mpn": m,
                "manufacturer": "FakeChip", "desc": "测试用假数据",
                "price": 3.05, "currency": "CNY", "price_cny": 3.05,
                "fx_rate": 1.0, "fx_src": "本币",
                "stock": 800, "lead_time": "110 Days",
                "lifecycle": "在产", "url": "https://fake/mo",
                "datasheet": "", "qty": 1, "error": "",
            }]
            out[m] = rows
        return out


market._load_suppliers = lambda: FakeSP()

print("=" * 66)
print("测试 1：首次 sync（两个型号 × 两个渠道 = 应写 4 条快照）")
print("=" * 66)
market.cmd_sync()

print()
print("=" * 66)
print("测试 2：立刻再 sync（都在 30 天窗口内 → 应全部跳过）")
print("=" * 66)
market.cmd_sync()

print()
print("=" * 66)
print("测试 3：--force 强制重查（应再写 4 条，并触发 NRND 告警）")
print("=" * 66)
market.cmd_sync(force=True)

print()
print("=" * 66)
print("测试 4：写入结果校验")
print("=" * 66)
rows = market._read_csv(market.HIST_PATH)
print("快照总条数：%d（期望 12 = 首次4 + 强制4 + 再强制4？见下）" % len(rows))
for r in rows[:4]:
    print("  ", r["日期"], r["物料型号"], r["渠道"], "¥" + r["单价"],
          "环比" + str(r["涨跌幅"]), r["生命周期"], "|", r["备注"])

print()
print("生命周期映射自测：")
for t in ("Active", "New Product", "NRND", "Not Recommended for New Designs",
          "Obsolete", "End of Life", "", "在产", "不推荐用于新设计"):
    print("   %-34s -> %s" % (repr(t), market.suppliers_lc_map(t)
                              if hasattr(market, "suppliers_lc_map")
                              else __import__("suppliers")._map_lifecycle(t)))

print()
print("数字解析自测：")
from suppliers import _float, _int
for v in ("$4.49", "¥ 31.9", "1,234.5", "733 In Stock", "", None, "110 Days"):
    print("   %-16r -> float=%s int=%s" % (v, _float(v), _int(v)))

# ---------------------------------------------------------------- 省配额闸门
print()
print("=" * 66)
print("测试 5：本地预筛 _looks_like_mpn（非型号条目不该烧配额）")
print("=" * 66)
_MPN_CASES = [
    ("P1", False), ("Z3.5", False), ("Z6.0", False), ("458*3", False),
    ("26MHz/0.5ppm", False), ("Z3.5 PG2.0-3.5-2.5-1.54", False),
    ("GNSS 1.57G", False), ("0402 100nF", False),
    ("2SK3541", True), ("FR8018HD", True), ("GLF71311", True),
    ("ML307N-DC 4+4", True), ("ML307N-DL", True), ("0402ESDA-05N", True),
    ("B5819S", True), ("LM620S", True), ("OM6626B_X311", True),
    ("GRM155R71C104KA88D", True),
]
_mpn_bad = 0
for s, want in _MPN_CASES:
    got = market._looks_like_mpn(s)
    if got != want:
        _mpn_bad += 1
        print("   ✗ %-26r -> %s（期望 %s）" % (s, got, want))
print("   预筛 %d 项，差异 %d 项 %s" % (
    len(_MPN_CASES), _mpn_bad, "✓" if _mpn_bad == 0 else "✗"))

print()
print("=" * 66)
print("测试 6：未知缓存（拿不到有效报价的型号，TTL 内不重复查）")
print("=" * 66)
_orig_cache = market.UNKNOWN_CACHE
market.UNKNOWN_CACHE = os.path.join(tempfile.gettempdir(), "_t_unknown_mpn.json")
try:
    if os.path.exists(market.UNKNOWN_CACHE):
        os.remove(market.UNKNOWN_CACHE)
    market._mark_unknown(["AAA111", "BBB222", "CCC333"])
    skip = market._unknown_skip(["AAA111", "BBB222", "CCC333", "DDD444"])
    print("   缓存 3 项后，再查 4 个型号 → 应跳过 3 个：", sorted(skip))
    assert set(skip) == {"AAA111", "BBB222", "CCC333"}, "未知缓存未按预期命中"
    forced = market._unknown_skip(["AAA111"], force=True)
    print("   --force 时应跳过 0 个：", forced)
    assert not forced, "--force 未绕过未知缓存"
    print("   ✓ 未知缓存正常，--force 可绕过")
finally:
    if os.path.exists(market.UNKNOWN_CACHE):
        os.remove(market.UNKNOWN_CACHE)
    market.UNKNOWN_CACHE = _orig_cache

# 清理
for p in (market.WATCH_PATH, market.HIST_PATH):
    if os.path.exists(p):
        os.remove(p)
print("\n[ok] 临时测试文件已清理")
