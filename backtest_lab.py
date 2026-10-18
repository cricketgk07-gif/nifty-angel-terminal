import datetime
import math
import os
import time
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pyotp
import streamlit as st
import zoneinfo
from dotenv import load_dotenv

load_dotenv()

try:
    from SmartApi import SmartConnect
except ImportError:
    SmartConnect = None

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
INDEX_TOKEN = "99926000"
DATA_CACHE_FILE = "nifty_5m_cache_v17.parquet"

st.set_page_config(page_title="NIFTY ORB Institutional Backtest Lab", layout="wide")

# ==============================================================================
# 1. ANGEL ONE DATA FETCHING WITH 180-DAY PRE-WARMUP
# ==============================================================================
def get_angel_session():
    if SmartConnect is None:
        return None
    api_key = os.getenv("API_KEY", "")
    client_code = os.getenv("CLIENT_CODE", "")
    pin = os.getenv("PIN", "")
    totp_secret = os.getenv("TOTP_SECRET", "")
    if not (api_key and client_code and pin and totp_secret):
        return None
    try:
        api = SmartConnect(api_key)
        totp = pyotp.TOTP(totp_secret).now()
        session = api.generateSession(client_code, pin, totp)
        if session.get("status"):
            return api
    except Exception:
        pass
    return None

def fetch_chunked_5m_candles(api, start_date, end_date):
    fetch_start = start_date - datetime.timedelta(days=180)
    candles = []
    curr_start = fetch_start
    delta_chunk = datetime.timedelta(days=25)

    prog_bar = st.progress(0, text="Fetching raw 5m historical candles from Angel One...")
    total_days = max(1, (end_date - fetch_start).days)

    while curr_start < end_date:
        curr_end = min(curr_start + delta_chunk, end_date)
        try:
            resp = api.getCandleData({
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "FIVE_MINUTE",
                "fromdate": curr_start.strftime("%Y-%m-%d 09:15"),
                "todate": curr_end.strftime("%Y-%m-%d 15:30")
            })
            if resp and resp.get("status") and resp.get("data"):
                for b in resp["data"]:
                    dt = pd.to_datetime(b[0])
                    candles.append({
                        "datetime": dt,
                        "open": float(b[1]),
                        "high": float(b[2]),
                        "low": float(b[3]),
                        "close": float(b[4]),
                        "vol": float(b[5])
                    })
        except Exception:
            pass
        time.sleep(0.3)
        curr_start = curr_end + datetime.timedelta(days=1)
        done_days = min(total_days, (curr_start - fetch_start).days)
        prog_bar.progress(min(1.0, done_days / total_days))

    prog_bar.empty()
    df = pd.DataFrame(candles)
    if not df.empty:
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        df.to_parquet(DATA_CACHE_FILE)
    return df

def get_complete_data(start_date, end_date, force_refresh=False):
    if force_refresh:
        if os.path.exists(DATA_CACHE_FILE):
            os.remove(DATA_CACHE_FILE)
        if "cached_5m_df" in st.session_state:
            del st.session_state["cached_5m_df"]

    df_5m = None
    if not force_refresh and os.path.exists(DATA_CACHE_FILE):
        df_5m = pd.read_parquet(DATA_CACHE_FILE)
    elif "cached_5m_df" in st.session_state and not force_refresh:
        df_5m = st.session_state["cached_5m_df"]
    else:
        api = get_angel_session()
        if api:
            df_5m = fetch_chunked_5m_candles(api, start_date, end_date)
            st.session_state["cached_5m_df"] = df_5m

    return df_5m

# ==============================================================================
# 2. PINE SCRIPT MATH ENGINE (RMA ATR & PURE ta.vwap(hlc3))
# ==============================================================================
def pine_rma(series, length):
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, min_periods=length, adjust=False).mean()

