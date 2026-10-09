import datetime
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pyotp
import requests
from SmartApi import SmartConnect
import streamlit as st

st.set_page_config(
    layout="wide", page_title="Nifty TradingView Pro Terminal", page_icon="📈"
)

# Custom Styling for TradingView look & feel
st.markdown(
    """
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .block-container {padding-top: 0.5rem; padding-bottom: 0.5rem; padding-left: 1rem; padding-right: 1rem;}
    .stMetric {background-color: #1e222d; padding: 10px; border-radius: 6px; border: 1px solid #2a2e39;}
</style>
""",
    unsafe_allow_html=True,
)

# --- Secrets ---
API_KEY = st.secrets.get("API_KEY", "")
CLIENT_CODE = st.secrets.get("CLIENT_CODE", "")
PIN = st.secrets.get("PIN", "")
TOTP_SECRET = st.secrets.get("TOTP_SECRET", "")
INDEX_TOKEN = "99926000"  # Nifty 50 Spot Token


@st.cache_resource(ttl=28800)
def init_angel_session(api_key, client_code, pin, totp_sec):
    try:
        api = SmartConnect(api_key)
        totp = pyotp.TOTP(totp_sec).now()
        data = api.generateSession(client_code, pin, totp)
        if data.get("status"):
            return api
        return None
    except Exception as e:
        st.error(f"Auth Error: {e}")
        return None


api = init_angel_session(API_KEY, CLIENT_CODE, PIN, TOTP_SECRET)
if not api:
    st.error("Authentication failed. Please verify Streamlit Secrets.")
    st.stop()

# --- Sidebar Controls ---
st.sidebar.header("Chart & Strategy Controls")
timeframe = st.sidebar.selectbox(
    "Timeframe",
    options=["FIVE_MINUTE", "ONE_MINUTE", "THREE_MINUTE", "FIFTEEN_MINUTE"],
    index=0,
)
history_days = st.sidebar.slider(
    "Historical Data Lookback (Days)", min_value=1, max_value=7, value=2
)
refresh_rate = st.sidebar.slider(
    "Auto-Refresh Rate (Seconds)", min_value=5, max_value=60, value=15
)


def fetch_nifty_data(tf, days):
    now = datetime.datetime.now()
    from_date = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d 09:15")
    to_date = now.strftime("%Y-%m-%d %H:%M")

    resp = api.getCandleData(
        {
            "exchange": "NSE",
            "symboltoken": INDEX_TOKEN,
            "interval": tf,
            "fromdate": from_date,
            "todate": to_date,
        }
    )

    if not resp.get("status") or not resp.get("data"):
        return None

    df = pd.DataFrame(
        resp["data"],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    # Date column to separate intraday sessions
    df["date"] = df["timestamp"].dt.date
    today = df["date"].iloc[-1]
    today_mask = df["date"] == today

    # Session VWAP (Resets each morning at 09:15)
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vol_mult"] = (
        df["typical_price"]
        * df["volume"].apply(lambda v: v if v > 0 else 1000.0)
    )
    df["cum_vol"] = (
        df.groupby("date")["volume"]
        .apply(lambda s: s.replace(0, 1000).cumsum())
        .reset_index(level=0, drop=True)
    )
    df["cum_vp"] = (
        df.groupby("date")["vol_mult"]
        .cumsum()
        .reset_index(level=0, drop=True)
    )
    df["vwap"] = df["cum_vp"] / df["cum_vol"]

    # 9 EMA
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()

    # Calculate Today's 15-Minute Opening Range (09:15 - 09:30)
    today_df = df[today_mask]
    orb_window = today_df[
        today_df["timestamp"].dt.time <= datetime.time(9, 30)
    ]
    if len(orb_window) > 0:
        orb_h = orb_window["high"].max()
        orb_l = orb_window["low"].min()
    else:
        orb_h = today_df.iloc[0:3]["high"].max()
        orb_l = today_df.iloc[0:3]["low"].min()

    df["orb_h"] = orb_h
    df["orb_l"] = orb_l

    return df, today


data_res = fetch_nifty_data(timeframe, history_days)
if data_res is None or len(data_res[0]) == 0:
    st.warning("Awaiting market ticks...")
    st.stop()

df, today_date = data_res
today_df = df[df["date"] == today_date].copy()
curr = df.iloc[-1]
orb_h = curr["orb_h"]
orb_l = curr["orb_l"]

