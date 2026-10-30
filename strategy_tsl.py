# strategy_tsl.py
from datetime import time as dtime
import numpy as np
import pandas as pd


def run_fixed_replay(cand_df, raw_arrays):
  """Simulates trade execution candle-by-candle with initial stops and 35/25/12 trailing stages."""
  replayed = []
  for r in cand_df.itertuples(index=False):
    day = str(r.day)[:10]
    et = pd.Timestamp(r.entry_time).to_datetime64()
    side = r.side
    entry = float(r.entry_price)
    data = raw_arrays.get(day)

    if data is None:
      replayed.append(0.0)
      continue

    dt, o, h, l, c = data
    idx = np.where(dt >= et)[0]
    idx = idx[
        np.array([pd.Timestamp(x).time() <= dtime(15, 5) for x in dt[idx]])
    ]
    if len(idx) == 0:
      replayed.append(0.0)
      continue

    dt_sub, o_sub, h_sub, l_sub, c_sub = dt[idx], o[idx], h[idx], l[idx], c[idx]

    ce = side == 'CE'
    initial_stop = entry - 40.0 if ce else entry + 40.0
    stop = initial_stop
    best_val = entry
    exit_price = c_sub[-1]

    for i in range(len(o_sub)):
      oi, hi, lo, cl = o_sub[i], h_sub[i], l_sub[i], c_sub[i]

      if ce and lo <= stop:
        exit_price = oi if oi < stop else stop
        break
      if not ce and hi >= stop:
        exit_price = oi if oi > stop else stop
        break

      favorable_close = (cl - entry) if ce else (entry - cl)
      favorable_high = (hi - entry) if ce else (entry - lo)

      if ce:
        best_val = max(best_val, cl, hi)
      else:
        best_val = min(best_val, cl, lo)

      if favorable_high >= 150.0 or favorable_close >= 150.0:
        stage3_stop = (best_val - 12.0) if ce else (best_val + 12.0)
        stop = max(stop, stage3_stop) if ce else min(stop, stage3_stop)
      elif favorable_high >= 90.0 or favorable_close >= 90.0:
        stage2_stop = (best_val - 25.0) if ce else (best_val + 25.0)
        stop = max(stop, stage2_stop) if ce else min(stop, stage2_stop)
      elif favorable_high >= 50.0 or favorable_close >= 50.0:
        buffer_stop = (entry + 35.0) if ce else (entry - 35.0)
        stop = max(stop, buffer_stop) if ce else min(stop, buffer_stop)

    pts = (exit_price - entry) if ce else (entry - exit_price)
    replayed.append(pts)

  return replayed