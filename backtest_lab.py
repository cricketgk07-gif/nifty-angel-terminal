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
DATA_CACHE_FILE = "nifty_5m_historical_cache.parquet"

st.set_page_config(page_title="NIFTY ORB Institutional Backtest Lab", layout="wide")

# ==============================================================================
# 1. ANGEL ONE HISTORICAL DATA DOWNLOADER & LOCAL CACHE
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

def fetch_chunked_data(api, start_date, end_date):
    candles = []
    curr_start = start_date
    delta_chunk = datetime.timedelta(days=28)

    prog_bar = st.progress(0, text="Downloading Multi-Year Historical 5m Candles...")
    total_days = max(1, (end_date - start_date).days)

    while curr_start < end_date:
        curr_end = min(curr_start + delta_chunk, end_date)
        from_str = curr_start.strftime("%Y-%m-%d 09:15")
        to_str = curr_end.strftime("%Y-%m-%d 15:30")
        try:
            resp = api.getCandleData({
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "FIVE_MINUTE",
                "fromdate": from_str,
                "todate": to_str
            })
            if resp and resp.get("status") and resp.get("data"):
                for b in resp["data"]:
                    candles.append({
                        "datetime": pd.to_datetime(b[0]),
                        "open": float(b[1]),
                        "high": float(b[2]),
                        "low": float(b[3]),
                        "close": float(b[4]),
                        "vol": float(b[5])
                    })
        except Exception:
            pass
        time.sleep(0.35)
        curr_start = curr_end + datetime.timedelta(days=1)
        done_days = min(total_days, (curr_start - start_date).days)
        prog_bar.progress(min(1.0, done_days / total_days))

    prog_bar.empty()
    df = pd.DataFrame(candles)
    if not df.empty:
        df["datetime"] = df["datetime"].dt.tz_localize(None)
        df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        df.to_parquet(DATA_CACHE_FILE)
    return df

@st.cache_data(show_spinner=False)
def load_historical_data(start_date, end_date):
    if os.path.exists(DATA_CACHE_FILE):
        df = pd.read_parquet(DATA_CACHE_FILE)
        df["datetime"] = pd.to_datetime(df["datetime"])
        if df["datetime"].min().date() <= start_date and df["datetime"].max().date() >= (end_date - datetime.timedelta(days=5)):
            mask = (df["datetime"].dt.date >= start_date) & (df["datetime"].dt.date <= end_date)
            return df[mask].copy().reset_index(drop=True)

    api = get_angel_session()
    if api is not None:
        return fetch_chunked_data(api, start_date, end_date)
    return pd.DataFrame()

# ==============================================================================
# 2. STRATEGY EXECUTION ENGINE
# ==============================================================================
def prepare_indicators(df):
    df["date"] = df["datetime"].dt.date
    df["time"] = df["datetime"].dt.time
    
    # 5m ATR (14)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr14"] = df["tr"].rolling(14).mean().fillna(14.0)

    # Intraday VWAP
    df["hlc3"] = (df["high"] + df["low"] + df["close"]) / 3.0
    spread = (df["high"] - df["low"]) + (df["close"] - df["open"]).abs()
    df["vol_weight"] = spread * 1250.0 + 500.0
    df["pv"] = df["hlc3"] * df["vol_weight"]
    df["cum_vol"] = df.groupby("date")["vol_weight"].cumsum()
    df["cum_pv"] = df.groupby("date")["pv"].cumsum()
    df["vwap"] = (df["cum_pv"] / df["cum_vol"]).round(2)

    # Daily 10-ATR (Closed Bars, Lookahead Disabled)
    daily = df.groupby("date").agg({"high": "max", "low": "min", "close": "last"}).reset_index()
    d_tr1 = daily["high"] - daily["low"]
    d_tr2 = (daily["high"] - daily["close"].shift(1)).abs()
    d_tr3 = (daily["low"] - daily["close"].shift(1)).abs()
    daily["d_tr"] = pd.concat([d_tr1, d_tr2, d_tr3], axis=1).max(axis=1)
    daily["daily_atr10"] = daily["d_tr"].rolling(10).mean().shift(1).fillna(180.0)
    
    df = df.merge(daily[["date", "daily_atr10"]], on="date", how="left")
    return df

