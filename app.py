import html
import json
import os
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="NIFTY Dual-Regime Master Terminal",
    layout="wide",
    initial_sidebar_state="collapsed",
)

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
BUDGET_CEILING_DEFAULT = float(os.getenv("BUDGET_CEILING", "80.0"))


def read_redis_key(key: str):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    url = f"{UPSTASH_REDIS_REST_URL}/get/{key}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=3)
        if r.status_code == 200:
            val = r.json().get("result")
            if val:
                return json.loads(val)
    except Exception:
        pass
    return None


def write_redis_key(key: str, value):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return
    url = f"{UPSTASH_REDIS_REST_URL}/set/{key}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        requests.post(url, headers=headers, data=json.dumps(value), timeout=3)
    except Exception:
        pass


state_data = read_redis_key("nifty_live_state") or {}

ltp = float(state_data.get("ltp", 0.0))
opt_ltp = float(state_data.get("opt_ltp", 0.0))
candles = state_data.get("candles", [])
markers = state_data.get("markers", [])
history_cards = state_data.get("history_cards", {})
pivots = state_data.get("pivots", {})
orb_levels = state_data.get("orb_levels", {})
fib_lines = state_data.get("fib_lines", {})
contract_preview = state_data.get("contract_preview", {})
prev_day_summary = state_data.get("prev_day_summary", {})
hud = state_data.get("hud", {})

daily_atr10 = float(state_data.get("daily_atr10", 0.0))
is_high_vol = state_data.get("is_high_vol", False)
vol_threshold = float(state_data.get("vol_threshold", 229.0))
trades_taken = state_data.get("trades_taken_today", 0)

saved_budget = read_redis_key("user_selected_budget")
current_budget = float(saved_budget) if saved_budget is not None else BUDGET_CEILING_DEFAULT

# -------------------------------------------------------------
# Header Bar & Live Point Monitor
# -------------------------------------------------------------
col_title, col_budget, col_refresh = st.columns([3, 2, 1])

regime_str = "BREAKOUT DAY" if is_high_vol else "FADE DAY"
badge_color = "#00e676" if is_high_vol else "#2962ff"
badge_text_color = "#000000" if is_high_vol else "#ffffff"
regime_badge = f"<span style='background:{badge_color}; color:{badge_text_color}; padding:3px 10px; border-radius:4px; font-size:12px; font-weight:bold; margin-left:10px;'>{regime_str} (10-ATR: {daily_atr10:.1f})</span>"

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
        help="Strike selector picks contracts trading closest to and below this ceiling.",
    )
    if new_budget != current_budget:
        write_redis_key("user_selected_budget", new_budget)
        st.rerun()

with col_refresh:
    if st.button("🔄 Force Refresh"):
        st.rerun()