def prepare_indicators(df_5m):
    df = df_5m.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["date"] = df["datetime"].dt.date
    df["hour"] = df["datetime"].dt.hour
    df["minute"] = df["datetime"].dt.minute

    # 1. Daily 10-ATR Lagged by 1 Day: request.security(..., "D", ta.atr(10)[1])
    daily = df.groupby("date").agg({
        "high": "max",
        "low": "min",
        "close": "last"
    }).reset_index()

    d_prev = daily["close"].shift(1)
    d_tr = pd.concat([
        daily["high"] - daily["low"],
        (daily["high"] - d_prev).abs(),
        (daily["low"] - d_prev).abs()
    ], axis=1).max(axis=1)

    daily["daily_atr10"] = pine_rma(d_tr, 10).shift(1)
    df = df.merge(daily[["date", "daily_atr10"]], on="date", how="left")
    df["daily_atr10"] = df["daily_atr10"].ffill().bfill()

    # 2. 5m ATR (14): ta.atr(trailAtrLen)
    prev_c = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_c).abs(),
        (df["low"] - prev_c).abs()
    ], axis=1).max(axis=1)
    df["tr"] = tr
    df["atr14"] = pine_rma(df["tr"], 14).fillna(df["high"] - df["low"])

    # 3. Session VWAP: ta.vwap(hlc3)
    df["hlc3"] = (df["high"] + df["low"] + df["close"]) / 3.0
    has_vol = (df["vol"] > 0).any()
    if has_vol:
        df["pv"] = df["hlc3"] * df["vol"]
        df["cum_vol"] = df.groupby("date")["vol"].cumsum()
        df["cum_pv"] = df.groupby("date")["pv"].cumsum()
        df["vwap"] = (df["cum_pv"] / df["cum_vol"]).round(2)
    else:
        df["vwap"] = df.groupby("date")["hlc3"].expanding().mean().round(2).values

    return df

