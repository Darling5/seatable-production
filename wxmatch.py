#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wxmatch.py — 群消息 ↔ SeaTable 业务核对引擎。

背景（2026-09-03 用户需求）：光靠群消息「归纳总结」不够——群里所有跟公司
相关的关键业务信号（收款、客户新下单、已下单的合同 PDF）必须跟 SeaTable
业务表**逐条对上账**，而不是停留在摘要里。三类核对：

  1. 收款信号：群消息出现「已打款/已付/到账/尾款 + 金额」
     → 项目表「待收/实收」匹配：找出该客户待收金额 ≈ 消息金额的项目，
       生成回款更新意图（确认后写 SeaTable）。
  2. 下单信号：「下单/订单/合同已签 + 数量」
     → 项目表按客户名匹配；匹配不到 = 可能漏立项，提示。
  3. 合同 PDF：微信收到的合同文件（磁盘 msg/file/月份/ 下明文文件名，
     微信 4.x 数据库里 content 是加密容器解不出 XML，但**磁盘文件名是明文**）
     → 项目表「合同」列比对：同名 = 已登记；项目有收款但合同列为空 = 提示补登记。

数据源（全部本地只读，零凭证零外联）：
  - data/���信事件.csv           wechat_intake.py 登记的事件（含 AI 从 summary 提取的）
  - data/项目.csv               SeaTable 同步下来的项目表（客户/合同/实收/待收）
  - 微信 msg/file/月份/          接收的文件（按 mtime 过滤时间窗）
  - wxengine 探测的数据根目录    auto_detect_db_dir()（v1.6.2 起支持 APPDATA 缺失兜底）

输出：
  - data/核对结果.csv            核对明细（供驾驶舱/播报消费）
  - 终端报告                     人类可读，含建议动作

铁律：
  - **只读核对，绝不自动写 SeaTable**。更新实收/立项/补合同都必须人确认
    （走 wechat_intake.py approve 流程或对话确认），引擎只产「建议意图」。
  - 金额/日期匹配是**启发式**，置信度写进结果列，低置信度的只提示不预填。
  - 微信文件名是**隐私数据**，核对结果 CSV 落 data/（gitignore），绝不入库。
