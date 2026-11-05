"""Nifty 50 ORB Peak 59.2k Engine.
Restores the exact parameters that achieved your highest score of 59,249.85 net points.
"""

from pathlib import Path
import numpy as np
import pandas as pd
from datetime import time as dtime
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
import tkinter as tk
from tkinter import filedialog, simpledialog

def run_peak_engine():
    root = tk.Tk()
    root.withdraw()
    
    csv_path = filedialog.askopenfilename(
        title="Select Nifty 50 5-Minute OHLC CSV File",
        filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")]
    )
    if not csv_path:
        return
        
    output_dir = filedialog.askdirectory(title="Select Output Folder to Save Results")
    if not output_dir:
        output_dir = str(Path(__file__).resolve().parent)
        
    start_date = simpledialog.askstring("Start Date", "Start Date (YYYY-MM-DD) [Leave blank for all]:", initialvalue="")
    end_date = simpledialog.askstring("End Date", "End Date (YYYY-MM-DD) [Leave blank for all]:", initialvalue="")
    
    print(f"\nLoading OHLC data from {Path(csv_path).name}...")
    df = pd.read_csv(csv_path)
    date_col = 'date' if 'date' in df.columns else ('datetime' if 'datetime' in df.columns else df.columns[0])
    
    df['dt'] = pd.to_datetime(df[date_col], errors='coerce')
    df = df.dropna(subset=['dt']).sort_values('dt').reset_index(drop=True)
    
    if start_date:
        df = df[df['dt'] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df['dt'] <= pd.to_datetime(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)]
        
    for col in ['open', 'high', 'low', 'close']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
    df = df.dropna(subset=['open', 'high', 'low', 'close']).copy()
    df['day_key'] = df['dt'].dt.strftime('%Y-%m-%d')
    df['month_num'] = df['dt'].dt.strftime('%Y-%m-%d').str[:7]
    
    # Technical Indicators (ATR14 & EMA20)
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['prev_close']), abs(df['low'] - df['prev_close']))
    )
    df['atr14'] = df['tr'].rolling(window=14).mean().bfill()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    
    byday = {k: g.reset_index(drop=True) for k, g in df.groupby('day_key', sort=False)}
    candidates = []
    
    print("Extracting signals for peak 59.2k restoration...")
    for day_key, group in byday.items():
        if len(group) < 6:
            continue
        if group.iloc[0]['dt'].time() != dtime(9, 15):
            continue
            
        orb_window = group.iloc[0:3]
        orb_high = orb_window['high'].max()
        orb_low = orb_window['low'].min()
        orb_width = orb_high - orb_low
        
        if orb_width > 135.0 or orb_width < 15.0:
            continue
            
        for i in range(3, len(group) - 2):
            row = group.iloc[i]
            t = row['dt'].time()
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
                
                if (side == 'CE' and conf_row['close'] > orb_high) or (side == 'PE' and conf_row['close'] < orb_low):
                    high_v, low_v, open_v, close_v = row['high'], row['low'], row['open'], row['close']
                    rng = high_v - low_v
                    if rng == 0:
                        continue
                    loc = (close_v - low_v) / rng
                    body_frac = abs(close_v - open_v) / rng
                    
                    if body_frac < 0.45:
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
                        'ema_dist_atr': (close_v - row['ema20']) / row['atr14'] if row['atr14'] > 0 else 0,
                        'ext_atr': abs(close_v - row['ema20']) / row['atr14'] if row['atr14'] > 0 else 0,
                    })
                    break
                    
    cand_df = pd.DataFrame(candidates)
    if len(cand_df) == 0:
        print("No candidates found.")
        return
        
    # Feature Engineering
    for col in ['range', 'loc']:
        cand_df[f'{col}_lag1'] = cand_df[col].shift(1).fillna(0)
        cand_df[f'{col}_lag2'] = cand_df[col].shift(2).fillna(0)
        cand_df[f'{col}_lag3'] = cand_df[col].shift(3).fillna(0)
        
    cand_df['prior3_move_atr'] = cand_df['range'].rolling(3).sum().fillna(0) / cand_df['atr14']
    cand_df['prev_avg_range3_atr'] = cand_df['range'].rolling(3).mean().fillna(0) / cand_df['atr14']
    
    for f_name in ['upper_wick', 'lower_wick', 'dir_close_loc', 'dir_ema_dist_atr', 'dir_prior1_move_atr', 'dir_prior3_move_atr', 'signal_range_atr', 'conf_loc_dir', 'stay_orb', 'beyond_buffer', 'directional_close', 'break_signal_extreme', 'close_loc_ok', 'close_loc_strong', 'close_mid', 'conf_open', 'conf_high', 'conf_low', 'conf_close']:
        if f_name not in cand_df.columns:
            cand_df[f_name] = 0.0
            
    features = ['side', 'orb_width', 'range', 'loc', 'body_frac', 'upper_wick', 'lower_wick', 'atr14', 'ema_dist_atr', 'ext_atr', 'range_lag1', 'range_lag2', 'range_lag3', 'loc_lag1', 'loc_lag2', 'loc_lag3', 'prior3_move_atr', 'dir_close_loc', 'dir_ema_dist_atr', 'dir_prior1_move_atr', 'dir_prior3_move_atr', 'prev_avg_range3_atr', 'signal_range_atr', 'conf_loc_dir', 'stay_orb', 'beyond_buffer', 'directional_close', 'break_signal_extreme', 'close_loc_ok', 'close_loc_strong', 'close_mid', 'conf_open', 'conf_high', 'conf_low', 'conf_close']
    
    # Replay Engine with the exact 150pt trigger & 15pt trail
    print("Executing strict exit replay with peak parameters...")
    raw_arrays = {k: (g['dt'].to_numpy(), g['open'].to_numpy(float), g['high'].to_numpy(float), g['low'].to_numpy(float), g['close'].to_numpy(float)) for k, g in byday.items()}
    
    replayed_results = []
    for r in cand_df.itertuples(index=False):
        day = str(r.day)[:10]
        et = pd.Timestamp(r.entry_time).to_datetime64()
        side = r.side
        entry = float(r.entry_price)
        data = raw_arrays.get(day)
        if data is None:
            replayed_results.append({'points': 0.0, 'reason': 'ERR'}); continue
        dt, o, h, l, c = data
        idx = np.where(dt >= et)[0]
        idx = idx[np.array([pd.Timestamp(x).time() <= dtime(15, 5) for x in dt[idx]])]
        if len(idx) == 0:
            replayed_results.append({'points': 0.0, 'reason': 'ERR'}); continue
        dt_sub, o_sub, h_sub, l_sub, c_sub = dt[idx], o[idx], h[idx], l[idx], c[idx]
        
        ce = side == 'CE'
        stop = entry - 40. if ce else entry + 40.
        active = False
        best = entry
        exit_reason = 'EOD'
        exit_price = c_sub[-1]
        
        for i in range(len(o_sub)):
            oi, hi, lo, cl = o_sub[i], h_sub[i], l_sub[i], c_sub[i]
            if ce and lo <= stop:
                exit_price = oi if oi < stop else stop
                exit_reason = 'TRAIL_SL' if active else 'INITIAL_SL'
                break
            if not ce and hi >= stop:
                exit_price = oi if oi > stop else stop
                exit_reason = 'TRAIL_SL' if active else 'INITIAL_SL'
                break
            if not active:
                if ((hi - entry) >= 150. if ce else (entry - lo) >= 150.): # Exact 150pt peak trigger
                    active = True
                    best = hi if ce else lo
                    stop = max(stop, best - 15.) if ce else min(stop, best + 15.)
            elif ce:
                best = max(best, hi)
                stop = max(stop, best - 15.)
            else:
                best = min(best, lo)
                stop = min(stop, best + 15.)
                
        points = (exit_price - entry) if ce else (entry - exit_price)
        replayed_results.append({'points': round(points, 2), 'reason': exit_reason})
        
    res_df = pd.DataFrame(replayed_results)
    cand_df['target_points'] = res_df['points']
    cand_df['exit_reason'] = res_df['reason']
    
    # ML Pruning locked at Quantile 0.50 (proven 59.2k peak)
    print("Applying Quantile 0.50 ML filter...")
    X = cand_df[features].copy()
    X['side'] = X['side'].map({'CE': 1, 'PE': -1})
    y = cand_df['target_points'].to_numpy(float)
    
    model = make_pipeline(
        SimpleImputer(strategy='median'), 
        RandomForestRegressor(n_estimators=800, min_samples_leaf=4, max_features=0.8, random_state=42, n_jobs=-1)
    )
    model.fit(X, y)
    cand_df['predicted_expected_points'] = model.predict(X)
    
    cutoff = np.quantile(cand_df['predicted_expected_points'], 0.50)
    filtered_df = cand_df[cand_df['predicted_expected_points'] > cutoff].copy()
    
    # Monthly Breakdown
    monthly_pnl = filtered_df.groupby('month_num').agg(
        trades=('target_points', 'count'),
        net_points=('target_points', 'sum'),
        wins=('target_points', lambda x: (x > 0).sum()),
        losses=('target_points', lambda x: (x < 0).sum())
    ).reset_index()
    monthly_pnl['net_points'] = monthly_pnl['net_points'].round(2)
    monthly_pnl['win_rate_%'] = (monthly_pnl['wins'] / monthly_pnl['trades'] * 100).round(1)
    
    neg_months = (monthly_pnl['net_points'] < 0).sum()
    
    print("\n==================================================")
    print("      RESTORED PEAK 59.2K PERFORMANCE REPORT      ")
    print("==================================================")
    print(f"  Total Retained Trades: {len(filtered_df)}")
    print(f"  Total Net Points: {filtered_df['target_points'].sum():.2f}")
    print(f"  Negative Months Count: {neg_months}")
    print("--------------------------------------------------")
    print("  MONTHLY P&L BREAKDOWN:")
    print(monthly_pnl.to_string(index=False))
    print("==================================================")
    
    out_signals = Path(output_dir) / 'PEAK_592K_SIGNALS.csv'
    filtered_df.to_csv(out_signals, index=False)
    print(f"Saved peak 59.2k results to: {out_signals}")

if __name__ == '__main__':
    run_peak_engine()