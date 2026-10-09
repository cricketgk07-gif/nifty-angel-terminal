import datetime
import json
import pandas as pd
import numpy as np
import pyotp
import requests
from SmartApi import SmartConnect
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
        padding: 0 !important;
        margin: 0 !important;
        max-width: 100vw !important;
        height: 100vh !important;
        overflow: hidden !important;
    }
    iframe {
        border: none !important;
        width: 100vw !important;
        height: 100vh !important;
        display: block !important;
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

# Secrets
API_KEY = st.secrets.get("API_KEY", "")
CLIENT_CODE = st.secrets.get("CLIENT_CODE", "")
PIN = st.secrets.get("PIN", "")
TOTP_SECRET = st.secrets.get("TOTP_SECRET", "")
INDEX_TOKEN = "99926000"


def get_authenticated_api():
    try:
        api = SmartConnect(API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        sess = api.generateSession(CLIENT_CODE, PIN, totp)
        if sess and sess.get("status"):
            return api
        return None
    except Exception:
        return None


api = get_authenticated_api()
if not api:
    st.error("Authentication failed. Please verify credentials in Secrets.")
    st.stop()

# Timeframe Query Parameter
params = st.query_params
current_interval = params.get("interval", "5m")

timeframe_config = {
    "1m": ("ONE_MINUTE", 5),
    "3m": ("THREE_MINUTE", 10),
    "5m": ("FIVE_MINUTE", 30),
    "15m": ("FIFTEEN_MINUTE", 60),
    "30m": ("THIRTY_MINUTE", 90),
    "1h": ("ONE_HOUR", 180),
    "1D": ("ONE_DAY", 3650),
}

if current_interval not in timeframe_config:
    current_interval = "5m"

api_interval, lookback_days = timeframe_config[current_interval]


def fetch_nifty_candles(interval_code, days_back):
    now = datetime.datetime.now()
    from_date = (now - datetime.timedelta(days=days_back)).strftime(
        "%Y-%m-%d 09:15"
    )
    to_date = now.strftime("%Y-%m-%d %H:%M")

    try:
        resp = api.getCandleData(
            {
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": interval_code,
                "fromdate": from_date,
                "todate": to_date,
            }
        )
    except Exception:
        return None

    if not isinstance(resp, dict) or not resp.get("status") or not resp.get("data"):
        return None

    df = pd.DataFrame(
        resp["data"],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["dt"] = pd.to_datetime(df["timestamp"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    # Clean zero ticks
    df = df[(df["open"] > 1000) & (df["high"] > 1000) & (df["low"] > 1000) & (df["close"] > 1000)].copy()

    # NSE Trading Hours
    if interval_code != "ONE_DAY":
        df = df[
            (df["dt"].dt.time >= datetime.time(9, 15))
            & (df["dt"].dt.time <= datetime.time(15, 30))
        ].copy()

    if len(df) == 0:
        return None

    t_clean = df["dt"].dt.tz_localize(None)
    df["time"] = (
        (t_clean - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)
    ).astype(int)
    df["date"] = df["dt"].dt.date

    # Dynamic Volume
    candle_spread = (df["high"] - df["low"]) + (df["close"] - df["open"]).abs()
    raw_vol = df["volume"].apply(lambda v: float(v) if pd.notnull(v) and v > 0 else 0.0)
    df["calc_vol"] = raw_vol.where(raw_vol > 0, candle_spread * 1250.0 + 500.0)

    # Indicators: Day-Reset VWAP
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vol_mult"] = df["tp"] * df["calc_vol"]
    df["cum_vol"] = df.groupby("date")["calc_vol"].cumsum()
    df["cum_vp"] = df.groupby("date")["vol_mult"].cumsum()
    df["vwap"] = df["cum_vp"] / df["cum_vol"]
    df["vwap"] = df["vwap"].fillna(df["tp"])
    df.loc[df["vwap"] < 1000, "vwap"] = df["tp"]

    # 9-EMA
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()

    # 14-period ATR
    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift(1)).abs()
    low_cp = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    df["atr"] = tr.rolling(window=14, min_periods=1).mean()
    df["atr"] = df["atr"].fillna(15.0)

    return df


df = fetch_nifty_candles(api_interval, lookback_days)
if df is None or len(df) == 0:
    st.info("Market feed is initializing...")
    st.stop()

# --- Two-Phase Adaptive Trailing Engine ---
markers = []
latest_trade_for_hud = None

grouped = df.groupby("date")

for session_date, day_df in grouped:
    orb_window = day_df[day_df["dt"].dt.time <= datetime.time(9, 30)]
    if len(orb_window) == 0:
        continue

    day_orb_h = orb_window["high"].max()
    day_orb_l = orb_window["low"].min()

    session_trade = None
    trade_executed_today = False

    for idx in range(len(day_df)):
        row = day_df.iloc[idx]
        t = row["dt"].time()
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        ema = row["ema9"]
        vwap_val = row["vwap"]
        atr_val = row["atr"]

        if session_trade and not session_trade["closed"]:
            t_type = session_trade["type"]
            entry = session_trade["entry"]
            base_atr = session_trade["entry_atr"]
            tp1_target = session_trade["tp1"]

            # Pullback Room Buffer (Wide before TP1) vs Tight Room Buffer (Nearby/After TP1)
            wide_buffer = min(max(round(atr_val * 1.1, 1), 8.0), 14.0)
            tight_buffer = min(max(round(atr_val * 0.4, 1), 3.5), 5.5)

            if t_type == "CE":
                # Check if price is nearby TP1 (within 85% of 3-ATR distance)
                nearby_threshold = entry + (2.5 * base_atr)
                is_nearby_tp1 = h >= nearby_threshold
                has_crossed_tp1 = h >= tp1_target

                if has_crossed_tp1:
                    session_trade["tp1_hit"] = True
                    session_trade["trail_stage"] = "TP1 CROSSED (Tightened Buffer)"
                elif is_nearby_tp1 and not session_trade["tp1_hit"]:
                    session_trade["trail_stage"] = "NEARBY TP1 (Tightening Trail)"

                # Breakeven Lock once +1.5 ATR is achieved
                if h >= (entry + (1.5 * base_atr)) and not session_trade["be_locked"]:
                    session_trade["be_locked"] = True
                    session_trade["current_sl"] = max(session_trade["current_sl"], round(entry + 3.0, 1))

                # Adaptive Trailing Calculation
                if session_trade["tp1_hit"] or is_nearby_tp1:
                    # Phase 2: Tight room around EMA and preceding candle support
                    tight_trail = max(round(ema - tight_buffer, 1), round(l - 1.5, 1))
                    min_locked = round(entry + (1.8 * base_atr), 1) if session_trade["tp1_hit"] else round(entry + 3.0, 1)
                    target_sl = max(tight_trail, min_locked)
                    if target_sl > session_trade["current_sl"]:
                        session_trade["current_sl"] = target_sl
                else:
                    # Phase 1: Wide structural pullback room
                    wide_trail = round(ema - wide_buffer, 1)
                    if session_trade["be_locked"]:
                        session_trade["current_sl"] = max(session_trade["current_sl"], wide_trail)
                    elif wide_trail > session_trade["current_sl"]:
                        session_trade["current_sl"] = wide_trail

                # Dynamic Final Target
                if h > session_trade["tp_final"]:
                    session_trade["tp_final"] = round(h + (session_trade["risk"] * 1.0), 1)

                # Breach Check
                if l <= session_trade["current_sl"] or (c < ema and c < o and c < (day_orb_h - 3.0)):
                    exit_p = min(c, session_trade["current_sl"])
                    pts = round(exit_p - entry, 1)
                    markers.append(
                        {
                            "time": int(row["time"]),
                            "position": "aboveBar",
                            "color": "#f23645",
                            "shape": "arrowDown",
                            "text": f"EXIT SL @ {exit_p:.1f} ({pts:+.1f})",
                        }
                    )
                    session_trade["closed"] = True
                    session_trade["exit_price"] = exit_p
                    session_trade["exit_pts"] = pts
                    latest_trade_for_hud = session_trade.copy()

            elif t_type == "PE":
                nearby_threshold = entry - (2.5 * base_atr)
                is_nearby_tp1 = l <= nearby_threshold
                has_crossed_tp1 = l <= tp1_target

                if has_crossed_tp1:
                    session_trade["tp1_hit"] = True
                    session_trade["trail_stage"] = "TP1 CROSSED (Tightened Buffer)"
                elif is_nearby_tp1 and not session_trade["tp1_hit"]:
                    session_trade["trail_stage"] = "NEARBY TP1 (Tightening Trail)"

                # Breakeven Lock once +1.5 ATR is achieved
                if l <= (entry - (1.5 * base_atr)) and not session_trade["be_locked"]:
                    session_trade["be_locked"] = True
                    session_trade["current_sl"] = min(session_trade["current_sl"], round(entry - 3.0, 1))

                # Adaptive Trailing Calculation
                if session_trade["tp1_hit"] or is_nearby_tp1:
                    tight_trail = min(round(ema + tight_buffer, 1), round(h + 1.5, 1))
                    min_locked = round(entry - (1.8 * base_atr), 1) if session_trade["tp1_hit"] else round(entry - 3.0, 1)
                    target_sl = min(tight_trail, min_locked)
                    if target_sl < session_trade["current_sl"]:
                        session_trade["current_sl"] = target_sl
                else:
                    wide_trail = round(ema + wide_buffer, 1)
                    if session_trade["be_locked"]:
                        session_trade["current_sl"] = min(session_trade["current_sl"], wide_trail)
                    elif wide_trail < session_trade["current_sl"]:
                        session_trade["current_sl"] = wide_trail

                # Dynamic Final Target
                if l < session_trade["tp_final"]:
                    session_trade["tp_final"] = round(l - (session_trade["risk"] * 1.0), 1)

                # Breach Check
                if h >= session_trade["current_sl"] or (c > ema and c > o and c > (day_orb_l + 3.0)):
                    exit_p = max(c, session_trade["current_sl"])
                    pts = round(entry - exit_p, 1)
                    markers.append(
                        {
                            "time": int(row["time"]),
                            "position": "belowBar",
                            "color": "#00bfa5",
                            "shape": "arrowUp",
                            "text": f"EXIT SL @ {exit_p:.1f} ({pts:+.1f})",
                        }
                    )
                    session_trade["closed"] = True
                    session_trade["exit_price"] = exit_p
                    session_trade["exit_pts"] = pts
                    latest_trade_for_hud = session_trade.copy()

        # Breakout Entry after 09:30 AM
        if t >= datetime.time(9, 30) and not trade_executed_today:
            initial_buf = min(max(round(atr_val * 0.6, 1), 6.0), 9.0)
            if c > day_orb_h and c > vwap_val and c > ema:
                init_sl = round(day_orb_h - initial_buf, 1)
                risk = round(c - init_sl, 1)
                tp1 = round(c + (atr_val * 3.0), 1)
                tp_final = round(c + (risk * 3.0), 1)
                session_trade = {
                    "call_or_put": "CALL (CE)",
                    "type": "CE",
                    "entry": round(c, 1),
                    "entry_atr": atr_val,
                    "init_sl": init_sl,
                    "current_sl": init_sl,
                    "tp1": tp1,
                    "tp_final": tp_final,
                    "be_locked": False,
                    "tp1_hit": False,
                    "trail_stage": "PULLBACK ROOM (Wide Trail)",
                    "risk": risk,
                    "closed": False,
                    "reason": "Close > ORB High & VWAP",
                }
                markers.append(
                    {
                        "time": int(row["time"]),
                        "position": "belowBar",
                        "color": "#00bfa5",
                        "shape": "arrowUp",
                        "text": f"BUY CE @ {c:.1f}",
                    }
                )
                trade_executed_today = True
                latest_trade_for_hud = session_trade.copy()

            elif c < day_orb_l and c < vwap_val and c < ema:
                init_sl = round(day_orb_l + initial_buf, 1)
                risk = round(init_sl - c, 1)
                tp1 = round(c - (atr_val * 3.0), 1)
                tp_final = round(c - (risk * 3.0), 1)
                session_trade = {
                    "call_or_put": "PUT (PE)",
                    "type": "PE",
                    "entry": round(c, 1),
                    "entry_atr": atr_val,
                    "init_sl": init_sl,
                    "current_sl": init_sl,
                    "tp1": tp1,
                    "tp_final": tp_final,
                    "be_locked": False,
                    "tp1_hit": False,
                    "trail_stage": "PULLBACK ROOM (Wide Trail)",
                    "risk": risk,
                    "closed": False,
                    "reason": "Close < ORB Low & VWAP",
                }
                markers.append(
                    {
                        "time": int(row["time"]),
                        "position": "aboveBar",
                        "color": "#f23645",
                        "shape": "arrowDown",
                        "text": f"BUY PE @ {c:.1f}",
                    }
                )
                trade_executed_today = True
                latest_trade_for_hud = session_trade.copy()

today_date = df["date"].iloc[-1]
today_df = df[df["date"] == today_date]
today_orb = today_df[today_df["dt"].dt.time <= datetime.time(9, 30)]
curr_orb_h = (
    today_orb["high"].max()
    if len(today_orb) > 0
    else today_df.iloc[0:3]["high"].max()
)
curr_orb_l = (
    today_orb["low"].min()
    if len(today_orb) > 0
    else today_df.iloc[0:3]["low"].min()
)
curr = df.iloc[-1]

candles_data = []
volume_data = []
vol_max = df["calc_vol"].max() if df["calc_vol"].max() > 0 else 1.0

for _, r in df.iterrows():
    candles_data.append(
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
    )
    norm_v = (float(r["calc_vol"]) / vol_max) * 100.0
    v_color = (
        "rgba(8, 153, 129, 0.55)"
        if r["close"] >= r["open"]
        else "rgba(242, 54, 69, 0.55)"
    )
    volume_data.append(
        {
            "time": int(r["time"]),
            "value": round(norm_v, 2),
            "color": v_color,
        }
    )

candles_json = json.dumps(candles_data)
volume_json = json.dumps(volume_data)

vwap_json = json.dumps(
    [
        {"time": int(r["time"]), "value": round(float(r["vwap"]), 2)}
        for _, r in df.iterrows()
    ]
)
ema_json = json.dumps(
    [
        {"time": int(r["time"]), "value": round(float(r["ema9"]), 2)}
        for _, r in df.iterrows()
    ]
)
markers_json = json.dumps(markers)

# HUD Table Payload
hud_payload = None
if latest_trade_for_hud:
    is_ce = latest_trade_for_hud["type"] == "CE"
    entry_p = latest_trade_for_hud["entry"]
    trail_p = latest_trade_for_hud["current_sl"]
    secured_pts = round(trail_p - entry_p, 1) if is_ce else round(entry_p - trail_p, 1)
    risk_pts = latest_trade_for_hud["risk"]
    target1_pts = round(abs(latest_trade_for_hud["tp1"] - entry_p), 1)
    target_final_pts = round(abs(latest_trade_for_hud["tp_final"] - entry_p), 1)

    hud_payload = {
        "call_or_put": latest_trade_for_hud["call_or_put"],
        "reason": latest_trade_for_hud["reason"],
        "entry": f"{entry_p:.1f}",
        "sl_risk": f"{latest_trade_for_hud['init_sl']:.1f} (-{risk_pts:.1f} pts)",
        "target1": f"{latest_trade_for_hud['tp1']:.1f} (+{target1_pts:.1f} pts)",
        "target_final": f"{latest_trade_for_hud['tp_final']:.1f} (+{target_final_pts:.1f} pts)",
        "trailing_sl": f"{trail_p:.1f}",
        "trail_stage": latest_trade_for_hud["trail_stage"],
        "secured_pts": f"{secured_pts:+.1f} pts",
        "theme": "#089981" if is_ce else "#f23645",
    }

hud_json = json.dumps(hud_payload)

day_open = today_df.iloc[0]["open"]
chg = curr["close"] - day_open
chg_pct = (chg / day_open) * 100
chg_str = f"{chg:+.2f} ({chg_pct:+.2f}%)"
chg_color = "#089981" if chg >= 0 else "#f23645"

# --- Responsive Web Terminal ---
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
        }}
        #chartArea {{
            width: 100vw; height: calc(100vh - 35px);
            position: absolute; top: 0; left: 0;
        }}

        .fixed-top-box {{
            position: absolute; top: 6px; left: 8px; z-index: 50;
            display: flex; flex-direction: column; gap: 4px; pointer-events: none;
            max-width: calc(100vw - 80px);
        }}
        .top-row-1 {{
            display: flex; align-items: center; gap: 8px; pointer-events: auto; flex-wrap: wrap;
        }}
        .sym-group {{
            display: flex; align-items: center; gap: 5px;
        }}
        .badge {{
            background: #2962ff; color: #fff; font-size: 11px; padding: 2px 5px;
            border-radius: 3px; font-weight: 700;
        }}
        .sym-title {{ font-size: 13px; font-weight: 700; color: #d1d4dc; }}
        .sym-price {{ font-size: 12px; font-weight: 700; }}

        .tf-bar {{
            display: flex; gap: 2px; background: rgba(30, 34, 45, 0.95);
            padding: 2px 4px; border-radius: 4px; border: 1px solid #2a2e39;
        }}
        .tf-btn {{
            background: transparent; border: none; color: #787b86;
            font-size: 10px; font-weight: 600; padding: 2px 5px;
            border-radius: 2px; cursor: pointer;
        }}
        .tf-btn.active {{
            background: #2a2e39; color: #d1d4dc; font-weight: 700;
        }}

        .dynamic-ohlc-row {{
            font-size: 10px; color: #787b86; display: flex; gap: 6px;
            background: rgba(11, 14, 20, 0.92); padding: 2px 6px;
            border-radius: 3px; border: 1px solid rgba(42, 46, 57, 0.4);
            font-family: monospace; width: fit-content;
        }}
        .dynamic-ohlc-row b {{ color: #d1d4dc; }}

        .fixed-strategy-box {{
            position: absolute; bottom: 48px; right: 65px; z-index: 50;
            background: rgba(19, 23, 34, 0.96); border: 1px solid #2a2e39;
            border-radius: 5px; font-size: 9.5px; color: #d1d4dc; overflow: hidden;
            box-shadow: 0 4px 15px rgba(0,0,0,0.85);
        }}
        .fixed-strategy-box table {{ border-collapse: collapse; }}
        .fixed-strategy-box td {{ padding: 3px 8px; border-bottom: 1px solid #222631; white-space: nowrap; }}
        .fixed-strategy-box tr:last-child td {{ border-bottom: none; }}
        .label-cell {{ color: #787b86; font-weight: 500; }}
        .val-cell {{ font-weight: 700; color: #ffffff; text-align: right; }}
        .tag-pill {{ color: #fff; font-weight: bold; border-radius: 2px; padding: 1px 5px; text-align: center; font-size: 9px; }}
        .text-red {{ color: #f23645; }}
        .text-green {{ color: #089981; }}
        .text-trail {{ color: #2962ff; font-weight: bold; }}
        .text-stage {{ color: #ffb300; font-size: 8.5px; font-weight: bold; }}
    </style>
</head>
<body>
    <div class="fixed-top-box">
        <div class="top-row-1">
            <div class="sym-group">
                <span class="badge">50</span>
                <span class="sym-title">NIFTY</span>
                <span class="sym-price" style="color: {chg_color};">{curr['close']:.2f} <span style="font-size: 10px;">{chg_str}</span></span>
            </div>

            <div class="tf-bar">
                <button class="tf-btn {'active' if current_interval=='1m' else ''}" onclick="changeTF('1m')">1m</button>
                <button class="tf-btn {'active' if current_interval=='3m' else ''}" onclick="changeTF('3m')">3m</button>
                <button class="tf-btn {'active' if current_interval=='5m' else ''}" onclick="changeTF('5m')">5m</button>
                <button class="tf-btn {'active' if current_interval=='15m' else ''}" onclick="changeTF('15m')">15m</button>
                <button class="tf-btn {'active' if current_interval=='30m' else ''}" onclick="changeTF('30m')">30m</button>
                <button class="tf-btn {'active' if current_interval=='1h' else ''}" onclick="changeTF('1h')">1h</button>
                <button class="tf-btn {'active' if current_interval=='1D' else ''}" onclick="changeTF('1D')">1D</button>
            </div>
        </div>

        <div id="ohlcRow" class="dynamic-ohlc-row">
            <span>O: <b id="barO">{curr['open']:.2f}</b></span>
            <span>H: <b id="barH">{curr['high']:.2f}</b></span>
            <span>L: <b id="barL">{curr['low']:.2f}</b></span>
            <span>C: <b id="barC">{curr['close']:.2f}</b></span>
            <span>VWAP: <b id="barVWAP" style="color:#ab47bc;">{curr['vwap']:.2f}</b></span>
            <span>EMA: <b id="barEMA" style="color:#2962ff;">{curr['ema9']:.2f}</b></span>
        </div>
    </div>

    <!-- Strategy Table Box -->
    <div id="strategyBox" class="fixed-strategy-box" style="display: none;"></div>

    <!-- Main Chart Canvas Area -->
    <div id="chartArea"></div>

    <script>
        const container = document.getElementById('chartArea');
        const chart = LightweightCharts.createChart(container, {{
            width: window.innerWidth,
            height: window.innerHeight - 35,
            layout: {{
                background: {{ color: '#0b0e14' }},
                textColor: '#787b86',
                fontSize: 10,
            }},
            grid: {{
                vertLines: {{ color: '#161a25' }},
                horzLines: {{ color: '#161a25' }}
            }},
            crosshair: {{
                mode: LightweightCharts.CrosshairMode.Normal,
                vertLine: {{ color: '#758696', width: 1, style: 3 }},
                horzLine: {{ color: '#758696', width: 1, style: 3 }}
            }},
            rightPriceScale: {{
                borderColor: '#2a2e39',
                autoScale: true,
                scaleMargins: {{ top: 0.12, bottom: 0.20 }},
                alignLabels: true,
                entireTextOnly: true
            }},
            timeScale: {{
                borderColor: '#2a2e39',
                timeVisible: true,
                secondsVisible: false,
                rightOffset: 8
            }},
            localization: {{
                priceFormatter: p => p.toFixed(2)
            }}
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#089981',
            downColor: '#f23645',
            borderUpColor: '#089981',
            borderDownColor: '#f23645',
            wickUpColor: '#089981',
            wickDownColor: '#f23645',
            priceFormat: {{ type: 'price', precision: 2, minMove: 0.05 }}
        }});
        candleSeries.setData({candles_json});

        const volumeSeries = chart.addHistogramSeries({{
            priceFormat: {{ type: 'volume' }},
            priceScaleId: 'vol_scale',
            scaleMargins: {{
                top: 0.82,
                bottom: 0.02
            }}
        }});
        chart.priceScale('vol_scale').applyOptions({{
            scaleMargins: {{ top: 0.82, bottom: 0.02 }}
        }});
        volumeSeries.setData({volume_json});

        const vwapSeries = chart.addLineSeries({{
            color: '#ab47bc',
            lineWidth: 2,
            title: 'VWAP',
            priceFormat: {{ type: 'price', precision: 2, minMove: 0.05 }}
        }});
        vwapSeries.setData({vwap_json});

        const emaSeries = chart.addLineSeries({{
            color: '#2962ff',
            lineWidth: 1,
            title: '9-EMA',
            priceFormat: {{ type: 'price', precision: 2, minMove: 0.05 }}
        }});
        emaSeries.setData({ema_json});

        candleSeries.setMarkers({markers_json});

        candleSeries.createPriceLine({{
            price: {curr_orb_h:.2f},
            color: '#089981',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'ORB HIGH'
        }});

        candleSeries.createPriceLine({{
            price: {curr_orb_l:.2f},
            color: '#f23645',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'ORB LOW'
        }});

        const s = {hud_json};
        if (s) {{
            const table = document.getElementById('strategyBox');
            table.style.display = 'block';
            table.innerHTML = `
                <table>
                    <tr><td class="label-cell">Call or Put</td><td class="val-cell"><span class="tag-pill" style="background:${{s.theme}}">${{s.call_or_put}}</span></td></tr>
                    <tr><td class="label-cell">Reason for trade</td><td class="val-cell" style="font-size:9px; color:#cfd3dc;">${{s.reason}}</td></tr>
                    <tr><td class="label-cell">Entry</td><td class="val-cell"><b>${{s.entry}}</b></td></tr>
                    <tr><td class="label-cell">SL (risk pts)</td><td class="val-cell text-red">${{s.sl_risk}}</td></tr>
                    <tr><td class="label-cell">Target 1 (3 ATR)</td><td class="val-cell text-green">${{s.target1}}</td></tr>
                    <tr><td class="label-cell">Target Final</td><td class="val-cell text-green">${{s.target_final}}</td></tr>
                    <tr><td class="label-cell">Trailing SL</td><td class="val-cell text-trail">${{s.trailing_sl}}</td></tr>
                    <tr><td class="label-cell">Trail Mode</td><td class="val-cell text-stage">${{s.trail_stage}}</td></tr>
                    <tr><td class="label-cell">Secured Points</td><td class="val-cell text-green"><b>${{s.secured_pts}}</b></td></tr>
                </table>
            `;
        }}

        chart.subscribeCrosshairMove(param => {{
            if (!param.time || !param.seriesData.get(candleSeries)) return;
            const bar = param.seriesData.get(candleSeries);
            document.getElementById('barO').innerText = bar.open.toFixed(2);
            document.getElementById('barH').innerText = bar.high.toFixed(2);
            document.getElementById('barL').innerText = bar.low.toFixed(2);
            document.getElementById('barC').innerText = bar.close.toFixed(2);
            const vBar = param.seriesData.get(vwapSeries);
            if (vBar) document.getElementById('barVWAP').innerText = vBar.value.toFixed(2);
            const eBar = param.seriesData.get(emaSeries);
            if (eBar) document.getElementById('barEMA').innerText = eBar.value.toFixed(2);
        }});

        window.addEventListener('resize', () => {{
            chart.applyOptions({{
                width: window.innerWidth,
                height: window.innerHeight - 35
            }});
        }});

        function changeTF(tf) {{
            const url = new URL(window.parent.location.href);
            url.searchParams.set('interval', tf);
            window.parent.location.href = url.href;
        }}
    </script>
</body>
</html>
"""

components.html(html_code, height=720, scrolling=False)

# Auto-refresh
st.markdown(
    """
    <script>
        setTimeout(function(){
            window.location.reload();
        }, 15000);
    </script>
""",
    unsafe_allow_html=True,
)
