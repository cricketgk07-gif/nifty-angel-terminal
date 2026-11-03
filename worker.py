# worker.py
from datetime import datetime, time as dtime
import time
import os
import json
import urllib.request
import joblib
import pandas as pd
import numpy as np
import requests
import http.server
import socketserver
import threading
from dotenv import load_dotenv
from SmartApi import SmartConnect
from upstash_redis import Redis

load_dotenv()

# ==========================================
# 0. RENDER PORT & HEALTH CHECK SERVER
# ==========================================
PORT = int(os.environ.get("PORT", 10000))

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
        pass  # Suppress default access logs

def run_ping_server():
    try:
        with socketserver.TCPServer(("0.0.0.0", PORT), HealthHandler) as httpd:
            print(f"[Health Server] Listening on port {PORT}...")
            httpd.serve_forever()
    except Exception as e:
        print(f"[Health Server Error] {e}")

# Start the health check server in a background thread to satisfy Render's port scan requirement
threading.Thread(target=run_ping_server, daemon=True).start()

# Optional keepalive self-ping thread
def self_ping_keepalive():
    while True:
        time.sleep(480)
        try:
            requests.get(f"http://127.0.0.1:{PORT}/", timeout=5)
        except Exception:
            pass

threading.Thread(target=self_ping_keepalive, daemon=True).start()


# ==========================================
# 1. MODEL AUTO-DOWNLOAD & UPSTASH INIT
# ==========================================
MODEL_URL = "https://github.com/cricketgk07-gif/nifty-angel-terminal/releases/download/v1.0/trained_orb_model.pkl"
CUTOFF_URL = "https://github.com/cricketgk07-gif/nifty-angel-terminal/releases/download/v1.0/model_cutoff.pkl"

# Auto-download model files on startup if missing from local container storage
if not os.path.exists('trained_orb_model.pkl'):
    print("[INIT] Downloading trained_orb_model.pkl from GitHub Releases...")
    try:
        urllib.request.urlretrieve(MODEL_URL, 'trained_orb_model.pkl')
        print("[INIT] Model downloaded successfully.")
    except Exception as e:
        print(f"[INIT ERROR] Failed to download model: {e}")

if not os.path.exists('model_cutoff.pkl'):
    print("[INIT] Downloading model_cutoff.pkl from GitHub Releases...")
    try:
        urllib.request.urlretrieve(CUTOFF_URL, 'model_cutoff.pkl')
        print("[INIT] Cutoff downloaded successfully.")
    except Exception as e:
        print(f"[INIT ERROR] Failed to download cutoff: {e}")

# Load Upstash Redis credentials
try:
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    redis_client = Redis(url=url, token=token) if url and token else None
    if redis_client:
        print("[INIT] Connected to Upstash Redis cloud cache successfully.")
    else:
        print("[INIT WARNING] Upstash credentials missing, falling back to local storage.")
except Exception as e:
    print(f"[INIT ERROR] Upstash connection failed: {e}")
    redis_client = None

STATE_KEY = "nifty_live_state"
STATE_FILE = "nifty_live_state.json"
BUDGET_CEILING_DEFAULT = 80.0
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print('==================================================')
print('  RENDER ANGEL ONE ALL-IN-ONE TRADER DAEMON       ')
print('==================================================')

# Load trained ML model and cutoff threshold
ml_model = None
ml_cutoff = 0.52
try:
    if os.path.exists('trained_orb_model.pkl') and os.path.exists('model_cutoff.pkl'):
        ml_model = joblib.load('trained_orb_model.pkl')
        ml_cutoff = joblib.load('model_cutoff.pkl')
        print(f'[INIT] Loaded trained ML model and cutoff successfully: {ml_cutoff}')
    else:
        print('[INIT WARNING] Model files (.pkl) not found in root directory!')
except Exception as e:
    print(f'[INIT ERROR] Failed to load model files: {e}')


# ==========================================
# 2. NOTIFIER FUNCTIONS
# ==========================================
def _send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=5)
        return response.status_code == 200
    except Exception as e:
        print(f"Telegram alert failed: {e}")
        return False

