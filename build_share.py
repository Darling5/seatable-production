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
        # 三种状态：① 实际进度 = status/stage；② 合同交期 = 原值（没填就是空）；③ 历史推算交期 = _due_est
        "due": _clean(r.get("合同交期")),
        "due_est": _clean(r.get("_due_est")),
        "est": bool(r.get("_est")),
        "days": _nstr(r.get("花费天数")),
        "batch": _clean(r.get("批次号")),
    } for r in (m.get("plans_full") or [])]

    # 甘特：三条口径并存（业主 2026-09-15）——
    #   progress   ① 实际进度（生产计划表「阶段」实测值）
    #   due        ② 合同交期（客户承诺；未填为 None）
    #   due_est    ③ 历史推算交期（立项日 + 已交付中位工期，恒有值）
    # est=True 表示「条长是按推算画的」（该计划未填合同交期），前端用虚线 + ≈ 区分。
    gantt = [{
        "name": x.get("name") or "(未命名)",
        "status": x.get("status") or "",
        "start": x.get("start") or "",
        "end": x.get("end") or "",
        "due": x.get("due") or "",
        "due_est": x.get("due_est") or "",
        "done": bool(x.get("done")),
        "overdue": bool(x.get("overdue")),
        "progress": x.get("progress") or 0,
        "est": bool(x.get("est")),
        "stage": x.get("stage") or "",
    } for x in (m.get("gantt") or [])]

    # 生产计划「未交付」= 全部 − 已交付（真实库里状态有：已交付/计划中/暂停，没有「进行中」）
    pl_done = sum(1 for r in plans if r["status"] in cockpit.STATUS_DONE)
    pl_open = len(plans) - pl_done
    h = m.get("gantt_hist") or {}

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
            "est_due": m.get("gantt_pending") or 0,
            "hist_n": h.get("n"), "hist_median": h.get("median"),
            "hist_p75": h.get("p75"), "hist_p90": h.get("p90"),
            "est_days": m.get("gantt_est_days"),
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
.gtrack{position:relative;height:26px}
/* 交期菱形标记走轨道上半部（0~9px），进度条压在下半部（10~26px），互不遮挡 */
.gbar{position:absolute;top:10px;height:16px;border-radius:4px;background:var(--primary);overflow:hidden;
  display:flex;align-items:center;min-width:2px}
.gbar.done{background:var(--green)} .gbar.overdue{background:var(--red)}
/* ── 三种状态同轴并列（业主 2026-09-15）──
   .gms-due = ② 合同交期（实心菱形 = 客户承诺）
   .gms-est = ③ 历史推算交期（空心虚线菱形 = 自家历史对标线） */