# --- Strategy Evaluation & Dynamic Moving Targets ---
signals = []  # tuple: (index, type, entry, sl, t1, t2)
active_trade = None

for i in range(len(today_df)):
    row = today_df.iloc[i]
    t = row["timestamp"].time()
    if t < datetime.time(9, 30):
        continue  # Skip initial ORB accumulation window

    c_open, c_high, c_low, c_close = (
        row["open"],
        row["high"],
        row["low"],
        row["close"],
    )
    vwap_val = row["vwap"]
    body = abs(c_close - c_open)
    u_wick = c_high - max(c_open, c_close)
    l_wick = min(c_open, c_close) - c_low

    # Strategy 2: Bull Trap (Fake Breakout at ORB High with Rejection Wick)
    if (
        c_high > orb_h
        and c_close < orb_h
        and c_close < vwap_val
        and (u_wick >= body * 0.45)
    ):
        sl = round(c_high + 4.0, 1)
        risk = sl - c_close
        t1 = round(c_close - (risk * 1.5), 1)
        t2 = round(orb_l, 1)
        signals.append((row["timestamp"], "BEAR TRAP (PE)", c_close, sl, t1, t2))
        active_trade = {
            "type": "PUT (PE)",
            "strategy": "Bull Trap Rejection",
            "entry": c_close,
            "sl": sl,
            "t1": t1,
            "t2": t2,
        }

    # Strategy 2: Bear Trap (Fake Breakdown at ORB Low with Rejection Wick)
    elif (
        c_low < orb_l
        and c_close > orb_l
        and c_close > vwap_val
        and (l_wick >= body * 0.45)
    ):
        sl = round(c_low - 4.0, 1)
        risk = c_close - sl
        t1 = round(c_close + (risk * 1.5), 1)
        t2 = round(orb_h, 1)
        signals.append((row["timestamp"], "BEAR TRAP (CE)", c_close, sl, t1, t2))
        active_trade = {
            "type": "CALL (CE)",
            "strategy": "Bear Trap Rejection",
            "entry": c_close,
            "sl": sl,
            "t1": t1,
            "t2": t2,
        }

    # Strategy 1: Clean Breakout
    elif c_close > orb_h and c_close > vwap_val and not active_trade:
        sl = round(orb_h - 4.0, 1)
        risk = c_close - sl
        t1 = round(c_close + (risk * 1.5), 1)
        t2 = round(c_close + (risk * 2.5), 1)
        signals.append(
            (row["timestamp"], "ORB BREAKOUT (CE)", c_close, sl, t1, t2)
        )
        active_trade = {
            "type": "CALL (CE)",
            "strategy": "ORB Breakout",
            "entry": c_close,
            "sl": sl,
            "t1": t1,
            "t2": t2,
        }

    # Strategy 1: Clean Breakdown
    elif c_close < orb_l and c_close < vwap_val and not active_trade:
        sl = round(orb_l + 4.0, 1)
        risk = sl - c_close
        t1 = round(c_close - (risk * 1.5), 1)
        t2 = round(c_close - (risk * 2.5), 1)
        signals.append(
            (row["timestamp"], "ORB BREAKDOWN (PE)", c_close, sl, t1, t2)
        )
        active_trade = {
            "type": "PUT (PE)",
            "strategy": "ORB Breakdown",
            "entry": c_close,
            "sl": sl,
            "t1": t1,
            "t2": t2,
        }

# Trailing SL update (Using 9 EMA)
if active_trade:
    current_trail = (
        round(curr["ema9"] - 3.0, 1)
        if "CE" in active_trade["type"]
        else round(curr["ema9"] + 3.0, 1)
    )
    active_trade["trailing_sl"] = current_trail

# --- Top Header & Live Metrics ---
chg = curr["close"] - df.iloc[0]["open"]
chg_pct = (chg / df.iloc[0]["open"]) * 100

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("NIFTY 50", f"{curr['close']:.2f}", f"{chg:+.2f} ({chg_pct:+.2f}%)")
c2.metric("ORB High (09:15-09:30)", f"{orb_h:.2f}")
c3.metric("ORB Low (09:15-09:30)", f"{orb_l:.2f}")
c4.metric("Session VWAP", f"{curr['vwap']:.2f}")
c5.metric("9 EMA (Trail Ref)", f"{curr['ema9']:.2f}")

# --- TradingView Candlestick Chart ---
fig = go.Figure()

