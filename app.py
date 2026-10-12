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

raw_candles = live_state["candles"]
df = pd.DataFrame(raw_candles)
df["dt"] = pd.to_datetime(df["time"], unit="s").dt.tz_localize("UTC").dt.tz_convert(IST)
df["date"] = df["dt"].dt.date

# Technical Indicators
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

# Markers and History directly supplied by Worker
markers = live_state.get("markers", [])
historical_trade_cards = live_state.get("history_cards", {})

# Calculate Today's ORB Lines and Fibonacci Extensions
latest_session_date = df["date"].iloc[-1]
today_df = df[df["date"] == latest_session_date]
today_orb = today_df[(today_df["dt"].dt.time >= datetime.time(9, 15)) & (today_df["dt"].dt.time <= datetime.time(9, 30))]

curr_orb_h = float(today_orb["high"].max()) if len(today_orb) > 0 else float(today_df.iloc[0:3]["high"].max())
curr_orb_l = float(today_orb["low"].min()) if len(today_orb) > 0 else float(today_df.iloc[0:3]["low"].min())
orb_range_val = max(10.0, curr_orb_h - curr_orb_l)

# Check if there is an active trade in worker or a detected trade today
locked_trade = live_state.get("locked_trade")

# Search history cards for today's trade if no live trade is active
today_card = None
for t_sec, card in historical_trade_cards.items():
    if card.get("date") == str(latest_session_date):
        today_card = card

if locked_trade:
    t_type = locked_trade.get("type", "CE")
    entry_p = float(locked_trade.get("entry", spot_price))
    sl_p = float(locked_trade.get("init_sl", entry_p - 20.0))
    trail_p = float(locked_trade.get("current_sl", sl_p))
    t1_p = float(locked_trade.get("tp1", entry_p + 40.0 if t_type == "CE" else entry_p - 40.0))
    tmid_p = float(locked_trade.get("tp_mid", entry_p + 70.0 if t_type == "CE" else entry_p - 70.0))
    tfinal_p = float(locked_trade.get("tp_final", entry_p + 110.0 if t_type == "CE" else entry_p - 110.0))
    strike_val = locked_trade.get("strike", int(round(entry_p / 50.0) * 50))
    active_symbol = locked_trade.get("symbol", f"NIFTY {strike_val} {t_type}")
    active_delta = float(locked_trade.get("delta", 0.50))
elif today_card:
    t_type = today_card["type"]
    entry_p = float(today_card["entry"])
    sl_p = float(today_card["sl"])
    trail_p = sl_p
    t1_p = float(today_card["target1"])
    tmid_p = float(today_card["target_mid"])
    tfinal_p = float(today_card["target_final"])
    strike_val = int(round(entry_p / 50.0) * 50)
    active_symbol = f"NIFTY {strike_val} {t_type}"
    active_delta = 0.50
else:
    # Awaiting ORB breakout today
    t_type = "CE" if spot_price >= curr["vwap"] else "PE"
    entry_p = curr_orb_h if t_type == "CE" else curr_orb_l
    sl_p = round(curr_orb_l - 5.0 if t_type == "CE" else curr_orb_h + 5.0, 1)
    trail_p = sl_p
    t1_p = round(curr_orb_h + (1.000 * orb_range_val) if t_type == "CE" else curr_orb_l - (1.000 * orb_range_val), 1)
    tmid_p = round(curr_orb_h + (1.618 * orb_range_val) if t_type == "CE" else curr_orb_l - (1.618 * orb_range_val), 1)
    tfinal_p = round(curr_orb_h + (2.618 * orb_range_val) if t_type == "CE" else curr_orb_l - (2.618 * orb_range_val), 1)
    strike_val = int(round(spot_price / 50.0) * 50)
    active_symbol = f"NIFTY {strike_val} {t_type}"
    active_delta = 0.50

if live_real_ltp <= 0.0:
    live_real_ltp = 75.0

risk_pts = round(abs(entry_p - sl_p), 1)
t1_pts = round(abs(t1_p - entry_p), 1)
tmid_pts = round(abs(tmid_p - entry_p), 1)
tfinal_pts = round(abs(tfinal_p - entry_p), 1)
secured_pts = round(trail_p - entry_p, 1) if t_type == "CE" else round(entry_p - trail_p, 1)

