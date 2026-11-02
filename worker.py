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
  print(f'[ERROR] Model files not found! Details: {e}')

def send_telegram_alert(message):
  """Sends an instant alert message via Telegram."""
  try:
    # Add your Telegram Bot Token and Chat ID if configured
    token = "YOUR_BOT_TOKEN"
    chat_id = "YOUR_CHAT_ID"
    if token != "YOUR_BOT_TOKEN":
      url = f"https://api.telegram.org/bot{token}/sendMessage"
      payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
      requests.post(url, json=payload, timeout=5)
  except Exception as e:
    print(f"[ALERT ERROR] Failed to send Telegram alert: {e}")

def poll_live_market():
  """Polls live market feed, checks Nifty spot, and evaluates breakout triggers."""
  try:
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f"[{now_str}] Polling live Nifty spot & candles...")
    
    # Add your live spot fetching logic or broker LTP API integration here
    # Example state update or alert trigger when condition is met:
    # if breakout_condition_met:
    #     send_telegram_alert("🚨 *NIFTY ORB Breakout Triggered!*")
      
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
