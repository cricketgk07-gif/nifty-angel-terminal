"""Nifty 50 ORB 1-Lot Production Script - Fully Synchronized
with Updated Entry (body_frac < 0.12), TSL Milestone Tiers, and Direct Local PKL Loading.
"""

from pathlib import Path
import http.server
import json
import math
import os
import joblib
import socketserver
import threading
import time
import pandas as pd
import numpy as np
import pyotp
import requests
import zoneinfo
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

INDEX_TOKEN = "99926000"
LOT_SIZE_QTY = 65
IST = zoneinfo.ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("API_KEY", "")
CLIENT_CODE = os.getenv("CLIENT_CODE", "")
PIN = os.getenv("PIN", "")
TOTP_SECRET = os.getenv("TOTP_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8925956740:AAHHo18Gp-rwlsGlnsyS5mz40RbvIN_tXqw")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6898891302")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
BUDGET_CEILING = float(os.getenv("BUDGET_CEILING", "80.0"))
PORT = int(os.getenv("PORT", 10000))

# ==========================================
# 0. DIRECT LOCAL ML MODEL SETUP (.pkl)
# ==========================================
ml_model = None
ml_cutoff = 0.52
try:
    if os.path.exists('trained_orb_model.pkl') and os.path.exists('model_cutoff.pkl'):
        ml_model = joblib.load('trained_orb_model.pkl')
        ml_cutoff = joblib.load('model_cutoff.pkl')
        print(f'[INIT] Loaded compressed trained ML model and cutoff successfully: {ml_cutoff}')[cite: 2]
    else:
        print('[INIT ERROR] Model files (.pkl) not found in local directory!')
except Exception as e:
    print(f'[INIT ERROR] Failed to load local model files: {e}')[cite: 2]

# ==========================================
# 1. Health Server & Keepalive (Render Fix)
# ==========================================
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"WORKER_RUNNING_OK")[cite: 2]

    def log_message(self, format, *args):
        pass

def run_ping_server():
    with socketserver.TCPServer(("0.0.0.0", PORT), HealthHandler) as httpd:
        httpd.serve_forever()

threading.Thread(target=run_ping_server, daemon=True).start()

def self_ping_keepalive():
    while True:
        time.sleep(480)
        try:
            requests.get(f"http://127.0.0.1:{PORT}/", timeout=5)[cite: 2]
        except Exception:
            pass

threading.Thread(target=self_ping_keepalive, daemon=True).start()

# ==========================================
# 2. Math & Redis Helpers
# ==========================================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0[cite: 2]

def compute_contract_delta(s, k, days_to_exp, is_ce=True, iv=0.14, r=0.07):
    tau = max(days_to_exp / 365.0, 0.001)
    d1 = (math.log(s / k) + (r + 0.5 * iv ** 2) * tau) / (iv * math.sqrt(tau))
    ce_delta = norm_cdf(d1)
    return round(ce_delta if is_ce else (1.0 - ce_delta), 2)[cite: 2]

def is_market_hours():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return datetime.time(9, 15) <= now.time() <= datetime.time(15, 30)[cite: 2]

def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=6)
        return r.status_code == 200[cite: 2]
    except Exception:
        return False

def push_to_redis(key, data_obj):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return
    url = f"{UPSTASH_REDIS_REST_URL}/set/{key}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        requests.post(url, headers=headers, data=json.dumps(data_obj), timeout=3)
    except Exception as e:
        print(f"[Redis Push Error]: {e}")[cite: 2]

def read_from_redis(key="nifty_live_state"):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    url = f"{UPSTASH_REDIS_REST_URL}/get/{key}"
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

def get_dynamic_budget():
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return BUDGET_CEILING
    try:
        url = f"{UPSTASH_REDIS_REST_URL}/get/user_selected_budget"
        headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=2)
        if r.status_code == 200:
            val = r.json().get("result")
            if val:
                return float(val)
    except Exception:
        pass
    return BUDGET_CEILING

# ==========================================
# 3. Strategy & ML Execution Core
# ==========================================
FEATURE_COLUMNS = [
    'side', 'orb_width', 'range', 'loc', 'body_frac', 'upper_wick', 'lower_wick',
    'atr14', 'ema_dist_atr', 'ext_atr', 'range_lag1', 'range_lag2', 'range_lag3',
    'loc_lag1', 'loc_lag2', 'loc_lag3', 'prior3_move_atr', 'dir_close_loc',
    'dir_ema_dist_atr', 'dir_prior1_move_atr', 'dir_prior3_move_atr',
    'prev_avg_range3_atr', 'signal_range_atr', 'conf_loc_dir', 'stay_orb',
    'beyond_buffer', 'directional_close', 'break_signal_extreme',
    'close_loc_ok', 'close_loc_strong', 'close_mid', 'conf_open',
    'conf_high', 'conf_low', 'conf_close',
]

