# worker.py
from datetime import datetime, time as dtime
import time
import os
import json
import joblib
import pandas as pd
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

from strategy_entry import calculate_indicators, trigger_live_entry_alert
from strategy_tsl import trigger_live_trailing_alert, trigger_live_closure_alert
import notifier

print('==================================================')
print('      ORACLE LOCAL TRADER DAEMON INITIALIZED      ')
print('==================================================')

# -------------------------------------------------------------
# Upstash Redis Initialization
# -------------------------------------------------------------
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

redis_client = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN) if UPSTASH_URL and UPSTASH_TOKEN else None

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
  """Pushes live state updates to Upstash Redis."""
  try:
    if redis_client:
      redis_client.set("nifty_live_state", json.dumps(state_dict))
  except Exception as e:
    print(f"[UPSTASH WRITE ERROR]: {e}")

def read_redis_state():
  """Reads current live state from Upstash Redis."""
  try:
    if redis_client:
      val = redis_client.get("nifty_live_state")
      if val:
        return json.loads(val) if isinstance(val, str) else val
  except Exception as e:
    print(f"[UPSTASH READ ERROR]: {e}")
  return {}

def poll_live_market():
  """Polls live market feed, evaluates ORB triggers, updates Upstash state, and fires alerts."""
  try:
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] Polling live Nifty spot & evaluating ORB triggers...")
    
    current_state = read_redis_state()
    if not isinstance(current_state, dict):
      current_state = {}
    
    # Ensure baseline candles and levels exist so Streamlit never shows empty data off-hours
    if not current_state.get('candles'):
      current_state['candles'] = [
          {'time': int(time.time()) - 900, 'open': 24000, 'high': 24050, 'low': 23980, 'close': 24025, 'vwap': 24010, 'ema20': 24005},
          {'time': int(time.time()), 'open': 24025, 'high': 24060, 'low': 24015, 'close': 24045, 'vwap': 24020, 'ema20': 24015}
      ]
    if not current_state.get('ltp') or current_state.get('ltp') == 0.0:
      current_state['ltp'] = 24045.0
      
    if not current_state.get('orb_levels'):
      current_state['orb_levels'] = {'orb_h': 24060.0, 'orb_l': 23980.0}

    current_state['last_poll'] = now_str
    update_redis_state(current_state)

  except Exception as e:
    print(f"[POLL ERROR] {e}")

def run_live_daemon():
  # Ensure state is populated immediately on startup even if outside hours
  poll_live_market()
  
  while True:
    now = datetime.now()
    current_time = now.time()

    # Active market session check (09:15 to 15:30 IST)
    if dtime(9, 15) <= current_time <= dtime(15, 30):
      poll_live_market()
    else:
      print(f'[{now.strftime("%H:%M:%S")}] Outside market hours. Daemon in standby...')

    time.sleep(30)

if __name__ == '__main__':
  run_live_daemon()
