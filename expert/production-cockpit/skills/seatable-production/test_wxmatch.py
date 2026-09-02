#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_wxmatch.py — wxmatch.py 核对引擎离线回归测试。

全部用假数据，不碰真实微信库/真实 CSV。跑法：
    python test_wxmatch.py
"""
import csv
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wxmatch as wm  # noqa: E402

TMP = tempfile.gettempdir()
FAILED = []


def check(name, got, want):
    ok = got == want
    print("  %s %s%s" % ("✓" if ok else "✗", name, "" if ok else "  got=%r want=%r" % (got, want)))
    if not ok:
        FAILED.append(name)


def check_true(name, cond):
    check(name, bool(cond), True)


# ---------------------------------------------------------------- 测试 1：金额提取
print("=" * 70)
print("测试 1：_extract_amounts（收款金额识别）")
print("=" * 70)
amts = wm._extract_amounts("已打款 58225 元，请查收")
check("普通金额", amts and amts[0][0], 58225.0)
amts = wm._extract_amounts("尾款 11.5万 已安排")
check("万元换算", amts and amts[0][0], 115000.0)
amts = wm._extract_amounts("合同编号 20260829 没提钱")
check("年份不触发", amts, [])
amts = wm._extract_amounts("已付款 30 块定金")
check("小额不触发（<100）", amts, [])
amts = wm._extract_amounts("已转款 3,650.00")
check("千分位", amts and amts[0][0], 3650.0)

# ---------------------------------------------------------------- 测试 2：客户匹配
print()
print("=" * 70)
print("测试 2：_match_customer（客户名 + 产品词双维度）")
print("=" * 70)
PROJECTS = [
    {"项目": "郑州云峰（振道）", "待收": "58227", "产品需求": "UWB信标 人员定位",
     "__row_id__": "r1"},
    {"项目": "上海汇撰-智能发卡充电柜及定位卡", "待收": "", "产品需求": "发卡充电柜 定位卡",
     "__row_id__": "r2"},
    {"项目": "大学（禾木）", "待收": "", "产品需求": "蓝牙信标 UWB信标 防爆",
     "__row_id__": "r3"},
    {"项目": "上海汇撰-蓝牙信标", "待收": "", "产品需求": "蓝牙信标", "__row_id__": "r4"},
]
hits = wm._match_customer("郑州云峰：款项已付 58227", PROJECTS)
check("客户名命中", [h["__row_id__"] for h in hits][:1], ["r1"])
hits = wm._match_customer("00 智能人脸识别发卡充电柜及定位卡采购合同.pdf", PROJECTS)
check("产品词 2 词重叠命中汇撰项目", hits and hits[0]["__row_id__"], "r2")
hits = wm._match_customer("蓝牙信标采购合同（盖章版）.pdf", PROJECTS)
check("单产品词不命中（太泛）", hits, [])
# 双词门槛 + 项目名原词排序：r3（大学）产品词 2 词命中；r4（汇撰-蓝牙信标）
# 只有 1 个产品词不进候选——这是防泛匹配的设计行为（单「蓝牙信标」词
# 无法区分 5+ 个信标项目）
hits = wm._match_customer("355个蓝牙信标与UWB信标合同.pdf", PROJECTS)
check("双词命中唯一候选大学", [h["__row_id__"] for h in hits], ["r3"])
# 项目名原词优先：给 r4 也配 UWB信标 产品词后，项目名含「蓝牙信标」原词的 r4 应排前
P2 = [dict(p) for p in PROJECTS]
P2[3]["产品需求"] = "蓝牙信标 UWB信标"
hits = wm._match_customer("355个蓝牙信标与UWB信标合同.pdf", P2)
check("项目名原词优先于泛匹配", hits and hits[0]["__row_id__"], "r4")

# ---------------------------------------------------------------- 测试 3：金额匹配
print()
print("=" * 70)
print("测试 3：_match_amount（待收金额 ±2% 容差）")
print("=" * 70)
P = [{"项目": "A", "待收": "58227", "__row_id__": "a"},
     {"项目": "B", "待收": "100000", "__row_id__": "b"}]
check("精确命中", [x["__row_id__"] for x in wm._match_amount(58227, P)], ["a"])
check("2% 容差内命中", [x["__row_id__"] for x in wm._match_amount(58000, P)], ["a"])
check("超容差不命中", wm._match_amount(50000, P), [])
check("空待收不命中", wm._match_amount(100, [{"待收": ""}]), [])

# ---------------------------------------------------------------- 测试 4：事件扫描（假事件）
print()
print("=" * 70)
print("测试 4：scan_events（收款/下单信号 → 匹配 → 意图生成）")
print("=" * 70)
tmp_ev = os.path.join(TMP, "wxmatch_test_events.csv")
tmp_prj = os.path.join(TMP, "wxmatch_test_projects.csv")
with open(tmp_ev, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(wm._read_csv.__globals__ and ["事件编号", "日期", "时间", "来源群", "发送人",
                                             "分类", "原文", "意图", "状态", "确认时间", "写入结果"])
    w.writerow(["E1", "2026-09-02", "10:00", "客户群", "张三", "其他",
                "郑州云峰这边已打款 58225 元，请查收", "", "待确认", "", ""])
    w.writerow(["E2", "2026-09-02", "11:00", "客户群", "李四", "其他",
                "我们确认订单，这周先订 300 台", "", "待确认", "", ""])
    w.writerow(["E3", "2026-01-01", "11:00", "客户群", "王五", "其他",
                "已打款 50000（超时事件，不该出现）", "", "待确认", "", ""])
with open(tmp_prj, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["__row_id__", "项目编号", "项目", "合同", "合同总价", "实收", "待收"])
    w.writerow(["row-a", "20260413-001", "郑州云峰（振道）", "", "116450", "58225", "58227"])
    w.writerow(["row-b", "20260410-001", "云南天奥（智环）", "", "193000", "183350", "9650"])
old_ev, old_prj = wm.EVENTS_PATH, wm.PROJECTS_PATH
wm.EVENTS_PATH, wm.PROJECTS_PATH = tmp_ev, tmp_prj
rows = wm.scan_events(days=7)
wm.EVENTS_PATH, wm.PROJECTS_PATH = old_ev, old_prj
pay = [r for r in rows if r["类型"] == "收款"]
check("收款信号识别", len(pay), 1)
check("收款金额匹配到郑州云峰", pay and pay[0]["匹配项目"], "郑州云峰（振道）")
check("收款高置信", pay and pay[0]["置信度"], "高")
check("预填意图含 row_id", pay and "row-a" in (pay[0]["预填意图"] or ""), True)
check("预填意图含新实收 116450", pay and "116450" in (pay[0]["预填意图"] or ""), True)
order = [r for r in rows if r["类型"] == "下单"]
check("下单信号识别", len(order), 1)
check("下单未匹配提示立项", order and "立项" in (order[0]["匹配结果"] + order[0]["建议动作"]), True)
check("过期事件被过滤", any("超时" in r["信号内容"] for r in rows), False)

# ---------------------------------------------------------------- 测试 5：合同 PDF 分类规则
print()
print("=" * 70)
print("测试 5：合同 PDF 判别正则（附件/发票排除 + 我方主体归类）")
print("=" * 70)
check("合同命中", bool(wm.CONTRACT_PAT.search("产品购销合同.pdf")), True)
check("PO 命中", bool(wm.CONTRACT_PAT.search("PO260820采购单.pdf")), True)
check("发票排除", bool(wm.CONTRACT_EXCL.search("电子发票20260828.pdf")), True)
check("快递单排除", bool(wm.CONTRACT_EXCL.search("跨越速运快递单.pdf")), True)
check("附件不独立核算", bool(wm.CONTRACT_ATTACHMENT.search("02 附件二 安装调试方案.pdf")), True)
check("我方主体识别", bool(wm.OWN_COMPANY_PAT.search("智环未来(深圳)科技有限公司_销售合同.pdf")), True)
check("供应商别名", wm.SUPPLIER_ALIASES.get("禾电迅"), "禾电讯")

# ---------------------------------------------------------------- 测试 6：供应商合同匹配
print()
print("=" * 70)
print("测试 6：_match_supplier_contract（含别名归一）")
print("=" * 70)
PUR = [{"供应商": "禾电讯", "表": "IC采购记录", "状态": "已下单", "花销": 24000,
        "下单时间": "2026-08-07", "物料": "LM620S"}]
check("别名命中（迅→讯）", len(wm._match_supplier_contract("禾电迅-振道技术2026080701_已签章.pdf", PUR)), 1)
check("原名命中", len(wm._match_supplier_contract("禾电讯对账单.pdf", PUR)), 1)
check("无关不命中", wm._match_supplier_contract("其他公司合同.pdf", PUR), [])

# ---------------------------------------------------------------- 收尾
print()
print("=" * 70)
if FAILED:
    print("失败 %d 项 ✗：%s" % (len(FAILED), "、".join(FAILED)))
    sys.exit(1)
print("失败 0 项 ✓ 全部通过")
print("=" * 70)
for p in (tmp_ev, tmp_prj):
    try:
        os.remove(p)
    except OSError:
        pass
