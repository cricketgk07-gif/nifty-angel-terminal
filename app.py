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
    st.error("Authentication failed. Please check Streamlit Secrets.")
    st.stop()


def fetch_raw_candles(interval_code, days_back):
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

    df = df[(df["open"] > 1000) & (df["high"] > 1000) & (df["low"] > 1000) & (df["close"] > 1000)].copy()

    # NSE Trading Hours
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
    return df


# 1. Fetch 5m candles (Primary Chart & Breakout)
df_5m = fetch_raw_candles("FIVE_MINUTE", 25)
# 2. Fetch 1m candles (Micro-Swing Trailing Engine)
df_1m = fetch_raw_candles("ONE_MINUTE", 5)

if df_5m is None or len(df_5m) == 0:
    st.info("Market feed is initializing...")
    st.stop()

# Indicators on 5m
candle_spread = (df_5m["high"] - df_5m["low"]) + (df_5m["close"] - df_5m["open"]).abs()
raw_vol = df_5m["volume"].apply(lambda v: float(v) if pd.notnull(v) and v > 0 else 0.0)
df_5m["calc_vol"] = raw_vol.where(raw_vol > 0, candle_spread * 1250.0 + 500.0)

df_5m["tp"] = (df_5m["high"] + df_5m["low"] + df_5m["close"]) / 3.0
df_5m["vol_mult"] = df_5m["tp"] * df_5m["calc_vol"]
df_5m["cum_vol"] = df_5m.groupby("date")["calc_vol"].cumsum()
df_5m["cum_vp"] = df_5m.groupby("date")["vol_mult"].cumsum()
df_5m["vwap"] = df_5m["cum_vp"] / df_5m["cum_vol"]
df_5m["vwap"] = df_5m["vwap"].fillna(df_5m["tp"])
df_5m.loc[df_5m["vwap"] < 1000, "vwap"] = df_5m["tp"]
df_5m["ema9"] = df_5m["close"].ewm(span=9, adjust=False).mean()

# 14-period ATR
h_l = df_5m["high"] - df_5m["low"]
h_cp = (df_5m["high"] - df_5m["close"].shift(1)).abs()
l_cp = (df_5m["low"] - df_5m["close"].shift(1)).abs()
tr = pd.concat([h_l, h_cp, l_cp], axis=1).max(axis=1)
df_5m["atr"] = tr.rolling(window=14, min_periods=1).mean().fillna(15.0)

# Pre-process 1m Swing Highs / Lows (3-bar fractal pivot)
if df_1m is not None and len(df_1m) > 0:
    df_1m["swing_low"] = (
        (df_1m["low"] < df_1m["low"].shift(1)) & (df_1m["low"] < df_1m["low"].shift(-1))
    )
    df_1m["swing_high"] = (
        (df_1m["high"] > df_1m["high"].shift(1)) & (df_1m["high"] > df_1m["high"].shift(-1))
    )

# --- Dual-Timeframe Multi-Swing Engine ---
markers = []
historical_trade_cards = {}
latest_trade_for_hud = None

grouped_5m = df_5m.groupby("date")

