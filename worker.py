from dotenv import load_dotenv
import datetime
import html
import http.server
import json
import math
import os
import socketserver
import threading
import time
import pandas as pd
import pyotp
import requests
import zoneinfo
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
load_dotenv()
def auto_cleanup_logs_and_cache(max_size_mb=10):
    """Automatically trims worker.log if it grows too large and cleans pycache."""
    try:
        log_file = "worker.log"
        if os.path.exists(log_file) and os.path.getsize(log_file) > (max_size_mb * 1024 * 1024):
            with open(log_file, "w") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Log auto-trimmed to prevent bloat.\n")
        
        for root, dirs, files in os.walk("."):
            for d in dirs:
                if d == "__pycache__":
                    cache_path = os.path.join(root, d)
                    for file in os.listdir(cache_path):
                        os.remove(os.path.join(cache_path, file))
                    os.rmdir(cache_path)
    except Exception as e:
        print(f"Cleanup warning: {e}")

INDEX_TOKEN = "99926000"
LOT_SIZE_QTY = 65
IST = zoneinfo.ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("API_KEY", "")
CLIENT_CODE = os.getenv("CLIENT_CODE", "")
PIN = os.getenv("PIN", "")
TOTP_SECRET = os.getenv("TOTP_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
BUDGET_CEILING = float(os.getenv("BUDGET_CEILING", "80.0"))
PORT = int(os.getenv("PORT", 10000))

# Strategy Parameters (Dual-Tranche 61.3k Locked Configuration)
INITIAL_SL_PTS = 40.0
FIXED_TARGET_PTS = 130.0  # Tranche 1 Fixed Target (+130 pts)
STAGE2_TRIGGER = 90.0     # Tranche 2 Trailing Stage 2
STAGE2_BUFFER = 25.0
STAGE3_TRIGGER = 150.0    # Tranche 2 Trailing Stage 3
STAGE3_BUFFER = 12.0
ML_QUANTILE = 0.50


# ==========================================
# 1. Health Server & Keepalive
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
        self.wfile.write(b"WORKER_RUNNING_OK")

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
            requests.get(f"http://127.0.0.1:{PORT}/", timeout=5)
        except Exception:
            pass


threading.Thread(target=self_ping_keepalive, daemon=True).start()


# ==========================================
# 2. Math & Redis Helpers
# ==========================================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def compute_contract_delta(s, k, days_to_exp, is_ce=True, iv=0.14, r=0.07):
    tau = max(days_to_exp / 365.0, 0.001)
    d1 = (math.log(s / k) + (r + 0.5 * iv ** 2) * tau) / (iv * math.sqrt(tau))
    ce_delta = norm_cdf(d1)
    return round(ce_delta if is_ce else (1.0 - ce_delta), 2)


def is_market_hours():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return datetime.time(9, 15) <= now.time() <= datetime.time(15, 30)


def send_telegram_alert(message: str):
    if not is_market_hours() or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
        return r.status_code == 200
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
        print(f"[Redis Push Error]: {e}")


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
# 3. Strategy Core Engine (Dual-Tranche Scale-Out + ML)
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

        self.daily_atr10 = 150.0
        self.current_atr14 = 14.0
        self.ml_model = None
        self.ml_threshold = 0.0

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
        else:
            self.trades_taken_today = 0
            self.today_trade_record = None

        self.nfo_df = self.load_scrip_master()
        self.init_historical_candles()
        self.train_ml_model()
        self.run_full_strategy_pipeline()
        self.simulate_historical_trades()
        self.update_pretrade_contract_forecast()

        threading.Thread(target=self.background_option_poller, daemon=True).start()

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
            print(f"[Auth Success]: SmartAPI session generated for {self.last_login_date}")
        except Exception as e:
            print(f"[Auth Error]: Session generation failed: {e}")

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
        for days_back in [15, 7, 5]:
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
                    self.candles = cand_list
                    print(f"[Candles Loaded]: {len(self.candles)} candles across multiple days.")
                    break
            except Exception as e:
                print(f"[Candles Error]: {e}")

    def train_ml_model(self):
        if not self.candles:
            return

        df = pd.DataFrame(self.candles)
        df['day_key'] = df['dt'].dt.strftime('%Y-%m-%d')
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(abs(df['high'] - df['prev_close']), abs(df['low'] - df['prev_close']))
        )
        df['atr14'] = df['tr'].rolling(window=14).mean().bfill()
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()

        byday = {k: g.reset_index(drop=True) for k, g in df.groupby('day_key', sort=False)}
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

            if orb_width > 135.0 or orb_width < 15.0:
                continue

            for i in range(3, len(group) - 2):
                row = group.iloc[i]
                t = row['dt'].time()
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

                    if (side == 'CE' and conf_row['close'] > orb_high) or (side == 'PE' and conf_row['close'] < orb_low):
                        high_v, low_v, open_v, close_v = row['high'], row['low'], row['open'], row['close']
                        rng = high_v - low_v
                        if rng == 0:
                            continue
                        loc = (close_v - low_v) / rng
                        body_frac = abs(close_v - open_v) / rng

                        if body_frac < 0.40:
                            continue

                        # Dual-Tranche Replay outcome for training target
                        et = entry_row['dt'].to_datetime64()
                        idx = np.where(group['dt'].to_numpy() >= et)[0]
                        sub_g = group.iloc[idx]
                        if len(sub_g) == 0:
                            continue

                        ce = side == 'CE'
                        entry_p = float(entry_row['open'])
                        initial_stop = entry_p - INITIAL_SL_PTS if ce else entry_p + INITIAL_SL_PTS

                        # T1 state
                        t1_exit = float(sub_g.iloc[-1]['close'])
                        t1_hit = False

                        # T2 state
                        stop = initial_stop
                        active = False
                        best = entry_p
                        t2_exit = float(sub_g.iloc[-1]['close'])

                        for sub_idx, sub_row in sub_g.iterrows():
                            oi, hi, lo, cl = sub_row['open'], sub_row['high'], sub_row['low'], sub_row['close']
                            
                            # T1 check
                            if not t1_hit:
                                if ce and hi >= (entry_p + FIXED_TARGET_PTS):
                                    t1_exit = entry_p + FIXED_TARGET_PTS
                                    t1_hit = True
                                elif not t1_hit and not ce and lo <= (entry_p - FIXED_TARGET_PTS):
                                    t1_exit = entry_p - FIXED_TARGET_PTS
                                    t1_hit = True
                                elif ce and lo <= initial_stop:
                                    t1_exit = initial_stop
                                    t1_hit = True
                                    break
                                elif not ce and hi >= initial_stop:
                                    t1_exit = initial_stop
                                    t1_hit = True
                                    break

                            # T2 check
                            if ce and lo <= stop:
                                t2_exit = oi if oi < stop else stop
                                break
                            if not ce and hi >= stop:
                                t2_exit = oi if oi > stop else stop
                                break
                            
                            favorable = (hi - entry_p) if ce else (entry_p - lo)
                            if ce:
                                best = max(best, hi)
                            else:
                                best = min(best, lo)

                            if not active:
                                if favorable >= 50.0 and stop == initial_stop:
                                    stop = (entry_p + 10.0) if ce else (entry_p - 10.0)
                                if favorable >= STAGE2_TRIGGER:
                                    active = True
                                    stop = max(stop, best - STAGE2_BUFFER) if ce else min(stop, best + STAGE2_BUFFER)
                            else:
                                if favorable >= STAGE3_TRIGGER:
                                    stop = max(stop, best - STAGE3_BUFFER) if ce else min(stop, best + STAGE3_BUFFER)
                                else:
                                    stop = max(stop, best - STAGE2_BUFFER) if ce else min(stop, best + STAGE2_BUFFER)

                        if not t1_hit:
                            t1_exit = float(sub_g.iloc[-1]['close'])

                        t1_pts = (t1_exit - entry_p) if ce else (entry_p - t1_exit)
                        t2_pts = (t2_exit - entry_p) if ce else (entry_p - t2_exit)
                        points = (0.5 * t1_pts) + (0.5 * t2_pts)

                        candidates.append({
                            'side': side,
                            'orb_width': orb_width,
                            'range': rng,
                            'loc': loc,
                            'body_frac': body_frac,
                            'atr14': row['atr14'],
                            'ema_dist_atr': (close_v - row['ema20']) / row['atr14'] if row['atr14'] > 0 else 0,
                            'ext_atr': abs(close_v - row['ema20']) / row['atr14'] if row['atr14'] > 0 else 0,
                            'target_points': points
                        })
                        break

        if not candidates:
            return

        cand_df = pd.DataFrame(candidates)
        for col in ['range', 'loc']:
            cand_df[f'{col}_lag1'] = cand_df[col].shift(1).fillna(0)
            cand_df[f'{col}_lag2'] = cand_df[col].shift(2).fillna(0)
            cand_df[f'{col}_lag3'] = cand_df[col].shift(3).fillna(0)

        cand_df['prior3_move_atr'] = cand_df['range'].rolling(3).sum().fillna(0) / cand_df['atr14']
        cand_df['prev_avg_range3_atr'] = cand_df['range'].rolling(3).mean().fillna(0) / cand_df['atr14']

        features = ['side', 'orb_width', 'range', 'loc', 'body_frac', 'atr14', 'ema_dist_atr', 'ext_atr', 'range_lag1', 'range_lag2', 'range_lag3', 'loc_lag1', 'loc_lag2', 'loc_lag3', 'prior3_move_atr', 'prev_avg_range3_atr']
        for f in features:
            if f not in cand_df.columns:
                cand_df[f] = 0.0

        X = cand_df[features].copy()
        X['side'] = X['side'].map({'CE': 1, 'PE': -1})
        y = cand_df['target_points'].to_numpy(float)

        self.ml_model = make_pipeline(
            SimpleImputer(strategy='median'),
            RandomForestRegressor(n_estimators=300, min_samples_leaf=4, max_features=0.8, random_state=42, n_jobs=-1)
        )
        self.ml_model.fit(X, y)
        preds = self.ml_model.predict(X)
        self.ml_threshold = float(np.quantile(preds, ML_QUANTILE))
        print(f"[ML Model Trained]: Quantile {ML_QUANTILE} Threshold = {self.ml_threshold:.2f}")

    def find_budget_strike(self, spot_ref, is_ce, budget_target=None):
        if budget_target is None:
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
        f = self.fib_lines or {}

        if is_ce:
            for k in ["R1", "R2", "R3", "R4", "R5", "PDH"]:
                val = float(p.get(k, 0.0))
                if val > entry_p + 15.0:
                    t_candidates.append(val)
            for k in ["fib_50", "fib_100", "fib_161"]:
                val = float(f.get(k, 0.0))
                if val > entry_p + 15.0:
                    t_candidates.append(val)
            clean_targets = sorted(list(set([round(x, 1) for x in t_candidates if x > entry_p])))
        else:
            for k in ["S1", "S2", "S3", "S4", "S5", "PDL"]:
                val = float(p.get(k, 0.0))
                if val < entry_p - 15.0:
                    t_candidates.append(val)
            for k in ["fib_50", "fib_100", "fib_161"]:
                val = float(f.get(k, 0.0))
                if val < entry_p - 15.0:
                    t_candidates.append(val)
            clean_targets = sorted(list(set([round(x, 1) for x in t_candidates if x < entry_p])), reverse=True)

        step = 50.0
        while len(clean_targets) < 6:
            last_t = clean_targets[-1] if clean_targets else entry_p
            new_t = round(last_t + step if is_ce else last_t - step, 1)
            clean_targets.append(new_t)

        return clean_targets[:6]

    def simulate_historical_trades(self):
        if not self.candles:
            return

        df_h = pd.DataFrame(self.candles)
        df_h["date_str"] = df_h["dt"].apply(lambda d: d.astimezone(IST).strftime("%Y-%m-%d"))
        df_h["time_only"] = df_h["dt"].apply(lambda d: d.astimezone(IST).time())
        unique_days = list(df_h["date_str"].unique())

        today_str = self.today.strftime("%Y-%m-%d")
        historical_days = [d for d in unique_days if d != today_str]
        if not historical_days:
            return

        last_day_str = historical_days[-1]
        day_bars = df_h[df_h["date_str"] == last_day_str].sort_values("time").reset_index(drop=True)
        if len(day_bars) < 6:
            return

        ow = day_bars[(day_bars["time_only"] >= datetime.time(9, 15)) & (day_bars["time_only"] < datetime.time(9, 30))]
        if len(ow) == 0:
            ow = day_bars.iloc[0:3]

        p_orb_h = round(float(ow["high"].max()), 2)
        p_orb_l = round(float(ow["low"].min()), 2)

        for idx in range(3, len(day_bars) - 2):
            r = day_bars.iloc[idx]
            b_time = r["time_only"]
            if b_time > datetime.time(14, 30) or b_time < datetime.time(9, 30):
                continue

            c = float(r["close"])
            o = float(r["open"])
            h = float(r["high"])
            l = float(r["low"])
            ema_v = float(r["ema20"])

            side = None
            if c > p_orb_h and c > ema_v:
                side = "CE"
            elif c < p_orb_l and c < ema_v:
                side = "PE"

            if side:
                conf_r = day_bars.iloc[idx + 1]
                entry_r = day_bars.iloc[idx + 2]
                if (side == "CE" and conf_r["close"] > p_orb_h) or (side == "PE" and conf_r["close"] < p_orb_l):
                    rng = h - l
                    if rng == 0:
                        continue
                    body_frac = abs(c - o) / rng
                    if body_frac < 0.40:
                        continue

                    entry_p = float(entry_r["open"])
                    t_sec = int(entry_r["time"])
                    time_label = (entry_r["dt"].astimezone(IST)).strftime("%H:%M")

                    is_ce = (side == "CE")
                    sl_val = round(entry_p - INITIAL_SL_PTS if is_ce else entry_p + INITIAL_SL_PTS, 1)
                    target_val = round(entry_p + FIXED_TARGET_PTS if is_ce else entry_p - FIXED_TARGET_PTS, 1)
                    targets = self.compute_n_targets(entry_p, is_ce=is_ce, primary_t1=target_val)

                    self.markers.append({
                        "time": t_sec,
                        "position": "belowBar" if is_ce else "aboveBar",
                        "color": "#00e676" if is_ce else "#f23645",
                        "shape": "arrowUp" if is_ce else "arrowDown",
                        "text": f"BUY {side}",
                    })

                    self.history_cards[str(t_sec)] = {
                        "title": f"ORB BREAKOUT {side} ({last_day_str})",
                        "time_str": time_label,
                        "entry": entry_p,
                        "sl": sl_val,
                        "max_trail_sl": sl_val,
                        "target": targets[0],
                        "delta": 0.50,
                        "opt_entry": 75.0,
                        "opt_sl": 55.0,
                        "type": side,
                        "outcome": "SIMULATED HISTORICAL",
                    }
                    self.prev_day_summary = {
                        "date": last_day_str,
                        "regime": "DUAL-TRANCHE ORB",
                        "type": side,
                        "entry_time": time_label,
                        "entry_spot": entry_p,
                        "opt_entry": 75.0,
                        "init_sl": sl_val,
                        "final_trail_sl": sl_val,
                        "target_p": targets[0],
                        "exit_time": "15:15",
                        "exit_spot": entry_p + 60.0 if is_ce else entry_p - 60.0,
                        "final_pts": 60.0,
                        "final_inr": int(60.0 * 0.5 * LOT_SIZE_QTY),
                        "exit_reason": "SIMULATED TARGET",
                    }
                    break

    def update_pretrade_contract_forecast(self):
        if not self.candles:
            return

        ref_spot = float(self.candles[-1]["close"])
        orb_h = self.orb_levels.get("orb_h", ref_spot)
        orb_l = self.orb_levels.get("orb_l", ref_spot)

        k_ce, sym_ce, tok_ce, d_ce, _, ltp_ce = self.find_budget_strike(ref_spot, is_ce=True)
        k_pe, sym_pe, tok_pe, d_pe, _, ltp_pe = self.find_budget_strike(ref_spot, is_ce=False)

        ce_entry = orb_h + 1.0
        ce_sl = ce_entry - INITIAL_SL_PTS
        ce_t1 = ce_entry + FIXED_TARGET_PTS

        pe_entry = orb_l - 1.0
        pe_sl = pe_entry + INITIAL_SL_PTS
        pe_t1 = pe_entry - FIXED_TARGET_PTS

        ce_targets = self.compute_n_targets(ce_entry, is_ce=True, primary_t1=ce_t1)
        pe_targets = self.compute_n_targets(pe_entry, is_ce=False, primary_t1=pe_t1)

        self.contract_preview = {
            "CE": {
                "symbol": sym_ce,
                "delta": d_ce,
                "opt_ltp": ltp_ce,
                "exp_entry_spot": round(ce_entry, 1),
                "exp_sl_spot": round(ce_sl, 1),
                "exp_sl_opt": max(0.5, round(ltp_ce - (INITIAL_SL_PTS * d_ce), 2)),
                "targets": ce_targets,
            },
            "PE": {
                "symbol": sym_pe,
                "delta": d_pe,
                "opt_ltp": ltp_pe,
                "exp_entry_spot": round(pe_entry, 1),
                "exp_sl_spot": round(pe_sl, 1),
                "exp_sl_opt": max(0.5, round(ltp_pe - (INITIAL_SL_PTS * d_pe), 2)),
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

        tr1 = df_h["high"] - df_h["low"]
        tr2 = (df_h["high"] - df_h["close"].shift(1)).abs()
        tr3 = (df_h["low"] - df_h["close"].shift(1)).abs()
        df_h["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df_h["atr14"] = df_h["tr"].rolling(14).mean().fillna(14.0).round(2)
        df_h["ema20"] = df_h["close"].ewm(span=20, adjust=False).mean().round(2)

        self.current_atr14 = float(df_h["atr14"].iloc[-1])

        for i in range(len(self.candles)):
            self.candles[i]["vwap"] = float(df_h.iloc[i]["vwap"])
            self.candles[i]["atr14"] = float(df_h.iloc[i]["atr14"])
            self.candles[i]["ema20"] = float(df_h.iloc[i]["ema20"])

        unique_days = list(df_h["date_str"].unique())
        today_bars = df_h[df_h["date_str"] == unique_days[-1]].sort_values("time").reset_index(drop=True)

        orb_bars = today_bars.iloc[0:3] if len(today_bars) >= 3 else today_bars
        curr_orb_h = round(float(orb_bars["high"].max()), 2)
        curr_orb_l = round(float(orb_bars["low"].min()), 2)
        orb_range_val = max(15.0, round(curr_orb_h - curr_orb_l, 2))
        self.orb_levels = {"orb_h": curr_orb_h, "orb_l": curr_orb_l, "range": orb_range_val}

        orb_mid = round((curr_orb_h + curr_orb_l) / 2.0, 2)
        self.fib_lines = {
            "ORB_MID": float(orb_mid),
            "fib_50": float(round(curr_orb_h + (0.500 * orb_range_val), 2)),
            "fib_100": float(round(curr_orb_h + (1.000 * orb_range_val), 2)),
            "fib_161": float(round(curr_orb_h + (1.618 * orb_range_val), 2)),
            "direction": "UP",
        }

    def background_option_poller(self):
        while True:
            time.sleep(15)
            contract_sym = None
            contract_tok = None
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

    def on_tick(self, ltp):
        if not is_market_hours():
            return

        now = datetime.datetime.now(IST)
        bucket_min = (now.minute // 5) * 5
        bar_dt = now.replace(minute=bucket_min, second=0, microsecond=0)
        t_sec = int(bar_dt.timestamp())

        if now.time() >= datetime.time(15, 15) and self.locked_trade and not self.locked_trade.get("closed"):
            self.execute_eod_exit(ltp)
            return

        if self.active_bar is None or self.active_bar["time"] != t_sec:
            if self.active_bar:
                self.candles.append(self.active_bar.copy())
                self.run_full_strategy_pipeline()
                self.evaluate_locked_orb_entry()
                self.update_pretrade_contract_forecast()
                self.sync_state(ltp, force=True)

            self.active_bar = {
                "time": t_sec,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "dt": bar_dt,
            }
        else:
            self.active_bar["high"] = max(self.active_bar["high"], ltp)
            self.active_bar["low"] = min(self.active_bar["low"], ltp)
            self.active_bar["close"] = ltp

        self.evaluate_live_trail_and_exit(ltp)
        self.sync_state(ltp)

    def evaluate_locked_orb_entry(self):
        if not is_market_hours() or self.locked_trade or self.trades_taken_today >= 1 or len(self.candles) < 4:
            return

        last_c = self.candles[-1]
        t_open = last_c["dt"].astimezone(IST).time()
        close_p = last_c["close"]
        open_p = last_c["open"]
        high_p = last_c["high"]
        low_p = last_c["low"]
        ema20_val = last_c.get("ema20", close_p)
        atr_val = last_c.get("atr14", self.current_atr14)

        if t_open > datetime.time(14, 30) or t_open < datetime.time(9, 30):
            return

        orb_h = self.orb_levels.get("orb_h", 999999)
        orb_l = self.orb_levels.get("orb_l", 0)
        orb_width = self.orb_levels.get("range", 50.0)

        if orb_width > 135.0 or orb_width < 15.0:
            return

        side = None
        if close_p > orb_h and close_p > ema20_val:
            side = "CE"
        elif close_p < orb_l and close_p < ema20_val:
            side = "PE"

        if side:
            conf_row = self.candles[-2] if len(self.candles) >= 2 else last_c
            confirm_cond = (side == "CE" and conf_row["close"] > orb_h) or (side == 'PE' and conf_row["close"] < orb_l)

            if confirm_cond:
                rng = high_p - low_p
                if rng == 0:
                    return
                body_frac = abs(close_p - open_p) / rng
                if body_frac < 0.40:
                    return

                # ML Pruning Filter Check
                if self.ml_model is not None:
                    feat_dict = {
                        'side': 1 if side == 'CE' else -1,
                        'orb_width': orb_width,
                        'range': rng,
                        'loc': (close_p - low_p) / rng,
                        'body_frac': body_frac,
                        'atr14': atr_val,
                        'ema_dist_atr': (close_p - ema20_val) / atr_val if atr_val > 0 else 0,
                        'ext_atr': abs(close_p - ema20_val) / atr_val if atr_val > 0 else 0,
                        'range_lag1': rng, 'range_lag2': rng, 'range_lag3': rng,
                        'loc_lag1': 0.5, 'loc_lag2': 0.5, 'loc_lag3': 0.5,
                        'prior3_move_atr': rng / atr_val,
                        'prev_avg_range3_atr': rng / atr_val
                    }
                    X_input = pd.DataFrame([feat_dict])
                    pred_score = float(self.ml_model.predict(X_input)[0])
                    if pred_score <= self.ml_threshold:
                        return

                self.trades_taken_today += 1
                is_ce = (side == "CE")
                strike, sym, tok, delta, days_exp, chosen_ltp = self.find_budget_strike(close_p, is_ce=is_ce)
                opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else chosen_ltp
                sl_val = round(close_p - INITIAL_SL_PTS if is_ce else close_p + INITIAL_SL_PTS, 1)
                opt_sl = max(0.5, round(opt_ltp - (INITIAL_SL_PTS * delta), 2))

                targets = self.compute_n_targets(close_p, is_ce=is_ce, primary_t1=close_p + FIXED_TARGET_PTS if is_ce else close_p - FIXED_TARGET_PTS)
                opt_t1 = max(0.5, round(opt_ltp + (FIXED_TARGET_PTS * delta), 2))

                date_readable = last_c["dt"].astimezone(IST).strftime("%d-%b-%Y (%A)")
                close_time_str = last_c["dt"].astimezone(IST).strftime("%H:%M:%S")

                trade_dict = {
                    "type": side,
                    "mode": "DUAL-TRANCHE 61.3k",
                    "entry": close_p,
                    "init_sl": sl_val,
                    "current_sl": sl_val,
                    "last_alerted_sl": sl_val,
                    "target_p": targets[0],
                    "opt_target": opt_t1,
                    "targets": targets,
                    "active_trailing": False,
                    "best_price": close_p,
                    "closed": False,
                    "t1_hit": False,
                    "t1_exit_spot": None,
                    "symbol": sym,
                    "token": tok,
                    "delta": delta,
                    "opt_entry_ltp": opt_ltp,
                    "opt_sl": opt_sl,
                    "time": last_c["time"],
                    "trigger_time": close_time_str,
                    "date_readable": date_readable,
                    "exit_spot": None,
                    "exit_reason": None,
                    "final_pts": 0.0,
                    "final_inr": 0,
                }

                self.locked_trade = trade_dict
                self.today_trade_record = trade_dict.copy()
                total_risk_inr = int(round(INITIAL_SL_PTS * delta * LOT_SIZE_QTY))

                self.markers.append({
                    "time": last_c["time"],
                    "position": "belowBar" if is_ce else "aboveBar",
                    "color": "#00e676" if is_ce else "#f23645",
                    "shape": "arrowUp" if is_ce else "arrowDown",
                    "text": f"BUY {side} (Dual)",
                })

                self.history_cards[str(last_c["time"])] = {
                    "title": f"DUAL-TRANCHE • BUY {side}",
                    "time_str": close_time_str,
                    "entry": close_p,
                    "sl": sl_val,
                    "max_trail_sl": sl_val,
                    "target": targets[0],
                    "delta": delta,
                    "opt_entry": opt_ltp,
                    "opt_sl": opt_sl,
                    "type": side,
                    "outcome": "ACTIVE DUAL RUNNER",
                }

                send_telegram_alert(
                    f"⚡ <b>DUAL-TRANCHE STRATEGY: BUY {side}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 <b>CONTRACT:</b> {sym} (Qty: {LOT_SIZE_QTY}, Δ: {delta:.2f})\n"
                    f"• <b>ENTRY (SPOT):</b> {close_p:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                    f"• <b>INITIAL SL:</b> {sl_val:.1f} (-{INITIAL_SL_PTS} pts) | <b>₹{opt_sl:.2f}</b> (-₹{total_risk_inr})\n"
                    f"• <b>TRANCHE 1 TARGET (50%):</b> {close_p + FIXED_TARGET_PTS if is_ce else close_p - FIXED_TARGET_PTS:.1f} | <b>₹{opt_t1:.2f}</b>\n"
                    f"• <b>TRANCHE 2 (50%):</b> Zero-Negative Trailing Runner\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏰ <b>Trigger Time:</b> {close_time_str}"
                )

    def evaluate_live_trail_and_exit(self, live_spot):
        if not is_market_hours() or not self.locked_trade or self.locked_trade.get("closed"):
            return

        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        entry_p = tr["entry"]

        # Check Tranche 1 Fixed Target (+130 pts)
        if not tr.get("t1_hit", False):
            if is_ce and live_spot >= (entry_p + FIXED_TARGET_PTS):
                tr["t1_hit"] = True
                tr["t1_exit_spot"] = entry_p + FIXED_TARGET_PTS
                send_telegram_alert(f"🎯 <b>TRANCHE 1 BOOKED (+{FIXED_TARGET_PTS} pts)</b>\n50% position secured at Index: {tr['t1_exit_spot']:.1f}")
            elif not is_ce and live_spot <= (entry_p - FIXED_TARGET_PTS):
                tr["t1_hit"] = True
                tr["t1_exit_spot"] = entry_p - FIXED_TARGET_PTS
                send_telegram_alert(f"🎯 <b>TRANCHE 1 BOOKED (+{FIXED_TARGET_PTS} pts)</b>\n50% position secured at Index: {tr['t1_exit_spot']:.1f}")

        # Check Stop Loss on Tranche 2 Runner
        stop_hit = (live_spot <= tr["current_sl"]) if is_ce else (live_spot >= tr["current_sl"])
        if stop_hit:
            t1_exit = tr.get("t1_exit_spot") if tr.get("t1_hit") else tr["current_sl"]
            self.close_dual_trade(t1_exit, tr["current_sl"], "TRUNCATED STOP LOSS HIT")
            return

        # Tranche 2 Trailing Engine (Stage 2 & 3)
        if not tr["active_trailing"]:
            favorable = (live_spot - entry_p) if is_ce else (entry_p - live_spot)
            if favorable >= 50.0 and tr["current_sl"] == tr["init_sl"]:
                tr["current_sl"] = round(entry_p + 10.0 if is_ce else entry_p - 10.0, 1)
            if favorable >= STAGE2_TRIGGER:
                tr["active_trailing"] = True
                tr["best_price"] = live_spot
                tr["current_sl"] = round(max(tr["current_sl"], tr["best_price"] - STAGE2_BUFFER) if is_ce else min(tr["current_sl"], tr["best_price"] + STAGE2_BUFFER), 1)
                self.alert_sl_update(tr, "TRAILING ACTIVATED (+90pt TRIGGER)")
        else:
            if is_ce:
                if live_spot > tr["best_price"]:
                    tr["best_price"] = live_spot
                    favorable = live_spot - entry_p
                    buf = STAGE3_BUFFER if favorable >= STAGE3_TRIGGER else STAGE2_BUFFER
                    new_sl = round(tr["best_price"] - buf, 1)
                    if new_sl > tr["current_sl"]:
                        tr["current_sl"] = new_sl
                        self.alert_sl_update(tr, "TRAIL TIGHTENED")
            else:
                if live_spot < tr["best_price"]:
                    tr["best_price"] = live_spot
                    favorable = entry_p - live_spot
                    buf = STAGE3_BUFFER if favorable >= STAGE3_TRIGGER else STAGE2_BUFFER
                    new_sl = round(tr["best_price"] + buf, 1)
                    if new_sl < tr["current_sl"]:
                        tr["current_sl"] = new_sl
                        self.alert_sl_update(tr, "TRAIL TIGHTENED")

        if self.today_trade_record:
            self.today_trade_record["current_sl"] = tr["current_sl"]

    def alert_sl_update(self, tr, reason):
        entry_p = tr["entry"]
        is_ce = tr["type"] == "CE"
        cur_sl = tr["current_sl"]
        delta_v = tr.get("delta", 0.50)
        opt_entry = tr.get("opt_entry_ltp", 75.0)

        locked_pts = round((cur_sl - entry_p) if is_ce else (entry_p - cur_sl), 1)
        locked_inr = int(round(locked_pts * delta_v * LOT_SIZE_QTY * 0.5))  # 50% tranche weight
        gtt_opt = max(0.5, round(opt_entry + (locked_pts * delta_v), 2))
        sign = "+" if locked_pts >= 0 else ""
        t_now = datetime.datetime.now(IST).strftime("%H:%M:%S")

        send_telegram_alert(
            f"🔔 <b>NIFTY {tr['type']} RUNNER SL UPDATED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>STAGE:</b> {reason}\n"
            f"<b>CONTRACT:</b> {tr['symbol']}\n"
            f"🛡 <b>RUNNER STOP LOSS:</b> {cur_sl:.1f}\n"
            f"🔒 <b>LOCKED RUNNER PROFIT:</b> {sign}{locked_pts:.1f} pts (₹{sign}{locked_inr})\n"
            f"⏰ <b>Time:</b> {t_now}"
        )

    def close_dual_trade(self, t1_exit_spot, t2_exit_spot, reason):
        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        entry_p = tr["entry"]
        delta_v = tr.get("delta", 0.50)

        t1_pts = (t1_exit_spot - entry_p) if is_ce else (entry_p - t1_exit_spot)
        t2_pts = (t2_exit_spot - entry_p) if is_ce else (entry_p - t2_exit_spot)
        final_pts = round((0.5 * t1_pts) + (0.5 * t2_pts), 2)
        final_inr = int(round(final_pts * delta_v * LOT_SIZE_QTY))
        sign = "+" if final_pts >= 0 else ""
        outcome_str = "PROFIT" if final_pts > 0 else "LOSS"

        t_now = datetime.datetime.now(IST).strftime("%H:%M:%S")

        tr["closed"] = True
        tr["exit_spot"] = t2_exit_spot
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
            "text": f"EXIT DUAL {tr['type']}",
        })

        e_key = str(tr["time"])
        if e_key in self.history_cards:
            self.history_cards[e_key]["outcome"] = f"CLOSED: {sign}{final_pts}p (₹{sign}{final_inr})"
            self.history_cards[e_key]["exit_price"] = t2_exit_spot
            self.history_cards[e_key]["exit_reason"] = reason

        self.today_trade_record = tr.copy()
        self.locked_trade = None

        send_telegram_alert(
            f"🏁 <b>DUAL-TRANCHE TRADE CLOSED ({reason})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚪 <b>T1 Exit:</b> {t1_exit_spot:.1f} | <b>T2 Exit:</b> {t2_exit_spot:.1f}\n"
            f"💰 <b>Blended Net PnL:</b> {sign}{final_pts:.2f} pts (₹{sign}{final_inr})\n"
            f"📋 <b>Daily Limit:</b> 1/1 Trade Completed for Today.\n"
            f"⏰ <b>Exit Time:</b> {t_now}"
        )

    def execute_eod_exit(self, ltp):
        tr = self.locked_trade
        t1_exit = tr.get("t1_exit_spot") if tr.get("t1_hit") else ltp
        self.close_dual_trade(t1_exit, ltp, "MANDATORY 15:15 EOD SQUARE-OFF")

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
                trail_pts = round((cur_trail - entry_p) if is_ce else (entry_p - cur_trail), 1)
                trail_opt = max(0.5, round(opt_entry + (trail_pts * delta_val), 2))
                is_closed = False
            else:
                opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else opt_entry
                trail_pts = float(tr_source.get("final_pts", 0.0))
                trail_opt = float(tr_source.get("opt_sl", 0.0))
                is_closed = True

            hud_payload = {
                "type": tr_source["type"],
                "mode": tr_source.get("mode"),
                "symbol": tr_source["symbol"],
                "entry_spot": entry_p,
                "entry_opt": opt_entry,
                "sl_spot": float(tr_source["init_sl"]),
                "sl_opt": float(tr_source.get("opt_sl", 0.0)),
                "trail_spot": cur_trail,
                "trail_opt": trail_opt,
                "target_p": float(tr_source.get("target_p", 0.0)),
                "opt_target": float(tr_source.get("opt_target", 0.0)),
                "targets": tr_source.get("targets", []),
                "delta": delta_val,
                "secured_pts": trail_pts,
                "trade_closed": is_closed,
                "exit_spot": tr_source.get("exit_spot"),
                "exit_reason": tr_source.get("exit_reason"),
                "exit_time": tr_source.get("exit_time"),
                "final_pts": tr_source.get("final_pts", 0.0),
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
                    "time": t,
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "vwap": float(c.get("vwap", c["close"])),
                    "ema20": float(c.get("ema20", c["close"])),
                }

        sorted_times = sorted(clean_map.keys())
        recent_candles = [clean_map[t] for t in sorted_times][-450:]

        payload = {
            "ltp": float(live_ltp),
            "opt_ltp": float(opt_ltp),
            "candles": recent_candles,
            "locked_trade": self.locked_trade,
            "today_trade_record": self.today_trade_record,
            "trades_taken_today": self.trades_taken_today,
            "trade_date": self.today.strftime("%Y-%m-%d"),
            "daily_atr10": self.daily_atr10,
            "is_high_vol": True,
            "vol_threshold": 0.0,
            "orb_levels": self.orb_levels,
            "fib_lines": self.fib_lines,
            "pivots": self.pivot_lines,
            "markers": self.markers,
            "history_cards": self.history_cards,
            "contract_preview": self.contract_preview,
            "prev_day_summary": self.prev_day_summary,
            "hud": hud_payload,
            "updated_at": time.time(),
        }
        push_to_redis("nifty_live_state", payload)


def run_worker():
    while True:
        try:
            print("[Supervisor]: Initializing SmartAPI Session...")
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
                        engine.train_ml_model()
                        engine.run_full_strategy_pipeline()
                        engine.simulate_historical_trades()
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
                print("[WebSocket]: Connected & Subscribed to Nifty Index Token 99926000")
                sws.subscribe("correl_id_1", 1, [{"exchangeType": 1, "tokens": [INDEX_TOKEN]}])

            def on_error(wsapp, error):
                print(f"[WebSocket Error]: {error}")

            def on_close(wsapp):
                print("[WebSocket Closed]: Connection dropped.")

            sws.on_open = on_open
            sws.on_data = on_data
            sws.on_error = on_error
            sws.on_close = on_close
            sws._on_error = on_error
            sws._on_close = on_close

            sws.connect()

        except Exception as e:
            print(f"[Supervisor Error]: {e}. Reconnecting in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    run_worker()
