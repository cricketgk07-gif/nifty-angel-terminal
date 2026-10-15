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
# Dynamic Previous Day & Live Execution Card
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

# 1. Full Previous Day Card (Entry, Initial SL, Trail SL, Target, Exit & Points Secured)
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

# 2. Dynamic Live Box (Empty/Disable Inactive Side)
if active_trade_type == "CE":
    entry_spot = float(hud.get("entry_spot", 0.0))
    entry_opt = float(hud.get("entry_opt", 0.0))
    sl_spot = float(hud.get("sl_spot", 0.0))
    sl_opt = float(hud.get("sl_opt", 0.0))
    trail_spot = float(hud.get("trail_spot", 0.0))
    trail_opt = float(hud.get("trail_opt", 0.0))
    target_p = float(hud.get("target_p", 0.0))
    opt_target = float(hud.get("opt_target", 0.0))
    secured_pts = float(hud.get("secured_pts", 0.0))

    ce_box_html = (
        '<div style="background:#1f2430; padding:10px 14px; border-radius:6px; flex:1; min-width:280px; border-left:4px solid #00e676;">'
        f'<div style="display:flex; justify-content:space-between;"><span style="font-size:12px; color:#00e676; font-weight:bold;">CALL OPTION (CE) • {hud.get("symbol")}</span><span style="font-size:11px; color:#00e676; font-weight:bold;">● ACTIVE POSITION</span></div>'
        f'<div style="font-size:13px; color:#ffffff; margin-top:4px;">Index Entry: <b>{entry_spot:.1f}</b> | Opt Entry: <b>₹{entry_opt:.2f}</b> (LTP: ₹{opt_ltp:.2f})</div>'
        f'<div style="font-size:12px; color:#ef5350; margin-top:2px;">Initial SL: <b>{sl_spot:.1f}</b> (₹{sl_opt:.2f}) | Trail SL: <b style="color:#00e676;">{trail_spot:.1f} (₹{trail_opt:.2f})</b></div>'
        f'<div style="font-size:12px; color:#ffb74d; margin-top:2px;">Target: <b>{target_p:.1f} (₹{opt_target:.2f})</b> | Locked: <b style="color:#00e676;">{"+" if secured_pts>=0 else ""}{secured_pts:.1f} pts</b></div>'
        '</div>'
    )
    pe_box_html = (
        '<div style="background:#15181f; padding:10px 14px; border-radius:6px; flex:1; min-width:280px; border-left:4px solid #424242; opacity:0.35;">'
        '<div style="font-size:12px; color:#787b86; font-weight:bold;">PUT OPTION (PE) • DISABLED</div>'
        '<div style="font-size:12px; color:#616161; margin-top:6px;">Call signal triggered. Put side is completely disabled for today.</div>'
        '</div>'
    )

elif active_trade_type == "PE":
    entry_spot = float(hud.get("entry_spot", 0.0))
    entry_opt = float(hud.get("entry_opt", 0.0))
    sl_spot = float(hud.get("sl_spot", 0.0))
    sl_opt = float(hud.get("sl_opt", 0.0))
    trail_spot = float(hud.get("trail_spot", 0.0))
    trail_opt = float(hud.get("trail_opt", 0.0))
    target_p = float(hud.get("target_p", 0.0))
    opt_target = float(hud.get("opt_target", 0.0))
    secured_pts = float(hud.get("secured_pts", 0.0))

    ce_box_html = (
        '<div style="background:#15181f; padding:10px 14px; border-radius:6px; flex:1; min-width:280px; border-left:4px solid #424242; opacity:0.35;">'
        '<div style="font-size:12px; color:#787b86; font-weight:bold;">CALL OPTION (CE) • DISABLED</div>'
        '<div style="font-size:12px; color:#616161; margin-top:6px;">Put signal triggered. Call side is completely disabled for today.</div>'
        '</div>'
    )
    pe_box_html = (
        '<div style="background:#1f2430; padding:10px 14px; border-radius:6px; flex:1; min-width:280px; border-left:4px solid #ff5252;">'
        f'<div style="display:flex; justify-content:space-between;"><span style="font-size:12px; color:#ff5252; font-weight:bold;">PUT OPTION (PE) • {hud.get("symbol")}</span><span style="font-size:11px; color:#ff5252; font-weight:bold;">● ACTIVE POSITION</span></div>'
        f'<div style="font-size:13px; color:#ffffff; margin-top:4px;">Index Entry: <b>{entry_spot:.1f}</b> | Opt Entry: <b>₹{entry_opt:.2f}</b> (LTP: ₹{opt_ltp:.2f})</div>'
        f'<div style="font-size:12px; color:#ef5350; margin-top:2px;">Initial SL: <b>{sl_spot:.1f}</b> (₹{sl_opt:.2f}) | Trail SL: <b style="color:#00e676;">{trail_spot:.1f} (₹{trail_opt:.2f})</b></div>'
        f'<div style="font-size:12px; color:#ffb74d; margin-top:2px;">Target: <b>{target_p:.1f} (₹{opt_target:.2f})</b> | Locked: <b style="color:#00e676;">{"+" if secured_pts>=0 else ""}{secured_pts:.1f} pts</b></div>'
        '</div>'
    )