# ==============================================================================
# 3. POSITION & TRAILING ENGINE (EXACT MATCH WITH PINE SCRIPT)
# ==============================================================================
def run_backtest_simulation(df, p, start_date, end_date):
    trades = []
    all_dates = sorted(df["date"].unique())
    eval_dates = [d for d in all_dates if d >= start_date and d <= end_date]

    for d in eval_dates:
        day_bars = df[df["date"] == d].copy().reset_index(drop=True)
        if len(day_bars) < 6:
            continue

        # ORB Session: 09:15 - 09:30 (First 3 bars: 09:15, 09:20, 09:25)
        orb_bars = day_bars.iloc[0:3]
        orb_h = orb_bars["high"].max()
        orb_l = orb_bars["low"].min()

        d_atr = day_bars["daily_atr10"].iloc[0]
        is_high_vol = (d_atr >= p["vol_threshold"])

        max_high_outside = 0.0
        min_low_outside = 999999.0
        pierced_orbH = False
        pierced_orbL = False

        active_trade = None
        tradesToday = 0

        for i in range(3, len(day_bars)):
            bar = day_bars.iloc[i]
            h_val = bar["hour"]
            m_val = bar["minute"]
            c = bar["close"]
            o = bar["open"]
            h = bar["high"]
            l = bar["low"]
            vw = bar["vwap"]
            atr = bar["atr14"]

            # --- MANAGE ACTIVE TRADE ---
            if active_trade is not None:
                is_ce = (active_trade["type"] == "CE")
                live_sl = active_trade["current_sl"]
                target_p = active_trade["target_p"]
                mode = active_trade["mode"]
                bank_dist = active_trade["bank_dist"]

                exit_triggered = False
                exit_price = 0.0
                exit_reason = ""

                # FADE TRADES: Limit exit at Target
                if mode == "FADE":
                    if is_ce and h >= target_p:
                        exit_triggered = True
                        exit_price = target_p
                        exit_reason = "TARGET HIT"
                    elif (not is_ce) and l <= target_p:
                        exit_triggered = True
                        exit_price = target_p
                        exit_reason = "TARGET HIT"

                # STOP LOSS EXIT (BREAKOUT and FADE)
                if not exit_triggered:
                    if is_ce and l <= live_sl:
                        exit_triggered = True
                        exit_price = live_sl
                        exit_reason = "STOP LOSS HIT"
                    elif (not is_ce) and h >= live_sl:
                        exit_triggered = True
                        exit_price = live_sl
                        exit_reason = "STOP LOSS HIT"

                # MANDATORY 15:15 EOD CLOSE
                if not exit_triggered and (h_val > 15 or (h_val == 15 and m_val >= 15)):
                    exit_triggered = True
                    exit_price = c
                    exit_reason = "15:15 EOD CLOSE"

                if exit_triggered:
                    pts = (exit_price - active_trade["entry"]) if is_ce else (active_trade["entry"] - exit_price)
                    active_trade["exit_price"] = round(exit_price, 2)
                    active_trade["exit_time"] = bar["datetime"]
                    active_trade["exit_reason"] = exit_reason
                    active_trade["pts"] = round(pts, 1)
                    active_trade["pnl"] = round(pts * p["qty"], 2)
                    trades.append(active_trade)
                    active_trade = None
                    tradesToday += 1
                    break

                # BREAKOUT RUNNER TRAILING (Lines 150-180 in Pine Script)
                if mode == "BREAKOUT":
                    if is_ce:
                        active_trade["peak"] = max(active_trade["peak"], h)
                        gain_l = active_trade["peak"] - active_trade["entry"]
                        if gain_l >= bank_dist:
                            active_trade["banked"] = True
                        if active_trade["banked"]:
                            fib_tight_sl = (active_trade["peak"] - (0.236 * gain_l)) - (0.3 * atr)
                            min_guaranteed_sl = active_trade["entry"] + bank_dist - (0.3 * atr)
                            bar_trail_sl = day_bars.iloc[i - 1]["low"] + 2.0
                            active_trade["current_sl"] = max(active_trade["current_sl"], max(fib_tight_sl, max(min_guaranteed_sl, bar_trail_sl)))
                    else:
                        active_trade["peak"] = min(active_trade["peak"], l)
                        gain_s = active_trade["entry"] - active_trade["peak"]
                        if gain_s >= bank_dist:
                            active_trade["banked"] = True
                        if active_trade["banked"]:
                            fib_tight_sl = (active_trade["peak"] + (0.236 * gain_s)) + (0.3 * atr)
                            min_guaranteed_sl = active_trade["entry"] - bank_dist + (0.3 * atr)
                            bar_trail_sl = day_bars.iloc[i - 1]["high"] - 2.0
                            active_trade["current_sl"] = min(active_trade["current_sl"], min(fib_tight_sl, min(min_guaranteed_sl, bar_trail_sl)))
                continue

            # --- STRICT ENTRY WINDOW: 09:30 to 10:00 (Bars 09:30, 09:35, 09:40, 09:45, 09:50, 09:55) ---
            inWindow = (h_val == 9 and m_val >= 30)

            if inWindow and not math.isnan(orb_h):
                if h > orb_h:
                    pierced_orbH = True
                    max_high_outside = max(max_high_outside, h)
                if l < orb_l:
                    pierced_orbL = True
                    min_low_outside = min(min_low_outside, l)

            if inWindow and tradesToday == 0 and active_trade is None:
                fireCE_breakout = False
                firePE_breakout = False
                firePE_fade = False
                fireCE_fade = False

                buf_bo_ce = p["bo_ce_sl_pts"] if p["sl_mode"] == "Fixed Points" else (p["bo_ce_sl_atr"] * atr)
                buf_bo_pe = p["bo_pe_sl_pts"] if p["sl_mode"] == "Fixed Points" else (p["bo_pe_sl_atr"] * atr)
                buf_fade_pe = p["fade_pe_sl_pts"] if p["sl_mode"] == "Fixed Points" else (p["fade_pe_sl_atr"] * atr)
                buf_fade_ce = p["fade_ce_sl_pts"] if p["sl_mode"] == "Fixed Points" else (p["fade_ce_sl_atr"] * atr)

                # MODE A: HIGH VOLATILITY (>= 229) -> PURE MOMENTUM BREAKOUT (Lines 76-80)
                if is_high_vol:
                    if c > orb_h and c > vw and c > o:
                        fireCE_breakout = True
                    if c < orb_l and c < vw and c < o:
                        firePE_breakout = True

                # MODE B: LOW VOLATILITY (< 229) -> SHADOW SWEEP FADE (Lines 83-93)
                elif p["enable_fade"]:
                    shallow_high_breach = (max_high_outside - orb_h) <= (p["max_breach_mult"] * atr)
                    shallow_low_breach = (orb_l - min_low_outside) <= (p["max_breach_mult"] * atr)
                    decisive_pe_close = c <= (orb_h - (0.2 * atr)) and c < o
                    decisive_ce_close = c >= (orb_l + (0.2 * atr)) and c > o

                    if pierced_orbH and shallow_high_breach and decisive_pe_close:
                        firePE_fade = True
                    if pierced_orbL and shallow_low_breach and decisive_ce_close:
                        fireCE_fade = True

                # EXECUTION ON BAR CLOSE (process_orders_on_close = true)
                if fireCE_breakout:
                    b_dist = p["bo_ce_tg_pts"] if p["tg_mode"] == "Fixed Points" else (atr * p["bo_ce_tg_atr"])
                    active_trade = {
                        "date": d,
                        "entry_time": bar["datetime"],
                        "type": "CE",
                        "mode": "BREAKOUT",
                        "entry": c,
                        "daily_atr": round(d_atr, 1),
                        "current_sl": round(l - buf_bo_ce, 2),
                        "target_p": round(c + b_dist, 2),
                        "bank_dist": b_dist,
                        "banked": False,
                        "peak": c
                    }

                elif firePE_breakout:
                    b_dist = p["bo_pe_tg_pts"] if p["tg_mode"] == "Fixed Points" else (atr * p["bo_pe_tg_atr"])
                    active_trade = {
                        "date": d,
                        "entry_time": bar["datetime"],
                        "type": "PE",
                        "mode": "BREAKOUT",
                        "entry": c,
                        "daily_atr": round(d_atr, 1),
                        "current_sl": round(h + buf_bo_pe, 2),
                        "target_p": round(c - b_dist, 2),
                        "bank_dist": b_dist,
                        "banked": False,
                        "peak": c
                    }

                elif firePE_fade:
                    t_val = vw if p["tg_mode"] == "Structural (VWAP)" else (c - p["fade_pe_tg_pts"] if p["tg_mode"] == "Fixed Points" else c - (atr * p["fade_pe_tg_atr"]))
                    active_trade = {
                        "date": d,
                        "entry_time": bar["datetime"],
                        "type": "PE",
                        "mode": "FADE",
                        "entry": c,
                        "daily_atr": round(d_atr, 1),
                        "current_sl": round(max_high_outside + buf_fade_pe, 2),
                        "target_p": round(t_val, 2),
                        "bank_dist": 0.0,
                        "banked": False,
                        "peak": c
                    }

                elif fireCE_fade:
                    t_val = vw if p["tg_mode"] == "Structural (VWAP)" else (c + p["fade_ce_tg_pts"] if p["tg_mode"] == "Fixed Points" else c + (atr * p["fade_ce_tg_atr"]))
                    active_trade = {
                        "date": d,
                        "entry_time": bar["datetime"],
                        "type": "CE",
                        "mode": "FADE",
                        "entry": c,
                        "daily_atr": round(d_atr, 1),
                        "current_sl": round(min_low_outside - buf_fade_ce, 2),
                        "target_p": round(t_val, 2),
                        "bank_dist": 0.0,
                        "banked": False,
                        "peak": c
                    }

    return pd.DataFrame(trades)