# -------------------------------------------------------------
# Active Trade HUD Card or Standby Forecast Table
# -------------------------------------------------------------
if hud and hud.get("entry_spot"):
    trade_type = hud.get("type", "CE")
    mode = hud.get("mode", "BREAKOUT")
    badge_bg = "#00c853" if trade_type == "CE" else "#d50000"
    symbol = hud.get("symbol", f"NIFTY {trade_type}")
    entry_spot = float(hud.get("entry_spot", 0.0))
    entry_opt = float(hud.get("entry_opt", 0.0))
    sl_spot = float(hud.get("sl_spot", 0.0))
    sl_opt = float(hud.get("sl_opt", 0.0))
    trail_spot = float(hud.get("trail_spot", 0.0))
    trail_opt = float(hud.get("trail_opt", 0.0))
    target_p = float(hud.get("target_p", 0.0))
    opt_target = float(hud.get("opt_target", 0.0))
    delta = float(hud.get("delta", 0.50))
    secured_pts = float(hud.get("secured_pts", 0.0))

    active_html = (
        '<div style="background:#131722; border:1px solid #2a2e39; border-radius:8px; padding:12px 16px; margin-bottom:12px;">'
        '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;">'
        '<div style="display:flex; align-items:center; gap:8px;">'
        f'<span style="background:{badge_bg}; color:#fff; font-size:12px; font-weight:bold; padding:3px 8px; border-radius:4px;">{trade_type}</span>'
        f'<span style="background:#2a2e39; color:#ffb74d; font-size:11px; font-weight:bold; padding:3px 6px; border-radius:4px;">{mode}</span>'
        f'<span style="color:#ffffff; font-size:15px; font-weight:bold;">{symbol}</span>'
        f'<span style="color:#787b86; font-size:12px;">(Delta: {delta:.2f})</span>'
        '</div>'
        '<div style="font-size:12px; color:#00e676; font-weight:bold;">● ACTIVE RUNNER [1/1 DAILY TRADE IN PROGRESS]</div>'
        '</div>'
        '<div style="display:flex; gap:12px; align-items:stretch; flex-wrap:wrap;">'
        '<div style="background:#1f2430; padding:6px 12px; border-radius:4px; min-width:130px;">'
        '<div style="font-size:11px; color:#90a4ae;">ENTRY (INDEX / OPTION)</div>'
        f'<div style="font-size:14px; font-weight:bold; color:#fff;">{entry_spot:.1f}</div>'
        f'<div style="font-size:11px; color:#4fc3f7;">LTP: ₹{entry_opt:.2f}</div>'
        '</div>'
        '<div style="background:#1f2430; padding:6px 12px; border-radius:4px; min-width:130px;">'
        '<div style="font-size:11px; color:#90a4ae;">INITIAL STOP LOSS</div>'
        f'<div style="font-size:14px; font-weight:bold; color:#ef5350;">{sl_spot:.1f}</div>'
        f'<div style="font-size:11px; color:#ef5350;">GTT: ₹{sl_opt:.2f} (-{abs(entry_spot-sl_spot):.1f}p)</div>'
        '</div>'
        '<div style="background:#1f2430; padding:6px 12px; border-radius:4px; min-width:130px;">'
        '<div style="font-size:11px; color:#90a4ae;">CURRENT TRAIL SL</div>'
        f'<div style="font-size:14px; font-weight:bold; color:#00e676;">{trail_spot:.1f}</div>'
        f'<div style="font-size:11px; color:#00e676;">GTT: ₹{trail_opt:.2f} ({"+" if secured_pts>=0 else ""}{secured_pts:.1f}p)</div>'
        '</div>'
        '<div style="background:#1f2430; padding:6px 12px; border-radius:4px; min-width:130px;">'
        f'<div style="font-size:11px; color:#90a4ae;">{"3x ATR TARGET" if mode == "BREAKOUT" else "VWAP TARGET"}</div>'
        f'<div style="font-size:14px; font-weight:bold; color:#ffb74d;">{target_p:.1f}</div>'
        f'<div style="font-size:11px; color:#ffb74d;">Target: ₹{opt_target:.2f} (+{abs(target_p-entry_spot):.1f}p)</div>'
        '</div>'
        '</div>'
        '</div>'
    )
    st.markdown(active_html, unsafe_allow_html=True)
