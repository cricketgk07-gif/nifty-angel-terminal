import datetime
import html
import json
import os
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    layout="wide",
    page_title="Nifty 50 Pro Terminal",
    page_icon="📈",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
    #MainMenu, footer, header {display: none !important;}
    .block-container {
        padding: 4px 8px !important;
        margin: 0 !important;
        max-width: 100vw !important;
        height: 100vh !important;
        overflow: hidden !important;
    }
    .stApp[data-test-script-state="running"] {
        opacity: 1 !important;
        filter: none !important;
        transition: none !important;
    }
    div[data-testid="stStatusWidget"] {
        display: none !important;
    }
    div[data-testid="column"] {
        padding: 0 3px !important;
    }
    div[data-testid="stSelectbox"] label, div[data-testid="stNumberInput"] label {
        display: none !important;
    }
    div[data-testid="stSelectbox"] > div, div[data-testid="stNumberInput"] > div {
        background-color: #161a25 !important;
        color: #ffd600 !important;
        border: 1px solid #2a2e39 !important;
        border-radius: 4px !important;
        font-size: 11px !important;
        min-height: 32px !important;
    }
    iframe {
        border: none !important;
        width: 100vw !important;
        display: block !important;
        overflow: hidden !important;
    }
    body {
        background-color: #0b0e14;
        margin: 0;
        overflow: hidden;
    }
</style>
""",
    unsafe_allow_html=True,
)

UPSTASH_REDIS_REST_URL = st.secrets.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = st.secrets.get("UPSTASH_REDIS_REST_TOKEN", "")

LOT_SIZE_QTY = 65


def read_from_redis(key="nifty_live_state"):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    url = f"{UPSTASH_REDIS_REST_URL}/get/{key}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=3)
        if r.status_code == 200:
            val = r.json().get("result")
            if val:
                return json.loads(val)
    except Exception:
        pass
    return None


def write_selected_budget(budget_val):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return
    url = f"{UPSTASH_REDIS_REST_URL}/set/user_selected_budget"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        requests.post(url, headers=headers, data=str(budget_val), timeout=2)
    except Exception:
        pass


live_state = read_from_redis("nifty_live_state")

if not live_state or "candles" not in live_state or len(live_state["candles"]) == 0:
    st.info("Waiting for Render background worker to populate initial candle data...")
    st.stop()

# Raw arrays directly from worker
raw_candles = live_state["candles"]
hud = live_state.get("hud", {})
pivots = live_state.get("pivots", {})
orb_levels = live_state.get("orb_levels", {})
fib_lines = live_state.get("fib_lines", {})
markers = live_state.get("markers", [])
history_cards = live_state.get("history_cards", {})

spot_price = float(live_state.get("ltp", raw_candles[-1]["close"]))
live_real_ltp = float(live_state.get("opt_ltp", hud.get("entry_opt", 75.0)))
if live_real_ltp <= 0.0:
    live_real_ltp = float(hud.get("entry_opt", 75.0))

ctrl_cols = st.columns([2.2, 0.9, 6.9])

budget_map = {
    "Budget (< ₹50)": 45.0,
    "Budget (< ₹60)": 55.0,
    "Budget (< ₹70)": 65.0,
    "Budget (< ₹80)": 75.0,
    "Budget (< ₹90)": 85.0,
    "Budget (< ₹100)": 95.0,
    "ATM (At-The-Money)": 130.0,
}

with ctrl_cols[0]:
    mode_selection = st.selectbox(
        "Budget",
        list(budget_map.keys()),
        index=3,
    )
    write_selected_budget(budget_map.get(mode_selection, 75.0))

with ctrl_cols[1]:
    sel_lots = st.number_input("Lots", min_value=1, max_value=100, value=1, step=1)

total_qty = sel_lots * LOT_SIZE_QTY

# Values read directly from worker's HUD
t_type = hud.get("type", "CE")
active_symbol = hud.get("symbol", "NIFTY 22400 CE")
active_delta = float(hud.get("delta", 0.50))

with ctrl_cols[2]:
    st.markdown(
        f"""
        <div style="background:#161a25; border:1px solid #2a2e39; border-radius:4px; padding:3px 8px; display:flex; gap:12px; align-items:center; font-size:11px; height:32px;">
            <div>Contract: <b id="topContractSym" style="color:#00e5ff;">{active_symbol}</b></div>
            <div>LTP: <b id="topOptionLTP" style="color:#ffd600; font-size:13px;">₹{live_real_ltp:.2f}</b></div>
            <div>Mode: <b style="color:#ab47bc;">{mode_selection}</b></div>
            <div>Qty: <b style="color:#089981;">{total_qty}</b> ({sel_lots}L)</div>
            <div>Delta: <b id="topDeltaVal" style="color:#00e5ff;">{active_delta:.2f}</b></div>
            <div>Status: <b style="color:#00e5ff;">CLOUD ENGINE ACTIVE</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