def run_backtest_simulation(df, p):
    trades = []
    dates = df["date"].unique()

    for d in dates:
        day_bars = df[df["date"] == d].copy().reset_index(drop=True)
        if len(day_bars) < 6:
            continue

        orb_bars = day_bars[(day_bars["time"] >= datetime.time(9, 15)) & (day_bars["time"] < datetime.time(9, 30))]
        if len(orb_bars) == 0:
            orb_bars = day_bars.iloc[0:3]
            
        orb_h = orb_bars["high"].max()
        orb_l = orb_bars["low"].min()
        d_atr = day_bars["daily_atr10"].iloc[0]
        is_high_vol = (d_atr >= p["vol_threshold"])

        max_high_outside = orb_h
        min_low_outside = orb_l
        pierced_h = False
        pierced_l = False

        active_trade = None

        for i in range(len(day_bars)):
            bar = day_bars.iloc[i]
            b_time = bar["time"]
            c = bar["close"]
            o = bar["open"]
            h = bar["high"]
            l = bar["low"]
            vw = bar["vwap"]
            atr = bar["atr14"]

            if active_trade is not None:
                is_ce = (active_trade["type"] == "CE")
                cur_sl = active_trade["current_sl"]
                target_p = active_trade["target_p"]
                mode = active_trade["mode"]
                bank_dist = active_trade["bank_dist"]

                exit_triggered = False
                exit_price = 0.0
                exit_reason = ""

                if is_ce and h >= target_p:
                    exit_triggered = True
                    exit_price = target_p
                    exit_reason = "TARGET HIT"
                elif (not is_ce) and l <= target_p:
                    exit_triggered = True
                    exit_price = target_p
                    exit_reason = "TARGET HIT"

                if not exit_triggered:
                    if is_ce and l <= cur_sl:
                        exit_triggered = True
                        exit_price = cur_sl
                        exit_reason = "STOP LOSS HIT"
                    elif (not is_ce) and h >= cur_sl:
                        exit_triggered = True
                        exit_price = cur_sl
                        exit_reason = "STOP LOSS HIT"

                if not exit_triggered and b_time >= datetime.time(15, 15):
                    exit_triggered = True
                    exit_price = c
                    exit_reason = "15:15 EOD EXIT"

                if exit_triggered:
                    pts = (exit_price - active_trade["entry"]) if is_ce else (active_trade["entry"] - exit_price)
                    active_trade["exit_price"] = exit_price
                    active_trade["exit_time"] = bar["datetime"]
                    active_trade["exit_reason"] = exit_reason
                    active_trade["pts"] = round(pts, 1)
                    active_trade["pnl"] = round(pts * p["qty"], 2)
                    trades.append(active_trade)
                    active_trade = None
                    break

                if mode == "BREAKOUT":
                    if is_ce:
                        active_trade["peak"] = max(active_trade["peak"], h)
                        gain = active_trade["peak"] - active_trade["entry"]
                        if gain >= bank_dist:
                            active_trade["banked"] = True
                        if active_trade["banked"]:
                            fib_sl = (active_trade["peak"] - (0.236 * gain)) - (0.3 * atr)
                            min_sl = active_trade["entry"] + bank_dist - (0.3 * atr)
                            bar_sl = day_bars.iloc[i - 1]["low"] + 2.0
                            active_trade["current_sl"] = max(active_trade["current_sl"], max(fib_sl, max(min_sl, bar_sl)))
                    else:
                        active_trade["peak"] = min(active_trade["peak"], l)
                        gain = active_trade["entry"] - active_trade["peak"]
                        if gain >= bank_dist:
                            active_trade["banked"] = True
                        if active_trade["banked"]:
                            fib_sl = (active_trade["peak"] + (0.236 * gain)) + (0.3 * atr)
                            min_sl = active_trade["entry"] - bank_dist + (0.3 * atr)
                            bar_sl = day_bars.iloc[i - 1]["high"] - 2.0
                            active_trade["current_sl"] = min(active_trade["current_sl"], min(fib_sl, min(min_sl, bar_sl)))
                continue

            if datetime.time(9, 30) <= b_time < datetime.time(10, 0):
                if h > orb_h:
                    pierced_h = True
                    max_high_outside = max(max_high_outside, h)
                if l < orb_l:
                    pierced_l = True
                    min_low_outside = min(min_low_outside, l)

                fire_ce_bo = False
                fire_pe_bo = False
                fire_pe_fade = False
                fire_ce_fade = False

                if is_high_vol:
                    if c > orb_h and c > vw and c > o:
                        fire_ce_bo = True
                    elif c < orb_l and c < vw and c < o:
                        fire_pe_bo = True
                elif p["enable_fade"]:
                    shallow_h = (max_high_outside - orb_h) <= (p["max_breach_mult"] * atr)
                    shallow_l = (orb_l - min_low_outside) <= (p["max_breach_mult"] * atr)
                    decisive_pe = c <= (orb_h - (0.2 * atr)) and c < o
                    decisive_ce = c >= (orb_l + (0.2 * atr)) and c > o
                    if pierced_h and shallow_h and decisive_pe:
                        fire_pe_fade = True
                    if pierced_l and shallow_l and decisive_ce:
                        fire_ce_fade = True

                trade_type = None
                mode = ""
                sl_val = 0.0
                tg_val = 0.0
                bank_d = 0.0

                if fire_ce_bo:
                    trade_type = "CE"
                    mode = "BREAKOUT"
                    buf = p["bo_ce_sl_pts"] if p["sl_mode"] == "Fixed Points" else (p["bo_ce_sl_atr"] * atr)
                    sl_val = round(l - buf, 1)
                    bank_d = atr * p["bo_ce_tg_atr"]
                    tg_val = round(c + bank_d, 1)
                elif fire_pe_bo:
                    trade_type = "PE"
                    mode = "BREAKOUT"
                    buf = p["bo_pe_sl_pts"] if p["sl_mode"] == "Fixed Points" else (p["bo_pe_sl_atr"] * atr)
                    sl_val = round(h + buf, 1)
                    bank_d = atr * p["bo_pe_tg_atr"]
                    tg_val = round(c - bank_d, 1)
                elif fire_pe_fade:
                    trade_type = "PE"
                    mode = "FADE"
                    buf = p["fade_pe_sl_pts"] if p["sl_mode"] == "Fixed Points" else (p["fade_pe_sl_atr"] * atr)
                    sl_val = round(max_high_outside + buf, 1)
                    tg_val = round(c - (atr * p["fade_pe_tg_atr"]), 1)
                elif fire_ce_fade:
                    trade_type = "CE"
                    mode = "FADE"
                    buf = p["fade_ce_sl_pts"] if p["sl_mode"] == "Fixed Points" else (p["fade_ce_sl_atr"] * atr)
                    sl_val = round(min_low_outside - buf, 1)
                    tg_val = round(c + (atr * p["fade_ce_tg_atr"]), 1)

                if trade_type is not None:
                    active_trade = {
                        "date": d,
                        "entry_time": bar["datetime"],
                        "type": trade_type,
                        "mode": mode,
                        "entry": c,
                        "init_sl": sl_val,
                        "current_sl": sl_val,
                        "target_p": tg_val,
                        "bank_dist": bank_d,
                        "banked": False,
                        "peak": c
                    }

    return pd.DataFrame(trades)

