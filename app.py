import datetime
import json
import re
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
        padding: 0.5rem 1rem !important;
        margin: 0 !important;
        max-width: 100vw !important;
        height: 100vh !important;
        overflow: hidden !important;
    }
    iframe {
        border: none !important;
        width: 100vw !important;
        height: calc(100vh - 65px) !important;
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

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
LOT_SIZE_QTY = 65


@st.cache_resource
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


# Load Angel One NFO Scrip Master
@st.cache_data(ttl=3600)
def load_nfo_scrip_master():
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        nfo_items = [
            x
            for x in data
            if x.get("exch_seg") == "NFO"
            and x.get("name") == "NIFTY"
            and x.get("instrumenttype") == "OPTIDX"
        ]
        df_nfo = pd.DataFrame(nfo_items)
        if not df_nfo.empty:
            df_nfo["strike_num"] = (
                pd.to_numeric(df_nfo["strike"], errors="coerce") / 100.0
            )
            df_nfo["exp_dt"] = pd.to_datetime(
                df_nfo["expiry"], format="%d%b%Y", errors="coerce"
            )
        return df_nfo
    except Exception:
        return pd.DataFrame()


nfo_df = load_nfo_scrip_master()


def fetch_nifty_candles():
    now_ist = datetime.datetime.now(IST)
    collected_frames = []
    current_end = now_ist

    for _ in range(4):
        chunk_start = current_end - datetime.timedelta(days=6)
        from_str = chunk_start.strftime("%Y-%m-%d 09:15")
        to_str = current_end.strftime("%Y-%m-%d %H:%M")

        try:
            resp = api.getCandleData(
                {
                    "exchange": "NSE",
                    "symboltoken": INDEX_TOKEN,
                    "interval": "FIVE_MINUTE",
                    "fromdate": from_str,
                    "todate": to_str,
                }
            )
            if isinstance(resp, dict) and resp.get("status") and resp.get("data"):
                df_chunk = pd.DataFrame(
                    resp["data"],
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                collected_frames.append(df_chunk)
        except Exception:
            pass

        current_end = chunk_start

    if not collected_frames:
        return None

    df = pd.concat(collected_frames, ignore_index=True)
    df["dt"] = pd.to_datetime(df["timestamp"])
    df = df.drop_duplicates(subset=["dt"]).sort_values(by="dt").reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    df = df[(df["open"] > 1000) & (df["high"] > 1000) & (df["low"] > 1000) & (df["close"] > 1000)].copy()
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

    candle_spread = (df["high"] - df["low"]) + (df["close"] - df["open"]).abs()
    raw_vol = df["volume"].apply(lambda v: float(v) if pd.notnull(v) and v > 0 else 0.0)
    df["calc_vol"] = raw_vol.where(raw_vol > 0, candle_spread * 1250.0 + 500.0)

    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vol_mult"] = df["tp"] * df["calc_vol"]
    df["cum_vol"] = df.groupby("date")["calc_vol"].cumsum()
    df["cum_vp"] = df.groupby("date")["vol_mult"].cumsum()
    df["vwap"] = df["cum_vp"] / df["cum_vol"]
    df["vwap"] = df["vwap"].fillna(df["tp"])

    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()

    high_low = df["high"] - df["low"]
    high_cp = (df["high"] - df["close"].shift(1)).abs()
    low_cp = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_cp, low_cp], axis=1).max(axis=1)
    df["atr"] = tr.rolling(window=14, min_periods=1).mean().fillna(15.0)

    return df


df = fetch_nifty_candles()
if df is None or len(df) == 0:
    st.info("Market data feed is initializing...")
    st.stop()

curr = df.iloc[-1]
spot_price = float(curr["close"])

# --- Previous Day Floor Pivots ---
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
        R1 = round((2.0 * P) - pdl, 2)
        S1 = round((2.0 * P) - pdh, 2)
        R2 = round(P + (pdh - pdl), 2)
        S2 = round(P - (pdh - pdl), 2)
        R3 = round(pdh + 2.0 * (P - pdl), 2)
        S3 = round(pdl - 2.0 * (pdh - P), 2)

        pivot_lines_data = {
            "P": P, "R1": R1, "R2": R2, "R3": R3,
            "S1": S1, "S2": S2, "S3": S3,
            "PDH": pdh, "PDL": pdl
        }

# ATM ± 1000 Strikes
atm_strike = int(round(spot_price / 50.0) * 50)
strikes_list = [atm_strike + (x * 50) for x in range(-20, 21)]

# Expiry Contract List
available_expiries = []
today_dt = datetime.datetime.now(IST).date()
if not nfo_df.empty:
    future_nfo = nfo_df[nfo_df["exp_dt"].dt.date >= today_dt].sort_values("exp_dt")
    if not future_nfo.empty:
        available_expiries = future_nfo["expiry"].dropna().drop_duplicates().tolist()
    else:
        available_expiries = nfo_df["expiry"].dropna().drop_duplicates().tolist()

