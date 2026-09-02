# -*- coding: utf-8 -*-
"""离线自测：用假 offer 跑通 market.cmd_sync 的写库路径（不联网、不碰真实 CSV）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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

# 清理
for p in (market.WATCH_PATH, market.HIST_PATH):
    if os.path.exists(p):
        os.remove(p)
print("\n[ok] 临时测试文件已清理")