# 1. Main Candlesticks
fig.add_trace(
    go.Candlestick(
        x=df["timestamp"],
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name="NIFTY",
        increasing_line_color="#089981",
        decreasing_line_color="#f23645",
        increasing_fillcolor="#089981",
        decreasing_fillcolor="#f23645",
    )
)

# 2. VWAP & 9 EMA
fig.add_trace(
    go.Scatter(
        x=df["timestamp"],
        y=df["vwap"],
        mode="lines",
        name="VWAP",
        line=dict(color="#f6a000", width=1.5),
    )
)
fig.add_trace(
    go.Scatter(
        x=df["timestamp"],
        y=df["ema9"],
        mode="lines",
        name="9 EMA Trail",
        line=dict(color="#2962ff", width=1.2),
    )
)

# 3. Horizontal Level Lines for Today's ORB Range
fig.add_trace(
    go.Scatter(
        x=[today_df["timestamp"].iloc[0], today_df["timestamp"].iloc[-1]],
        y=[orb_h, orb_h],
        mode="lines+text",
        name="ORB High",
        line=dict(color="#089981", width=1.5, dash="dash"),
        text=["", f"ORB High ({orb_h:.1f})"],
        textposition="top right",
    )
)
fig.add_trace(
    go.Scatter(
        x=[today_df["timestamp"].iloc[0], today_df["timestamp"].iloc[-1]],
        y=[orb_l, orb_l],
        mode="lines+text",
        name="ORB Low",
        line=dict(color="#f23645", width=1.5, dash="dash"),
        text=["", f"ORB Low ({orb_l:.1f})"],
        textposition="bottom right",
    )
)

# 4. Paint Strategy Signal Markers Directly on Chart Bars
for sig_time, sig_type, price, sl, t1, t2 in signals:
    is_buy = "CE" in sig_type
    fig.add_annotation(
        x=sig_time,
        y=price,
        text="BUY CE" if is_buy else "BUY PE",
        showarrow=True,
        arrowhead=2,
        arrowsize=1.2,
        arrowcolor="#089981" if is_buy else "#f23645",
        arrowwidth=2,
        ax=0,
        ay=35 if is_buy else -35,
        font=dict(color="#ffffff", size=10),
        bgcolor="#089981" if is_buy else "#f23645",
        borderpad=3,
        bordercolor="#ffffff",
    )

# 5. TradingView Cursor / Unified Crosshair & Layout Settings
fig.update_layout(
    height=640,
    margin=dict(l=10, r=40, t=10, b=10),
    template="plotly_dark",
    paper_bgcolor="#131722",
    plot_bgcolor="#131722",
    hovermode="x unified",  # Displays full OHLC, VWAP, EMA at cursor position
    xaxis=dict(
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        showline=True,
        showgrid=True,
        gridcolor="#1e222d",
        rangeslider_visible=False,
    ),
    yaxis=dict(
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        showline=True,
        showgrid=True,
        gridcolor="#1e222d",
        side="right",  # Right-side price axis like TradingView
    ),
    legend=dict(
        orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0
    ),
)

st.plotly_chart(fig, use_container_width=True)

# --- Active Strategy HUD: Target, Trailing SL, and PnL Tracker ---
st.subheader("⚡ Live Trade Execution HUD")

if active_trade:
    is_ce = "CE" in active_trade["type"]
    entry = active_trade["entry"]
    points = (curr["close"] - entry) if is_ce else (entry - curr["close"])
    color_class = "🟢" if points >= 0 else "🔴"

    t_col1, t_col2, t_col3, t_col4, t_col5 = st.columns(5)
    t_col1.metric("Active Strategy", active_trade["strategy"])
    t_col2.metric("Execution Price", f"{entry:.1f}")
    t_col3.metric("Target 1 (1:1.5)", f"{active_trade['t1']:.1f}")
    t_col4.metric(
        "Target 2 / Final",
        f"{active_trade['t2']:.1f}",
        f"{points:+.1f} Pts PnL",
    )
    t_col5.metric(
        "Dynamic Trailing SL",
        f"{active_trade['trailing_sl']:.1f}",
        "9-EMA Trailed",
    )
else:
    st.info(
        "🔍 Market Structure Active: Price moving between session boundaries. Awaiting breakout close or wick trap trigger."
    )

# Auto-reloader script
st.markdown(
    f"""
    <script>
        setTimeout(function(){{
            window.location.reload();
        }}, {refresh_rate * 1000});
    </script>
""",
    unsafe_allow_html=True,
)