for session_date, day_5m in grouped_5m:
    orb_window = day_5m[day_5m["dt"].dt.time <= datetime.time(9, 30)]
    if len(orb_window) == 0:
        continue

    day_orb_h = orb_window["high"].max()
    day_orb_l = orb_window["low"].min()

    session_trade = None
    trade_executed_today = False

    # Get matching 1m session data if available
    day_1m = (
        df_1m[df_1m["date"] == session_date]
        if (df_1m is not None and session_date in df_1m["date"].values)
        else None
    )

    for idx in range(len(day_5m)):
        row = day_5m.iloc[idx]
        t = row["dt"].time()
        c = row["close"]
        h = row["high"]
        l = row["low"]
        o = row["open"]
        ema = row["ema9"]
        vwap_val = row["vwap"]
        atr_val = row["atr"]

        if session_trade and not session_trade["closed"]:
            t_type = session_trade["type"]
            entry = session_trade["entry"]
            base_atr = session_trade["entry_atr"]
            tp1_target = session_trade["tp1"]

            # Query 1m short-term micro-swings up to current 5m bar time
            curr_bar_dt = row["dt"]
            m1_data_slice = (
                day_1m[
                    (day_1m["dt"] >= session_trade["entry_time"])
                    & (day_1m["dt"] <= curr_bar_dt)
                ]
                if day_1m is not None
                else None
            )

            is_nearby_tp1 = (
                h >= (entry + (2.5 * base_atr))
                if t_type == "CE"
                else l <= (entry - (2.5 * base_atr))
            )
            has_crossed_tp1 = h >= tp1_target if t_type == "CE" else l <= tp1_target

            if has_crossed_tp1:
                session_trade["tp1_hit"] = True
                session_trade["trail_stage"] = "TP1 CROSSED: Tight 1m Bar Trail"
            elif is_nearby_tp1 and not session_trade["tp1_hit"]:
                session_trade["trail_stage"] = "NEARBY TP1: 1m Swing Trail Active"

            if t_type == "CE":
                # Find recent 1-minute swing lows
                if m1_data_slice is not None and len(m1_data_slice) > 0:
                    swings_1m = m1_data_slice[m1_data_slice["swing_low"]]["low"]
                    if len(swings_1m) > 0:
                        recent_1m_low = swings_1m.iloc[-1]
                        trail_candidate_1m = round(recent_1m_low - 2.5, 1)

                        # If crossed or nearby TP1, tighten to last 1m candle low
                        if session_trade["tp1_hit"] or is_nearby_tp1:
                            tight_1m = round(m1_data_slice["low"].iloc[-2] - 1.5, 1) if len(m1_data_slice) >= 2 else trail_candidate_1m
                            target_sl = max(tight_1m, round(entry + (1.8 * base_atr), 1))
                            if target_sl > session_trade["current_sl"]:
                                session_trade["current_sl"] = target_sl
                        else:
                            # Standard 1m swing low trail
                            if trail_candidate_1m > session_trade["current_sl"]:
                                session_trade["current_sl"] = trail_candidate_1m
                else:
                    # Fallback to 5m EMA if 1m history slice is unavailable
                    wide_trail = round(ema - (atr_val * 0.9), 1)
                    if wide_trail > session_trade["current_sl"]:
                        session_trade["current_sl"] = wide_trail

                if h > session_trade["tp_final"]:
                    session_trade["tp_final"] = round(h + (session_trade["risk"] * 1.0), 1)

                if l <= session_trade["current_sl"] or (c < ema and c < o and c < (day_orb_h - 3.0)):
                    exit_p = min(c, session_trade["current_sl"])
                    pts = round(exit_p - entry, 1)
                    session_trade["closed"] = True
                    session_trade["exit_price"] = exit_p
                    session_trade["exit_pts"] = pts
                    latest_trade_for_hud = session_trade.copy()

            elif t_type == "PE":
                if m1_data_slice is not None and len(m1_data_slice) > 0:
                    swings_1m = m1_data_slice[m1_data_slice["swing_high"]]["high"]
                    if len(swings_1m) > 0:
                        recent_1m_high = swings_1m.iloc[-1]
                        trail_candidate_1m = round(recent_1m_high + 2.5, 1)

                        if session_trade["tp1_hit"] or is_nearby_tp1:
                            tight_1m = round(m1_data_slice["high"].iloc[-2] + 1.5, 1) if len(m1_data_slice) >= 2 else trail_candidate_1m
                            target_sl = min(tight_1m, round(entry - (1.8 * base_atr), 1))
                            if target_sl < session_trade["current_sl"]:
                                session_trade["current_sl"] = target_sl
                        else:
                            if trail_candidate_1m < session_trade["current_sl"]:
                                session_trade["current_sl"] = trail_candidate_1m
                else:
                    wide_trail = round(ema + (atr_val * 0.9), 1)
                    if wide_trail < session_trade["current_sl"]:
                        session_trade["current_sl"] = wide_trail

                if l < session_trade["tp_final"]:
                    session_trade["tp_final"] = round(l - (session_trade["risk"] * 1.0), 1)

                if h >= session_trade["current_sl"] or (c > ema and c > o and c > (day_orb_l + 3.0)):
                    exit_p = max(c, session_trade["current_sl"])
                    pts = round(entry - exit_p, 1)
                    session_trade["closed"] = True
                    session_trade["exit_price"] = exit_p
                    session_trade["exit_pts"] = pts
                    latest_trade_for_hud = session_trade.copy()

        # 5m Morning Breakout Entry Trigger
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
                    "entry_time": row["dt"],
                    "entry_atr": atr_val,
                    "init_sl": init_sl,
                    "current_sl": init_sl,
                    "tp1": tp1,
                    "tp_final": tp_final,
                    "tp1_hit": False,
                    "trail_stage": "1m SWING TRAIL: Active",
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
                historical_trade_cards[int(row["time"])] = {
                    "title": "BUY CE (ORB BREAKOUT)",
                    "entry": round(c, 1),
                    "target": round(tp1, 1),
                    "target_pts": round(tp1 - c, 1),
                    "sl": round(init_sl, 1),
                    "sl_pts": round(c - init_sl, 1),
                    "type": "CE",
                }
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
                    "entry_time": row["dt"],
                    "entry_atr": atr_val,
                    "init_sl": init_sl,
                    "current_sl": init_sl,
                    "tp1": tp1,
                    "tp_final": tp_final,
                    "tp1_hit": False,
                    "trail_stage": "1m SWING TRAIL: Active",
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
                historical_trade_cards[int(row["time"])] = {
                    "title": "BUY PE (ORB BREAKDOWN)",
                    "entry": round(c, 1),
                    "target": round(tp1, 1),
                    "target_pts": round(c - tp1, 1),
                    "sl": round(init_sl, 1),
                    "sl_pts": round(init_sl - c, 1),
                    "type": "PE",
                }
                trade_executed_today = True
                latest_trade_for_hud = session_trade.copy()

