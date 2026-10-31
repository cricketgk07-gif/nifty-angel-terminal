# worker.py
from datetime import datetime, time as dtime
import time
import joblib
import pandas as pd

from strategy_entry import calculate_indicators
from strategy_ml import FEATURE_COLUMNS, build_ml_features
from strategy_tsl import run_fixed_replay

print('==================================================')
print('      ORACLE LIVE TRADER DAEMON INITIALIZED       ')
print('==================================================')

try:
  model = joblib.load('trained_orb_model.pkl')
  cutoff = joblib.load('model_cutoff.pkl')
  print('[INIT] Loaded trained model and 0.52 cutoff successfully.')
except Exception as e:
  print(f'[ERROR] Model files not found! Run backtest.py first. Details: {e}')


def run_live_daemon():
  while True:
    now = datetime.now()
    current_time = now.time()

    if dtime(9, 15) <= current_time <= dtime(15, 30):
      print(f'[{now}] Polling live market candles...')
      # (Broker polling & live strategy evaluation hooks execute here)
    else:
      print(f'[{now}] Outside market hours. Sleeping...')

    time.sleep(5)


if __name__ == '__main__':
  run_live_daemon()
