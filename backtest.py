# backtest.py
from pathlib import Path
import numpy as np
import pandas as pd
import tkinter as tk
from tkinter import filedialog, simpledialog

from strategy_entry import calculate_indicators, extract_entry_candidates
from strategy_ml import train_and_save_model
from strategy_tsl import run_detailed_replay


def run_backtest():
  root = tk.Tk()
  root.withdraw()
  root.attributes('-topmost', True)

  print('[1/5] Select Nifty 50 CSV File...')
  csv_path = filedialog.askopenfilename(
      title='Select Nifty 50 5-Minute OHLC CSV File',
      filetypes=[('CSV Files', '*.csv')],
  )
  if not csv_path:
    return

  output_dir = filedialog.askdirectory(title='Select Output Folder')
  if not output_dir:
    output_dir = str(Path(__file__).resolve().parent)

  start_date = simpledialog.askstring(
      'Start Date', 'Start Date (YYYY-MM-DD):', initialvalue=''
  )
  end_date = simpledialog.askstring(
      'End Date', 'End Date (YYYY-MM-DD):', initialvalue=''
  )

  print('\n[2/5] Loading and parsing data...')
  df = pd.read_csv(csv_path)
  date_col = (
      'date'
      if 'date' in df.columns
      else ('datetime' if 'datetime' in df.columns else df.columns[0])
  )
  df['dt'] = pd.to_datetime(df[date_col], errors='coerce')
  df = df.dropna(subset=['dt']).sort_values('dt').reset_index(drop=True)

  if start_date:
    df = df[df['dt'] >= pd.to_datetime(start_date)]
  if end_date:
    df = df[
        df['dt']
        <= pd.to_datetime(end_date) + pd.Timedelta(days=1)
        - pd.Timedelta(seconds=1)
    ]

  for col in ['open', 'high', 'low', 'close']:
    df[col] = pd.to_numeric(df[col], errors='coerce')

  df = df.dropna(subset=['open', 'high', 'low', 'close']).copy()
  df['day_key'] = df['dt'].dt.strftime('%Y-%m-%d')
  df['month_num'] = df['dt'].dt.strftime('%Y-%m-%d').str[:7]

  df = calculate_indicators(df)
  byday = {
      k: g.reset_index(drop=True) for k, g in df.groupby('day_key', sort=False)
  }

  print('[3/5] Extracting breakout candidates...')
  candidates = extract_entry_candidates(byday)
  cand_df = pd.DataFrame(candidates)
  if len(cand_df) == 0:
    print('No candidates found.')
    return

  raw_arrays = {
      k: (
          g['dt'].to_numpy(),
          g['open'].to_numpy(float),
          g['high'].to_numpy(float),
          g['low'].to_numpy(float),
          g['close'].to_numpy(float),
      )
      for k, g in byday.items()
  }

  print('[4/5] Running detailed replay simulation (Exit Prices & Times)...')
  detailed_res = run_detailed_replay(cand_df, raw_arrays)
  cand_df['target_points'] = detailed_res['target_points']
  cand_df['exit_price'] = detailed_res['exit_price']
  cand_df['exit_time'] = detailed_res['exit_time']
  cand_df['exit_reason'] = detailed_res['exit_reason']

  print('[5/5] Training ML Model & applying 0.52 Quantile Filter...')
  model, cutoff = train_and_save_model(cand_df)

  from strategy_ml import FEATURE_COLUMNS, build_ml_features

  cand_df = build_ml_features(cand_df)
  X = cand_df[FEATURE_COLUMNS].copy()
  X['side'] = X['side'].map({'CE': 1, 'PE': -1})
  cand_df['predicted_expected_points'] = model.predict(X)

  filtered_df = cand_df[cand_df['predicted_expected_points'] > cutoff].copy()

  monthly_pnl = (
      filtered_df.groupby('month_num')
      .agg(
          trades=('target_points', 'count'),
          net_points=('target_points', 'sum'),
          wins=('target_points', lambda x: (x > 0).sum()),
          losses=('target_points', lambda x: (x < 0).sum()),
      )
      .reset_index()
  )
  monthly_pnl['net_points'] = monthly_pnl['net_points'].round(2)
  monthly_pnl['win_rate_%'] = (
      monthly_pnl['wins'] / monthly_pnl['trades'] * 100
  ).round(1)

  print('\n==================================================')
  print('      GOLDEN LOCKED BACKTEST REPORT (53.4K)     ')
  print('==================================================')
  print(f'  Total Net Points: {filtered_df["target_points"].sum():.2f}')
  print(monthly_pnl.to_string(index=False))
  print('==================================================')

  filtered_df.to_csv(
      Path(output_dir) / 'GOLDEN_LOCKED_SIGNALS.csv', index=False
  )
  monthly_pnl.to_csv(Path(output_dir) / 'GOLDEN_LOCKED_MONTHLY.csv', index=False)


if __name__ == '__main__':
  run_backtest()
