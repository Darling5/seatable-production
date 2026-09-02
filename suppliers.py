#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""suppliers.py — 授权代理商官方 API 只读查价（得捷 DigiKey · 贸泽 Mouser）。

把两个渠道的差异抹平，对外只吐统一结构：
    {ok, source, source_key, mpn, manufacturer, desc, price, currency,
     price_cny, stock, lead_time, lifecycle, url, datasheet, qty, error}

四条铁律（改代码前先读）：
1. **只读**。只调查询类接口，绝不触碰下单 / 报价 / 购物车接口。
   本项目代理商凭证只用于查价与生命周期，下单必须人工在官网完成。
2. **凭证不落库**。只从本地 config.yaml 的 market.api_keys 读取；
   config.yaml 已被 .gitignore 排除。仓库里只有 config.yaml.example 的**空占位符**，
   从 GitHub clone 的人自己去官网申请。日志/报错里一律脱敏，不打印明文。
3. **失败降级**。网络超时 / 鉴权失败 / 型号查不到，一律返回 ok=False + error，
   **不抛异常**——批处理（market.py sync）遇到失败继续跑下一个，不中断整批。
4. **统一人民币**。默认展示与折算币种 ¥（CNY）。返回原始币种 + 折算后的 price_cny；
   汇率走免费接口，拿不到就退回 config 的静态汇率，并在结果里标注汇率来源。

CLI：
    python suppliers.py doctor                    # 凭证自检（脱敏）+ 可选 --live 连通实测
    python suppliers.py lookup FR8018HD           # 单型号全源查价
    python suppliers.py lookup FR8018HD --source digikey --qty 100
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TOKEN_CACHE = os.path.join(DATA, ".supplier_tokens.json")
FX_CACHE = os.path.join(DATA, ".fx_cache.json")

DEFAULT_TIMEOUT = 25

# 生命周期归一化：各渠道文案 → 本项目四态
LIFECYCLE_OK = "在产"
LIFECYCLE_WARN = "NRND"
LIFECYCLE_BAD = "EOL停产"
LIFECYCLE_UNK = "未知"

_EOL_KEYS = ("obsolete", "eol", "end of life", "discontinued", "停产", "已停产", "停止生产")
_NRND_KEYS = ("nrnd", "not recommended", "not for new design", "last time buy", "ltb",
              "不推荐", "不建议使用")
_OK_KEYS = ("active", "new product", "在产", "在售", "量产", "正常")


# ------------------------------------------------------------------ 配置
def _market_cfg():
    """读 config.yaml 的 market 段；拿不到返回空 dict（不抛异常）。"""
    try:
        from adapters.factory import load_config
        cfg = load_config() or {}
        return cfg.get("market") or {}
    except Exception:
        return {}


def _key_cfg(name):
    """取某个渠道的凭证配置；不存在返回 {}。"""
    return ((_market_cfg().get("api_keys") or {}).get(name) or {})


def _mask(s):
    """凭证脱敏：只留前 4 位，用于日志/报错。"""
    s = str(s or "")
    if not s:
        return "(未配置)"
    return s[:4] + "…" + ("(%d位)" % len(s))


def _dk_subscription_hint(body):
    """识别得捷「app 已创建但未订阅 API」这类 401，返回可操作指引。

    2026-09-02 实测取证：client_id/secret 有效、令牌能正常拿到（HTTP 200），
    但所有 /Search/v3/* 查询一律 401 且 ErrorDetails 为
    "You are not subscribed to this API. Please subscribe and try again."
    这跟「令牌过期」不是一回事——重试、清缓存、换端点都无效，
    必须在得捷开发者后台给 App 订阅 API Product。此处提前拦截，
    避免代码把配额浪费在无意义的自动重试上。
    """
    t = str(body or "")
    if "not subscribed" not in t.lower():
        return ""
    return ("得捷 App 未订阅 API（令牌可正常获取，但查询被拒）。"
            "请到 https://developer.digikey.com → My Apps → 选该 App → "
            "Subscribe to API Product，订阅 Product Search API（注意选 Production 而非 Sandbox），"
            "订阅生效后再跑一次 suppliers.py doctor --live。")


# ------------------------------------------------------------------ 汇率
def _static_fx():
    fx = (_market_cfg().get("fx") or {}).get("static") or {}
    return {str(k).upper(): float(v) for k, v in fx.items() if v}


