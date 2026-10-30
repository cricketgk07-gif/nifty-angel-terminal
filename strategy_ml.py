# strategy_ml.py
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

FEATURE_COLUMNS = [
    'side',
    'orb_width',
    'range',
    'loc',
    'body_frac',
    'upper_wick',
    'lower_wick',
    'atr14',
    'ema_dist_atr',
    'ext_atr',
    'range_lag1',
    'range_lag2',
    'range_lag3',
    'loc_lag1',
    'loc_lag2',
    'loc_lag3',
    'prior3_move_atr',
    'dir_close_loc',
    'dir_ema_dist_atr',
    'dir_prior1_move_atr',
    'dir_prior3_move_atr',
    'prev_avg_range3_atr',
    'signal_range_atr',
    'conf_loc_dir',
    'stay_orb',
    'beyond_buffer',
    'directional_close',
    'break_signal_extreme',
    'close_loc_ok',
    'close_loc_strong',
    'close_mid',
    'conf_open',
    'conf_high',
    'conf_low',
    'conf_close',
]


def build_ml_features(cand_df):
  for col in ['range', 'loc']:
    cand_df[f'{col}_lag1'] = cand_df[col].shift(1).fillna(0)
    cand_df[f'{col}_lag2'] = cand_df[col].shift(2).fillna(0)
    cand_df[f'{col}_lag3'] = cand_df[col].shift(3).fillna(0)

  cand_df['prior3_move_atr'] = (
      cand_df['range'].rolling(3).sum().fillna(0) / cand_df['atr14']
  )
  cand_df['prev_avg_range3_atr'] = (
      cand_df['range'].rolling(3).mean().fillna(0) / cand_df['atr14']
  )

  for f_name in FEATURE_COLUMNS:
    if f_name not in cand_df.columns:
      cand_df[f_name] = 0.0

  return cand_df


def train_and_save_model(cand_df):
  cand_df = build_ml_features(cand_df)
  X = cand_df[FEATURE_COLUMNS].copy()
  X['side'] = X['side'].map({'CE': 1, 'PE': -1})
  y = cand_df['target_points'].to_numpy(float)

  model = make_pipeline(
      SimpleImputer(strategy='median'),
      RandomForestRegressor(
          n_estimators=1500,
          min_samples_leaf=2,
          max_depth=22,
          max_features=0.9,
          random_state=42,
          n_jobs=-1,
      ),
  )
  model.fit(X, y)

  predictions = model.predict(X)
  cutoff = np.quantile(predictions, 0.52)

  joblib.dump(model, 'trained_orb_model.pkl')
  joblib.dump(cutoff, 'model_cutoff.pkl')
  print(
      '[ML] Model trained and saved to trained_orb_model.pkl & model_cutoff.pkl'
  )

  return model, cutoff