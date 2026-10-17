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
# 1. Header Bar & Real-time Point Monitor
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
# 2. Previous Day Session Summary Report
# -------------------------------------------------------------
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
        f'<span style="font-size:12px; font-weight:bold; color:#ffb74d;">📅 PREVIOUS DAY SESSION REPORT ({prev_day_summary.get("date")})</span>'
        f'<span style="font-size:13px; font-weight:bold; color:{p_pnl_color};">NET SECURED: {p_sign}{p_pts:.1f} pts (₹{p_sign}{p_inr}) [{prev_day_summary.get("exit_reason")}]</span>'
        '</div>'
        '<div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">SETUP:</span> <b style="color:{p_color}; font-size:12px;">{prev_day_summary.get("regime")} {p_type}</b></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">ENTRY:</span> <b style="color:#fff; font-size:12px;">{prev_day_summary.get("entry_spot")}</b> <span style="color:#787b86; font-size:11px;">({prev_day_summary.get("entry_time")})</span></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">INITIAL SL:</span> <b style="color:#ef5350; font-size:12px;">{prev_day_summary.get("init_sl")}</b></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">MAX TRAILED SL:</span> <b style="color:#00e676; font-size:12px;">{prev_day_summary.get("final_trail_sl")}</b></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">TARGET:</span> <b style="color:#ffb74d; font-size:12px;">{prev_day_summary.get("target_p")}</b></div>'
        f'<div style="background:#1f2430; padding:4px 10px; border-radius:4px;"><span style="color:#90a4ae; font-size:11px;">EXIT PRICE:</span> <b style="color:#fff; font-size:12px;">{prev_day_summary.get("exit_spot")}</b> <span style="color:#787b86; font-size:11px;">({prev_day_summary.get("exit_time")})</span></div>'
        '</div>'
        '</div>'
    )

# -------------------------------------------------------------
# 3. Session Key Levels
# -------------------------------------------------------------
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

active_trade_type = hud.get("type") if (hud and hud.get("entry_spot")) else None
is_trade_closed = hud.get("trade_closed", False) if hud else False

status_label = "Awaiting 09:30-10:00 Window Trigger" if trades_taken == 0 else ("1/1 Daily Trade Locked (Execution Active)" if not is_trade_closed else f"Trade Closed [{hud.get('exit_reason', 'STOP HIT')}]")

# -------------------------------------------------------------
# 4. Streamlined Strip Panel with Dynamic Trailing SL & N-Targets
# -------------------------------------------------------------
orb_mid = float(fib_lines.get("ORB_MID", ltp))
orb_h_val = float(orb_levels.get("orb_h", 999999.0))
orb_l_val = float(orb_levels.get("orb_l", 0.0))

# Strategic Selection: Breakout vs Fade logic
if is_high_vol:
    is_strat_call = (ltp >= orb_mid)
else:
    # On Fade days: Piercing above High triggers PE; below Low triggers CE
    if ltp >= orb_h_val:
        is_strat_call = False
    elif ltp <= orb_l_val:
        is_strat_call = True
    else:
        is_strat_call = (ltp <= orb_mid)

if active_trade_type:
    is_call = (active_trade_type == "CE")
    contract_title = f"{'CALL OPTION' if is_call else 'PUT OPTION'} ({active_trade_type}) {'• ACTIVE POSITION' if not is_trade_closed else '• COMPLETED'}"
    contract_symbol = hud.get("symbol", "NIFTY OPT")
    contract_delta = float(hud.get("delta", 0.50))
    header_color = "#00e676" if is_call else "#ff5252"

    entry_spot = float(hud.get("entry_spot", 0.0))
    entry_opt = float(hud.get("entry_opt", 0.0))
    sl_spot = float(hud.get("sl_spot", 0.0))
    sl_opt = float(hud.get("sl_opt", 0.0))
    cur_trail_spot = float(hud.get("trail_spot", sl_spot))
    cur_trail_opt = float(hud.get("trail_opt", sl_opt))
    secured_pts = float(hud.get("secured_pts", 0.0))

    entry_val = f"{entry_spot:.1f} (₹{entry_opt:.2f})"
    init_sl_val = f"{sl_spot:.1f} (₹{sl_opt:.2f})"
    
    trail_sign = "+" if secured_pts >= 0 else ""
    cur_sl_val = f"{cur_trail_spot:.1f} (₹{cur_trail_opt:.2f}) [{trail_sign}{secured_pts:.1f}p]"

    raw_targets = hud.get("targets", [])
    if not raw_targets:
        raw_targets = [float(hud.get("target_p", entry_spot))]

    t_elements_html = ""
    for i, tg_val in enumerate(raw_targets, 1):
        tg_diff = abs(tg_val - entry_spot)
        tg_opt = max(0.5, round(entry_opt + (tg_diff * contract_delta), 2))
        lbl = f"TARGET {i}"
        if i == 1:
            lbl += " (3ATR)" if hud.get("mode") == "BREAKOUT" else " (VWAP)"
        t_elements_html += (
            f'<div style="font-size:12px;"><span style="color:#90a4ae; font-weight:bold;">{lbl} :</span> '
            f'<b style="color:#ffb74d;">{tg_val:.1f} (₹{tg_opt:.2f})</b></div>'
        )

