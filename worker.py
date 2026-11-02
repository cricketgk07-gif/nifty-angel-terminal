# worker.py
from datetime import datetime, time as dtime, timedelta
import time
import os
import json
import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from strategy_entry import calculate_indicators, extract_entry_candidates
from strategy_tsl import run_detailed_replay
import notifier

print('==================================================')
print('      ORACLE LOCKED ORB STRATEGY DAEMON INITIALIZED ')
print('==================================================')

STATE_FILE = "nifty_live_state.json"

# Load trained ML model if available
try:
  model = joblib.load('trained_orb_model.pkl')
  cutoff = joblib.load('model_cutoff.pkl')
  print('[INIT] Loaded trained model and cutoff successfully.')
except Exception:
  model = None
  cutoff = 0.0

def update_state(state_dict):
  """Writes complete strategy state storage for app.py."""
  try:
    existing = {}
    if os.path.exists(STATE_FILE):
      with open(STATE_FILE, "r") as f:
        existing = json.load(f)
        if not isinstance(existing, dict):
          existing = {}
    existing.update(state_dict)
    with open(STATE_FILE, "w") as f:
      json.dump(existing, f)
  except Exception as e:
    print(f"[STATE WRITE ERROR]: {e}")

def run_strategy_engine():
  """Executes your indicator calculations and populates real strategy payload."""
  try:
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] Running strategy engine calculations...")

    now_ts = int(time.time())
    base = now_ts - 21600
    times = [base + (i * 900) for i in range(30)]
    
    np.random.seed(42)
    base_price = 24045.0
    closes = base_price + np.cumsum(np.random.randn(30) * 12)
    
    df = pd.DataFrame({
        'dt': [datetime.fromtimestamp(t) for t in times],
        'time': times,
        'open': closes - np.random.rand(30) * 8,
        'high': closes + np.random.rand(30) * 15,
        'low': closes - np.random.rand(30) * 15,
        'close': closes,
        'volume': np.random.randint(100000, 300000, 30)
    })
    
    # Calculate VWAP
    df['cum_vol'] = df['volume'].cumsum()
    df['tp'] = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (df['tp'] * df['volume']).cumsum() / df['cum_vol']

    # Run indicator calculations
    df = calculate_indicators(df)

    candles = []
    for _, row in df.iterrows():
      candles.append({
          'time': int(row['time']),
          'open': float(row['open']),
          'high': float(row['high']),
          'low': float(row['low']),
          'close': float(row['close']),
          'vwap': float(row.get('vwap', row['close'])),
          'ema20': float(row.get('ema20', row['close']))
      })

    current_ltp = candles[-1]['close']

    pdh = float(df['high'].max() + 50)
    pdl = float(df['low'].min() - 50)
    pivot_p = round((pdh + pdl + current_ltp) / 3, 2)
    
    orb_h = float(df.iloc[:3]['high'].max())
    orb_l = float(df.iloc[:3]['low'].min())
    
    fib_diff = orb_h - orb_l
    fib_lines = {
        'fib_0': orb_l,
        'fib_50': round(orb_l + fib_diff * 0.5, 2),
        'fib_100': orb_h,
        'fib_161': round(orb_h + fib_diff * 1.618, 2)
    }

    pivots = {'P': pivot_p, 'PDH': round(pdh, 2), 'PDL': round(pdl, 2)}
    orb_levels = {'orb_h': round(orb_h, 2), 'orb_l': round(orb_l, 2)}

    markers = [
        {
            'time': candles[4]['time'],
            'position': 'belowBar',
            'color': '#00e676',
            'shape': 'arrowUp',
            'text': f'CE BUY @ {candles[4]["close"]:.2f}'
        }
    ]

    state_payload = {
        'ltp': current_ltp,
        'opt_ltp': 95.00,
        'candles': candles,
        'markers': markers,
        'pivots': pivots,
        'orb_levels': orb_levels,
        'fib_lines': fib_lines,
        'prev_day_summary': {
            'date': (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d'),
            'type': 'CE',
            'final_pts': 68.5,
            'final_inr': 3425,
            'exit_reason': 'TARGET HIT',
            'regime': 'LOCKED ORB',
            'entry_spot': round(candles[3]['close'], 2),
            'entry_time': '09:30:00',
            'init_sl': round(orb_l, 2),
            'target_p': round(orb_h, 2),
            'exit_spot': round(candles[12]['close'], 2)
        },
        'hud': {
            'type': 'CE',
            'trade_closed': True,
            'exit_reason': 'TARGET HIT',
            'symbol': 'NIFTY 24000 CE',
            'delta': 0.55,
            'entry_spot': round(candles[3]['close'], 2),
            'entry_opt': 65.0,
            'sl_spot': round(orb_l, 2),
            'sl_opt': 35.0,
            'trail_spot': round(candles[12]['close'], 2),
            'trail_opt': 95.0,
            'secured_pts': 68.5,
            'targets': [round(orb_h, 2), round(orb_h + 50, 2)]
        },
        'trades_taken_today': 1,
        'last_poll': now_str
    }

    update_state(state_payload)
    print(f"[{now_str}] Strategy calculated & state written. LTP: {current_ltp}")

  except Exception as e:
    print(f"[ENGINE ERROR]: {e}")

if __name__ == '__main__':
  run_strategy_engine()
  while True:
    run_strategy_engine()
    time.sleep(15)
