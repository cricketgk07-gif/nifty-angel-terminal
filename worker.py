# worker.py
from datetime import datetime, time as dtime
import time
import os
import json
import joblib
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

from strategy_entry import calculate_indicators, extract_entry_candidates, trigger_live_entry_alert
from strategy_ml import build_ml_features, FEATURE_COLUMNS
from strategy_tsl import run_detailed_replay, trigger_live_trailing_alert, trigger_live_closure_alert
import notifier

# Connect to Upstash Redis cloud storage
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
redis_client = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN) if UPSTASH_URL and UPSTASH_TOKEN else None

print('==================================================')
print('   ORACLE CLOUD SYNC TRADER DAEMON (3s LOOP)      ')
print('==================================================')

# Load trained ML model and cutoff threshold
ml_model = None
ml_cutoff = 0.52
try:
  if os.path.exists('trained_orb_model.pkl') and os.path.exists('model_cutoff.pkl'):
    ml_model = joblib.load('trained_orb_model.pkl')
    ml_cutoff = joblib.load('model_cutoff.pkl')
    print(f'[INIT] Loaded trained ML model and cutoff threshold successfully: {ml_cutoff}')
  else:
    print('[INIT WARNING] Model files (.pkl) not found in root directory!')
except Exception as e:
  print(f'[INIT ERROR] Failed to load model files: {e}')

def push_to_upstash(state_dict):
  """Pushes live state updates directly to Upstash Redis cloud cache for Streamlit."""
  try:
    if redis_client:
      redis_client.set("nifty_live_state", json.dumps(state_dict))
  except Exception as e:
    print(f"[UPSTASH WRITE ERROR]: {e}")

def read_from_upstash():
  """Reads current live state from Upstash Redis cloud cache."""
  try:
    if redis_client:
      val = redis_client.get("nifty_live_state")
      if val:
        return json.loads(val) if isinstance(val, str) else val
  except Exception:
    pass
  return {}

def fetch_live_candles_dataframe():
  """
  Hook for pulling live 5-minute candles. 
  In production, integrate your Angel One SmartAPI candle fetch code here.
  """
  now = datetime.now()
  market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
  
  timestamps = []
  curr = market_open
  while curr <= now and len(timestamps) < 35:
    timestamps.append(curr)
    curr += pd.Timedelta(minutes=5)
    
  if not timestamps:
    timestamps = [now - pd.Timedelta(minutes=5*i) for i in range(15)][::-1]
    
  n = len(timestamps)
  np.random.seed(42)
  closes = 24000.0 + np.cumsum(np.random.randn(n) * 6)
  
  df = pd.DataFrame({
      'dt': timestamps,
      'open': closes - np.random.rand(n) * 4,
      'high': closes + np.random.rand(n) * 8,
      'low': closes - np.random.rand(n) * 8,
      'close': closes,
      'volume': np.random.randint(50000, 150000, n)
  })
  return df

def poll_and_evaluate_strategy():
  """Core high-speed evaluation loop: Indicators -> Entry -> ML -> Notifier -> Upstash Sync"""
  try:
    now_str = datetime.now().strftime('%H:%M:%S')
    
    # 1. Fetch candles & calculate indicators (ATR14, EMA20)[cite: 11]
    df = fetch_live_candles_dataframe()
    df = calculate_indicators(df)
    
    if len(df) < 6:
      return

    day_key = df['dt'].dt.strftime('%Y-%m-%d').iloc[-1]
    byday = {day_key: df.reset_index(drop=True)}

    # 2. Extract Entry Candidates (Validates ORB range, confirmation candle, body fraction >= 0.12)[cite: 11]
    candidates = extract_entry_candidates(byday)
    
    current_state = read_from_upstash()
    if not isinstance(current_state, dict):
      current_state = {}

    # Format candle payload for Streamlit Lightweight Charts frontend JSON
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

    # 3. Evaluate Machine Learning Model Gatekeeper (.pkl)[cite: 10, 13]
    trade_triggered = False
    if candidates and ml_model is not None:
      cand_df = pd.DataFrame(candidates)
      cand_df = build_ml_features(cand_df)
      X = cand_df[FEATURE_COLUMNS].copy()
      X['side'] = X['side'].map({'CE': 1, 'PE': -1})
      
      predictions = ml_model.predict(X)
      cand_df['predicted_expected_points'] = predictions

      # Check if prediction clears model cutoff threshold[cite: 10, 13]
      valid_signals = cand_df[cand_df['predicted_expected_points'] > ml_cutoff]

      if not valid_signals.empty and current_state.get('trades_taken_today', 0) == 0:
        sig = valid_signals.iloc[0]
        side = sig['side']
        entry_spot = float(sig['entry_price'])
        
        print(f"\n[{now_str}] [SIGNAL CONFIRMED] Valid {side} breakout detected! ML Score: {sig['predicted_expected_points']:.2f}")
        
        # 4. Trigger Instant Telegram Alert via Notifier
        contract_name = f"NIFTY 24000 {side}"
        trigger_live_entry_alert(
            side=side,
            contract=contract_name,
            qty=25,
            delta=0.55,
            entry_spot=entry_spot,
            entry_price=95.0,
            init_sl_spot=current_state['orb_levels']['orb_l'] if side == 'CE' else current_state['orb_levels']['orb_h'],
            init_sl_price=55.0,
            init_sl_pnl=-1000.0
        )
        
        # 5. Update HUD Execution State for Streamlit Dashboard
        current_state['trades_taken_today'] = 1
        current_state['hud'] = {
            'type': side,
            'trade_closed': False,
            'exit_reason': 'ACTIVE',
            'symbol': contract_name,
            'delta': 0.55,
            'entry_spot': entry_spot,
            'entry_opt': 95.0,
            'sl_spot': current_state['orb_levels']['orb_l'],
            'sl_opt': 55.0,
            'trail_spot': entry_spot,
            'trail_opt': 95.0,
            'secured_pts': 0.0,
            'targets': [entry_spot + 50, entry_spot + 100]
        }
        current_state['markers'] = [{
            'time': int(pd.Timestamp(sig['signal_time']).timestamp()),
            'position': 'belowBar' if side == 'CE' else 'aboveBar',
            'color': '#00e676' if side == 'CE' else '#ff5252',
            'shape': 'arrowUp' if side == 'CE' else 'arrowDown',
            'text': f'{side} BUY @ {entry_spot:.2f}'
        }]
        trade_triggered = True

    current_state['last_poll'] = now_str
    push_to_upstash(current_state)

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

    # High-speed polling loop set to 3 seconds for near-zero delay reactivity
    time.sleep(3)

if __name__ == '__main__':
  run_live_daemon()