"""
import argparse
import csv
import os
import re
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DATA = os.path.join(HERE, "data")
EVENTS_PATH = os.path.join(DATA, "微信事件.csv")
PROJECTS_PATH = os.path.join(DATA, "项目.csv")
MATCH_PATH = os.path.join(DATA, "核对结果.csv")
MATCH_COLS = ["核对编号", "日期", "类型", "信号来源", "信号内容", "匹配结果",
              "匹配项目", "建议动作", "预填意图", "置信度", "状态"]

# ─────────────────────────────────────────────── 信号识别
# 收款：钱的动作 + 金额。误报主要来自「付款方式讨论」「发票金额」——
# 用「动作在前」的短语锚定，纯数字/发票号不触发。
PAY_HINTS = [
    "已打款", "已付款", "已经付款", "款项已付", "已转款", "已转账", "已汇款",
    "已安排付款", "付款了", "打款了", "转账了", "付了", "付过了",
    "尾款已", "定金已", "预付款已", "已支付", "货款已", "款项到了",
    "钱已转", "已收到款", "收款成功", "到账",
]
ORDER_HINTS = [
    "下单", "新订单", "订单确认", "要订", "订一批", "采购合同", "合同已签",
    "合同签好", "已签章", "已盖章", "签回来了", "合同回签", "确认订单",
    "增加订单", "追加订单", "返单",
]
# 合同类 PDF 判别：文件名含「合同/协议/订单/PO/盖章版/已签章」；排除发票/快递单/说明书/宣传册
CONTRACT_PAT = re.compile(r"合同|协议|采购单|订单|PO\d|盖章|已签|回签")
CONTRACT_EXCL = re.compile(r"发票|快递|运单|说明书|折页|宣传|规格书|授权书|简历|报价单|合格证")
# 附件不单独算合同（附件二/附件三…都是主合同的附属文件，核对主文件即可）
CONTRACT_ATTACHMENT = re.compile(r"^0?\d?\s*附件|^附件|附件[一二三四五六七八九十\d]")

_AMOUNT_PAT = re.compile(
    r"([0-9][0-9,，]{1,9}(?:\.[0-9]{1,2})?)\s*(万|w|W|元|块|¥)")
# 不带单位词的金额（「已转款 3,650.00」句尾直接结束）：千分位是强信号
_AMOUNT_BARE = re.compile(r"([0-9]{1,3}[，,][0-9]{3}(?:\.[0-9]{1,2})?)(?![0-9])")

# 微信号数据根目录（探测失败时为 None，合同 PDF 核对自动跳过）
def _wx_file_root():
    try:
        from wxengine.wa_db import auto_detect_db_dir
        root = auto_detect_db_dir()
        if not root:
            return None
        for d in os.listdir(root):
            p = os.path.join(root, d, "msg", "file")
            if os.path.isdir(p):
                return p
    except Exception:
        return None
    return None


def _read_csv(path):
    if not os.path.exists(path):
        return []
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
        except Exception:
            return []
    return []


def _write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return None


def _extract_amounts(text):
    """从消息里抠金额（元），返回 [(数值, 原文)]。万元换算成元。"""
    out = []
    for m in _AMOUNT_PAT.finditer(text or ""):
        raw = m.group(1).replace(",", "").replace("，", "")
        val = float(raw)
        if m.group(2) in ("万", "w", "W"):
            val *= 10000
        # 过滤明显不是钱的：太小的数字（<100 元很少走公对公）与年份
        if 100 <= val <= 50_000_000 and not (2015 <= val <= 2035):
            out.append((val, m.group(0)))
    # 无单位词但有千分位的（「已转款 3,650.00」）：千分位分隔是钱的强信号
    if not out:
        for m in _AMOUNT_BARE.finditer(text or ""):
            val = float(m.group(1).replace(",", "").replace("，", ""))
            if 1000 <= val <= 50_000_000 and not (2015 <= val <= 2035):
                out.append((val, m.group(1)))
    return out


def _cust_of_project(proj_name):
    """项目名「郑州云峰（振道）」→ 客户「郑州云峰」；无括号取全名（≤8 字才算名）。"""
    name = re.sub(r"（.*", "", proj_name or "").strip()
    return name or (proj_name or "").strip()


def _match_customer(text, projects):
    """消息文本 ↔ 项目匹配（客户名 + 产品关键词双维度）。返回 [项目行]。

    维度1 客户名：「郑州云峰（振道）」→「郑州云峰」在文本中出现即命中。
    维度2 产品词：项目名/产品需求里的品类词（蓝牙信标/小卡/工卡/发卡机/
    定位卡/网关/RTK…）在合同文件名中出现，作为客户名不全时的补充信号
    （实测「智能人脸识别发卡充电柜及定位卡采购合同」文件名里只有产品词，
    客户「上海汇撰」根本不在文件名里）。
    产品词匹配要求 ≥2 个词重叠（单个词太泛——「小卡」「信标」几乎所有项目
    都有，一个词命中会造成大面积误匹配）。
    客户命中排前（更可信）；仅产品词命中的排后、置信度降档。
    """
    by_cust, by_prod = [], []
    for p in projects:
        cust = _cust_of_project(p.get("项目", ""))
        if cust and len(cust) >= 2 and cust in (text or ""):
            by_cust.append(p)
            continue
        blob = " ".join([p.get("项目", "") or "", p.get("产品需求", "") or "",
                         p.get("生产计划", "") or ""])
        words = _prod_words(blob)
        # 重叠词数：注意「发卡充电柜」包含「充电柜」「发卡机」，去重后数
        overlap = [w for w in words if w in (text or "")]
        # 「充电柜」是「发卡充电柜」的子串，同项目命中两个算一个语义
        if overlap:
            sem = set(overlap)
            if "充电柜" in sem and "发卡充电柜" in sem:
                sem.discard("充电柜")
            if "发卡机" in sem and "发卡充电柜" in sem:
                sem.discard("发卡机")
            if len(sem) >= 2:
                by_prod.append(p)
    # 产品词命中多个项目时（信标/小卡几乎所有项目都有），按「项目名包含
    # 命中词」优先排序——「蓝牙信标采购合同」在「上海汇撰-蓝牙信标」（项目名
    # 原词）应排在「大学（禾木）」（只产品需求里提过）之前。
    def _prod_rank(p):
        return sum(1 for w in _prod_words(
            " ".join([p.get("项目", "") or ""])) if w in (text or ""))
    by_prod.sort(key=lambda p: (_prod_rank(p), _num(p.get("待收")) or 0), reverse=True)
    by_cust.sort(key=lambda p: _num(p.get("待收")) or 0, reverse=True)
    return by_cust + by_prod


_PROD_PAT = re.compile(
    r"蓝牙信标|UWB信标|uwb信标|信标|小卡|工卡|发卡机|发卡充电柜|定位卡|人员定位|"
    r"物联网卡|4G大卡|RTK|网关|报警器|充电柜|定位系统|防爆|温湿度")

# 我方公司主体名（合同是「我方名义的采购合同」——按供应商核对，见 _load_purchase_rows）
OWN_COMPANY_PAT = re.compile(r"智环未来|振道技术|点晨")


# ─────────────────────────────────────────────── 供应商合同核对（采购对账）
PURCHASE_TABLES = ["IC采购记录", "PCBA半成品采购记录", "组装料采购记录",
                   "成品采购记录", "外壳采购记录"]
PURCHASE_FIELDS = {"供应商": "供应商", "状态": "状态", "交期": "交期",
                   "花销": "采购花销", "数量": "数量", "物料": None}


def _load_purchase_rows():
    """合并 5 张采购记录表 → [{表,行,供应商,状态,交期,花销,下单时间,物料}]。"""
    out = []
    for t in PURCHASE_TABLES:
        for r in _read_csv(os.path.join(DATA, "%s.csv" % t)):
            sup = (r.get("供应商") or "").strip()
            if not sup:
                continue
            out.append({
                "表": t, "行": r, "供应商": sup,
                "状态": (r.get("状态") or "").strip(),
                "交期": (r.get("交期") or "").strip(),
                "花销": _num(r.get("采购花销") or r.get("价格")),
                "下单时间": (r.get("下单时间") or r.get("采购时间") or "")[:10],
                "物料": (r.get("物料清单") or r.get("物料名称") or
                         r.get("组装料名称") or r.get("外壳名称") or "")[:40],
            })
    return out


# 供应商名别名：合同文件名写法 ↔ SeaTable 采购表写法（一字之差/简称）。
# 实测：合同写「禾电迅」，表里是「禾电讯」。发现新差异往这里加。
SUPPLIER_ALIASES = {
    "禾电迅": "禾电讯", "禾电訊": "禾电讯",
    "西崖鼎昇": "西崖/鼎昇", "华宸": "华宸振凯",
    "亿创源": "亿创源-马达散料",
}


def _match_supplier_contract(fname, purchases):
    """合同文件名 ↔ 供应商名匹配（含别名归一）。"""
    hits = []
    for p in purchases:
        sup = p["供应商"]
        names = {sup}
        for alias, real in SUPPLIER_ALIASES.items():
            if sup == real:
                names.add(alias)
            elif alias == sup:
                names.add(real)
        if any(n and n in fname for n in names):
            hits.append(p)
    return hits


def _prod_words(blob):
    """从项目名+产品需求里提品类词（≥2 字，出现一次即算）。"""
    return sorted(set(_PROD_PAT.findall(blob or "")))


def _match_amount(amount, projects, tol=0.02):
    """金额 ↔ 待收金额匹配（±2% 容差，尾款常有零头/手续费差）。"""
    out = []
    for p in projects:
        due = _num(p.get("待收"))
        if due and abs(due - amount) / max(due, amount) <= tol:
            out.append(p)
    return out


# ─────────────────────────────────────────────── 核对主逻辑
def scan_events(days=7):
    """扫微信事件 CSV，识别收款/下单信号 → 匹配项目表。"""
    events = _read_csv(EVENTS_PATH)
    projects = _read_csv(PROJECTS_PATH)
    if not events:
        return []
    cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    out = []
    for e in events:
        d = str(e.get("日期", ""))[:10]
        if d < cutoff:
            continue
        text = e.get("原文", "") or ""
        sig = "%s|%s" % (e.get("来源群", ""), e.get("发送人", ""))
        # 收款
        if any(h in text for h in PAY_HINTS):
            amounts = _extract_amounts(text)
            cust_hits = _match_customer(text, projects)
            if cust_hits and not amounts:
                # 提到客户没提金额：列出该客户待收项目，让人判断
                for p in cust_hits[:2]:
                    out.append(_mk(d, "收款", sig, text, cust_hits, p,
                                   "消息含收款词但未识别金额；该客户有待收项目",
                                   "", "低"))
            elif amounts:
                amt = max(a[0] for a in amounts)   # 多个数字取最大的当主金额
                amt_hits = _match_amount(amt, cust_hits or projects)
                if amt_hits:
                    p = amt_hits[0]
                    due = _num(p.get("待收")) or 0
                    got = _num(p.get("实收")) or 0
                    intent = ("[{\"op\":\"update\",\"table\":\"项目\","
                              "\"row_id\":\"%s\",\"data\":{\"实收\":\"%s\"},"
                              "\"reason\":\"群消息收款核对：待收 %s 已收\"}]"
                              % (p.get("__row_id__", ""), got + amt, due))
                    out.append(_mk(d, "收款", sig, text, amt_hits, p,
                                   "金额 %s 元与待收 %s 元吻合（±2%%）" % (
                                       _fmt(amt), _fmt(due)),
                                   "确认到账，更新实收", intent, "高", "待确认"))
                elif cust_hits:
                    p = cust_hits[0]
                    out.append(_mk(d, "收款", sig, text, cust_hits, p,
                                   "客户匹配但金额 %s 元与任一待收（%s 元）不吻合"
                                   % (_fmt(amt), _fmt(_num(p.get("待收")) or 0)),
                                   "人工核对：可能部分付款/分期", "", "中"))
                else:
                    out.append(_mk(d, "收款", sig, text, [], None,
                                   "未匹配到项目（客户名不在项目表）",
                                   "确认是否漏立项", "", "低"))
        # 下单
        if any(h in text for h in ORDER_HINTS):
            cust_hits = _match_customer(text, projects)
            if cust_hits:
                p = cust_hits[0]
                out.append(_mk(d, "下单", sig, text, cust_hits, p,
                               "客户 %s 在项目表有 %d 个项目"
                               % (_cust_of_project(p.get("项目", "")), len(cust_hits)),
                               "判断是已有项目的追加单还是新需求", "", "中"))
            else:
                out.append(_mk(d, "下单", sig, text, [], None,
                               "下单信号未匹配到任何项目——可能漏立项",
                               "确认是否需要立项（op.py 新增项目）", "", "中"))
    return out


def scan_contract_pdfs(days=30):
    """扫微信收到的合同类 PDF（磁盘明文文件名）↔ 项目表合同列 + 采购记录表。"""
    projects = _read_csv(PROJECTS_PATH)
    purchases = _load_purchase_rows()
    root = _wx_file_root()
    if not root or not projects:
        return []
    cutoff = datetime.now() - timedelta(days=int(days))
    registered = set()
    for p in projects:
        for fn in re.split(r"[,，]", p.get("合同", "") or ""):
            fn = fn.strip()
            if fn:
                registered.add(fn)
                registered.add(fn.replace(".pdf", "").replace(".PDF", ""))
    out = []
    seen = set()
    for root_dir, _dirs, fs in os.walk(root):
        for f in fs:
            if not f.lower().endswith(".pdf"):
                continue
            full = os.path.join(root_dir, f)
            try:
                if datetime.fromtimestamp(os.path.getmtime(full)) < cutoff:
                    continue
            except OSError:
                continue
            if not CONTRACT_PAT.search(f) or CONTRACT_EXCL.search(f):
                continue
            if CONTRACT_ATTACHMENT.search(f):
                continue
            if f in seen:
                continue
            seen.add(f)
            base = f.replace(".pdf", "").replace(".PDF", "")
            # 同名去重：微信重传文件常带 (1)(2) 后缀，剥掉后同名只核一次
            canon = re.sub(r"\(\d+\)", "", base).strip()
            if canon in seen:
                continue
            seen.add(canon)
            if any(base == r or f == r or canon == r for r in registered):
                continue          # 文件名已登记在项目合同列 → 正常，不产核对项
            if _match_supplier_contract(f, purchases):
                # 供应商名义采购合同 ↔ 采购记录表核对（供应商对账）
                for p in _match_supplier_contract(f, purchases)[:1]:
                    st = p["状态"] or "未填状态"
                    spent = p["花销"]
                    amt = _extract_amounts(f)
                    amt_txt = _fmt(amt[0][0]) if amt else "—"
                    mismatch = ""
                    if amt and spent and abs(amt[0][0] - spent) / max(amt[0][0], spent) > 0.05:
                        mismatch = "；**金额差 %.0f%%（合同 %s vs 表 %s）**" % (
                            abs(amt[0][0] - spent) / max(amt[0][0], spent) * 100,
                            amt_txt, _fmt(spent))
                    out.append(_mk(datetime.now().strftime("%Y-%m-%d"), "供应商合同",
                                   "微信文件/%s" % os.path.basename(root_dir), f,
                                   [], None,
                                   "供应商 %s 采购记录在「%s」（%s·下单 %s%s）"
                                   % (p["供应商"], p["表"], st, p["下单时间"] or "?", mismatch),
                                   "核对状态/到货是否同步；金额差异>5%%要查" if mismatch
                                   else "对上了；确认状态列是否最新", "", "中"))
                continue
            if OWN_COMPANY_PAT.search(f) and not _match_customer(f, projects):
                # 我方主体名义（智环未来/振道技术）且文件名里识别不出客户——
                # 这是我方开出去的销售合同回签，客户名大概率在 PDF 内文里，
                # 文件名层面无法自动归属。归为低置信待人工，不刷「漏立项」。
                out.append(_mk(datetime.now().strftime("%Y-%m-%d"), "合同PDF",
                               "微信文件/%s" % os.path.basename(root_dir), f,
                               [], None,
                               "我方名义销售合同（客户名在 PDF 内文，文件名识别不出）",
                               "人工确认客户归属项目后登记合同列", "", "低"))
                continue
            # 未登记：试着按客户名/产品词找归属项目
            cust_hits = _match_customer(f, projects)
            if cust_hits:
                p = cust_hits[0]
                proj = p.get("项目", "")
                has_contract = bool((p.get("合同") or "").strip())
                out.append(_mk(datetime.now().strftime("%Y-%m-%d"), "合同PDF",
                               "微信文件/%s" % os.path.basename(root_dir), f,
                               cust_hits, p,
                               "收到合同 PDF，项目「%s」合同列%s"
                               % (proj, "已登记其他文件" if has_contract else "为空"),
                               "确认是否该把此 PDF 登记到该项目的合同列", "",
                               "中" if not has_contract else "低"))
            else:
                out.append(_mk(datetime.now().strftime("%Y-%m-%d"), "合同PDF",
                               "微信文件/%s" % os.path.basename(root_dir), f,
                               [], None,
                               "收到合同 PDF 但匹配不到项目——可能漏立项或客户名不同",
                               "确认归属项目；若新客户需立项", "", "中"))
    return out


def _fmt(v):
    try:
        return "{:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return str(v or "")


def _mk(date, typ, src, text, hits, proj, result, action, intent, conf, status="待核对"):
    return {
        "核对编号": "", "日期": date, "类型": typ,
        "信号来源": src[:60], "信号内容": (text or "")[:200],
        "匹配结果": result, "匹配项目": (proj or {}).get("项目", ""),
        "建议动作": action, "预填意图": intent, "置信度": conf, "状态": status,
    }


def cmd_scan(days_ev=7, days_pdf=30, write=True):
    rows = scan_events(days_ev) + scan_contract_pdfs(days_pdf)
    # 编号：WX-M-YYYYMMDD-NN
    seq = 0
    for r in rows:
        seq += 1
        r["核对编号"] = "WX-M-%s-%03d" % (datetime.now().strftime("%Y%m%d"), seq)
    # 保留历史已处置行（状态 != 待核对），合并本轮新发现
    if write:
        old = [r for r in _read_csv(MATCH_PATH) if r.get("状态") not in ("待核对", "")]
        merged = old + rows
        _write_csv(MATCH_PATH, MATCH_COLS, merged)
    _report(rows)
    return rows


def _report(rows):
    print("=" * 66)
    print("群消息 ↔ SeaTable 核对报告（%s）" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("=" * 66)
    if not rows:
        print("本轮无待核对信号。")
        return
    for r in rows:
        conf = r.get("置信度", "")
        mark = "★" if conf == "高" else ("☆" if conf == "中" else " ")
        print("\n%s [%s|%s置信] %s %s" % (
            mark, r["类型"], conf, r["核对编号"], r["日期"]))
        print("   信号: %s" % r["信号内容"][:80])
        print("   项目: %s" % (r.get("匹配项目") or "（未匹配）"))
        print("   判定: %s" % r["匹配结果"])
        if r.get("建议动作"):
            print("   建议: %s" % r["建议动作"])
    hi = sum(1 for r in rows if r.get("置信度") == "高")
    mid = sum(1 for r in rows if r.get("置信度") == "中")
    print("\n" + "-" * 66)
    print("共 %d 条：高置信 %d（可一键确认写入）· 中 %d（需人工判断）· 低 %d"
          % (len(rows), hi, mid, len(rows) - hi - mid))
    print("处置：python wxmatch.py done WX-M-xxx-001  或在对话里逐条确认。")
    print("铁律：引擎只读核对不写库；高置信项也必须人确认后才写 SeaTable。")


def cmd_list(status=None):
    rows = _read_csv(MATCH_PATH)
    if status:
        rows = [r for r in rows if r.get("状态") == status]
    if not rows:
        print("（没有%s核对项）" % (status or ""))
        return
    for r in rows[-60:]:
        print("%s %-11s %-4s %-8s %-14s %s" % (
            r.get("核对编号", ""), r.get("日期", ""), r.get("类型", ""),
            r.get("置信度", ""), r.get("状态", ""), (r.get("匹配项目") or "未匹配")[:16]))


def cmd_done(no, note=""):
    """标记核对项已处置。注意：真正写 SeaTable 走 wechat_intake.py approve（意图 JSON
    复用其写库链路），这里只管核对台账状态。"""
    rows = _read_csv(MATCH_PATH)
    hit = False
    for r in rows:
        if r.get("核对编号") == no:
            r["状态"] = "已处置"
            r["建议动作"] = (r.get("建议动作", "") + ("；" + note if note else ""))[:200]
            hit = True
    if not hit:
        print("[skip] 没有核对项 %s" % no)
        return
    _write_csv(MATCH_PATH, MATCH_COLS, rows)
    print("[ok] %s 已标记处置" % no)


def cmd_export_intent(no):
    """把高置信项的预填意图打印出来（复制给 wechat_intake.py approve 用）。"""
    rows = _read_csv(MATCH_PATH)
    for r in rows:
        if r.get("核对编号") == no:
            it = r.get("预填意图") or ""
            if not it:
                print("[skip] %s 没有预填意图（低置信项需人工构造）" % no)
                return
            print(it)
            return
    print("[skip] 没有核对项 %s" % no)


def main():
    ap = argparse.ArgumentParser(description="群消息 ↔ SeaTable 业务核对引擎（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="扫描核对（事件 7 天 + 合同PDF 30 天）")
    p.add_argument("--days", type=int, default=7, help="事件回看天数")
    p.add_argument("--pdf-days", type=int, default=30, help="合同 PDF 回看天数")
    p.add_argument("--no-write", action="store_true", help="只打印不写核对台账")
    p = sub.add_parser("list", help="列核对项")
    p.add_argument("--status", default=None)
    p = sub.add_parser("done", help="标记已处置")
    p.add_argument("no")
    p.add_argument("--note", default="")
    sub.add_parser("intent", help="打印某项的预填意图 JSON")
    p.add_argument("no")
    a = ap.parse_args()
    if a.cmd == "scan":
        cmd_scan(a.days, a.pdf_days, write=not a.no_write)
    elif a.cmd == "list":
        cmd_list(a.status)
    elif a.cmd == "done":
        cmd_done(a.no, a.note)
    elif a.cmd == "intent":
        cmd_export_intent(a.no)


if __name__ == "__main__":
    main()
