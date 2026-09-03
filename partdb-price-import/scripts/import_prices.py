#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
partdb-price-import —— 采购合同 PDF -> PartDB 价格导入（可自动执行）

用法（由 agent 在 WorkBuddy 中调用，人只需丢 PDF 并说"更新/录入物料"）：

  # 1) 只读分析：提取 PDF + 匹配 PartDB + 生成审核报告（不写任何数据）
  python import_prices.py analyze --pdf <pdf路径或目录> [--out <输出目录>]

  # 2) 用户审核报告后确认，执行写入
  python import_prices.py apply  --report <report.json> --yes

报告（report.json / report.md）会清楚标出每一条的处置类别：
  SKIP      之安传感价已等于合同价，无需改动
  UPDATE    之安传感已有 MOQ=1 价格档且不同 -> 就地更新到合同价
  ADD_TIER  之安传感已有但无 MOQ=1 档 -> 新增一档合同价（保留历史）
  NEW_ORDER 该料号从无之安传感记录 -> 新建之安传感采购记录
  NEW_PART  PartDB 无此型号 -> 新建料 + 之安传感价
  CONFLICT  多候选/规格冲突 -> 需用户在报告里选定（默认给建议）

apply 前必须用户确认；CONFLICT 类若用户未给定 part_id，apply 会中断并提示。