def trigger_live_entry_alert(side, contract, qty, delta, entry_spot, entry_price, init_sl_spot, init_sl_price, init_sl_pnl):
    init_sl_pts = -40.0 if side == 'CE' else 40.0
    stages_str = "+50 (+5) → +75 (+30) → +100 (+65) → +125 (+90) → +150 (+125) → +200+"
    msg = (
        f"⚡ **GOLDEN LOCKED STRATEGY: BUY {side}**\n\n"
        f"📦 **CONTRACT:** {contract} (Qty: {qty}, Δ: {delta})\n"
        f"• **ENTRY (SPOT):** {entry_spot} | ₹{entry_price}\n"
        f"• **INITIAL SL:** {init_sl_spot} ({init_sl_pts} pts) | ₹{init_sl_price} (₹{init_sl_pnl})\n"
        f"• **TRAILING STAGES:** {stages_str}\n"
        f"⏰ **Trigger Time:** {datetime.now().strftime('%H:%M:%S')}"
    )
    _send_telegram(msg)


# ==========================================
# 3. INDICATORS & CANDIDATES
# ==========================================
def calculate_indicators(df):
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['prev_close']), abs(df['low'] - df['prev_close']))
    )
    df['atr14'] = df['tr'].rolling(window=14).mean().bfill()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    return df

def extract_entry_candidates(byday):
    candidates = []
    for day_key, group in byday.items():
        if len(group) < 6 or group.iloc[0]['dt'].time() != dtime(9, 15):
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
            if t > dtime(14, 30) or t < dtime(9, 30):
                continue

            side = 'CE' if (row['close'] > orb_high and row['close'] > row['ema20']) else ('PE' if (row['close'] < orb_low and row['close'] < row['ema20']) else None)

            if side:
                conf_row = group.iloc[i + 1]
                entry_row = group.iloc[i + 2]

                is_ce_confirmed = (side == 'CE' and conf_row['close'] > orb_high and conf_row['close'] > conf_row['open'])
                is_pe_confirmed = (side == 'PE' and conf_row['close'] < orb_low and conf_row['close'] < conf_row['open'])

                if is_ce_confirmed or is_pe_confirmed:
                    rng = row['high'] - row['low']
                    if rng == 0:
                        continue
                    loc = (row['close'] - row['low']) / rng
                    body_frac = abs(row['close'] - row['open']) / rng

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
                        'ema_dist_atr': ((row['close'] - row['ema20']) / row['atr14']) if row['atr14'] > 0 else 0,
                        'ext_atr': (abs(row['close'] - row['ema20']) / row['atr14']) if row['atr14'] > 0 else 0,
                    })
                    break
    return candidates


# ==========================================
# 4. ML FEATURE BUILDER
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

def build_ml_features(cand_df):
    for col in ['range', 'loc']:
        cand_df[f'{col}_lag1'] = cand_df[col].shift(1).fillna(0)
        cand_df[f'{col}_lag2'] = cand_df[col].shift(2).fillna(0)
        cand_df[f'{col}_lag3'] = cand_df[col].shift(3).fillna(0)

    cand_df['prior3_move_atr'] = cand_df['range'].rolling(3).sum().fillna(0) / cand_df['atr14']
    cand_df['prev_avg_range3_atr'] = cand_df['range'].rolling(3).mean().fillna(0) / cand_df['atr14']

    for f_name in FEATURE_COLUMNS:
        if f_name not in cand_df.columns:
            cand_df[f_name] = 0.0
    return cand_df


# ==========================================
# 5. STATE SYNC (Upstash Cloud + Local)
# ==========================================
def read_state():
    try:
        if redis_client:
            val = redis_client.get(STATE_KEY)
            if val:
                return json.loads(val) if isinstance(val, str) else val
    except Exception as e:
        print(f"[REDIS READ ERROR]: {e}")
        
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def write_state(state_dict):
    try:
        if redis_client:
            redis_client.set(STATE_KEY, json.dumps(state_dict))
    except Exception as e:
        print(f"[REDIS WRITE ERROR]: {e}")
        
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state_dict, f)
    except Exception:
        pass