.gms{position:absolute;top:0;width:9px;height:9px;transform:translateX(-50%) rotate(45deg);border-radius:2px;z-index:3}
.gms.due{background:var(--ink);border:1px solid var(--card);box-shadow:0 0 0 1px var(--ink)}
.gms.est{background:var(--card);border:1.5px dashed var(--primary)}
.gms-due{background:var(--ink)!important;transform:rotate(45deg);border-radius:2px}
.gms-est{background:var(--card)!important;border:1.5px dashed var(--primary)!important;transform:rotate(45deg);border-radius:2px}
.lgsep{color:var(--sub)}
.appx{color:var(--primary);font-variant-numeric:tabular-nums}
.dash{color:var(--sub);font-size:11.5px}
/* 交期为「历史推算」的条：仍按真实时间轴画，用虚线 + 半透明底与真实交期区分 */
.gbar.est{background:transparent;border:1px dashed var(--primary);color:var(--ink)}
.gbar.est.done{border-color:var(--green)} .gbar.est.overdue{border-color:var(--red)}
.gbar.est .gprog{background:color-mix(in srgb,var(--primary) 22%,transparent)}
.gbar.est .gcap{color:var(--ink);position:static}
.glab.est::after{content:"≈";margin-left:4px;color:var(--sub);font-weight:600}
.gprog{position:absolute;left:0;top:0;bottom:0;background:rgba(255,255,255,.32)}
.gcap{position:relative;font-size:10.5px;color:#fff;padding:0 6px;white-space:nowrap}
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
  <div class="note"><b>图中三种状态</b>：<b>①实际进度</b>＝条内浅色填充（读生产计划表「阶段」的实测值，每晚 19:00 同步）；<b>②合同交期</b>＝<b>实心菱形</b>（对客户的承诺）；<b>③历史推算交期</b>＝<b>空心菱形</b>（按自家历史工期推算的对标线）。两者同轴并列，可直观判断合同日期比历史惯例更紧还是更松。竖红线为今日。悬停条形可看明细。<b>默认按「计划中」优先排序</b>。</div>
  <div class="note" id="ganttBasis"></div>
  <div class="tools">
    <input type="search" id="gq" placeholder="🔍 搜索产品 / 状态 / 阶段">
    <select id="gst">
      <option value="">全部</option><option value="计划中">计划中</option>
      <option value="逾期">逾期</option><option value="进行中">进行中</option>
      <option value="已交付">已交付</option><option value="est">交期为推算</option>
    </select>
  </div>
  <div id="gantt"></div>
  <div class="legend">
    <span><i style="background:var(--primary)"></i>① 进行中</span>
    <span><i style="background:var(--green)"></i>① 已交付</span>
    <span><i style="background:var(--red)"></i>① 逾期</span>
    <span><i class="gms-due"></i>② 合同交期</span>
    <span><i class="gms-est"></i>③ 历史推算交期</span>
    <span class="lgsep">条前带 <b>≈</b> = 条长按推算值画（该计划未填合同交期）</span>
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
  <div class="note">按生产计划看推进状态与工序阶段；「阶段」为表内实测值（每晚 19:00 同步）。未填「合同交期」的按历史中位工期推算，交期列以 <b>≈</b> 标出。<b>默认按「计划中」优先排序。</b></div>
  <div class="tools">
    <input type="search" id="lq" placeholder="🔍 搜索产品 / 计划编号 / 关联项目">
    <select id="lst"><option value="">全部状态</option></select>
  </div>
  <div class="tblwrap"><table id="planTbl"><thead><tr>
    <th>计划编号</th><th>生产产品</th><th>数量</th><th>状态</th><th>阶段</th>
    <th>关联项目</th><th>立项</th><th>合同交期</th><th>推算交期</th><th>花费天数</th><th>批次号</th>
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
  ["交期推算", fmt(k.est_due), `未填合同交期 · 按历史中位 ${k.hist_median==null?"—":k.hist_median} 天推算`],
];
document.getElementById("kpis").innerHTML = KPI.map(x=>
  `<div class="kpi"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="s">${x[2]}</div></div>`).join("");