依赖：pypdf（managed venv: C:/Users/11430/.workbuddy/binaries/python/envs/default）
凭据：~/.qclaw/seatable-cache/config.env 里的 PARTDB_URL（含 /api）、PARTDB_TOKEN
"""
import os, re, sys, json, argparse, urllib.request, urllib.parse, glob

# ----------------------------- 配置 -----------------------------
CFG_PATH = os.path.expanduser("~/.qclaw/seatable-cache/config.env")

def load_config():
    env = {}
    if os.path.exists(CFG_PATH):
        for line in open(CFG_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

# ----------------------------- PartDB API -----------------------------
class PartDB:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        self._cache = {}

    def _req(self, method, path, data=None, ctype="application/ld+json"):
        url = self.url + path
        headers = {"Authorization": "Token " + self.token}
        if data is not None:
            headers["Content-Type"] = ctype
            body = json.dumps(data).encode("utf-8")
        else:
            body = None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def get(self, path):
        s, b = self._req("GET", path)
        if s == 200:
            try:
                return json.loads(b)
            except Exception:
                return None
        return None

    def get_all(self, path):
        """分页累加，直到某页 member 为空。

        ⚠️ 重要坑：本 PartDB 的 API 完全忽略 limit 参数，固定每页 30 条，
        且 hydra:totalItems 不可信（批量查询会提前截断）。
        因此必须按 page 翻页直到 member 为空，绝不能靠 limit/总数判断。
        """
        out = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            d = self.get(f"{path}{sep}page={page}")
            if not d:
                break
            mem = d.get("hydra:member", [])
            if not mem:
                break
            out.extend(mem)
            page += 1
            if page > 500:
                break
        return out

    def post(self, path, data):
        return self._req("POST", path, data)

    def patch(self, path, data):
        # ⚠️ PATCH 必须用 merge-patch+json，否则 415
        return self._req("PATCH", path, data, ctype="application/merge-patch+json")

    # --- 参考实体解析（缓存） ---
    def _all(self, key, path):
        if key not in self._cache:
            self._cache[key] = self.get_all(path)
        return self._cache[key]

    def resolve_category(self, kind):
        kw = {"res": ["电阻", "resistor"], "cap": ["电容", "capacitor"],
              "ind": ["电感", "inductor"]}.get(kind, [])
        for c in self._all("cats", "/categories"):
            nm = (c.get("name") or "").lower()
            if any(k.lower() in nm for k in kw):
                return c["id"]
        return None

    def resolve_footprint(self, pkg):
        if not pkg:
            return None
        for f in self._all("fps", "/footprints"):
            if (f.get("name") or "").lower() == pkg.lower():
                return f["id"]
        return None

    BRAND_MAP = {"国巨": "YAGEO", "村田": "muRata", "风华": "Fenghua",
                 "钰泰": "ETA", "创捷": "Chuangjie", "芯导": "XinDao",
                 "英联": "Yinglian", "鸿星": "Hongxing", "顺络": "Sunlord"}

    def resolve_manufacturer(self, brand):
        if not brand:
            return None
        allm = self._all("mans", "/manufacturers")
        # 1) 直接用合同品牌精确匹配
        for m in allm:
            if (m.get("name") or "").lower() == brand.lower():
                return m["id"]
        # 2) 用映射的英文名匹配
        en = self.BRAND_MAP.get(brand)
        if en:
            for m in allm:
                if (m.get("name") or "").lower() == en.lower():
                    return m["id"]
        return None

# ----------------------------- PDF 解析 -----------------------------
def _is_component(tokens):
    """判断一组 token 是否像一个元件型号行（过滤 同城费用/纯数字序列/页脚 等）。"""
    if not tokens:
        return False
    # 全是纯数字/小数 -> 不是元件（如 "4 5 6 7 ..." 数字串）
    if all(re.match(r"^[\d.]+$", t) for t in tokens):
        return False
    joined = " ".join(tokens)
    # 容值/阻值/感值/频率 + 单位
    if re.search(r"(pF|uF|nF|K\b|R\b|M\b|Ω|uh|mhz|khz)", joined, re.I):
        return True
    # IC 类：字母+数字混合（如 2SK3541 / ETA5055 / DF37NC）
    if re.search(r"[A-Za-z]{2,}\d|\d[A-Za-z]{1,}", joined):
        return True
    # 中文元件关键词
    if re.search(r"(LED|灯|开关|KEY|二极管|三极管|电容|电阻|电感|晶振|磁珠|连接器|模块)", joined, re.I):
        return True
    return False


def extract_rows(pdf_path):
    import pypdf
    r = pypdf.PdfReader(pdf_path)
    raw_lines = []
    for p in r.pages:
        t = p.extract_text() or ""
        for ln in t.split("\n"):
            raw_lines.append(ln.rstrip())

    FOOTER_KW = ["合计", "备注", "供方", "需方", "联系人", "税号", "银行",
                 "公账", "地址", "电话", "单位名称", "签订日期", "产品名称",
                 "供货时间", "销售合同", "销 售 合 同", "法定代表人", "开户",
                 "同城", "费用", "运", "税", "服务费"]
    rows = []          # 每行是一个 token 列表
    cur = None

    def is_row_start(ln):
        m = re.match(r"^\s*(\d{1,3})\s+(.*)$", ln)
        if not m:
            return False
        after = m.group(2).split()
        return _is_component(after)

    for ln in raw_lines:
        s = ln.strip()
        if not s:
            continue
        # 表头（字符间有空格，且很短）直接跳过
        if re.match(r"^[序型品供牌号量额注\s]{2,}$", s) and len(s) < 24:
            continue
        if is_row_start(s):
            if cur is not None:
                rows.append(cur)
            cur = s.split()
        else:
            # 续行仅接受"型号尾巴"：含拉丁字母或括号（如磁珠 "(GZ1005D121CTF)"）。
            # 纯数字串/中文费用行不会续接，避免误拼。
            if cur is not None and re.search(r"[A-Za-z(（]", s) \
                    and not any(k in s for k in FOOTER_KW):
                cur += s.split()
    if cur is not None:
        rows.append(cur)

    parsed = []
    for toks in rows:
        # 从右往左找 (qty[int], price[float], amount[float]) 三元组锚点
        hit = None
        for i in range(len(toks) - 1, 2, -1):
            if re.match(r"^\d+$", toks[i - 2]) and _isfloat(toks[i - 1]) and _isfloat(toks[i]):
                hit = i
                break
        if hit is None:
            continue
        qty = int(toks[hit - 2])
        price = float(toks[hit - 1])
        amount = float(toks[hit].replace(",", ""))
        brand = toks[hit - 3]
        model = " ".join(toks[1:hit - 3]).strip()
        if not model or not _is_component(model.split()):
            continue
        parsed.append({"model": model, "brand": brand, "qty": qty,
                       "price": price, "amount": amount})
    return parsed


def _isfloat(x):
    try:
        float(x.replace(",", ""))
        return True
    except ValueError:
        return False


# ----------------------------- 型号规格解析 -----------------------------
PKG_PATTERNS = ["0201", "0402", "0603", "0805", "1206", "sot23", "sot723",
                "sot89", "dfn8", "dfn10", "dfn", "wlcsp4", "3225", "2520",
                "2012", "to252", "sot"]


def spec_of(name, fp_name=None):
    s = {"unit": None, "value": None, "pkg": None, "dielectric": None,
         "voltage": None, "tol": None}
    n = name.lower()
    for p in PKG_PATTERNS:
        if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", n):
            s["pkg"] = p
            break
    if fp_name:
        fpl = fp_name.lower()
        for p in PKG_PATTERNS:
            if p in fpl:
                s["pkg"] = p
                break
    for d in ["x7r", "x5r", "c0g", "cog", "npo", "y5v", "x7s"]:
        if d in n:
            s["dielectric"] = "c0g" if d in ("c0g", "cog", "npo") else d
            break
    vm = re.search(r"(\d+(?:\.\d+)?)\s*v", n)
    if vm:
        s["voltage"] = float(vm.group(1))
    tm = re.search(r"(\d+(?:\.\d+)?)\s*%", n)
    if tm:
        s["tol"] = float(tm.group(1))
    # 电容
    for unit, scale in [("uf", 1e6), ("nf", 1e3), ("pf", 1)]:
        cm = re.search(r"(\d+(?:\.\d+)?)\s*" + unit + r"\b", n)
        if cm:
            s["unit"] = "cap"
            s["value"] = float(cm.group(1)) * scale
            return s
    # 电感
    im = re.search(r"(\d+(?:\.\d+)?)\s*uh", n)
    if im:
        s["unit"] = "ind"
        s["value"] = float(im.group(1))
        return s
    # 电阻（先剔除频率词，避免 Mhz 干扰）
    nn = re.sub(r"\d+(?:\.\d+)?\s*(mhz|khz)", "", n)
    rm = re.search(r"(\d+(?:\.\d+)?)\s*([krm])", nn)
    if rm:
        v = float(rm.group(1))
        suf = rm.group(2)
        s["value"] = v * 1000 if suf == "k" else (v * 1e6 if suf == "m" else v)
        s["unit"] = "res"
        return s
    # 晶振
    fm = re.search(r"(\d+(?:\.\d+)?)\s*(mhz|khz)", n)
    if fm:
        s["unit"] = "xtal"
        s["value"] = float(fm.group(1))
        return s
    s["unit"] = "other"
    return s


def _norm(x):
    return re.sub(r"\s+", " ", (x or "").lower()).strip()


# ----------------------------- 匹配 -----------------------------
def _ns(x):
    return re.sub(r"\s+", "", (x or "").lower())


# 合同品牌 -> PartDB 制造商英文名 映射
BRAND_MAP = {"国巨": "yageo", "村田": "murata", "风华": "fenghua", "顺络": "sunlord",
             "三星": "samsung", "钰泰": "eta", "创捷": "chuangjie", "芯导": "xindao",
             "英联": "yinglian", "鸿星": "hongxing", "KDS": "kds", "CJ": "cj",
             "GLF": "glf", "SLM": "slm", "HRS": "hrs", "ETA": "eta"}
# LED 颜色中文 -> PartDB 英文名
LED_COLOR = {"蓝色": "blue", "绿色": "green", "红色": "red", "黄色": "yellow",
             "白": "white", "暖": "warm"}


def _brand_match(contract_brand, man_name):
    if not contract_brand or not man_name:
        return False
    cb = contract_brand.lower()
    mn = man_name.lower()
    if cb in mn or mn in cb:
        return True
    en = BRAND_MAP.get(contract_brand)
    if en and en in mn:
        return True
    for k, v in BRAND_MAP.items():
        if v == mn and k.lower() == cb:
            return True
    return False


def find_candidates(line, parts):
    """返回 (candidates, reason)。reason: exact_name / substr / value / none"""
    model = line["model"]
    ne = _norm(model)
    ne_ns = _ns(model)
    # 1) 精确型号名
    exact = [p for p in parts if _norm(p.get("name")) == ne]
    if exact:
        return exact, "exact_name"
    s = spec_of(model)
    # 2) 无源器件：值/封装/介质/电压 匹配（用评分择优，见 pick_best）
    if s["unit"] in ("cap", "res", "ind", "xtal"):
        cands = []
        for p in parts:
            pn_all = (p.get("name") or "").lower()
            ps = spec_of(p.get("name", ""), (p.get("footprint") or {}).get("name"))
            if ps["unit"] != s["unit"]:
                continue
            if s["value"] is None or ps["value"] is None:
                continue
            if abs(ps["value"] - s["value"]) / max(s["value"], 1e-9) > 0.02:
                continue
            # 排除热敏/温度类假候选（如 10KNTC），除非合同型号本身也含 NTC/TC
            if re.search(r"ntc|ptc|therm", pn_all) and not re.search(r"ntc|ptc|therm", model.lower()):
                continue
            # 封装冲突才排除（part 缺封装则不排除）
            if s.get("pkg") and ps.get("pkg") and s["pkg"] != ps["pkg"]:
                continue
            # 介质冲突才排除（part 缺介质则不排除）
            if s.get("dielectric") and ps.get("dielectric") and s["dielectric"] != ps["dielectric"]:
                continue
            # 电压：part 额定必须 >= 合同要求，否则排除
            if s.get("voltage") and ps.get("voltage") and ps["voltage"] < s["voltage"] - 1e-9:
                continue
            cands.append(p)
        if cands:
            return cands, "value"
    # 3) LED / 开关 / 其它：按颜色/关键词/去空格包含匹配
    q = ne_ns
    for c, e in LED_COLOR.items():
        if c in model:
            q = e
            break
    if "开关" in model or "key" in ne or "轻触" in model:
        subs = [p for p in parts
                if "key" in _ns(p.get("name")) or "开关" in (p.get("name") or "")]
        if subs:
            return subs, "substr"
    if q != ne_ns:  # 颜色已映射
        subs = [p for p in parts if q in _ns(p.get("name"))]
        if subs:
            return subs, "substr"
    subs = [p for p in parts
            if ne_ns in _ns(p.get("name")) or _ns(p.get("name")) in ne_ns]
    if subs:
        return subs, "substr"
    return [], "none"


def pick_best(cands, line):
    """从候选里挑最贴合的一个。返回 (best, conflict, scored)。

    评分维度（越高越贴合）：品牌一致(+3) > 公差一致(+2) > 电压一致(+1)
      > 介质一致(+1) > 封装一致(+1) > 该料已有采购记录(+1)。
    冲突判定：并列最高分且出现多个不同 id -> CONFLICT（歧义/重复料，需用户选）。
    唯一最高分 -> 直接采用（不冲突），由后续价格动作分类决定 SKIP/UPDATE/...
    """
    s = spec_of(line["model"])
    brand = line.get("brand")
    scored = []
    for p in cands:
        ps = spec_of(p.get("name", ""), (p.get("footprint") or {}).get("name"))
        score = 0
        if s.get("tol") is not None and ps.get("tol") is not None:
            score += 2 if abs(s["tol"] - ps["tol"]) < 1e-6 else 0
        elif s.get("tol") is None or ps.get("tol") is None:
            score += 1
        if s.get("voltage") and ps.get("voltage") and abs(s["voltage"] - ps["voltage"]) < 1e-6:
            score += 1
        if s.get("dielectric") and ps.get("dielectric") and s["dielectric"] == ps["dielectric"]:
            score += 1
        if s.get("pkg") and ps.get("pkg") and s["pkg"] == ps["pkg"]:
            score += 1
        man = p.get("manufacturer")
        mname = (man.get("name") if isinstance(man, dict) else None) or ""
        if _brand_match(brand, mname):
            score += 2
        if (p.get("orderdetails") or []):
            score += 1
        scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    best = scored[0][1]
    best_score = scored[0][0]
    tops = [p for sc, p in scored if sc == best_score]
    conflict = len(tops) > 1
    return best, conflict, scored


# ----------------------------- 之安传感价格动作分类 -----------------------------
def classify_price_action(part, contract_price, supplier_id):
    """给定 Part 与合同价，判断之安传感(supplier_id)下该怎么做。"""
    ods = part.get("orderdetails") or []
    hua = None
    for od in ods:
        sid = od.get("supplier")
        sid = sid.get("id") if isinstance(sid, dict) else sid
        if sid == supplier_id:
            hua = od
            break
    if hua is None:
        return "NEW_ORDER", None
    pds = hua.get("pricedetails") or []
    moq1 = [pd for pd in pds if (pd.get("min_discount_quantity") or 0) == 1]
    if moq1:
        pd = moq1[0]
        ppu = pd.get("price_per_unit")
        if ppu is not None and abs(float(ppu) - contract_price) < 1e-9:
            return "SKIP", pd
        return "UPDATE", pd
    return "ADD_TIER", None


# ----------------------------- 报告生成 -----------------------------
def source_date_from_name(fname):
    m = re.search(r"(\d{2})-(\d{1,2})-(\d{1,2})", fname)
    if m:
        y, mo, d = m.groups()
        return f"20{y}-{int(mo):02d}-{int(d):02d}"
    return "unknown"


def analyze(pdf_paths, db, supplier_name="之安传感"):
    # 供应商
    sup = db.get("/suppliers?name=" + urllib.parse.quote(supplier_name) + "&limit=20")
    supplier = None
    if sup:
        for x in sup.get("hydra:member", []):
            if (x.get("name") or "").find(supplier_name) >= 0:
                supplier = x
                break
        if supplier is None and sup.get("hydra:member"):
            supplier = sup["hydra:member"][0]
    supplier_id = supplier["id"] if supplier else None

    # 全量零件（列表，用于匹配型号；但 orderdetails 在列表里未展开，
    # 价格动作分类需逐个拉详情，见下方 detail_cache）
    parts = db.get_all("/parts")

    # 零件详情缓存（列表响应的 orderdetails 只是 IRI 引用，必须 GET /parts/{id} 才展开 supplier/pricedetails）
    detail_cache = {}

    def get_detail(pid):
        if pid not in detail_cache:
            detail_cache[pid] = db.get(f"/parts/{pid}")
        return detail_cache[pid]

    # 解析所有 PDF，按型号去重（同型号多合同 -> 取最新日期价）
    lines_by_model = {}
    for fp in pdf_paths:
        rows = extract_rows(fp)
        sd = source_date_from_name(os.path.basename(fp))
        for r in rows:
            key = _norm(r["model"])
            item = lines_by_model.get(key)
            if item is None:
                lines_by_model[key] = {
                    "model": r["model"], "brand": r["brand"],
                    "price": r["price"], "sources": [(sd, fp)],
                    "pkgs": set(), "brands": set()}
                item = lines_by_model[key]
            else:
                # 取最新日期价
                if sd > item["sources"][0][0]:
                    item["price"] = r["price"]
                item["sources"].append((sd, fp))
            item["pkgs"].add(spec_of(r["model"]).get("pkg") or "")
            item["brands"].add(r["brand"])

    items = []
    for key, it in lines_by_model.items():
        cands, reason = find_candidates(it, parts)
        rec = {
            "model": it["model"], "brand": it["brand"], "price": it["price"],
            "sources": [s[0] for s in it["sources"]],
            "match_reason": reason, "candidates": [], "part_id": None,
            "ipn": None, "action": None, "decision": "auto",
            "note": "", "supplier_id": supplier_id,
        }
        if not cands:
            rec["action"] = "NEW_PART"
            rec["decision"] = "auto"
            rec["note"] = "PartDB 无此型号，将新建料+之安传感价（制造商/分类/封装在写入时解析）"
            items.append(rec)
            continue
        best, conflict, scored = pick_best(cands, it)
        best_detail = get_detail(best["id"])
        rec["part_id"] = best["id"]
        rec["ipn"] = best_detail.get("ipn")
        rec["candidates"] = [{"id": p["id"], "ipn": get_detail(p["id"]).get("ipn"),
                               "name": p.get("name"),
                               "price_now": _hua_price(get_detail(p["id"]), supplier_id)} for p in cands]
        # 冲突判定：只要出现 2 个以上候选就交用户定（PartDB 这些料 name 不含公差，
        # 数据上无法区分 5%/1% 等，瞎猜代价高）。评分仅用于排"默认建议"。
        conflict = len(cands) > 1
        if conflict:
            rec["action"] = "CONFLICT"
            rec["decision"] = "needs_user"
            rec["note"] = "规格/公差冲突或多候选，默认建议用 part_id=%s；请在报告中改 chosen_part_id 或标 skip" % best["id"]
            items.append(rec)
            continue
        # 非冲突 -> 价格动作（必须用详情，列表未展开 orderdetails）
        act, pd = classify_price_action(best_detail, it["price"], supplier_id)
        rec["action"] = act
        rec["note"] = {"SKIP": "之安传感价已等于合同价",
                       "UPDATE": "就地更新现有 MOQ=1 档到合同价",
                       "ADD_TIER": "新增一档合同价（保留历史）",
                       "NEW_ORDER": "新建之安传感采购记录"}[act]
        items.append(rec)

    # 去重：同一 (part_id, action, price) 的多条记录合并为一条（如 ETA5055 在不同合同写法不同但都指向 P0058）
    dedup = {}
    for it in items:
        if it["action"] in ("NEW_PART", "CONFLICT") or it["part_id"] is None:
            dedup.setdefault(("newc", it["model"]), it)
            continue
        k = (it["part_id"], it["action"], it["price"])
        if k not in dedup:
            dedup[k] = it
    items = list(dedup.values())

    items.sort(key=lambda x: (x["action"] != "CONFLICT", x["action"], str(x["part_id"])))
    return {"supplier": supplier, "supplier_id": supplier_id,
            "items": items, "pdfs": [os.path.basename(p) for p in pdf_paths]}


def _hua_price(part, supplier_id):
    for od in (part.get("orderdetails") or []):
        sid = od.get("supplier")
        sid = sid.get("id") if isinstance(sid, dict) else sid
        if sid == supplier_id:
            pp = [pd.get("price_per_unit") for pd in (od.get("pricedetails") or [])]
            return pp
    return None


# ----------------------------- 写入 -----------------------------
def apply_report(report_path, db):
    rep = json.load(open(report_path, encoding="utf-8"))
    results = []
    for it in rep["items"]:
        action = it["action"]
        price = it["price"]
        sid = it["supplier_id"]
        if action == "SKIP":
            results.append((it["model"], "SKIP", "已一致，跳过"))
            continue
        if action == "CONFLICT":
            chosen = it.get("chosen_part_id")
            if chosen in (None, "", "skip"):
                results.append((it["model"], "CONFLICT-UNRESOLVED", "用户未选定，跳过"))
                continue
            part = db.get(f"/parts/{chosen}")
            act2, pd = classify_price_action(part, price, sid)
            it["_resolved_part"] = chosen
            it["_resolved_action"] = act2
            action = act2
            part_id = chosen
        else:
            part_id = it["part_id"]

        ok, msg = _do_write(db, action, part_id, sid, price, it)
        results.append((it["model"], action, msg))
    return results


def _do_write(db, action, part_id, supplier_id, price, it):
    try:
        if action == "NEW_PART":
            return _create_part_and_price(db, it, supplier_id, price)
        if action == "NEW_ORDER":
            # 新建 orderdetail + pricedetail
            od, s1, b1 = db.post("/orderdetails", {
                "part": f"/api/parts/{part_id}", "supplier": f"/api/suppliers/{supplier_id}",
                "supplierpartnr": "待补"})
            if s1 >= 400:
                return False, f"orderdetail 失败 {s1}: {b1[:200]}"
            odj = json.loads(b1)
            odid = odj.get("id") or (odj.get("@id") and odj["@id"].split("/")[-1])
            s2, b2 = db.post("/pricedetails", {
                "orderdetail": f"/api/orderdetails/{odid}", "price": price,
                "price_per_unit": price, "min_discount_quantity": 1,
                "price_related_quantity": 1})
            if s2 >= 400:
                return False, f"pricedetail 失败 {s2}: {b2[:200]}"
            return True, f"新建之安传感记录 OK (od={odid})"
        if action == "ADD_TIER":
            # 找该 part 的之安传感 orderdetail
            part = db.get(f"/parts/{part_id}")
            odid = None
            for od in (part.get("orderdetails") or []):
                s_ = od.get("supplier")
                s_ = s_.get("id") if isinstance(s_, dict) else s_
                if s_ == supplier_id:
                    odid = od["id"]
                    break
            if odid is None:
                return False, "找不到之安传感 orderdetail"
            s2, b2 = db.post("/pricedetails", {
                "orderdetail": f"/api/orderdetails/{odid}", "price": price,
                "price_per_unit": price, "min_discount_quantity": 1,
                "price_related_quantity": 1})
            if s2 >= 400:
                return False, f"新增档失败 {s2}: {b2[:200]}"
            return True, "新增价格档 OK"
        if action == "UPDATE":
            # 就地更新现有 MOQ=1 档（PATCH price + price_related_quantity）
            pd = it.get("_resolved_pd") or _find_moq1_pd(db, part_id, supplier_id)
            if pd is None:
                return False, "找不到 MOQ=1 价格档"
            s, b = db.patch(f"/pricedetails/{pd}", {
                "price": price, "price_related_quantity": 1,
                "price_per_unit": price})
            if s >= 400:
                return False, f"PATCH 失败 {s}: {b[:200]}"
            return True, "就地更新价 OK"
    except Exception as e:
        return False, f"异常: {e}"
    return False, "未知 action"


def _find_moq1_pd(db, part_id, supplier_id):
    part = db.get(f"/parts/{part_id}")
    for od in (part.get("orderdetails") or []):
        s_ = od.get("supplier")
        s_ = s_.get("id") if isinstance(s_, dict) else s_
        if s_ == supplier_id:
            for pd in (od.get("pricedetails") or []):
                if (pd.get("min_discount_quantity") or 0) == 1:
                    return pd["id"]
    return None


def _create_part_and_price(db, it, supplier_id, price):
    model = it["model"]
    s = spec_of(model)
    kind = {"cap": "cap", "res": "res", "ind": "ind"}.get(s["unit"])
    cat = db.resolve_category(kind) if kind else None
    fp = db.resolve_footprint(s.get("pkg"))
    man = db.resolve_manufacturer(it["brand"])
    body = {"name": model}
    if cat:
        body["category"] = f"/categories/{cat}"
    if fp:
        body["footprint"] = f"/footprints/{fp}"
    if man:
        body["manufacturer"] = f"/manufacturers/{man}"
    s1, b1 = db.post("/parts", body)
    if s1 >= 400:
        return False, f"建料失败 {s1}: {b1[:200]}"
    pj = json.loads(b1)
    pid = pj.get("id")
    # 设 ipn = P + 4位零填充 id（本项目约定）
    ipn = "P%04d" % pid
    db.patch(f"/parts/{pid}", {"ipn": ipn})
    # 建之安传感采购记录 + 价格
    s2, b2 = db.post("/orderdetails", {
        "part": f"/api/parts/{pid}", "supplier": f"/api/suppliers/{supplier_id}",
        "supplierpartnr": "待补"})
    if s2 >= 400:
        return False, f"orderdetail 失败 {s2}: {b2[:200]}"
    odj = json.loads(b2)
    odid = odj.get("id")
    s3, b3 = db.post("/pricedetails", {
        "orderdetail": f"/api/orderdetails/{odid}", "price": price,
        "price_per_unit": price, "min_discount_quantity": 1,
        "price_related_quantity": 1})
    if s3 >= 400:
        return False, f"pricedetail 失败 {s3}: {b3[:200]}"
    return True, f"新建料 part={pid} ipn={ipn} 价={price} OK"


# ----------------------------- 报告落盘 -----------------------------
def write_reports(rep, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, "partdb_import_report.json")
    mp = os.path.join(out_dir, "partdb_import_report.md")
    json.dump(rep, open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    lines = []
    lines.append("# PartDB 价格导入审核报告")
    lines.append("")
    lines.append(f"- 来源 PDF：{', '.join(rep['pdfs'])}")
    lines.append(f"- 供应商：{rep['supplier']['name'] if rep['supplier'] else '未找到'} (id={rep['supplier_id']})")
    lines.append("")
    counts = {}
    for it in rep["items"]:
        counts[it["action"]] = counts.get(it["action"], 0) + 1
    lines.append("## 汇总")
    lines.append("")
    lines.append("| 处置 | 数量 |")
    lines.append("|---|---|")
    for k in ["SKIP", "UPDATE", "ADD_TIER", "NEW_ORDER", "NEW_PART", "CONFLICT"]:
        if counts.get(k):
            lines.append(f"| {k} | {counts[k]} |")
    lines.append("")
    lines.append("## 明细（请审核，CONFLICT 需先选定）")
    lines.append("")
    lines.append("| 型号 | 品牌 | 合同价 | 动作 | PartDB | 备注 |")
    lines.append("|---|---|---|---|---|---|")
    for it in rep["items"]:
        pid = it.get("part_id") or it.get("chosen_part_id") or "-"
        lines.append(f"| {it['model']} | {it['brand']} | {it['price']} | "
                     f"{it['action']} | {pid} | {it.get('note','')} |")
    lines.append("")
    lines.append("## CONFLICT 候选（供选择）")
    lines.append("")
    lines.append("> 确认方式：在 `partdb_import_report.json` 中，为每个 CONFLICT 项添加 "
                 "`\"chosen_part_id\": <候选part_id>` 指定采用哪个料；留空或设为 `\"skip\"` 表示跳过该项。"
                 "改完后再执行 `apply --report ... --yes`。默认建议见下表 `<==默认`。")
    lines.append("")
    for it in rep["items"]:
        if it["action"] == "CONFLICT":
            lines.append(f"- **{it['model']}**（合同价 {it['price']}，来源 {it['sources']}）")
            for c in it["candidates"]:
                mark = " ← 默认建议" if c["id"] == it["part_id"] else ""
                lines.append(f"  - part_id={c['id']} ipn={c['ipn']} name=`{c['name']}` "
                             f"之安传感现价为 {c['price_now']}{mark}")
            lines.append("")
    open(mp, "w", encoding="utf-8").write("\n".join(lines))
    return jp, mp


# ----------------------------- CLI -----------------------------
def main():
    ap = argparse.ArgumentParser(description="采购合同 PDF -> PartDB 价格导入")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("analyze", help="只读分析，生成审核报告")
    a.add_argument("--pdf", required=True, help="PDF 路径或目录")
    a.add_argument("--out", default=None, help="报告输出目录（默认 PDF 同级）")

    b = sub.add_parser("apply", help="确认后写入 PartDB")
    b.add_argument("--report", required=True, help="report.json 路径")
    b.add_argument("--yes", action="store_true", help="确认执行写入")

    args = ap.parse_args()
    cfg = load_config()
    if not cfg.get("PARTDB_URL") or not cfg.get("PARTDB_TOKEN"):
        print("✗ 缺少 PartDB 凭据，请检查 ~/.qclaw/seatable-cache/config.env")
        sys.exit(1)
    db = PartDB(cfg["PARTDB_URL"], cfg["PARTDB_TOKEN"])

    if args.cmd == "analyze":
        paths = []
        if os.path.isdir(args.pdf):
            paths = sorted(glob.glob(os.path.join(args.pdf, "*.pdf")))
        else:
            paths = [args.pdf]
        if not paths:
            print("✗ 未找到 PDF")
            sys.exit(1)
        rep = analyze(paths, db)
        out = args.out or os.path.dirname(paths[0]) or "."
        jp, mp = write_reports(rep, out)
        print(f"✓ 分析完成，报告：\n  {jp}\n  {mp}")
        print(f"  供应商：{rep['supplier']['name'] if rep['supplier'] else '未找到'}")
        print(f"  共 {len(rep['items'])} 条")
        cs = {}
        for it in rep["items"]:
            cs[it["action"]] = cs.get(it["action"], 0) + 1
        print("  分类：" + "  ".join(f"{k}={v}" for k, v in cs.items()))
        n_conf = cs.get("CONFLICT", 0)
        if n_conf:
            print(f"⚠ {n_conf} 条为 CONFLICT，需用户在报告中选定后再 apply")
    elif args.cmd == "apply":
        if not args.yes:
            print("⚠ 这是写入操作。请在审核报告后加 --yes 确认执行。")
            sys.exit(0)
        results = apply_report(args.report, db)
        ok = sum(1 for r in results if r[2].startswith(("OK", "跳过", "已一致")) or "OK" in r[2])
        print(f"✓ 写入完成，{len(results)} 条：")
        for model, act, msg in results:
            print(f"  [{act}] {model} -> {msg}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
