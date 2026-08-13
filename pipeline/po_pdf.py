# -*- coding: utf-8 -*-
"""按供应商生成采购订单 PDF（可直接发给供应商）。"""
import glob
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from core import cn_amount, die, load, load_rules, money, run_dir, today

FONT = "CN"
FONT_B = "CN-B"


def reg_font():
    cands = [("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
             ("C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/Dengb.ttf"),
             ("C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/simsunb.ttf")]
    for reg, bold in cands:
        if os.path.exists(reg):
            pdfmetrics.registerFont(TTFont(FONT, reg))
            pdfmetrics.registerFont(TTFont(FONT_B, bold if os.path.exists(bold) else reg))
            return True
    raise RuntimeError("未找到中文字体（simhei/Deng/simsun），PDF 无法生成中文")


def num(v):
    try:
        return float(str(v).replace(",", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def order_no(run_id, idx):
    return f"PO{today().replace('-', '')}-{idx:02d}"


def build_pdf(path, company, supplier, items, amount, no, plan=None):
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"采购订单 {no}")
    h1 = ParagraphStyle("h1", fontName=FONT_B, fontSize=17, leading=23,
                        alignment=1, spaceAfter=2 * mm)
    small = ParagraphStyle("s", fontName=FONT, fontSize=8.5, leading=12)
    cell = ParagraphStyle("c", fontName=FONT, fontSize=8, leading=10.5)
    body = [Paragraph(company.get("name") or "采购订单", h1),
            Paragraph("采 购 订 单", ParagraphStyle("h2", fontName=FONT_B,
                      fontSize=13, leading=18, alignment=1, spaceAfter=3 * mm))]
    info = [[Paragraph(f"<b>供应商：</b>{supplier}", small),
             Paragraph(f"<b>采购单号：</b>{no}", small)],
            [Paragraph(f"<b>采购方：</b>{company.get('buyer') or ''}", small),
             Paragraph(f"<b>日期：</b>{today()}", small)],
            [Paragraph(f"<b>联系电话：</b>{company.get('phone') or ''}", small),
             Paragraph(f"<b>关联计划：</b>{plan or '—'}", small)],
            [Paragraph(f"<b>采购地址：</b>{company.get('address') or ''}", small), ""]]
    t = Table(info, colWidths=[95 * mm, 83 * mm])
    t.setStyle(TableStyle([("SPAN", (0, 3), (1, 3)),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    body += [t, Spacer(1, 3 * mm)]

    head = ["序", "IPN", "型号/规格", "封装", "数量", "含税单价", "含税金额"]
    data = [[Paragraph(f"<b>{h}</b>", cell) for h in head]]
    for i, r in enumerate(items, 1):
        q, p = num(r.get("purchase_qty")), num(r.get("unit_price"))
        data.append([Paragraph(str(i), cell),
                     Paragraph(str(r.get("ipn") or ""), cell),
                     Paragraph(str(r.get("model") or ""), cell),
                     Paragraph(str(r.get("footprint") or ""), cell),
                     Paragraph(f"{q:g}", cell),
                     Paragraph(money(p) if p else "待确认", cell),
                     Paragraph(money(q * p) if p else "待确认", cell)])
    data.append([Paragraph("<b>合计</b>", cell), "", "", "",
                 Paragraph(f"<b>{sum(num(r.get('purchase_qty')) for r in items):g}</b>", cell),
                 "", Paragraph(f"<b>{money(amount)}</b>", cell)])
    tbl = Table(data, colWidths=[9 * mm, 22 * mm, 62 * mm, 20 * mm,
                                 17 * mm, 23 * mm, 25 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#888888")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
        ("SPAN", (0, len(data) - 1), (3, len(data) - 1)),
        ("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), colors.HexColor("#F5F5F5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, len(data) - 2),
         [colors.white, colors.HexColor("#FAFBFD")]),
    ]))
    body += [tbl, Spacer(1, 2 * mm),
             Paragraph(f"<b>合计金额（大写）：</b>{cn_amount(amount)}", small),
             Spacer(1, 3 * mm),
             Paragraph(f"<b>付款条款：</b>{company.get('payment_terms') or ''}", small),
             Paragraph(f"<b>备注：</b>{company.get('remark') or ''}", small),
             Spacer(1, 10 * mm)]
    sign = Table([[Paragraph("采购方（签章）：", small), Paragraph("供应商（签章）：", small)],
                  [Paragraph("日期：", small), Paragraph("日期：", small)]],
                 colWidths=[89 * mm, 89 * mm], rowHeights=[16 * mm, 10 * mm])
    sign.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    body.append(sign)
    doc.build(body)
    return path


def validate_formal_order(company, orders):
    """PDF 可直接发供应商前，必须具备完整抬头与已确认报价。"""
    required = ("name", "buyer", "phone", "address", "payment_terms")
    missing = [key for key in required if not str(company.get(key) or "").strip()]
    if missing:
        die("不能生成正式采购订单，缺采购方字段：" + "、".join(missing)
            + "。请填写 pipeline/rules.local.yaml 后重试。")
    no_price = [order["supplier"] for order in orders if order.get("price_missing")]
    if no_price:
        die("不能生成正式采购订单，以下供应商订单仍有待确认单价：" + "、".join(no_price)
            + "。请先在库存审核表补齐单价并重新执行 plan。")


def run(run_id, plan_name=None):
    reg_font()
    rules = load_rules()
    company = rules.get("company") or {}
    data = load(run_id, "orders.json")
    validate_formal_order(company, data["orders"])
    d = run_dir(run_id)
    for f in glob.glob(os.path.join(d, "采购订单_*.pdf")):
        os.remove(f)
    out = []
    for i, o in enumerate(data["orders"], 1):
        safe = "".join(c for c in o["supplier"] if c not in '\\/:*?"<>|').strip()
        no = order_no(run_id, i)
        p = os.path.join(d, f"采购订单_{safe}_{no}.pdf")
        build_pdf(p, company, o["supplier"], o["items"], o["amount"], no,
                  plan_name or data.get("plan"))
        out.append(p)
        warn = f"（{len(o['price_missing'])} 项待确认单价）" if o["price_missing"] else ""
        print(f"[PDF] {o['supplier']}  {money(o['amount'])}{warn}\n      {p}")
    if not out:
        print("[空] orders.json 无订单，无法生成 PDF")
    return out