/* ── 甘特图 ── */
function renderGantt(){
  const g = DATA.gantt||[];
  const host = document.getElementById("gantt");
  document.getElementById("ganttCt").textContent = `共 ${g.length} 条`;
  /* 推算依据必须写在页面上 —— 让「这个交期怎么来的」有出处，而不是凭空出现一个日期 */
  const basis = document.getElementById("ganttBasis");
  if(basis) basis.innerHTML = k.est_due
    ? `<b>${k.est_due} 条未填「合同交期」</b>（多为合同写明「收款后 X 日内交货」、尚未收款故留空）→ 其条长与<b>③空心菱形</b>均按推算值画，名称后带 ≈；<b>②实心菱形</b>只在填了合同交期时才出现。`
      + (k.hist_n
         ? `<br>③ 推算依据＝<b>已交付计划的实际工期</b>：样本 ${k.hist_n} 条，中位 <b>${k.hist_median} 天</b>（p75 ${k.hist_p75} / p90 ${k.hist_p90}）→ 取「立项日 + ${k.est_days} 天」。<b>推算仅供排期对标、非客户承诺交期</b>，也不回写系统；计划表补录交期后，两种菱形会分开显示，便于复盘当初排得准不准。`
         : "历史工期样本不足，暂用兜底工期推算。")
    : "";
  if(!g.length){ host.innerHTML = '<div class="empty">暂无生产计划</div>'; return; }
  const toD = s => { const p=String(s).split("-").map(Number); return new Date(p[0],p[1]-1,p[2]); };
  const td = toD(DATA.snapshot);
  /* 所有条目现在都在时间轴上（没填交期的用历史工期补上了），刻度范围把「今日」也框进来；
     三状态改造后还要并入 due / due_est —— 合同交期可能晚于条尾、推算交期也可能晚于合同交期
     （合同比历史惯例更紧时就会这样），漏了任一都会把菱形标记甩出画布。 */
  const allD = g.map(x=>toD(x.start)).concat(g.map(x=>toD(x.end))).concat([td])
    .concat(g.filter(x=>x.due).map(x=>toD(x.due)))
    .concat(g.filter(x=>x.due_est).map(x=>toD(x.due_est)));
  const min = new Date(Math.min.apply(null,allD)), max = new Date(Math.max.apply(null,allD));
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
    const est = !!x.est;
    const s=toD(x.start), e=toD(x.end);
    /* 少数记录的「合同交期」早于「立项日期」（录入倒挂）——按区间取 min/max 画，并在 meta 前打 ⚠ */
    const inv = e < s;
    const d0 = inv?e:s, d1 = inv?s:e;
    const left = pct(d0), w = Math.max(1.2, pct(d1)-pct(d0));
    const prog = x.done?100:(x.progress||0);
    /* 状态口径与「计划中优先」排序一致：已交付 → 逾期 → 计划中 → 进行中 */
    const st = x.done?"已交付":(x.overdue?"逾期":(String(x.status||"")==="计划中"?"计划中":"进行中"));
    const cap = x.done?"已完成":(x.overdue?"逾期":prog+"%");
    const ld = Math.round((e-td)/86400000);
    /* ── 三种状态同轴并列（业主 2026-09-15）──
       ① 实际进度 = 条本体（prog 填充 + done/overdue 配色）
       ② 合同交期 = 实心菱形；未填则不画（不是画在今天）
       ③ 历史推算交期 = 空心虚线菱形
       合同 vs 推算 的松紧（slack）在悬停里给出，这是「客户给的日子比我们历来做得完的时间紧多少」的判据。 */
    const cl = v => Math.max(0,Math.min(100,v)).toFixed(2);
    const mkDue = x.due ? `<i class="gms due" style="left:${cl(pct(toD(x.due)))}%" title="② 合同交期 ${x.due}"></i>` : "";
    const mkEst = x.due_est ? `<i class="gms est" style="left:${cl(pct(toD(x.due_est)))}%" title="③ 历史推算交期 ${x.due_est}"></i>` : "";
    const sl = (x.due && x.due_est) ? Math.round((toD(x.due)-toD(x.due_est))/86400000) : null;
    const slTxt = sl===null ? "" : (sl<0 ? ` · 合同比历史惯例紧 ${-sl} 天` : (sl>0 ? ` · 合同比历史惯例松 ${sl} 天` : " · 与历史惯例持平"));
    const tip = `${x.name} · ${x.status||"—"} · 当前阶段 ${x.stage||"—"}`
      + ` · 立项 ${x.start}`
      + ` · ② 合同交期 ${x.due||"未填"}`
      + ` · ③ 推算交期 ${x.due_est||"—"}`
      + ` · 进度 ${prog}%（取表内阶段实测值）`
      + (x.done?"":" · 距条尾交期 "+ld+" 天")
      + slTxt
      + (est?"（条长按历史工期推算画，非客户承诺）":"")
      + (inv?"（交期早于立项，疑录入倒挂）":"");
    return `<div class="grow" data-st="${esc(st)}" data-est="${est?1:0}" data-q="${esc((x.name+" "+x.status+" "+x.stage).toLowerCase())}">
      <div class="glab${est?" est":""}" title="${esc(x.name)}">${esc(x.name)}</div>
      <div class="gtrack">${mkEst}${mkDue}<div class="gbar ${x.done?"done":(x.overdue?"overdue":"")}${est?" est":""}" style="left:${left.toFixed(2)}%;width:${w.toFixed(2)}%" title="${esc(tip)}">
        <i class="gprog" style="width:${prog}%"></i><span class="gcap">${cap}</span></div></div>
      <div class="gmeta" title="${esc(tip)}">${inv?"⚠ ":""}${est?"≈ ":""}${esc(x.start)} → ${esc(x.end)}</div></div>`;
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
    <td>${esc(p.project)}</td><td>${esc(p.start)}</td>
    <td>${p.due?esc(p.due):'<span class="dash">未填</span>'}</td>
    <td>${p.due_est?('<span class="appx" title="立项日 + 历史中位工期推算，非客户承诺">≈ '+esc(p.due_est)+'</span>'):"—"}</td>
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
      /* 「交期为推算」是**另一条轴**（交期来源），与状态筛选用 OR 之外的方式并存 */
      const ok = (!v || (v==="est" ? r.dataset.est==="1" : r.dataset.st===v)) && (!s || r.dataset.q.includes(s));
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