# Fibonacci Extension Lines for ORB Breakout
if t_type == "CE":
    fib_100 = round(curr_orb_h + (1.000 * orb_range_val), 1)
    fib_161 = round(curr_orb_h + (1.618 * orb_range_val), 1)
    fib_261 = round(curr_orb_h + (2.618 * orb_range_val), 1)
else:
    fib_100 = round(curr_orb_l - (1.000 * orb_range_val), 1)
    fib_161 = round(curr_orb_l - (1.618 * orb_range_val), 1)
    fib_261 = round(curr_orb_l - (2.618 * orb_range_val), 1)

fibs_dict = {"fib_100": fib_100, "fib_161": fib_161, "fib_261": fib_261}

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

candles_json = json.dumps(candles_data)
volume_json = json.dumps(volume_data)
vwap_json = json.dumps([{"time": int(r["time"]), "value": round(float(r["vwap"]), 2)} for _, r in df.iterrows()])
ema_json = json.dumps([{"time": int(r["time"]), "value": round(float(r["ema9"]), 2)} for _, r in df.iterrows()])
markers_json = json.dumps(markers)
history_cards_json = json.dumps(historical_trade_cards)
pivots_json = json.dumps(pivot_lines_data)
fibs_json = json.dumps(fibs_dict)

day_open = float(today_df.iloc[0]["open"])
chg = spot_price - day_open
chg_pct = (chg / day_open) * 100
chg_str = f"{chg:+.2f} ({chg_pct:+.2f}%)"
chg_color = "#089981" if chg >= 0 else "#f23645"

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
                    <td class="val-cell"><b id="hudEntrySpot">{entry_p:.1f}</b></td>
                    <td class="opt-cell"><b id="hudEntryOpt">₹{live_real_ltp:.2f}</b></td>
                </tr>
                <tr>
                    <td class="label-cell">SL (risk pts)</td>
                    <td class="val-cell text-red"><span id="hudSLSpot">{sl_p:.1f} (-{risk_pts:.1f} pts)</span></td>
                    <td class="opt-cell text-red"><span id="hudSLOpt">₹{max(0.5, round(live_real_ltp - (risk_pts * active_delta), 1)):.2f} (-₹{int(round(risk_pts * active_delta * total_qty))})</span></td>
                </tr>
                <tr>
                    <td class="label-cell">Target 1</td>
                    <td class="val-cell text-green"><span id="hudT1Spot">{t1_p:.1f} (+{t1_pts:.1f} pts)</span></td>
                    <td class="opt-cell text-green"><span id="hudT1Opt">₹{round(live_real_ltp + (t1_pts * active_delta), 1):.2f} (+₹{int(round(t1_pts * active_delta * total_qty))})</span></td>
                </tr>
                <tr>
                    <td class="label-cell">Target Mid</td>
                    <td class="val-cell text-green"><span id="hudTMidSpot">{tmid_p:.1f} (+{tmid_pts:.1f} pts)</span></td>
                    <td class="opt-cell text-green"><span id="hudTMidOpt">₹{round(live_real_ltp + (tmid_pts * active_delta), 1):.2f}</span></td>
                </tr>
                <tr>
                    <td class="label-cell">Target Final</td>
                    <td class="val-cell text-green"><b id="hudTFinalSpot">{tfinal_p:.1f} (+{tfinal_pts:.1f} pts)</b></td>
                    <td class="opt-cell text-green"><b id="hudTFinalOpt">₹{round(live_real_ltp + (tfinal_pts * active_delta), 1):.2f} (+₹{int(round(tfinal_pts * active_delta * total_qty))})</b></td>
                </tr>
                <tr>
                    <td class="label-cell">Trailing SL</td>
                    <td class="val-cell text-trail"><span id="hudTrailSpot">{trail_p:.1f}</span></td>
                    <td class="opt-cell text-trail"><span id="hudTrailOpt">₹{max(0.5, round(live_real_ltp + (secured_pts * active_delta), 1)):.2f}</span></td>
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

        // Set Historical Breakout Markers straight from Worker
        candleSeries.setMarkers({markers_json});

        // Current ORB Lines
        candleSeries.createPriceLine({{ price: {curr_orb_h:.2f}, color: '#089981', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'ORB HIGH' }});
        candleSeries.createPriceLine({{ price: {curr_orb_l:.2f}, color: '#f23645', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'ORB LOW' }});

        // ORB Fibonacci Extensions
        const fibs = {fibs_json};
        if (fibs && fibs.fib_100) {{
            candleSeries.createPriceLine({{ price: fibs.fib_100, color: '#ffd600', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'EXT 100%' }});
            candleSeries.createPriceLine({{ price: fibs.fib_161, color: '#00e5ff', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'EXT 161.8%' }});
            candleSeries.createPriceLine({{ price: fibs.fib_261, color: '#ff6d00', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Solid, title: 'EXT 261.8% (MAX)' }});
        }}

        // Floor Pivots
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
                    if (stData.locked_trade) {{
                        const lt = stData.locked_trade;
                        const isCE = lt.type === 'CE';
                        const spotLtp = stData.ltp || lt.entry;
                        const optLtp = stData.opt_ltp || lt.opt_entry_ltp || 75.0;
                        liveDelta = lt.delta || 0.50;

                        document.getElementById('topDeltaVal').innerText = liveDelta.toFixed(2);
                        document.getElementById('hudOptionHead').innerText = "Option (" + lt.symbol + ")";
                        document.getElementById('topContractSym').innerText = lt.symbol;
                        document.getElementById('hudTypePill').innerText = lt.type;
                        document.getElementById('hudTypeOptPill').innerText = lt.type;
                        document.getElementById('hudTypePill').style.background = isCE ? '#089981' : '#f23645';
                        document.getElementById('hudTypeOptPill').style.background = isCE ? '#089981' : '#f23645';

                        document.getElementById('hudEntrySpot').innerText = lt.entry.toFixed(1);
                        document.getElementById('hudEntryOpt').innerText = "₹" + (lt.opt_entry_ltp ? lt.opt_entry_ltp.toFixed(2) : optLtp.toFixed(2));

                        const rPts = Math.abs(lt.entry - lt.init_sl);
                        document.getElementById('hudSLSpot').innerText = lt.init_sl.toFixed(1) + " (-" + rPts.toFixed(1) + " pts)";
                        document.getElementById('hudSLOpt').innerText = "₹" + Math.max(0.5, optLtp - (rPts * liveDelta)).toFixed(2) + " (-₹" + Math.round(rPts * liveDelta * activeQty) + ")";

                        const t1_val = lt.tp1;
                        const t1Pts = Math.abs(t1_val - lt.entry);
                        document.getElementById('hudT1Spot').innerText = t1_val.toFixed(1) + " (+" + t1Pts.toFixed(1) + " pts)";
                        document.getElementById('hudT1Opt').innerText = "₹" + (optLtp + (t1Pts * liveDelta)).toFixed(2) + " (+₹" + Math.round(t1Pts * liveDelta * activeQty) + ")";

                        const tMid_val = lt.tp_mid;
                        const tMidPts = Math.abs(tMid_val - lt.entry);
                        document.getElementById('hudTMidSpot').innerText = tMid_val.toFixed(1) + " (+" + tMidPts.toFixed(1) + " pts)";
                        document.getElementById('hudTMidOpt').innerText = "₹" + (optLtp + (tMidPts * liveDelta)).toFixed(2);

                        const tFinal_val = lt.tp_final;
                        const tFinalPts = Math.abs(tFinal_val - lt.entry);
                        document.getElementById('hudTFinalSpot').innerText = tFinal_val.toFixed(1) + " (+" + tFinalPts.toFixed(1) + " pts)";
                        document.getElementById('hudTFinalOpt').innerText = "₹" + (optLtp + (tFinalPts * liveDelta)).toFixed(2) + " (+₹" + Math.round(tFinalPts * liveDelta * activeQty) + ")";

                        const curSL = lt.current_sl || lt.init_sl;
                        const secPts = isCE ? (curSL - lt.entry) : (lt.entry - curSL);
                        document.getElementById('hudTrailSpot').innerText = curSL.toFixed(1);
                        document.getElementById('hudTrailOpt').innerText = "₹" + Math.max(0.5, optLtp + (secPts * liveDelta)).toFixed(2);

                        const pnlSpotElem = document.getElementById('hudPnLSpot');
                        const pnlOptElem = document.getElementById('hudPnLOpt');
                        const sign = secPts >= 0 ? "+" : "";
                        pnlSpotElem.innerText = sign + secPts.toFixed(1) + " pts";
                        pnlOptElem.innerText = "₹" + sign + Math.round(secPts * liveDelta * activeQty);
                        pnlSpotElem.parentElement.style.color = secPts >= 0 ? '#089981' : '#f23645';
                        pnlOptElem.parentElement.style.color = secPts >= 0 ? '#089981' : '#f23645';
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
