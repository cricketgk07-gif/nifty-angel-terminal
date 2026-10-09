import datetime
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pyotp
import requests
from SmartApi import SmartConnect
import streamlit as st

# Configure wide layout
st.set_page_config(
    layout="wide", page_title="Nifty Dual Strategy Terminal", page_icon="📈"
)

# Hide Streamlit header/footer for a clean UI on corporate screens
st.markdown(
    """
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    .block-container {padding-top: 1rem; padding-bottom: 1rem;}
</style>
""",
    unsafe_allow_html=True,
)

# --- Fetch credentials from Streamlit Secrets or Sidebar ---
API_KEY = st.secrets.get("API_KEY", "")
CLIENT_CODE = st.secrets.get("CLIENT_CODE", "")
PIN = st.secrets.get("PIN", "")
TOTP_SECRET = st.secrets.get("TOTP_SECRET", "")

INDEX_TOKEN = "99926000"  # Nifty 50 Spot Token


@st.cache_resource(ttl=28800)  # Session caches for 8 hours
def init_angel_session(api_key, client_code, pin, totp_sec):
    try:
        api = SmartConnect(api_key)
        totp = pyotp.TOTP(totp_sec).now()
        data = api.generateSession(client_code, pin, totp)
        if data.get("status"):
            return api
        return None
    except Exception as e:
        st.error(f"Authentication Error: {e}")
        return None


# Sidebar Controls
st.sidebar.title("Configuration")
refresh_rate = st.sidebar.slider(
    "Live Refresh Rate (Seconds)", min_value=5, max_value=60, value=15
)
st.sidebar.caption("Data feeds directly from Angel One live servers.")

api = init_angel_session(API_KEY, CLIENT_CODE, PIN, TOTP_SECRET)

if api is None:
    st.warning(
        "Please configure your Angel One Credentials in Streamlit Secrets."
    )
    st.stop()


def get_data():
    now = datetime.datetime.now()
    from_d = now.strftime("%Y-%m-%d 09:15")
    to_d = now.strftime("%Y-%m-%d %H:%M")

    resp = api.getCandleData(
        {
            "exchange": "NSE",
            "symboltoken": INDEX_TOKEN,
            "interval": "FIVE_MINUTE",
            "fromdate": from_d,
            "todate": to_d,
        }
    )

    if not resp.get("status") or not resp.get("data"):
        return None

    df = pd.DataFrame(
        resp["data"],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col])

    # 15m ORB
    df["orb_h"] = df.iloc[0:3]["high"].max() if len(df) >= 3 else df["high"].max()
    df["orb_l"] = df.iloc[0:3]["low"].min() if len(df) >= 3 else df["low"].min()

    # VWAP & 9 EMA
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vwap"] = df["typical"].expanding().mean()
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()

    return df


df = get_data()

if df is not None and len(df) > 0:
    curr = df.iloc[-1]
    orb_h = curr["orb_h"]
    orb_l = curr["orb_l"]
    vwap = curr["vwap"]
    c_close = curr["close"]

    # Dashboard Metrics Row
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Nifty Spot Price", f"{c_close:.1f}")
    m2.metric("ORB High (9:15-9:30)", f"{orb_h:.1f}")
    m3.metric("ORB Low (9:15-9:30)", f"{orb_l:.1f}")
    m4.metric("Session VWAP", f"{vwap:.1f}")

    # Build Plotly Candlestick
    fig = go.Figure()

    # Candlestick trace
    fig.add_trace(
        go.Candlestick(
            x=df["timestamp"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="NIFTY 50",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        )
    )

    # Indicator Overlays
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["vwap"],
            mode="lines",
            name="VWAP",
            line=dict(color="#ab47bc", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["ema9"],
            mode="lines",
            name="9 EMA Trail",
            line=dict(color="#42a5f5", width=1.5),
        )
    )

    # Static Reference Lines
    fig.add_hline(
        y=orb_h,
        line=dict(color="green", width=1.5, dash="dash"),
        annotation_text="ORB High",
        annotation_position="top right",
    )
    fig.add_hline(
        y=orb_l,
        line=dict(color="red", width=1.5, dash="dash"),
        annotation_text="ORB Low",
        annotation_position="bottom right",
    )

    fig.update_layout(
        height=620,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        paper_bgcolor="#131722",
        plot_bgcolor="#131722",
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
        ),
    )

    st.plotly_chart(fig, use_container_width=True)

    # Strategy Evaluation Engine
    st.subheader("Live Strategy Signal Monitor")
    c_body = abs(curr["close"] - curr["open"])
    u_wick = curr["high"] - max(curr["open"], curr["close"])
    l_wick = min(curr["open"], curr["close"]) - curr["low"]

    bull_trap = (
        curr["high"] > orb_h
        and curr["close"] < orb_h
        and curr["close"] < vwap
        and (u_wick >= c_body * 0.5)
    )
    bear_trap = (
        curr["low"] < orb_l
        and curr["close"] > orb_l
        and curr["close"] > vwap
        and (l_wick >= c_body * 0.5)
    )
    orb_break_ce = curr["close"] > orb_h and curr["close"] > vwap
    orb_break_pe = curr["close"] < orb_l and curr["close"] < vwap

    if bull_trap:
        st.error(
            f"🚨 **STRATEGY 2 TRIGGER: BULL TRAP (PE)** | Entry: {c_close:.1f} | Initial SL: {curr['high'] + 4.0:.1f} | Target: {orb_l:.1f}"
        )
    elif bear_trap:
        st.success(
            f"🚨 **STRATEGY 2 TRIGGER: BEAR TRAP (CE)** | Entry: {c_close:.1f} | Initial SL: {curr['low'] - 4.0:.1f} | Target: {orb_h:.1f}"
        )
    elif orb_break_ce:
        st.info(
            f"⚡ **STRATEGY 1 TRIGGER: ORB BREAKOUT (CE)** | Entry: {c_close:.1f} | SL: {orb_h - 4.0:.1f}"
        )
    elif orb_break_pe:
        st.info(
            f"⚡ **STRATEGY 1 TRIGGER: ORB BREAKDOWN (PE)** | Entry: {c_close:.1f} | SL: {orb_l + 4.0:.1f}"
        )
    else:
        st.caption("Status: Scanning market structure... No active triggers.")

else:
    st.info("Market session waiting for candle accumulation.")

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
