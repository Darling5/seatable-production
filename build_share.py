# -*- coding: utf-8 -*-
"""生成「对内共享页」——只含 项目进度 / 生产进度 / 甘特图，供公司内部同事查看。

刻意【不包含】：合同金额、实收/待收、成本、供应商、库存价格、微信情报、口令门禁。
数据源与 cockpit.py 完全一致（复用同一个 compute()），保证两个页面不会各说各话。

用法：
    python build_share.py [输出路径]
    SHARE_OUT=<path> python build_share.py
默认输出：<本目录>/项目进度共享页.html
"""
import os
import sys
import json
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.chdir(HERE)

from adapters.factory import load_config, get_adapter
import cockpit

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _clean(v):
    """SeaTable 公式列会回传 '#VALUE!' 之类错误串 —— 一律显示为「—」，别把公式错误当数据。"""
    s = str(v if v is not None else "").strip()
    return "—" if (not s or s[0] == "#") else s


def _nstr(v):
    """数值型字符串去掉多余小数（'66.0' → '66'），非数值原样返回。"""
    s = _clean(v)
    try:
        f = float(s)
    except Exception:
        return s
    return str(int(f)) if f == int(f) else ("%.1f" % f)


def build_model(m):
    k = m.get("kpi") or {}
    t = m.get("time") or {}

    proj = [{
        "no": _clean(r.get("项目编号")),
        "name": _clean(r.get("项目")),
        "status": _clean(r.get("状态")),
        "due": _clean(r.get("合同交期")),
        "remain": _nstr(r.get("剩余（天）")),
        "days": _nstr(r.get("花费天数")),
        "plans": r.get("_计划") or 0,
        "ships": r.get("_发货") or 0,
        "repairs": r.get("_维修") or 0,
    } for r in (m.get("projects_full") or [])]

    plans = [{
        "no": _clean(r.get("生产计划编号")),
        "product": _clean(r.get("生产产品")),
        "qty": r.get("数量"),
        "status": _clean(r.get("状态")),
        "stage": _clean(r.get("阶段")),
        "project": _clean(r.get("关联项目")),
        "start": _clean(r.get("立项日期")),
        "due": _clean(r.get("合同交期")),
        "days": _nstr(r.get("花费天数")),
        "batch": _clean(r.get("批次号")),
    } for r in (m.get("plans_full") or [])]

    gantt = [{
        "name": x.get("name") or "(未命名)",
        "status": x.get("status") or "",
        "start": x.get("start") or "",
        "end": x.get("end") or "",
        "done": bool(x.get("done")),
        "overdue": bool(x.get("overdue")),
        "progress": x.get("progress") or 0,
        "pending": bool(x.get("pending") or not x.get("end")),
    } for x in (m.get("gantt") or [])]

    # 生产计划「未交付」= 全部 − 已交付（真实库里状态有：已交付/计划中/暂停，没有「进行中」）
    pl_done = sum(1 for r in plans if r["status"] in cockpit.STATUS_DONE)
    pl_open = len(plans) - pl_done

    return {
        "snapshot": m.get("snapshot"),
        "synced_at": m.get("synced_at"),
        "projects": proj,
        "plans": plans,
        "gantt": gantt,
        "kpi": {
            "projects": k.get("projects"), "active": k.get("active"),
            "planned": k.get("planned"), "done": k.get("done"),
            "plans_total": len(plans), "plans_open": pl_open, "plans_done": pl_done,
            "pending_due": m.get("gantt_pending") or 0,
            "ontime_rate": t.get("ontime_rate"), "ontime": t.get("ontime"),
            "dated": t.get("dated"),
        },
    }


SHARE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>生产进度看板（对内共享）</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--ink:#111827;--sub:#6b7280;--line:#e5e7eb;--subbg:#f8fafc;
  --primary:#3b5bdb;--green:#059669;--red:#dc2626;--amber:#d97706;--radius:12px}