candles_data = [
    {
        "time": int(c["time"]),
        "open": float(c["open"]),
        "high": float(c["high"]),
        "low": float(c["low"]),
        "close": float(c["close"]),
    }
    for c in raw_candles
]

vwap_data = [{"time": int(c["time"]), "value": float(c.get("vwap", c["close"]))} for c in raw_candles]
ema_data = [{"time": int(c["time"]), "value": float(c.get("ema9", c["close"]))} for c in raw_candles]

candles_json = json.dumps(candles_data)
vwap_json = json.dumps(vwap_data)
ema_json = json.dumps(ema_data)
markers_json = json.dumps(markers)
history_cards_json = json.dumps(history_cards)
pivots_json = json.dumps(pivots)
orb_json = json.dumps(orb_levels)
fibs_json = json.dumps(fib_lines)
hud_json = json.dumps(hud)

day_open = float(raw_candles[0]["open"])
chg = spot_price - day_open
chg_pct = (chg / day_open) * 100
chg_str = f"{chg:+.2f} ({chg_pct:+.2f}%)"
chg_color = "#089981" if chg >= 0 else "#f23645"

curr = raw_candles[-1]
redis_url_str = UPSTASH_REDIS_REST_URL
redis_token_str = UPSTASH_REDIS_REST_TOKEN

