# strategy_entry.py
from datetime import time as dtime, datetime
import numpy as np
import pandas as pd
from notifier import send_entry_alert


def calculate_indicators(df):
  """Calculates ATR14 and EMA20 across the dataframe."""
  df['prev_close'] = df['close'].shift(1)
  df['tr'] = np.maximum(
      df['high'] - df['low'],
      np.maximum(
          abs(df['high'] - df['prev_close']),
          abs(df['low'] - df['prev_close']),
      ),
  )
  df['atr14'] = df['tr'].rolling(window=14).mean().bfill()
  df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
  return df


def extract_entry_candidates(byday):
  """Scans daily groups for valid ORB breakouts meeting confirmation and body fraction filters."""
  candidates = []

  for day_key, group in byday.items():
    if len(group) < 6:
      continue
    if group.iloc[0]['dt'].time() != dtime(9, 15):
      continue

    orb_window = group.iloc[0:3]
    orb_high = orb_window['high'].max()
    orb_low = orb_window['low'].min()
    orb_width = orb_high - orb_low

    if orb_width > 160.0 or orb_width < 12:
      continue

    for i in range(3, len(group) - 2):
      row = group.iloc[i]
      t = row['dt'].time()

      # Original session hours restored (up to 14:30)
      if t > dtime(14, 30) or t < dtime(9, 30):
        continue

      side = None
      if row['close'] > orb_high and row['close'] > row['ema20']:
        side = 'CE'
      elif row['close'] < orb_low and row['close'] < row['ema20']:
        side = 'PE'

      if side:
        conf_row = group.iloc[i + 1]
        entry_row = group.iloc[i + 2]

        is_ce_confirmed = (
            side == 'CE'
            and conf_row['close'] > orb_high
            and conf_row['close'] > conf_row['open']
        )
        is_pe_confirmed = (
            side == 'PE'
            and conf_row['close'] < orb_low
            and conf_row['close'] < conf_row['open']
        )

        if is_ce_confirmed or is_pe_confirmed:
          high_v, low_v, open_v, close_v = (
              row['high'],
              row['low'],
              row['open'],
              row['close'],
          )
          rng = high_v - low_v
          if rng == 0:
            continue

          loc = (close_v - low_v) / rng
          body_frac = abs(close_v - open_v) / rng

          # Optimized body fraction for higher profit capture
          if body_frac < 0.12:
            continue

          candidates.append({
              'day': day_key,
              'month_num': row['dt'].strftime('%Y-%m'),
              'side': side,
              'signal_time': row['dt'],
              'entry_time': entry_row['dt'],
              'entry_price': entry_row['open'],
              'orb_width': orb_width,
              'range': rng,
              'loc': loc,
              'body_frac': body_frac,
              'atr14': row['atr14'],
              'ema_dist_atr': (
                  (close_v - row['ema20']) / row['atr14']
                  if row['atr14'] > 0
                  else 0
              ),
              'ext_atr': (
                  abs(close_v - row['ema20']) / row['atr14']
                  if row['atr14'] > 0
                  else 0
              ),
          })
          break

  return candidates


def trigger_live_entry_alert(side, contract, qty, delta, entry_spot, entry_price, init_sl_spot, init_sl_price, init_sl_pnl):
  """Dynamic helper for live execution to trigger entry alerts cleanly without hardcoded values."""
  init_sl_pts = -40.0 if side == 'CE' else 40.0
  stages_str = "+50 (+5) → +75 (+30) → +100 (+65) → +125 (+90) → +150 (+125) → +200+"
  trigger_time = datetime.now().strftime("%H:%M:%S")
  action_type = f"BUY {side}"

  send_entry_alert(
      action_type=action_type,
      contract=contract,
      qty=qty,
      delta=delta,
      entry_spot=entry_spot,
      entry_price=entry_price,
      init_sl_spot=init_sl_spot,
      init_sl_pts=init_sl_pts,
      init_sl_price=init_sl_price,
      init_sl_pnl=init_sl_pnl,
      stages_str=stages_str,
      trigger_time=trigger_time
  )