# ==============================================================================
# 4. STREAMLIT UI & INTERACTIVE DASHBOARD
# ==============================================================================
st.sidebar.title("Backtest Configuration")
force_reload = st.sidebar.button("Force Refresh & Re-download Data", type="primary")

exec_mode = st.sidebar.radio("Execution Mode", ["Manual Input Backtest", "Automated Grid Optimizer"])

st.sidebar.subheader("Date Horizon")
col_s1, col_s2, col_s3 = st.sidebar.columns(3)
start_year = col_s1.selectbox("Start Year", list(range(2015, 2027)), index=11)
start_month = col_s2.selectbox("Start Month", list(range(1, 13)), index=6)
start_day = col_s3.selectbox("Start Day", list(range(1, 32)), index=5)

col_e1, col_e2, col_e3 = st.sidebar.columns(3)
end_year = col_e1.selectbox("End Year", list(range(2015, 2027)), index=11)
end_month = col_e2.selectbox("End Month", list(range(1, 13)), index=9)
end_day = col_e3.selectbox("End Day", list(range(1, 32)), index=15)

d_start = datetime.date(start_year, start_month, start_day)
d_end = datetime.date(end_year, end_month, end_day)

st.sidebar.subheader("Macro Strategy Parameters")
vol_threshold = st.sidebar.number_input("Macro Regime Threshold (Daily 10-ATR)", value=229.0, step=5.0)
enable_fade = st.sidebar.checkbox("Trade Fades when Daily ATR < 229", value=True)
max_breach_mult = st.sidebar.number_input("Max Breach Allowance (x ATR)", value=0.5, step=0.1)