# ==========================================
# 6. ANGEL ONE API & SCANNER
# ==========================================
def fetch_angel_one_candles():
    try:
        obj = SmartConnect(api_key=os.getenv("API_KEY") or os.getenv("ANGEL_API_KEY"))
        exchange = "NSE"
        symboltoken = "99926000"  # Nifty 50 token
        interval = "FIVE_MINUTE"
        
        from_date = (datetime.now() - pd.Timedelta(days=15)).strftime("%Y-%m-%d %H:%M")
        to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        historic_param = {
            "exchange": exchange,
            "symboltoken": symboltoken,
            "interval": interval,
            "fromdate": from_date,
            "todate": to_date
        }
        
        response = obj.getCandleData(historic_param)
        if response and response.get('data'):
            raw_data = response['data']
            df = pd.DataFrame(raw_data, columns=['dt', 'open', 'high', 'low', 'close', 'volume'])
            df['dt'] = pd.to_datetime(df['dt'])
            df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            return df
    except Exception as e:
        print(f"[ANGEL ONE API ERROR]: {e}")
    return pd.DataFrame()

def get_live_option_chain_data(atm_strike, side, max_budget=80.0):
    try:
        obj = SmartConnect(api_key=os.getenv("API_KEY") or os.getenv("ANGEL_API_KEY"))
        instrument_url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        response = urllib.request.urlopen(instrument_url)
        instrument_list = json.loads(response.read().decode('utf-8'))
        
        filtered_opts = [
            i for i in instrument_list 
            if i['name'] == 'NIFTY' 
            and i['instrumenttype'] == 'OPTIDX' 
            and i['tradingsymbol'].endswith(side)
            and abs((float(i['strike']) / 100.0) - atm_strike) <= 150
        ]
        
        if not filtered_opts:
            raise ValueError(f"No matching Nifty option contracts found for ATM {atm_strike} {side}.")
            
        filtered_opts.sort(key=lambda x: abs((float(x['strike']) / 100.0) - atm_strike))
        
        for opt in filtered_opts[:5]:
            symbol = opt['tradingsymbol']
            token = opt['symboltoken']
            exchange = opt['exchange']
            
            ltp_res = obj.getLtpData(exchange, symbol, token)
            if ltp_res and ltp_res.get('data'):
                live_ltp = float(ltp_res['data']['ltp'])
                if 0 < live_ltp <= max_budget:
                    strike_val = float(opt['strike']) / 100.0
                    moneyness = strike_val - atm_strike if side == 'CE' else atm_strike - strike_val
                    dynamic_delta = round(max(0.20, min(0.65, 0.50 - (moneyness / 200.0) * 0.15)), 2)
                    return {'symbol': symbol, 'ltp': live_ltp, 'delta': dynamic_delta}
                    
        raise ValueError(f"No option contract found under ₹{max_budget} budget ceiling.")
    except Exception as e:
        print(f"[DYNAMIC OPTION ERROR]: {e}")
        raise e

def scan_historical_markers(df):
    markers = []
    if df.empty:
        return markers
    try:
        df['date'] = df['dt'].dt.date
        for day, group in df.groupby('date'):
            if len(group) < 10:
                continue
            sub_df = group.reset_index(drop=True)
            sub_df = calculate_indicators(sub_df)
            day_key = str(day)
            candidates = extract_entry_candidates({day_key: sub_df})
            
            if candidates and ml_model is not None:
                cand_df = pd.DataFrame(candidates)
                cand_df = build_ml_features(cand_df)
                X = cand_df[FEATURE_COLUMNS].copy()
                X['side'] = X['side'].map({'CE': 1, 'PE': -1})
                predictions = ml_model.predict(X)
                cand_df['predicted_expected_points'] = predictions
                
                valid_signals = cand_df[cand_df['predicted_expected_points'] > ml_cutoff]
                if not valid_signals.empty:
                    sig = valid_signals.iloc[0]
                    side = sig['side']
                    entry_spot = float(sig['entry_price'])
                    sig_time = pd.Timestamp(sig['signal_time'])
                    
                    markers.append({
                        'time': int(sig_time.timestamp()),
                        'position': 'belowBar' if side == 'CE' else 'aboveBar',
                        'color': '#00e676' if side == 'CE' else '#ff5252',
                        'shape': 'arrowUp' if side == 'CE' else 'arrowDown',
                        'text': f"ORB {side} [{day_key}]\nTime: {sig_time.strftime('%H:%M')}\nIndex Entry: {entry_spot:.1f} | Opt: ₹75"
                    })
    except Exception as e:
        print(f"[MARKER SCAN ERROR] {e}")
    return markers


