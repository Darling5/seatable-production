#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_wxmatch.py — wxmatch.py 核对引擎离线回归测试。

全部用假数据，不碰真实微信库/真实 CSV。跑法：
    python test_wxmatch.py
"""
import csv
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 模块在上一级
sys.path.insert(0, HERE)
from wx import wxmatch as wm  # noqa: E402

# 别名表 / 我方主体表的内容随本地 config.yaml 而变（真实主体名不入库、外置在配置里），
# 所以这里自备一份泛化值，只验证**机制**本身，结果不随环境漂移。
wm.OWN_COMPANY_PAT = re.compile(r"示例科技|示例集团|示例贸易")
wm.SUPPLIER_ALIASES = {"示例供应商AO": "示例供应商AP"}

TMP = tempfile.gettempdir()
# ⚠️ 写入链路的数据目录必须是**本测试独占**的：原先直接用系统临时目录当
#    data_dir，而可信执行层会在 data_dir 里落 write_operations.sqlite3（操作状态），
#    于是上一次跑留下的 verify_failed 记录会被下一次当成「已写过的同一操作」复用，
#    导致断言随机失败（2026-09-27 实测）。这里给它一个干净子目录。
APPLY_DIR = os.path.join(TMP, "wxmatch_test_apply_dir")
shutil.rmtree(APPLY_DIR, ignore_errors=True)
os.makedirs(APPLY_DIR, exist_ok=True)
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
    {"项目": "客户B（示例集团）", "待收": "58227", "产品需求": "UWB信标 人员定位",
     "__row_id__": "r1"},
    {"项目": "客户F-智能发卡充电柜及定位卡", "待收": "", "产品需求": "发卡充电柜 定位卡",
     "__row_id__": "r2"},
    {"项目": "客户O", "待收": "", "产品需求": "蓝牙信标 UWB信标 防爆",
     "__row_id__": "r3"},
    {"项目": "客户F-蓝牙信标", "待收": "", "产品需求": "蓝牙信标", "__row_id__": "r4"},
]
hits = wm._match_customer("客户B：款项已付 58227", PROJECTS)
check("客户名命中", [h["__row_id__"] for h in hits][:1], ["r1"])
hits = wm._match_customer("00 智能人脸识别发卡充电柜及定位卡采购合同.pdf", PROJECTS)
check("产品词 2 词重叠命中客户F项目", hits and hits[0]["__row_id__"], "r2")
hits = wm._match_customer("蓝牙信标采购合同（盖章版）.pdf", PROJECTS)
check("单产品词不命中（太泛）", hits, [])
# 双词门槛 + 项目名原词排序：r3（大学）产品词 2 词命中；r4（客户F-蓝牙信标）
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
# 事件日期改为相对日期：原写死 2026-09-02，scan_events(days=7) 随日期推移会全部过期
# （2026-09-12 实测 7 项失败，均为时间敏感导致，非代码回归）
import datetime as _dt
_d_recent = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
_d_stale = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
with open(tmp_ev, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(wm._read_csv.__globals__ and ["事件编号", "日期", "时间", "来源群", "发送人",
                                             "分类", "原文", "意图", "状态", "确认时间", "写入结果"])
    w.writerow(["E1", _d_recent, "10:00", "客户群", "张三", "其他",
                "客户B这边已打款 58225 元，请查收", "", "待确认", "", ""])
    w.writerow(["E2", _d_recent, "11:00", "客户群", "李四", "其他",
                "我们确认订单，这周先订 300 台", "", "待确认", "", ""])
    w.writerow(["E3", _d_stale, "11:00", "客户群", "王五", "其他",
                "已打款 50000（超时事件，不该出现）", "", "待确认", "", ""])
with open(tmp_prj, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["__row_id__", "项目编号", "项目", "合同", "合同总价", "实收", "待收"])
    w.writerow(["row-a", "20260413-001", "客户B（示例集团）", "", "116450", "58225", "58227"])
    w.writerow(["row-b", "20260410-001", "客户H（示例科技）", "", "193000", "183350", "9650"])
old_ev, old_prj = wm.EVENTS_PATH, wm.PROJECTS_PATH
wm.EVENTS_PATH, wm.PROJECTS_PATH = tmp_ev, tmp_prj
rows = wm.scan_events(days=7)
wm.EVENTS_PATH, wm.PROJECTS_PATH = old_ev, old_prj
pay = [r for r in rows if r["类型"] == "收款"]
check("收款信号识别", len(pay), 1)
check("收款金额匹配到客户B", pay and pay[0]["匹配项目"], "客户B（示例集团）")
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
check("我方主体识别", bool(wm.OWN_COMPANY_PAT.search("示例科技(深圳)有限公司_销售合同.pdf")), True)
check("供应商别名", wm.SUPPLIER_ALIASES.get("示例供应商AO"), "示例供应商AP")

# ---------------------------------------------------------------- 测试 6：供应商合同匹配
print()
print("=" * 70)
print("测试 6：_match_supplier_contract（含别名归一）")
print("=" * 70)
PUR = [{"供应商": "示例供应商AP", "表": "IC采购记录", "状态": "已下单", "花销": 24000,
        "下单时间": "2026-08-07", "物料": "LM620S"}]
check("别名命中（迅→讯）", len(wm._match_supplier_contract("示例供应商AO-示例集团2026080701_已签章.pdf", PUR)), 1)
check("原名命中", len(wm._match_supplier_contract("示例供应商AP对账单.pdf", PUR)), 1)
check("无关不命中", wm._match_supplier_contract("其他公司合同.pdf", PUR), [])

# ---------------------------------------------------------------- 测试 7：到货/发货核对
print()
print("=" * 70)
print("测试 7：scan_arrivals（供应商唯一命中 → 高置信到货意图）")
print("=" * 70)
tmp_arr_ev = os.path.join(TMP, "wxmatch_test_arrival_events.csv")
tmp_arr_pur = os.path.join(TMP, "wxmatch_test_arrival_purchase.csv")
with open(tmp_arr_ev, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["事件编号", "日期", "时间", "来源群", "发送人", "分类", "原文", "意图", "状态", "确认时间", "写入结果"])
    w.writerow(["A1", _d_recent, "12:00", "采购群", "供应商", "其他", "示例供应商AP的 LM620S 已到货，已签收", "", "待确认", "", ""])
    w.writerow(["A2", _d_recent, "13:00", "采购群", "采购员", "其他", "预计到货提醒：示例供应商AP下周到货", "", "待确认", "", ""])
with open(tmp_arr_pur, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["__row_id__", "供应商", "状态", "交期", "采购花销", "下单时间", "物料清单"])
    w.writerow(["pur-a", "示例供应商AP", "已下单", _d_recent, "24000", _d_recent, "LM620S"])
old_ev, old_pur = wm.EVENTS_PATH, wm.DATA
wm.EVENTS_PATH = tmp_arr_ev
wm.DATA = TMP
# 让 _load_purchase_rows 只读临时的 IC采购记录，其余采购表不存在时自然跳过
old_purchase = wm.PURCHASE_TABLES
wm.PURCHASE_TABLES = ["wxmatch_test_arrival_purchase"]
try:
    arrivals = wm.scan_arrivals(days=7)
finally:
    wm.EVENTS_PATH, wm.DATA, wm.PURCHASE_TABLES = old_ev, old_pur, old_purchase
check("到货事实识别", len(arrivals), 1)
check("供应商唯一命中高置信", arrivals and arrivals[0]["置信度"], "高")
check("到货意图含目标表和 row_id", arrivals and "pur-a" in (arrivals[0]["预填意图"] or ""), True)
check("到货意图更新状态", arrivals and "已到货" in (arrivals[0]["预填意图"] or ""), True)
check("预计到货问句排除", any("预计到货提醒" in r["信号内容"] for r in arrivals), False)

# ---------------------------------------------------------------- 测试 8：apply 授权写入（可信执行层 v1）
print()
print("=" * 70)
print("测试 8：cmd_apply（高置信≠授权：无授权只列清单；有授权才写并读回）")
print("=" * 70)
import json  # noqa: E402
from application import authorization as AUTH  # noqa: E402

tmp_match = os.path.join(APPLY_DIR, "核对结果.csv")
tmp_ledger = os.path.join(APPLY_DIR, "write_ledger.csv")
for _p in (tmp_match, tmp_ledger):
    try:
        os.remove(_p)
    except OSError:
        pass
apply_rows = [
    {"核对编号": "WX-A-001", "日期": _d_recent, "类型": "到货", "信号来源": "采购群|供应商",
     "信号内容": "示例供应商AP已到货", "匹配结果": "在途唯一", "匹配项目": "", "建议动作": "确认到货",
     "预填意图": '[{"op":"update","table":"IC采购记录","row_id":"pur-a","data":{"状态":"已到货"},"reason":"测试"}]',
     "置信度": "高", "状态": "待确认"},
    {"核对编号": "WX-A-002", "日期": _d_recent, "类型": "到货", "信号来源": "采购群|采购员",
     "信号内容": "某物料到货", "匹配结果": "多条在途", "匹配项目": "", "建议动作": "人工确认",
     "预填意图": "", "置信度": "中", "状态": "待确认"},
    {"核对编号": "WX-A-003", "日期": _d_recent, "类型": "收款", "信号来源": "客户群|张三",
     "信号内容": "已打款", "匹配结果": "已写入", "匹配项目": "项目A", "建议动作": "",
     "预填意图": '[{"op":"update","table":"项目","row_id":"already","data":{"实收":"100"}}]',
     "置信度": "高", "状态": "已授权写入"},
]
with open(tmp_match, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=wm.MATCH_COLS)
    w.writeheader()
    w.writerows(apply_rows)


class _ApplyAdapter:
    """带 list_rows 的假适配器 —— 读回验证需要它。

    旧版 fake 只有 update_row/append_row，所以「每条写完读回验证由
    adapter 自带」这句话在测试里从没被验证过；新链路显式需要 list_rows。
    """

    def __init__(self):
        self.calls = []
        self.rows = {}

    def update_row(self, table, row_id, data):
        self.calls.append((table, row_id, dict(data)))
        self.rows[row_id] = dict(data)
        self.rows[row_id]["__row_id__"] = row_id

    def append_row(self, table, data):
        rid = "new-row"
        self.calls.append((table, "append", dict(data)))
        self.rows[rid] = dict(data)
        self.rows[rid]["__row_id__"] = rid
        return rid

    def list_rows(self, table):
        return [dict(r) for r in self.rows.values()]


old_match, old_data = wm.MATCH_PATH, wm.DATA
old_get_adapter = wm._get_business_adapter
stub = _ApplyAdapter()
wm.MATCH_PATH, wm.DATA = tmp_match, APPLY_DIR
wm._get_business_adapter = lambda: (stub, "stub")

# ① 无授权：高置信也不行，只列清单、一条都不写
try:
    wm.cmd_apply()
    no_grant_rows = {r["核对编号"]: r for r in wm._read_csv(tmp_match)}
finally:
    pass
check("高置信 + 无授权 → 一条都不写", len(stub.calls), 0)
check("无授权时状态保持待确认", no_grant_rows["WX-A-001"]["状态"], "待确认")

# ② 带人工授权文件：写入 + 读回验证
grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("update",),
                          actor="老板", reason="人工核对无误")
grant_file = os.path.join(APPLY_DIR, "wxmatch_test_grant.json")
with open(grant_file, "w", encoding="utf-8") as f:
    json.dump(grant.to_dict(), f, ensure_ascii=False)
try:
    wm.cmd_apply(grant_file=grant_file)
    after_apply = wm._read_csv(tmp_match)
finally:
    wm.MATCH_PATH, wm.DATA = old_match, old_data
    wm._get_business_adapter = old_get_adapter

by_no = {r["核对编号"]: r for r in after_apply}
check("授权后写入 1 条", len(stub.calls), 1)
check("写入目标正确", stub.calls and stub.calls[0][0:2], ("IC采购记录", "pur-a"))
check("读回通过后回填已授权写入", by_no["WX-A-001"]["状态"], "已授权写入")
check("中置信不被写入", by_no["WX-A-002"]["状态"], "待确认")
check("已处理项不重复写", by_no["WX-A-003"]["状态"], "已授权写入")
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