st.sidebar.subheader("Independent Stop Loss Settings")
sl_mode = st.sidebar.selectbox("SL Buffer Mode", ["Fixed Points", "ATR Multiplier"], index=0)
bo_ce_sl_pts = st.sidebar.number_input("BO CE SL: Points below Low", value=5.0, step=1.0)
bo_pe_sl_pts = st.sidebar.number_input("BO PE SL: Points above High", value=105.0, step=5.0)
fade_pe_sl_pts = st.sidebar.number_input("Fade PE SL: Points above Breach High", value=25.0, step=1.0)
fade_ce_sl_pts = st.sidebar.number_input("Fade CE SL: Points below Breach Low", value=65.0, step=1.0)

bo_ce_sl_atr = 0.3
bo_pe_sl_atr = 0.3
fade_pe_sl_atr = 0.3
fade_ce_sl_atr = 0.3

st.sidebar.subheader("Independent Target Settings")
tg_mode = st.sidebar.selectbox("Target Mode", ["ATR Multiplier", "Fixed Points", "Structural (VWAP)"], index=0)
bo_ce_tg_pts = 105.0
bo_pe_tg_pts = 105.0
fade_pe_tg_pts = 40.0
fade_ce_tg_pts = 40.0

bo_ce_tg_atr = st.sidebar.number_input("BO CE Target: ATR Multiplier", value=3.5, step=0.1)
bo_pe_tg_atr = st.sidebar.number_input("BO PE Target: ATR Multiplier", value=5.0, step=0.1)
fade_pe_tg_atr = st.sidebar.number_input("Fade PE Target: ATR Multiplier", value=1.9, step=0.1)
fade_ce_tg_atr = st.sidebar.number_input("Fade CE Target: ATR Multiplier", value=1.4, step=0.1)

st.title("NIFTY Dual-Regime Master: Institutional Backtest Lab")

df_5m = get_complete_data(d_start, d_end, force_refresh=force_reload)

if df_5m is None or df_5m.empty:
    st.info("Awaiting historical data. Provide Angel One API credentials in Render Environment Variables or verify cache.")
    st.stop()

df_prepared = prepare_indicators(df_5m)
st.success(f"Ready: Loaded {len(df_prepared):,} bars across {df_prepared['date'].nunique()} trading sessions.")

sim_params = {
    "vol_threshold": vol_threshold,
    "enable_fade": enable_fade,
    "max_breach_mult": max_breach_mult,
    "sl_mode": sl_mode,
    "bo_ce_sl_pts": bo_ce_sl_pts,
    "bo_pe_sl_pts": bo_pe_sl_pts,
    "fade_pe_sl_pts": fade_pe_sl_pts,
    "fade_ce_sl_pts": fade_ce_sl_pts,
    "bo_ce_sl_atr": bo_ce_sl_atr,
    "bo_pe_sl_atr": bo_pe_sl_atr,
    "fade_pe_sl_atr": fade_pe_sl_atr,
    "fade_ce_sl_atr": fade_ce_sl_atr,
    "tg_mode": tg_mode,
    "bo_ce_tg_pts": bo_ce_tg_pts,
    "bo_pe_tg_pts": bo_pe_tg_pts,
    "fade_pe_tg_pts": fade_pe_tg_pts,
    "fade_ce_tg_pts": fade_ce_tg_pts,
    "bo_ce_tg_atr": bo_ce_tg_atr,
    "bo_pe_tg_atr": bo_pe_tg_atr,
    "fade_pe_tg_atr": fade_pe_tg_atr,
    "fade_ce_tg_atr": fade_ce_tg_atr,
    "qty": 65
}

