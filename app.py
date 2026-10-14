import streamlit as st
import json
import os
import requests
import streamlit.components.v1 as components

st.set_page_config(layout="wide", page_title="NIFTY Live Strategy HUD", initial_sidebar_state="collapsed")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

def read_from_redis():
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    url = f"{UPSTASH_REDIS_REST_URL}/get/nifty_live_state"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=2)
        if r.status_code == 200:
            val = r.json().get("result")
            if val:
                return json.loads(val)
    except Exception:
        pass
    return None

data = read_from_redis() or {}
candles = data.get("candles", [])
markers = data.get("markers", [])
history_cards = data.get("history_cards", {})
pivots = data.get("pivots", {})
orb_levels = data.get("orb_levels", {})

# Extract Key Levels for display badges
orb_h = orb_levels.get("orb_h", 0.0)
orb_l = orb_levels.get("orb_l", 0.0)
pivot_p = pivots.get("P", 0.0)
pdh = pivots.get("PDH", 0.0)
pdl = pivots.get("PDL", 0.0)

# Header metric badges
col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    st.metric("ORB HIGH", f"{orb_h:.1f}")
with col2:
    st.metric("ORB LOW", f"{orb_l:.1f}")
with col3:
    st.metric("PIVOT P", f"{pivot_p:.2f}")
with col4:
    st.metric("PREV DAY HIGH", f"{pdh:.2f}")
with col5:
    st.metric("PREV DAY LOW", f"{pdl:.2f}")

candles_json = json.dumps(candles)
markers_json = json.dumps(markers)
history_cards_json = json.dumps(history_cards)
pivots_json = json.dumps(pivots)
orb_json = json.dumps(orb_levels)

html_code = f"""
<!DOCTYPE html>
<html>
<head>
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        body {{
            margin: 0;
            padding: 0;
            background-color: #131722;
            color: #d1d4dc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            overflow: hidden;
        }}
        #tv_chart {{
            width: 100vw;
            height: 85vh;
        }}
    </style>
</head>
<body>
    <div id="tv_chart"></div>
    <script>
        const chartContainer = document.getElementById('tv_chart');
        const chart = LightweightCharts.createChart(chartContainer, {{
            layout: {{
                backgroundColor: '#131722',
                textColor: '#d1d4dc',
            }},
            grid: {{
                vertLines: {{ color: '#1f2434' }},
                horzLines: {{ color: '#1f2434' }},
            }},
            timeScale: {{
                timeVisible: true,
                secondsVisible: false,
            }},
            rightPriceScale: {{
                borderColor: '#2b2f3e',
            }}
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#089981',
            downColor: '#f23645',
            borderVisible: false,
            wickUpColor: '#089981',
            wickDownColor: '#f23645',
        }});

        const vwapSeries = chart.addLineSeries({{
            color: '#ff9800',
            lineWidth: 2,
            title: 'VWAP',
        }});

        const emaSeries = chart.addLineSeries({{
            color: '#2962ff',
            lineWidth: 2,
            title: 'EMA 9',
        }});

        const rawCandles = {candles_json};
        const rawMarkers = {markers_json};
        const pivots = {pivots_json};
        const orb = {orb_json};

        if (rawCandles && rawCandles.length > 0) {{
            const cData = [];
            const vData = [];
            const eData = [];

            rawCandles.forEach(c => {{
                cData.push({{ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close }});
                if (c.vwap) vData.push({{ time: c.time, value: c.vwap }});
                if (c.ema9) eData.push({{ time: c.time, value: c.ema9 }});
            }});

            candleSeries.setData(cData);
            vwapSeries.setData(vData);
            emaSeries.setData(eData);

            if (rawMarkers && rawMarkers.length > 0) {{
                candleSeries.setMarkers(rawMarkers);
            }}

            // Draw ORB Lines
            if (orb.orb_h) {{
                candleSeries.createPriceLine({{
                    price: orb.orb_h,
                    color: '#089981',
                    lineWidth: 2,
                    lineStyle: LightweightCharts.LineStyle.Solid,
                    axisLabelVisible: true,
                    title: 'ORB HIGH',
                }});
            }}
            if (orb.orb_l) {{
                candleSeries.createPriceLine({{
                    price: orb.orb_l,
                    color: '#f23645',
                    lineWidth: 2,
                    lineStyle: LightweightCharts.LineStyle.Solid,
                    axisLabelVisible: true,
                    title: 'ORB LOW',
                }});
            }}

            // Draw Pivot Lines
            if (pivots.P) {{
                candleSeries.createPriceLine({{
                    price: pivots.P,
                    color: '#fbc02d',
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Dashed,
                    axisLabelVisible: true,
                    title: 'Pivot P',
                }});
            }}
            if (pivots.PDH) {{
                candleSeries.createPriceLine({{
                    price: pivots.PDH,
                    color: '#29b6f6',
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Dotted,
                    axisLabelVisible: true,
                    title: 'PDH',
                }});
            }}
            if (pivots.PDL) {{
                candleSeries.createPriceLine({{
                    price: pivots.PDL,
                    color: '#ab47bc',
                    lineWidth: 1,
                    lineStyle: LightweightCharts.LineStyle.Dotted,
                    axisLabelVisible: true,
                    title: 'PDL',
                }});
            }}
        }}

        window.addEventListener('resize', () => {{
            chart.applyOptions({{ width: window.innerWidth, height: window.innerHeight * 0.85 }});
        }});
    </script>
</body>
</html>
"""

components.html(html_code, height=720)
