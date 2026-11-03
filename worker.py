# worker.py
from datetime import datetime, time as dtime, timedelta
import time
import os
import json
import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

from strategy_entry import calculate_indicators, extract_entry_candidates, trigger_live_entry_alert
from strategy_ml import build_ml_features
from strategy_tsl import run_detailed_replay, trigger_live_trailing_alert, trigger_live_closure_alert
import notifier

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
redis_client = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN) if UPSTASH_URL and UPSTASH_TOKEN else None

print('==================================================')
print('   ORACLE LIVE PRODUCTION WORKER INITIALIZED      ')
print('==================================================')

# Load trained ML model and cutoff if available
ml_model = None
ml_cutoff = 0.0
try:
    if os.path.exists('trained_orb_model.pkl') and os.path.exists('model_cutoff.pkl'):
        ml_model = joblib.load('trained_orb_model.pkl')
        ml_cutoff = joblib.load('model_cutoff.pkl')
        print(f"[ML] Successfully loaded trained model and cutoff: {ml_cutoff}")
    else:
        print("[ML WARNING] Model .pkl files not found in root directory!")
except Exception as e:
    print(f"[ML ERROR]: {e}")

def push_to_upstash(state_dict):
    try:
        if redis_client:
            redis_client.set("nifty_live_state", json.dumps(state_dict))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Live state successfully pushed to Upstash Redis.")
    except Exception as e:
        print(f"[UPSTASH WRITE ERROR]: {e}")

def run_live_strategy():
    # In a live production environment, you would pull today's real 5m OHLC bars from your broker (e.g., Angel One).
    # For now, let's ensure it hooks into your strategy extraction and ML filter pipeline seamlessly.
    
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    
    # Placeholder payload structure synced to Redis so Streamlit renders instantly
    payload = {
        'ltp': 24045.0,
        'opt_ltp': 95.00,
        'candles': [],
        'markers': [],
        'pivots': {'P': 24010.0, 'PDH': 24120.0, 'PDL': 23950.0},
        'orb_levels': {'orb_h': 24060.0, 'orb_l': 23980.0},
        'hud': {
            'type': None,
            'trade_closed': False,
            'exit_reason': 'STANDBY',
            'symbol': 'NIFTY 24000 CE',
            'delta': 0.55,
            'entry_spot': 0.0,
            'entry_opt': 0.0,
            'sl_spot': 0.0,
            'sl_opt': 0.0,
            'secured_pts': 0.0,
            'targets': []
        },
        'trades_taken_today': 0,
        'last_poll': now.strftime('%H:%M:%S')
    }

    push_to_upstash(payload)

if __name__ == '__main__':
    while True:
        try:
            run_live_strategy()
        except Exception as e:
            print(f"[WORKER ERROR]: {e}")
        time.sleep(15)
