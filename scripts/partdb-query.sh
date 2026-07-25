#!/bin/bash
# 配置驱动的 PartDB 库存查询（从 config.yaml 读 partdb.*）
# 用法: ./partdb-query.sh <关键词> [返回数量]
# 未启用 partdb（enabled=false 或无 url）时自动跳过。
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$SKILL_DIR/config.yaml"
[ -f "$CFG" ] || { echo "未找到 config.yaml"; exit 1; }

read_vars=$(python3 - "$CFG" "$1" "$2" <<'PY'
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
print('URL='+get('partdb','url'))
print('TOKEN='+get('partdb','token'))
print('ENABLED='+get('partdb','enabled'))
print('KW='+(sys.argv[2] if len(sys.argv)>2 else ''))
print('LIMIT='+(sys.argv[3] if len(sys.argv)>3 else '20'))
PY
)
eval "$read_vars"
[ "$ENABLED" != "True" ] && { echo "[info] config.yaml 中 partdb.enabled != true，跳过缺料检查。"; exit 0; }
[ -z "$URL" ] && { echo "partdb 未配置 url，跳过。"; exit 0; }
[ -z "$KW" ] && { echo "用法: $0 <关键词> [数量]"; exit 1; }

curl -s -L "${URL}/parts?limit=200" -H "Authorization: Token ${TOKEN}" | python3 -c "
import json,sys
kw='$KW'.lower(); limit=int('$LIMIT')
data=json.load(sys.stdin)
matches=[p for p in data.get('hydra:member',[]) if kw in ' '.join(str(p.get(f,'')) for f in ('name','ipn','tags','description')).lower()]
print(f'=== PartDB 搜索:{kw} (共{len(matches)}条) ===')
for p in matches[:limit]:
    print(f\"【{p.get('name')}】 料号:{p.get('ipn')} 库存:{p.get('total_instock')}\")
"
