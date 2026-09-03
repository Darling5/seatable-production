# -*- coding: utf-8 -*-
"""示例：新建供应商 + 物料 + 采购信息（orderdetail + pricedetail）三步建料脚本。

来源于真实建料流程的脱敏模板（供应商/型号/合同号均已泛化为示例）。
- --apply 才真正写入；否则 dry-run 只打印将写入的 payload（安全闸门）。
- 关联实体 IRI 必须带 /api/ 前缀（part / supplier / orderdetail），否则报 Invalid IRI。
- 用法：python create_supplier_part.py [--apply]（物料定义在 PARTS 列表里改）
"""
import os, sys, json, urllib.request, urllib.parse

env = {}
for line in open(os.path.expanduser('~/.qclaw/seatable-cache/config.env'), encoding='utf-8'):
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

BASE = env['PARTDB_URL']; TOK = env['PARTDB_TOKEN']

class DB:
    def __init__(s, url, tok):
        s.url = url.rstrip('/'); s.tok = tok
    def _req(s, method, path, data=None, ctype='application/ld+json'):
        url = s.url + path
        h = {'Authorization': 'Token ' + s.tok}
        body = None
        if data is not None:
            h['Content-Type'] = ctype
            body = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=30)
            return r.status, r.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', 'replace')
    def get(s, p):
        st, b = s._req('GET', p)
        if st == 200:
            try: return json.loads(b)
            except: return None
        return None
    def get_all(s, p):
        out = []; page = 1
        while True:
            d = s.get(f'{p}?page={page}')
            if not d: break
            mem = d.get('hydra:member', [])
            if not mem: break
            out.extend(mem); page += 1
            if page > 500: break
        return out
    def post(s, p, data): return s._req('POST', p, data)
    def patch(s, p, data):
        return s._req('PATCH', p, data, ctype='application/merge-patch+json')

db = DB(BASE, TOK)
APPLY = '--apply' in sys.argv

# ---- 物料定义（示例数据，替换为你的真实物料） ----
spec_common = "规格公共部分：分辨率/触摸方式/主板配置/接口配置等"
PARTS = [
    {
        "name": "示例物料A 18.5寸显示屏",
        "desc": "示例描述；型号 DEMO-A；18.5寸；" + spec_common + "；含税单价1800；来源合同 示例合同编号",
        "cat": 20, "tags": "示例,LCD,显示屏",
        "model": "DEMO-A", "price": 1800.0,
    },
    {
        "name": "示例物料B 21.5寸显示屏",
        "desc": "示例描述；型号 DEMO-B；21.5寸；" + spec_common + "；含税单价1800；来源合同 示例合同编号",
        "cat": 20, "tags": "示例,LCD,显示屏",
        "model": "DEMO-B", "price": 1800.0,
    },
]
SUP_NAME = "示例供应商（深圳）有限公司"

print("=== DRY RUN 预览（加 --apply 才真实写入）===\n")
# 供应商
existing = db.get('/suppliers?name=' + urllib.parse.quote(SUP_NAME) + '&limit=20')
supplier = None
if existing:
    for x in existing.get('hydra:member', []):
        if (x.get('name') or '').find(SUP_NAME) >= 0:
            supplier = x; break
if supplier:
    print(f"[供应商] 已存在: {supplier['id']} {supplier['name']}")
else:
    print(f"[供应商] 将新建: {SUP_NAME}")

for p in PARTS:
    print('\n' + '-'*60)
    print(f"[物料] name={p['name']}")
    print(f"       category=/categories/{p['cat']}   tags={p['tags']}")
    print(f"       supplierpartnr={p['model']}   price={p['price']}")
    print(f"       desc={p['desc']}")

if not APPLY:
    print("\n（dry-run 结束，未做任何写入）")
    sys.exit(0)

# ---- 真实写入 ----
print('\n=== 开始写入 ===')
# 1) 供应商
if supplier:
    sid = supplier['id']
    print(f"[供应商] 复用 id={sid}")
else:
    st, b = db.post('/suppliers', {'name': SUP_NAME})
    if st >= 400:
        print(f"[供应商] 新建失败 {st}: {b[:200]}"); sys.exit(1)
    sid = json.loads(b).get('id')
    print(f"[供应商] 新建 OK id={sid}")

for p in PARTS:
    # 2) 建料
    body = {"name": p['name'], "description": p['desc'], "category": f"/api/categories/{p['cat']}", "tags": p['tags']}
    st, b = db.post('/parts', body)
    if st >= 400:
        print(f"[物料] {p['name']} 建料失败 {st}: {b[:200]}"); continue
    pj = json.loads(b); pid = pj.get('id'); ipn = "P%04d" % pid
    db.patch(f"/parts/{pid}", {"ipn": ipn})
    print(f"[物料] {p['name']} 建料 OK pid={pid} ipn={ipn}")
    # 3) 采购记录（IRI 带 /api/ 前缀）
    st, b = db.post('/orderdetails', {"part": f"/api/parts/{pid}", "supplier": f"/api/suppliers/{sid}", "supplierpartnr": p['model']})
    if st >= 400:
        print(f"   orderdetail 失败 {st}: {b[:200]}"); continue
    odj = json.loads(b); odid = odj.get('id')
    # 4) 价格
    st, b = db.post('/pricedetails', {"orderdetail": f"/api/orderdetails/{odid}", "price": p['price'],
                                      "price_per_unit": p['price'], "min_discount_quantity": 1, "price_related_quantity": 1})
    if st >= 400:
        print(f"   pricedetail 失败 {st}: {b[:200]}"); continue
    print(f"   采购记录+价格 OK od={odid} 价={p['price']}")
print('\n=== 完成 ===')