@media (prefers-color-scheme:dark){:root{--bg:#0b0e14;--card:#151a23;--ink:#f3f4f6;--sub:#8b95a5;
  --line:#252c39;--subbg:#1a212c;--primary:#6f8cff;--green:#34d399;--red:#f87171;--amber:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1500px;margin:0 auto;padding:22px 20px 60px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 16px;margin-bottom:16px}
h1{font-size:21px;margin:0;letter-spacing:.3px}
.tag{font-size:12px;color:var(--sub);background:var(--card);border:1px solid var(--line);
  border-radius:99px;padding:3px 11px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:11px;margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:13px 15px}
.kpi .l{font-size:12px;color:var(--sub)}
.kpi .v{font-size:25px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px}
.kpi .s{font-size:11.5px;color:var(--sub);margin-top:1px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 18px;margin-bottom:18px}
h2{font-size:15.5px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
h2 .ct{font-size:12px;color:var(--sub);font-weight:400}
.note{font-size:12px;color:var(--sub);margin:4px 0 12px}
.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input[type=search],select{font:inherit;font-size:13px;padding:6px 11px;border:1px solid var(--line);
  border-radius:9px;background:var(--subbg);color:var(--ink);min-width:150px}
input[type=search]:focus,select:focus{outline:none;border-color:var(--primary)}
.tblwrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 11px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-size:11.5px;color:var(--sub);font-weight:600;background:var(--subbg);position:sticky;top:0}
tbody tr:hover{background:var(--subbg)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:2px 9px;border-radius:6px;font-size:11.5px;font-weight:600;
  border:1px solid var(--line);background:var(--subbg);color:var(--sub)}
.p-已交付{background:rgba(5,150,105,.12);color:var(--green);border-color:rgba(5,150,105,.28)}
.p-计划中,.p-备料中,.p-改造中,.p-生产中{background:rgba(217,119,6,.12);color:var(--amber);border-color:rgba(217,119,6,.28)}
.p-暂放,.p-暂停,.p-已超期,.p-可能延迟{background:rgba(220,38,38,.12);color:var(--red);border-color:rgba(220,38,38,.28)}
.p-进行中,.p-待客户下单{background:rgba(59,91,219,.12);color:var(--primary);border-color:rgba(59,91,219,.28)}
.empty{padding:18px;color:var(--sub);font-size:13px;text-align:center}
.hide{display:none!important}
/* 甘特 */
.gwrap{position:relative;overflow-x:auto}
.grow,.ghead{display:grid;grid-template-columns:210px minmax(320px,1fr) 130px;gap:10px;align-items:center;height:30px}
.grow:hover{background:var(--subbg)}
.ghead{height:22px;cursor:default}
.ghead:hover{background:transparent}
.glab{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:2px}
.gmeta{font-size:11px;color:var(--sub);font-variant-numeric:tabular-nums;white-space:nowrap}
.gtrack{position:relative;height:16px}
.gbar{position:absolute;top:0;height:16px;border-radius:4px;background:var(--primary);overflow:hidden;
  display:flex;align-items:center;min-width:2px}
.gbar.done{background:var(--green)} .gbar.overdue{background:var(--red)}
.gbar.pend{background:transparent;border:1px dashed var(--sub);color:var(--sub)}
.gprog{position:absolute;left:0;top:0;bottom:0;background:rgba(255,255,255,.32)}
.gcap{position:relative;font-size:10.5px;color:#fff;padding:0 6px;white-space:nowrap}
.gbar.pend .gcap{color:var(--sub)}
.gbody{position:relative}
.ggrid{position:absolute;left:220px;right:140px;top:0;bottom:0;pointer-events:none}
.ggrid .tk{position:absolute;top:0;bottom:0;border-left:1px dashed var(--line)}
.ggrid .gtoday{position:absolute;top:0;bottom:0;border-left:2px solid var(--red);opacity:.75}
.tkl{position:absolute;top:2px;transform:translateX(-50%);font-size:10px;color:var(--sub);white-space:nowrap}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--sub);margin-top:10px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}
footer{color:var(--sub);font-size:11.5px;text-align:center;margin-top:26px;line-height:1.8}
</style></head>
<body><div class="wrap">
<header>
  <h1>生产进度看板</h1>
  <span class="tag">对内共享版</span>
  <span class="tag">数据快照 __SNAPSHOT__</span>
</header>
<div class="kpis" id="kpis"></div>
<div class="card">
  <h2>生产计划甘特图 <span class="ct" id="ganttCt"></span></h2>
  <div class="note">按「立项日期 → 合同交期」排期，条内浅色填充为当前进度。竖红线为今日。悬停条形可看明细。</div>
  <div class="tools">
    <input type="search" id="gq" placeholder="🔍 搜索产品 / 状态">
    <select id="gst">
      <option value="">全部状态</option><option value="run">进行中</option>
      <option value="overdue">逾期</option><option value="done">已交付</option>
      <option value="pend">待补交期</option>
    </select>
  </div>
  <div id="gantt"></div>
  <div class="legend">
    <span><i style="background:var(--primary)"></i>进行中</span>
    <span><i style="background:var(--green)"></i>已交付</span>
    <span><i style="background:var(--red)"></i>逾期</span>
    <span><i style="border:1px dashed var(--sub);background:transparent"></i>待补交期（未填合同交期）</span>
    <span id="gcnt" style="margin-left:auto"></span>
  </div>
</div>
<div class="card">
  <h2>项目进度 <span class="ct" id="projCt"></span></h2>
  <div class="note">按项目看整体状态与交付节点；「计划 / 发货 / 维修」为该项目关联单据条数。</div>
  <div class="tools">
    <input type="search" id="pq" placeholder="🔍 搜索项目名 / 编号">
    <select id="pst"><option value="">全部状态</option></select>
  </div>
  <div class="tblwrap"><table id="projTbl"><thead><tr>
    <th>项目编号</th><th>项目</th><th>状态</th><th>合同交期</th><th title="正数=距交期还有几天；负数=已逾期几天">剩余/逾期(天)</th>
    <th>花费天数</th><th>计划</th><th>发货</th><th>维修</th>
  </tr></thead><tbody></tbody></table></div>
</div>
<div class="card">
  <h2>生产进度 <span class="ct" id="planCt"></span></h2>
  <div class="note">按生产计划看推进状态与工序阶段；未填交期的先沉底，补录后自动排入甘特图。</div>
  <div class="tools">
    <input type="search" id="lq" placeholder="🔍 搜索产品 / 计划编号 / 关联项目">
    <select id="lst"><option value="">全部状态</option></select>
  </div>
  <div class="tblwrap"><table id="planTbl"><thead><tr>
    <th>计划编号</th><th>生产产品</th><th>数量</th><th>状态</th><th>阶段</th>
    <th>关联项目</th><th>立项</th><th>合同交期</th><th>花费天数</th><th>批次号</th>
  </tr></thead><tbody></tbody></table></div>
</div>
<footer>
  数据源：SeaTable 生产库 · 快照 __SNAPSHOT__<br>
  本页为对内沟通版，仅含项目/生产进度，不含合同金额、收付款与成本数据。
</footer>
</div>
<script>
const DATA = __SHARE_MODEL__;
const esc = s => String(s==null?"":s).replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = n => (n==null||n==="")?"—":Number(n).toLocaleString("zh-CN");
const k = DATA.kpi;

/* ── KPI ── */
const KPI = [
  ["项目总数", fmt(k.projects), `进行中 ${k.active} · 计划中 ${k.planned} · 已交付 ${k.done}`],
  ["生产计划", fmt(k.plans_total), `未交付 ${k.plans_open} · 已交付 ${k.plans_done}`],
  ["交期达成率", (k.ontime_rate==null?"—":k.ontime_rate+"%"), `${k.ontime||0}/${k.dated||0} 条有交期`],
  ["待补交期", fmt(k.pending_due), "生产计划未填合同交期"],
];
document.getElementById("kpis").innerHTML = KPI.map(x=>
  `<div class="kpi"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="s">${x[2]}</div></div>`).join("");

/* ── 甘特图 ── */
function renderGantt(){
  const g = DATA.gantt||[];
  const host = document.getElementById("gantt");
  document.getElementById("ganttCt").textContent = `共 ${g.length} 条`;
  if(!g.length){ host.innerHTML = '<div class="empty">暂无生产计划</div>'; return; }
  const toD = s => { const p=String(s).split("-").map(Number); return new Date(p[0],p[1]-1,p[2]); };
  const isPend = x => x.pending || !x.end;
  const dated = g.filter(x=>!isPend(x) && x.start);
  const td = toD(DATA.snapshot);
  if(!dated.length){ host.innerHTML = '<div class="empty">所有生产计划均未填「合同交期」，暂无可排期的甘特图</div>'; return; }
  const starts = dated.map(x=>toD(x.start)), ends = dated.map(x=>toD(x.end));
  const min = new Date(Math.min.apply(null,starts)), max = new Date(Math.max.apply(null,ends));
  const span = Math.max(1,(max-min)/86400000);
  const pct = d => (d-min)/86400000/span*100;
  /* 月份刻度：跨度大时隔月/隔季标一次，避免标签互相压字；1 月或首个刻度带年份 */
  const months = Math.max(1,(max.getFullYear()-min.getFullYear())*12 + (max.getMonth()-min.getMonth()) + 1);
  const step = months > 14 ? 3 : (months > 9 ? 2 : 1);
  let labels = [], lines = [], idx = 0;
  let cur = new Date(min.getFullYear(), min.getMonth(), 1);
  while(cur <= max){
    if(cur >= min){
      const p = pct(cur);
      lines.push(`<i class="tk" style="left:${p.toFixed(2)}%"></i>`);
      if(idx % step === 0){
        const txt = (idx === 0 || cur.getMonth() === 0)
          ? cur.getFullYear()+"年"+(cur.getMonth()+1)+"月" : (cur.getMonth()+1)+"月";
        labels.push(`<span class="tkl" style="left:${p.toFixed(2)}%">${txt}</span>`);
      }
    }
    idx++; cur = new Date(cur.getFullYear(), cur.getMonth()+1, 1);
  }
  const rows = g.map(x=>{
    if(isPend(x)){
      return `<div class="grow" data-st="pend" data-q="${esc((x.name+" "+x.status).toLowerCase())}">
        <div class="glab" title="${esc(x.name)}">${esc(x.name)}</div>
        <div class="gtrack"><div class="gbar pend" style="left:0;width:100%"><span class="gcap">${x.done?"已完成":"待补交期"}</span></div></div>
        <div class="gmeta">${x.start?esc(x.start)+" →":""} 待补</div></div>`;
    }
    const s=toD(x.start), e=toD(x.end);
    /* 少数记录的「合同交期」早于「立项日期」（录入倒挂）——按区间取 min/max 画，并在 meta 前打 ⚠ */
    const inv = e < s;
    const d0 = inv?e:s, d1 = inv?s:e;
    const left = pct(d0), w = Math.max(1.2, pct(d1)-pct(d0));
    const st = x.done?"done":(x.overdue?"overdue":"run");
    const cap = x.done?"已完成":(x.overdue?"逾期":x.progress+"%");
    const ld = Math.round((e-td)/86400000);
    const tip = `${x.name} · ${x.status||"—"} · 立项 ${x.start} → 交期 ${x.end} · 进度 ${x.progress}%`
      + (x.done?"":" · 距交期 "+ld+" 天") + (inv?"（交期早于立项，疑录入倒挂）":"");
    return `<div class="grow" data-st="${st}" data-q="${esc((x.name+" "+x.status).toLowerCase())}">
      <div class="glab" title="${esc(x.name)}">${esc(x.name)}</div>
      <div class="gtrack"><div class="gbar ${st}" style="left:${left.toFixed(2)}%;width:${w.toFixed(2)}%" title="${esc(tip)}">
        <i class="gprog" style="width:${x.progress}%"></i><span class="gcap">${cap}</span></div></div>
      <div class="gmeta" title="${esc(tip)}">${inv?"⚠ ":""}${esc(x.start)} → ${esc(x.end)}</div></div>`;
  }).join("");
  const tp = Math.max(0,Math.min(100,pct(td)));
  host.innerHTML = `<div class="gwrap">
    <div class="ghead"><div class="glab"></div><div class="gtrack">${labels.join("")}</div><div class="gmeta"></div></div>
    <div class="gbody">
      <div class="ggrid">${lines.join("")}<i class="gtoday" style="left:${tp.toFixed(2)}%"></i></div>
      ${rows}
    </div>
  </div>`;
}

/* ── 通用表格过滤 ── */
function fillSelect(sel, rows, key){
  const vals = [...new Set(rows.map(r=>r[key]).filter(v=>v&&v!=="—"))].sort();
  vals.forEach(v=>{ const o=document.createElement("option"); o.value=v; o.textContent=v; sel.appendChild(o); });
}

function renderProjects(){
  const rows = DATA.projects||[];
  const tb = document.querySelector("#projTbl tbody");
  document.getElementById("projCt").textContent = `共 ${rows.length} 个项目`;
  tb.innerHTML = rows.map(p=>`<tr data-st="${esc(p.status)}" data-q="${esc((p.name+" "+p.no).toLowerCase())}">
    <td>${esc(p.no)}</td><td>${esc(p.name)}</td>
    <td><span class="pill p-${esc(p.status)}">${esc(p.status)}</span></td>
    <td>${esc(p.due)}</td><td class="num">${esc(p.remain)}</td><td class="num">${esc(p.days)}</td>
    <td class="num">${p.plans}</td><td class="num">${p.ships}</td><td class="num">${p.repairs}</td></tr>`).join("");
}

function renderPlans(){
  const rows = DATA.plans||[];
  const tb = document.querySelector("#planTbl tbody");
  document.getElementById("planCt").textContent = `共 ${rows.length} 条生产计划`;
  tb.innerHTML = rows.map(p=>`<tr data-st="${esc(p.status)}" data-q="${esc((p.product+" "+p.no+" "+p.project).toLowerCase())}">
    <td>${esc(p.no)}</td><td>${esc(p.product)}</td><td class="num">${fmt(p.qty)}</td>
    <td><span class="pill p-${esc(p.status)}">${esc(p.status)}</span></td>
    <td><span class="pill p-${esc(p.stage)}">${esc(p.stage)}</span></td>
    <td>${esc(p.project)}</td><td>${esc(p.start)}</td><td>${esc(p.due)}</td>
    <td class="num">${esc(p.days)}</td><td>${esc(p.batch)}</td></tr>`).join("");
}

function bindFilter(rootSel, qSel, stSel, cntId){
  const root = document.querySelector(rootSel), q = document.getElementById(qSel), st = document.getElementById(stSel);
  const rows = [...root.querySelectorAll("tr[data-q]")];
  const upd = () => {
    const s = q.value.trim().toLowerCase(), v = st.value; let n=0;
    rows.forEach(r=>{
      const ok = (!v || r.dataset.st===v) && (!s || r.dataset.q.includes(s));
      r.classList.toggle("hide", !ok); if(ok) n++;
    });
    if(cntId) document.getElementById(cntId).textContent = `显示 ${n} / ${rows.length}`;
  };
  q.oninput = upd; st.onchange = upd;
  return upd;
}

renderGantt();
renderProjects();
renderPlans();
fillSelect(document.getElementById("pst"), DATA.projects||[], "status");
fillSelect(document.getElementById("lst"), DATA.plans||[], "status");
bindFilter("#projTbl tbody","pq","pst",null);
bindFilter("#planTbl tbody","lq","lst",null);

/* 甘特过滤（自己一套，因为 DOM 在 .gwrap 里） */
(function(){
  const host = document.getElementById("gantt");
  const q = document.getElementById("gq"), st = document.getElementById("gst");
  const upd = () => {
    const s = q.value.trim().toLowerCase(), v = st.value; let n=0;
    const rows = [...host.querySelectorAll(".grow[data-q]")];
    rows.forEach(r=>{
      const ok = (!v || r.dataset.st===v) && (!s || r.dataset.q.includes(s));
      r.classList.toggle("hide", !ok); if(ok) n++;
    });
    const c = document.getElementById("gcnt"); if(c) c.textContent = `显示 ${n} / ${rows.length} 条`;
  };
  q.oninput = upd; st.onchange = upd;
  upd();
})();
</script>
</body></html>
"""


def main():
    out = os.environ.get("SHARE_OUT") or (sys.argv[1] if len(sys.argv) > 1 else
                                          os.path.join(HERE, "项目进度共享页.html"))
    adapter = get_adapter(load_config())
    adapter.auth()
    today = datetime.datetime.now(cockpit._TZ).date()
    model = cockpit.compute(cockpit._NormAdapter(adapter), today)
    share = build_model(model)
    html = SHARE_HTML.replace("__SHARE_MODEL__", json.dumps(share, ensure_ascii=False)) \
                     .replace("__SNAPSHOT__", str(share["snapshot"]))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] 共享页已生成：{out}")
    print(f"     快照 {share['snapshot']} · 项目 {len(share['projects'])} · "
          f"生产计划 {len(share['plans'])} · 甘特 {len(share['gantt'])} 条")


if __name__ == "__main__":
    main()
