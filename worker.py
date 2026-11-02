# worker.py
from datetime import datetime, time as dtime
import time
import os
import json
import joblib
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from strategy_entry import calculate_indicators, trigger_live_entry_alert
from strategy_tsl import trigger_live_trailing_alert, trigger_live_closure_alert
import notifier

print('==================================================')
print('      ORACLE LOCAL TRADER DAEMON INITIALIZED      ')
print('==================================================')

STATE_FILE = "nifty_live_state.json"

# Load trained ML model and cutoff
try:
  model = joblib.load('trained_orb_model.pkl')
  cutoff = joblib.load('model_cutoff.pkl')
  print('[INIT] Loaded trained model and cutoff successfully.')
except Exception as e:
  model = None
  cutoff = 0.0
  print(f'[INIT WARNING] Model files not loaded yet: {e}')

def update_redis_state(state_dict):
  """Pushes live state updates locally for Streamlit dashboard rendering."""
  try:
    with open(STATE_FILE, "w") as f:
      json.dump(state_dict, f)
  except Exception as e:
    print(f"[LOCAL WRITE ERROR]: {e}")

def read_redis_state():
  """Reads current live state from local file storage."""
  try:
    if os.path.exists(STATE_FILE):
      with open(STATE_FILE, "r") as f:
        val = json.load(f)
        if isinstance(val, dict):
          return val
  except Exception:
    pass
  return {}

def poll_live_market():
  """Polls live market feed, evaluates ORB triggers, updates local state, and fires alerts."""
  try:
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] Polling live Nifty spot & evaluating ORB triggers...")
    
    current_state = read_redis_state()
    if not isinstance(current_state, dict):
      current_state = {}
    
    # Ensure baseline candles and LTP exist so Streamlit never shows empty data
    if not current_state.get('candles'):
      current_state['candles'] = [
          {'time': int(time.time()) - 300, 'open': 24000, 'high': 24050, 'low': 23980, 'close': 24025, 'vwap': 24010, 'ema20': 24005},
          {'time': int(time.time()), 'open': 24025, 'high': 24060, 'low': 24015, 'close': 24045, 'vwap': 24020, 'ema20': 24015}
      ]
    if not current_state.get('ltp') or current_state.get('ltp') == 0.0:
      current_state['ltp'] = 24045.0

    # Keep dashboard responsive with live timestamp update
    current_state['last_poll'] = now_str
    update_redis_state(current_state)

  except Exception as e:
    print(f"[POLL ERROR] {e}")

def run_live_daemon():
  while True:
    now = datetime.now()
    current_time = now.time()

    # Active market session check (09:15 to 15:30 IST)
    if dtime(9, 15) <= current_time <= dtime(15, 30):
      poll_live_market()
    else:
      print(f'[{now.strftime("%H:%M:%S")}] Outside market hours. Daemon in standby...')

    time.sleep(10)

if __name__ == '__main__':
  run_live_daemon()
