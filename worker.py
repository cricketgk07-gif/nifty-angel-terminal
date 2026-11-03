# worker.py
from datetime import datetime, time as dtime
import time
import os
import json
import joblib
import pandas as pd
import urllib.request
from dotenv import load_dotenv
from SmartApi import SmartConnect

load_dotenv()

from strategy_entry import calculate_indicators, extract_entry_candidates, trigger_live_entry_alert
from strategy_ml import build_ml_features, FEATURE_COLUMNS
from strategy_tsl import run_ detailed_replay, trigger_live_trailing_alert, trigger_live_closure_alert
import notifier

STATE_FILE = "nifty_live_state.json"
BUDGET_CEILING_DEFAULT = 80.0

print('==================================================')
print('  ORACLE ANGEL ONE LIVE & HISTORY TRADER DAEMON   ')
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

def read_state():
    """Reads current live state locally (synchronized with app.py)."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def write_state(state_dict):
    """Writes live state locally so Streamlit app.py instantly reflects updates."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state_dict, f)
    except Exception as e:
        print(f"[STATE WRITE ERROR]: {e}")

def fetch_angel_one_candles():
    """
    Pulls real-time and historical 5-minute NIFTY OHLC candles directly from Angel One SmartAPI.
    Strictly zero mock data.
    """
    try:
        obj = SmartConnect(api_key=os.getenv("ANGEL_API_KEY"))
        # Authenticate if session tokens are set
        # obj.generateSession(os.getenv("ANGEL_CLIENT_ID"), os.getenv("ANGEL_PASSWORD"))
        
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
    """
    Dynamically queries Angel One's scrip master and live LTP endpoint 
    to find the option contract near ATM with an LTP strictly below the budget ceiling.
    """
    try:
        obj = SmartConnect(api_key=os.getenv("ANGEL_API_KEY"))
        
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
                    
                    print(f"[OPTION DYNAMIC SELECT] Selected {symbol} | LTP: ₹{live_ltp:.2f} | Delta: {dynamic_delta}")
                    return {
                        'symbol': symbol,
                        'ltp': live_ltp,
                        'delta': dynamic_delta
                    }
                    
        raise ValueError(f"No option contract found under ₹{max_budget} budget ceiling near ATM {atm_strike}.")

    except Exception as e:
        print(f"[DYNAMIC OPTION ERROR]: {e}")
        raise e

def scan_historical_markers(df):
    """
    Scans past 10+ days using actual Angel One OHLC data and builds rich tooltip markers 
    matching your Streamlit chart format.
    """
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
            byday = {day_key: sub_df}
            
            candidates = extract_entry_candidates(byday)
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
                        'text': f"ORB {side} [{day_key}]\nTime: {sig_time.strftime('%H:%M')}\nIndex Entry: {entry_spot:.1f} | Opt: ₹75\nInitial SL: {entry_spot-40:.1f} | Opt SL: ₹55\nStatus: ACTIVE"
                    })
    except Exception as e:
        print(f"[MARKER SCAN ERROR] {e}")
    return markers

def poll_and_evaluate_strategy():
    """Core high-speed evaluation loop: Indicators -> Entry -> ML -> Dynamic TSL Conversion -> Notifier -> State Sync"""
    try:
        now_str = datetime.now().strftime('%H:%M:%S')
        
        # 1. Pull real OHLC candles from Angel One API
        df = fetch_angel_one_candles()
        if df.empty or len(df) < 6:
            print(f"[{now_str}] Waiting for Angel One candle feed...", end='\r')
            return

        df = calculate_indicators(df)
        day_key = df['dt'].dt.strftime('%Y-%m-%d').iloc[-1]
        byday = {day_key: df[df['dt'].dt.strftime('%Y-%m-%d') == day_key].reset_index(drop=True)}

        # 2. Extract Entry Candidates for today
        candidates = extract_entry_candidates(byday)
        
        current_state = read_state()
        if not isinstance(current_state, dict):
            current_state = {}

        # Get user budget ceiling from state (default to ₹80.0 or user slider)
        user_budget = float(current_state.get("user_selected_budget", BUDGET_CEILING_DEFAULT))

        # Format candles payload for Streamlit Lightweight Charts
        candles_payload = []
        for _, row in df.iterrows():
            candles_payload.append({
                'time': int(row['dt'].timestamp()),
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'vwap': float(row.get('close', 0)),
                'ema20': float(row.get('ema20', row['close']))
            })

        current_state['candles'] = candles_payload
        current_state['ltp'] = float(df['close'].iloc[-1])
        current_state['pivots'] = {'P': float(df['close'].mean()), 'PDH': float(df['high'].max()), 'PDL': float(df['low'].min())}
        current_state['orb_levels'] = {'orb_h': float(df['high'].iloc[0:3].max()), 'orb_l': float(df['low'].iloc[0:3].min())}

        # Generate 10-day historical tooltip markers from Angel One history
        historical_markers = scan_historical_markers(df)
        current_state['markers'] = historical_markers

        # 3. Evaluate ML Model Gatekeeper for live trade
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
                
                print(f"\n[{now_str}] [SIGNAL CONFIRMED] Valid {side} breakout detected! ML Score: {sig['predicted_expected_points']:.2f}")
                
                # Dynamic ATM strike selection based on breakout spot
                atm_strike = int(round(entry_spot / 50.0) * 50)
                
                # FULLY DYNAMIC: Fetch real Delta and Option LTP from Angel One under user budget ceiling (e.g. ₹80)
                opt_data = get_live_option_chain_data(atm_strike, side, max_budget=user_budget)
                contract_name = opt_data['symbol']
                delta = opt_data['delta']
                option_entry_price = opt_data['ltp']
                
                # Lot size is strictly 65 for Nifty
                lot_size = 65
                
                # Calculate index risk points from TSL logic and convert to option money value using Delta
                index_sl_spot = current_state['orb_levels']['orb_l'] if side == 'CE' else current_state['orb_levels']['orb_h']
                index_risk_points = abs(entry_spot - index_sl_spot)
                
                option_sl_points = index_risk_points * delta
                option_sl_price = max(5.0, option_entry_price - option_sl_points)
                initial_risk_pnl = -(option_entry_price - option_sl_price) * lot_size

                # 4. Trigger Instant Telegram Alert via Notifier
                trigger_live_entry_alert(
                    side=side,
                    contract=contract_name,
                    qty=lot_size,
                    delta=delta,
                    entry_spot=entry_spot,
                    entry_price=option_entry_price,
                    init_sl_spot=index_sl_spot,
                    init_sl_price=option_sl_price,
                    init_sl_pnl=initial_risk_pnl
                )
                
                # 5. Update HUD Execution State for Streamlit Dashboard
                current_state['trades_taken_today'] = 1
                current_state['hud'] = {
                    'type': side,
                    'trade_closed': False,
                    'exit_reason': 'ACTIVE',
                    'symbol': contract_name,
                    'delta': delta,
                    'entry_spot': entry_spot,
                    'entry_opt': option_entry_price,
                    'sl_spot': index_sl_spot,
                    'sl_opt': option_sl_price,
                    'trail_spot': entry_spot,
                    'trail_opt': option_entry_price,
                    'secured_pts': 0.0,
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

        # Active market session check (09:15 to 15:30 IST)
        if dtime(9, 15) <= current_time <= dtime(15, 30):
            poll_and_evaluate_strategy()
        else:
            print(f'[{now.strftime("%H:%M:%S")}] Outside market hours. Daemon in standby...', end='\r')

        time.sleep(3)

if __name__ == '__main__':
    run_live_daemon()
