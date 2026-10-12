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

# Values directly read from worker's HUD object
t_type = hud.get("type", "CE")
active_symbol = hud.get("symbol", "NIFTY 22350 CE")
active_delta = float(hud.get("delta", 0.50))

entry_spot = float(hud.get("entry_spot", spot_price))
entry_opt = float(hud.get("entry_opt", live_real_ltp))
sl_spot = float(hud.get("sl_spot", entry_spot - 20.0))
sl_opt = float(hud.get("sl_opt", entry_opt - 10.0))
risk_pts = float(hud.get("risk_pts", 20.0))

t1_spot = float(hud.get("t1_spot", entry_spot + 40.0))
t1_opt = float(hud.get("t1_opt", entry_opt + 20.0))
t1_pts = float(hud.get("t1_pts", 40.0))

tmid_spot = float(hud.get("tmid_spot", entry_spot + 70.0))
tmid_opt = float(hud.get("tmid_opt", entry_opt + 35.0))
tmid_pts = float(hud.get("tmid_pts", 70.0))

tfinal_spot = float(hud.get("tfinal_spot", entry_spot + 110.0))
tfinal_opt = float(hud.get("tfinal_opt", entry_opt + 55.0))
tfinal_pts = float(hud.get("tfinal_pts", 110.0))

