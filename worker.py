# worker.py
from datetime import datetime, time as dtime
import time
import joblib
import pandas as pd
import requests

from strategy_entry import calculate_indicators
from strategy_ml import FEATURE_COLUMNS, build_ml_features
from strategy_tsl import run_fixed_replay

print('==================================================')
print('      ORACLE LIVE TRADER DAEMON INITIALIZED       ')
print('==================================================')

try:
  model = joblib.load('trained_orb_model.pkl')
  cutoff = joblib.load('model_cutoff.pkl')
  print('[INIT] Loaded trained model and cutoff successfully.')
except Exception as e:
  print(f'[ERROR] Model files not found! Run backtest.py first. Details: {e}')


def poll_live_market():
  """Polls live market feed or broker API for Nifty spot updates."""
  try:
    # Example public spot fetch or broker LTP hook
    url = "https://www.nseindia.com" # Or your broker LTP endpoint
    headers = {'User-Agent': 'Mozilla/5.0'}
    # Replace with your active broker LTP fetch function or API call
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Polling live Nifty spot & candles...")
  except Exception as e:
    print(f"[POLL ERROR] {e}")


def run_live_daemon():
  while True:
    now = datetime.now()
    current_time = now.time()

    # Market hours check (09:15 to 15:30)
    if dtime(9, 15) <= current_time <= dtime(15, 30):
      poll_live_market()
    else:
      print(f'[{now.strftime("%H:%M:%S")}] Outside market hours. Sleeping...')

    time.sleep(60)


if __name__ == '__main__':
  run_live_daemon()