today_date = df_5m["date"].iloc[-1]
today_df = df_5m[df_5m["date"] == today_date]
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
curr = df_5m.iloc[-1]

candles_data = []
volume_data = []
vol_max = df_5m["calc_vol"].max() if df_5m["calc_vol"].max() > 0 else 1.0

for _, r in df_5m.iterrows():
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
        for _, r in df_5m.iterrows()
    ]
)
ema_json = json.dumps(
    [
        {"time": int(r["time"]), "value": round(float(r["ema9"]), 2)}
        for _, r in df_5m.iterrows()
    ]
)
markers_json = json.dumps(markers)
history_cards_json = json.dumps(historical_trade_cards)

hud_payload = None
dynamic_lines = {}
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
    dynamic_lines = {
        "entry": entry_p,
        "target": latest_trade_for_hud["tp1"],
        "trailing_sl": trail_p,
    }

hud_json = json.dumps(hud_payload)
lines_json = json.dumps(dynamic_lines)

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
            border-radius: 2px; cursor: not-allowed; opacity: 0.45;
        }}
        .tf-btn.active {{
            background: #2962ff; color: #ffffff; font-weight: 700; cursor: default; opacity: 1.0;
        }}

        .dynamic-ohlc-row {{
            font-size: 10px; color: #787b86; display: flex; gap: 6px;
            background: rgba(11, 14, 20, 0.92); padding: 2px 6px;
            border-radius: 3px; border: 1px solid rgba(42, 46, 57, 0.4);
            font-family: monospace; width: fit-content;
        }}
        .dynamic-ohlc-row b {{ color: #d1d4dc; }}

        /* Draggable HUD Table */
        .draggable-strategy-box {{
            position: absolute; bottom: 48px; right: 65px; z-index: 60;
            background: rgba(19, 23, 34, 0.97); border: 1px solid #2a2e39;
            border-radius: 6px; font-size: 9.5px; color: #d1d4dc; overflow: hidden;
            box-shadow: 0 4px 18px rgba(0,0,0,0.9); cursor: grab; user-select: none;
            touch-action: none;
        }}
        .draggable-strategy-box:active {{ cursor: grabbing; }}
        .box-drag-handle {{
            background: #161a25; padding: 3px 6px; font-size: 8.5px; font-weight: 700;
            color: #787b86; text-align: center; border-bottom: 1px solid #2a2e39;
            letter-spacing: 0.5px;
        }}
        .draggable-strategy-box table {{ border-collapse: collapse; }}
        .draggable-strategy-box td {{ padding: 3px 8px; border-bottom: 1px solid #222631; white-space: nowrap; }}
        .draggable-strategy-box tr:last-child td {{ border-bottom: none; }}
        .label-cell {{ color: #787b86; font-weight: 500; }}
        .val-cell {{ font-weight: 700; color: #ffffff; text-align: right; }}
        .tag-pill {{ color: #fff; font-weight: bold; border-radius: 2px; padding: 1px 5px; text-align: center; font-size: 9px; }}
        .text-red {{ color: #f23645; }}
        .text-green {{ color: #089981; }}
        .text-trail {{ color: #2962ff; font-weight: bold; }}
        .text-stage {{ color: #00e5ff; font-size: 8.5px; font-weight: bold; }}

        /* Hover History Signal Tag */
        .history-signal-tag {{
            position: absolute; z-index: 55; pointer-events: none; display: none;
            background: rgba(22, 26, 37, 0.96); border: 1px solid #363c4e; border-radius: 5px;
            padding: 5px 8px; font-size: 9.5px; color: #d1d4dc; line-height: 1.4;
            box-shadow: 0 4px 12px rgba(0,0,0,0.7); transform: translate(-50%, -100%);
        }}
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
                <button class="tf-btn" disabled>1m</button>
                <button class="tf-btn" disabled>3m</button>
                <button class="tf-btn active">5m</button>
                <button class="tf-btn" disabled>15m</button>
                <button class="tf-btn" disabled>30m</button>
                <button class="tf-btn" disabled>1h</button>
                <button class="tf-btn" disabled>1D</button>
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

    <!-- Draggable HUD Table -->
    <div id="strategyBox" class="draggable-strategy-box" style="display: none;"></div>

    <!-- Hover History Tag -->
    <div id="historyTag" class="history-signal-tag"></div>

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
            scaleMargins: {{ top: 0.82, bottom: 0.02 }}
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

        // Entry markers only
        candleSeries.setMarkers({markers_json});

        // Static Session ORB Lines
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

        // Dynamic Horizontal Strategy Lines
        const dLines = {lines_json};
        if (dLines && dLines.entry) {{
            candleSeries.createPriceLine({{
                price: dLines.entry,
                color: '#2962ff',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Dotted,
                axisLabelVisible: true,
                title: 'ENTRY'
            }});
            candleSeries.createPriceLine({{
                price: dLines.target,
                color: '#00bfa5',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Solid,
                axisLabelVisible: true,
                title: 'TARGET 1 (3 ATR)'
            }});
            candleSeries.createPriceLine({{
                price: dLines.trailing_sl,
                color: '#ff9800',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Solid,
                axisLabelVisible: true,
                title: '1m TRAIL SL'
            }});
        }}

        // Render Strategy Table
        const s = {hud_json};
        const table = document.getElementById('strategyBox');
        if (s) {{
            table.style.display = 'block';
            table.innerHTML = `
                <div class="box-drag-handle">::: DRAG TABLE :::</div>
                <table>
                    <tr><td class="label-cell">Call or Put</td><td class="val-cell"><span class="tag-pill" style="background:${{s.theme}}">${{s.call_or_put}}</span></td></tr>
                    <tr><td class="label-cell">Reason for trade</td><td class="val-cell" style="font-size:9px; color:#cfd3dc;">${{s.reason}}</td></tr>
                    <tr><td class="label-cell">Entry</td><td class="val-cell"><b>${{s.entry}}</b></td></tr>
                    <tr><td class="label-cell">SL (risk pts)</td><td class="val-cell text-red">${{s.sl_risk}}</td></tr>
                    <tr><td class="label-cell">Target 1 (3 ATR)</td><td class="val-cell text-green">${{s.target1}}</td></tr>
                    <tr><td class="label-cell">Target Final</td><td class="val-cell text-green">${{s.target_final}}</td></tr>
                    <tr><td class="label-cell">Trailing SL (1m)</td><td class="val-cell text-trail">${{s.trailing_sl}}</td></tr>
                    <tr><td class="label-cell">Trail Source</td><td class="val-cell text-stage">${{s.trail_stage}}</td></tr>
                    <tr><td class="label-cell">Secured Points</td><td class="val-cell text-green"><b>${{s.secured_pts}}</b></td></tr>
                </table>
            `;
        }}

        // Mouse & Touch Drag Implementation
        let isDragging = false;
        let startX, startY, initLeft, initTop;

        function onDragStart(e) {{
            isDragging = true;
            const clientX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
            const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            startX = clientX;
            startY = clientY;
            const rect = table.getBoundingClientRect();
            initLeft = rect.left;
            initTop = rect.top;
            table.style.right = 'auto';
            table.style.bottom = 'auto';
            table.style.left = initLeft + 'px';
            table.style.top = initTop + 'px';
        }}

        function onDragMove(e) {{
            if (!isDragging) return;
            const clientX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
            const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            const dx = clientX - startX;
            const dy = clientY - startY;
            table.style.left = (initLeft + dx) + 'px';
            table.style.top = (initTop + dy) + 'px';
        }}

        function onDragEnd() {{
            isDragging = false;
        }}

        table.addEventListener('mousedown', onDragStart);
        window.addEventListener('mousemove', onDragMove);
        window.addEventListener('mouseup', onDragEnd);
        table.addEventListener('touchstart', onDragStart, {{ passive: true }});
        window.addEventListener('touchmove', onDragMove, {{ passive: true }});
        window.addEventListener('touchend', onDragEnd);

        // Historical Hover Signal Card & Crosshair
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
                        ${{sig.title}}
                    </div>
                    <div><b>Entry:</b> ${{sig.entry.toFixed(1)}}</div>
                    <div><b>Target:</b> ${{sig.target.toFixed(1)}} (+${{sig.target_pts.toFixed(1)}} pts)</div>
                    <div><b>Stop Loss:</b> ${{sig.sl.toFixed(1)}} (-${{sig.sl_pts.toFixed(1)}} pts)</div>
                `;
            }} else {{
                hTag.style.display = 'none';
            }}
        }});

        window.addEventListener('resize', () => {{
            chart.applyOptions({{
                width: window.innerWidth,
                height: window.innerHeight - 35
            }});
        }});
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
