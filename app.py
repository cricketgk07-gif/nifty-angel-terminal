import datetime
import json
import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    layout="wide", page_title="Nifty 50 Pro Terminal", page_icon="📈"
)

st.markdown(
    """
<style>
    #MainMenu, footer, header {visibility: hidden;}
    .block-container {padding: 0 !important; max-width: 100% !important;}
    body {background-color: #0b0e14;}
    div[data-testid="stRadio"] > div {
        flex-direction: row;
        gap: 6px;
        background: #131722;
        padding: 4px 8px;
        border-bottom: 1px solid #2a2e39;
    }
    div[data-testid="stRadio"] label {
        color: #787b86 !important;
        font-weight: 600 !important;
        font-size: 12px !important;
        padding: 2px 6px;
        cursor: pointer;
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


@st.cache_resource(ttl=28800)
def init_angel_session(api_key, client_code, pin, totp_sec):
    try:
        api = SmartConnect(api_key)
        totp = pyotp.TOTP(totp_sec).now()
        data = api.generateSession(client_code, pin, totp)
        if data.get("status"):
            return api
        return None
    except Exception:
        return None


api = init_angel_session(API_KEY, CLIENT_CODE, PIN, TOTP_SECRET)
if not api:
    st.error("Authentication failed. Please verify Streamlit Secrets.")
    st.stop()

# --- Top Navigation Bar: 1m to 1 Month ---
timeframe_dict = {
    "1m": ("ONE_MINUTE", 2),
    "3m": ("THREE_MINUTE", 4),
    "5m": ("FIVE_MINUTE", 5),
    "15m": ("FIFTEEN_MINUTE", 15),
    "30m": ("THIRTY_MINUTE", 30),
    "1h": ("ONE_HOUR", 60),
    "1D": ("ONE_DAY", 365),
    "1W": ("ONE_DAY", 730),  # Aggregated from Daily
    "1M": ("ONE_DAY", 1500),  # Aggregated from Daily
}

selected_label = st.radio(
    "Interval",
    options=list(timeframe_dict.keys()),
    index=2,  # Default to 5m
    horizontal=True,
    label_visibility="collapsed",
)
api_interval, lookback_days = timeframe_dict[selected_label]


def fetch_nifty_data(interval_code, days_back):
    now = datetime.datetime.now()
    from_date = (now - datetime.timedelta(days=days_back)).strftime(
        "%Y-%m-%d 09:15"
    )
    to_date = now.strftime("%Y-%m-%d %H:%M")

    resp = api.getCandleData(
        {
            "exchange": "NSE",
            "symboltoken": INDEX_TOKEN,
            "interval": interval_code,
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

    # Convert IST timestamp correctly to integer seconds Unix timestamp
    # (Angel One gives IST timestamps; we convert to proper UTC seconds for lightweight-charts)
    df["time"] = (
        (df["timestamp"] - datetime.timedelta(hours=5, minutes=30)).astype(
            "int64"
        )
        // 10**9
    )

    # Intraday filter (09:15 - 15:30) for intraday timeframes
    if interval_code not in ["ONE_DAY"]:
        df = df[
            (df["timestamp"].dt.time >= datetime.time(9, 15))
            & (df["timestamp"].dt.time <= datetime.time(15, 30))
        ].copy()

    df["date"] = df["timestamp"].dt.date
    today = df["date"].iloc[-1]

    # Session VWAP & 9-EMA
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vol_mult"] = df["tp"] * df["volume"].apply(
        lambda v: v if v > 0 else 1000.0
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
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()

    # Calculate Today's 15m ORB (09:15 - 09:30)
    today_df = df[df["date"] == today]
    orb_df = today_df[today_df["timestamp"].dt.time <= datetime.time(9, 30)]

    orb_h = (
        orb_df["high"].max()
        if len(orb_df) > 0
        else today_df.iloc[0:3]["high"].max()
    )
    orb_l = (
        orb_df["low"].min() if len(orb_df) > 0 else today_df.iloc[0:3]["low"].min()
    )

    df["orb_h"] = orb_h
    df["orb_l"] = orb_l

    return df, today, orb_h, orb_l


data_tuple = fetch_nifty_data(api_interval, lookback_days)
if not data_tuple or len(data_tuple[0]) == 0:
    st.info("Fetching market data...")
    st.stop()

df, today_date, orb_h, orb_l = data_tuple
today_df = df[df["date"] == today_date].copy()
curr = df.iloc[-1]

# Strategy Trailing SL & Signal Engine
markers = []
active_trade = None
completed_trade = None

for i in range(len(today_df)):
    row = today_df.iloc[i]
    t = row["timestamp"].time()
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    ema = row["ema9"]
    vwap_val = row["vwap"]

    if active_trade:
        trade_type = active_trade["type"]

        if trade_type == "CE":
            trail_ref = round(ema - 2.5, 1)
            if trail_ref > active_trade["current_sl"]:
                active_trade["current_sl"] = trail_ref

            if h >= active_trade["tp1"] and not active_trade["tp1_hit"]:
                active_trade["tp1_hit"] = True
                active_trade["current_sl"] = max(
                    active_trade["current_sl"], active_trade["entry"] + 2.0
                )

            if l <= active_trade["current_sl"] or (c < ema and c < o):
                exit_price = min(c, active_trade["current_sl"])
                pts = round(exit_price - active_trade["entry"], 1)
                markers.append(
                    {
                        "time": int(row["time"]),
                        "position": "aboveBar",
                        "color": "#f23645",
                        "shape": "arrowDown",
                        "text": f"EXIT SL @ {exit_price:.1f} ({pts:+.1f} pts)",
                    }
                )
                completed_trade = active_trade.copy()
                completed_trade["status"] = (
                    f"STOPPED OUT ({pts:+.1f} pts)"
                    if pts <= 0
                    else f"TRAIL HIT (+{pts:.1f} pts)"
                )
                completed_trade["exit_price"] = exit_price
                completed_trade["exit_pts"] = pts
                active_trade = None

            elif h >= active_trade["tp2"]:
                markers.append(
                    {
                        "time": int(row["time"]),
                        "position": "aboveBar",
                        "color": "#00bfa5",
                        "shape": "circle",
                        "text": f"TARGET 2 HIT @ {active_trade['tp2']:.1f}",
                    }
                )
                completed_trade = active_trade.copy()
                completed_trade["status"] = "TARGET 2 ACHIEVED"
                completed_trade["exit_price"] = active_trade["tp2"]
                completed_trade["exit_pts"] = round(
                    active_trade["tp2"] - active_trade["entry"], 1
                )
                active_trade = None

        elif trade_type == "PE":
            trail_ref = round(ema + 2.5, 1)
            if trail_ref < active_trade["current_sl"]:
                active_trade["current_sl"] = trail_ref

            if l <= active_trade["tp1"] and not active_trade["tp1_hit"]:
                active_trade["tp1_hit"] = True
                active_trade["current_sl"] = min(
                    active_trade["current_sl"], active_trade["entry"] - 2.0
                )

            if h >= active_trade["current_sl"] or (c > ema and c > o):
                exit_price = max(c, active_trade["current_sl"])
                pts = round(active_trade["entry"] - exit_price, 1)
                markers.append(
                    {
                        "time": int(row["time"]),
                        "position": "belowBar",
                        "color": "#00bfa5",
                        "shape": "arrowUp",
                        "text": f"EXIT SL @ {exit_price:.1f} ({pts:+.1f} pts)",
                    }
                )
                completed_trade = active_trade.copy()
                completed_trade["status"] = (
                    f"STOPPED OUT ({pts:+.1f} pts)"
                    if pts <= 0
                    else f"TRAIL HIT (+{pts:.1f} pts)"
                )
                completed_trade["exit_price"] = exit_price
                completed_trade["exit_pts"] = pts
                active_trade = None

            elif l <= active_trade["tp2"]:
                markers.append(
                    {
                        "time": int(row["time"]),
                        "position": "belowBar",
                        "color": "#00bfa5",
                        "shape": "circle",
                        "text": f"TARGET 2 HIT @ {active_trade['tp2']:.1f}",
                    }
                )
                completed_trade = active_trade.copy()
                completed_trade["status"] = "TARGET 2 ACHIEVED"
                completed_trade["exit_price"] = active_trade["tp2"]
                completed_trade["exit_pts"] = round(
                    active_trade["entry"] - active_trade["tp2"], 1
                )
                active_trade = None

    if t >= datetime.time(9, 30) and not active_trade and not completed_trade:
        if c > orb_h and c > vwap_val and c > ema:
            init_sl = round(orb_h - 5.0, 1)
            risk = round(c - init_sl, 1)
            tp1 = round(c + (risk * 1.5), 1)
            tp2 = round(c + (risk * 2.5), 1)
            active_trade = {
                "name": "ORB BREAKOUT (CE)",
                "type": "CE",
                "entry": round(c, 1),
                "init_sl": init_sl,
                "current_sl": init_sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp1_hit": False,
                "risk": risk,
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

        elif c < orb_l and c < vwap_val and c < ema:
            init_sl = round(orb_l + 5.0, 1)
            risk = round(init_sl - c, 1)
            tp1 = round(c - (risk * 1.5), 1)
            tp2 = round(c - (risk * 2.5), 1)
            active_trade = {
                "name": "ORB BREAKDOWN (PE)",
                "type": "PE",
                "entry": round(c, 1),
                "init_sl": init_sl,
                "current_sl": init_sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp1_hit": False,
                "risk": risk,
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

trade_for_hud = active_trade if active_trade else completed_trade

candles_json = json.dumps(
    [
        {
            "time": int(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
        for _, r in df.iterrows()
    ]
)

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
hud_json = json.dumps(trade_for_hud)

day_open = today_df.iloc[0]["open"]
chg = curr["close"] - day_open
chg_pct = (chg / day_open) * 100
chg_str = f"{chg:+.2f} ({chg_pct:+.2f}%)"
chg_color = "#089981" if chg >= 0 else "#f23645"

# TradingView lightweight canvas
html_code = f"""
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ background-color: #0b0e14; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; overflow: hidden; }}
        #chartContainer {{ width: 100vw; height: calc(100vh - 40px); position: relative; }}

        .tv-header {{
            position: absolute; top: 8px; left: 12px; z-index: 20; pointer-events: none;
            display: flex; flex-direction: column; gap: 2px;
        }}
        .tv-title {{ font-size: 14px; font-weight: 700; color: #d1d4dc; display: flex; align-items: center; gap: 6px; }}
        .badge {{ background: #2962ff; color: #fff; font-size: 10px; padding: 1px 5px; border-radius: 3px; font-weight: 600; }}
        .tv-price {{ font-size: 13px; font-weight: 600; }}
        .tv-ohlc {{ font-size: 11px; color: #787b86; display: flex; gap: 6px; font-family: monospace; }}

        .strategy-table {{
            position: absolute; bottom: 25px; right: 55px; z-index: 25;
            background: rgba(19, 23, 34, 0.95); border: 1px solid #2a2e39;
            border-radius: 6px; font-size: 11px; color: #d1d4dc; overflow: hidden;
            box-shadow: 0 4px 14px rgba(0,0,0,0.6);
        }}
        .strategy-table table {{ border-collapse: collapse; }}
        .strategy-table td {{ padding: 4px 8px; border-bottom: 1px solid #2a2e39; }}
        .strategy-table tr:last-child td {{ border-bottom: none; }}
        .td-tag {{ color: #fff; font-weight: bold; border-radius: 3px; padding: 2px 6px; text-align: center; }}
        .text-red {{ color: #f23645; }}
        .text-green {{ color: #089981; }}
        .text-trail {{ color: #2962ff; font-weight: bold; }}
    </style>
</head>
<body>
    <div id="chartContainer">
        <div class="tv-header">
            <div class="tv-title">
                <span class="badge">50</span> NIFTY 50 ({selected_label})
            </div>
            <div class="tv-price" style="color: {chg_color};">
                {curr['close']:.2f} <span style="font-size: 11px;">{chg_str}</span>
            </div>
            <div id="ohlcRow" class="tv-ohlc">
                <span>O: <b id="barO">{curr['open']:.2f}</b></span>
                <span>H: <b id="barH">{curr['high']:.2f}</b></span>
                <span>L: <b id="barL">{curr['low']:.2f}</b></span>
                <span>C: <b id="barC">{curr['close']:.2f}</b></span>
                <span>VWAP: <b id="barVWAP" style="color:#ab47bc;">{curr['vwap']:.2f}</b></span>
                <span>9-EMA: <b id="barEMA" style="color:#2962ff;">{curr['ema9']:.2f}</b></span>
            </div>
        </div>

        <div id="strategyTable" class="strategy-table" style="display: none;"></div>
    </div>

    <script>
        const chart = LightweightCharts.createChart(document.getElementById('chartContainer'), {{
            layout: {{
                background: {{ color: '#0b0e14' }},
                textColor: '#787b86',
                fontSize: 11,
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
                scaleMargins: {{ top: 0.12, bottom: 0.12 }}
            }},
            timeScale: {{
                borderColor: '#2a2e39',
                timeVisible: true,
                secondsVisible: false,
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

        // Set markers on the candles
        candleSeries.setMarkers({markers_json});

        candleSeries.createPriceLine({{
            price: {orb_h:.2f},
            color: '#089981',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'ORB HIGH'
        }});

        candleSeries.createPriceLine({{
            price: {orb_l:.2f},
            color: '#f23645',
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            axisLabelVisible: true,
            title: 'ORB LOW'
        }});

        // Render Strategy HUD Table
        const tData = {hud_json};
        if (tData) {{
            const isBuy = tData.type === 'CE';
            const theme = isBuy ? '#00bfa5' : '#f23645';
            const statusText = tData.status ? tData.status : 'IN TRADE (TRAILED)';
            const statusColor = (tData.exit_pts !== undefined && tData.exit_pts < 0) ? '#f23645' : '#089981';

            const table = document.getElementById('strategyTable');
            table.style.display = 'block';
            table.innerHTML = `
                <table>
                    <tr><td>Signal</td><td class="td-tag" style="background:${{theme}}">${{tData.name}}</td></tr>
                    <tr><td>Status</td><td style="font-weight:bold; color:${{statusColor}}">${{statusText}}</td></tr>
                    <tr><td>Entry</td><td><b>${{tData.entry.toFixed(1)}}</b></td></tr>
                    <tr><td>Initial SL</td><td class="text-red">${{tData.init_sl.toFixed(1)}}</td></tr>
                    <tr><td>Target 1</td><td class="text-green">${{tData.tp1.toFixed(1)}}</td></tr>
                    <tr><td>Target 2</td><td class="text-green">${{tData.tp2.toFixed(1)}}</td></tr>
                    <tr><td>Current Trail SL</td><td class="text-trail">${{tData.current_sl.toFixed(1)}}</td></tr>
                </table>
            `;
        }}

        // Make chart automatically focus and fit the latest candles on screen
        chart.timeScale().fitContent();

        // Crosshair dynamic OHLC inspector
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
            chart.applyOptions({{ width: window.innerWidth, height: window.innerHeight - 40 }});
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