else:
    orb_h_display = orb_levels.get("orb_h", "—")
    orb_l_display = orb_levels.get("orb_l", "—")
    pivot_p_display = pivots.get("P", "—")
    pdh_display = pivots.get("PDH", "—")
    pdl_display = pivots.get("PDL", "—")
    fib_dir = fib_lines.get("direction", "NEUTRAL")
    fib_50 = fib_lines.get("fib_50", "—")
    fib_100 = fib_lines.get("fib_100", "—")
    fib_161 = fib_lines.get("fib_161", "—")

    ce_f = contract_preview.get("CE", {})
    pe_f = contract_preview.get("PE", {})
    regime_mode_desc = f"BREAKOUT (≥{vol_threshold:.0f} ATR: 3x ATR Runner)" if is_high_vol else f"FADE (<{vol_threshold:.0f} ATR: 0.5 ATR Sweep to VWAP)"
    status_label = "Awaiting 09:30-10:00 Window Trigger" if trades_taken == 0 else "Daily Trade Completed (1/1 Limit Reached)"

    # Previous Day Summary Banner
    prev_summary_html = ""
    if prev_day_summary and prev_day_summary.get("type"):
        p_type = prev_day_summary.get("type", "—")
        p_color = "#00e676" if p_type == "CE" else "#ff5252"
        p_pts = prev_day_summary.get("final_pts", 0.0)
        p_inr = prev_day_summary.get("final_inr", 0)
        p_sign = "+" if p_pts >= 0 else ""
        p_pnl_color = "#00e676" if p_pts >= 0 else "#ff5252"

        prev_summary_html = (
            '<div style="background:#1a1f2c; border:1px solid #363a45; border-radius:6px; padding:8px 12px; margin-bottom:10px;">'
            '<div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">'
            f'<div style="font-size:12px; color:#d1d4dc;"><b>PREVIOUS DAY SESSION ({prev_day_summary.get("date")}):</b> '
            f'<span style="color:{p_color}; font-weight:bold;">BUY {p_type} ({prev_day_summary.get("regime")})</span> @ <b>{prev_day_summary.get("entry_spot")}</b> '
            f'| Initial SL: <b>{prev_day_summary.get("init_sl")}</b> | Exit: <b>{prev_day_summary.get("exit_spot")}</b> ({prev_day_summary.get("exit_reason")})</div>'
            f'<div style="font-size:13px; font-weight:bold; color:{p_pnl_color};">NET PnL: {p_sign}{p_pts:.1f} pts (₹{p_sign}{p_inr})</div>'
            '</div>'
            '</div>'
        )

    standby_html = (
        '<div style="background:#131722; border:1px solid #2a2e39; border-radius:8px; padding:12px 16px; margin-bottom:12px;">'
        + prev_summary_html +
        '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">'
        '<span style="color:#90a4ae; font-size:13px; font-weight:bold;">STANDBY MONITOR • DUAL REGIME SESSION LEVELS</span>'
        f'<span style="color:#787b86; font-size:12px;">{status_label}</span>'
        '</div>'
        '<div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:10px;">'
        '<div style="background:#1f2430; padding:6px 12px; border-radius:4px;">'
        '<div style="font-size:11px; color:#90a4ae;">DAILY 10-ATR</div>'
        f'<div style="font-size:13px; font-weight:bold; color:#ffb74d;">{daily_atr10:.1f} / {vol_threshold:.0f}</div>'
        '</div>'
        '<div style="background:#1f2430; padding:6px 12px; border-radius:4px;">'
        '<div style="font-size:11px; color:#90a4ae;">REGIME MODE</div>'
        f'<div style="font-size:13px; font-weight:bold; color:#42a5f5;">{regime_mode_desc}</div>'
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
        '<div style="background:#1f2430; padding:6px 12px; border-radius:4px;">'
        f'<div style="font-size:11px; color:#90a4ae;">FIB 50% / 100% / 161% ({fib_dir})</div>'
        f'<div style="font-size:13px; font-weight:bold; color:#b388ff;">{fib_50} / {fib_100} / {fib_161}</div>'
        '</div>'
        '</div>'
        '<div style="border-top:1px solid #2a2e39; padding-top:8px;">'
        f'<div style="font-size:11px; color:#787b86; margin-bottom:6px; font-weight:bold;">PRE-TRIGGER STRIKE FORECAST (BUDGET: ≤ ₹{current_budget:.0f})</div>'
        '<div style="display:flex; gap:12px; flex-wrap:wrap;">'
        '<div style="background:#1f2430; padding:6px 10px; border-radius:4px; flex:1; min-width:240px; border-left:3px solid #00e676;">'
        f'<div style="font-size:11px; color:#00e676; font-weight:bold;">CALL OPTION (CE) • {ce_f.get("symbol", "NIFTY CE")} (Δ: {ce_f.get("delta", 0.5):.2f})</div>'
        f'<div style="font-size:12px; color:#d1d4dc;">LTP: <b>₹{ce_f.get("opt_ltp", 0.0):.2f}</b> | Trig: <b>{ce_f.get("exp_entry_spot", 0.0):.1f}</b></div>'
        f'<div style="font-size:11px; color:#90a4ae;">SL: {ce_f.get("exp_sl_spot", 0.0):.1f} (₹{ce_f.get("exp_sl_opt", 0.0):.2f}) | Target: {ce_f.get("exp_target_spot", 0.0):.1f} (₹{ce_f.get("exp_target_opt", 0.0):.2f})</div>'
        '</div>'
        '<div style="background:#1f2430; padding:6px 10px; border-radius:4px; flex:1; min-width:240px; border-left:3px solid #ff5252;">'
        f'<div style="font-size:11px; color:#ff5252; font-weight:bold;">PUT OPTION (PE) • {pe_f.get("symbol", "NIFTY PE")} (Δ: {pe_f.get("delta", 0.5):.2f})</div>'
        f'<div style="font-size:12px; color:#d1d4dc;">LTP: <b>₹{pe_f.get("opt_ltp", 0.0):.2f}</b> | Trig: <b>{pe_f.get("exp_entry_spot", 0.0):.1f}</b></div>'
        f'<div style="font-size:11px; color:#90a4ae;">SL: {pe_f.get("exp_sl_spot", 0.0):.1f} (₹{pe_f.get("exp_sl_opt", 0.0):.2f}) | Target: {pe_f.get("exp_target_spot", 0.0):.1f} (₹{pe_f.get("exp_target_opt", 0.0):.2f})</div>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )
    st.markdown(standby_html, unsafe_allow_html=True)

# -------------------------------------------------------------
# Lightweight Charts Component (Interactive Chart with Pivots & Fibs)
# -------------------------------------------------------------
candles_json = json.dumps(candles)
markers_json = json.dumps(markers)
history_cards_json = json.dumps(history_cards)
pivots_json = json.dumps(pivots)
orb_json = json.dumps(orb_levels)
fib_json = json.dumps(fib_lines)
redis_url_str = UPSTASH_REDIS_REST_URL
redis_token_str = UPSTASH_REDIS_REST_TOKEN

chart_regime_text = f"BREAKOUT DAY (10-ATR: {daily_atr10:.0f} ≥ {vol_threshold:.0f})" if is_high_vol else f"FADE DAY (10-ATR: {daily_atr10:.0f} < {vol_threshold:.0f})"
chart_badge_bg = "rgba(0, 230, 118, 0.2)" if is_high_vol else "rgba(41, 98, 255, 0.2)"
chart_border_color = "#00e676" if is_high_vol else "#2962ff"

html_code = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    body {{
      margin: 0;
      padding: 0;
      background-color: #131722;
      color: #d1d4dc;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      overflow: hidden;
    }}
    #chart_container {{
      width: 100vw;
      height: 620px;
      position: relative;
    }}
    #regime_pill {{
      position: absolute;
      top: 12px;
      left: 12px;
      background: {chart_badge_bg};
      border: 1px solid {chart_border_color};
      color: #fff;
      font-size: 11px;
      font-weight: bold;
      padding: 5px 10px;
      border-radius: 4px;
      z-index: 10;
      pointer-events: none;
      box-shadow: 0 2px 6px rgba(0,0,0,0.5);
    }}
    #hover_card {{
      position: absolute;
      display: none;
      background: #1e222d;
      border: 1px solid #363a45;
      border-radius: 6px;
      padding: 10px 14px;
      font-size: 12px;
      line-height: 1.5;
      color: #d1d4dc;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
      z-index: 1000;
      pointer-events: none;
      min-width: 230px;
    }}
  </style>
