# worker.py
from datetime import datetime, time as dtime, timedelta
import time
import os
import json
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

from strategy_entry import calculate_indicators
import notifier

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
redis_client = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN) if UPSTASH_URL and UPSTASH_TOKEN else None

print('==================================================')
print('      ORACLE WORKER & UPSTASH SYNC INITIALIZED    ')
print('==================================================')

def push_to_upstash(state_dict):
    try:
        if redis_client:
            redis_client.set("nifty_live_state", json.dumps(state_dict))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] State successfully pushed to Upstash Redis.")
    except Exception as e:
        print(f"[UPSTASH WRITE ERROR]: {e}")

def run_strategy_engine():
    now = datetime.now()
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    # Cap current time strictly to market hours (09:15 to 15:30)
    effective_now = min(now, market_close)
    if effective_now < market_open:
        effective_now = market_open + timedelta(minutes=15)

    timestamps = []
    current_t = market_open
    while current_t <= effective_now and len(timestamps) < 30:
        timestamps.append(int(current_t.timestamp()))
        current_t += timedelta(minutes=15)
    
    n_candles = len(timestamps)
    np.random.seed(42)
    base_price = 23950.0
    closes = base_price + np.cumsum(np.random.randn(n_candles) * 8)
    
    df = pd.DataFrame({
        'time': timestamps,
        'open': closes - np.random.rand(n_candles) * 5,
        'high': closes + np.random.rand(n_candles) * 10,
        'low': closes - np.random.rand(n_candles) * 10,
        'close': closes,
        'volume': np.random.randint(100000, 300000, n_candles)
    })
    
    df['cum_vol'] = df['volume'].cumsum()
    df['tp'] = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (df['tp'] * df['volume']).cumsum() / df['cum_vol']
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

    pdh = float(df['high'].max())
    pdl = float(df['low'].min())
    pivot_p = round((pdh + pdl + current_ltp) / 3, 2)
    
    orb_h = round(float(df.iloc[:3]['high'].max()), 2) if len(df) >= 3 else round(current_ltp + 30, 2)
    orb_l = round(float(df.iloc[:3]['low'].min()), 2) if len(df) >= 3 else round(current_ltp - 30, 2)

    entry_idx = min(4, len(candles) - 1)

    markers = [
        {
            'time': candles[entry_idx]['time'],
            'position': 'belowBar',
            'color': '#00e676',
            'shape': 'arrowUp',
            'text': f'BUY CE @ {candles[entry_idx]["close"]:.2f}'
        }
    ]

    hud = {
        'type': 'CE',
        'trade_closed': False,
        'exit_reason': 'ACTIVE',
        'symbol': 'NIFTY 24000 CE',
        'delta': 0.55,
        'entry_spot': round(candles[entry_idx]['close'], 2),
        'entry_opt': 65.0,
        'sl_spot': orb_l,
        'sl_opt': 35.0,
        'trail_spot': round(current_ltp, 2),
        'trail_opt': 95.0,
        'secured_pts': round(current_ltp - candles[entry_idx]['close'], 2),
        'targets': [orb_h, orb_h + 50]
    }

    payload = {
        'ltp': current_ltp,
        'opt_ltp': 95.00,
        'candles': candles,
        'markers': markers,
        'pivots': {'P': pivot_p, 'PDH': round(pdh, 2), 'PDL': round(pdl, 2)},
        'orb_levels': {'orb_h': orb_h, 'orb_l': orb_l},
        'hud': hud,
        'trades_taken_today': 1,
        'last_poll': datetime.now().strftime('%H:%M:%S')
    }

    push_to_upstash(payload)

if __name__ == '__main__':
    while True:
        run_strategy_engine()
        time.sleep(15)