# ----------------- NATIVE DYNAMIC INPUT BAR -----------------
col1, col2, col3, col4, col5 = st.columns([1.5, 1.5, 1, 1, 3])

with col1:
    default_strike_idx = strikes_list.index(atm_strike) if atm_strike in strikes_list else 20
    selected_strike = st.selectbox("Strike", strikes_list, index=default_strike_idx)

with col2:
    selected_expiry = st.selectbox("Expiry", available_expiries if available_expiries else ["CURRENT"])

with col3:
    selected_type = st.selectbox("Type", ["CE", "PE"])

with col4:
    selected_lots = st.number_input("Lots", min_value=1, max_value=100, value=1, step=1)

# Fetch Exact Live Market LTP via SmartAPI
live_real_ltp = 0.0
target_symbol = ""
target_token = ""

if not nfo_df.empty:
    scrip_match = nfo_df[
        (nfo_df["strike_num"] == float(selected_strike))
        & (nfo_df["symbol"].str.endswith(selected_type))
        & (nfo_df["expiry"] == selected_expiry)
    ]
    if not scrip_match.empty:
        target_symbol = str(scrip_match.iloc[0]["symbol"])
        target_token = str(scrip_match.iloc[0]["token"])
        try:
            res = api.ltpData("NFO", target_symbol, target_token)
            if isinstance(res, dict) and res.get("status") and res.get("data"):
                live_real_ltp = float(res["data"].get("ltp", 0.0))
        except Exception:
            pass

# Delta calculation
diff_val = (spot_price - float(selected_strike)) if selected_type == "CE" else (float(selected_strike) - spot_price)
active_delta = round(min(0.95, max(0.05, 0.50 + (diff_val / 800.0))), 2)

total_qty = selected_lots * LOT_SIZE_QTY