if exec_mode == "Manual Input Backtest":
    df_trades = run_backtest_simulation(df_prepared, sim_params, d_start, d_end)

    if df_trades.empty:
        st.warning("No trades triggered for this date range with the selected settings.")
        st.stop()

    df_trades["cum_pnl"] = df_trades["pnl"].cumsum()
    total_trades = len(df_trades)
    wins = len(df_trades[df_trades["pnl"] > 0])
    losses = len(df_trades[df_trades["pnl"] <= 0])
    win_rate = (wins / total_trades) * 100
    gross_profit = df_trades[df_trades["pnl"] > 0]["pnl"].sum()
    gross_loss = abs(df_trades[df_trades["pnl"] < 0]["pnl"].sum())
    profit_factor = gross_profit / max(1.0, gross_loss)
    total_pnl = df_trades["pnl"].sum()

    cum_max = df_trades["cum_pnl"].cummax()
    drawdown = cum_max - df_trades["cum_pnl"]
    max_dd = drawdown.max()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total PnL", f"₹{total_pnl:,.2f}", f"{win_rate:.1f}% Win Rate")
    c2.metric("Profit Factor", f"{profit_factor:.2f}")
    c3.metric("Trades (W / L)", f"{total_trades} ({wins} / {losses})")
    c4.metric("Max Drawdown", f"₹{max_dd:,.2f}")

    fig_equity = go.Figure()
    fig_equity.add_trace(go.Scatter(
        x=df_trades["exit_time"],
        y=df_trades["cum_pnl"],
        mode="lines+markers",
        name="Cumulative PnL",
        line=dict(color="#00e676", width=2),
        marker=dict(size=4)
    ))
    fig_equity.update_layout(
        title="TradingView Matching Cumulative Equity Curve",
        plot_bgcolor="#131722",
        paper_bgcolor="#131722",
        font=dict(color="#d1d4dc"),
        xaxis=dict(gridcolor="#1f2430"),
        yaxis=dict(gridcolor="#1f2430", title="Profit & Loss (₹)")
    )
    st.plotly_chart(fig_equity, use_container_width=True)

    st.subheader("Historical Trade Ledger (Full Trade List)")
    st.dataframe(df_trades[["entry_time", "type", "mode", "daily_atr", "entry", "current_sl", "exit_price", "exit_time", "exit_reason", "pts", "pnl"]], use_container_width=True)

