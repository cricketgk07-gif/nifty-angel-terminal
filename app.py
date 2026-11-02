# app.py
import json
import os
import streamlit as st
import streamlit.components.v1 as components
from upstash_redis import Redis
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="NIFTY ORB Master Terminal",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Connect to Upstash Redis (bridges Oracle VM to Streamlit Cloud)
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
redis_client = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN) if UPSTASH_URL and UPSTASH_TOKEN else None

def read_redis_state():
    try:
        if redis_client:
            val = redis_client.get("nifty_live_state")
            if val:
                return json.loads(val) if isinstance(val, str) else val
    except Exception:
        pass
    return {}

state_data = read_redis_state()
if not isinstance(state_data, dict):
    state_data = {}

ltp = float(state_data.get("ltp", 24045.0))
candles = state_data.get("candles", [])
markers = state_data.get("markers", [])
pivots = state_data.get("pivots", {})
orb_levels = state_data.get("orb_levels", {})
fib_lines = state_data.get("fib_lines", {})
prev_day_summary = state_data.get("prev_day_summary", {})
hud = state_data.get("hud", {})

# Header Bar
col_title, col_budget, col_refresh = st.columns([3, 2, 1])
with col_title:
    st.markdown(
        f"<h3 style='margin:0; color:#e0e0e0;'>NIFTY ORB Master Terminal "
        f"<span style='color:#00e676; font-size:22px;'>₹{ltp:.2f}</span></h3>",
        unsafe_allow_html=True,
    )

with col_refresh:
    if st.button("🔄 Force Refresh"):
        st.rerun()

# Chart Canvas Rendering Component
candles_json = json.dumps(candles)
markers_json = json.dumps(markers)
pivots_json = json.dumps(pivots)
orb_json = json.dumps(orb_levels)

html_code = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    body, html {{ margin: 0; padding: 0; width: 100%; height: 100%; background-color: #131722; color: #d1d4dc; }}
    #chart_zone {{ width: 100%; height: 600px; position: relative; }}
  </style>
</head>
<body>
  <div id="chart_zone"></div>
  <script>
    window.addEventListener('DOMContentLoaded', () => {{
      const rawCandles = {candles_json} || [];
      const rawMarkers = {markers_json} || [];
      const pivots = {pivots_json} || {{}};
      const orbLevels = {orb_json} || {{}};
      const IST_OFFSET = 19800;

      const formattedCandles = rawCandles.map(c => ({{
        time: c.time + IST_OFFSET, open: c.open, high: c.high, low: c.low, close: c.close, vwap: c.vwap, ema20: c.ema20
      }}));

      const container = document.getElementById('chart_zone');
      const chart = LightweightCharts.createChart(container, {{
        width: container.clientWidth || window.innerWidth,
        height: 600,
        layout: {{ background: {{ color: '#131722' }}, textColor: '#d1d4dc' }},
        grid: {{ vertLines: {{ color: '#1f2430' }}, horzLines: {{ color: '#1f2430' }} }},
        rightPriceScale: {{ borderColor: '#2a2e39', autoScale: true }},
        timeScale: {{ borderColor: '#2a2e39', timeVisible: true }}
      }});

      const candleSeries = chart.addCandlestickSeries({{ upColor: '#089981', downColor: '#f23645' }});
      candleSeries.setData(formattedCandles);

      const vwapSeries = chart.addLineSeries({{ color: '#9c27b0', lineWidth: 2, title: 'VWAP' }});
      vwapSeries.setData(formattedCandles.filter(d => d.vwap).map(d => ({{ time: d.time, value: d.vwap }})));

      const emaSeries = chart.addLineSeries({{ color: '#2962ff', lineWidth: 1.5, title: 'EMA 20' }});
      emaSeries.setData(formattedCandles.filter(d => d.ema20).map(d => ({{ time: d.time, value: d.ema20 }})));

      if (rawMarkers.length > 0) {{
        candleSeries.setMarkers(rawMarkers.map(m => ({{ ...m, time: m.time + IST_OFFSET }})));
      }}

      if (orbLevels.orb_h) {{
        candleSeries.createPriceLine({{ price: parseFloat(orbLevels.orb_h), color: '#00e676', lineWidth: 2, title: 'ORB HIGH' }});
      }}
      if (orbLevels.orb_l) {{
        candleSeries.createPriceLine({{ price: parseFloat(orbLevels.orb_l), color: '#ff5252', lineWidth: 2, title: 'ORB LOW' }});
      }}

      chart.timeScale().fitContent();
    }});
  </script>
</body>
</html>
"""
components.html(html_code, height=620, scrolling=False)
