import datetime
import html
import json
import math
import os
import requests
import pandas as pd
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

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
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


live_state = read_from_redis("nifty_live_state")

if not live_state or "candles" not in live_state or len(live_state["candles"]) == 0:
    st.info("Waiting for Render background worker to populate initial candle data...")
    st.stop()

raw_candles = live_state["candles"]
df = pd.DataFrame(raw_candles)
df["dt"] = pd.to_datetime(df["time"], unit="s").dt.tz_localize("UTC").dt.tz_convert(IST)
df["date"] = df["dt"].dt.date

# VWAP & Indicators
candle_spread = (df["high"] - df["low"]) + (df["close"] - df["open"]).abs()
df["calc_vol"] = candle_spread * 1250.0 + 500.0
df["tp"] = (df["high"] + df["low"] + df["close"]) / 3.0
df["vol_mult"] = df["tp"] * df["calc_vol"]
df["cum_vol"] = df.groupby("date")["calc_vol"].cumsum()
df["cum_vp"] = df.groupby("date")["vol_mult"].cumsum()
df["vwap"] = df["cum_vp"] / df["cum_vol"]
df["vwap"] = df["vwap"].fillna(df["tp"])
df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()

# High-Low Pivots
unique_dates = sorted(df["date"].unique())
pivot_lines_data = {}
if len(unique_dates) >= 2:
    prev_date = unique_dates[-2]
    prev_day_df = df[df["date"] == prev_date]
    if len(prev_day_df) > 0:
        pdh = round(float(prev_day_df["high"].max()), 2)
        pdl = round(float(prev_day_df["low"].min()), 2)
        pdc = round(float(prev_day_df["close"].iloc[-1]), 2)
        P = round((pdh + pdl + pdc) / 3.0, 2)
        pivot_lines_data = {
            "P": P,
            "R1": round((2.0 * P) - pdl, 2),
            "S1": round((2.0 * P) - pdh, 2),
            "R2": round(P + (pdh - pdl), 2),
            "S2": round(P - (pdh - pdl), 2),
            "R3": round(pdh + 2.0 * (P - pdl), 2),
            "S3": round(pdl - 2.0 * (pdh - P), 2),
            "PDH": pdh,
            "PDL": pdl,
        }

curr = df.iloc[-1]
spot_price = float(live_state.get("ltp", curr["close"]))
live_real_ltp = float(live_state.get("opt_ltp", 0.0))

today_df = df[df["date"] == df["date"].iloc[-1]]
today_orb = today_df[(today_df["dt"].dt.time >= datetime.time(9, 15)) & (today_df["dt"].dt.time <= datetime.time(9, 30))]
curr_orb_h = today_orb["high"].max() if len(today_orb) > 0 else today_df.iloc[0:3]["high"].max()
curr_orb_l = today_orb["low"].min() if len(today_orb) > 0 else today_df.iloc[0:3]["low"].min()

ctrl_cols = st.columns([2.2, 0.9, 6.9])

with ctrl_cols[0]:
    mode_selection = st.selectbox(
        "Budget",
        [
            "Budget (< ₹50)",
            "Budget (< ₹60)",
            "Budget (< ₹70)",
            "Budget (< ₹80)",
            "Budget (< ₹90)",
            "Budget (< ₹100)",
            "ATM (At-The-Money)",
        ],
        index=3,
    )

with ctrl_cols[1]:
    sel_lots = st.number_input("Lots", min_value=1, max_value=100, value=1, step=1)

total_qty = sel_lots * LOT_SIZE_QTY

locked_trade = live_state.get("locked_trade")
active_symbol = locked_trade.get("symbol", f"NIFTY {int(round(spot_price/50)*50)} CE") if locked_trade else f"NIFTY {int(round(spot_price/50)*50)} CE"
active_delta = 0.50