else:
    st.subheader("Master Multi-Parameter Optimizer")
    st.write("Simulates combinations across Stop Losses AND Targets to extract optimal values.")

    opt_focus = st.selectbox("Select Optimization Scope", [
        "Master (CE & PE: Both SL and Targets)",
        "Fade SL & Targets",
        "Target Multipliers Only"
    ])

    if st.button("Start Grid Optimization"):
        results = []

        if opt_focus == "Master (CE & PE: Both SL and Targets)":
            grid_ce_tg = [3.0, 3.5, 4.0]
            grid_pe_tg = [4.0, 5.0, 6.0]
            grid_ce_sl = [5.0, 10.0]
            grid_pe_sl = [90.0, 105.0, 117.0]

            total_sims = len(grid_ce_tg) * len(grid_pe_tg) * len(grid_ce_sl) * len(grid_pe_sl)
            prog = st.progress(0, text="Evaluating Master SL + Target Grid...")
            idx = 0

            for ct in grid_ce_tg:
                for pt in grid_pe_tg:
                    for c_sl in grid_ce_sl:
                        for p_sl in grid_pe_sl:
                            p_copy = sim_params.copy()
                            p_copy["bo_ce_tg_atr"] = ct
                            p_copy["bo_pe_tg_atr"] = pt
                            p_copy["bo_ce_sl_pts"] = c_sl
                            p_copy["bo_pe_sl_pts"] = p_sl

                            tdf = run_backtest_simulation(df_prepared, p_copy, d_start, d_end)
                            if not tdf.empty:
                                gp = tdf[tdf["pnl"] > 0]["pnl"].sum()
                                gl = abs(tdf[tdf["pnl"] < 0]["pnl"].sum())
                                pf = gp / max(1.0, gl)
                                net = tdf["pnl"].sum()
                                wr = (len(tdf[tdf["pnl"] > 0]) / len(tdf)) * 100

                                results.append({
                                    "BO CE SL (Pts)": c_sl,
                                    "BO PE SL (Pts)": p_sl,
                                    "Fade CE SL (Pts)": p_copy["fade_ce_sl_pts"],
                                    "Fade PE SL (Pts)": p_copy["fade_pe_sl_pts"],
                                    "BO CE Target (ATR)": ct,
                                    "BO PE Target (ATR)": pt,
                                    "Fade CE Target (ATR)": p_copy["fade_ce_tg_atr"],
                                    "Fade PE Target (ATR)": p_copy["fade_pe_tg_atr"],
                                    "Profit Factor": round(pf, 2),
                                    "Win Rate (%)": round(wr, 1),
                                    "Total Trades": len(tdf),
                                    "Net PnL (₹)": int(net)
                                })
                            idx += 1
                            prog.progress(idx / total_sims)
            prog.empty()

        elif opt_focus == "Fade SL & Targets":
            grid_f_pe_tg = [1.5, 1.9, 2.5]
            grid_f_ce_tg = [1.2, 1.4, 1.8]
            grid_f_pe_sl = [15.0, 25.0, 35.0]
            grid_f_ce_sl = [40.0, 50.0, 65.0]

            total_sims = len(grid_f_pe_tg) * len(grid_f_ce_tg) * len(grid_f_pe_sl) * len(grid_f_ce_sl)
            prog = st.progress(0, text="Evaluating Fade SL + Target Grid...")
            idx = 0

            for fpt in grid_f_pe_tg:
                for fct in grid_f_ce_tg:
                    for fps in grid_f_pe_sl:
                        for fcs in grid_f_ce_sl:
                            p_copy = sim_params.copy()
                            p_copy["fade_pe_tg_atr"] = fpt
                            p_copy["fade_ce_tg_atr"] = fct
                            p_copy["fade_pe_sl_pts"] = fps
                            p_copy["fade_ce_sl_pts"] = fcs

                            tdf = run_backtest_simulation(df_prepared, p_copy, d_start, d_end)
                            if not tdf.empty:
                                gp = tdf[tdf["pnl"] > 0]["pnl"].sum()
                                gl = abs(tdf[tdf["pnl"] < 0]["pnl"].sum())
                                pf = gp / max(1.0, gl)
                                net = tdf["pnl"].sum()
                                wr = (len(tdf[tdf["pnl"] > 0]) / len(tdf)) * 100

                                results.append({
                                    "BO CE SL (Pts)": p_copy["bo_ce_sl_pts"],
                                    "BO PE SL (Pts)": p_copy["bo_pe_sl_pts"],
                                    "Fade CE SL (Pts)": fcs,
                                    "Fade PE SL (Pts)": fps,
                                    "BO CE Target (ATR)": p_copy["bo_ce_tg_atr"],
                                    "BO PE Target (ATR)": p_copy["bo_pe_tg_atr"],
                                    "Fade CE Target (ATR)": fct,
                                    "Fade PE Target (ATR)": fpt,
                                    "Profit Factor": round(pf, 2),
                                    "Win Rate (%)": round(wr, 1),
                                    "Total Trades": len(tdf),
                                    "Net PnL (₹)": int(net)
                                })
                            idx += 1
                            prog.progress(idx / total_sims)
            prog.empty()

        else:
            grid_ce_tg = [2.5, 3.0, 3.5, 4.0]
            grid_pe_tg = [3.5, 4.0, 5.0, 6.0]

            total_sims = len(grid_ce_tg) * len(grid_pe_tg)
            prog = st.progress(0, text="Evaluating Targets...")
            idx = 0

            for ct in grid_ce_tg:
                for pt in grid_pe_tg:
                    p_copy = sim_params.copy()
                    p_copy["bo_ce_tg_atr"] = ct
                    p_copy["bo_pe_tg_atr"] = pt

                    tdf = run_backtest_simulation(df_prepared, p_copy, d_start, d_end)
                    if not tdf.empty:
                        gp = tdf[tdf["pnl"] > 0]["pnl"].sum()
                        gl = abs(tdf[tdf["pnl"] < 0]["pnl"].sum())
                        pf = gp / max(1.0, gl)
                        net = tdf["pnl"].sum()
                        wr = (len(tdf[tdf["pnl"] > 0]) / len(tdf)) * 100

                        results.append({
                            "BO CE SL (Pts)": p_copy["bo_ce_sl_pts"],
                            "BO PE SL (Pts)": p_copy["bo_pe_sl_pts"],
                            "Fade CE SL (Pts)": p_copy["fade_ce_sl_pts"],
                            "Fade PE SL (Pts)": p_copy["fade_pe_sl_pts"],
                            "BO CE Target (ATR)": ct,
                            "BO PE Target (ATR)": pt,
                            "Fade CE Target (ATR)": p_copy["fade_ce_tg_atr"],
                            "Fade PE Target (ATR)": p_copy["fade_pe_tg_atr"],
                            "Profit Factor": round(pf, 2),
                            "Win Rate (%)": round(wr, 1),
                            "Total Trades": len(tdf),
                            "Net PnL (₹)": int(net)
                        })
                    idx += 1
                    prog.progress(idx / total_sims)
            prog.empty()

        if results:
            res_df = pd.DataFrame(results).sort_values("Profit Factor", ascending=False).reset_index(drop=True)
            st.subheader("Optimal Parameter Configurations")
            st.dataframe(res_df, use_container_width=True)
