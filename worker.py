# worker.py
from datetime import datetime, time as dtime
import time
import os
import json
import joblib
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

from strategy_entry import calculate_indicators, trigger_live_entry_alert
from strategy_tsl import trigger_live_trailing_alert, trigger_live_closure_alert
import notifier

print('==================================================')
print('      ORACLE LIVE TRADER DAEMON INITIALIZED       ')
print('==================================================')

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

try:
  model = joblib.load('trained_orb_model.pkl')
  cutoff = joblib.load('model_cutoff.pkl')
  print('[INIT] Loaded trained model and cutoff successfully.')
except Exception as e:
  print(f'[INIT WARNING] Model files not loaded yet: {e}')

def update_redis_state(state_dict):
  """Pushes live state updates to Upstash Redis for Streamlit dashboard rendering."""
  if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
    return
  url = f"{UPSTASH_REDIS_REST_URL}/set/nifty_live_state"
  headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
  try:
    requests.post(url, headers=headers, data=json.dumps(json.dumps(state_dict)), timeout=3)
  except Exception as e:
    print(f"[REDIS ERROR] {e}")

def read_redis_state():
  """Reads current live state from Upstash Redis."""
  if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
    return {}
  url = f"{UPSTASH_REDIS_REST_URL}/get/nifty_live_state"
  headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
  try:
    r = requests.get(url, headers=headers, timeout=3)
    if r.status_code == 200:
      val = r.json().get("result")
      if val:
        return json.loads(val)
  except Exception:
    pass
  return {}

def poll_live_market():
  """Polls live market feed, updates LTP, evaluates ORB triggers, and fires alerts."""
  try:
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] Polling live Nifty spot & evaluating ORB triggers...")
    
    # Fetch live Nifty Spot & Option LTP (integrate your broker LTP fetch or Upstox feed here)
    # Example live state update structure for Streamlit & Redis:
    current_state = read_redis_state()
    
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
