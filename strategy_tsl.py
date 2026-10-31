# strategy_tsl.py
from datetime import time as dtime, datetime
import numpy as np
import pandas as pd
from notifier import send_trailing_sl_alert, send_trade_closed_alert


def run_fixed_replay(cand_df, raw_arrays):
  """Simulates trade execution and locks exact fixed profit points upon stop hit."""
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
    
    locked_pts = -40.0 
    exit_triggered = False

    for i in range(len(o_sub)):
      oi, hi, lo, cl = o_sub[i], h_sub[i], l_sub[i], c_sub[i]

      if ce and lo <= stop:
        exit_triggered = True
        break
      if not ce and hi >= stop:
        exit_triggered = True
        break

      favorable_high = (hi - entry) if ce else (entry - lo)
      favorable_close = (cl - entry) if ce else (entry - cl)
      max_fav = max(favorable_high, favorable_close)

      if max_fav >= 200.0:
        excess_blocks = int((max_fav - 200.0) // 25.0)
        milestone = 200.0 + (excess_blocks * 25.0)
        tier_profit = milestone - 15.0
      elif max_fav >= 175.0:
        tier_profit = 150.0
      elif max_fav >= 150.0:
        tier_profit = 125.0
      elif max_fav >= 125.0:
        tier_profit = 90.0
      elif max_fav >= 100.0:
        tier_profit = 65.0
      elif max_fav >= 75.0:
        tier_profit = 30.0
      elif max_fav >= 50.0:
        tier_profit = 5.0
      else:
        tier_profit = None

      if tier_profit is not None:
        locked_pts = max(locked_pts, tier_profit) if ce else max(locked_pts, tier_profit)
        milestone_stop = (entry + locked_pts) if ce else (entry - locked_pts)
        stop = max(stop, milestone_stop) if ce else min(stop, milestone_stop)

    if not exit_triggered:
      final_exit = c_sub[-1]
      pts = (final_exit - entry) if ce else (entry - final_exit)
    else:
      pts = locked_pts

    replayed.append(pts)

  return replayed


def run_detailed_replay(cand_df, raw_arrays):
  """Simulates trade execution and returns a detailed DataFrame including exit price, exit time, and exit reason."""
  records = []
  for r in cand_df.itertuples(index=False):
    day = str(r.day)[:10]
    et = pd.Timestamp(r.entry_time).to_datetime64()
    side = r.side
    entry = float(r.entry_price)
    data = raw_arrays.get(day)

    if data is None:
      records.append({
          'exit_time': pd.NaT,
          'exit_price': np.nan,
          'target_points': 0.0,
          'exit_reason': 'No Data'
      })
      continue

    dt, o, h, l, c = data
    idx = np.where(dt >= et)[0]
    idx = idx[
        np.array([pd.Timestamp(x).time() <= dtime(15, 5) for x in dt[idx]])
    ]
    if len(idx) == 0:
      records.append({
          'exit_time': pd.NaT,
          'exit_price': np.nan,
          'target_points': 0.0,
          'exit_reason': 'No Candles'
      })
      continue

    dt_sub, o_sub, h_sub, l_sub, c_sub = dt[idx], o[idx], h[idx], l[idx], c[idx]

    ce = side == 'CE'
    initial_stop = entry - 40.0 if ce else entry + 40.0
    stop = initial_stop
    
    locked_pts = -40.0 
    exit_triggered = False
    exit_time = dt_sub[-1]
    exit_price = c_sub[-1]
    exit_reason = 'EOD Close'

    for i in range(len(o_sub)):
      oi, hi, lo, cl = o_sub[i], h_sub[i], l_sub[i], c_sub[i]
      current_dt = dt_sub[i]

      if ce and lo <= stop:
        exit_triggered = True
        exit_time = current_dt
        exit_price = oi if oi < stop else stop
        exit_reason = 'Milestone SL Hit' if locked_pts > 0 else 'Initial SL Hit'
        break
      if not ce and hi >= stop:
        exit_triggered = True
        exit_time = current_dt
        exit_price = oi if oi > stop else stop
        exit_reason = 'Milestone SL Hit' if locked_pts > 0 else 'Initial SL Hit'
        break

      favorable_high = (hi - entry) if ce else (entry - lo)
      favorable_close = (cl - entry) if ce else (entry - cl)
      max_fav = max(favorable_high, favorable_close)

      if max_fav >= 200.0:
        excess_blocks = int((max_fav - 200.0) // 25.0)
        milestone = 200.0 + (excess_blocks * 25.0)
        tier_profit = milestone - 15.0
      elif max_fav >= 175.0:
        tier_profit = 150.0
      elif max_fav >= 150.0:
        tier_profit = 125.0
      elif max_fav >= 125.0:
        tier_profit = 90.0
      elif max_fav >= 100.0:
        tier_profit = 65.0
      elif max_fav >= 75.0:
        tier_profit = 30.0
      elif max_fav >= 50.0:
        tier_profit = 5.0
      else:
        tier_profit = None

      if tier_profit is not None:
        locked_pts = max(locked_pts, tier_profit) if ce else max(locked_pts, tier_profit)
        milestone_stop = (entry + locked_pts) if ce else (entry - locked_pts)
        stop = max(stop, milestone_stop) if ce else min(stop, milestone_stop)

    if not exit_triggered:
      pts = (exit_price - entry) if ce else (entry - exit_price)
    else:
      pts = locked_pts
      exit_price = (entry + locked_pts) if ce else (entry - locked_pts)

    records.append({
        'exit_time': exit_time,
        'exit_price': round(exit_price, 2),
        'target_points': pts,
        'exit_reason': exit_reason
    })

  return pd.DataFrame(records)


def trigger_live_trailing_alert(option_type, stage_name, contract, index_spot, option_gtt_price, locked_pts, locked_inr):
  """Dynamic helper for live TSL updates."""
  update_time = datetime.now().strftime("%H:%M:%S")
  send_trailing_sl_alert(
      option_type=option_type,
      stage_name=stage_name,
      contract=contract,
      index_spot=index_spot,
      option_gtt_price=option_gtt_price,
      locked_pts=locked_pts,
      locked_inr=locked_inr,
      update_time=update_time
  )


def trigger_live_closure_alert(exit_reason, exit_spot, net_pts, net_inr, daily_limit_str):
  """Dynamic helper for trade closures (Stop Loss Hit / Target Hit)."""
  exit_time = datetime.now().strftime("%H:%M:%S")
  send_trade_closed_alert(
      exit_reason=exit_reason,
      exit_spot=exit_spot,
      net_pts=net_pts,
      net_inr=net_inr,
      daily_limit_str=daily_limit_str,
      exit_time=exit_time
  )