# ==========================================
# 7. MAIN EXECUTION LOOP
# ==========================================
def poll_and_evaluate_strategy():
    try:
        now_str = datetime.now().strftime('%H:%M:%S')
        df = fetch_angel_one_candles()
        if df.empty or len(df) < 6:
            print(f"[{now_str}] Waiting for Angel One candle feed...", end='\r')
            return

        df = calculate_indicators(df)
        day_key = df['dt'].dt.strftime('%Y-%m-%d').iloc[-1]
        byday = {day_key: df[df['dt'].dt.strftime('%Y-%m-%d') == day_key].reset_index(drop=True)}

        candidates = extract_entry_candidates(byday)
        current_state = read_state()
        if not isinstance(current_state, dict):
            current_state = {}

        user_budget = float(current_state.get("user_selected_budget", BUDGET_CEILING_DEFAULT))

        candles_payload = []
        for _, row in df.iterrows():
            candles_payload.append({
                'time': int(row['dt'].timestamp()),
                'open': float(row['open']), 'high': float(row['high']),
                'low': float(row['low']), 'close': float(row['close']),
                'vwap': float(row.get('close', 0)), 'ema20': float(row.get('ema20', row['close']))
            })

        current_state['candles'] = candles_payload
        current_state['ltp'] = float(df['close'].iloc[-1])
        current_state['pivots'] = {'P': float(df['close'].mean()), 'PDH': float(df['high'].max()), 'PDL': float(df['low'].min())}
        current_state['orb_levels'] = {'orb_h': float(df['high'].iloc[0:3].max()), 'orb_l': float(df['low'].iloc[0:3].min())}
        current_state['markers'] = scan_historical_markers(df)

        if candidates and ml_model is not None:
            cand_df = pd.DataFrame(candidates)
            cand_df = build_ml_features(cand_df)
            X = cand_df[FEATURE_COLUMNS].copy()
            X['side'] = X['side'].map({'CE': 1, 'PE': -1})
            
            predictions = ml_model.predict(X)
            cand_df['predicted_expected_points'] = predictions
            valid_signals = cand_df[cand_df['predicted_expected_points'] > ml_cutoff]

            if not valid_signals.empty and current_state.get('trades_taken_today', 0) == 0:
                sig = valid_signals.iloc[0]
                side = sig['side']
                entry_spot = float(sig['entry_price'])
                
                print(f"\n[{now_str}] [SIGNAL CONFIRMED] Valid {side} breakout! ML Score: {sig['predicted_expected_points']:.2f}")
                
                atm_strike = int(round(entry_spot / 50.0) * 50)
                opt_data = get_live_option_chain_data(atm_strike, side, max_budget=user_budget)
                contract_name = opt_data['symbol']
                delta = opt_data['delta']
                option_entry_price = opt_data['ltp']
                lot_size = 65
                
                index_sl_spot = current_state['orb_levels']['orb_l'] if side == 'CE' else current_state['orb_levels']['orb_h']
                index_risk_points = abs(entry_spot - index_sl_spot)
                option_sl_points = index_risk_points * delta
                option_sl_price = max(5.0, option_entry_price - option_sl_points)
                initial_risk_pnl = -(option_entry_price - option_sl_price) * lot_size

                trigger_live_entry_alert(side, contract_name, lot_size, delta, entry_spot, option_entry_price, index_sl_spot, option_sl_price, initial_risk_pnl)
                
                current_state['trades_taken_today'] = 1
                current_state['hud'] = {
                    'type': side, 'trade_closed': False, 'exit_reason': 'ACTIVE',
                    'symbol': contract_name, 'delta': delta, 'entry_spot': entry_spot,
                    'entry_opt': option_entry_price, 'sl_spot': index_sl_spot, 'sl_opt': option_sl_price,
                    'trail_spot': entry_spot, 'trail_opt': option_entry_price, 'secured_pts': 0.0,
                    'targets': [entry_spot + 50, entry_spot + 100]
                }

        current_state['last_poll'] = now_str
        write_state(current_state)
    except Exception as e:
        print(f"[PIPELINE ERROR] {e}")

def run_live_daemon():
    while True:
        now = datetime.now()
        current_time = now.time()
        if dtime(9, 15) <= current_time <= dtime(15, 30):
            poll_and_evaluate_strategy()
        else:
            print(f'[{now.strftime("%H:%M:%S")}] Outside market hours. Standby...', end='\r')
        time.sleep(3)

if __name__ == '__main__':
    run_live_daemon()
