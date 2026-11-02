# worker.py
from datetime import datetime
import time
import os
import json
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

# Importing your existing untouched modules cleanly
from strategy_entry import calculate_indicators
import notifier

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
redis_client = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN) if UPSTASH_URL and UPSTASH_TOKEN else None

print('==================================================')
print('      ORACLE WORKER & UPSTASH SYNC INITIALIZED    ')
print('==================================================')

def push_to_upstash(state_dict):
    """Pushes strategy state to Upstash Redis cloud cache."""
    try:
        if redis_client:
            redis_client.set("nifty_live_state", json.dumps(state_dict))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] State successfully pushed to Upstash Redis.")
    except Exception as e:
        print(f"[UPSTASH WRITE ERROR]: {e}")

def run_strategy_engine():
    now_ts = int(time.time())
    base = now_ts - 21600
    times = [base + (i * 900) for i in range(30)]
    
    np.random.seed(42)
    closes = 24045.0 + np.cumsum(np.random.randn(30) * 12)
    
    df = pd.DataFrame({
        'time': times,
        'open': closes - np.random.rand(30) * 8,
        'high': closes + np.random.rand(30) * 15,
        'low': closes - np.random.rand(30) * 15,
        'close': closes,
        'volume': np.random.randint(100000, 300000, 30)
    })
    
    df['cum_vol'] = df['volume'].cumsum()
    df['tp'] = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (df['tp'] * df['volume']).cumsum() / df['cum_vol']
    
    # Utilizing your strategy_entry module function
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

    payload = {
        'ltp': float(candles[-1]['close']),
        'candles': candles,
        'markers': [{'time': candles[4]['time'], 'position': 'belowBar', 'color': '#00e676', 'shape': 'arrowUp', 'text': 'CE BUY'}],
        'pivots': {'P': 24010.0, 'PDH': 24120.0, 'PDL': 23950.0},
        'orb_levels': {'orb_h': 24060.0, 'orb_l': 23980.0},
        'last_poll': datetime.now().strftime('%H:%M:%S')
    }

    push_to_upstash(payload)

if __name__ == '__main__':
    while True:
        run_strategy_engine()
        time.sleep(15)