# ==============================================================================
# 3. INTERACTIVE UI & SIDEBAR INPUTS
# ==============================================================================
st.sidebar.title("Backtest Configuration")
exec_mode = st.sidebar.radio("Execution Mode", ["Manual Input Backtest", "Automated Grid Optimizer"])

st.sidebar.subheader("Date Horizon")
col_s1, col_s2 = st.sidebar.columns(2)
start_year = col_s1.selectbox("Start Year", list(range(2015, 2027)), index=6) # 2021
start_month = col_s2.selectbox("Start Month", list(range(1, 13)), index=0)

col_e1, col_e2 = st.sidebar.columns(2)
end_year = col_e1.selectbox("End Year", list(range(2015, 2027)), index=11) # 2026
end_month = col_e2.selectbox("End Month", list(range(1, 13)), index=9)

d_start = datetime.date(start_year, start_month, 1)
d_end = datetime.date(end_year, end_month, 28)

st.sidebar.subheader("Macro Strategy Parameters")
vol_threshold = st.sidebar.number_input("Macro Regime Threshold (Daily 10-ATR)", value=229.0, step=5.0)
enable_fade = st.sidebar.checkbox("Trade Fades when Daily ATR < 229", value=True)
max_breach_mult = st.sidebar.number_input("Max Breach Allowance (x ATR)", value=0.5, step=0.1)

st.sidebar.subheader("Independent Stop Loss Settings")
sl_mode = st.sidebar.selectbox("SL Buffer Mode", ["Fixed Points", "ATR Multiplier"], index=0)
bo_ce_sl_pts = st.sidebar.number_input("BO CE SL: Points below Low", value=5.0, step=1.0)
bo_pe_sl_pts = st.sidebar.number_input("BO PE SL: Points above High", value=105.0, step=5.0)
fade_pe_sl_pts = st.sidebar.number_input("Fade PE SL: Points above Breach High", value=25.0, step=1.0)
fade_ce_sl_pts = st.sidebar.number_input("Fade CE SL: Points below Breach Low", value=5.0, step=1.0)