html_code = f"""
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{
            width: 100vw; height: 100vh;
            background-color: #0b0e14;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            overflow: hidden;
            display: flex; flex-direction: column;
        }}

        #topHeaderArea {{
            width: 100vw; height: 38px;
            background: #0b0e14;
            padding: 2px 8px;
            display: flex; align-items: center; justify-content: space-between;
            z-index: 60; flex-shrink: 0;
            border-bottom: 1px solid #161a25;
        }}

        .top-row-1 {{
            display: flex; align-items: center; gap: 8px;
        }}
        .sym-group {{
            display: flex; align-items: center; gap: 4px; flex-shrink: 0;
        }}
        .badge {{
            background: #2962ff; color: #fff; font-size: 11px; padding: 2px 5px;
            border-radius: 3px; font-weight: 700;
        }}
        .sym-title {{ font-size: 13px; font-weight: 700; color: #d1d4dc; }}
        .sym-price {{ font-size: 12px; font-weight: 700; }}

        .tf-bar {{
            display: flex; gap: 2px; background: rgba(30, 34, 45, 0.95);
            padding: 2px 4px; border-radius: 4px; border: 1px solid #2a2e39; flex-shrink: 0;
        }}
        .tf-btn {{
            background: transparent; border: none; color: #787b86;
            font-size: 10px; font-weight: 600; padding: 2px 5px;
            border-radius: 2px; cursor: not-allowed; opacity: 0.45;
        }}
        .tf-btn.active {{
            background: #2962ff; color: #ffffff; font-weight: 700; cursor: default; opacity: 1.0;
        }}

        .tools-bar {{
            display: flex; align-items: center; gap: 3px; background: rgba(22, 26, 37, 0.95);
            padding: 2px 5px; border-radius: 4px; border: 1px solid #363c4e; font-size: 9px;
            flex-shrink: 0;
        }}
        .tool-btn {{
            background: #161a25; border: 1px solid #2a2e39; color: #d1d4dc;
            font-size: 9px; padding: 2px 5px; border-radius: 3px; cursor: pointer;
            font-weight: 600;
        }}
        .tool-btn:hover {{ background: #2962ff; color: #ffffff; }}
        .tool-btn.active-draw {{ background: #00e5ff; color: #000; font-weight: 800; }}

        .dynamic-ohlc-row {{
            font-size: 9.5px; color: #787b86; display: flex; gap: 6px;
            background: rgba(11, 14, 20, 0.92); padding: 2px 6px; border-radius: 3px;
            font-family: monospace;
        }}
        .dynamic-ohlc-row b {{ color: #d1d4dc; }}

        #chartArea {{
            width: 100vw;
            height: calc(100vh - 80px);
            position: relative;
            flex: 1 1 auto;
            overflow: hidden;
            margin-bottom: 24px;
            cursor: default !important;
        }}

        .draggable-strategy-box {{
            position: absolute; bottom: 35px; right: 55px; z-index: 60;
            background: rgba(19, 23, 34, 0.97); border: 1px solid #2a2e39;
            border-radius: 6px; font-size: 9.5px; color: #d1d4dc; overflow: hidden;
            box-shadow: 0 4px 18px rgba(0,0,0,0.9); cursor: grab; user-select: none;
            touch-action: none; min-width: 340px;
        }}
        .draggable-strategy-box:active {{ cursor: grabbing; }}
        .box-drag-handle {{
            background: #161a25; padding: 3px 6px; font-size: 8.5px; font-weight: 700;
            color: #787b86; text-align: center; border-bottom: 1px solid #2a2e39;
            letter-spacing: 0.5px;
        }}
        .draggable-strategy-box table {{ border-collapse: collapse; width: 100%; }}
        .draggable-strategy-box th {{
            background: #131722; padding: 3px 6px; font-size: 8.5px; font-weight: 700;
            color: #787b86; border-bottom: 1px solid #2a2e39; text-align: right;
        }}
        .draggable-strategy-box th:first-child {{ text-align: left; }}
        .draggable-strategy-box td {{ padding: 3px 6px; border-bottom: 1px solid #222631; white-space: nowrap; }}
        .label-cell {{ color: #787b86; font-weight: 500; text-align: left; }}
        .val-cell {{ font-weight: 700; color: #ffffff; text-align: right; }}
        .opt-cell {{ font-weight: 700; color: #ffd600; text-align: right; }}
        .tag-pill {{ color: #fff; font-weight: bold; border-radius: 2px; padding: 1px 5px; text-align: center; font-size: 9px; }}
        .text-red {{ color: #f23645; }}
        .text-green {{ color: #089981; }}
        .text-trail {{ color: #2962ff; font-weight: bold; }}

        /* Persistent High-Priority Hover Tag */
        .history-signal-tag {{
            position: absolute;
            z-index: 99999 !important;
            pointer-events: none !important;
            display: none;
            background: rgba(18, 22, 32, 0.98);
            border: 1.5px solid #363c4e;
            border-radius: 6px;
            padding: 8px 12px;
            font-size: 10.5px;
            color: #e0e3eb;
            line-height: 1.45;
            box-shadow: 0 8px 26px rgba(0,0,0,0.95);
            transform: translate(-50%, -105%);
            min-width: 200px;
        }}

        .tv-widget-item {{
            position: absolute; z-index: 55; user-select: none; touch-action: none;
            font-family: sans-serif; border-radius: 4px; overflow: visible;
            display: flex; flex-direction: column; width: 220px;
        }}
        .tv-pos-zone {{
            position: relative; padding: 6px 8px; font-size: 9px;
            display: flex; flex-direction: column; justify-content: center;
        }}
        .tv-pos-green {{ background: rgba(8, 153, 129, 0.40); border: 1.5px solid #089981; }}
        .tv-pos-red {{ background: rgba(242, 54, 69, 0.40); border: 1.5px solid #f23645; }}
        .tv-center-handle {{
            position: absolute; left: 4px; width: 12px; height: 12px;
            background: #2962ff; border: 2px solid #ffffff; border-radius: 50%;
            cursor: move; touch-action: none; z-index: 68; top: -5px;
        }}
        .tv-del-btn {{
            position: absolute; top: -9px; right: -9px; z-index: 70;
            background: #1e222d; border: 1px solid #f23645; color: #f23645;
            border-radius: 50%; width: 20px; height: 20px; font-size: 11px;
            display: flex; align-items: center; justify-content: center; cursor: pointer;
            box-shadow: 0 2px 6px rgba(0,0,0,0.8); font-weight: 800;
        }}
        .chart-click-crosshair {{ cursor: crosshair !important; }}
    </style>
</head>
<body>
    <div id="topHeaderArea">
        <div class="top-row-1">
            <div class="sym-group">
                <span class="badge">50</span>
                <span class="sym-title">NIFTY</span>
                <span id="topPriceSpan" class="sym-price" style="color: {chg_color};">{spot_price:.2f} <span id="topChgSpan" style="font-size: 10px;">{chg_str}</span></span>
            </div>

            <div class="tf-bar">
                <button class="tf-btn" disabled>1m</button>
                <button class="tf-btn" disabled>3m</button>
                <button class="tf-btn active">5m</button>
                <button class="tf-btn" disabled>15m</button>
                <button class="tf-btn" disabled>30m</button>
                <button class="tf-btn" disabled>1h</button>
                <button class="tf-btn" disabled>1D</button>
            </div>

            <div id="ohlcRow" class="dynamic-ohlc-row">
                <span>O: <b id="barO">{curr['open']:.2f}</b></span>
                <span>H: <b id="barH">{curr['high']:.2f}</b></span>
                <span>L: <b id="barL">{curr['low']:.2f}</b></span>
                <span>C: <b id="barC">{spot_price:.2f}</b></span>
                <span>VWAP: <b id="barVWAP" style="color:#ab47bc;">{curr.get('vwap', spot_price):.2f}</b></span>
                <span>EMA: <b id="barEMA" style="color:#2962ff;">{curr.get('ema9', spot_price):.2f}</b></span>
            </div>
        </div>

        <div style="display:flex; align-items:center; gap:6px;">
            <div class="tools-bar">
                <button id="btnFibR" class="tool-btn" onclick="activateDrawMode('FIB_RETRACE')">+ Fib Retrace</button>
                <button id="btnFibE" class="tool-btn" onclick="activateDrawMode('FIB_EXT')">+ Fib Ext</button>
                <button id="btnLong" class="tool-btn" onclick="activateDrawMode('LONG')">+ Long Tool</button>
                <button id="btnShort" class="tool-btn" onclick="activateDrawMode('SHORT')">+ Short Tool</button>
            </div>
        </div>
    </div>

    <div id="chartArea"></div>
    <div id="activeToolsContainer"></div>

    <!-- Scalable Multi-Row Strategy HUD Container -->
    <div id="strategyBox" class="draggable-strategy-box">
        <div class="box-drag-handle">::: STRATEGY HUD :::</div>
        <div id="hudTableContainer"></div>
    </div>

    <!-- Hover Tag Overlay Card -->
    <div id="historyTag" class="history-signal-tag"></div>

    <script>
        let liveDelta = {active_delta};
        const activeLots = {sel_lots};
        const LOT_SIZE = {LOT_SIZE_QTY};
        const activeQty = activeLots * LOT_SIZE;

        const chartContainer = document.getElementById('chartArea');
        const chart = LightweightCharts.createChart(chartContainer, {{
            width: chartContainer.clientWidth,
            height: chartContainer.clientHeight,
            layout: {{ background: {{ color: '#0b0e14' }}, textColor: '#787b86', fontSize: 10 }},
            grid: {{ vertLines: {{ color: '#161a25' }}, horzLines: {{ color: '#161a25' }} }},
            crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
            rightPriceScale: {{ borderColor: '#2a2e39', autoScale: true, scaleMargins: {{ top: 0.08, bottom: 0.20 }} }},
            timeScale: {{ borderColor: '#2a2e39', timeVisible: true, secondsVisible: false, rightOffset: 10, barSpacing: 6 }}
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#089981', downColor: '#f23645', borderUpColor: '#089981',
            borderDownColor: '#f23645', wickUpColor: '#089981', wickDownColor: '#f23645'
        }});
        candleSeries.setData({candles_json});

        const vwapSeries = chart.addLineSeries({{ color: '#ab47bc', lineWidth: 2, title: 'VWAP' }});
        vwapSeries.setData({vwap_json});

        const emaSeries = chart.addLineSeries({{ color: '#2962ff', lineWidth: 1, title: '9-EMA' }});
        emaSeries.setData({ema_json});

        // Set Historical Breakout Markers directly from Worker
        candleSeries.setMarkers({markers_json});

        // Precalculated ORB Lines directly from Worker
        const orb = {orb_json};
        if (orb && orb.orb_h) {{
            candleSeries.createPriceLine({{ price: orb.orb_h, color: '#089981', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'ORB HIGH' }});
            candleSeries.createPriceLine({{ price: orb.orb_l, color: '#f23645', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'ORB LOW' }});
        }}

        // Precalculated Fibonacci Extensions directly from Worker
        const fibs = {fibs_json};
        if (fibs && fibs.fib_100) {{
            candleSeries.createPriceLine({{ price: fibs.fib_100, color: '#ffd600', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'EXT 100%' }});
            candleSeries.createPriceLine({{ price: fibs.fib_161, color: '#00e5ff', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'EXT 161.8%' }});
            candleSeries.createPriceLine({{ price: fibs.fib_261, color: '#ff6d00', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Solid, title: 'EXT 261.8% (MAX)' }});
        }}

        // Precalculated Floor Pivots directly from Worker
        const pv = {pivots_json};
        if (pv && pv.P) {{
            candleSeries.createPriceLine({{ price: pv.P, color: '#ffd600', lineWidth: 1.5, title: 'PIVOT (P)' }});
            candleSeries.createPriceLine({{ price: pv.R1, color: '#f23645', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'R1' }});
            candleSeries.createPriceLine({{ price: pv.S1, color: '#089981', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'S1' }});
            candleSeries.createPriceLine({{ price: pv.PDH, color: '#ffb300', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: 'PDH' }});
            candleSeries.createPriceLine({{ price: pv.PDL, color: '#fb8c00', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: 'PDL' }});
        }}

        // Dynamic Multi-Row Target HUD Table
        function renderHUDTable(h) {{
            if (!h || !h.entry_spot) return;
            const isCE = h.type === 'CE';
            const themeCol = isCE ? '#089981' : '#f23645';
            const totalRiskINR = Math.round(h.risk_pts * liveDelta * activeQty);

            let tgRows = '';
            if (h.targets && h.targets.length > 0) {{
                h.targets.forEach(tg => {{
                    const profitINR = Math.round(tg.pts * liveDelta * activeQty);
                    tgRows += `
                        <tr>
                            <td class="label-cell">${{tg.label}}</td>
                            <td class="val-cell text-green">${{tg.spot.toFixed(1)}} (+${{tg.pts.toFixed(1)}} pts)</td>
                            <td class="opt-cell text-green">₹${{tg.opt.toFixed(2)}} (+₹${{profitINR}})</td>
                        </tr>
                    `;
                }});
            }}

            const secINR = Math.round(h.secured_pts * liveDelta * activeQty);
            const sign = h.secured_pts >= 0 ? '+' : '';
            const pnlCol = h.secured_pts >= 0 ? '#089981' : '#f23645';

            document.getElementById('hudTableContainer').innerHTML = `
                <table>
                    <thead>
                        <tr>
                            <th>Parameter</th>
                            <th>Index (Spot)</th>
                            <th>Option (${{h.symbol}})</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td class="label-cell">Call or Put</td>
                            <td class="val-cell"><span class="tag-pill" style="background:${{themeCol}}">${{h.type}}</span></td>
                            <td class="opt-cell"><span class="tag-pill" style="background:${{themeCol}}">${{h.type}}</span></td>
                        </tr>
                        <tr>
                            <td class="label-cell">Entry</td>
                            <td class="val-cell"><b>${{h.entry_spot.toFixed(1)}}</b></td>
                            <td class="opt-cell"><b>₹${{h.entry_opt.toFixed(2)}}</b></td>
                        </tr>
                        <tr>
                            <td class="label-cell">SL (risk pts)</td>
                            <td class="val-cell text-red">${{h.sl_spot.toFixed(1)}} (-${{h.risk_pts.toFixed(1)}} pts)</td>
                            <td class="opt-cell text-red">₹${{h.sl_opt.toFixed(2)}} (-₹${{totalRiskINR}})</td>
                        </tr>
                        ${{tgRows}}
                        <tr>
                            <td class="label-cell">Trailing SL</td>
                            <td class="val-cell text-trail">${{h.trail_spot.toFixed(1)}}</td>
                            <td class="opt-cell text-trail">₹${{h.trail_opt.toFixed(2)}}</td>
                        </tr>
                        <tr>
                            <td class="label-cell">Secured P&L (at SL)</td>
                            <td class="val-cell" style="color:${{pnlCol}}; font-weight:800;">${{sign}}${{h.secured_pts.toFixed(1)}} pts</td>
                            <td class="opt-cell" style="color:${{pnlCol}}; font-weight:800;">₹${{sign}}${{secINR}}</td>
                        </tr>
                    </tbody>
                </table>
            `;
        }}

        renderHUDTable({hud_json});

        // Tooltip Hover on Breakout Candle
        const historyCards = {history_cards_json};
        const hTag = document.getElementById('historyTag');

        chart.subscribeCrosshairMove(param => {{
            if (!param.time || !param.seriesData.get(candleSeries)) {{
                hTag.style.display = 'none';
                return;
            }}
            const bar = param.seriesData.get(candleSeries);
            document.getElementById('barO').innerText = bar.open.toFixed(2);
            document.getElementById('barH').innerText = bar.high.toFixed(2);
            document.getElementById('barL').innerText = bar.low.toFixed(2);
            document.getElementById('barC').innerText = bar.close.toFixed(2);
            const vBar = param.seriesData.get(vwapSeries);
            if (vBar) document.getElementById('barVWAP').innerText = vBar.value.toFixed(2);
            const eBar = param.seriesData.get(emaSeries);
            if (eBar) document.getElementById('barEMA').innerText = eBar.value.toFixed(2);

            // Robust Lookup for both Integer and String Timestamps
            const timeKey = param.time;
            const sig = historyCards[timeKey] || historyCards[String(timeKey)];

            if (sig && param.point) {{
                hTag.style.display = 'block';
                hTag.style.left = param.point.x + 'px';
                hTag.style.top = (param.point.y - 14) + 'px';
                const tagColor = sig.type === 'CE' ? '#00bfa5' : '#f23645';

                let tgsHtml = '';
                if (sig.targets && sig.targets.length > 0) {{
                    sig.targets.forEach((tg, idx) => {{
                        tgsHtml += `<div><b>Target ${{idx+1}}:</b> ${{tg.toFixed(1)}} (+${{Math.abs(tg - sig.entry).toFixed(1)}} pts)</div>`;
                    }});
                }}

                hTag.innerHTML = `
                    <div style="font-weight:bold; color:${{tagColor}}; border-bottom:1px solid #363c4e; padding-bottom:3px; margin-bottom:4px; font-size:11px;">
                        ${{sig.title}}
                    </div>
                    <div><b>Date:</b> ${{sig.date_readable || sig.date}}</div>
                    <div><b>Time:</b> ${{sig.time_str}}</div>
                    <div><b>Entry Spot:</b> ${{sig.entry.toFixed(1)}}</div>
                    <div><b>Initial SL:</b> ${{sig.sl.toFixed(1)}} (-${{Math.abs(sig.entry - sig.sl).toFixed(1)}} pts)</div>
                    ${{tgsHtml}}
                `;
            }} else {{
                hTag.style.display = 'none';
            }}
        }});

        function makeDraggable(elem) {{
            let isDragging = false, startX, startY, initLeft, initTop;
            function onStart(e) {{
                if (e.target.classList.contains('tv-del-btn')) return;
                isDragging = true;
                const cX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
                const cY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
                startX = cX; startY = cY;
                const r = elem.getBoundingClientRect();
                initLeft = r.left; initTop = r.top;
                elem.style.right = 'auto'; elem.style.bottom = 'auto';
                elem.style.left = initLeft + 'px'; elem.style.top = initTop + 'px';
            }}
            function onMove(e) {{
                if (!isDragging) return;
                const cX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
                const cY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
                elem.style.left = (initLeft + (cX - startX)) + 'px';
                elem.style.top = (initTop + (cY - startY)) + 'px';
            }}
            function onEnd() {{ isDragging = false; }}
            elem.addEventListener('mousedown', onStart);
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onEnd);
            elem.addEventListener('touchstart', onStart, {{ passive: true }});
            window.addEventListener('touchmove', onMove, {{ passive: true }});
            window.addEventListener('touchend', onEnd);
        }}
        makeDraggable(document.getElementById('strategyBox'));

        // Dynamic State Polling via Upstash Redis
        const redisUrl = "{redis_url_str}";
        const redisToken = "{redis_token_str}";

        async function pullLiveCloudState() {{
            if (!redisUrl || !redisToken) return;
            try {{
                const r = await fetch(redisUrl + "/get/nifty_live_state", {{
                    headers: {{ "Authorization": "Bearer " + redisToken }}
                }});
                const d = await r.json();
                if (d && d.result) {{
                    const stData = JSON.parse(d.result);
                    if (stData.candles && stData.candles.length > 0) {{
                        const lastBar = stData.candles[stData.candles.length - 1];
                        candleSeries.update(lastBar);
                        document.getElementById('barC').innerText = lastBar.close.toFixed(2);
                        document.getElementById('barH').innerText = lastBar.high.toFixed(2);
                        document.getElementById('barL').innerText = lastBar.low.toFixed(2);
                    }}
                    if (stData.opt_ltp) {{
                        const optLtpElem = document.getElementById('topOptionLTP');
                        if (optLtpElem) optLtpElem.innerText = "₹" + stData.opt_ltp.toFixed(2);
                    }}
                    if (stData.hud) {{
                        liveDelta = stData.hud.delta || 0.50;
                        document.getElementById('topDeltaVal').innerText = liveDelta.toFixed(2);
                        document.getElementById('topContractSym').innerText = stData.hud.symbol;
                        renderHUDTable(stData.hud);
                    }}
                }}
            }} catch(e) {{}}
        }}

        setInterval(pullLiveCloudState, 1500);

        function resizeChartProperly() {{
            chart.applyOptions({{ width: chartContainer.clientWidth, height: chartContainer.clientHeight }});
        }}
        window.addEventListener('resize', resizeChartProperly);
        setTimeout(resizeChartProperly, 200);
    </script>
</body>
</html>
"""

components.html(html_code, height=650, scrolling=False)
