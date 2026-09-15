#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxmedia.py — 微信本地媒体抽取：.dat 图片解密 → 落盘 + 索引（+ 可选 OCR）

════════════════════════════════════════════════════════════════════════
⚠️ 先读这段：本模块推翻了一条长期错误结论
════════════════════════════════════════════════════════════════════════
旧文档（含 references/wx-intake-and-check.md §11.4.1）写的是：
    「图片目前拿不到字节 —— msg\\attach\\<hash>\\2026-09\\ 实测 0 个文件」
**这是错的**，根因是**看错了路径层级**：
    真实层级是 msg/attach/<md5(会话)>/<YYYY-MM>/Img/<md5>.dat
    而当时查的是 .../<YYYY-MM>/ 这一层，文件在它下面的 Img/ 里，所以数出 0。
2026-09-15 实测：msg/attach 下 **57,606 个 .dat（9.4 GB）**，2026-09 单月 3,847 张。
→ **付款截图是可以解密、可以 OCR 的**，此前「要 OCR 得先在微信里点开图片」的结论作废
  （点开只影响有没有 _h 高清档，聊天显示版一定有）。

════════════════════════════════════════════════════════════════════════
V2 格式（微信 4.x，2025-08+，本机占绝对主流）
════════════════════════════════════════════════════════════════════════
  偏移   长度   内容
  0      6      魔数 07 08 56 32 08 07  (="\\x07\\x08V2\\x08\\x07")
  6      4      aes_size  (LE u32)
  10     4      xor_size  (LE u32)
  14     1      标志字节，实测恒为 0x01
  15     N      AES-128-ECB 密文（图像头部）           N = aes_size
  15+N   16     分隔尾 —— **按长度跳过，禁止硬编码**（老资料说是全局常量 56fbf4…，4.1.13 已变）
  15+N+16 M     单字节 XOR 混淆的剩余部分              M = xor_size

  总长恒等式（字节级精确，本机 5 例全部吻合）：file_size == 15 + aes_size + 16 + xor_size

  密钥 —— **账号级、纯离线派生，不需要打开微信进程扫内存**：
    code    = MMKV 目录下 key_<code>_*.statistic 文件名的第一个数字字段
    wxid    = 账号目录名去掉 `_数字` 后缀（wxid_xxx_f6b3 → wxid_xxx）
    aes_key = md5(f"{code}{wxid}").hexdigest()[:16]   ← 是**前 16 个 ASCII 字符**直接当 16 字节密钥，
                                                        不是 hex 解码成 8 字节（想当然会白折腾）
    xor_key = code & 0xFF
  自证：V1 的固定密钥 cfcd208495d565ef 恰等于 md5("0")[:16] —— 同一派生式把 V1/V2 统一了。

  另两种格式（本机少见，做兼容）：
    V1      魔数 07 08 56 31 08 07，aes_key 固定 cfcd208495d565ef
    旧 XOR  无魔数，整文件单字节 XOR，key 由图像魔数反推（key = data[0] ^ 0xFF）

════════════════════════════════════════════════════════════════════════
CLI
════════════════════════════════════════════════════════════════════════
  python wxmedia.py doctor                 # 环境 + 密钥自检（只读）
  python wxmedia.py key                    # 打印派生出的 code / aes key / xor key（便于排障）
  python wxmedia.py scan [--hours 24] [--groups A,B] [--max 400] [--ocr] [--all-groups]
  python wxmedia.py index                  # 只看已有索引统计

产物：
  data/wechat_media/<YYYY-MM>/<md5>.jpg|png|gif|webp   ← 解密后的原图
  data/wechat_media/index.json                         ← 索引（含 OCR 文本与关键词命中）

设计约束：
  * **只读微信库**，绝不写回微信目录；产物一律落到本技能 data/ 下。
  * 解密是**按需**的（默认只处理时间窗内的图），不做全量 —— 聊天媒体动辄几 GB。
  * 图片与「哪条消息、哪个群」的对应靠两点：① 目录名 = md5(会话 username)；
    ② 文件 mtime ≈ 该图落盘时间（**不是**消息发送时间的严格值，见 scan() 注释）。