with ctrl_cols[2]:
    st.markdown(
        f"""
        <div style="background:#161a25; border:1px solid #2a2e39; border-radius:4px; padding:3px 8px; display:flex; gap:12px; align-items:center; font-size:11px; height:32px;">
            <div>Contract: <b id="topContractSym" style="color:#00e5ff;">{active_symbol}</b></div>
            <div>LTP: <b id="topOptionLTP" style="color:#ffd600; font-size:13px;">₹{live_real_ltp:.2f}</b></div>
            <div>Mode: <b style="color:#ab47bc;">{mode_selection}</b></div>
            <div>Qty: <b style="color:#089981;">{total_qty}</b> ({sel_lots}L)</div>
            <div>Delta: <b style="color:#00e5ff;">{active_delta:.2f}</b></div>
            <div>Status: <b style="color:#00e5ff;">CLOUD ENGINE ACTIVE</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

candles_data = [
    {
        "time": int(r["time"]),
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
    }
    for _, r in df.iterrows()
]

vol_max = df["calc_vol"].max() if df["calc_vol"].max() > 0 else 1.0
volume_data = [
    {
        "time": int(r["time"]),
        "value": round((float(r["calc_vol"]) / vol_max) * 100.0, 2),
        "color": "rgba(8, 153, 129, 0.55)" if r["close"] >= r["open"] else "rgba(242, 54, 69, 0.55)",
    }
    for _, r in df.iterrows()
]

hud_payload = None
if locked_trade:
    is_ce = locked_trade["type"] == "CE"
    entry_p = locked_trade["entry"]
    trail_p = locked_trade.get("current_sl", locked_trade["init_sl"])
    secured_pts = round(trail_p - entry_p, 1) if is_ce else round(entry_p - trail_p, 1)
    risk_pts = round(abs(entry_p - locked_trade["init_sl"]), 1)

    hud_payload = {
        "is_no_trade": False,
        "call_or_put": "CALL (CE)" if is_ce else "PUT (PE)",
        "reason": "ORB Breakout via Worker",
        "entry": f"{entry_p:.1f}",
        "sl_risk": f"{locked_trade['init_sl']:.1f} (-{risk_pts:.1f} pts)",
        "target1": f"{entry_p + 40:.1f} (+40.0 pts)" if is_ce else f"{entry_p - 40:.1f} (+40.0 pts)",
        "target_mid": f"{entry_p + 70:.1f} (+70.0 pts)" if is_ce else f"{entry_p - 70:.1f} (+70.0 pts)",
        "target_final": f"{entry_p + 110:.1f} (+110.0 pts)" if is_ce else f"{entry_p - 110:.1f} (+110.0 pts)",
        "trailing_sl": f"{trail_p:.1f}",
        "trail_stage": "ACTIVE CLOUD TRADE",
        "pnl_label": "Secured P&L",
        "pts_display": f"{secured_pts:+.1f} pts",
        "inr_display": f"{int(round(secured_pts * active_delta * total_qty)):+d}",
        "pnl_theme": "#089981" if secured_pts >= 0 else "#f23645",
        "raw_risk_pts": risk_pts,
        "raw_target1_pts": 40.0,
        "raw_target_mid_pts": 70.0,
        "raw_target_final_pts": 110.0,
        "raw_secured_pts": secured_pts,
        "theme": "#089981" if is_ce else "#f23645",
    }
else:
    hud_payload = {
        "is_no_trade": True,
        "status": "MONITORING ACTIVE",
        "reason": "Waiting for 09:15-09:30 range breakout",
        "action": "Worker engine listening in cloud",
        "theme": "#787b86",
    }

candles_json = json.dumps(candles_data)
volume_json = json.dumps(volume_data)
vwap_json = json.dumps([{"time": int(r["time"]), "value": round(float(r["vwap"]), 2)} for _, r in df.iterrows()])
ema_json = json.dumps([{"time": int(r["time"]), "value": round(float(r["ema9"]), 2)} for _, r in df.iterrows()])
markers_json = json.dumps([])
pivots_json = json.dumps(pivot_lines_data)
hud_json = json.dumps(hud_payload)

day_open = float(today_df.iloc[0]["open"])
chg = spot_price - day_open
chg_pct = (chg / day_open) * 100
chg_str = f"{chg:+.2f} ({chg_pct:+.2f}%)"
chg_color = "#089981" if chg >= 0 else "#f23645"

# Upstash REST credentials for browser client
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
            touch-action: none; min-width: 330px;
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
        .text-stage {{ color: #00e5ff; font-size: 8.5px; font-weight: bold; }}

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
        .tv-touch-circle {{
            position: absolute; right: 4px; width: 14px; height: 14px;
            background: #ffffff; border: 2px solid #2962ff; border-radius: 50%;
            cursor: ns-resize; touch-action: none; z-index: 68;
        }}
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
                <span>VWAP: <b id="barVWAP" style="color:#ab47bc;">{curr['vwap']:.2f}</b></span>
                <span>EMA: <b id="barEMA" style="color:#2962ff;">{curr['ema9']:.2f}</b></span>
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
    <div id="strategyBox" class="draggable-strategy-box" style="display: none;"></div>
    <div id="activeToolsContainer"></div>

    <script>
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

        const volumeSeries = chart.addHistogramSeries({{
            priceFormat: {{ type: 'volume' }}, priceScaleId: 'vol_scale',
            scaleMargins: {{ top: 0.80, bottom: 0.04 }}
        }});
        chart.priceScale('vol_scale').applyOptions({{ scaleMargins: {{ top: 0.80, bottom: 0.04 }} }});
        volumeSeries.setData({volume_json});

        const vwapSeries = chart.addLineSeries({{ color: '#ab47bc', lineWidth: 2, title: 'VWAP' }});
        vwapSeries.setData({vwap_json});

        const emaSeries = chart.addLineSeries({{ color: '#2962ff', lineWidth: 1, title: '9-EMA' }});
        emaSeries.setData({ema_json});

        candleSeries.createPriceLine({{ price: {curr_orb_h:.2f}, color: '#089981', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'ORB HIGH' }});
        candleSeries.createPriceLine({{ price: {curr_orb_l:.2f}, color: '#f23645', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'ORB LOW' }});

        const pv = {pivots_json};
        if (pv && pv.P) {{
            candleSeries.createPriceLine({{ price: pv.P, color: '#ffd600', lineWidth: 1.5, title: 'PIVOT (P)' }});
            candleSeries.createPriceLine({{ price: pv.R1, color: '#f23645', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'R1' }});
            candleSeries.createPriceLine({{ price: pv.S1, color: '#089981', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'S1' }});
            candleSeries.createPriceLine({{ price: pv.PDH, color: '#ffb300', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: 'PDH' }});
            candleSeries.createPriceLine({{ price: pv.PDL, color: '#fb8c00', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: 'PDL' }});
        }}

        // Render Strategy HUD
        const s = {hud_json};
        const table = document.getElementById('strategyBox');
        if (s) {{
            table.style.display = 'block';
            table.innerHTML = `
                <div class="box-drag-handle">::: STRATEGY HUD :::</div>
                <table>
                    <tr><td class="label-cell">Session Status</td><td class="val-cell" colspan="2"><span class="tag-pill" style="background:#546e7a;">${{s.status || s.call_or_put}}</span></td></tr>
                    <tr><td class="label-cell">Reason</td><td class="val-cell" colspan="2" style="font-size:9px; color:#cfd3dc;">${{s.reason}}</td></tr>
                    <tr><td class="label-cell">Target 1</td><td class="val-cell text-green">${{s.target1 || 'N/A'}}</td></tr>
                    <tr><td class="label-cell">Trailing SL</td><td class="val-cell text-trail">${{s.trailing_sl || 'N/A'}}</td></tr>
                </table>
            `;
        }}

        function makeDraggable(elem) {{
            let isDragging = false, startX, startY, initLeft, initTop;
            function onStart(e) {{
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
        makeDraggable(table);

        // Fetch Live State From Upstash (Polls Every 1.5s Without Full Page Reloads)
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