with col5:
    st.markdown(
        f"""
        <div style="background:#161a25; border:1px solid #2a2e39; border-radius:5px; padding:6px 12px; margin-top:20px; display:flex; gap:16px; align-items:center;">
            <div>Contract: <b style="color:#00e5ff;">{target_symbol or 'NIFTY OPT'}</b></div>
            <div>Live LTP: <b style="color:#ffd600; font-size:16px;">₹{live_real_ltp:.2f}</b></div>
            <div>Qty: <b style="color:#089981;">{total_qty}</b> ({selected_lots}L)</div>
            <div>Delta: <b style="color:#ab47bc;">{active_delta}</b></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ----------------- STRATEGY ENGINE & HUD TABLE -----------------
markers = []
historical_trade_cards = {}
latest_trade_for_hud = None
alarm_signal_triggered = False

grouped = df.groupby("date")
latest_session_date = df["date"].iloc[-1]
trade_executed_in_latest_session = False

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

            wide_buffer = min(max(round(atr_val * 1.0, 1), 7.0), 12.0)
            tight_buffer = min(max(round(atr_val * 0.35, 1), 3.0), 5.0)

            if t_type == "CE":
                nearby_threshold = entry + (2.4 * base_atr)
                is_nearby_tp1 = h >= nearby_threshold
                has_crossed_tp1 = h >= tp1_target

                if has_crossed_tp1:
                    session_trade["tp1_hit"] = True
                    session_trade["trail_stage"] = "TP1 CROSSED (Tightened Buffer)"
                elif is_nearby_tp1 and not session_trade["tp1_hit"]:
                    session_trade["trail_stage"] = "NEARBY TP1 (Tightening Trail)"

                if h >= (entry + (1.5 * base_atr)) and not session_trade["be_locked"]:
                    session_trade["be_locked"] = True
                    session_trade["current_sl"] = max(session_trade["current_sl"], round(entry + 3.0, 1))

                if session_trade["tp1_hit"] or is_nearby_tp1:
                    tight_trail = max(round(ema - tight_buffer, 1), round(l - 1.5, 1))
                    min_locked = round(entry + (1.8 * base_atr), 1) if session_trade["tp1_hit"] else round(entry + 3.0, 1)
                    target_sl = max(tight_trail, min_locked)
                    if target_sl > session_trade["current_sl"]:
                        session_trade["current_sl"] = target_sl
                else:
                    wide_trail = round(ema - wide_buffer, 1)
                    if session_trade["be_locked"]:
                        session_trade["current_sl"] = max(session_trade["current_sl"], wide_trail)
                    elif wide_trail > session_trade["current_sl"]:
                        session_trade["current_sl"] = wide_trail

                if h > session_trade["tp_final"]:
                    session_trade["tp_final"] = round(h + (session_trade["risk"] * 1.0), 1)

                if l <= session_trade["current_sl"] or (c < ema and c < o and c < (day_orb_h - 3.0)):
                    exit_p = min(c, session_trade["current_sl"])
                    pts = round(exit_p - entry, 1)
                    session_trade["closed"] = True
                    session_trade["exit_price"] = exit_p
                    session_trade["exit_pts"] = pts
                    session_trade["trail_stage"] = f"TRADE EXITED ({pts:+.1f} pts)"
                    latest_trade_for_hud = session_trade.copy()

            elif t_type == "PE":
                nearby_threshold = entry - (2.4 * base_atr)
                is_nearby_tp1 = l <= nearby_threshold
                has_crossed_tp1 = l <= tp1_target

                if has_crossed_tp1:
                    session_trade["tp1_hit"] = True
                    session_trade["trail_stage"] = "TP1 CROSSED (Tightened Buffer)"
                elif is_nearby_tp1 and not session_trade["tp1_hit"]:
                    session_trade["trail_stage"] = "NEARBY TP1 (Tightening Trail)"

                if l <= (entry - (1.5 * base_atr)) and not session_trade["be_locked"]:
                    session_trade["be_locked"] = True
                    session_trade["current_sl"] = min(session_trade["current_sl"], round(entry - 3.0, 1))

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

                if l < session_trade["tp_final"]:
                    session_trade["tp_final"] = round(l - (session_trade["risk"] * 1.0), 1)

                if h >= session_trade["current_sl"] or (c > ema and c > o and c > (day_orb_l + 3.0)):
                    exit_p = max(c, session_trade["current_sl"])
                    pts = round(entry - exit_p, 1)
                    session_trade["closed"] = True
                    session_trade["exit_price"] = exit_p
                    session_trade["exit_pts"] = pts
                    session_trade["trail_stage"] = f"TRADE EXITED ({pts:+.1f} pts)"
                    latest_trade_for_hud = session_trade.copy()

        # Breakout Entry Window: 09:30 to 10:30 AM
        if datetime.time(9, 30) < t <= datetime.time(10, 30) and not trade_executed_today:
            if c > day_orb_h and c > vwap_val and c > ema:
                init_sl = round(l - 5.0, 1)
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
                if session_date == latest_session_date:
                    trade_executed_in_latest_session = True
                    alarm_signal_triggered = True

            elif c < day_orb_l and c < vwap_val and c < ema:
                init_sl = round(h + 5.0, 1)
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
                if session_date == latest_session_date:
                    trade_executed_in_latest_session = True
                    alarm_signal_triggered = True

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
latest_bar_time = curr["dt"].time()

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

hud_payload = None
if trade_executed_in_latest_session and latest_trade_for_hud:
    is_ce = latest_trade_for_hud["type"] == "CE"
    entry_p = latest_trade_for_hud["entry"]
    trail_p = latest_trade_for_hud["current_sl"]
    secured_pts = round(trail_p - entry_p, 1) if is_ce else round(entry_p - trail_p, 1)
    risk_pts = latest_trade_for_hud["risk"]
    target1_pts = round(abs(latest_trade_for_hud["tp1"] - entry_p), 1)
    target_final_pts = round(abs(latest_trade_for_hud["tp_final"] - entry_p), 1)
    current_pts = round(curr["close"] - entry_p, 1) if is_ce else round(entry_p - curr["close"], 1)

    hud_payload = {
        "is_no_trade": False,
        "call_or_put": latest_trade_for_hud["call_or_put"],
        "reason": latest_trade_for_hud["reason"],
        "entry": f"{entry_p:.1f}",
        "sl_risk": f"{latest_trade_for_hud['init_sl']:.1f} (-{risk_pts:.1f} pts)",
        "target1": f"{latest_trade_for_hud['tp1']:.1f} (+{target1_pts:.1f} pts)",
        "target_final": f"{latest_trade_for_hud['tp_final']:.1f} (+{target_final_pts:.1f} pts)",
        "trailing_sl": f"{trail_p:.1f}",
        "trail_stage": latest_trade_for_hud["trail_stage"],
        "secured_pts": f"{secured_pts:+.1f} pts",
        "current_pts": current_pts,
        "raw_risk_pts": risk_pts,
        "raw_target1_pts": target1_pts,
        "raw_target_final_pts": target_final_pts,
        "raw_secured_pts": secured_pts,
        "theme": "#089981" if is_ce else "#f23645",
    }
elif (not trade_executed_in_latest_session) and (latest_bar_time >= datetime.time(10, 30)):
    hud_payload = {
        "is_no_trade": True,
        "status": "NO TRADE TODAY",
        "reason": "No valid breakout before 10:30 AM",
        "action": "Capital Protected (Wait for tomorrow)",
        "theme": "#787b86",
    }
elif latest_trade_for_hud:
    is_ce = latest_trade_for_hud["type"] == "CE"
    entry_p = latest_trade_for_hud["entry"]
    trail_p = latest_trade_for_hud["current_sl"]
    secured_pts = round(trail_p - entry_p, 1) if is_ce else round(entry_p - trail_p, 1)
    risk_pts = latest_trade_for_hud["risk"]
    target1_pts = round(abs(latest_trade_for_hud["tp1"] - entry_p), 1)
    target_final_pts = round(abs(latest_trade_for_hud["tp_final"] - entry_p), 1)
    current_pts = round(curr["close"] - entry_p, 1) if is_ce else round(entry_p - curr["close"], 1)

    hud_payload = {
        "is_no_trade": False,
        "call_or_put": latest_trade_for_hud["call_or_put"],
        "reason": latest_trade_for_hud["reason"],
        "entry": f"{entry_p:.1f}",
        "sl_risk": f"{latest_trade_for_hud['init_sl']:.1f} (-{risk_pts:.1f} pts)",
        "target1": f"{latest_trade_for_hud['tp1']:.1f} (+{target1_pts:.1f} pts)",
        "target_final": f"{latest_trade_for_hud['tp_final']:.1f} (+{target_final_pts:.1f} pts)",
        "trailing_sl": f"{trail_p:.1f}",
        "trail_stage": latest_trade_for_hud["trail_stage"],
        "secured_pts": f"{secured_pts:+.1f} pts",
        "current_pts": current_pts,
        "raw_risk_pts": risk_pts,
        "raw_target1_pts": target1_pts,
        "raw_target_final_pts": target_final_pts,
        "raw_secured_pts": secured_pts,
        "theme": "#089981" if is_ce else "#f23645",
    }

hud_json = json.dumps(hud_payload)
play_alarm_flag = "true" if alarm_signal_triggered else "false"

# ----------------- EMBEDDED JAVASCRIPT TERMINAL -----------------
html_code = f"""
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        html, body {{
            width: 100vw; height: 100%;
            background-color: #0b0e14;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            overflow: hidden;
        }}
        #chartArea {{
            width: 100vw; height: 100vh;
            position: absolute; top: 0; left: 0;
        }}

        .tools-bar {{
            position: absolute; top: 8px; left: 12px; z-index: 60;
            display: flex; align-items: center; gap: 4px; background: rgba(22, 26, 37, 0.95);
            padding: 3px 6px; border-radius: 4px; border: 1px solid #363c4e;
        }}
        .tool-btn {{
            background: #161a25; border: 1px solid #2a2e39; color: #d1d4dc;
            font-size: 9.5px; padding: 3px 6px; border-radius: 3px; cursor: pointer;
            font-weight: 600;
        }}
        .tool-btn:hover {{ background: #2962ff; color: #ffffff; }}
        .tool-btn.active-draw {{ background: #00e5ff; color: #000; font-weight: 800; }}

        .alarm-toggle-btn {{
            background: rgba(30, 34, 45, 0.95); border: 1px solid #363c4e; color: #00e5ff;
            font-size: 10px; font-weight: 700; padding: 3px 6px; border-radius: 4px;
            cursor: pointer; display: flex; align-items: center; gap: 4px;
        }}
        .alarm-toggle-btn.enabled {{
            background: #00bfa5; color: #000; border-color: #00bfa5;
        }}

        .alarm-ringing-banner {{
            position: fixed; top: 8px; left: 50%; transform: translateX(-50%); z-index: 100;
            background: #f23645; color: #ffffff; font-weight: 800; font-size: 11px;
            padding: 6px 14px; border-radius: 20px; box-shadow: 0 0 20px rgba(242, 54, 69, 0.8);
            cursor: pointer; display: none; animation: pulseAlarm 0.8s infinite alternate;
        }}
        @keyframes pulseAlarm {{
            from {{ transform: translateX(-50%) scale(1); }}
            to {{ transform: translateX(-50%) scale(1.06); }}
        }}

        /* 3-Column Strategy Table */
        .draggable-strategy-box {{
            position: absolute; bottom: 35px; right: 55px; z-index: 60;
            background: rgba(19, 23, 34, 0.97); border: 1px solid #2a2e39;
            border-radius: 6px; font-size: 9.5px; color: #d1d4dc; overflow: hidden;
            box-shadow: 0 4px 18px rgba(0,0,0,0.9); cursor: grab; user-select: none;
            touch-action: none; min-width: 320px;
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
        .draggable-strategy-box tr:last-child td {{ border-bottom: none; }}
        .label-cell {{ color: #787b86; font-weight: 500; text-align: left; }}
        .val-cell {{ font-weight: 700; color: #ffffff; text-align: right; }}
        .opt-cell {{ font-weight: 700; color: #ffd600; text-align: right; }}
        .tag-pill {{ color: #fff; font-weight: bold; border-radius: 2px; padding: 1px 5px; text-align: center; font-size: 9px; }}
        .text-red {{ color: #f23645; }}
        .text-green {{ color: #089981; }}
        .text-trail {{ color: #2962ff; font-weight: bold; }}
        .text-stage {{ color: #00e5ff; font-size: 8.5px; font-weight: bold; }}

        /* Position Box */
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
    <div id="ringingBanner" class="alarm-ringing-banner" onclick="silenceAlarmNow()">
        🚨 SIGNAL CONFIRMED! [TAP TO MUTE] 🔇
    </div>

    <!-- Drawing Tools -->
    <div class="tools-bar">
        <button id="btnFibR" class="tool-btn" onclick="activateDrawMode('FIB_RETRACE')">+ Fib Retrace</button>
        <button id="btnFibE" class="tool-btn" onclick="activateDrawMode('FIB_EXT')">+ Fib Ext</button>
        <button id="btnLong" class="tool-btn" onclick="activateDrawMode('LONG')">+ Long Tool</button>
        <button id="btnShort" class="tool-btn" onclick="activateDrawMode('SHORT')">+ Short Tool</button>
        <button id="alarmBtn" class="alarm-toggle-btn" onclick="toggleAudioAlarm()">
            🔔 <span id="alarmTxt">Audio</span>
        </button>
    </div>

    <div id="strategyBox" class="draggable-strategy-box" style="display: none;"></div>
    <div id="activeToolsContainer"></div>
    <div id="chartArea"></div>

    <script>
        const activeRealLTP = {live_real_ltp};
        const activeDelta = {active_delta};
        const activeQty = {total_qty};
        const activeContractLabel = "{selected_strike} {selected_type}";

        let audioCtx = null;
        let alarmUnlocked = localStorage.getItem('nifty_alarm_active') === 'true';
        let alarmTimer = null;

        function updateAlarmButtonUI() {{
            const btn = document.getElementById('alarmBtn');
            const txt = document.getElementById('alarmTxt');
            if (alarmUnlocked) {{
                btn.className = 'alarm-toggle-btn enabled';
                txt.innerText = 'Audio 🔔';
            }} else {{
                btn.className = 'alarm-toggle-btn';
                txt.innerText = 'Audio 🔕';
            }}
        }}

        function toggleAudioAlarm() {{
            if (!audioCtx) {{
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            }}
            if (audioCtx.state === 'suspended') {{
                audioCtx.resume();
            }}
            alarmUnlocked = !alarmUnlocked;
            localStorage.setItem('nifty_alarm_active', alarmUnlocked ? 'true' : 'false');
            updateAlarmButtonUI();
            if (alarmUnlocked) {{
                playBeepTone(880, 0.15);
            }} else {{
                silenceAlarmNow();
            }}
        }}

        function silenceAlarmNow() {{
            if (alarmTimer) {{
                clearInterval(alarmTimer);
                alarmTimer = null;
            }}
            document.getElementById('ringingBanner').style.display = 'none';
            sessionStorage.setItem('alarm_silenced_for_session', 'true');
        }}

        function playBeepTone(freq = 750, duration = 0.3) {{
            try {{
                if (!audioCtx) {{
                    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                }}
                if (audioCtx.state === 'suspended') {{
                    audioCtx.resume();
                }}
                const osc = audioCtx.createOscillator();
                const gain = audioCtx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(freq, audioCtx.currentTime);
                gain.gain.setValueAtTime(0.3, audioCtx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + duration);
                osc.connect(gain);
                gain.connect(audioCtx.destination);
                osc.start();
                osc.stop(audioCtx.currentTime + duration);
            }} catch(err) {{}}
        }}

        const shouldRingAlarm = {play_alarm_flag};
        updateAlarmButtonUI();
        const isSilenced = sessionStorage.getItem('alarm_silenced_for_session') === 'true';
        if (shouldRingAlarm && alarmUnlocked && !isSilenced) {{
            document.getElementById('ringingBanner').style.display = 'block';
            alarmTimer = setInterval(() => {{
                playBeepTone(880, 0.2);
                setTimeout(() => playBeepTone(1100, 0.2), 300);
            }}, 2000);
        }}

        const container = document.getElementById('chartArea');
        const chart = LightweightCharts.createChart(container, {{
            width: window.innerWidth,
            height: window.innerHeight,
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

        // 2 DOTS REMOVED: crosshairMarkerVisible set to false
        const vwapSeries = chart.addLineSeries({{
            color: '#ab47bc',
            lineWidth: 2,
            title: 'VWAP',
            crosshairMarkerVisible: false,
            priceFormat: {{ type: 'price', precision: 2, minMove: 0.05 }}
        }});
        vwapSeries.setData({vwap_json});

        // 2 DOTS REMOVED: crosshairMarkerVisible set to false
        const emaSeries = chart.addLineSeries({{
            color: '#2962ff',
            lineWidth: 1,
            title: '9-EMA',
            crosshairMarkerVisible: false,
            priceFormat: {{ type: 'price', precision: 2, minMove: 0.05 }}
        }});
        emaSeries.setData({ema_json});

        candleSeries.setMarkers({markers_json});

        // ORB Lines
        candleSeries.createPriceLine({{ price: {curr_orb_h:.2f}, color: '#089981', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'ORB HIGH' }});
        candleSeries.createPriceLine({{ price: {curr_orb_l:.2f}, color: '#f23645', lineWidth: 2, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'ORB LOW' }});

        // Floor Pivots
        const pv = {pivots_json};
        if (pv && pv.P) {{
            candleSeries.createPriceLine({{ price: pv.P, color: '#ffd600', lineWidth: 1.5, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: 'PIVOT (P)' }});
            candleSeries.createPriceLine({{ price: pv.R1, color: '#f23645', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'R1' }});
            candleSeries.createPriceLine({{ price: pv.R2, color: '#f23645', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'R2' }});
            candleSeries.createPriceLine({{ price: pv.R3, color: '#d50000', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: 'R3' }});
            candleSeries.createPriceLine({{ price: pv.S1, color: '#089981', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'S1' }});
            candleSeries.createPriceLine({{ price: pv.S2, color: '#089981', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: 'S2' }});
            candleSeries.createPriceLine({{ price: pv.S3, color: '#00c853', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Solid, axisLabelVisible: true, title: 'S3' }});
            candleSeries.createPriceLine({{ price: pv.PDH, color: '#ffb300', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: 'PDH' }});
            candleSeries.createPriceLine({{ price: pv.PDL, color: '#fb8c00', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true, title: 'PDL' }});
        }}

        // Interactive Drawing Tools
        let toolCounter = 0;
        let currentDrawMode = null;
        let drawPoints = [];
        const activePositionWidgets = [];

        function activateDrawMode(mode) {{
            currentDrawMode = mode;
            drawPoints = [];
            container.classList.add('chart-click-crosshair');
            ['btnFibR', 'btnFibE', 'btnLong', 'btnShort'].forEach(b => document.getElementById(b).classList.remove('active-draw'));
            if (mode === 'FIB_RETRACE') document.getElementById('btnFibR').classList.add('active-draw');
            if (mode === 'FIB_EXT') document.getElementById('btnFibE').classList.add('active-draw');
            if (mode === 'LONG') document.getElementById('btnLong').classList.add('active-draw');
            if (mode === 'SHORT') document.getElementById('btnShort').classList.add('active-draw');
        }}

        function cancelDrawMode() {{
            currentDrawMode = null;
            drawPoints = [];
            container.classList.remove('chart-click-crosshair');
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
            const optTgtGain = Math.round((wObj.tgtPts * activeDelta) * 10) / 10;
            const optStopLoss = Math.round((wObj.slPts * activeDelta) * 10) / 10;
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
                        <span style="color:#d1d4dc;">Opt Tgt: ₹${{(activeRealLTP + optTgtGain).toFixed(2)}} | 1:${{rr}}</span>
                        <div class="tv-touch-circle" style="top:4px;" onmousedown="resizeWidgetTgt(event, '${{wObj.id}}')" ontouchstart="resizeWidgetTgt(event, '${{wObj.id}}')"></div>
                    </div>
                    <div style="height:3px; background:#2962ff; position:relative;">
                        <div class="tv-center-handle" onmousedown="startMovePosEntry(event, '${{wObj.id}}')" ontouchstart="startMovePosEntry(event, '${{wObj.id}}')"></div>
                    </div>
                    <div class="tv-pos-zone tv-pos-red" style="height:${{lHeight}}px;">
                        <span style="font-weight:700; color:#fff;">Stop: -${{wObj.slPts.toFixed(1)}} pts (₹${{expectedLoss}})</span>
                        <span style="color:#d1d4dc;">Opt SL: ₹${{Math.max(0, activeRealLTP - optStopLoss).toFixed(2)}} | Qty: ${{activeQty}}</span>
                        <div class="tv-touch-circle" style="bottom:4px;" onmousedown="resizeWidgetSL(event, '${{wObj.id}}')" ontouchstart="resizeWidgetSL(event, '${{wObj.id}}')"></div>
                    </div>
                `;
            }} else {{
                wObj.domElem.innerHTML = `
                    <div class="tv-del-btn" onclick="deletePosWidget('${{wObj.id}}')">✕</div>
                    <div class="tv-pos-zone tv-pos-red" style="height:${{lHeight}}px;">
                        <span style="font-weight:700; color:#fff;">Stop: -${{wObj.slPts.toFixed(1)}} pts (₹${{expectedLoss}})</span>
                        <span style="color:#d1d4dc;">Opt SL: ₹${{Math.max(0, activeRealLTP - optStopLoss).toFixed(2)}} | Qty: ${{activeQty}}</span>
                        <div class="tv-touch-circle" style="top:4px;" onmousedown="resizeWidgetSL(event, '${{wObj.id}}')" ontouchstart="resizeWidgetSL(event, '${{wObj.id}}')"></div>
                    </div>
                    <div style="height:3px; background:#2962ff; position:relative;">
                        <div class="tv-center-handle" onmousedown="startMovePosEntry(event, '${{wObj.id}}')" ontouchstart="startMovePosEntry(event, '${{wObj.id}}')"></div>
                    </div>
                    <div class="tv-pos-zone tv-pos-green" style="height:${{pHeight}}px;">
                        <span style="font-weight:700; color:#fff;">Target: +${{wObj.tgtPts.toFixed(1)}} pts (₹${{expectedProfit}})</span>
                        <span style="color:#d1d4dc;">Opt Tgt: ₹${{(activeRealLTP + optTgtGain).toFixed(2)}} | 1:${{rr}}</span>
                        <div class="tv-touch-circle" style="bottom:4px;" onmousedown="resizeWidgetTgt(event, '${{wObj.id}}')" ontouchstart="resizeWidgetTgt(event, '${{wObj.id}}')"></div>
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

        function resizeWidgetTgt(e, id) {{
            e.stopPropagation();
            const wObj = activePositionWidgets.find(w => w.id === id);
            if (!wObj) return;
            const startY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            const origTgt = wObj.tgtPts;
            function onMove(ev) {{
                const curY = ev.type.includes('touch') ? ev.touches[0].clientY : ev.clientY;
                wObj.tgtPts = Math.max(10.0, origTgt + (wObj.type === 'LONG' ? (startY - curY) * 0.9 : (curY - startY) * 0.9));
                renderSinglePosWidget(wObj);
            }}
            function onUp() {{
                window.removeEventListener('mousemove', onMove);
                window.removeEventListener('mouseup', onUp);
                window.removeEventListener('touchmove', onMove);
                window.removeEventListener('touchend', onUp);
            }}
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onUp);
            window.addEventListener('touchmove', onMove, {{ passive: true }});
            window.addEventListener('touchend', onUp);
        }}

        function resizeWidgetSL(e, id) {{
            e.stopPropagation();
            const wObj = activePositionWidgets.find(w => w.id === id);
            if (!wObj) return;
            const startY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            const origSL = wObj.slPts;
            function onMove(ev) {{
                const curY = ev.type.includes('touch') ? ev.touches[0].clientY : ev.clientY;
                wObj.slPts = Math.max(8.0, origSL + (wObj.type === 'LONG' ? (curY - startY) * 0.9 : (startY - curY) * 0.9));
                renderSinglePosWidget(wObj);
            }}
            function onUp() {{
                window.removeEventListener('mousemove', onMove);
                window.removeEventListener('mouseup', onUp);
                window.removeEventListener('touchmove', onMove);
                window.removeEventListener('touchend', onUp);
            }}
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onUp);
            window.addEventListener('touchmove', onMove, {{ passive: true }});
            window.addEventListener('touchend', onUp);
        }}

        function startMovePosEntry(e, id) {{
            e.stopPropagation();
            const wObj = activePositionWidgets.find(w => w.id === id);
            if (!wObj) return;
            const startY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            const elem = wObj.domElem;
            const origTop = parseFloat(elem.style.top);
            function onMove(ev) {{
                const curY = ev.type.includes('touch') ? ev.touches[0].clientY : ev.clientY;
                elem.style.top = (origTop + (curY - startY)) + 'px';
            }}
            function onUp() {{
                window.removeEventListener('mousemove', onMove);
                window.removeEventListener('mouseup', onUp);
                window.removeEventListener('touchmove', onMove);
                window.removeEventListener('touchend', onUp);
            }}
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onUp);
            window.addEventListener('touchmove', onMove, {{ passive: true }});
            window.addEventListener('touchend', onUp);
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

        // Dynamic 3-Column Parallel Table
        const s = {hud_json};
        const table = document.getElementById('strategyBox');

        if (s) {{
            table.style.display = 'block';
            if (s.is_no_trade) {{
                table.innerHTML = `
                    <div class="box-drag-handle">::: DRAG STRATEGY HUD :::</div>
                    <table>
                        <tr><td class="label-cell">Session Status</td><td class="val-cell" colspan="2"><span class="tag-pill" style="background:#546e7a;">${{s.status}}</span></td></tr>
                        <tr><td class="label-cell">Reason</td><td class="val-cell" colspan="2" style="font-size:9px; color:#cfd3dc;">${{s.reason}}</td></tr>
                        <tr><td class="label-cell">Action</td><td class="val-cell text-green" colspan="2">${{s.action}}</td></tr>
                    </table>
                `;
            }} else {{
                const optRiskPts = Math.round((s.raw_risk_pts * activeDelta) * 10) / 10;
                const optSLPrice = Math.max(0.0, Math.round((activeRealLTP - optRiskPts) * 10) / 10);
                const totalMaxRisk = Math.round(optRiskPts * activeQty);

                const optT1Pts = Math.round((s.raw_target1_pts * activeDelta) * 10) / 10;
                const optT1Price = Math.round((activeRealLTP + optT1Pts) * 10) / 10;
                const totalT1Profit = Math.round(optT1Pts * activeQty);

                const optFinalPts = Math.round((s.raw_target_final_pts * activeDelta) * 10) / 10;
                const optFinalPrice = Math.round((activeRealLTP + optFinalPts) * 10) / 10;

                const optTrailPts = Math.round((s.raw_secured_pts * activeDelta) * 10) / 10;
                const optTrailPrice = Math.round((activeRealLTP + optTrailPts) * 10) / 10;

                const currentRunningPts = Math.round((s.current_pts * activeDelta) * 10) / 10;
                const currentTotalProfit = Math.round(currentRunningPts * activeQty);
                const currentSign = currentTotalProfit >= 0 ? '+' : '';
                const pnlColor = currentTotalProfit >= 0 ? '#089981' : '#f23645';

                table.innerHTML = `
                    <div class="box-drag-handle">::: DRAG STRATEGY HUD :::</div>
                    <table>
                        <thead>
                            <tr>
                                <th>Parameter</th>
                                <th>Index (Spot)</th>
                                <th>Option (${{activeContractLabel}})</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td class="label-cell">Call or Put</td>
                                <td class="val-cell"><span class="tag-pill" style="background:${{s.theme}}">${{s.call_or_put}}</span></td>
                                <td class="opt-cell"><span class="tag-pill" style="background:${{s.theme}}">${{s.call_or_put}}</span></td>
                            </tr>
                            <tr>
                                <td class="label-cell">Reason for trade</td>
                                <td class="val-cell" colspan="2" style="font-size:8.5px; color:#cfd3dc; text-align:right;">${{s.reason}}</td>
                            </tr>
                            <tr>
                                <td class="label-cell">Entry</td>
                                <td class="val-cell"><b>${{s.entry}}</b></td>
                                <td class="opt-cell">₹${{activeRealLTP.toFixed(2)}}</td>
                            </tr>
                            <tr>
                                <td class="label-cell">SL (risk pts)</td>
                                <td class="val-cell text-red">${{s.sl_risk}}</td>
                                <td class="opt-cell text-red">₹${{optSLPrice.toFixed(2)}} (-₹${{totalMaxRisk}})</td>
                            </tr>
                            <tr>
                                <td class="label-cell">Target 1 (3 ATR)</td>
                                <td class="val-cell text-green">${{s.target1}}</td>
                                <td class="opt-cell text-green">₹${{optT1Price.toFixed(2)}} (+₹${{totalT1Profit}})</td>
                            </tr>
                            <tr>
                                <td class="label-cell">Target Final</td>
                                <td class="val-cell text-green">${{s.target_final}}</td>
                                <td class="opt-cell text-green">₹${{optFinalPrice.toFixed(2)}}</td>
                            </tr>
                            <tr>
                                <td class="label-cell">Trailing SL</td>
                                <td class="val-cell text-trail">${{s.trailing_sl}}</td>
                                <td class="opt-cell text-trail">₹${{optTrailPrice.toFixed(2)}}</td>
                            </tr>
                            <tr>
                                <td class="label-cell">Trail Mode</td>
                                <td class="val-cell text-stage" colspan="2" style="text-align:right;">${{s.trail_stage}}</td>
                            </tr>
                            <tr>
                                <td class="label-cell">Secured / Live P&L</td>
                                <td class="val-cell text-green"><b>${{s.secured_pts}}</b></td>
                                <td class="opt-cell" style="font-weight:800; color:${{pnlColor}};">${{currentSign}}₹${{currentTotalProfit}}</td>
                            </tr>
                        </tbody>
                    </table>
                `;
            }}
        }}

        function makeDraggable(elem) {{
            let isDragging = false;
            let startX, startY, initLeft, initTop;

            function onStart(e) {{
                if (e.target.classList.contains('tv-touch-circle') || e.target.classList.contains('tv-center-handle') || e.target.classList.contains('tv-del-btn')) return;
                isDragging = true;
                const clientX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
                const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
                startX = clientX;
                startY = clientY;
                const rect = elem.getBoundingClientRect();
                initLeft = rect.left;
                initTop = rect.top;
                elem.style.right = 'auto';
                elem.style.bottom = 'auto';
                elem.style.left = initLeft + 'px';
                elem.style.top = initTop + 'px';
            }}
            function onMove(e) {{
                if (!isDragging) return;
                const clientX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
                const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
                elem.style.left = (initLeft + (clientX - startX)) + 'px';
                elem.style.top = (initTop + (clientY - startY)) + 'px';
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

        window.addEventListener('resize', () => {{
            chart.applyOptions({{ width: window.innerWidth, height: window.innerHeight }});
        }});
    </script>
</body>
</html>
"""

components.html(html_code, height=720, scrolling=False)

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