def calculate_indicators(df):
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['prev_close']),
            abs(df['low'] - df['prev_close']),
        ),
    )
    df['atr14'] = df['tr'].rolling(window=14).mean().bfill()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    return df

def extract_entry_candidates(byday):
    candidates = []
    for day_key, group in byday.items():
        if len(group) < 6:
            continue
        if group.iloc[0]['dt'].time() != datetime.time(9, 15):
            continue

        orb_window = group.iloc[0:3]
        orb_high = orb_window['high'].max()
        orb_low = orb_window['low'].min()
        orb_width = orb_high - orb_low

        if orb_width > 160.0 or orb_width < 12:
            continue

        for i in range(3, len(group) - 2):
            row = group.iloc[i]
            t = row['dt'].time()

            # Session hours restored up to 14:30 as per strategy_entry.py
            if t > datetime.time(14, 30) or t < datetime.time(9, 30):
                continue

            side = None
            if row['close'] > orb_high and row['close'] > row['ema20']:
                side = 'CE'
            elif row['close'] < orb_low and row['close'] < row['ema20']:
                side = 'PE'

            if side:
                conf_row = group.iloc[i + 1]
                entry_row = group.iloc[i + 2]

                is_ce_confirmed = (
                    side == 'CE'
                    and conf_row['close'] > orb_high
                    and conf_row['close'] > conf_row['open']
                )
                is_pe_confirmed = (
                    side == 'PE'
                    and conf_row['close'] < orb_low
                    and conf_row['close'] < conf_row['open']
                )

                if is_ce_confirmed or is_pe_confirmed:
                    high_v, low_v, open_v, close_v = (
                        row['high'],
                        row['low'],
                        row['open'],
                        row['close'],
                    )
                    rng = high_v - low_v
                    if rng == 0:
                        continue

                    loc = (close_v - low_v) / rng
                    body_frac = abs(close_v - open_v) / rng

                    # Optimized body fraction (< 0.12) as per strategy_entry.py
                    if body_frac < 0.12:
                        continue

                    candidates.append({
                        'day': day_key,
                        'month_num': row['dt'].strftime('%Y-%m'),
                        'side': side,
                        'signal_time': row['dt'],
                        'entry_time': entry_row['dt'],
                        'entry_price': entry_row['open'],
                        'orb_width': orb_width,
                        'range': rng,
                        'loc': loc,
                        'body_frac': body_frac,
                        'atr14': row['atr14'],
                        'ema_dist_atr': (
                            (close_v - row['ema20']) / row['atr14']
                            if row['atr14'] > 0
                            else 0
                        ),
                        'ext_atr': (
                            abs(close_v - row['ema20']) / row['atr14']
                            if row['atr14'] > 0
                            else 0
                        ),
                    })
                    break
    return candidates

def build_ml_features(cand_df):
    for col in ['range', 'loc']:
        cand_df[f'{col}_lag1'] = cand_df[col].shift(1).fillna(0)
        cand_df[f'{col}_lag2'] = cand_df[col].shift(2).fillna(0)
        cand_df[f'{col}_lag3'] = cand_df[col].shift(3).fillna(0)

    cand_df['prior3_move_atr'] = (
        cand_df['range'].rolling(3).sum().fillna(0) / cand_df['atr14']
    )
    cand_df['prev_avg_range3_atr'] = (
        cand_df['range'].rolling(3).mean().fillna(0) / cand_df['atr14']
    )

    for f_name in FEATURE_COLUMNS:
        if f_name not in cand_df.columns:
            cand_df[f_name] = 0.0

    return cand_df