def _fx_rate(cur, cfg=None):
    """返回 1 单位 cur 折合多少 CNY。CNY 恒为 1.0。

    优先级：当日缓存 → 免费汇率接口 → config 静态汇率 → 兜底 1.0（并在结果标注不明）。
    """
    cur = (cur or "CNY").upper().strip()
    if cur in ("CNY", "RMB", "¥", "YUAN"):
        return 1.0, "本币"

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if os.path.exists(FX_CACHE):
            c = json.load(open(FX_CACHE, "r", encoding="utf-8"))
            if c.get("date") == today and cur in (c.get("rates") or {}):
                return float(c["rates"][cur]), "缓存(%s)" % today
    except Exception:
        pass

    # 免费汇率接口（无需 key）
    try:
        import requests
        r = requests.get("https://api.frankfurter.app/latest",
                         params={"from": cur, "to": "CNY"}, timeout=10)
        if r.status_code == 200:
            rate = float(((r.json() or {}).get("rates") or {}).get("CNY"))
            if rate > 0:
                rates = {}
                try:
                    if os.path.exists(FX_CACHE):
                        old = json.load(open(FX_CACHE, "r", encoding="utf-8"))
                        if old.get("date") == today:
                            rates = old.get("rates") or {}
                except Exception:
                    pass
                rates[cur] = rate
                try:
                    os.makedirs(DATA, exist_ok=True)
                    json.dump({"date": today, "rates": rates},
                              open(FX_CACHE, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=2)
                except Exception:
                    pass
                return rate, "实时汇率"
    except Exception:
        pass

    st = _static_fx()
    if cur in st:
        return st[cur], "静态汇率(config)"
    return 1.0, "汇率未知"


def to_cny(price, currency):
    """折算人民币；(price_cny, rate, rate_src)。price 为空返回 (None, ...)。"""
    if price in (None, ""):
        return None, None, ""
    try:
        p = float(price)
    except Exception:
        return None, None, ""
    rate, src = _fx_rate(currency)
    return round(p * rate, 4), rate, src


# ------------------------------------------------------------------ 工具
def _map_lifecycle(text):
    t = (text or "").strip().lower()
    if not t:
        return LIFECYCLE_UNK
    for k in _EOL_KEYS:
        if k in t:
            return LIFECYCLE_BAD
    for k in _NRND_KEYS:
        if k in t:
            return LIFECYCLE_WARN
    for k in _OK_KEYS:
        if k in t:
            return LIFECYCLE_OK
    return LIFECYCLE_UNK


def _float(v):
    """从 '$4.49' / '¥ 31.9' / '1,234.5' 里抠出数字。"""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    m = re.search(r"-?\d[\d,]*\.?\d*", s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except Exception:
        return None


def _int(v):
    f = _float(v)
    return int(f) if f is not None else None


def _blank_offer(source_key, source_cn, mpn, error=""):
    return {"ok": False, "source": source_cn, "source_key": source_key, "mpn": mpn,
            "manufacturer": "", "desc": "", "price": None, "currency": "",
            "price_cny": None, "fx_rate": None, "fx_src": "",
            "stock": None, "lead_time": "", "lifecycle": LIFECYCLE_UNK,
            "url": "", "datasheet": "", "qty": 1, "error": error}


# ------------------------------------------------------------------ 得捷 DigiKey
class DigiKeySupplier(object):
    """DigiKey API：OAuth2 client_credentials，默认 Product Information **V4**，
    可配置回退 Search v3。

    文档要点（2026-09-02 实测核对）：
      - 令牌：POST https://api.digikey.com/v1/oauth2/token
              grant_type=client_credentials&client_id=..&client_secret=..（表单）
              生产与沙箱共用同一令牌端点路径（host 不同），令牌有效期约 10 分钟
      - V4 查询（App 需订阅 Product Information API V4）：
              POST /products/v4/search/keyword   body {"Keywords": mpn, "Limit": N}
              返回 Products[]：UnitPrice / QuantityAvailable / ProductStatus.Status
              （中文语言下直接给“在售/停产”）/ Discontinued / EndOfLife / ProductUrl
              注意：productdetails 端点**不返回真实库存**（2-legged 下恒 0），用 keyword
      - v3 查询（App 订阅的是旧 Product Search API 时用）：
              GET /Search/v3/Products/{mpn}
      - 必带头：X-DIGIKEY-Client-Id / Authorization: Bearer
      - locale 头决定币种与站点，本项目默认 CN/CNY；语言码差异：v3 用 zh，v4 用 zhs（简体）
      - 沙盒：sandbox-api.digikey.com（数据为假，仅供联调）
      - 订阅判别：令牌 200 但查询 401 “not subscribed” = App 没订阅对应 API 产品
    """
    key = "digikey"
    name_cn = "得捷"

    def __init__(self):
        self.cfg = _key_cfg("digikey")
        self.client_id = (self.cfg.get("client_id") or "").strip()
        self.client_secret = (self.cfg.get("client_secret") or "").strip()
        self.sandbox = bool(self.cfg.get("sandbox"))
        self.base = ("https://sandbox-api.digikey.com" if self.sandbox
                     else "https://api.digikey.com")
        self.api_version = str(self.cfg.get("api_version") or "v4").lower()
        self.site = (self.cfg.get("site") or "CN")
        lang = (self.cfg.get("language") or "zh")
        # 语言码差异：v3 用 zh，V4 用 zhs/zht（简/繁）。容忍旧配置写 zh。
        if self.api_version == "v4" and lang.lower() == "zh":
            lang = "zhs"
        self.language = lang
        self.currency = (self.cfg.get("currency") or "CNY")
        self.ship_to = (self.cfg.get("ship_to") or "CN")

    def configured(self):
        return bool(self.client_id and self.client_secret)

    # ---- 令牌
    def _load_token(self):
        try:
            if os.path.exists(TOKEN_CACHE):
                c = json.load(open(TOKEN_CACHE, "r", encoding="utf-8"))
                tk = (c.get("digikey") or {})
                if tk.get("access_token") and float(tk.get("expires_at") or 0) > time.time() + 60:
                    return tk["access_token"]
        except Exception:
            pass
        return None

    def _save_token(self, token, expires_in):
        try:
            os.makedirs(DATA, exist_ok=True)
            c = {}
            if os.path.exists(TOKEN_CACHE):
                try:
                    c = json.load(open(TOKEN_CACHE, "r", encoding="utf-8"))
                except Exception:
                    c = {}
            c["digikey"] = {"access_token": token,
                            "expires_at": time.time() + float(expires_in or 1800)}
            json.dump(c, open(TOKEN_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _token(self):
        tk = self._load_token()
        if tk:
            return tk, ""
        try:
            import requests
            r = requests.post(
                self.base + "/v1/oauth2/token",
                data={"grant_type": "client_credentials",
                      "client_id": self.client_id,
                      "client_secret": self.client_secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=DEFAULT_TIMEOUT)
            if r.status_code != 200:
                return None, "取令牌失败 HTTP %s（client_id=%s）：%s" % (
                    r.status_code, _mask(self.client_id), r.text[:180])
            d = r.json() or {}
            if not d.get("access_token"):
                return None, "取令牌失败：响应无 access_token —— %s" % str(d)[:180]
            self._save_token(d["access_token"], d.get("expires_in"))
            return d["access_token"], ""
        except Exception as e:
            return None, "取令牌异常：%s" % e

    # ---- 查价
    def lookup(self, mpn, qty=1):
        off = _blank_offer(self.key, self.name_cn, mpn)
        if not self.configured():
            off["error"] = "未配置凭证（config.yaml market.api_keys.digikey）"
            return off
        fn = self._lookup_v4 if self.api_version == "v4" else self._lookup_v3
        return fn(mpn, qty)

    def _headers(self, tk):
        h = {
            "X-DIGIKEY-Client-Id": self.client_id,
            "Authorization": "Bearer " + tk,
            "X-DIGIKEY-Locale-Site": self.site,
            "X-DIGIKEY-Locale-Language": self.language,
            "X-DIGIKEY-Locale-Currency": self.currency,
            "Accept": "application/json",
        }
        if self.api_version == "v3":
            h["X-DIGIKEY-Customer-Id"] = "0"
            h["X-DIGIKEY-Locale-ShipToCountry"] = self.ship_to
        return h

    def _drop_token(self):
        try:
            if os.path.exists(TOKEN_CACHE):
                c = json.load(open(TOKEN_CACHE, "r", encoding="utf-8"))
                c.pop("digikey", None)
                json.dump(c, open(TOKEN_CACHE, "w", encoding="utf-8"))
        except Exception:
            pass

    def _request(self, method, path, tk, json_body=None, params=None):
        """发一次查询。返回 (response, error)。401 时清令牌缓存由调用方重取。"""
        import requests
        try:
            r = requests.request(method, self.base + path, headers=self._headers(tk),
                                 json=json_body, params=params, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 401:
                sub = _dk_subscription_hint(r.text)
                if sub:
                    return None, sub
                self._drop_token()
            return r, ""
        except Exception as e:
            return None, "查询异常：%s" % e

    def _lookup_v4(self, mpn, qty=1):
        """Product Information API V4 keyword 搜索（实测信息最全：价/库存/生命周期）。"""
        off = _blank_offer(self.key, self.name_cn, mpn)
        tk, err = self._token()
        if not tk:
            off["error"] = err
            return off
        r, err = self._request("POST", "/products/v4/search/keyword", tk,
                               json_body={"Keywords": mpn, "Limit": 10})
        if r is None:
            off["error"] = err
            return off
        if r.status_code == 401:
            # 令牌过期：重取一次再试
            tk2, err2 = self._token()
            if not tk2:
                off["error"] = "鉴权失败（401）且刷新令牌失败：%s" % err2
                return off
            r, err = self._request("POST", "/products/v4/search/keyword", tk2,
                                   json_body={"Keywords": mpn, "Limit": 10})
            if r is None:
                off["error"] = err
                return off
        if r.status_code != 200:
            off["error"] = "查询失败 HTTP %s：%s" % (r.status_code, r.text[:180])
            return off
        d = r.json() or {}
        prods = d.get("Products") or []
        if not prods:
            off["error"] = "得捷无此型号"
            return off
        # 精确匹配优先（MPN 忽略大小写）；其次「前缀变体」—— 2SK3541 → 2SK3541T2L
        # （T2L 是包装编码，选型场景就该命中它）；同一 MPN 多个包装变体（TR/CT/Digi-Reel）
        # 挑库存最大的那条 —— 卷带变体库存常为 0，剪切带才有货，挑错就把库存显示成 0
        want = str(mpn).strip().lower()
        cands = [p for p in prods
                 if str(p.get("ManufacturerProductNumber") or "").strip().lower() == want]
        exact = bool(cands)
        if not cands:
            cands = [p for p in prods
                     if str(p.get("ManufacturerProductNumber") or "")
                     .strip().lower().startswith(want)]
        if not cands and (d.get("ExactMatches") or 0) > 0:
            cands = prods[:1]
        if not cands:
            near = "、".join(str(p.get("ManufacturerProductNumber") or "") for p in prods[:3])
            off["error"] = "得捷无此精确型号（最近似：%s）" % near
            return off
        pick = max(cands, key=lambda p: _int(p.get("QuantityAvailable")) or 0)
        off = self._to_offer_v4(pick, mpn, qty)
        if not exact and (off.get("mpn") or "").lower() != want:
            # 前缀变体命中：如实提示实际型号，避免误当精确匹配
            off["desc"] = ("[近似命中→%s] %s" % (off.get("mpn"), off.get("desc") or "")).strip()
        return off

    def _to_offer_v4(self, p, mpn, qty):
        off = _blank_offer(self.key, self.name_cn, mpn)
        price = _float(p.get("UnitPrice"))      # keyword 端点为 1 片单价（剪切带档）
        cur = ((p.get("SearchLocaleUsed") or {}).get("Currency")
               or ((p.get("SearchLocale") or {}).get("Currency")) or self.currency)
        cny, rate, src = to_cny(price, cur)
        status = ""
        if isinstance(p.get("ProductStatus"), dict):
            status = p["ProductStatus"].get("Status") or ""
        lc = _map_lifecycle(status)
        if p.get("Discontinued") or p.get("EndOfLife"):
            lc = LIFECYCLE_BAD
        off.update({
            "ok": True,
            "mpn": p.get("ManufacturerProductNumber") or mpn,
            "manufacturer": ((p.get("Manufacturer") or {}).get("Name") or ""),
            "desc": ((p.get("Description") or {}).get("ProductDescription") or ""),
            "price": price, "currency": cur, "price_cny": cny,
            "fx_rate": rate, "fx_src": src,
            "stock": _int(p.get("QuantityAvailable")),
            "lead_time": (p.get("ManufacturerLeadWeeks") or ""),
            "lifecycle": lc,
            "url": p.get("ProductUrl") or "",
            "datasheet": p.get("DatasheetUrl") or "",
            "qty": 1,
            "error": "",
        })
        return off

    def _lookup_v3(self, mpn, qty=1):
        off = _blank_offer(self.key, self.name_cn, mpn)
        tk, err = self._token()
        if not tk:
            off["error"] = err
            return off
        try:
            import requests
            headers = self._headers(tk)
            r = requests.get("%s/Search/v3/Products/%s" % (self.base, mpn),
                             headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 404:
                off["error"] = "得捷无此型号"
                return off
            if r.status_code == 401:
                # 订阅缺失类错误：重试也无用，直接给出可操作指引，别浪费配额
                sub = _dk_subscription_hint(r.text)
                if sub:
                    off["error"] = sub
                    return off
                # 令牌可能过期：清缓存后重试一次
                self._drop_token()
                tk2, err2 = self._token()
                if not tk2:
                    off["error"] = "鉴权失败（401）且刷新令牌失败：%s" % err2
                    return off
                headers["Authorization"] = "Bearer " + tk2
                r = requests.get("%s/Search/v3/Products/%s" % (self.base, mpn),
                                 headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code != 200:
                off["error"] = "查询失败 HTTP %s：%s" % (r.status_code, r.text[:180])
                return off
            p = (r.json() or {}).get("Product") or {}
            if not p:
                off["error"] = "得捷返回空结果"
                return off

            price = _float(p.get("UnitPrice"))
            cur = (p.get("SearchLocale") or {}).get("Currency") or self.currency
            cny, rate, src = to_cny(price, cur)
            status = ""
            if isinstance(p.get("ProductStatus"), dict):
                status = p["ProductStatus"].get("Status") or ""
            off.update({
                "ok": True,
                "mpn": p.get("ManufacturerProductNumber") or mpn,
                "manufacturer": ((p.get("Manufacturer") or {}).get("Name") or ""),
                "desc": ((p.get("Description") or {}).get("ProductDescription") or ""),
                "price": price, "currency": cur, "price_cny": cny,
                "fx_rate": rate, "fx_src": src,
                "stock": _int(p.get("QuantityAvailable")),
                "lead_time": (p.get("ManufacturerLeadWeeks") or ""),
                "lifecycle": _map_lifecycle(status),
                "url": p.get("ProductUrl") or "",
                "datasheet": p.get("DatasheetUrl") or "",
                "qty": 1,
                "error": "",
            })
            return off
        except Exception as e:
            off["error"] = "查询异常：%s" % e
            return off


# ------------------------------------------------------------------ 贸泽 Mouser
class MouserSupplier(object):
    """Mouser Search API v1：POST /api/v1/search/partnumber?apiKey=xxx

    文档要点（已实测核对）：
      - 凭证是单个 UUID，走 URL 查询参数，无 OAuth、无回调
      - 一次最多 **10 个型号**，用 | 分隔（这是省配额的关键）
      - 返回 SearchResults.Parts[]：PriceBreaks[{Quantity, Price, Currency}]
        / LifecycleStatus / Availability / LeadTime / ProductDetailUrl
      - Price 形如 '$4.49'，需抠数字；Availability 形如 '733 In Stock'
    """
    key = "mouser"
    name_cn = "贸泽"
    BATCH = 10

    def __init__(self):
        self.cfg = _key_cfg("mouser")
        self.api_key = (self.cfg.get("api_key") or "").strip()
        self.base = "https://api.mouser.com"

    def configured(self):
        return bool(self.api_key)

    def _post(self, mpns):
        """POST 一批（≤10）型号，返回 (parts_list, error)。"""
        try:
            import requests
            r = requests.post(
                self.base + "/api/v1/search/partnumber?apiKey=" + self.api_key,
                json={"SearchByPartRequest": {
                    "mouserPartNumber": "|".join(mpns),
                    "partSearchOptions": "Exact"}},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=DEFAULT_TIMEOUT)
            if r.status_code != 200:
                return None, "查询失败 HTTP %s：%s" % (r.status_code, r.text[:180])
            d = r.json() or {}
            errs = d.get("Errors") or []
            if errs:
                # 401/403 通常是 key 无效或未授权；仍尝试解析 parts
                msg = "; ".join(str(e.get("Message") or e) for e in errs[:2])
                parts = ((d.get("SearchResults") or {}).get("Parts") or [])
                if not parts:
                    return None, "接口报错：%s" % msg[:180]
            return ((d.get("SearchResults") or {}).get("Parts") or []), ""
        except Exception as e:
            return None, "查询异常：%s" % e

    @staticmethod
    def _pick_price(part, qty):
        """按目标数量挑价格档：取 Quantity<=qty 的最大档；没有就取最小档。"""
        breaks = part.get("PriceBreaks") or []
        if not breaks:
            return None, "", 1
        rows = []
        for b in breaks:
            q = _int(b.get("Quantity"))
            p = _float(b.get("Price"))
            if q is None or p is None:
                continue
            rows.append((q, p, b.get("Currency") or ""))
        if not rows:
            return None, "", 1
        rows.sort(key=lambda x: x[0])
        hit = rows[0]
        for q, p, c in rows:
            if q <= qty:
                hit = (q, p, c)
        return hit[1], hit[2], hit[0]

    def _to_offer(self, part, mpn, qty, error=""):
        off = _blank_offer(self.key, self.name_cn, mpn, error)
        price, cur, bq = self._pick_price(part, qty)
        cny, rate, src = to_cny(price, cur or "USD")
        off.update({
            "ok": True,
            "mpn": part.get("ManufacturerPartNumber") or mpn,
            "manufacturer": part.get("Manufacturer") or "",
            "desc": part.get("Description") or "",
            "price": price, "currency": cur or "USD", "price_cny": cny,
            "fx_rate": rate, "fx_src": src,
            "stock": _int(part.get("AvailabilityInStock") or part.get("Availability")),
            "lead_time": part.get("LeadTime") or "",
            "lifecycle": _map_lifecycle(part.get("LifecycleStatus")),
            "url": part.get("ProductDetailUrl") or "",
            "datasheet": part.get("DataSheetUrl") or "",
            "qty": bq,
            "error": "",
        })
        return off

    def lookup(self, mpn, qty=1):
        if not self.configured():
            return _blank_offer(self.key, self.name_cn, mpn,
                                "未配置凭证（config.yaml market.api_keys.mouser）")
        parts, err = self._post([mpn])
        if parts is None:
            return _blank_offer(self.key, self.name_cn, mpn, err)
        if not parts:
            return _blank_offer(self.key, self.name_cn, mpn, "贸泽无此型号")
        return self._to_offer(parts[0], mpn, qty)

    def lookup_batch(self, mpns, qty=1):
        """批量查（内部按 10 个/批切分）。返回 {mpn: offer}。"""
        out = {m: _blank_offer(self.key, self.name_cn, m) for m in mpns}
        if not self.configured():
            for m in out:
                out[m]["error"] = "未配置凭证（config.yaml market.api_keys.mouser）"
            return out
        for i in range(0, len(mpns), self.BATCH):
            chunk = mpns[i:i + self.BATCH]
            parts, err = self._post(chunk)
            if parts is None:
                for m in chunk:
                    out[m]["error"] = err
                continue
            got = {}
            for p in parts:
                key = (p.get("ManufacturerPartNumber") or "").upper()
                got.setdefault(key, p)
            for m in chunk:
                p = got.get(m.upper())
                if p:
                    out[m] = self._to_offer(p, m, qty)
                else:
                    out[m]["error"] = "贸泽无此型号"
        return out


SUPPLIERS = {c.key: c for c in (DigiKeySupplier, MouserSupplier)}
SOURCE_CN = {"digikey": "得捷", "mouser": "贸泽"}


def get_supplier(name):
    return SUPPLIERS.get(name)()


def available_sources():
    """已配置凭证的渠道列表。"""
    return [k for k, cls in SUPPLIERS.items() if cls().configured()]


def lookup(mpn, sources=None, qty=1):
    """查一个型号；sources=None 表示所有已配置渠道。返回 offer 列表。"""
    srcs = sources or available_sources()
    out = []
    for s in srcs:
        cls = SUPPLIERS.get(s)
        if not cls:
            out.append(_blank_offer(s, SOURCE_CN.get(s, s), mpn, "未知渠道"))
            continue
        out.append(cls().lookup(mpn, qty=qty))
    return out


def lookup_many(mpns, sources=None, qty=1):
    """批量查多个型号；Mouser 走 10 个/批以省配额，DigiKey 逐条。

    返回 {mpn: [offer, ...]}。
    """
    srcs = sources or available_sources()
    res = {m: [] for m in mpns}
    if "mouser" in srcs:
        batch = MouserSupplier().lookup_batch(mpns, qty=qty)
        for m in mpns:
            res[m].append(batch.get(m))
    if "digikey" in srcs:
        dk = DigiKeySupplier()
        for m in mpns:
            res[m].append(dk.lookup(m, qty=qty))
    for s in srcs:
        if s in ("mouser", "digikey"):
            continue
        cls = SUPPLIERS.get(s)
        if cls:
            inst = cls()
            for m in mpns:
                res[m].append(inst.lookup(m, qty=qty))
    return res


# ------------------------------------------------------------------ CLI
def _fmt_offer(o):
    if not o.get("ok"):
        return "  %-6s ✗ %s" % (o["source"], o.get("error") or "无结果")
    price = o.get("price")
    cur = o.get("currency") or ""
    pn = ("%s %s" % (cur, price)) if price is not None else "—"
    cn = ("¥%s" % o["price_cny"]) if o.get("price_cny") is not None else "—"
    lc = o.get("lifecycle") or LIFECYCLE_UNK
    flag = " ⚠" if lc in (LIFECYCLE_WARN, LIFECYCLE_BAD) else ""
    return ("  %-6s ✓ %-14s（原 %s）= %-10s 库存 %-9s 生命周期 %s%s  %s" % (
        o["source"], o.get("mpn") or "", pn, cn,
        o.get("stock") if o.get("stock") is not None else "—",
        lc, flag, o.get("url") or ""))


def cmd_doctor(live=False, probe_mpn=None):
    print("渠道凭证自检（值已脱敏，不打印明文）")
    print("-" * 62)
    any_ok = False
    for key, cls in SUPPLIERS.items():
        inst = cls()
        cn = SOURCE_CN.get(key, key)
        if not inst.configured():
            print("  %-6s ✗ 未配置 —— 在 config.yaml 的 market.api_keys.%s 填写" % (cn, key))
            continue
        if key == "digikey":
            shown = "client_id=%s secret=%s %s %s/%s" % (
                _mask(inst.client_id), _mask(inst.client_secret),
                inst.api_version, inst.site, inst.currency)
        else:
            shown = "api_key=%s" % _mask(inst.api_key)
        print("  %-6s ✓ 已配置（%s）" % (cn, shown))
        any_ok = True
    print("-" * 62)
    if not any_ok:
        print("没有任何渠道配置凭证。复制 config.yaml.example 的 market.api_keys 段填写后重试。")
        return
    if not live:
        print("加 --live 做真实联网实测（会消耗一次 API 配额）。")
        return
    mpn = probe_mpn or "GRM155R71C104KA88D"  # 村田 0402 100nF，两渠道都有
    print("联网实测：探测型号 %s" % mpn)
    for o in lookup(mpn):
        print(_fmt_offer(o))


def cmd_lookup(mpn, source=None, qty=1):
    srcs = [source] if source else available_sources()
    if not srcs:
        print("没有已配置凭证的渠道，先填 config.yaml 的 market.api_keys。")
        return
    print("查价：%s（目标数量 %d）" % (mpn, qty))
    for o in lookup(mpn, sources=srcs, qty=qty):
        print(_fmt_offer(o))


def main():
    ap = argparse.ArgumentParser(description="得捷/贸泽官方 API 只读查价")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor", help="凭证自检 + 联网实测")
    p.add_argument("--live", action="store_true", help="真实联网探测（消耗配额）")
    p.add_argument("--mpn", help="指定探测型号，默认村田 GRM155R71C104KA88D")
    p = sub.add_parser("lookup", help="查单个型号")
    p.add_argument("model")
    p.add_argument("--source", choices=sorted(SUPPLIERS.keys()))
    p.add_argument("--qty", type=int, default=1, help="目标采购量，用于挑价格档")
    a = ap.parse_args()
    if a.cmd == "doctor":
        cmd_doctor(a.live, a.mpn)
    else:
        cmd_lookup(a.model, a.source, a.qty)


if __name__ == "__main__":
    main()
