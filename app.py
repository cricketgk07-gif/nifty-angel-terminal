import html
import json
import os
import time
import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="NIFTY 50 ORB Terminal",
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


# -------------------------------------------------------------
# Read Live Trading State
# -------------------------------------------------------------
state_data = read_redis_key("nifty_live_state") or {}

ltp = float(state_data.get("ltp", 25000.0))
opt_ltp = float(state_data.get("opt_ltp", 0.0))
candles = state_data.get("candles", [])
markers = state_data.get("markers", [])
history_cards = state_data.get("history_cards", {})
pivots = state_data.get("pivots", {})
orb_levels = state_data.get("orb_levels", {})
fib_lines = state_data.get("fib_lines", {})
hud = state_data.get("hud", {})
updated_at = state_data.get("updated_at", 0)

saved_budget = read_redis_key("user_selected_budget")
current_budget = float(saved_budget) if saved_budget is not None else BUDGET_CEILING_DEFAULT

# -------------------------------------------------------------
# Header Bar & Budget Controls
# -------------------------------------------------------------
col_title, col_budget, col_refresh = st.columns([3, 2, 1])

with col_title:
    st.markdown(
        f"<h3 style='margin:0; padding:0; color:#e0e0e0; font-family:sans-serif;'>"
        f"NIFTY 50 Opening Range Terminal "
        f"<span style='color:#00e676; font-size:18px;'>₹{ltp:.2f}</span>"
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
    if st.button("🔄 Refresh"):
        st.rerun()

# -------------------------------------------------------------
# Active Trade HUD Card
# -------------------------------------------------------------
if hud:
    trade_type = hud.get("type", "CE")
    badge_bg = "#00c853" if trade_type == "CE" else "#d50000"
    symbol = hud.get("symbol", f"NIFTY {trade_type}")
    entry_spot = float(hud.get("entry_spot", 0.0))
    entry_opt = float(hud.get("entry_opt", 0.0))
    trail_spot = float(hud.get("trail_spot", 0.0))
    trail_opt = float(hud.get("trail_opt", 0.0))
    secured_pts = float(hud.get("secured_pts", 0.0))
    sl_spot = float(hud.get("sl_spot", 0.0))
    sl_opt = float(hud.get("sl_opt", 0.0))
    risk_pts = float(hud.get("risk_pts", 0.0))
    is_closed = hud.get("trade_closed", False)
    status_label = "CLOSED (STOP HIT)" if is_closed else "ACTIVE RUNNER"

    tg_html = ""
    for tg in hud.get("targets", []):
        tg_html += (
            f"<div style='background:#1f2430; padding:6px 10px; border-radius:4px; text-align:center; min-width:110px;'>"
            f"<div style='font-size:11px; color:#90a4ae;'>{tg.get('label')}</div>"
            f"<div style='font-size:13px; font-weight:bold; color:#ffb74d;'>{tg.get('spot', 0.0):.1f}</div>"
            f"<div style='font-size:11px; color:#81c784;'>₹{tg.get('opt', 0.0):.2f} (+{tg.get('pts', 0.0):.1f}p)</div>"
            f"</div>"
        )

    st.markdown(
        f"""
        <div style="background:#131722; border:1px solid #2a2e39; border-radius:8px; padding:12px 16px; margin-bottom:12px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;">
                <div style="display:flex; align-items:center; gap:8px;">
                    <span style="background:{badge_bg}; color:#fff; font-size:12px; font-weight:bold; padding:3px 8px; border-radius:4px;">{trade_type}</span>
                    <span style="color:#ffffff; font-size:15px; font-weight:bold;">{symbol}</span>
                    <span style="color:#787b86; font-size:12px;">(Delta: {hud.get('delta', 0.5):.2f})</span>
                </div>
                <div style="font-size:12px; color:{'#ff5252' if is_closed else '#00e676'}; font-weight:bold;">
                    ● {status_label}
                </div>
            </div>
            <div style="display:flex; gap:12px; align-items:stretch; flex-wrap:wrap;">
                <div style="background:#1f2430; padding:6px 12px; border-radius:4px; min-width:130px;">
                    <div style="font-size:11px; color:#90a4ae;">ENTRY</div>
                    <div style="font-size:14px; font-weight:bold; color:#fff;">{entry_spot:.1f}</div>
                    <div style="font-size:11px; color:#4fc3f7;">LTP: ₹{entry_opt:.2f}</div>
                </div>
                <div style="background:#1f2430; padding:6px 12px; border-radius:4px; min-width:130px;">
                    <div style="font-size:11px; color:#90a4ae;">INITIAL SL</div>
                    <div style="font-size:14px; font-weight:bold; color:#ef5350;">{sl_spot:.1f}</div>
                    <div style="font-size:11px; color:#ef5350;">- {risk_pts:.1f} pts (₹{sl_opt:.2f})</div>
                </div>
                <div style="background:#1f2430; padding:6px 12px; border-radius:4px; min-width:130px;">
                    <div style="font-size:11px; color:#90a4ae;">CURRENT TRAIL SL</div>
                    <div style="font-size:14px; font-weight:bold; color:#00e676;">{trail_spot:.1f}</div>
                    <div style="font-size:11px; color:#00e676;">{'Locked: +' if secured_pts >= 0 else 'Risk: '}{secured_pts:.1f} pts (₹{trail_opt:.2f})</div>
                </div>
                <div style="display:flex; gap:8px; overflow-x:auto;">
                    {tg_html}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# -------------------------------------------------------------
# Lightweight Charts Injection with Levels up to R5 / S5
# -------------------------------------------------------------
candles_json = json.dumps(candles)
markers_json = json.dumps(markers)
history_cards_json = json.dumps(history_cards)
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
      margin: 0;
      padding: 0;
      background-color: #131722;
      color: #d1d4dc;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      overflow: hidden;
    }}
    #chart_container {{
      width: 100vw;
      height: 620px;
      position: relative;
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
      min-width: 200px;
    }}
  </style>
</head>
<body>
  <div id="chart_container">
    <div id="hover_card"></div>
  </div>

  <script>
    const candlesData = {candles_json};
    const markersData = {markers_json};
    const historyCards = {history_cards_json};
    const pivots = {pivots_json};
    const orbLevels = {orb_json};
    const fibLines = {fib_json};

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

    candleSeries.setData(candlesData);

    const vwapSeries = chart.addLineSeries({{
      color: '#ff9800',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'VWAP',
    }});
    vwapSeries.setData(candlesData.filter(d => d.vwap).map(d => ({{ time: d.time, value: d.vwap }})));

    const emaSeries = chart.addLineSeries({{
      color: '#2962ff',
      lineWidth: 1,
      priceLineVisible: false,
      title: 'EMA 9',
    }});
    emaSeries.setData(candlesData.filter(d => d.ema9).map(d => ({{ time: d.time, value: d.ema9 }})));

    if (markersData && markersData.length > 0) {{
      candleSeries.setMarkers(markersData);
    }}

    // ==========================================
    // Render Complete Pivot Lines up to R5 / S5
    // ==========================================
    const pivotConfig = {{
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

    Object.entries(pivots || {{}}).forEach(([k, val]) => {{
      if (val && pivotConfig[k]) {{
        candleSeries.createPriceLine({{
          price: parseFloat(val),
          color: pivotConfig[k].color,
          lineWidth: 1,
          lineStyle: LightweightCharts.LineStyle.Dashed,
          axisLabelVisible: true,
          title: pivotConfig[k].title,
        }});
      }}
    }});

    // Render ORB Boundaries
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

    // Render Fibonacci Projection Lines
    if (fibLines) {{
      Object.entries(fibLines).forEach(([fKey, fVal]) => {{
        if (fVal) {{
          candleSeries.createPriceLine({{
            price: parseFloat(fVal),
            color: '#b388ff',
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dotted,
            axisLabelVisible: true,
            title: fKey.toUpperCase().replace('_', ' '),
          }});
        }}
      }});
    }}

    // Signal Hover Card Handler
    chart.subscribeCrosshairMove(param => {{
      if (!param || !param.time || !param.point) {{
        hoverCard.style.display = 'none';
        return;
      }}
      const tKey = String(param.time);
      const cardData = historyCards[tKey];
      if (cardData) {{
        const isCe = cardData.type === 'CE';
        const color = isCe ? '#00e676' : '#ff5252';
        let targetsHtml = '';
        if (cardData.targets && cardData.targets.length > 0) {{
          targetsHtml = '<div style="margin-top:4px; font-size:11px; color:#90a4ae;"><b>Targets:</b> ' +
            cardData.targets.slice(0, 4).join(', ') + '</div>';
        }}
        hoverCard.innerHTML = `
          <div style="font-weight:bold; color:${{color}}; margin-bottom:4px;">${{cardData.title}}</div>
          <div><b>Time:</b> ${{cardData.time_str}} | ${{cardData.date_readable || ''}}</div>
          <div><b>Entry:</b> ${{cardData.entry}} | <b>SL:</b> ${{cardData.sl}}</div>
          <div><b>Status:</b> ${{cardData.outcome || 'ACTIVE'}}</div>
          ${{targetsHtml}}
        `;
        hoverCard.style.display = 'block';
        hoverCard.style.left = Math.min(param.point.x + 15, container.clientWidth - 220) + 'px';
        hoverCard.style.top = Math.max(param.point.y - 70, 10) + 'px';
      }} else {{
        hoverCard.style.display = 'none';
      }}
    }});

    window.addEventListener('resize', () => {{
      chart.applyOptions({{ width: container.clientWidth }});
    }});
  </script>
</body>
</html>
"""

components.html(html_code, height=640)