# ==========================================
# 4. Dedicated Trading Worker Engine
# ==========================================
class DedicatedTradingWorker:
    def __init__(self):
        self.candles = []
        self.active_bar = None
        self.today = datetime.datetime.now(IST).date()
        self.markers = []
        self.history_cards = {}
        self.pivot_lines = {}
        self.fib_lines = {}
        self.orb_levels = {}
        self.contract_preview = {}
        self.prev_day_summary = {}

        self.locked_trade = None
        self.today_trade_record = None
        self.trades_taken_today = 0

        self.auth_token = None
        self.feed_token = None
        self.last_login_date = None
        self.last_polled_opt_ltp = 0.0
        self.last_push_time = 0.0

        self.authenticate_angel_one()

        today_str = self.today.strftime("%Y-%m-%d")
        saved_state = read_from_redis("nifty_live_state") or {}

        if saved_state.get("trade_date") == today_str:
            self.trades_taken_today = saved_state.get("trades_taken_today", 0)
            self.today_trade_record = saved_state.get("today_trade_record")
            if saved_state.get("locked_trade") and not saved_state.get("locked_trade", {}).get("closed", False):
                self.locked_trade = saved_state.get("locked_trade")

        self.nfo_df = self.load_scrip_master()
        self.init_historical_candles()
        self.fetch_daily_pivots_and_levels()
        self.run_full_strategy_pipeline()
        self.update_pretrade_contract_forecast()

        threading.Thread(target=self.background_option_poller, daemon=True).start()
        threading.Thread(target=self.background_index_poller, daemon=True).start()

        if self.candles:
            self.sync_state(self.candles[-1]["close"], force=True)

    def authenticate_angel_one(self):
        try:
            self.api = SmartConnect(API_KEY)
            totp = pyotp.TOTP(TOTP_SECRET).now()
            self.session = self.api.generateSession(CLIENT_CODE, PIN, totp)
            self.auth_token = self.session["data"]["jwtToken"]
            self.feed_token = self.api.getfeedToken()
            self.last_login_date = datetime.datetime.now(IST).date()
            print(f"[Auth Success]: SmartAPI session generated for {self.last_login_date}")[cite: 2]
        except Exception as e:
            print(f"[Auth Error]: Session generation failed: {e}")[cite: 2]

    def load_scrip_master(self):
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        try:
            r = requests.get(url, timeout=15)
            data = r.json()
            nfo_items = [
                x for x in data
                if x.get("exch_seg") == "NFO"
                and x.get("name") == "NIFTY"
                and x.get("instrumenttype") == "OPTIDX"
            ]
            df = pd.DataFrame(nfo_items)
            if not df.empty:
                df["strike_num"] = pd.to_numeric(df["strike"], errors="coerce") / 100.0
                df["exp_dt"] = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce")
            return df
        except Exception:
            return pd.DataFrame()

    def init_historical_candles(self):
        now = datetime.datetime.now(IST)
        for days_back in [15, 7]:
            from_dt = now - datetime.timedelta(days=days_back)
            from_str = from_dt.strftime("%Y-%m-%d 09:15")
            to_str = now.strftime("%Y-%m-%d %H:%M")
            try:
                resp = self.api.getCandleData({
                    "exchange": "NSE",
                    "symboltoken": INDEX_TOKEN,
                    "interval": "FIVE_MINUTE",
                    "fromdate": from_str,
                    "todate": to_str,
                })
                if resp and resp.get("status") and resp.get("data") and len(resp["data"]) > 10:
                    cand_list = []
                    for b in resp["data"]:
                        dt_parsed = pd.to_datetime(b[0])
                        dt_ist = dt_parsed.tz_localize(IST) if dt_parsed.tzinfo is None else dt_parsed.astimezone(IST)
                        t_sec = int(dt_ist.timestamp())
                        cand_list.append({
                            "time": t_sec,
                            "open": float(b[1]),
                            "high": float(b[2]),
                            "low": float(b[3]),
                            "close": float(b[4]),
                            "dt": dt_ist,
                        })
                    df_c = pd.DataFrame(cand_list)
                    df_c = calculate_indicators(df_c)
                    self.candles = df_c.to_dict("records")
                    print(f"[Candles Loaded]: {len(self.candles)} candles starting from 09:15.")[cite: 2]
                    break
            except Exception as e:
                print(f"[Candles Error]: {e}")[cite: 2]

    def fetch_daily_pivots_and_levels(self):
        now = datetime.datetime.now(IST)
        from_str = (now - datetime.timedelta(days=35)).strftime("%Y-%m-%d 09:15")
        to_str = now.strftime("%Y-%m-%d 15:30")
        try:
            resp = self.api.getCandleData({
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "ONE_DAY",
                "fromdate": from_str,
                "todate": to_str,
            })
            if resp and resp.get("status") and resp.get("data") and len(resp["data"]) >= 2:
                df_d = pd.DataFrame(resp["data"], columns=["time", "open", "high", "low", "close", "vol"])
                df_d["high"] = df_d["high"].astype(float)
                df_d["low"] = df_d["low"].astype(float)
                df_d["close"] = df_d["close"].astype(float)

                prev_bar = df_d.iloc[-2]
                pdh = round(float(prev_bar["high"]), 2)
                pdl = round(float(prev_bar["low"]), 2)
                pdc = round(float(prev_bar["close"]), 2)
                P = round((pdh + pdl + pdc) / 3.0, 2)
                rng = round(pdh - pdl, 2)

                self.pivot_lines = {
                    "P": P,
                    "R1": round((2.0 * P) - pdl, 2), "S1": round((2.0 * P) - pdh, 2),
                    "R2": round(P + rng, 2), "S2": round(P - rng, 2),
                    "PDH": pdh,
                    "PDL": pdl,
                }
        except Exception as e:
            print(f"[Daily Pivots Error]: {e}")[cite: 2]

    def find_budget_strike(self, spot_ref, is_ce):
        budget_target = get_dynamic_budget()
        atm = int(round(spot_ref / 50.0) * 50)
        opt_type = "CE" if is_ce else "PE"

        if self.nfo_df.empty:
            return atm, f"NIFTY {atm} {opt_type}", "", 0.50, 4, 75.0

        future_exp = self.nfo_df[self.nfo_df["exp_dt"].dt.date >= self.today].sort_values("exp_dt")
        if future_exp.empty:
            return atm, f"NIFTY {atm} {opt_type}", "", 0.50, 4, 75.0

        curr_exp = future_exp["expiry"].iloc[0]
        exp_slice = future_exp[future_exp["expiry"] == curr_exp]
        days_to_exp = max(1, (exp_slice.iloc[0]["exp_dt"].date() - self.today).days)

        candidates = []
        strike_offsets = range(0, 8) if is_ce else range(0, -8, -1)

        for offset in strike_offsets:
            k = atm + (offset * 50)
            row = exp_slice[(exp_slice["strike_num"] == float(k)) & (exp_slice["symbol"].str.endswith(opt_type))]
            if not row.empty:
                sym = str(row.iloc[0]["symbol"])
                tok = str(row.iloc[0]["token"])
                real_ltp = 0.0
                try:
                    res = self.api.ltpData("NFO", sym, tok)
                    if isinstance(res, dict) and res.get("status") and res.get("data"):
                        real_ltp = float(res["data"].get("ltp", 0.0))
                except Exception:
                    pass

                if real_ltp <= 0.0:
                    otm_dist = (k - spot_ref) if is_ce else (spot_ref - k)
                    real_ltp = max(5.0, 110.0 - (otm_dist * 0.45))

                candidates.append((k, sym, tok, real_ltp))

        valid = [c for c in candidates if c[3] <= budget_target]
        if valid:
            valid.sort(key=lambda x: x[3], reverse=True)
            chosen_strike, chosen_sym, chosen_tok, chosen_ltp = valid[0]
        elif candidates:
            candidates.sort(key=lambda x: x[3])
            chosen_strike, chosen_sym, chosen_tok, chosen_ltp = candidates[0]
        else:
            chosen_strike = atm
            chosen_sym = f"NIFTY {atm} {opt_type}"
            chosen_tok = ""
            chosen_ltp = 75.0

        delta = compute_contract_delta(spot_ref, chosen_strike, days_to_exp, is_ce=is_ce)
        return chosen_strike, chosen_sym, chosen_tok, delta, days_to_exp, chosen_ltp

    def compute_n_targets(self, entry_p, is_ce, primary_t1):
        t_candidates = [primary_t1]
        p = self.pivot_lines or {}

        if is_ce:
            for k in ["R1", "R2", "PDH"]:
                val = float(p.get(k, 0.0))
                if val > entry_p + 15.0:
                    t_candidates.append(val)
            clean_targets = sorted(list(set([round(x, 1) for x in t_candidates if x > entry_p])))
        else:
            for k in ["S1", "S2", "PDL"]:
                val = float(p.get(k, 0.0))
                if val < entry_p - 15.0:
                    t_candidates.append(val)
            clean_targets = sorted(list(set([round(x, 1) for x in t_candidates if x < entry_p])), reverse=True)

        step = 30.0
        while len(clean_targets) < 5:
            last_t = clean_targets[-1] if clean_targets else entry_p
            new_t = round(last_t + step if is_ce else last_t - step, 1)
            clean_targets.append(new_t)

        return clean_targets[:6]

    def update_pretrade_contract_forecast(self):
        if not self.candles:
            return

        ref_spot = float(self.candles[-1]["close"])
        orb_h = self.orb_levels.get("orb_h", ref_spot)
        orb_l = self.orb_levels.get("orb_l", ref_spot)

        k_ce, sym_ce, tok_ce, d_ce, _, ltp_ce = self.find_budget_strike(ref_spot, is_ce=True)
        k_pe, sym_pe, tok_pe, d_pe, _, ltp_pe = self.find_budget_strike(ref_spot, is_ce=False)

        ce_entry = orb_h + 1.0
        ce_sl = orb_l - 40.0
        ce_t1 = ce_entry + 100.0

        pe_entry = orb_l - 1.0
        pe_sl = orb_h + 40.0
        pe_t1 = pe_entry - 100.0

        ce_targets = self.compute_n_targets(ce_entry, is_ce=True, primary_t1=ce_t1)
        pe_targets = self.compute_n_targets(pe_entry, is_ce=False, primary_t1=pe_t1)

        self.contract_preview = {
            "CE": {
                "symbol": sym_ce, "delta": d_ce, "opt_ltp": ltp_ce,
                "exp_entry_spot": round(ce_entry, 1), "exp_sl_spot": round(ce_sl, 1),
                "exp_sl_opt": max(0.5, round(ltp_ce - (abs(ce_entry - ce_sl) * d_ce), 2)),
                "targets": ce_targets,
            },
            "PE": {
                "symbol": sym_pe, "delta": d_pe, "opt_ltp": ltp_pe,
                "exp_entry_spot": round(pe_entry, 1), "exp_sl_spot": round(pe_sl, 1),
                "exp_sl_opt": max(0.5, round(ltp_pe - (abs(pe_sl - pe_entry) * d_pe), 2)),
                "targets": pe_targets,
            },
        }

    def run_full_strategy_pipeline(self):
        if not self.candles:
            return

        df_h = pd.DataFrame(self.candles)
        df_h["date_str"] = df_h["dt"].apply(lambda d: d.astimezone(IST).strftime("%Y-%m-%d"))

        df_h["hlc3"] = (df_h["high"] + df_h["low"] + df_h["close"]) / 3.0
        candle_spread = (df_h["high"] - df_h["low"]) + (df_h["close"] - df_h["open"]).abs()
        df_h["calc_vol"] = candle_spread * 1250.0 + 500.0
        df_h["pv"] = df_h["hlc3"] * df_h["calc_vol"]
        df_h["cum_vol"] = df_h.groupby("date_str")["calc_vol"].cumsum()
        df_h["cum_pv"] = df_h.groupby("date_str")["pv"].cumsum()
        df_h["vwap"] = (df_h["cum_pv"] / df_h["cum_vol"]).round(2)

        unique_days = list(df_h["date_str"].unique())
        today_bars = df_h[df_h["date_str"] == unique_days[-1]].sort_values("time").reset_index(drop=True)

        orb_bars = today_bars.iloc[0:3] if len(today_bars) >= 3 else today_bars
        curr_orb_h = round(float(orb_bars["high"].max()), 2)
        curr_orb_l = round(float(orb_bars["low"].min()), 2)
        orb_range_val = max(15.0, round(curr_orb_h - curr_orb_l, 2))
        self.orb_levels = {"orb_h": curr_orb_h, "orb_l": curr_orb_l, "range": orb_range_val}

        for i in range(len(self.candles)):
            self.candles[i]["vwap"] = float(df_h.iloc[i]["vwap"])

    def background_option_poller(self):
        while True:
            time.sleep(15)
            contract_sym, contract_tok = None, None
            if self.locked_trade and self.locked_trade.get("token"):
                contract_sym = self.locked_trade["symbol"]
                contract_tok = self.locked_trade["token"]
            elif self.today_trade_record and self.today_trade_record.get("token"):
                contract_sym = self.today_trade_record["symbol"]
                contract_tok = self.today_trade_record["token"]

            if is_market_hours() and contract_tok:
                try:
                    res = self.api.ltpData("NFO", contract_sym, contract_tok)
                    if isinstance(res, dict) and res.get("status") and res.get("data"):
                        self.last_polled_opt_ltp = float(res["data"].get("ltp", 0.0))
                except Exception:
                    pass

    def background_index_poller(self):
        while True:
            time.sleep(2.0)
            if is_market_hours():
                try:
                    res = self.api.ltpData("NSE", "Nifty 50", INDEX_TOKEN)
                    if isinstance(res, dict) and res.get("status") and res.get("data"):
                        ltp = float(res["data"].get("ltp", 0.0))
                        if ltp > 0:
                            self.on_tick(ltp)
                except Exception:
                    pass

    def on_tick(self, ltp):
        if not is_market_hours():
            return

        now = datetime.datetime.now(IST)
        bucket_min = (now.minute // 5) * 5
        bar_dt = now.replace(minute=bucket_min, second=0, microsecond=0)
        t_sec = int(bar_dt.timestamp())

        if now.time() >= datetime.time(15, 15) and self.locked_trade and not self.locked_trade.get("closed"):
            self.close_trade(ltp, "MANDATORY 15:15 EOD SQUARE-OFF")
            return

        if self.active_bar is None or self.active_bar["time"] != t_sec:
            if self.active_bar:
                self.candles.append(self.active_bar.copy())
                self.run_full_strategy_pipeline()
                self.evaluate_ml_orb_entry()
                self.update_pretrade_contract_forecast()
                self.sync_state(ltp, force=True)

            self.active_bar = {
                "time": t_sec, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "dt": bar_dt,
            }
        else:
            self.active_bar["high"] = max(self.active_bar["high"], ltp)
            self.active_bar["low"] = min(self.active_bar["low"], ltp)
            self.active_bar["close"] = ltp

        self.evaluate_live_trail_and_exit(ltp)
        self.sync_state(ltp)

    def evaluate_ml_orb_entry(self):
        if not is_market_hours() or self.locked_trade or self.trades_taken_today >= 1 or len(self.candles) < 6 or ml_model is None:
            return

        df = pd.DataFrame(self.candles)
        day_key = self.today.strftime('%Y-%m-%d')
        sub_df = df[df['dt'].dt.strftime('%Y-%m-%d') == day_key].reset_index(drop=True)
        if len(sub_df) < 6:
            return

        sub_df = calculate_indicators(sub_df)
        candidates = extract_entry_candidates({day_key: sub_df})
        if candidates:
            cand_df = pd.DataFrame(candidates)
            cand_df = build_ml_features(cand_df)
            X = cand_df[FEATURE_COLUMNS].copy()
            X['side'] = X['side'].map({'CE': 1, 'PE': -1})
            preds = ml_model.predict(X)
            cand_df['predicted_expected_points'] = preds
            valid = cand_df[cand_df['predicted_expected_points'] > ml_cutoff]

            if not valid.empty:
                sig = valid.iloc[0]
                side = sig['side']
                entry_spot = float(sig['entry_price'])
                strike, sym, tok, delta, days_exp, chosen_ltp = self.find_budget_strike(entry_spot, is_ce=(side == 'CE'))
                opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else chosen_ltp

                init_sl = entry_spot - 40.0 if side == 'CE' else entry_spot + 40.0
                opt_sl = max(5.0, round(opt_ltp - (40.0 * delta), 2))

                self.trades_taken_today += 1
                t_sec = int(pd.Timestamp(sig['signal_time']).timestamp())
                trigger_time_str = datetime.datetime.now(IST).strftime('%H:%M:%S')

                trade_dict = {
                    "type": side, "mode": "ML-ORB", "symbol": sym, "token": tok,
                    "entry": entry_spot, "init_sl": init_sl, "current_sl": init_sl,
                    "peak": entry_spot, "locked_pts": -40.0, "delta": delta,
                    "opt_entry_ltp": opt_ltp, "opt_sl": opt_sl, "closed": False,
                    "time": t_sec, "trigger_time": trigger_time_str,
                }

                self.locked_trade = trade_dict
                self.today_trade_record = trade_dict.copy()

                self.markers.append({
                    "time": t_sec,
                    "position": "belowBar" if side == 'CE' else "aboveBar",
                    "color": "#00e676" if side == 'CE' else "#ff5252",
                    "shape": "arrowUp" if side == 'CE' else "arrowDown",
                    "text": f"BUY {side}",
                })

                self.history_cards[str(t_sec)] = {
                    "title": f"ML-ORB {side}",
                    "time_str": trigger_time_str,
                    "entry": entry_spot, "sl": init_sl,
                    "max_trail_sl": init_sl,
                    "target": entry_spot + 100 if side == 'CE' else entry_spot - 100,
                    "delta": delta, "opt_entry": opt_ltp, "opt_sl": opt_sl,
                    "type": side, "outcome": "ACTIVE RUNNER",
                }

                send_telegram_alert(
                    f"⚡ <b>ML-ORB STRATEGY: BUY {side}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 <b>CONTRACT:</b> {sym} (Qty: {LOT_SIZE_QTY}, Δ: {delta:.2f})\n"
                    f"• <b>ENTRY (SPOT):</b> {entry_spot:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                    f"• <b>INITIAL SL:</b> {init_sl:.1f} | <b>₹{opt_sl:.2f}</b>\n"
                    f"⏰ <b>Trigger Time:</b> {trigger_time_str}"
                )

    def evaluate_live_trail_and_exit(self, live_spot):
        if not is_market_hours() or not self.locked_trade or self.locked_trade.get("closed"):
            return

        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        entry_p = tr["entry"]

        if is_ce:
            tr['peak'] = max(tr.get('peak', entry_p), live_spot)
            max_fav = tr['peak'] - entry_p
        else:
            tr['peak'] = min(tr.get('peak', entry_p), live_spot)
            max_fav = entry_p - tr['peak']

        # TSL Milestone Tiers Logic synchronized from strategy_tsl.py
        if max_fav >= 200.0:
            excess_blocks = int((max_fav - 200.0) // 25.0)
            milestone = 200.0 + (excess_blocks * 25.0)
            tier_profit = milestone - 15.0
        elif max_fav >= 175.0:
            tier_profit = 150.0
        elif max_fav >= 150.0:
            tier_profit = 125.0
        elif max_fav >= 125.0:
            tier_profit = 90.0
        elif max_fav >= 100.0:
            tier_profit = 65.0
        elif max_fav >= 75.0:
            tier_profit = 30.0
        elif max_fav >= 50.0:
            tier_profit = 5.0
        else:
            tier_profit = None

        if tier_profit is not None:
            tr['locked_pts'] = max(tr.get('locked_pts', -40.0), tier_profit)
            tr['current_sl'] = (entry_p + tr['locked_pts']) if is_ce else (entry_p - tr['locked_pts'])

        if is_ce and live_spot <= tr['current_sl']:
            self.close_trade(tr['current_sl'], "TRAILING STOP LOSS HIT" if tr['locked_pts'] > 0 else "INITIAL SL HIT")
        elif not is_ce and live_spot >= tr['current_sl']:
            self.close_trade(tr['current_sl'], "TRAILING STOP LOSS HIT" if tr['locked_pts'] > 0 else "INITIAL SL HIT")

    def close_trade(self, exit_spot, reason):
        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        entry_p = tr["entry"]
        delta_v = float(tr.get("delta", 0.50))
        final_pts = float(tr.get("locked_pts", -40.0))
        final_inr = int(round(final_pts * delta_v * LOT_SIZE_QTY))
        sign = "+" if final_pts >= 0 else ""
        outcome_str = "PROFIT" if final_pts > 0 else "LOSS"

        t_now = datetime.datetime.now(IST).strftime("%H:%M:%S")
        tr["closed"] = True
        tr["exit_spot"] = exit_spot
        tr["exit_reason"] = reason
        tr["exit_time"] = t_now
        tr["final_pts"] = final_pts
        tr["final_inr"] = final_inr
        tr["outcome"] = outcome_str

        now_ts = int(time.time())
        self.markers.append({
            "time": now_ts,
            "position": "aboveBar" if is_ce else "belowBar",
            "color": "#ef5350" if is_ce else "#00e676",
            "shape": "arrowDown" if is_ce else "arrowUp",
            "text": f"EXIT {tr['type']}",
        })

        self.today_trade_record = tr.copy()
        self.locked_trade = None

        send_telegram_alert(
            f"🏁 <b>TRADE CLOSED ({reason})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚪 <b>Exit Price (Spot):</b> {exit_spot:.1f}\n"
            f"💰 <b>Net PnL:</b> {sign}{final_pts:.1f} pts (₹{sign}{final_inr})\n"
            f"⏰ <b>Exit Time:</b> {t_now}"
        )

    def sync_state(self, live_ltp, force=False):
        now_ts = time.time()
        if not force and (now_ts - self.last_push_time < 1.0):
            return
        self.last_push_time = now_ts

        tr_source = self.locked_trade or self.today_trade_record
        opt_ltp = 0.0
        hud_payload = {}

        if tr_source:
            is_ce = (tr_source["type"] == "CE")
            delta_val = float(tr_source.get("delta", 0.50))
            entry_p = float(tr_source["entry"])
            opt_entry = float(tr_source.get("opt_entry_ltp", 75.0))
            cur_trail = float(tr_source.get("current_sl", tr_source["init_sl"]))

            if self.locked_trade:
                pts_gain = (live_ltp - entry_p) if is_ce else (entry_p - live_ltp)
                opt_ltp = max(0.5, round(opt_entry + (pts_gain * delta_val), 2))
                trail_pts = float(tr_source.get("locked_pts", -40.0))
                trail_opt = max(0.5, round(opt_entry + (trail_pts * delta_val), 2))
                is_closed = False
            else:
                opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else opt_entry
                trail_pts = float(tr_source.get("final_pts", 0.0))
                trail_opt = float(tr_source.get("opt_sl", 0.0))
                is_closed = True

            hud_payload = {
                "type": tr_source["type"], "mode": tr_source.get("mode"),
                "symbol": tr_source["symbol"], "entry_spot": entry_p, "entry_opt": opt_entry,
                "sl_spot": float(tr_source["init_sl"]), "sl_opt": float(tr_source.get("opt_sl", 0.0)),
                "trail_spot": cur_trail, "trail_opt": trail_opt, "delta": delta_val,
                "secured_pts": trail_pts, "trade_closed": is_closed,
                "exit_spot": tr_source.get("exit_spot"), "exit_reason": tr_source.get("exit_reason"),
                "exit_time": tr_source.get("exit_time"), "final_pts": tr_source.get("final_pts", 0.0),
                "final_inr": tr_source.get("final_inr", 0),
            }

        raw_list = list(self.candles)
        if self.active_bar:
            raw_list.append(self.active_bar)

        clean_map = {}
        for c in raw_list:
            t = int(c.get("time", 0))
            if t > 0:
                clean_map[t] = {
                    "time": t, "open": float(c["open"]), "high": float(c["high"]),
                    "low": float(c["low"]), "close": float(c["close"]),
                    "vwap": float(c.get("vwap", c["close"])),
                }

        recent_candles = [clean_map[t] for t in sorted(clean_map.keys())][-450:]

        payload = {
            "ltp": float(live_ltp), "opt_ltp": float(opt_ltp),
            "candles": recent_candles, "locked_trade": self.locked_trade,
            "today_trade_record": self.today_trade_record,
            "trades_taken_today": self.trades_taken_today,
            "trade_date": self.today.strftime("%Y-%m-%d"),
            "pivots": self.pivot_lines, "orb_levels": self.orb_levels,
            "fib_lines": self.fib_lines, "markers": self.markers,
            "history_cards": self.history_cards, "contract_preview": self.contract_preview,
            "prev_day_summary": self.prev_day_summary, "hud": hud_payload,
            "updated_at": time.time(),
        }
        push_to_redis("nifty_live_state", payload)

def run_worker():
    while True:
        try:
            print("[Supervisor]: Initializing SmartAPI Session...")[cite: 2]
            engine = DedicatedTradingWorker()

            def schedule_checker():
                while True:
                    time.sleep(30)
                    now = datetime.datetime.now(IST)
                    if engine.last_login_date != now.date() and now.time() >= datetime.time(8, 30):
                        engine.today = now.date()
                        engine.authenticate_angel_one()
                        engine.locked_trade = None
                        engine.today_trade_record = None
                        engine.trades_taken_today = 0
                        engine.nfo_df = engine.load_scrip_master()
                        engine.init_historical_candles()
                        engine.fetch_daily_pivots_and_levels()
                        engine.run_full_strategy_pipeline()
                        try:
                            sws.close_connection()
                        except Exception:
                            pass
                        break

            threading.Thread(target=schedule_checker, daemon=True).start()

            sws = SmartWebSocketV2(engine.auth_token, API_KEY, CLIENT_CODE, engine.feed_token)

            def on_data(wsapp, msg):
                if isinstance(msg, dict) and "last_traded_price" in msg:
                    ltp = float(msg["last_traded_price"]) / 100.0
                    engine.on_tick(ltp)

            def on_open(wsapp):
                print("[WebSocket]: Connected & Subscribed to Nifty Index Token 99926000")[cite: 2]
                sws.subscribe("correl_id_1", 1, [{"exchangeType": 1, "tokens": [INDEX_TOKEN]}])

            def on_error(wsapp, error):
                print(f"[WebSocket Error]: {error}")[cite: 2]

            def on_close(wsapp):
                print("[WebSocket Closed]: Connection dropped.")[cite: 2]

            sws.on_open = on_open
            sws.on_data = on_data
            sws.on_error = on_error
            sws.on_close = on_close
            sws.connect()

        except Exception as e:
            print(f"[Supervisor Error]: {e}. Reconnecting in 10s...")[cite: 2]
            time.sleep(10)

if __name__ == "__main__":
    run_worker()
