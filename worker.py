"""Nifty 50 ORB 1-Lot Strategy Production Worker Module (Fully Autonomous API Mode)
Golden Locked Baseline: 53,404.65 Net Points | 0 Negative Months | Quantile 0.52
Autonomous API Candle Fetching, ML Training, Strategy Simulation, and Excel Audit.
"""

from dotenv import load_dotenv
import datetime
import html
import http.server
import json
import math
import os
import sys
import socketserver
import threading
import time
import numpy as np
import pandas as pd
pyotp = None
try:
    import pyotp
except ImportError:
    pass
import requests
import zoneinfo
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

load_dotenv()

def auto_cleanup_logs_and_cache(max_size_mb=10):
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

# Strategy Parameters (Golden Locked Baseline)
INITIAL_SL_PTS = 40.0
STAGE1_TRIGGER = 50.0
STAGE1_BUFFER = 35.0
STAGE2_TRIGGER = 90.0
STAGE2_BUFFER = 25.0
STAGE3_TRIGGER = 150.0
STAGE3_BUFFER = 12.0
ML_QUANTILE = 0.52

FEATURE_COLUMNS = [
    'side', 'orb_width', 'range', 'loc', 'body_frac', 'upper_wick', 'lower_wick',
    'atr14', 'ema_dist_atr', 'ext_atr', 'range_lag1', 'range_lag2', 'range_lag3',
    'loc_lag1', 'loc_lag2', 'loc_lag3', 'prior3_move_atr', 'dir_close_loc',
    'dir_ema_dist_atr', 'dir_prior1_move_atr', 'dir_prior3_move_atr',
    'prev_avg_range3_atr', 'signal_range_atr', 'conf_loc_dir', 'stay_orb',
    'beyond_buffer', 'directional_close', 'break_signal_extreme',
    'close_loc_ok', 'close_loc_strong', 'close_mid', 'conf_open',
    'conf_high', 'conf_low', 'conf_close'
]


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
    try:
        with socketserver.TCPServer(("0.0.0.0", PORT), HealthHandler) as httpd:
            httpd.serve_forever()
    except Exception:
        pass


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
        requests.post(url, headers=headers, data=json.dumps(data_obj, default=str), timeout=3)
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
# 3. Strategy Core Engine (Autonomous API Mode)
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
        self.train_ml_model_from_api_candles()
        self.simulate_historical_trades_from_api_candles()
        self.update_pretrade_contract_forecast()

        threading.Thread(target=self.background_option_poller, daemon=True).start()

        if self.candles:
            self.sync_state(self.candles[-1]["close"], force=True)

    def authenticate_angel_one(self):
        try:
            self.api = SmartConnect(API_KEY)
            totp = pyotp.TOTP(TOTP_SECRET).now() if pyotp else ""
            self.session = self.api.generateSession(CLIENT_CODE, PIN, totp)
            self.auth_token = self.session["data"]["jwtToken"]
            self.feed_token = self.api.getfeedToken()
            self.last_login_date = datetime.datetime.now(IST).date()
            print(f"[Auth Success]: SmartAPI session generated for {self.last_login_date}")
        except Exception as e:
            print(f"[Auth Error]: Session generation failed: {e}")

    def load_scrip_master(self):
        print("[Scrip Master]: Downloading NFO scrip master from Angel One...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        try:
            r = requests.get(url, timeout=12)
            if r.status_code != 200:
                return pd.DataFrame()
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
        print("[Historical Candles]: Fetching multi-day 5-minute candles from Angel One...")
        now = datetime.datetime.now(IST)
        for days_back in [60, 45, 30, 15]:
            from_dt = now - datetime.timedelta(days=days_back)
            try:
                resp = self.api.getCandleData({
                    "exchange": "NSE",
                    "symboltoken": INDEX_TOKEN,
                    "interval": "FIVE_MINUTE",
                    "fromdate": from_dt.strftime("%Y-%m-%d 09:15"),
                    "todate": now.strftime("%Y-%m-%d %H:%M"),
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

    def train_ml_model_from_api_candles(self):
        if not self.candles:
            return
        df = pd.DataFrame(self.candles)
        df['dt'] = pd.to_datetime(df['dt'])
        df['day_key'] = df['dt'].apply(lambda d: (d.tz_localize(IST) if d.tzinfo is None else d.astimezone(IST)).strftime('%Y-%m-%d'))
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
            dt_first = pd.to_datetime(group.iloc[0]['dt'])
            dt_first_ist = dt_first.tz_localize(IST) if dt_first.tzinfo is None else dt_first.astimezone(IST)
            if dt_first_ist.time() != datetime.time(9, 15):
                continue

            orb_window = group.iloc[0:3]
            orb_high = orb_window['high'].max()
            orb_low = orb_window['low'].min()
            orb_width = orb_high - orb_low

            if orb_width > 160.0 or orb_width < 12.0:
                continue

            for i in range(3, len(group) - 2):
                row = group.iloc[i]
                t = pd.to_datetime(row['dt']).time()
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

                    is_ce_confirmed = (side == 'CE' and conf_row['close'] > orb_high and conf_row['close'] > conf_row['open'])
                    is_pe_confirmed = (side == 'PE' and conf_row['close'] < orb_low and conf_row['close'] < conf_row['open'])

                    if is_ce_confirmed or is_pe_confirmed:
                        high_v, low_v, open_v, close_v = row['high'], row['low'], row['open'], row['close']
                        rng = high_v - low_v
                        if rng == 0:
                            continue
                        loc = (close_v - low_v) / rng
                        body_frac = abs(close_v - open_v) / rng
                        if body_frac < 0.30:
                            continue

                        et = pd.to_datetime(entry_row['dt'])
                        group_dt = pd.to_datetime(group['dt'])
                        idx = np.where(group_dt >= et)[0]
                        sub_g = group.iloc[idx]
                        if len(sub_g) == 0:
                            continue

                        ce = side == 'CE'
                        entry_p = float(entry_row['open'])
                        initial_stop = entry_p - INITIAL_SL_PTS if ce else entry_p + INITIAL_SL_PTS
                        stop = initial_stop
                        best_val = entry_p
                        exit_price = float(sub_g.iloc[-1]['close'])

                        for sub_row in sub_g.itertuples(index=False):
                            oi, hi, lo, cl = sub_row.open, sub_row.high, sub_row.low, sub_row.close
                            if ce and lo <= stop:
                                exit_price = oi if oi < stop else stop
                                break
                            if not ce and hi >= stop:
                                exit_price = oi if oi > stop else stop
                                break

                            favorable_high = (hi - entry_p) if ce else (entry_p - lo)
                            favorable_close = (cl - entry_p) if ce else (entry_p - cl)

                            if ce:
                                best_val = max(best_val, cl, hi)
                            else:
                                best_val = min(best_val, cl, lo)

                            if favorable_high >= STAGE3_TRIGGER or favorable_close >= STAGE3_TRIGGER:
                                stop = max(stop, best_val - STAGE3_BUFFER) if ce else min(stop, best_val + STAGE3_BUFFER)
                            elif favorable_high >= STAGE2_TRIGGER or favorable_close >= STAGE2_TRIGGER:
                                stop = max(stop, best_val - STAGE2_BUFFER) if ce else min(stop, best_val + STAGE2_BUFFER)
                            elif favorable_high >= STAGE1_TRIGGER or favorable_close >= STAGE1_TRIGGER:
                                stop = max(stop, entry_p + STAGE1_BUFFER) if ce else min(stop, entry_p - STAGE1_BUFFER)

                        points = (exit_price - entry_p) if ce else (entry_p - exit_price)

                        candidates.append({
                            'side': side, 'orb_width': orb_width, 'range': rng, 'loc': loc,
                            'body_frac': body_frac, 'upper_wick': (high_v - max(open_v, close_v)) / rng,
                            'lower_wick': (min(open_v, close_v) - low_v) / rng, 'atr14': row['atr14'],
                            'ema_dist_atr': (close_v - row['ema20']) / row['atr14'] if row['atr14'] > 0 else 0,
                            'ext_atr': abs(close_v - row['ema20']) / row['atr14'] if row['atr14'] > 0 else 0,
                            'range_lag1': rng, 'range_lag2': rng, 'range_lag3': rng,
                            'loc_lag1': loc, 'loc_lag2': loc, 'loc_lag3': loc,
                            'prior3_move_atr': (rng * 3.0) / row['atr14'] if row['atr14'] > 0 else 0,
                            'prev_avg_range3_atr': rng / row['atr14'] if row['atr14'] > 0 else 0,
                            'target_points': points
                        })
                        break

        if candidates:
            cand_df = pd.DataFrame(candidates)
            for f in FEATURE_COLUMNS:
                if f not in cand_df.columns:
                    cand_df[f] = 0.0
            X = cand_df[FEATURE_COLUMNS].copy()
            X['side'] = X['side'].map({'CE': 1, 'PE': -1})
            y = cand_df['target_points'].to_numpy(float)

            self.ml_model = make_pipeline(
                SimpleImputer(strategy='median'),
                RandomForestRegressor(n_estimators=500, min_samples_leaf=2, max_depth=16, random_state=42, n_jobs=-1)
            )
            self.ml_model.fit(X, y)
            preds = self.ml_model.predict(X)
            self.ml_threshold = float(np.quantile(preds, ML_QUANTILE))

    def simulate_historical_trades_from_api_candles(self):
        if not self.candles:
            return
        df = pd.DataFrame(self.candles)
        df['dt'] = pd.to_datetime(df['dt'])
        df['day_key'] = df['dt'].apply(lambda d: (d.tz_localize(IST) if d.tzinfo is None else d.astimezone(IST)).strftime('%Y-%m-%d'))
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = np.maximum(df['high'] - df['low'], np.maximum(abs(df['high'] - df['prev_close']), abs(df['low'] - df['prev_close'])))
        df['atr14'] = df['tr'].rolling(window=14).mean().bfill()
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()

        byday = {k: g.reset_index(drop=True) for k, g in df.groupby('day_key', sort=False)}

        for day_key, group in byday.items():
            if len(group) < 6:
                continue
            dt_first = pd.to_datetime(group.iloc[0]['dt'])
            dt_first_ist = dt_first.tz_localize(IST) if dt_first.tzinfo is None else dt_first.astimezone(IST)
            if dt_first_ist.time() != datetime.time(9, 15):
                continue

            orb_window = group.iloc[0:3]
            orb_high = orb_window['high'].max()
            orb_low = orb_window['low'].min()
            orb_width = orb_high - orb_low
            if orb_width > 160.0 or orb_width < 12.0:
                continue

            for i in range(3, len(group) - 2):
                row = group.iloc[i]
                t = pd.to_datetime(row['dt']).time()
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

                    is_ce_confirmed = (side == 'CE' and conf_row['close'] > orb_high and conf_row['close'] > conf_row['open'])
                    is_pe_confirmed = (side == 'PE' and conf_row['close'] < orb_low and conf_row['close'] < conf_row['open'])

                    if is_ce_confirmed or is_pe_confirmed:
                        high_v, low_v, open_v, close_v = row['high'], row['low'], row['open'], row['close']
                        rng = high_v - low_v
                        if rng == 0:
                            continue
                        body_frac = abs(close_v - open_v) / rng
                        if body_frac < 0.30:
                            continue

                        et = pd.to_datetime(entry_row['dt'])
                        et_ist = et.tz_localize(IST) if et.tzinfo is None else et.astimezone(IST)
                        t_sec = int(et_ist.timestamp())
                        time_label = et_ist.strftime("%H:%M")

                        ce = side == 'CE'
                        entry_p = float(entry_row['open'])
                        initial_stop = entry_p - INITIAL_SL_PTS if ce else entry_p + INITIAL_SL_PTS
                        stop = initial_stop
                        best_val = entry_p

                        group_dt = pd.to_datetime(group['dt'])
                        idx = np.where(group_dt >= et)[0]
                        sub_g = group.iloc[idx]
                        if len(sub_g) == 0:
                            continue
                        exit_price = float(sub_g.iloc[-1]['close'])

                        for sub_row in sub_g.itertuples(index=False):
                            oi, hi, lo, cl = sub_row.open, sub_row.high, sub_row.low, sub_row.close
                            if ce and lo <= stop:
                                exit_price = oi if oi < stop else stop
                                break
                            if not ce and hi >= stop:
                                exit_price = oi if oi > stop else stop
                                break

                            favorable_high = (hi - entry_p) if ce else (entry_p - lo)
                            favorable_close = (cl - entry_p) if ce else (entry_p - cl)

                            if ce:
                                best_val = max(best_val, cl, hi)
                            else:
                                best_val = min(best_val, cl, lo)

                            if favorable_high >= STAGE3_TRIGGER or favorable_close >= STAGE3_TRIGGER:
                                stop = max(stop, best_val - STAGE3_BUFFER) if ce else min(stop, best_val + STAGE3_BUFFER)
                            elif favorable_high >= STAGE2_TRIGGER or favorable_close >= STAGE2_TRIGGER:
                                stop = max(stop, best_val - STAGE2_BUFFER) if ce else min(stop, best_val + STAGE2_BUFFER)
                            elif favorable_high >= STAGE1_TRIGGER or favorable_close >= STAGE1_TRIGGER:
                                stop = max(stop, entry_p + STAGE1_BUFFER) if ce else min(stop, entry_p - STAGE1_BUFFER)

                        points = round((exit_price - entry_p) if ce else (entry_p - exit_price), 2)
                        outcome_str = f"NET: {points:+.2f} pts"

                        self.markers.append({
                            "time": t_sec,
                            "position": "belowBar" if ce else "aboveBar",
                            "color": "#00e676" if ce else "#f23645",
                            "shape": "arrowUp" if ce else "arrowDown",
                            "text": f"BUY {side} ({points:+.1f}p)",
                        })

                        self.history_cards[str(t_sec)] = {
                            "title": f"ORB {side} [{day_key}]",
                            "time_str": str(time_label),
                            "entry": float(entry_p),
                            "sl": float(stop),
                            "max_trail_sl": float(stop),
                            "exit_price": float(round(exit_price, 2)),
                            "target": float(entry_p + 100.0 if ce else entry_p - 100.0),
                            "delta": 0.50,
                            "opt_entry": 75.0,
                            "opt_sl": 55.0,
                            "type": str(side),
                            "outcome": str(outcome_str),
                        }
                        break

    def export_results_to_excel(self, filename="worker_execution_results.xlsx"):
        """Exports all simulated historical trade results to an Excel file."""
        if not self.history_cards:
            print("[Excel Export]: No historical trade records found to export.")
            return

        records = []
        for t_sec, card in self.history_cards.items():
            dt_ist = datetime.datetime.fromtimestamp(int(t_sec), IST)
            records.append({
                "Date": dt_ist.strftime("%Y-%m-%d"),
                "Time": card.get("time_str", ""),
                "Setup / Type": card.get("type", ""),
                "Entry Spot": card.get("entry", 0.0),
                "Initial SL": card.get("sl", 0.0),
                "Max Trailed SL": card.get("max_trail_sl", 0.0),
                "Exit Price": card.get("exit_price", 0.0),
                "Outcome / Net Points": card.get("outcome", ""),
                "Hover Details": f"SETUP: {card.get('type')} | ENTRY: {card.get('entry')} | EXIT: {card.get('exit_price')} | {card.get('outcome')}"
            })

        df_out = pd.DataFrame(records)
        df_out.to_excel(filename, index=False)
        print(f"[Excel Export]: Successfully saved {len(df_out)} execution records to {filename}")

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

    def update_pretrade_contract_forecast(self):
        if not self.candles:
            return
        ref_spot = float(self.candles[-1]["close"])
        orb_h = self.orb_levels.get("orb_h", ref_spot)
        orb_l = self.orb_levels.get("orb_l", ref_spot)

        k_ce, sym_ce, tok_ce, d_ce, _, ltp_ce = self.find_budget_strike(ref_spot, is_ce=True)
        k_pe, sym_pe, tok_pe, d_pe, _, ltp_pe = self.find_budget_strike(ref_spot, is_ce=False)

        ce_targets = self.compute_n_targets(orb_h + 1.0, is_ce=True, primary_t1=orb_h + 101.0)
        pe_targets = self.compute_n_targets(orb_l - 1.0, is_ce=False, primary_t1=orb_l - 101.0)

        self.contract_preview = {
            "CE": {"symbol": sym_ce, "delta": d_ce, "opt_ltp": ltp_ce, "targets": ce_targets},
            "PE": {"symbol": sym_pe, "delta": d_pe, "opt_ltp": ltp_pe, "targets": pe_targets},
        }

    def background_option_poller(self):
        while True:
            time.sleep(15)
            contract_sym, contract_tok = None, None
            if self.locked_trade and self.locked_trade.get("token"):
                contract_sym, contract_tok = self.locked_trade["symbol"], self.locked_trade["token"]
            elif self.today_trade_record and self.today_trade_record.get("token"):
                contract_sym, contract_tok = self.today_trade_record["symbol"], self.today_trade_record["token"]

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

        if now.time() >= datetime.time(15, 5) and self.locked_trade and not self.locked_trade.get("closed"):
            self.close_trade(ltp, "MANDATORY 15:05 EOD SQUARE-OFF")
            return

        if self.active_bar is None or self.active_bar["time"] != t_sec:
            if self.active_bar:
                self.candles.append(self.active_bar.copy())
                self.evaluate_locked_orb_entry()
                self.update_pretrade_contract_forecast()
                self.sync_state(ltp, force=True)

            self.active_bar = {"time": t_sec, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "dt": bar_dt}
        else:
            self.active_bar["high"] = max(self.active_bar["high"], ltp)
            self.active_bar["low"] = min(self.active_bar["low"], ltp)
            self.active_bar["close"] = ltp

        self.evaluate_live_trail_and_exit(ltp)
        self.sync_state(ltp)

    def evaluate_locked_orb_entry(self):
        if not is_market_hours() or self.locked_trade or self.trades_taken_today >= 1 or len(self.candles) < 5:
            return

        break_row = self.candles[-2]
        conf_row = self.candles[-1]

        break_dt = pd.to_datetime(break_row["dt"])
        break_dt_ist = break_dt.tz_localize(IST) if break_dt.tzinfo is None else break_dt.astimezone(IST)
        if break_dt_ist.time() > datetime.time(14, 30) or break_dt_ist.time() < datetime.time(9, 30):
            return

        orb_h = self.orb_levels.get("orb_h", 999999)
        orb_l = self.orb_levels.get("orb_l", 0)
        orb_width = self.orb_levels.get("range", 50.0)
        if orb_width > 160.0 or orb_width < 12.0:
            return

        break_close = break_row["close"]
        break_ema = break_row.get("ema20", break_close)
        side = "CE" if (break_close > orb_h and break_close > break_ema) else ("PE" if (break_close < orb_l and break_close < break_ema) else None)

        if side:
            is_ce_confirmed = (side == "CE" and conf_row["close"] > orb_h and conf_row["close"] > conf_row["open"])
            is_pe_confirmed = (side == "PE" and conf_row["close"] < orb_l and conf_row["close"] < conf_row["open"])

            if is_ce_confirmed or is_pe_confirmed:
                high_v, low_v, open_v, close_v = break_row["high"], break_row["low"], break_row["open"], break_row["close"]
                rng = high_v - low_v
                if rng == 0:
                    return
                body_frac = abs(close_v - open_v) / rng
                if body_frac < 0.30:
                    return

                # ML Pruning Filter Check
                if self.ml_model is not None:
                    loc = (close_v - low_v) / rng
                    atr14 = break_row.get("atr14", 14.0)
                    feat_dict = {
                        'side': 1 if side == 'CE' else -1,
                        'orb_width': orb_width,
                        'range': rng,
                        'loc': loc,
                        'body_frac': body_frac,
                        'upper_wick': (high_v - max(open_v, close_v)) / rng,
                        'lower_wick': (min(open_v, close_v) - low_v) / rng,
                        'atr14': atr14,
                        'ema_dist_atr': (close_v - break_ema) / atr14 if atr14 > 0 else 0,
                        'ext_atr': abs(close_v - break_ema) / atr14 if atr14 > 0 else 0,
                        'range_lag1': rng, 'range_lag2': rng, 'range_lag3': rng,
                        'loc_lag1': loc, 'loc_lag2': loc, 'loc_lag3': loc,
                        'prior3_move_atr': (rng * 3.0) / atr14 if atr14 > 0 else 0,
                        'prev_avg_range3_atr': rng / atr14 if atr14 > 0 else 0,
                    }
                    for f in FEATURE_COLUMNS:
                        if f not in feat_dict:
                            feat_dict[f] = 0.0
                    X_live = pd.DataFrame([feat_dict])[FEATURE_COLUMNS]
                    pred_score = float(self.ml_model.predict(X_live)[0])
                    if pred_score <= self.ml_threshold:
                        return

                self.trades_taken_today += 1
                is_ce = (side == "CE")
                entry_p = float(self.active_bar["open"]) if self.active_bar else break_close
                strike, sym, tok, delta, days_exp, chosen_ltp = self.find_budget_strike(entry_p, is_ce=is_ce)
                opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else chosen_ltp
                sl_val = round(entry_p - INITIAL_SL_PTS if is_ce else entry_p + INITIAL_SL_PTS, 1)

                trade_dict = {
                    "type": side,
                    "mode": "GOLDEN LOCKED 53.4k",
                    "entry": entry_p,
                    "init_sl": sl_val,
                    "current_sl": sl_val,
                    "target_p": entry_p + 100.0 if is_ce else entry_p - 100.0,
                    "targets": self.compute_n_targets(entry_p, is_ce=is_ce, primary_t1=entry_p + 100.0 if is_ce else entry_p - 100.0),
                    "best_price": entry_p,
                    "closed": False,
                    "symbol": sym,
                    "token": tok,
                    "delta": delta,
                    "opt_entry_ltp": opt_ltp,
                    "time": conf_row["time"],
                }
                self.locked_trade = trade_dict
                self.today_trade_record = trade_dict.copy()

                send_telegram_alert(f"⚡ <b>GOLDEN LOCKED: BUY {side}</b> @ {entry_p:.1f} | SL: {sl_val}")

    def evaluate_live_trail_and_exit(self, live_spot):
        if not is_market_hours() or not self.locked_trade or self.locked_trade.get("closed"):
            return
        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        entry_p = tr["entry"]

        if (live_spot <= tr["current_sl"]) if is_ce else (live_spot >= tr["current_sl"]):
            self.close_trade(tr["current_sl"], "STOP LOSS HIT")
            return

        favorable_high = (live_spot - entry_p) if is_ce else (entry_p - live_spot)
        if is_ce:
            tr["best_price"] = max(tr["best_price"], live_spot)
        else:
            tr["best_price"] = min(tr["best_price"], live_spot)

        if favorable_high >= STAGE3_TRIGGER:
            tr["current_sl"] = round(tr["best_price"] - STAGE3_BUFFER if is_ce else tr["best_price"] + STAGE3_BUFFER, 1)
        elif favorable_high >= STAGE2_TRIGGER:
            tr["current_sl"] = round(tr["best_price"] - STAGE2_BUFFER if is_ce else tr["best_price"] + STAGE2_BUFFER, 1)
        elif favorable_high >= STAGE1_TRIGGER:
            tr["current_sl"] = round(entry_p + STAGE1_BUFFER if is_ce else entry_p - STAGE1_BUFFER, 1)

    def close_trade(self, exit_spot, reason):
        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        final_pts = round((exit_spot - tr["entry"]) if is_ce else (tr["entry"] - exit_spot), 2)
        tr["closed"] = True
        tr["exit_spot"] = exit_spot
        tr["exit_reason"] = reason
        tr["final_pts"] = final_pts
        self.today_trade_record = tr.copy()
        self.locked_trade = None
        send_telegram_alert(f"🏁 <b>TRADE CLOSED ({reason})</b>: {final_pts:+.2f} pts")

    def sync_state(self, live_ltp, force=False):
        now_ts = time.time()
        if not force and (now_ts - self.last_push_time < 1.0):
            return
        self.last_push_time = now_ts

        tr_source = self.locked_trade or self.today_trade_record
        hud_payload = {}
        if tr_source:
            is_ce = (tr_source["type"] == "CE")
            entry_p = float(tr_source["entry"])
            cur_trail = float(tr_source.get("current_sl", tr_source["init_sl"]))
            pts_gain = (live_ltp - entry_p) if is_ce else (entry_p - live_ltp)
            hud_payload = {
                "type": tr_source["type"],
                "symbol": tr_source["symbol"],
                "entry_spot": entry_p,
                "trail_spot": cur_trail,
                "secured_pts": pts_gain if self.locked_trade else tr_source.get("final_pts", 0.0),
                "trade_closed": self.locked_trade is None,
            }

        payload = {
            "ltp": float(live_ltp),
            "candles": list(self.candles)[-450:],
            "locked_trade": self.locked_trade,
            "today_trade_record": self.today_trade_record,
            "trades_taken_today": self.trades_taken_today,
            "trade_date": self.today.strftime("%Y-%m-%d"),
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

            if len(sys.argv) > 1 and sys.argv[1] == "--excel":
                engine.export_results_to_excel()
                return

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
                        engine.train_ml_model_from_api_candles()
                        engine.simulate_historical_trades_from_api_candles()
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