else:
    # Standby Preview Mode
    ce_box_html = (
        '<div style="background:#1f2430; padding:10px 14px; border-radius:6px; flex:1; min-width:280px; border-left:4px solid #00e676;">'
        f'<div style="font-size:12px; color:#00e676; font-weight:bold;">CALL OPTION (CE) • {ce_f.get("symbol", "NIFTY CE")} (Δ: {ce_f.get("delta", 0.5):.2f})</div>'
        f'<div style="font-size:12px; color:#d1d4dc; margin-top:4px;">LTP: <b>₹{ce_f.get("opt_ltp", 0.0):.2f}</b> | Trig: <b>{ce_f.get("exp_entry_spot", 0.0):.1f}</b></div>'
        f'<div style="font-size:11px; color:#90a4ae; margin-top:2px;">SL: {ce_f.get("exp_sl_spot", 0.0):.1f} (₹{ce_f.get("exp_sl_opt", 0.0):.2f}) | Target: {ce_f.get("exp_target_spot", 0.0):.1f} (₹{ce_f.get("exp_target_opt", 0.0):.2f})</div>'
        '</div>'
    )
    pe_box_html = (
        '<div style="background:#1f2430; padding:10px 14px; border-radius:6px; flex:1; min-width:280px; border-left:4px solid #ff5252;">'
        f'<div style="font-size:12px; color:#ff5252; font-weight:bold;">PUT OPTION (PE) • {pe_f.get("symbol", "NIFTY PE")} (Δ: {pe_f.get("delta", 0.5):.2f})</div>'
        f'<div style="font-size:12px; color:#d1d4dc; margin-top:4px;">LTP: <b>₹{pe_f.get("opt_ltp", 0.0):.2f}</b> | Trig: <b>{pe_f.get("exp_entry_spot", 0.0):.1f}</b></div>'
        f'<div style="font-size:11px; color:#90a4ae; margin-top:2px;">SL: {pe_f.get("exp_sl_spot", 0.0):.1f} (₹{pe_f.get("exp_sl_opt", 0.0):.2f}) | Target: {pe_f.get("exp_target_spot", 0.0):.1f} (₹{pe_f.get("exp_target_opt", 0.0):.2f})</div>'
        '</div>'
    )

status_label = "Awaiting 09:30-10:00 Window Trigger" if trades_taken == 0 else "Daily Trade Completed (1/1 Limit Reached)"

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
    f'<div style="font-size:11px; color:#787b86; margin-bottom:6px; font-weight:bold;">EXECUTION & STRIKE MONITOR (BUDGET: ≤ ₹{current_budget:.0f})</div>'
    '<div style="display:flex; gap:12px; flex-wrap:wrap;">'
    + ce_box_html + pe_box_html +
    '</div>'
    '</div>'
    '</div>'
)

st.markdown(complete_banner_html, unsafe_allow_html=True)

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
    const pivots