bo_ce_sl_atr = 0.3
bo_pe_sl_atr = 0.3
fade_pe_sl_atr = 0.3
fade_ce_sl_atr = 0.3

st.sidebar.subheader("Independent Target Settings (ATR Multipliers)")
bo_ce_tg_atr = st.sidebar.number_input("BO CE Target: ATR Multiplier", value=3.5, step=0.1)
bo_pe_tg_atr = st.sidebar.number_input("BO PE Target: ATR Multiplier", value=5.0, step=0.1)
fade_pe_tg_atr = st.sidebar.number_input("Fade PE Target: ATR Multiplier", value=1.9, step=0.1)
fade_ce_tg_atr = st.sidebar.number_input("Fade CE Target: ATR Multiplier", value=1.4, step=0.1)

# ==============================================================================
# 4. EXECUTION & VISUALIZATION
# ==============================================================================
st.title("NIFTY Dual-Regime Master: Multi-Year Institutional Backtester")

with st.spinner("Loading Multi-Year 5m Intraday Data..."):
    df_raw = load_historical_data(d_start, d_end)

if df_raw.empty:
    st.info("Awaiting historical data. Ensure your API keys are in Render Environment Variables or test within available cached bars.")
    st.stop()

df_prepared = prepare_indicators(df_raw)
st.success(f"Loaded {len(df_prepared):,} 5-minute bars across {df_prepared['date'].nunique()} trading sessions.")

default_p = {
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
    "bo_ce_tg_atr": bo_ce_tg_atr,
    "bo_pe_tg_atr": bo_pe_tg_atr,
    "fade_pe_tg_atr": fade_pe_tg_atr,
    "fade_ce_tg_atr": fade_ce_tg_atr,
    "qty": 65
}

if exec_mode == "Manual Input Backtest":
    df_trades = run_backtest_simulation(df_prepared, default_p)

    if df_trades.empty:
        st.warning("No trades triggered under current parameters.")
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
        title="TradingView Cumulative Equity Curve",
        plot_bgcolor="#131722",
        paper_bgcolor="#131722",
        font=dict(color="#d1d4dc"),
        xaxis=dict(gridcolor="#1f2430"),
        yaxis=dict(gridcolor="#1f2430", title="Profit & Loss (₹)")
    )
    st.plotly_chart(fig_equity, use_container_width=True)

    st.subheader("Historical Trade Ledger")
    st.dataframe(df_trades[["entry_time", "type", "mode", "entry", "init_sl", "exit_price", "exit_time", "exit_reason", "pts", "pnl"]], use_container_width=True)

else:
    st.subheader("Automated Multi-Year Parameter Grid Search")
    st.write("Simulating across combinations of SL & Target inputs to extract the mathematical frontier...")

    if st.button("Start Grid Optimization"):
        results = []
        ce_targets = [2.5, 3.0, 3.5, 4.0]
        pe_targets = [3.5, 4.0, 5.0, 6.0]
        pe_sls = [75.0, 90.0, 105.0, 117.0]

        total_sims = len(ce_targets) * len(pe_targets) * len(pe_sls)
        bar = st.progress(0, text="Running Combinations...")
        idx = 0

        for ct in ce_targets:
            for pt in pe_targets:
                for psl in pe_sls:
                    sim_p = default_p.copy()
                    sim_p["bo_ce_tg_atr"] = ct
                    sim_p["bo_pe_tg_atr"] = pt
                    sim_p["bo_pe_sl_pts"] = psl

                    tdf = run_backtest_simulation(df_prepared, sim_p)
                    if not tdf.empty:
                        gp = tdf[tdf["pnl"] > 0]["pnl"].sum()
                        gl = abs(tdf[tdf["pnl"] < 0]["pnl"].sum())
                        pf = gp / max(1.0, gl)
                        net = tdf["pnl"].sum()
                        wr = (len(tdf[tdf["pnl"] > 0]) / len(tdf)) * 100

                        results.append({
                            "CE Target (ATR)": ct,
                            "PE Target (ATR)": pt,
                            "PE SL (Pts)": psl,
                            "Profit Factor": round(pf, 2),
                            "Win Rate (%)": round(wr, 1),
                            "Total Trades": len(tdf),
                            "Net PnL (₹)": int(net)
                        })
                    idx += 1
                    bar.progress(idx / total_sims)

        bar.empty()
        res_df = pd.DataFrame(results).sort_values("Profit Factor", reverse=True).reset_index(drop=True)
        st.subheader("Optimal Parameter Configurations")
        st.dataframe(res_df.head(15), use_container_width=True)