trail_spot = float(hud.get("trail_spot", sl_spot))
trail_opt = float(hud.get("trail_opt", sl_opt))
secured_pts = float(hud.get("secured_pts", -risk_pts))

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

        .history-signal-tag {{
            position: absolute; z-index: 55; pointer-events: none; display: none;
            background: rgba(22, 26, 37, 0.96); border: 1px solid #363c4e; border-radius: 5px;
            padding: 5px 8px; font-size: 9.5px; color: #d1d4dc; line-height: 1.4;
            box-shadow: 0 4px 12px rgba(0,0,0,0.7); transform: translate(-50%, -100%);
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

    <div id="strategyBox" class="draggable-strategy-box">
        <div class="box-drag-handle">::: STRATEGY HUD :::</div>
        <table>
            <thead>
                <tr>
                    <th>Parameter</th>
                    <th>Index (Spot)</th>
                    <th id="hudOptionHead">Option ({active_symbol})</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td class="label-cell">Call or Put</td>
                    <td class="val-cell"><span id="hudTypePill" class="tag-pill" style="background:{'#089981' if t_type == 'CE' else '#f23645'};">{t_type}</span></td>
                    <td class="opt-cell"><span id="hudTypeOptPill" class="tag-pill" style="background:{'#089981' if t_type == 'CE' else '#f23645'};">{t_type}</span></td>
                </tr>
                <tr>
                    <td class="label-cell">Entry</td>
                    <td class="val-cell"><b id="hudEntrySpot">{entry_spot:.1f}</b></td>
                    <td class="opt-cell"><b id="hudEntryOpt">₹{entry_opt:.2f}</b></td>
                </tr>
                <tr>
                    <td class="label-cell">SL (risk pts)</td>
                    <td class="val-cell text-red"><span id="hudSLSpot">{sl_spot:.1f} (-{risk_pts:.1f} pts)</span></td>
                    <td class="opt-cell text-red"><span id="hudSLOpt">₹{sl_opt:.2f} (-₹{int(round(risk_pts * active_delta * total_qty))})</span></td>
                </tr>
                <tr>
                    <td class="label-cell">Target 1</td>
                    <td class="val-cell text-green"><span id="hudT1Spot">{t1_spot:.1f} (+{t1_pts:.1f} pts)</span></td>
                    <td class="opt-cell text-green"><span id="hudT1Opt">₹{t1_opt:.2f} (+₹{int(round(t1_pts * active_delta * total_qty))})</span></td>
                </tr>
                <tr>
                    <td class="label-cell">Target Mid</td>
                    <td class="val-cell text-green"><span id="hudTMidSpot">{tmid_spot:.1f} (+{tmid_pts:.1f} pts)</span></td>
                    <td class="opt-cell text-green"><span id="hudTMidOpt">₹{tmid_opt:.2f}</span></td>
                </tr>
                <tr>
                    <td class="label-cell">Target Final</td>
                    <td class="val-cell text-green"><b id="hudTFinalSpot">{tfinal_spot:.1f} (+{tfinal_pts:.1f} pts)</b></td>
                    <td class="opt-cell text-green"><b id="hudTFinalOpt">₹{tfinal_opt:.2f} (+₹{int(round(tfinal_pts * active_delta * total_qty))})</b></td>
                </tr>
                <tr>
                    <td class="label-cell">Trailing SL</td>
                    <td class="val-cell text-trail"><span id="hudTrailSpot">{trail_spot:.1f}</span></td>
                    <td class="opt-cell text-trail"><span id="hudTrailOpt">₹{trail_opt:.2f}</span></td>
                </tr>
                <tr>
                    <td class="label-cell">Secured P&L (at SL)</td>
                    <td class="val-cell" style="color:{'#089981' if secured_pts >= 0 else '#f23645'}; font-weight:800;"><span id="hudPnLSpot">{secured_pts:+.1f} pts</span></td>
                    <td class="opt-cell" style="color:{'#089981' if secured_pts >= 0 else '#f23645'}; font-weight:800;"><span id="hudPnLOpt">₹{int(round(secured_pts * active_delta * total_qty)):+d}</span></td>
                </tr>
            </tbody>
        </table>
    </div>

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

        // Historical Breakout Markers directly from Worker
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

        // ==========================================
        // Interactive Drawing Tools System
        // ==========================================
        let toolCounter = 0;
        let currentDrawMode = null;
        let drawPoints = [];
        const activePositionWidgets = [];

        function activateDrawMode(mode) {{
            currentDrawMode = mode;
            drawPoints = [];
            chartContainer.classList.add('chart-click-crosshair');
            ['btnFibR', 'btnFibE', 'btnLong', 'btnShort'].forEach(b => document.getElementById(b).classList.remove('active-draw'));
            if (mode === 'FIB_RETRACE') document.getElementById('btnFibR').classList.add('active-draw');
            if (mode === 'FIB_EXT') document.getElementById('btnFibE').classList.add('active-draw');
            if (mode === 'LONG') document.getElementById('btnLong').classList.add('active-draw');
            if (mode === 'SHORT') document.getElementById('btnShort').classList.add('active-draw');
        }}

        function cancelDrawMode() {{
            currentDrawMode = null;
            drawPoints = [];
            chartContainer.classList.remove('chart-click-crosshair');
            ['btnFibR', 'btnFibE', 'btnLong', 'btnShort'].forEach(b => document.getElementById(b).classList.remove('active-draw'));
        }}

        chart.subscribeClick(param => {{
            if (!currentDrawMode || !param.point) return;
            const price = candleSeries.coordinateToPrice(param.point.y);
            if (!price) return;
            drawPoints.push({{ price: price, x: param.point.x, y: param.point.y }});

            if (currentDrawMode === 'LONG' || currentDrawMode === 'SHORT') {{
                spawnInteractivePositionWidget(currentDrawMode, price, param.point.x, param.point.y);
                cancelDrawMode();
            }} else if (currentDrawMode === 'FIB_RETRACE' && drawPoints.length === 2) {{
                spawnTwoPointFibRetrace(drawPoints[0], drawPoints[1]);
                cancelDrawMode();
            }} else if (currentDrawMode === 'FIB_EXT' && drawPoints.length === 3) {{
                spawnThreePointFibExtension(drawPoints[0], drawPoints[1], drawPoints[2]);
                cancelDrawMode();
            }}
        }});

        function spawnInteractivePositionWidget(type, entryPrice, clickX, clickY) {{
            toolCounter++;
            const wId = 'pos_w_' + toolCounter;
            const wObj = {{ id: wId, type: type, entryPrice: entryPrice, tgtPts: 60.0, slPts: 25.0, domElem: null }};
            const wElem = document.createElement('div');
            wElem.className = 'tv-widget-item';
            wElem.style.left = (clickX - 40) + 'px';
            wElem.style.top = (clickY - 50) + 'px';
            wObj.domElem = wElem;
            activePositionWidgets.push(wObj);

            renderSinglePosWidget(wObj);
            makeDraggable(wElem);
            document.getElementById('activeToolsContainer').appendChild(wElem);
        }}

        function renderSinglePosWidget(wObj) {{
            const optTgtGain = Math.round((wObj.tgtPts * liveDelta) * 10) / 10;
            const optStopLoss = Math.round((wObj.slPts * liveDelta) * 10) / 10;
            const expectedProfit = Math.round(optTgtGain * activeQty);
            const expectedLoss = Math.round(optStopLoss * activeQty);
            const rr = (wObj.tgtPts / wObj.slPts).toFixed(2);

            const pHeight = Math.max(30, Math.round(wObj.tgtPts * 1.5));
            const lHeight = Math.max(26, Math.round(wObj.slPts * 1.5));

            if (wObj.type === 'LONG') {{
                wObj.domElem.innerHTML = `
                    <div class="tv-del-btn" onclick="deletePosWidget('${{wObj.id}}')">✕</div>
                    <div class="tv-pos-zone tv-pos-green" style="height:${{pHeight}}px;">
                        <span style="font-weight:700; color:#fff;">Target: +${{wObj.tgtPts.toFixed(1)}} pts (₹${{expectedProfit}})</span>
                        <span style="color:#d1d4dc;">Opt Tgt: ₹${{({live_real_ltp} + optTgtGain).toFixed(2)}} | 1:${{rr}}</span>
                    </div>
                    <div style="height:3px; background:#2962ff; position:relative;">
                        <div class="tv-center-handle"></div>
                    </div>
                    <div class="tv-pos-zone tv-pos-red" style="height:${{lHeight}}px;">
                        <span style="font-weight:700; color:#fff;">Stop: -${{wObj.slPts.toFixed(1)}} pts (₹${{expectedLoss}})</span>
                        <span style="color:#d1d4dc;">Opt SL: ₹${{Math.max(0, {live_real_ltp} - optStopLoss).toFixed(2)}}</span>
                    </div>
                `;
            }} else {{
                wObj.domElem.innerHTML = `
                    <div class="tv-del-btn" onclick="deletePosWidget('${{wObj.id}}')">✕</div>
                    <div class="tv-pos-zone tv-pos-red" style="height:${{lHeight}}px;">
                        <span style="font-weight:700; color:#fff;">Stop: -${{wObj.slPts.toFixed(1)}} pts (₹${{expectedLoss}})</span>
                        <span style="color:#d1d4dc;">Opt SL: ₹${{Math.max(0, {live_real_ltp} - optStopLoss).toFixed(2)}}</span>
                    </div>
                    <div style="height:3px; background:#2962ff; position:relative;">
                        <div class="tv-center-handle"></div>
                    </div>
                    <div class="tv-pos-zone tv-pos-green" style="height:${{pHeight}}px;">
                        <span style="font-weight:700; color:#fff;">Target: +${{wObj.tgtPts.toFixed(1)}} pts (₹${{expectedProfit}})</span>
                        <span style="color:#d1d4dc;">Opt Tgt: ₹${{({live_real_ltp} + optTgtGain).toFixed(2)}} | 1:${{rr}}</span>
                    </div>
                `;
            }}
        }}

        function deletePosWidget(id) {{
            const idx = activePositionWidgets.findIndex(w => w.id === id);
            if (idx !== -1) {{
                activePositionWidgets[idx].domElem.remove();
                activePositionWidgets.splice(idx, 1);
            }}
        }}

        function spawnTwoPointFibRetrace(pt1, pt2) {{
            toolCounter++;
            const pHigh = Math.max(pt1.price, pt2.price);
            const pLow = Math.min(pt1.price, pt2.price);
            const range = pHigh - pLow;
            const levels = [
                {{ p: pHigh, c: '#787b86', lbl: '0.0%' }},
                {{ p: pHigh - (range * 0.236), c: '#f23645', lbl: '23.6%' }},
                {{ p: pHigh - (range * 0.382), c: '#ff9800', lbl: '38.2%' }},
                {{ p: pHigh - (range * 0.500), c: '#4caf50', lbl: '50.0%' }},
                {{ p: pHigh - (range * 0.618), c: '#00bcd4', lbl: '61.8%' }},
                {{ p: pHigh - (range * 0.786), c: '#2196f3', lbl: '78.6%' }},
                {{ p: pLow, c: '#787b86', lbl: '100.0%' }}
            ];
            const lines = levels.map(lv => candleSeries.createPriceLine({{ price: lv.p, color: lv.c, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.SparseDotted, title: lv.lbl }}));

            const anchor = document.createElement('div');
            anchor.className = 'tv-widget-item';
            anchor.style.left = Math.min(pt1.x, pt2.x) + 'px';
            anchor.style.top = Math.min(pt1.y, pt2.y) + 'px';
            anchor.style.background = '#1e222d';
            anchor.style.border = '1px solid #ffd600';
            anchor.style.padding = '4px 8px';
            anchor.style.width = '140px';
            anchor.innerHTML = `<div style="font-size:8.5px; color:#ffd600; font-weight:700;">Fib Retrace #${{toolCounter}}</div><div class="tv-del-btn">✕</div>`;
            anchor.querySelector('.tv-del-btn').onclick = () => {{ lines.forEach(l => candleSeries.removePriceLine(l)); anchor.remove(); }};
            makeDraggable(anchor);
            document.getElementById('activeToolsContainer').appendChild(anchor);
        }}

        function spawnThreePointFibExtension(pt1, pt2, pt3) {{
            toolCounter++;
            const impulse = Math.abs(pt2.price - pt1.price);
            const isUp = pt2.price >= pt1.price;
            const levels = [
                {{ p: isUp ? (pt3.price + impulse * 0.618) : (pt3.price - impulse * 0.618), lbl: 'Ext 61.8%', c: '#00e5ff' }},
                {{ p: isUp ? (pt3.price + impulse * 1.000) : (pt3.price - impulse * 1.000), lbl: 'Ext 100.0%', c: '#ffd600' }},
                {{ p: isUp ? (pt3.price + impulse * 1.272) : (pt3.price - impulse * 1.272), lbl: 'Ext 127.2%', c: '#00bfa5' }},
                {{ p: isUp ? (pt3.price + impulse * 1.618) : (pt3.price - impulse * 1.618), lbl: 'Ext 161.8%', c: '#ff6d00' }}
            ];
            const lines = levels.map(lv => candleSeries.createPriceLine({{ price: lv.p, color: lv.c, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: lv.lbl }}));

            const anchor = document.createElement('div');
            anchor.className = 'tv-widget-item';
            anchor.style.left = pt3.x + 'px';
            anchor.style.top = pt3.y + 'px';
            anchor.style.background = '#1e222d';
            anchor.style.border = '1px solid #00bfa5';
            anchor.style.padding = '4px 8px';
            anchor.style.width = '140px';
            anchor.innerHTML = `<div style="font-size:8.5px; color:#00bfa5; font-weight:700;">Fib Ext #${{toolCounter}}</div><div class="tv-del-btn">✕</div>`;
            anchor.querySelector('.tv-del-btn').onclick = () => {{ lines.forEach(l => candleSeries.removePriceLine(l)); anchor.remove(); }};
            makeDraggable(anchor);
            document.getElementById('activeToolsContainer').appendChild(anchor);
        }}

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

            const sig = historyCards[param.time];
            if (sig && param.point) {{
                hTag.style.display = 'block';
                hTag.style.left = param.point.x + 'px';
                hTag.style.top = (param.point.y - 12) + 'px';
                const tagColor = sig.type === 'CE' ? '#00bfa5' : '#f23645';
                hTag.innerHTML = `
                    <div style="font-weight:bold; color:${{tagColor}}; border-bottom:1px solid #363c4e; padding-bottom:2px; margin-bottom:3px;">
                        ${{sig.title}} (${{sig.time_str}})
                    </div>
                    <div><b>Entry:</b> ${{sig.entry.toFixed(1)}}</div>
                    <div><b>Stop Loss:</b> ${{sig.sl.toFixed(1)}} (-${{Math.abs(sig.entry - sig.sl).toFixed(1)}} pts)</div>
                    <div><b>Target 1:</b> ${{sig.target1.toFixed(1)}} (+${{Math.abs(sig.target1 - sig.entry).toFixed(1)}} pts)</div>
                    <div><b>Target Mid:</b> ${{sig.target_mid.toFixed(1)}} (+${{Math.abs(sig.target_mid - sig.entry).toFixed(1)}} pts)</div>
                    <div><b>Target Max:</b> ${{sig.target_final.toFixed(1)}} (+${{Math.abs(sig.target_final - sig.entry).toFixed(1)}} pts)</div>
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
                        const h = stData.hud;
                        const isCE = h.type === 'CE';
                        liveDelta = h.delta || 0.50;

                        document.getElementById('topDeltaVal').innerText = liveDelta.toFixed(2);
                        document.getElementById('hudOptionHead').innerText = "Option (" + h.symbol + ")";
                        document.getElementById('topContractSym').innerText = h.symbol;
                        document.getElementById('hudTypePill').innerText = h.type;
                        document.getElementById('hudTypeOptPill').innerText = h.type;
                        document.getElementById('hudTypePill').style.background = isCE ? '#089981' : '#f23645';
                        document.getElementById('hudTypeOptPill').style.background = isCE ? '#089981' : '#f23645';

                        document.getElementById('hudEntrySpot').innerText = h.entry_spot.toFixed(1);
                        document.getElementById('hudEntryOpt').innerText = "₹" + h.entry_opt.toFixed(2);

                        document.getElementById('hudSLSpot').innerText = h.sl_spot.toFixed(1) + " (-" + h.risk_pts.toFixed(1) + " pts)";
                        document.getElementById('hudSLOpt').innerText = "₹" + h.sl_opt.toFixed(2) + " (-₹" + Math.round(h.risk_pts * liveDelta * activeQty) + ")";

                        document.getElementById('hudT1Spot').innerText = h.t1_spot.toFixed(1) + " (+" + h.t1_pts.toFixed(1) + " pts)";
                        document.getElementById('hudT1Opt').innerText = "₹" + h.t1_opt.toFixed(2) + " (+₹" + Math.round(h.t1_pts * liveDelta * activeQty) + ")";

                        document.getElementById('hudTMidSpot').innerText = h.tmid_spot.toFixed(1) + " (+" + h.tmid_pts.toFixed(1) + " pts)";
                        document.getElementById('hudTMidOpt').innerText = "₹" + h.tmid_opt.toFixed(2);

                        document.getElementById('hudTFinalSpot').innerText = h.tfinal_spot.toFixed(1) + " (+" + h.tfinal_pts.toFixed(1) + " pts)";
                        document.getElementById('hudTFinalOpt').innerText = "₹" + h.tfinal_opt.toFixed(2) + " (+₹" + Math.round(h.tfinal_pts * liveDelta * activeQty) + ")";

                        document.getElementById('hudTrailSpot').innerText = h.trail_spot.toFixed(1);
                        document.getElementById('hudTrailOpt').innerText = "₹" + h.trail_opt.toFixed(2);

                        const pnlSpotElem = document.getElementById('hudPnLSpot');
                        const pnlOptElem = document.getElementById('hudPnLOpt');
                        const sign = h.secured_pts >= 0 ? "+" : "";
                        pnlSpotElem.innerText = sign + h.secured_pts.toFixed(1) + " pts";
                        pnlOptElem.innerText = "₹" + sign + Math.round(h.secured_pts * liveDelta * activeQty);
                        pnlSpotElem.parentElement.style.color = h.secured_pts >= 0 ? '#089981' : '#f23645';
                        pnlOptElem.parentElement.style.color = h.secured_pts >= 0 ? '#089981' : '#f23645';
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
