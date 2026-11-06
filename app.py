# app.py - NIFTY ORB-ML terminal (Streamlit).
# The page is ONE html component. Its javascript polls the worker's /live (about 0.6 KB per second)
# and reloads /static only when something changed. Streamlit itself never re-runs, so no flicker,
# no big Upstash downloads, and NO Upstash token is sent to the browser any more.
#
# ENV (Render, app service): UPSTASH_REDIS_REST_URL  UPSTASH_REDIS_REST_TOKEN  (a read+write token is fine here, server side)
#                            WORKER_URL   e.g. https://your-worker.onrender.com   (no trailing slash)
#                            BUDGET_CEILING (optional)
import json
import os

import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="NIFTY ORB-ML Terminal", layout="wide", initial_sidebar_state="collapsed")
st.markdown("<style>.block-container{padding-top:0.6rem;padding-bottom:0}</style>", unsafe_allow_html=True)

URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
WORKER_URL = os.getenv("WORKER_URL", "").rstrip("/")
BUDGET_DEFAULT = float(os.getenv("BUDGET_CEILING", "80.0"))


def redis_get(key):
    if not URL or not TOKEN:
        return None
    try:
        r = requests.get(f"{URL}/get/{key}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=6)
        if r.status_code == 200 and r.json().get("result"):
            return json.loads(r.json()["result"])
    except Exception:
        pass
    return None


def redis_set(key, value):
    if not URL or not TOKEN:
        return
    try:
        requests.post(f"{URL}/set/{key}", headers={"Authorization": f"Bearer {TOKEN}"}, data=json.dumps(value), timeout=6)
    except Exception:
        pass


@st.cache_data(ttl=20, show_spinner=False)
def load_hist():
    return redis_get("nifty_hist") or {}


def save_budget():
    redis_set("user_selected_budget", float(st.session_state["budget"]))


saved = redis_get("user_selected_budget")
try:
    start_budget = float(saved) if saved is not None else BUDGET_DEFAULT
except Exception:
    start_budget = BUDGET_DEFAULT

c1, c2 = st.columns([3, 2])
with c1:
    st.markdown("<h4 style='margin:0;color:#e0e0e0'>NIFTY ORB-ML Terminal</h4>", unsafe_allow_html=True)
with c2:
    st.slider("Max premium (₹) for strike selection", 30.0, 150.0, start_budget, 5.0, key="budget", on_change=save_budget)

hist = load_hist()
if not WORKER_URL:
    st.error("WORKER_URL env var is not set - live updates are OFF. Add it in Render (the worker's public URL).")

PAGE = r"""
<!DOCTYPE html><html><head><meta charset="utf-8"/>
<script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
<style>
 body{margin:0;background:#131722;color:#d1d4dc;font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:12px}
 .box{background:#131722;border:1px solid #2a2e39;border-radius:8px;padding:10px 12px;margin-bottom:8px}
 .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
 .chip{background:#1f2430;padding:5px 10px;border-radius:4px}
 .chip small{display:block;color:#90a4ae;font-size:10px}
 .chip b{font-size:12px}
 .g{color:#00e676}.r{color:#ff5252}.y{color:#ffb74d}.b{color:#42a5f5}.p{color:#b388ff}.w{color:#fff}
 #top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
 #ltp{font-size:24px;font-weight:bold;color:#00e676}
 #wrap{position:relative;width:100%;height:520px}
 #hover{position:absolute;display:none;background:#1e222d;border:1px solid #363a45;border-radius:6px;padding:9px 12px;
        line-height:1.5;box-shadow:0 4px 12px rgba(0,0,0,.5);z-index:50;pointer-events:none;min-width:230px}
 table{width:100%;border-collapse:collapse}
 th,td{padding:4px 6px;text-align:right;border-bottom:1px solid #1f2430;white-space:nowrap}
 th{color:#90a4ae;font-weight:normal} th:first-child,td:first-child{text-align:left}
 .scroll{overflow-x:auto}
</style></head><body>
<div class="box"><div id="top">
  <div><span style="color:#90a4ae">NIFTY 50</span> <span id="ltp">--</span></div>
  <div id="status" class="y"></div><div id="conn" style="color:#787b86">connecting...</div></div></div>
<div class="box" id="levels"></div>
<div class="box" id="strip"></div>
<div id="wrap"><div id="hover"></div></div>
<div class="box" style="margin-top:8px"><div style="color:#90a4ae;font-weight:bold;margin-bottom:6px" id="logtitle">TRADE LOG</div>
  <div class="scroll"><table id="log"></table></div></div>

<script>
const WORKER = "__WORKER__";
const HIST = __HIST__;
const IST = 19800;
let S = HIST.static || {};
let live = {};
let lastVer = S.ver || 0;
let lines = [];

const chart = LightweightCharts.createChart(document.getElementById('wrap'), {
  width: document.getElementById('wrap').clientWidth, height: 520,
  layout:{background:{color:'#131722'},textColor:'#d1d4dc'},
  grid:{vertLines:{color:'#1f2430'},horzLines:{color:'#1f2430'}},
  crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
  rightPriceScale:{borderColor:'#2a2e39'},
  timeScale:{borderColor:'#2a2e39',timeVisible:true,secondsVisible:false}});
const cs = chart.addCandlestickSeries({upColor:'#089981',downColor:'#f23645',borderVisible:false,wickUpColor:'#089981',wickDownColor:'#f23645'});
const vw = chart.addLineSeries({color:'#9c27b0',lineWidth:2,priceLineVisible:false,lastValueVisible:false,title:'VWAP'});
const em = chart.addLineSeries({color:'#2962ff',lineWidth:1,priceLineVisible:false,lastValueVisible:false,title:'EMA20'});

function uniq(arr){ const m=new Map(); arr.forEach(x=>m.set(x.time,x)); return [...m.values()].sort((a,b)=>a.time-b.time); }
const C = uniq((HIST.candles||[]).map(c=>({...c,time:c.time+IST})));
cs.setData(C.map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close})));
vw.setData(C.filter(c=>c.vwap).map(c=>({time:c.time,value:c.vwap})));
em.setData(C.filter(c=>c.ema20).map(c=>({time:c.time,value:c.ema20})));
if (C.length) chart.timeScale().fitContent();

const LV = {P:['#ffeb3b','Pivot'],R1:['#ef5350','R1'],R2:['#e53935','R2'],R3:['#d32f2f','R3'],S1:['#26a69a','S1'],S2:['#00897b','S2'],S3:['#00796b','S3'],PDH:['#42a5f5','PDH'],PDL:['#ab47bc','PDL']};
function pl(price,color,title,w,style){ if(price&&price>0) lines.push(cs.createPriceLine({price:+price,color,lineWidth:w,lineStyle:style,axisLabelVisible:true,title})); }
function applyStatic(){
  lines.forEach(l=>cs.removePriceLine(l)); lines=[];
  const D=LightweightCharts.LineStyle.Dashed, T=LightweightCharts.LineStyle.Dotted, SO=LightweightCharts.LineStyle.Solid;
  Object.entries(S.pivots||{}).forEach(([k,v])=>{ if(LV[k]) pl(v,LV[k][0],LV[k][1],1,D); });
  const o=S.orb||{}; pl(o.orb_h,'#00e676','ORB HIGH',2,SO); pl(o.orb_l,'#ff5252','ORB LOW',2,SO);
  const f=S.fibs||{}; [['ORB_MID','ORB MID'],['fib_50','FIB 50'],['fib_100','FIB 100'],['fib_161','FIB 161.8']].forEach(([k,t])=>pl(f[k],'#b388ff',t,1,T));
  const mk=(S.markers||[]).map(m=>({...m,time:m.time+IST})).sort((a,b)=>a.time-b.time);
  cs.setMarkers(mk);
  renderLevels(); renderLog(); renderStrip();
}

const fx=(v,d=1)=> (v===null||v===undefined||v==='')?'—':Number(v).toFixed(d);
const sg=v=> v>=0?'+':'';
function renderLevels(){
  const o=S.orb||{}, p=S.pivots||{}, f=S.fibs||{};
  const chip=(l,v,c)=>`<div class="chip"><small>${l}</small><b class="${c}">${v}</b></div>`;
  document.getElementById('levels').innerHTML='<div class="row">'+
    chip('ORB HIGH',fx(o.orb_h),'g')+chip('ORB LOW',fx(o.orb_l),'r')+chip('PIVOT',fx(p.P),'y')+
    chip('PDH / PDL',fx(p.PDH)+' / '+fx(p.PDL),'b')+chip('FIB 50/100/161',fx(f.fib_50,0)+' / '+fx(f.fib_100,0)+' / '+fx(f.fib_161,0),'p')+
    chip('ML CUTOFF',fx(S.cutoff,2),'y')+chip('MAX PREMIUM','₹'+fx(S.budget,0),'w')+'</div>';
  document.getElementById('status').textContent=S.status||'';
}
function renderStrip(){
  const h=live.hud; let html='';
  if(h){
    const ce=h.type==='CE', col=ce?'g':'r';
    const pc=h.pts_now>=0?'g':'r';
    let tg=(h.targets||[]).map((t,i)=>{const d=Math.abs(t-h.entry); return `<div class="chip"><small>TARGET ${i+1}</small><b class="y">${fx(t)} (₹${fx(Math.max(.5,h.entry_opt+d*h.delta),2)})</b></div>`}).join('');
    html=`<div class="${col}" style="font-weight:bold;margin-bottom:6px">${ce?'CALL':'PUT'} (${h.type}) ${h.closed?'• COMPLETED':'• ACTIVE'} • ${h.symbol} (Δ ${fx(h.delta,2)})</div><div class="row">`+
      `<div class="chip"><small>ENTRY</small><b class="w">${fx(h.entry)} (₹${fx(h.entry_opt,2)})</b></div>`+
      `<div class="chip"><small>INITIAL SL</small><b class="r">${fx(h.init_sl)} (₹${fx(h.init_sl_opt,2)})</b></div>`+
      `<div class="chip"><small>CURRENT SL</small><b class="g">${fx(h.cur_sl)} (₹${fx(h.cur_sl_opt,2)}) [${sg(h.locked)}${fx(h.locked,0)}p]</b></div>`+
      (h.closed?`<div class="chip"><small>EXIT ${h.exit_time||''}</small><b class="w">${fx(h.exit_spot)} • ${h.exit_reason||''}</b></div>`:
        `<div class="chip"><small>NEXT TRAIL AT</small><b class="p">${fx(h.next_trail_at)}</b></div>`)+
      `<div class="chip"><small>${h.closed?'NET':'LIVE'} P&amp;L</small><b class="${pc}">${sg(h.pts_now)}${fx(h.pts_now)} pts (₹${sg(h.inr_now)}${h.inr_now}) • opt ₹${fx(h.opt_now,2)}</b></div>`+
      tg+'</div>';
  } else {
    const pv=S.preview||{};
    html='<div style="color:#90a4ae;font-weight:bold;margin-bottom:6px">STANDBY - CONTRACT PREVIEW (entry fires only after ORB break + confirm + ML pass)</div>';
    ['CE','PE'].forEach(k=>{ const c=pv[k]; if(!c) return; const col=k==='CE'?'g':'r';
      html+=`<div class="row" style="margin-bottom:6px"><b class="${col}" style="min-width:150px">${c.symbol} (Δ ${fx(c.delta,2)})</b>`+
        `<div class="chip"><small>OPTION LTP</small><b class="w">₹${fx(c.opt_ltp,2)}</b></div>`+
        `<div class="chip"><small>IF ENTRY ≈</small><b class="w">${fx(c.exp_entry_spot)}</b></div>`+
        `<div class="chip"><small>SL</small><b class="r">${fx(c.exp_sl_spot)} (₹${fx(c.exp_sl_opt,2)})</b></div>`+
        `<div class="chip"><small>TARGETS</small><b class="y">${(c.targets||[]).slice(0,4).map(t=>fx(t,0)).join(' / ')}</b></div></div>`;});
  }
  const pd=S.prev_day;
  if(pd&&pd.date){ html+=`<div style="margin-top:8px;color:#ffb74d">📅 Last backtest trade ${pd.date}: ${pd.side} ${pd.entry_time} @ ${fx(pd.entry)} → ${pd.exit_time} @ ${fx(pd.exit)} = <b>${sg(pd.pts)}${fx(pd.pts)} pts</b> (${pd.reason})</div>`; }
  const el=document.getElementById('strip'); if(el.dataset.h!==html){ el.innerHTML=html; el.dataset.h=html; }
}
function renderLog(){
  const t=(S.trades||[]).slice().reverse(); let tot=0;
  (S.trades||[]).forEach(x=>tot+=Number(x.pts)||0);
  document.getElementById('logtitle').innerHTML=`TRADE LOG (last sessions) &nbsp; NET: <b class="${tot>=0?'g':'r'}">${sg(tot)}${tot.toFixed(1)} pts</b> &nbsp; trades: ${t.length}`;
  let h='<tr><th>Date</th><th>Side</th><th>Entry</th><th>Entry px</th><th>Exit</th><th>Exit px</th><th>Pts</th><th>₹</th><th>Reason</th></tr>';
  t.forEach(x=>{ const c=x.pts>=0?'g':'r'; h+=`<tr><td>${x.date}</td><td class="${x.side==='CE'?'g':'r'}">${x.side}</td><td>${x.entry_time}</td><td>${fx(x.entry)}</td><td>${x.exit_time}</td><td>${fx(x.exit)}</td><td class="${c}">${sg(x.pts)}${fx(x.pts)}</td><td class="${c}">${x.inr}</td><td style="text-align:left">${x.reason}</td></tr>`; });
  document.getElementById('log').innerHTML=h;
}

// hover / tap card on entry or exit arrow
const hv=document.getElementById('hover'), wrap=document.getElementById('wrap');
function showCard(param){
  if(!param||!param.time||!param.point){ hv.style.display='none'; return; }
  const d=(S.cards||{})[String(param.time-IST)];
  if(!d){ hv.style.display='none'; return; }
  const col=d.type==='CE'?'#00e676':'#ff5252', pc=d.pts>=0?'#00e676':'#ff5252';
  hv.innerHTML=`<div style="font-weight:bold;color:${col};margin-bottom:3px">${d.title}</div>
   <div><b>Entry:</b> ${d.entry_time} @ ${fx(d.entry)}</div>
   <div><b>Exit:</b> ${d.exit_time} @ ${fx(d.exit)}</div>
   <div><b>Initial SL:</b> ${fx(d.sl)} &nbsp; <b>Max trail SL:</b> ${fx(d.max_trail_sl)}</div>
   <div style="margin-top:3px;font-weight:bold;color:${pc}">Total: ${sg(d.pts)}${fx(d.pts)} pts (₹${d.inr}) • ${d.reason}</div>`+
   (d.pred!==null&&d.pred!==undefined?`<div style="color:#90a4ae">ML predicted: ${d.pred}</div>`:'');
  hv.style.display='block';
  hv.style.left=Math.max(5,Math.min(param.point.x+15,wrap.clientWidth-250))+'px';
  hv.style.top=Math.max(param.point.y-90,5)+'px';
}
chart.subscribeCrosshairMove(showCard); chart.subscribeClick(showCard);
window.addEventListener('resize',()=>chart.applyOptions({width:wrap.clientWidth}));

// live polling
function applyLive(){
  if(live.ltp) document.getElementById('ltp').textContent='₹'+Number(live.ltp).toFixed(2);
  const send=(c)=>({time:c.time+IST,open:c.open,high:c.high,low:c.low,close:c.close});
  try{
    if(live.last) cs.update(send(live.last));
    if(live.bar) cs.update(send(live.bar));
  }catch(e){}
  renderStrip();
  const a=live.tick_age, el=document.getElementById('conn');
  if(!live.mkt){ el.textContent='● market closed'; el.style.color='#787b86'; }
  else if(a===null||a>15){ el.textContent='● worker alive, NO TICKS ('+(a===null?'?':a)+'s)'; el.style.color='#ff5252'; }
  else { el.textContent='● live ('+a+'s)'; el.style.color='#00e676'; }
}
async function poll(){
  let delay=1000;
  if(!WORKER){ document.getElementById('conn').textContent='WORKER_URL not set'; return; }
  try{
    const r=await fetch(WORKER+'/live',{cache:'no-store'}); live=await r.json();
    if(live.ver!==undefined && live.ver!==lastVer){
      const s=await (await fetch(WORKER+'/static',{cache:'no-store'})).json();
      S=s; lastVer=live.ver; applyStatic();
    }
    applyLive();
  }catch(e){
    const el=document.getElementById('conn'); el.textContent='● worker unreachable / waking up (can take ~50s)...'; el.style.color='#ffb74d'; delay=4000;
  }
  setTimeout(poll, document.hidden?5000:delay);
}
applyStatic(); poll();
</script></body></html>
"""

html = PAGE.replace("__WORKER__", WORKER_URL).replace("__HIST__", json.dumps(hist).replace("</", "<\\/"))
components.html(html, height=1380, scrolling=True)
