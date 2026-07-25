#!/bin/bash
# 配置驱动的 SeaTable access_token 获取（从 config.yaml 读 seatable.*）
# 仅 SeaTable 模式需要；local 模式无需 token。
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$SKILL_DIR/config.yaml"
[ -f "$CFG" ] || { echo "未找到 config.yaml，请先 cp config.yaml.example config.yaml"; exit 1; }

read_vars=$(python3 - "$CFG" <<'PY'
import sys, re
txt=open(sys.argv[1],encoding='utf-8').read()
def get(sec,key):
    m=re.search(r'^'+sec+r':\s*$',txt,re.M)
    if not m: return ''
    rest=txt[m.end():]
    for line in rest.splitlines():
        if re.match(r'^\S',line): break
        mm=re.match(r'\s+'+key+r':\s*(.+?)\s*$',line)
        if mm: return mm.group(1).strip().strip('"\'')
    return ''
print('TOKEN='+get('seatable','api_token'))
print('SERVER='+(get('seatable','server') or 'https://cloud.seatable.cn'))
PY
)
eval "$read_vars"
[ -z "$TOKEN" ] && { echo "[info] config.yaml 未配置 seatable.api_token → 当前为 local 模式，无需 token。"; exit 0; }

curl -s -X GET "https://cloud.seatable.cn/api/v2.1/dtable/app-access-token/" \
  -H "Accept: application/json; charset=utf-8" \
  -H "Authorization: Bearer $TOKEN"