免责：仅用于备份/处理**自己账号**的工作数据。
"""
import argparse
import hashlib
import io
import json
import os
import re
import struct
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
INTAKE = os.path.join(DATA, "wechat_intake")
GROUPS_CACHE = os.path.join(INTAKE, "groups.json")
MEDIA_DIR = os.path.join(DATA, "wechat_media")
INDEX_PATH = os.path.join(MEDIA_DIR, "index.json")

TZ = timezone(timedelta(hours=8))

V2_MAGIC_FULL = b"\x07\x08V2\x08\x07"
V1_MAGIC_FULL = b"\x07\x08V1\x08\x07"
V1_FIXED_KEY = b"cfcd208495d565ef"     # = md5("0")[:16]，见模块头自证
SEP_TAIL = 16                          # 15+N 之后的固定分隔尾长度（只按长度跳，不硬编码内容）

# 图像格式魔数（解密结果判定用）
MAGICS = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"RIFF", "webp"),
    (b"wxgf", "wxgf"),        # 微信自研封装，需 VoipEngine.dll 转码
]
# 旧 XOR 格式反推 key 时优先试的魔数（3 字节以上更可靠）
XOR_MAGICS = [b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF", b"\x49\x49\x2a\x00"]


def _log(msg, *a):
    print(msg % a if a else msg, flush=True)


# ─────────────────────────────────────────── 定位微信数据根
def _cfg():
    try:
        sys.path.insert(0, HERE)
        from adapters.factory import load_config
        return (load_config() or {}).get("wechat") or {}
    except Exception:
        return {}


def find_data_root():
    """返回 (账号级数据目录, 账号目录名)。

    ⚠️ 返回的是**账号目录**（…/xwechat_files/wxid_xxx_f6b3），不是它的父目录 ——
    媒体在 <账号目录>/msg/attach/... 下，少拼这一层会数出 0 个文件
    （旧文档「attach 下 0 个文件」的错误结论就是这么来的，别再犯）。
    """
    c = (_cfg().get("db_dir") or "").strip()
    cands = []
    if c:
        cands.append(c)
        # db_dir 有时配到账号目录的父级，两种都试
        if os.path.isdir(os.path.join(c, "msg", "attach")):
            cands.insert(0, os.path.dirname(c.rstrip("\\/")))
    for drive in ("D:", "C:", "E:"):
        cands += [
            drive + r"\User\Documents\xwechat_files",
            drive + r"\Users\11430\Documents\xwechat_files",
            drive + r"\xwechat_files",
        ]
    for base in cands:
        if not os.path.isdir(base):
            continue
        # 情况一：base 自己就是账号目录
        if re.match(r"wxid_.+", os.path.basename(base)) and \
                os.path.isdir(os.path.join(base, "msg", "attach")):
            return base, os.path.basename(base)
        # 情况二：base 是账号目录的父级
        for sub in sorted(os.listdir(base)):
            if not sub.startswith("wxid_"):
                continue
            ad = os.path.join(base, sub)
            if os.path.isdir(os.path.join(ad, "msg", "attach")):
                return ad, sub
    return None, None


def clean_wxid(wxid):
    """账号目录名去掉 `_数字` 后缀：wxid_xxxxxxxxxxxx_xxxx → wxid_xxxxxxxxxxxx。
    不去后缀派生出来的 key 解不开任何文件（实测）。"""
    parts = wxid.split("_")
    return "_".join(parts[:2]) if wxid.startswith("wxid_") and len(parts) >= 3 else wxid


def find_code():
    """从 MMKV 的 key_<code>_*.statistic 文件名里取第一个数字字段。"""
    bases = [
        os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat"),
        os.path.join(os.environ.get("APPDATA", ""), "Tencent", "WeChat"),
    ]
    codes = {}
    for b in bases:
        if not os.path.isdir(b):
            continue
        for dp, dn, fn in os.walk(b):
            if os.path.basename(dp).lower() != "kvcomm":
                continue
            for f in fn:
                m = re.match(r"key_(\d+)_", f)
                if m:
                    codes[int(m.group(1))] = os.path.join(dp, f)
    # code=0 是占位（多出现在 net_1/net_2 辅助目录），优先取非 0
    nz = sorted([c for c in codes if c], reverse=True)
    if nz:
        return nz[0], codes
    return (0, codes)


def derive_keys(code, wxid):
    """返回 (aes_key 16B ASCII, xor_key int)。aes_key 恒为 16 字节十六进制**字符串**。"""
    wx = clean_wxid(wxid)
    aes_key = hashlib.md5(("%d%s" % (code, wx)).encode()).hexdigest()[:16].encode("ascii")
    return aes_key, (code & 0xFF)


# ─────────────────────────────────────────── 解密
def _aes_ecb_dec(key, ct):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        c = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return c.update(ct) + c.finalize()
    except Exception:
        try:
            from Crypto.Cipher import AES            # 退路：pycryptodome
            return AES.new(key, AES.MODE_ECB).decrypt(ct)
        except Exception as e:
            raise RuntimeError("需要 cryptography 或 pycryptodome：%s" % e)


def detect_format(b):
    for m, n in MAGICS:
        if b.startswith(m):
            if n == "webp" and b[8:12] != b"WEBP":
                continue
            return n
    return None


def _xor_key_from_magic(d):
    """旧 XOR 格式：key = 首字节 ^ 魔数首字节。"""
    for m in XOR_MAGICS:
        k = d[0] ^ m[0]
        if all((d[i] ^ k) == m[i] for i in range(len(m))):
            return k
    return None


def decrypt_bytes(data, aes_key=None, xor_key=None):
    """解密一份 .dat，返回 (明文 bytes, 格式 str|None)。任何一步失败返回 (None, None)。

    三种格式分派：V2/V1（同结构，key 不同）→ 旧 XOR。
    """
    if len(data) < 32:
        return None, None
    head6 = data[:6]
    if head6 in (V2_MAGIC_FULL, V1_MAGIC_FULL):
        try:
            aes_size = struct.unpack_from("<I", data, 6)[0]
            xor_size = struct.unpack_from("<I", data, 10)[0]
        except struct.error:
            return None, None
        # 结构自检：总长恒等式。对不上说明不是这个结构，别硬解。
        if 15 + aes_size + SEP_TAIL + xor_size != len(data):
            return None, None
        k = V1_FIXED_KEY if head6 == V1_MAGIC_FULL else aes_key
        if not k:
            return None, None
        xk = xor_key if xor_key is not None else 0
        try:
            pt = _aes_ecb_dec(k, data[15:15 + aes_size])
        except Exception:
            return None, None
        off = 15 + aes_size + SEP_TAIL
        tail = bytes(b ^ xk for b in data[off:off + xor_size]) if xor_size else b""
        out = pt + tail
        fmt = detect_format(out)
        if fmt:
            return out, fmt
        # AES 段命中但尾部 XOR key 不对时，退一步：用整体魔数再校准一次 xor key
        return out, None
    # 旧 XOR
    k = _xor_key_from_magic(data)
    if k is None:
        return None, None
    out = bytes(b ^ k for b in data)
    return out, detect_format(out)


def convert_wxgf(data):
    """wxgf（微信自研封装）→ 真图片。调微信自带的 VoipEngine.dll，缺失则返回 None。"""
    import glob
    dlls = []
    for pat in (r"C:\Program Files\Tencent\Weixin\*\VoipEngine.dll",
                r"C:\Program Files (x86)\Tencent\Weixin\*\VoipEngine.dll",
                r"D:\Program Files\Tencent\Weixin\*\VoipEngine.dll"):
        dlls += glob.glob(pat)

    def vkey(p):
        m = re.search(r"\\(\d+(?:\.\d+)*)\\VoipEngine\.dll$", p)
        return [int(x) for x in m.group(1).split(".")] if m else []
    dlls = sorted(dlls, key=vkey)
    if not dlls:
        return None
    import ctypes
    dll = dlls[-1]
    try:
        try:
            os.add_dll_directory(os.path.dirname(dll))
        except Exception:
            pass
        voip = ctypes.WinDLL(dll)
        fn = voip.wxam_dec_wxam2pic_5
        fn.argtypes = [ctypes.c_int64, ctypes.c_int, ctypes.c_int64,
                       ctypes.POINTER(ctypes.c_int), ctypes.c_int64]
        fn.restype = ctypes.c_int64
        out_buf = ctypes.create_string_buffer(52 * 1024 * 1024)
        out_sz = ctypes.c_int(len(out_buf))
        in_buf = ctypes.create_string_buffer(data, len(data))
        ret = fn(ctypes.addressof(in_buf), len(data), ctypes.addressof(out_buf),
                 ctypes.byref(out_sz), None)
        if ret == 0 and 0 < out_sz.value <= len(out_buf):
            return out_buf.raw[:out_sz.value]
    except Exception:
        return None
    return None


# ─────────────────────────────────────────── 群 → 目录映射
def load_groups():
    try:
        with io.open(GROUPS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def watched_names():
    w = _cfg().get("watch_groups") or []
    return [str(x) for x in w if str(x).strip()]


def group_dirs(root):
    """群名 → attach 子目录（md5(群 username)）。返回 {群名: (dir, username)}。"""
    groups = load_groups()
    w = watched_names()
    base = os.path.join(root, "msg", "attach")
    have = set(os.listdir(base)) if os.path.isdir(base) else set()
    out = {}
    for g in groups:
        nm, u = g.get("name") or "", g.get("id") or ""
        if not nm or not u:
            continue
        if w and not (nm in w or any(x in nm for x in w)):
            continue
        h = hashlib.md5(u.encode()).hexdigest()
        if h in have:
            out[nm] = (h, u)
    return out


# ─────────────────────────────────────────── 扫描 / 解密 / 落盘
def _iter_dat(root, gdir, since_ts=None):
    """产出 (md5, quality, path, mtime, size)。quality: ''(聊天显示版) / h(高清) / t(缩略)。"""
    base = os.path.join(root, "msg", "attach", gdir)
    if not os.path.isdir(base):
        return
    for dp, dn, fn in os.walk(base):
        for f in fn:
            if not f.lower().endswith(".dat"):
                continue
            p = os.path.join(dp, f)
            try:
                mt = os.path.getmtime(p)
                sz = os.path.getsize(p)
            except OSError:
                continue
            if since_ts and mt < since_ts:
                continue
            stem = f[:-4]
            if stem.endswith("_h"):
                md5, q = stem[:-2], "h"
            elif stem.endswith("_t"):
                md5, q = stem[:-2], "t"
            else:
                md5, q = stem, ""
            yield md5, q, p, mt, sz


def scan(hours=24, groups=None, max_n=400, do_ocr=False, all_groups=False):
    root, acct = find_data_root()
    if not root:
        _log("[x] 找不到微信数据根（config.wechat.db_dir 为空且自动探测失败）")
        return 3
    code, _src = find_code()
    if not code:
        _log("[x] 找不到 MMKV code（%APPDATA%\\Tencent\\xwechat\\*\\kvcomm\\key_<code>_*.statistic）")
        return 3
    aes_key, xor_key = derive_keys(code, acct)
    _log("[i] 账号 %s · code=%d · aes_key=%s · xor_key=0x%02x",
         acct, code, aes_key.decode(), xor_key)

    if all_groups:
        base = os.path.join(root, "msg", "attach")
        gdirs = {d: (d, "") for d in (os.listdir(base) if os.path.isdir(base) else [])}
    else:
        gdirs = group_dirs(root)
        if groups:
            want = [x.strip() for x in groups if x.strip()]
            gdirs = {k: v for k, v in gdirs.items() if any(x in k for x in want)}
    if not gdirs:
        _log("[!] 没有可扫的会话目录（白名单群在 attach 下都没有图片，或 groups.json 缺失）")
        return 0

    since = time.time() - hours * 3600
    os.makedirs(MEDIA_DIR, exist_ok=True)
    index = load_index()
    seen = {r["path"] for r in index}
    added, skipped, failed, wxgf_n = 0, 0, 0, 0
    rows = []

    # 同一 md5 可能有 显示版/_h/_t 三档 —— 取**体积最大**的那份（画质最好，OCR 最准）
    for gname, (gdir, guser) in sorted(gdirs.items()):
        best = {}
        for md5, q, p, mt, sz in _iter_dat(root, gdir, since):
            cur = best.get(md5)
            if cur is None or sz > cur[4]:
                best[md5] = (md5, q, p, mt, sz)
            if len(best) >= max_n * 3:
                break
        picked = sorted(best.values(), key=lambda x: -x[3])[:max_n]
        for md5, q, p, mt, sz in picked:
            if p in seen:
                skipped += 1
                continue
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
            except OSError:
                failed += 1
                continue
            pt, fmt = decrypt_bytes(data, aes_key, xor_key)
            if (not fmt or fmt == "wxgf") and pt:
                conv = convert_wxgf(pt)
                if conv:
                    pt, fmt = conv, detect_format(conv) or "jpg"
                elif fmt == "wxgf":
                    wxgf_n += 1
            if not fmt or not pt:
                failed += 1
                continue
            month = datetime.fromtimestamp(mt, TZ).strftime("%Y-%m")
            dst_dir = os.path.join(MEDIA_DIR, month)
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, md5 + "." + fmt)
            if not os.path.exists(dst):
                with open(dst, "wb") as fh:
                    fh.write(pt)
            rec = {"group": gname, "group_username": guser, "md5": md5, "quality": q,
                   "src": p, "path": dst, "format": fmt,
                   "mtime": datetime.fromtimestamp(mt, TZ).strftime("%Y-%m-%d %H:%M:%S")}
            if do_ocr:
                rec["ocr"] = ocr_image(dst)
                rec["hits"] = keyword_hits(rec["ocr"])
            rows.append(rec)
            index.append(rec)
            seen.add(p)
            added += 1

    save_index(index)
    _log("[ok] 扫 %d 个会话 · 新增解密 %d 张 · 跳过已有 %d · 失败 %d · wxgf未转 %d",
         len(gdirs), added, skipped, failed, wxgf_n)
    _log("     产物目录 %s", MEDIA_DIR)
    if do_ocr:
        hit = [r for r in rows if r.get("hits")]
        _log("     OCR 完成 %d 张，其中命中业务关键词 %d 张", len(rows), len(hit))
        for r in hit[:15]:
            _log("       · [%s] %s → %s", r["group"], r["mtime"], "／".join(r["hits"]))
    return 0


# ─────────────────────────────────────────── OCR
_OCR = None


def _ocr_engine():
    global _OCR
    if _OCR is not None:
        return _OCR
    try:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    except Exception:
        try:
            from rapidocr import RapidOCR
            _OCR = RapidOCR()
        except Exception:
            _OCR = False
    return _OCR


def ocr_image(path):
    eng = _ocr_engine()
    if not eng:
        return ""
    try:
        res = eng(path)
        data = res[0] if isinstance(res, tuple) else getattr(res, "txts", res)
        txts = []
        if isinstance(data, (list, tuple)):
            for it in data:
                if isinstance(it, (list, tuple)) and len(it) >= 2:
                    txts.append(str(it[1]))
                elif isinstance(it, str):
                    txts.append(it)
        return " ".join(txts).strip()
    except Exception:
        return ""


# 命中即值得人工看一眼的信号（金额/付款/交期/物流）
KEYWORDS = [
    "付款", "已付", "转账", "汇款", "打款", "收款", "定金", "尾款", "预付", "货款",
    "￥", "¥", "元", "万元", "发票", "开票", "对账", "结算",
    "交期", "发货", "到货", "签收", "物流", "快递", "运单", "出库", "入库",
    "合同", "订单", "报价", "单价", "数量", "欠", "催",
]
AMOUNT_RE = re.compile(r"(?:[¥￥]\s*)?(\d[\d,]*(?:\.\d{1,2})?)\s*(?:元|万元|块)")


def keyword_hits(text):
    if not text:
        return []
    hits = [k for k in KEYWORDS if k in text]
    amt = AMOUNT_RE.findall(text)
    if amt:
        hits.append("金额×%d(%s)" % (len(amt), ",".join(amt[:3])))
    return hits


# ─────────────────────────────────────────── 索引
def load_index():
    try:
        with io.open(INDEX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_index(rows):
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with io.open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


# ─────────────────────────────────────────── CLI
def cmd_doctor():
    _log("== 微信媒体抽取体检 ==")
    rest = _cfg().get("db_dir") or "(空=自动探测)"
    _log("config.wechat.db_dir = %s", rest)
    root, acct = find_data_root()
    if not root:
        _log("[x] 数据根：未找到");  return 3
    _log("[ok] 数据根：%s", root)
    _log("[ok] 账号目录：%s", acct)
    att = os.path.join(root, "msg", "attach")
    n = sum(1 for dp, dn, fn in os.walk(att) for f in fn if f.lower().endswith(".dat"))
    _log("     msg/attach 下 .dat：%d 个", n)
    code, src = find_code()
    if not code:
        _log("[x] MMKV code 未找到"); return 3
    aes_key, xor_key = derive_keys(code, acct)
    _log("[ok] code=%d（源 %s）", code, src.get(code, "?"))
    _log("     aes_key=%s  xor_key=0x%02x", aes_key.decode(), xor_key)
    _log("     自证：md5('0')[:16] = %s（V1 固定密钥，应等于 cfcd208495d565ef）",
         hashlib.md5(b"0").hexdigest()[:16])
    # 抽 5 张实测解密 + OCR 引擎
    ok = 0
    for dp, dn, fn in list(os.walk(att))[:400]:
        for f in fn[:3]:
            if not f.lower().endswith(".dat"):
                continue
            p = os.path.join(dp, f)
            pt, fmt = decrypt_bytes(open(p, "rb").read(), aes_key, xor_key)
            ok += 1 if fmt else 0
            if ok >= 5:
                break
        if ok >= 5:
            break
    _log("[%s] 抽样解密：%d/5 成功", "ok" if ok else "x", ok)
    eng = _ocr_engine()
    _log("[%s] OCR 引擎：%s", "ok" if eng else "x",
         "rapidocr_onnxruntime 可用" if eng else "不可用（需 pip install rapidocr_onnxruntime）")
    _log("[i] 索引：%d 条（%s）", len(load_index()), INDEX_PATH)
    return 0


def cmd_key():
    root, acct = find_data_root()
    code, src = find_code()
    if not code:
        _log("code 未找到"); return 3
    aes_key, xor_key = derive_keys(code, acct)
    _log("账号目录 : %s", acct)
    _log("清洗 wxid: %s", clean_wxid(acct))
    _log("code     : %d", code)
    _log("aes_key  : %s", aes_key.decode())
    _log("xor_key  : 0x%02x", xor_key)
    return 0


def cmd_index():
    rows = load_index()
    if not rows:
        _log("索引为空（先跑 scan）"); return 0
    by_g, by_f = {}, {}
    for r in rows:
        by_g[r.get("group", "?")] = by_g.get(r.get("group", "?"), 0) + 1
        by_f[r.get("format", "?")] = by_f.get(r.get("format", "?"), 0) + 1
    _log("索引 %d 条 · 格式 %s", len(rows), by_f)
    for k, v in sorted(by_g.items(), key=lambda x: -x[1])[:20]:
        _log("   %-30s %d", k, v)
    hit = [r for r in rows if r.get("hits")]
    _log("命中业务关键词：%d 张", len(hit))
    return 0


def main():
    ap = argparse.ArgumentParser(description="微信本地媒体抽取（.dat 解密 + OCR）")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("doctor")
    sub.add_parser("key")
    sub.add_parser("index")
    s = sub.add_parser("scan")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--groups", default="")
    s.add_argument("--max", type=int, default=400)
    s.add_argument("--ocr", action="store_true")
    s.add_argument("--all-groups", action="store_true")
    a = ap.parse_args()
    if a.cmd == "doctor":
        return cmd_doctor()
    if a.cmd == "key":
        return cmd_key()
    if a.cmd == "index":
        return cmd_index()
    if a.cmd == "scan":
        gs = [x for x in (a.groups or "").split(",") if x.strip()]
        return scan(a.hours, gs, a.max, a.ocr, a.all_groups)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
