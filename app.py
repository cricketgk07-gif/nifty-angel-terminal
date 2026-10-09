import datetime
import json
import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    layout="wide",
    page_title="Nifty 50 Morning Alert Terminal",
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
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
    }
    body {
        background-color: #0c0d10;
        margin: 0;
        overflow: hidden;
        touch-action: none;
        user-select: none;
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


def fetch_nifty_candles():
    now = datetime.datetime.now()
    from_date = (now - datetime.timedelta(days=12)).strftime("%Y-%m-%d 09:15")
    to_date = now.strftime("%Y-%m-%d %H:%M")

    try:
        resp = api.getCandleData(
            {
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "FIVE_MINUTE",
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

    # NSE Intraday Only (09:15 - 15:30)
    df = df[
        (df["dt"].dt.time >= datetime.time(9, 15))
        & (df["dt"].dt.time <= datetime.time(15, 30))
    ].copy()

    if len(df) == 0:
        return None

    # Precise UTC seconds to match TradingView IST axis
    t_clean = df["dt"].dt.tz_localize(None)
    df["time"] = (
        (t_clean - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)
    ).astype(int)
    df["date"] = df["dt"].dt.date

    # Purple Session VWAP
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

    return df


df = fetch_nifty_candles()
if df is None or len(df) == 0:
    st.info("Market feed is loading...")
    st.stop()

# --- Pine Script Exact Strategy Logic: Option Morning Window Alert Suite (9:30 - 10:15) ---
cards_data = []
lines_data = []
latest_day_info = {}

grouped = df.groupby("date")

for session_date, day_df in grouped:
    # 09:15 - 09:30 Accumulation Window
    orb_window = day_df[day_df["dt"].dt.time <= datetime.time(9, 30)]
    if len(orb_window) == 0:
        continue

    day_orb_h = round(float(orb_window["high"].max()), 2)
    day_orb_l = round(float(orb_window["low"].min()), 2)

    # Store lines for this session
    start_time = int(day_df.iloc[0]["time"])
    end_time = int(day_df.iloc[-1]["time"])
    lines_data.append(
        {
            "start": start_time,
            "end": end_time,
            "high": day_orb_h,
            "low": day_orb_l,
        }
    )

    trade_taken = False
    trade_info = None

    for idx in range(len(day_df)):
        row = day_df.iloc[idx]
        t = row["dt"].time()
        c = row["close"]
        vwap_val = row["vwap"]

        # Morning Window: 09:30 to 10:15 strictly (1 Trade Max)
        if (
            datetime.time(9, 30) < t <= datetime.time(10, 15)
            and not trade_taken
        ):
            # MORNING CE BREAKOUT
            if c > day_orb_h and c > vwap_val:
                sl = round(day_orb_h - 4.5, 1)
                risk = round(c - sl, 1)
                tp = round(c + (risk * 2.0), 1)
                cards_data.append(
                    {
                        "time": int(row["time"]),
                        "price": float(row["low"]),
                        "type": "CE",
                        "name": "MORNING CE",
                        "entry": round(c, 1),
                        "sl": sl,
                        "tp": tp,
                        "risk": risk,
                        "gain": round(tp - c, 1),
                    }
                )
                trade_taken = True
                trade_info = {"type": "CE", "entry": c, "sl": sl, "tp": tp}

            # MORNING PE BREAKDOWN
            elif c < day_orb_l and c < vwap_val:
                sl = round(day_orb_l + 4.5, 1)
                risk = round(sl - c, 1)
                tp = round(c - (risk * 2.0), 1)
                cards_data.append(
                    {
                        "time": int(row["time"]),
                        "price": float(row["high"]),
                        "type": "PE",
                        "name": "MORNING PE",
                        "entry": round(c, 1),
                        "sl": sl,
                        "tp": tp,
                        "risk": risk,
                        "gain": round(c - tp, 1),
                    }
                )
                trade_taken = True
                trade_info = {"type": "PE", "entry": c, "sl": sl, "tp": tp}

    # Record latest active session status for bottom table
    latest_day_info = {
        "trades_taken": "1 / 1 Max" if trade_taken else "0 / 1 Max",
        "orb_h": day_orb_h,
        "orb_l": day_orb_l,
        "status": "CLOSED (Do Not Trade)"
        if (day_df.iloc[-1]["dt"].time() > datetime.time(10, 15) or trade_taken)
        else "SCANNING WINDOW",
    }

# Serialization
curr = df.iloc[-1]
today_df = df[df["date"] == df["date"].iloc[-1]]
day_open = today_df.iloc[0]["open"]
chg = curr["close"] - day_open
chg_pct = (chg / day_open) * 100
chg_str = f"{chg:+.2f} ({chg_pct:+.2f}%)"
chg_color = "#089981" if chg >= 0 else "#f23645"

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

cards_json = json.dumps(cards_data)
lines_json = json.dumps(lines_data)
status_json = json.dumps(latest_day_info)

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
            background-color: #000000;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            overflow: hidden;
            touch-action: none;
            user-select: none;
        }}
        #chartArea {{ width: 100vw; height: 100vh; position: relative; }}

        /* Top Bar Header */
        .tv-top {{
            position: absolute; top: 10px; left: 14px; z-index: 30; pointer-events: none;
            display: flex; flex-direction: column; gap: 3px;
        }}
        .sym-row {{ display: flex; align-items: center; gap: 6px; }}
        .badge {{ background: #2962ff; color: #fff; font-size: 11px; padding: 1px 5px; border-radius: 3px; font-weight: 700; }}
        .sym-title {{ font-size: 15px; font-weight: 700; color: #d1d4dc; }}
        .sym-p {{ font-size: 13px; font-weight: 600; color: {chg_color}; }}
        .strat-subtitle {{ font-size: 11px; color: #787b86; }}

        /* Exact Strategy Cards Anchored to Candles */
        .candle-card {{
            position: absolute; z-index: 25; pointer-events: none;
            padding: 6px 10px; border-radius: 4px; font-size: 10px; font-weight: 700;
            text-align: center; line-height: 1.35; transform: translate(-50%, 0);
            box-shadow: 0 4px 12px rgba(0,0,0,0.6);
        }}
        .card-ce {{
            background: #2e7d32; color: #ffffff; border: 1px solid #4caf50;
        }}
        .card-pe {{
            background: #c62828; color: #ffffff; border: 1px solid #ef5350;
        }}

        /* TradingView Bottom Info Table */
        .tv-hud {{
            position: absolute; bottom: 25px; left: 50%; transform: translateX(-50%);
            z-index: 30; background: rgba(18, 20, 26, 0.95);
            border: 1px solid #2a2e39; border-radius: 6px; font-size: 11px;
            color: #d1d4dc; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.8);
        }}
        .tv-hud table {{ border-collapse: collapse; }}
        .tv-hud td {{ padding: 4px 10px; border-bottom: 1px solid #222631; white-space: nowrap; }}
        .tv-hud tr:last-child td {{ border-bottom: none; }}
        .tag-status {{ background: #2a2e39; color: #ff5252; font-weight: 700; border-radius: 3px; padding: 2px 6px; }}
        .tag-val {{ font-weight: 700; color: #ffffff; text-align: right; }}
    </style>
</head>
<body>
    <div id="chartArea">
        <div class="tv-top">
            <div class="sym-row">
                <span class="badge">50</span>
                <span class="sym-title">Nifty 50 Index</span>
            </div>
            <div class="sym-p">{curr['close']:.2f} {chg_str}</div>
            <div class="strat-subtitle">Option Morning Window Alert Suite (9:30 - 10:15)</div>
        </div>

        <!-- Dynamic Candle-Anchored Cards Container -->
        <div id="cardsLayer"></div>

        <!-- TradingView Fixed Strategy Table -->
        <div class="tv-hud" id="hudTable"></div>
    </div>

    <script>
        const container = document.getElementById('chartArea');
        const chart = LightweightCharts.createChart(container, {{
            width: window.innerWidth,
            height: window.innerHeight,
            layout: {{
                background: {{ color: '#000000' }},
                textColor: '#787b86',
                fontSize: 11,
            }},
            grid: {{
                vertLines: {{ color: '#13151b' }},
                horzLines: {{ color: '#13151b' }}
            }},
            crosshair: {{
                mode: LightweightCharts.CrosshairMode.Normal,
                vertLine: {{ color: '#555e6f', width: 1, style: 3 }},
                horzLine: {{ color: '#555e6f', width: 1, style: 3 }}
            }},
            rightPriceScale: {{
                borderColor: '#2a2e39',
                autoScale: true,
                scaleMargins: {{ top: 0.1, bottom: 0.15 }},
                alignLabels: true
            }},
            timeScale: {{
                borderColor: '#2a2e39',
                timeVisible: true,
                secondsVisible: false,
                rightOffset: 10
            }},
            localization: {{
                priceFormatter: p => p.toFixed(2)
            }}
        }});

        // Candlesticks (Volume deleted to prevent vertical screen overflow)
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

        // Purple Session VWAP
        const vwapSeries = chart.addLineSeries({{
            color: '#ab47bc',
            lineWidth: 2,
            title: 'VWAP',
            priceFormat: {{ type: 'price', precision: 2, minMove: 0.05 }}
        }});
        vwapSeries.setData({vwap_json});

        // Latest ORB Horizontal Level Lines (Green High / Red Low)
        const lines = {lines_json};
        if (lines.length > 0) {{
            const lastLine = lines[lines.length - 1];
            candleSeries.createPriceLine({{
                price: lastLine.high,
                color: '#2e7d32',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Solid,
                axisLabelVisible: true,
                title: 'ORB HIGH'
            }});
            candleSeries.createPriceLine({{
                price: lastLine.low,
                color: '#c62828',
                lineWidth: 2,
                lineStyle: LightweightCharts.LineStyle.Solid,
                axisLabelVisible: true,
                title: 'ORB LOW'
            }});
        }}

        // Render Strategy Table
        const sData = {status_json};
        document.getElementById('hudTable').innerHTML = `
            <table>
                <tr>
                    <td>Window (9:30-10:15)</td>
                    <td><span class="tag-status">${{sData.status}}</span></td>
                </tr>
                <tr>
                    <td>Trades Taken</td>
                    <td class="tag-val">${{sData.trades_taken}}</td>
                </tr>
                <tr>
                    <td>Daily ORB High</td>
                    <td class="tag-val" style="color: #4caf50;">${{sData.orb_h.toFixed(1)}}</td>
                </tr>
                <tr>
                    <td>Daily ORB Low</td>
                    <td class="tag-val" style="color: #ef5350;">${{sData.orb_l.toFixed(1)}}</td>
                </tr>
            </table>
        `;

        // Candle-Anchored Moving Cards (Tracks zoom & pan)
        const cards = {cards_json};
        const cardsLayer = document.getElementById('cardsLayer');

        function updateCardPositions() {{
            cardsLayer.innerHTML = '';
            cards.forEach(card => {{
                const x = chart.timeScale().timeToCoordinate(card.time);
                const y = candleSeries.priceToCoordinate(card.price);

                if (x !== null && y !== null && x >= 0 && x <= window.innerWidth && y >= 0 && y <= window.innerHeight) {{
                    const el = document.createElement('div');
                    el.className = 'candle-card ' + (card.type === 'CE' ? 'card-ce' : 'card-pe');
                    el.innerHTML = `
                        ${{card.name}}<br>
                        Entry: ${{card.entry.toFixed(1)}}<br>
                        SL: ${{card.sl.toFixed(1)}}<br>
                        TP: ${{card.tp.toFixed(1)}}
                    `;
                    el.style.left = x + 'px';
                    if (card.type === 'CE') {{
                        el.style.top = (y + 12) + 'px';
                    }} else {{
                        el.style.bottom = (window.innerHeight - y + 12) + 'px';
                    }}
                    cardsLayer.appendChild(el);
                }}
            }});
        }}

        // Keep cards synced whenever you zoom or pan
        chart.timeScale().subscribeVisibleTimeRangeChange(updateCardPositions);
        updateCardPositions();

        window.addEventListener('resize', () => {{
            chart.applyOptions({{ width: window.innerWidth, height: window.innerHeight }});
            updateCardPositions();
        }});
    </script>
</body>
</html>
"""

components.html(html_code, height=920, scrolling=False)

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
