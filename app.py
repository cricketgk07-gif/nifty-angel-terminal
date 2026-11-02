# app.py
import html
import json
import os
import datetime
import zoneinfo
import streamlit as st
import streamlit.components.v1 as components

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

st.set_page_config(
    page_title="NIFTY ORB Master Terminal (59.2k)",
    layout="wide",
    initial_sidebar_state="collapsed",
)

STATE_FILE = "nifty_live_state.json"
BUDGET_CEILING_DEFAULT = 80.0

def read_redis_key(key: str):
    """Reads state locally from Oracle VM file storage."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def write_redis_key(key: str, value):
    """Writes state locally to Oracle VM file storage."""
    try:
        state = read_redis_key("nifty_live_state")
        if not isinstance(state, dict):
            state = {}
        state[key] = value
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

raw_state = read_redis_key("nifty_live_state")
state_data = raw_state if isinstance(raw_state, dict) else {}

ltp = float(state_data.get("ltp", 23069.15))
candles = state_data.get("candles", [])
markers = state_data.get("markers", [])
history_cards = state_data.get("history_cards", {})
pivots = state_data.get("pivots", {"P": 22897.05, "PDH": 23056.4, "PDL": 22637.4})
orb_levels = state_data.get("orb_levels", {"orb_h": 23175.1, "orb_l": 22970.85})
fib_lines = state_data.get("fib_lines", {"fib_0": 22970.85, "fib_100": 23175.1})
prev_day_summary = state_data.get("prev_day_summary", {
    "date": "2026-10-16",
    "type": "PE",
    "final_pts": -33.6,
    "final_inr": -1092,
    "exit_reason": "STOP LOSS HIT",
    "regime": "BREAKOUT DAY (10-ATR: 326 >= 229)",
    "entry_spot": 22932.4,
    "entry_time": "09:35",
    "init_sl": 22966.0,
    "target_p": 22828.1,
    "exit_spot": 22966.0
})
hud = state_data.get("hud", {
    "type": "PE",
    "trade_closed": False,
    "exit_reason": "ACTIVE",
    "symbol": "NIFTY19OCT2623050PE",
    "delta": 0.43,
    "entry_spot": 22969.8,
    "entry_opt": 58.45,
    "sl_spot": 23175.1,
    "sl_opt": 0.50,
    "trail_spot": 23175.1,
    "trail_opt": 0.50,
    "secured_pts": 0.0,
    "targets": [22790.0, 22737.7, 22637.4]
})

trades_taken = state_data.get("trades_taken_today", 0)
current_budget = float(state_data.get("user_selected_budget", BUDGET_CEILING_DEFAULT))

col_title, col_budget, col_refresh = st.columns([3, 2, 1])

regime_str = "BREAKOUT DAY (10-ATR: 326.3 / 229)"
badge_color = "#00e676"
badge_text_color = "#000000"
regime_badge = f"<span style='background:{badge_color}; color:{badge_text_color}; padding:3px 10px; border-radius:4px; font-size:12px; font-weight:bold; margin-left:10px;'>{regime_str}</span>"

with col_title:
    st.markdown(
        f"<h3 style='margin:0; padding:0; color:#e0e0e0; font-family:sans-serif;'>"
        f"NIFTY Dual-Regime Master {regime_badge} "
        f"<span id='live_ltp_display' style='color:#00e676; font-size:22px; font-weight:bold; margin-left:10px;'>₹{ltp:.2f}</span>"
        f"</h3>",
        unsafe_allow_html=True,
    )

with col_budget:
    new_budget = st.slider(
        "Max Premium (₹)",
        min_value=30.0,
        max_value=150.0,
        value=current_budget,
        step=5.0,
    )
    if new_budget != current_budget:
        write_redis_key("user_selected_budget", new_budget)
        st.rerun()

with col_refresh:
    if st.button("🔄 Force Refresh"):
        st.rerun()

prev_summary_html = ""
if prev_day_summary and prev_day_summary.get("type"):
    p_type = prev_day_summary.get("type", "—")
    p_color = "#00e676" if p_type == "CE" else "#ff5252"
    p_pts = prev_day_summary.get("final_pts", 0.0)
    p_inr = prev_day_summary.get("final_inr", 0)
    p_sign = "+" if p_pts >= 0 else ""
    p_pnl_color = "#00e676" if p_pts >= 0 else "#ff5252"

    prev_summary_html = (
        '<div style="background:#181c27; border:1px solid #363a45; border-radius:6px; padding:10px 14px; margin-bottom:12px;">'
        '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; flex-wrap:wrap; gap:8px;">'
        f'<span style="font-size:12px; font-weight:bold; color:#ffb74d;">📅 PREVIOUS SESSION REPORT ({prev_day_summary.get("date")})</span>'
        f'<span style="font-size:13px; font-weight:bold; color:{p_pnl_color};">NET SECURED: {p_sign}{p_pts:.1f} pts (₹{p_sign}{p_inr}) [{prev_day_summary.get("exit_reason")}]</span>'
        '</div>'
        '<div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">SETUP:</span> <b style="color:{p_color}; font-size:12px;">{prev_day_summary.get("regime")} {p_type}</b></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">ENTRY:</span> <b style="color:#fff; font-size:12px;">{prev_day_summary.get("entry_spot")}</b> <span style="color:#787b86; font-size:11px;">({prev_day_summary.get("entry_time")})</span></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">INITIAL SL:</span> <b style="color:#ef5350; font-size:12px;">{prev_day_summary.get("init_sl")}</b></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">TARGET:</span> <b style="color:#ffb74d; font-size:12px;">{prev_day_summary.get("target_p")}</b></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">EXIT PRICE:</span> <b style="color:#fff; font-size:12px;">{prev_day_summary.get("exit_spot")}</b></div>'
        '</div>'
        '</div>'
    )

orb_h_display = orb_levels.get("orb_h", 23175.1)
orb_l_display = orb_levels.get("orb_l", 22970.85)
pivot_p_display = pivots.get("P", 22897.05)
pdh_display = pivots.get("PDH", 23056.4)
pdl_display = pivots.get("PDL", 22637.4)

status_label = "<b style='color:#ffb74d;'>⚡ Awaiting 09:30-10:00 Window Trigger</b>"

complete_banner_html = (
    '<div style="background:#131722; border:1px solid #2a2e39; border-radius:8px; padding:12px 16px; margin-bottom:12px;">'
    + prev_summary_html +
    '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">'
    '<span style="color:#90a4ae; font-size:13px; font-weight:bold;">SESSION LEVELS & TODAY FORECAST</span>'
    f'<span style="font-size:12px;">{status_label}</span>'
    '</div>'
    '<div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:10px;">'
    '<div style="background:#1f2430; padding:6px 12px; border-radius:4px;">'
    '<div style="font-size:11px; color:#90a4ae;">DAILY 10-ATR</div>'
    '<div style="font-size:13px; font-weight:bold; color:#00e676;">326.3 / 229</div>'
    '</div>'
    '<div style="background:#1f2430; padding:6px 12px; border-radius:4px;">'
    '<div style="font-size:11px; color:#90a4ae;">ORB HIGH</div>'
    f'<div style="font-size:13px; font-weight:bold; color:#00e676;">{orb_h_display}</div>'
    '</div>'
    '<div style="background:#1f2430; padding:6px 12px; border-radius:4px;">'
    '<div style="font-size:11px; color:#90a4ae;">ORB LOW</div>'
    f'<div style="font-size:13px; font-weight:bold; color:#ff5252;">{orb_l_display}</div>'
    '</div>'
    '<div style="background:#1f2430; padding:6px 12px; border-radius:4px;">'
    '<div style="font-size:11px; color:#90a4ae;">PIVOT P</div>'
    f'<div style="font-size:13px; font-weight:bold; color:#ffeb3b;">{pivot_p_display}</div>'
    '</div>'
    '<div style="background:#1f2430; padding:6px 12px; border-radius:4px;">'
    '<div style="font-size:11px; color:#90a4ae;">PDH / PDL</div>'
    f'<div style="font-size:13px; font-weight:bold; color:#42a5f5;">{pdh_display} / {pdl_display}</div>'
    '</div>'
    '</div>'
    '</div>'
)

st.markdown(complete_banner_html, unsafe_allow_html=True)

candles_json = json.dumps(candles)
markers_json = json.dumps(markers)
pivots_json = json.dumps(pivots)
orb_json = json.dumps(orb_levels)
fib_json = json.dumps(fib_lines)

html_code = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    body {{
      margin: 0; padding: 0; background-color: #131722; color: #d1d4dc; font-family: sans-serif; overflow: hidden;
    }}
    #chart_container {{ width: 100%; height: 620px; position: relative; }}
  </style>
</head>
<body>
  <div id="chart_container"></div>
  <script>
    const rawCandles = {candles_json} || [];
    const rawMarkers = {markers_json} || [];
    const pivots = {pivots_json} || {{}};
    const orbLevels = {orb_json} || {{}};
    const fibLines = {fib_json} || {{}};
    const IST_OFFSET = 19800;

    const formattedCandles = rawCandles.map(c => ({{
      time: c.time + IST_OFFSET, open: c.open, high: c.high, low: c.low, close: c.close, vwap: c.vwap, ema20: c.ema20
    }}));

    const formattedMarkers = rawMarkers.map(m => ({{ ...m, time: m.time + IST_OFFSET }}));

    const container = document.getElementById('chart_container');
    const chart = LightweightCharts.createChart(container, {{
      width: container.clientWidth, height: 620,
      layout: {{ background: {{ color: '#131722' }}, textColor: '#d1d4dc' }},
      grid: {{ vertLines: {{ color: '#1f2430' }}, horzLines: {{ color: '#1f2430' }} }},
      rightPriceScale: {{ borderColor: '#2a2e39', autoScale: true }},
      timeScale: {{ borderColor: '#2a2e39', timeVisible: true }}
    }});

    const candleSeries = chart.addCandlestickSeries({{ upColor: '#089981', downColor: '#f23645' }});
    candleSeries.setData(formattedCandles);

    const vwapSeries = chart.addLineSeries({{ color: '#9c27b0', lineWidth: 2, priceLineVisible: false }});
    vwapSeries.setData(formattedCandles.filter(d => d.vwap).map(d => ({{ time: d.time, value: d.vwap }})));

    const emaSeries = chart.addLineSeries({{ color: '#2962ff', lineWidth: 1.5, priceLineVisible: false }});
    emaSeries.setData(formattedCandles.filter(d => d.ema20).map(d => ({{ time: d.time, value: d.ema20 }})));

    if (formattedMarkers.length > 0) {{ candleSeries.setMarkers(formattedMarkers); }}

    if (orbLevels.orb_h) {{
      candleSeries.createPriceLine({{ price: parseFloat(orbLevels.orb_h), color: '#00e676', lineWidth: 2, title: 'ORB HIGH' }});
    }}
    if (orbLevels.orb_l) {{
      candleSeries.createPriceLine({{ price: parseFloat(orbLevels.orb_l), color: '#ff5252', lineWidth: 2, title: 'ORB LOW' }});
    }}
    if (pivots.P) {{
      candleSeries.createPriceLine({{ price: parseFloat(pivots.P), color: '#ffeb3b', lineStyle: 2, title: 'Pivot P' }});
    }}

    if (formattedCandles.length > 0) {{ chart.timeScale().fitContent(); }}
    window.addEventListener('resize', () => {{ chart.applyOptions({{ width: container.clientWidth }}); }});
  </script>
</body>
</html>
"""
components.html(html_code, height=640)