</head>
<body>
  <div id="chart_container">
    <div id="regime_pill">⚡ REGIME: {chart_regime_text}</div>
    <div id="hover_card"></div>
  </div>

  <script>
    const rawCandles = {candles_json} || [];
    const rawMarkers = {markers_json} || [];
    const historyCards = {history_cards_json} || {{}};
    const pivots = {pivots_json} || {{}};
    const orbLevels = {orb_json} || {{}};
    const fibLines = {fib_json} || {{}};
    const IST_OFFSET = 19800;

    const formattedCandles = rawCandles.map(c => ({{
      time: c.time + IST_OFFSET,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
      vwap: c.vwap
    }}));

    const formattedMarkers = rawMarkers.map(m => ({{
      ...m,
      time: m.time + IST_OFFSET
    }}));

    const container = document.getElementById('chart_container');
    const hoverCard = document.getElementById('hover_card');

    const chart = LightweightCharts.createChart(container, {{
      width: container.clientWidth,
      height: 620,
      layout: {{
        background: {{ color: '#131722' }},
        textColor: '#d1d4dc',
      }},
      grid: {{
        vertLines: {{ color: '#1f2430' }},
        horzLines: {{ color: '#1f2430' }},
      }},
      crosshair: {{
        mode: LightweightCharts.CrosshairMode.Normal,
      }},
      rightPriceScale: {{
        borderColor: '#2a2e39',
        autoScale: true,
      }},
      timeScale: {{
        borderColor: '#2a2e39',
        timeVisible: true,
        secondsVisible: false,
      }},
    }});

    const candleSeries = chart.addCandlestickSeries({{
      upColor: '#089981',
      downColor: '#f23645',
      borderVisible: false,
      wickUpColor: '#089981',
      wickDownColor: '#f23645',
    }});
    candleSeries.setData(formattedCandles);

    const vwapSeries = chart.addLineSeries({{
      color: '#9c27b0',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'VWAP',
    }});
    vwapSeries.setData(formattedCandles.filter(d => d.vwap).map(d => ({{ time: d.time, value: d.vwap }})));

    if (formattedMarkers.length > 0) {{
      candleSeries.setMarkers(formattedMarkers);
    }}

    const visiblePivots = {{
      'P':   {{ color: '#ffeb3b', title: 'Pivot P' }},
      'R1':  {{ color: '#ef5350', title: 'R1' }},
      'R2':  {{ color: '#e53935', title: 'R2' }},
      'R3':  {{ color: '#d32f2f', title: 'R3' }},
      'R4':  {{ color: '#c62828', title: 'R4' }},
      'R5':  {{ color: '#b71c1c', title: 'R5' }},
      'S1':  {{ color: '#26a69a', title: 'S1' }},
      'S2':  {{ color: '#00897b', title: 'S2' }},
      'S3':  {{ color: '#00796b', title: 'S3' }},
      'S4':  {{ color: '#00695c', title: 'S4' }},
      'S5':  {{ color: '#004d40', title: 'S5' }},
      'PDH': {{ color: '#42a5f5', title: 'PDH' }},
      'PDL': {{ color: '#ab47bc', title: 'PDL' }},
    }};

    Object.entries(pivots).forEach(([k, val]) => {{
      const numVal = parseFloat(val);
      if (!isNaN(numVal) && numVal > 0 && visiblePivots[k]) {{
        candleSeries.createPriceLine({{
          price: numVal,
          color: visiblePivots[k].color,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true,
          title: visiblePivots[k].title,
        }});
      }}
    }});

    if (orbLevels && orbLevels.orb_h) {{
      candleSeries.createPriceLine({{
        price: parseFloat(orbLevels.orb_h),
        color: '#00e676',
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title: 'ORB HIGH',
      }});
    }}
    if (orbLevels && orbLevels.orb_l) {{
      candleSeries.createPriceLine({{
        price: parseFloat(orbLevels.orb_l),
        color: '#ff5252',
        lineWidth: 2,
        lineStyle: LightweightCharts.LineStyle.Solid,
        axisLabelVisible: true,
        title: 'ORB LOW',
      }});
    }}

    if (fibLines) {{
      const fibTitles = {{
        'ORB_MID': 'ORB MID',
        'fib_50': 'FIB 50%',
        'fib_100': 'FIB 100%',
        'fib_161': 'FIB 161.8%'
      }};
      Object.entries(fibLines).forEach(([fKey, fVal]) => {{
        const numVal = parseFloat(fVal);
        if (!isNaN(numVal) && numVal > 0 && fibTitles[fKey]) {{
          candleSeries.createPriceLine({{
            price: numVal,
            color: '#b388ff',
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dotted,
            axisLabelVisible: true,
            title: fibTitles[fKey],
          }});
        }}
      }});
    }}

    if (formattedCandles.length > 0) {{
      chart.timeScale().fitContent();
    }}

    chart.subscribeCrosshairMove(param => {{
      if (!param || !param.time || !param.point) {{
        hoverCard.style.display = 'none';
        return;
      }}
      const rawSecKey = String(param.time - IST_OFFSET);
      const cardData = historyCards[rawSecKey] || historyCards[String(param.time)];
      if (cardData) {{
        const isCe = cardData.type === 'CE';
        const color = isCe ? '#00e676' : '#ff5252';
        hoverCard.innerHTML = `
          <div style="font-weight:bold; color:${{color}}; margin-bottom:4px;">${{cardData.title}}</div>
          <div><b>Time:</b> ${{cardData.time_str}}</div>
          <div><b>Index Entry:</b> ${{cardData.entry}} | <b>SL:</b> ${{cardData.sl}}</div>
          <div><b>Target:</b> ${{cardData.target}}</div>
          <div><b>Outcome:</b> ${{cardData.outcome}}</div>
        `;
        hoverCard.style.display = 'block';
        hoverCard.style.left = Math.min(param.point.x + 15, container.clientWidth - 240) + 'px';
        hoverCard.style.top = Math.max(param.point.y - 80, 10) + 'px';
      }} else {{
        hoverCard.style.display = 'none';
      }}
    }});

    const redisUrl = "{redis_url_str}";
    const redisToken = "{redis_token_str}";

    if (redisUrl && redisToken) {{
      setInterval(async () => {{
        try {{
          const res = await fetch(redisUrl + '/get/nifty_live_state', {{
            headers: {{ 'Authorization': 'Bearer ' + redisToken }}
          }});
          if (res.ok) {{
            const dataObj = await res.json();
            if (dataObj && dataObj.result) {{
              const liveState = JSON.parse(dataObj.result);
              if (liveState && liveState.candles && liveState.candles.length > 0) {{
                const latestCandle = liveState.candles[liveState.candles.length - 1];
                const shiftedCandle = {{
                  time: latestCandle.time + IST_OFFSET,
                  open: latestCandle.open,
                  high: latestCandle.high,
                  low: latestCandle.low,
                  close: latestCandle.close
                }};
                candleSeries.update(shiftedCandle);
                if (latestCandle.vwap) {{
                  vwapSeries.update({{ time: shiftedCandle.time, value: latestCandle.vwap }});
                }}
              }}
              if (liveState && liveState.ltp) {{
                const el = window.parent.document.getElementById('live_ltp_display');
                if (el) {{
                  el.innerText = '₹' + Number(liveState.ltp).toFixed(2);
                }}
              }}
            }}
          }}
        }} catch (err) {{}}
      }}, 1000);
    }}

    window.addEventListener('resize', () => {{
      chart.applyOptions({{ width: container.clientWidth }});
    }});
  </script>
</body>
</html>
"""

components.html(html_code, height=640)