else:
    # Standby Preview Mode dynamically follows strategy setup
    chosen_f = ce_f if is_strat_call else pe_f
    contract_title = f"{'CALL OPTION (CE)' if is_strat_call else 'PUT OPTION (PE)'} PREVIEW"
    contract_symbol = chosen_f.get("symbol", "NIFTY OPT")
    contract_delta = float(chosen_f.get("delta", 0.45))
    header_color = "#00e676" if is_strat_call else "#ff5252"

    entry_spot = float(chosen_f.get("exp_entry_spot", 0.0))
    opt_ltp_f = float(chosen_f.get("opt_ltp", 75.0))
    sl_spot = float(chosen_f.get("exp_sl_spot", 0.0))
    sl_opt = float(chosen_f.get("exp_sl_opt", 0.0))

    entry_val = f"{entry_spot:.1f} (₹{opt_ltp_f:.2f})"
    init_sl_val = f"{sl_spot:.1f} (₹{sl_opt:.2f})"
    cur_sl_val = f"{sl_spot:.1f} (₹{sl_opt:.2f}) [STANDBY]"

    raw_targets = chosen_f.get("targets", [])
    t_elements_html = ""
    for i, tg_val in enumerate(raw_targets, 1):
        tg_diff = abs(tg_val - entry_spot)
        tg_opt = max(0.5, round(opt_ltp_f + (tg_diff * contract_delta), 2))
        lbl = f"TARGET {i}"
        if i == 1:
            lbl += " (3ATR)" if is_high_vol else " (VWAP)"
        t_elements_html += (
            f'<div style="font-size:12px;"><span style="color:#90a4ae; font-weight:bold;">{lbl} :</span> '
            f'<b style="color:#b388ff;">{tg_val:.1f} (₹{tg_opt:.2f})</b></div>'
        )

execution_strip_panel_html = (
    '<div style="background:#121620; border:1px solid #2a2e39; border-radius:6px; padding:10px 14px; margin-top:8px;">'
    '<div style="font-size:12px; font-weight:bold; color:' + header_color + '; margin-bottom:10px; letter-spacing:0.3px;">'
    + contract_title + ' • ' + contract_symbol + f' (DELTA: {contract_delta:.2f})' +
    '</div>'
    '<div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; border-top:1px solid #1f2430; padding-top:10px;">'
    f'<div style="font-size:12px;"><span style="color:#90a4ae; font-weight:bold;">ENTRY :</span> <b style="color:#ffffff;">{entry_val}</b></div>'
    f'<div style="font-size:12px;"><span style="color:#90a4ae; font-weight:bold;">INITIAL SL :</span> <b style="color:#ef5350;">{init_sl_val}</b></div>'
    f'<div style="font-size:12px;"><span style="color:#90a4ae; font-weight:bold;">CURRENT SL :</span> <b style="color:#00e676;">{cur_sl_val}</b></div>'
    + t_elements_html +
    '</div>'
    '</div>'
)

complete_banner_html = (
    '<div style="background:#131722; border:1px solid #2a2e39; border-radius:8px; padding:12px 16px; margin-bottom:12px;">'
    + prev_summary_html +
    '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">'
    '<span style="color:#90a4ae; font-size:13px; font-weight:bold;">SESSION LEVELS & TODAY FORECAST</span>'
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
    f'<div style="font-size:11px; color:#787b86; margin-bottom:4px; font-weight:bold;">EXECUTION & STRIKE MONITOR (BUDGET: ≤ ₹{current_budget:.0f})</div>'
    + execution_strip_panel_html +
    '</div>'
    '</div>'
)

st.markdown(complete_banner_html, unsafe_allow_html=True)

# -------------------------------------------------------------
# 5. Interactive Candlestick Chart (Lightweight Charts Canvas)
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
      vwap: c.vwap,
      ema9: c.ema9
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

    const emaSeries = chart.addLineSeries({{
      color: '#2962ff',
      lineWidth: 1,
      priceLineVisible: false,
      title: 'EMA 9',
    }});
    emaSeries.setData(formattedCandles.filter(d => d.ema9).map(d => ({{ time: d.time, value: d.ema9 }})));

    if (formattedMarkers.length > 0) {{
      candleSeries.setMarkers(formattedMarkers);
    }}

    // Classical Floor Pivots
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

    // ORB High & Low Bands
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

    // Dynamic ORB Fibonacci Lines
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

    // Tooltip Card displayed when hovering over Buy/Sell signals
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
          <div><b>Index Entry:</b> ${{cardData.entry}} | <b>Opt:</b> ₹${{cardData.opt_entry || '—'}}</div>
          <div><b>Initial SL:</b> ${{cardData.sl}} | <b>Opt SL:</b> ₹${{cardData.opt_sl || '—'}}</div>
          <div><b>Max Trailed SL:</b> ${{cardData.max_trail_sl || cardData.sl}}</div>
          <div><b>Target:</b> ${{cardData.target || '—'}}</div>
          <div style="margin-top:4px; font-weight:bold; color:#ffb74d;">Status: ${{cardData.outcome}}</div>
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
                if (latestCandle.ema9) {{
                  emaSeries.update({{ time: shiftedCandle.time, value: latestCandle.ema9 }});
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
