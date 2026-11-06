# worker.py  -  NIFTY ORB + ML worker  (Angel One -> Render)
#
# What this file does
#   * Angel One websocket (index ticks) -> 5-min candles -> ORB entry logic -> PKL model -> trail SL
#   * Telegram alerts: ENTRY, every TRAIL-SL move, EXIT
#   * Serves tiny JSON on its own URL:  /live (every second, ~0.6 KB)   /static (only when something changed)
#   * Writes snapshots to Upstash only when something changes (few commands, no bandwidth problem)
#
# ENV VARS (Render dashboard):  API_KEY CLIENT_CODE PIN TOTP_SECRET TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
#                               UPSTASH_REDIS_REST_URL UPSTASH_REDIS_REST_TOKEN  [BUDGET_CEILING]
import datetime
import http.server
import json
import math
import os
import threading
import time
import traceback
import urllib.request
import zoneinfo
from datetime import time as dtime

import joblib
import numpy as np
import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

IST = zoneinfo.ZoneInfo("Asia/Kolkata")
INDEX_TOKEN = "99926000"
LOT_SIZE_QTY = int(os.getenv("LOT_SIZE_QTY", "65"))
HISTORY_SESSIONS = int(os.getenv("HISTORY_SESSIONS", "10"))   # Angel gives ~10 days of 5-min; we show last 10 sessions
EOD_EXIT = dtime(15, 10)          # same as backtest: last bar used = 15:05 candle (closes 15:10)

API_KEY = os.getenv("API_KEY", "")
CLIENT_CODE = os.getenv("CLIENT_CODE", "")
PIN = os.getenv("PIN", "")
TOTP_SECRET = os.getenv("TOTP_SECRET", "")
# NOTE: no default token here any more. Keep secrets only in Render env vars.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
BUDGET_CEILING = float(os.getenv("BUDGET_CEILING", "80.0"))
PORT = int(os.getenv("PORT", "10000"))
KEEPALIVE_ALWAYS = os.getenv("KEEPALIVE_ALWAYS", "1") == "1"    # 0 = only ping 08:00-16:30 IST on weekdays

MODEL_URL = "https://github.com/cricketgk07-gif/nifty-angel-terminal/releases/download/v1.0/trained_orb_model.pkl"
CUTOFF_URL = "https://github.com/cricketgk07-gif/nifty-angel-terminal/releases/download/v1.0/model_cutoff.pkl"


# ------------------------------------------------------------------ logging (no more silent failures)
def log(*a):
    print(datetime.datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


_err_seen = {}


def log_exc(tag):
    """Print traceback, but at most once per 30 s per tag."""
    now = time.time()
    if now - _err_seen.get(tag, 0) > 30:
        _err_seen[tag] = now
        log(f"[ERROR:{tag}]\n{traceback.format_exc()}")


def bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def ist_now():
    return datetime.datetime.now(IST)


def market_open(now=None):
    now = now or ist_now()
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


def bucket_epoch(now):
    return int(now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0).timestamp())


def _jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime.datetime)):
        return o.isoformat()
    return str(o)


def dumps(obj):
    return json.dumps(obj, default=_jsonable, separators=(",", ":"))


# ------------------------------------------------------------------ Telegram / Redis
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("[Telegram] token/chat id missing in env")
        return
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=6,
            )
            if r.status_code == 200:
                return
            log(f"[Telegram] HTTP {r.status_code}: {r.text[:150]}")
        except Exception as e:
            log(f"[Telegram] {e}")
        time.sleep(1)


_http = requests.Session()
_budget_cache = {"v": BUDGET_CEILING, "t": 0}


def redis_set(key, obj):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        r = _http.post(f"{UPSTASH_URL}/set/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
                       data=dumps(obj), timeout=8)
        if r.status_code != 200:
            log(f"[Redis set {key}] HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log(f"[Redis set {key}] {e}")


def redis_get(key):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        r = _http.get(f"{UPSTASH_URL}/get/{key}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=8)
        if r.status_code == 200:
            val = r.json().get("result")
            if val:
                return json.loads(val)
    except Exception as e:
        log(f"[Redis get {key}] {e}")
    return None


def get_budget():
    if time.time() - _budget_cache["t"] > 60:
        v = redis_get("user_selected_budget")
        try:
            _budget_cache["v"] = float(v) if v is not None else BUDGET_CEILING
        except Exception:
            _budget_cache["v"] = BUDGET_CEILING
        _budget_cache["t"] = time.time()
    return _budget_cache["v"]


# ------------------------------------------------------------------ model
def _fetch(url, path):
    if not os.path.exists(path):
        log(f"[INIT] downloading {path}")
        urllib.request.urlretrieve(url, path)


ml_model, ml_cutoff = None, 0.52
try:
    _fetch(MODEL_URL, "trained_orb_model.pkl")
    _fetch(CUTOFF_URL, "model_cutoff.pkl")
    ml_model = joblib.load("trained_orb_model.pkl")
    ml_cutoff = float(joblib.load("model_cutoff.pkl"))
    log(f"[INIT] model loaded, cutoff={ml_cutoff:.4f}")
except Exception:
    log_exc("model-load")
    log("[INIT] !!! MODEL NOT LOADED - no ML signals will fire. (sklearn must be 1.6.1)")


# ------------------------------------------------------------------ keepalive + HTTP
STATE = {"live": b"{}", "static": b"{}"}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/live":
            self._send(STATE["live"])
        elif p == "/static":
            self._send(STATE["static"])
        else:
            self._send(b"WORKER_RUNNING_OK", "text/plain")

    def log_message(self, *a):
        pass


def serve_http():
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()


def keepalive_loop():
    """Render only counts requests that come in through its public proxy.
    Pinging 127.0.0.1 (old code) did nothing. We ping our own public URL instead."""
    url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not url:
        log("[Keepalive] RENDER_EXTERNAL_URL not set (only on Render). Use UptimeRobot on the worker URL.")
        return
    while True:
        time.sleep(540)
        n = ist_now()
        if KEEPALIVE_ALWAYS or (n.weekday() < 5 and dtime(8, 0) <= n.time() <= dtime(16, 30)):
            try:
                requests.get(url + "/health", timeout=15)
            except Exception as e:
                log(f"[Keepalive] {e}")


# ------------------------------------------------------------------ strategy maths (same rules as your strategy files)
FEATURE_COLUMNS = [
    'side', 'orb_width', 'range', 'loc', 'body_frac', 'upper_wick', 'lower_wick',
    'atr14', 'ema_dist_atr', 'ext_atr', 'range_lag1', 'range_lag2', 'range_lag3',
    'loc_lag1', 'loc_lag2', 'loc_lag3', 'prior3_move_atr', 'dir_close_loc',
    'dir_ema_dist_atr', 'dir_prior1_move_atr', 'dir_prior3_move_atr',
    'prev_avg_range3_atr', 'signal_range_atr', 'conf_loc_dir', 'stay_orb',
    'beyond_buffer', 'directional_close', 'break_signal_extreme',
    'close_loc_ok', 'close_loc_strong', 'close_mid', 'conf_open',
    'conf_high', 'conf_low', 'conf_close',
]

TRAIL_STAGES = {5.0: "+50 (+5)", 30.0: "+75 (+30)", 65.0: "+100 (+65)", 90.0: "+125 (+90)", 125.0: "+150 (+125)",
                150.0: "+175 (+150)"}


def calculate_indicators(df):
    df['prev_close'] = df['close'].shift(1)
    df['tr'] = np.maximum(df['high'] - df['low'],
                          np.maximum(abs(df['high'] - df['prev_close']), abs(df['low'] - df['prev_close'])))
    df['atr14'] = df['tr'].rolling(window=14).mean().bfill()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    return df


def _signal_side(row, orb_high, orb_low):
    if row['close'] > orb_high and row['close'] > row['ema20']:
        return 'CE'
    if row['close'] < orb_low and row['close'] < row['ema20']:
        return 'PE'
    return None


def _confirmed(side, conf_row, orb_high, orb_low):
    if side == 'CE':
        return conf_row['close'] > orb_high and conf_row['close'] > conf_row['open']
    return conf_row['close'] < orb_low and conf_row['close'] < conf_row['open']


def _cand_fields(day_key, row, side, orb_width):
    rng = row['high'] - row['low']
    if rng == 0:
        return None
    loc = (row['close'] - row['low']) / rng
    body_frac = abs(row['close'] - row['open']) / rng
    if body_frac < 0.12:
        return None
    atr = row['atr14']
    return {
        'day': day_key, 'month_num': row['dt'].strftime('%Y-%m'), 'side': side, 'signal_time': row['dt'],
        'orb_width': orb_width, 'range': rng, 'loc': loc, 'body_frac': body_frac, 'atr14': atr,
        'ema_dist_atr': (row['close'] - row['ema20']) / atr if atr > 0 else 0,
        'ext_atr': abs(row['close'] - row['ema20']) / atr if atr > 0 else 0,
    }


def extract_entry_candidates(byday):
    """BACKTEST version (identical to strategy_entry.py): needs signal bar i, confirm bar i+1, entry bar i+2."""
    out = []
    for day_key, g in byday.items():
        if len(g) < 6 or g.iloc[0]['dt'].time() != dtime(9, 15):
            continue
        ob = g.iloc[0:3]
        oh, ol = ob['high'].max(), ob['low'].min()
        w = oh - ol
        if w > 160.0 or w < 12:
            continue
        for i in range(3, len(g) - 2):
            row = g.iloc[i]
            t = row['dt'].time()
            if t > dtime(14, 30) or t < dtime(9, 30):
                continue
            side = _signal_side(row, oh, ol)
            if not side or not _confirmed(side, g.iloc[i + 1], oh, ol):
                continue
            c = _cand_fields(day_key, row, side, w)
            if c is None:
                continue
            entry_row = g.iloc[i + 2]
            c['entry_time'] = entry_row['dt']
            c['entry_price'] = entry_row['open']
            out.append(c)
            break
    return out


def live_candidate(g):
    """LIVE version: called the moment a bar closes. Last completed bar = CONFIRM bar, the one before = SIGNAL bar.
    Entry = open of the bar that is just starting  ->  alert comes at the entry candle open (0 delay),
    old code waited for the entry candle to CLOSE (5 min late)."""
    n = len(g)
    if n < 5 or g.iloc[0]['dt'].time() != dtime(9, 15):
        return None
    ob = g.iloc[0:3]
    oh, ol = ob['high'].max(), ob['low'].min()
    w = oh - ol
    if w > 160.0 or w < 12:
        return None
    i = n - 2
    if i < 3:
        return None
    row = g.iloc[i]
    t = row['dt'].time()
    if t > dtime(14, 30) or t < dtime(9, 30):
        return None
    side = _signal_side(row, oh, ol)
    if not side or not _confirmed(side, g.iloc[i + 1], oh, ol):
        return None
    return _cand_fields(g.iloc[0]['dt'].strftime('%Y-%m-%d'), row, side, w)


def build_ml_features(cand_df):
    for col in ['range', 'loc']:
        for k in (1, 2, 3):
            cand_df[f'{col}_lag{k}'] = cand_df[col].shift(k).fillna(0)
    cand_df['prior3_move_atr'] = cand_df['range'].rolling(3).sum().fillna(0) / cand_df['atr14']
    cand_df['prev_avg_range3_atr'] = cand_df['range'].rolling(3).mean().fillna(0) / cand_df['atr14']
    for f in FEATURE_COLUMNS:
        if f not in cand_df.columns:
            cand_df[f] = 0.0
    return cand_df


def ml_predict(cand_df):
    cand_df = build_ml_features(cand_df.copy())
    X = cand_df[FEATURE_COLUMNS].copy()
    X['side'] = X['side'].map({'CE': 1, 'PE': -1})
    return ml_model.predict(X)


def trail_tier(max_fav):
    """Locked profit (pts) for the best move so far. Single copy used by replay AND live."""
    if max_fav >= 200.0:
        return 200.0 + int((max_fav - 200.0) // 25.0) * 25.0 - 15.0
    for thr, lock in ((175.0, 150.0), (150.0, 125.0), (125.0, 90.0), (100.0, 65.0), (75.0, 30.0), (50.0, 5.0)):
        if max_fav >= thr:
            return lock
    return None


def next_trail_threshold(max_fav):
    for thr in (50.0, 75.0, 100.0, 125.0, 150.0, 175.0, 200.0):
        if max_fav < thr:
            return thr
    return 200.0 + (int((max_fav - 200.0) // 25.0) + 1) * 25.0


def replay_trade(arr, entry_epoch, side, entry):
    """Candle replay of one trade (same rules as strategy_tsl.run_detailed_replay), epoch based = no timezone bugs."""
    t, o, h, l, c, mod = arr
    idx = np.where((t >= entry_epoch) & (mod <= 15 * 60 + 5))[0]
    if len(idx) == 0:
        return None
    ce = side == 'CE'
    stop = entry - 40.0 if ce else entry + 40.0
    locked = -40.0
    for k in idx:
        if (ce and l[k] <= stop) or ((not ce) and h[k] >= stop):
            ex = entry + locked if ce else entry - locked
            return {"exit_epoch": int(t[k]), "exit_price": round(float(ex), 2), "pts": float(locked),
                    "reason": "Trail SL Hit" if locked > 0 else "Initial SL Hit", "max_locked": float(locked)}
        fav = max((h[k] - entry) if ce else (entry - l[k]), (c[k] - entry) if ce else (entry - c[k]))
        tier = trail_tier(fav)
        if tier is not None:
            locked = max(locked, tier)
            ms = entry + locked if ce else entry - locked
            stop = max(stop, ms) if ce else min(stop, ms)
    k = idx[-1]
    pts = (c[k] - entry) if ce else (entry - c[k])
    return {"exit_epoch": int(t[k]), "exit_price": round(float(c[k]), 2), "pts": float(pts),
            "reason": "EOD Close", "max_locked": float(locked)}


def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def contract_delta(s, k, days, is_ce=True, iv=0.14, r=0.07):
    tau = max(days / 365.0, 0.001)
    d1 = (math.log(s / k) + (r + 0.5 * iv ** 2) * tau) / (iv * math.sqrt(tau))
    ce = norm_cdf(d1)
    return round(ce if is_ce else 1.0 - ce, 2)


def hhmm(epoch):
    return datetime.datetime.fromtimestamp(epoch, IST).strftime("%H:%M")


# ------------------------------------------------------------------ engine
class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.api = None
        self.auth_token = self.feed_token = None
        self.last_login_date = None
        self.need_ws_restart = False

        self.today = ist_now().date()
        self.candles = []          # completed 5-min bars: time/open/high/low/close/dt
        self.df = pd.DataFrame()   # same + indicators + vwap + date_str
        self.active_bar = None
        self.pivots, self.fibs, self.orb = {}, {}, {}
        self.nfo_df = pd.DataFrame()
        self.preview = {}
        self.prev_day = {}
        self.cand_hist = []        # structural candidates of past days (needed for the model's lag features)
        self.hist_markers, self.hist_cards, self.hist_trades = [], {}, []

        self.trade = None          # today's trade dict (open or closed)
        self.trades_taken_today = 0
        self.signal_checked_today = False
        self.ml_note = ""

        self.last_tick_ts = 0.0
        self.opt_ltp_polled = 0.0
        self.ltp = 0.0
        self.ver = 1
        self.last_live_ts = 0.0
        self.last_state_push = 0.0
        self.last_budget = None

        if not self.login():
            raise RuntimeError("Angel login failed")
        self.restore_state()
        self.nfo_df = self.load_scrip_master()
        self.full_refresh(first=True)

    # ---------- login / data
    def login(self):
        for attempt in range(5):
            try:
                self.api = SmartConnect(API_KEY)
                sess = self.api.generateSession(CLIENT_CODE, PIN, pyotp.TOTP(TOTP_SECRET).now())
                if not sess or not sess.get("status"):
                    raise RuntimeError(f"generateSession: {sess}")
                self.auth_token = sess["data"]["jwtToken"]
                self.feed_token = self.api.getfeedToken()
                self.last_login_date = ist_now().date()
                log(f"[Auth] OK for {self.last_login_date}")
                return True
            except Exception:
                log_exc("login")
                time.sleep(5 * (attempt + 1))
        return False

    def load_scrip_master(self):
        """Streams the huge scrip-master JSON (old code loaded ~60 MB at once = out-of-memory risk on 512 MB)."""
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        items = []
        try:
            try:
                import ijson
                with requests.get(url, stream=True, timeout=90) as r:
                    r.raise_for_status()
                    r.raw.decode_content = True
                    for x in ijson.items(r.raw, "item"):
                        if x.get("exch_seg") == "NFO" and x.get("name") == "NIFTY" and x.get("instrumenttype") == "OPTIDX":
                            items.append(x)
            except ImportError:
                data = requests.get(url, timeout=90).json()
                items = [x for x in data if x.get("exch_seg") == "NFO" and x.get("name") == "NIFTY"
                         and x.get("instrumenttype") == "OPTIDX"]
                del data
            df = pd.DataFrame(items)
            if not df.empty:
                df["strike_num"] = pd.to_numeric(df["strike"], errors="coerce") / 100.0
                df["exp_dt"] = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce")
            log(f"[ScripMaster] {len(df)} NIFTY option rows")
            return df
        except Exception:
            log_exc("scrip-master")
            return pd.DataFrame()

    def fetch_5m(self):
        now = ist_now()
        resp = self.api.getCandleData({
            "exchange": "NSE", "symboltoken": INDEX_TOKEN, "interval": "FIVE_MINUTE",
            "fromdate": (now - datetime.timedelta(days=18)).strftime("%Y-%m-%d 09:15"),
            "todate": now.strftime("%Y-%m-%d %H:%M"),
        })
        if not (resp and resp.get("status") and resp.get("data")):
            log(f"[Candles] bad response: {str(resp)[:200]}")
            return None
        bars = []
        for b in resp["data"]:
            d = pd.to_datetime(b[0])
            d = d.tz_localize(IST) if d.tzinfo is None else d.astimezone(IST)
            bars.append({"time": int(d.timestamp()), "open": float(b[1]), "high": float(b[2]),
                         "low": float(b[3]), "close": float(b[4]), "dt": d})
        days = sorted({b["dt"].strftime("%Y-%m-%d") for b in bars})[-HISTORY_SESSIONS:]
        bars = [b for b in bars if b["dt"].strftime("%Y-%m-%d") in days]
        bars.sort(key=lambda x: x["time"])
        return bars

    def reload_candles(self):
        bars = self.fetch_5m()
        if not bars:
            return False
        cur = bucket_epoch(ist_now())
        completed = [b for b in bars if b["time"] < cur]
        partial = [b for b in bars if b["time"] == cur]
        with self.lock:
            self.candles = completed
            if partial and market_open():       # forming candle: keep its true open/high/low
                p = partial[0]
                if self.active_bar is None or self.active_bar["time"] != p["time"]:
                    self.active_bar = dict(p)
            self.rebuild()
        log(f"[Candles] {len(completed)} completed bars loaded, last={hhmm(completed[-1]['time']) if completed else '-'}")
        return True

    def rebuild(self):
        """Indicators + VWAP + today's ORB/fib levels."""
        if not self.candles:
            return
        df = calculate_indicators(pd.DataFrame(self.candles))
        df["date_str"] = df["dt"].dt.strftime("%Y-%m-%d")
        hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
        vol = ((df["high"] - df["low"]) + (df["close"] - df["open"]).abs()) * 1250.0 + 500.0   # synthetic volume (index has none)
        df["vwap"] = ((hlc3 * vol).groupby(df["date_str"]).cumsum() / vol.groupby(df["date_str"]).cumsum()).round(2)
        self.df = df
        last_day = df["date_str"].iloc[-1]
        tb = df[df["date_str"] == last_day].iloc[0:3]
        oh, ol = round(float(tb["high"].max()), 2), round(float(tb["low"].min()), 2)
        rng = max(15.0, round(oh - ol, 2))
        self.orb = {"orb_h": oh, "orb_l": ol, "range": rng, "day": last_day}
        self.fibs = {"ORB_MID": round((oh + ol) / 2.0, 2), "fib_50": round(oh + 0.5 * rng, 2),
                     "fib_100": round(oh + 1.0 * rng, 2), "fib_161": round(oh + 1.618 * rng, 2)}

    def fetch_pivots(self):
        now = ist_now()
        try:
            resp = self.api.getCandleData({
                "exchange": "NSE", "symboltoken": INDEX_TOKEN, "interval": "ONE_DAY",
                "fromdate": (now - datetime.timedelta(days=35)).strftime("%Y-%m-%d 09:15"),
                "todate": now.strftime("%Y-%m-%d 15:30")})
            rows = [r for r in (resp or {}).get("data", []) if str(r[0])[:10] < self.today.strftime("%Y-%m-%d")]
            if not rows:
                return
            _, _, h, l, c, _ = rows[-1]          # last COMPLETED session (old code used iloc[-2] -> wrong before 9:15)
            pdh, pdl, pdc = round(float(h), 2), round(float(l), 2), round(float(c), 2)
            P = round((pdh + pdl + pdc) / 3.0, 2)
            r_ = round(pdh - pdl, 2)
            self.pivots = {"P": P, "R1": round(2 * P - pdl, 2), "S1": round(2 * P - pdh, 2),
                           "R2": round(P + r_, 2), "S2": round(P - r_, 2),
                           "R3": round(pdh + 2 * (P - pdl), 2), "S3": round(pdl - 2 * (pdh - P), 2),
                           "PDH": pdh, "PDL": pdl}
            log(f"[Pivots] P={P} PDH={pdh} PDL={pdl}")
        except Exception:
            log_exc("pivots")

    # ---------- history (what the chart shows for past days)
    def simulate_history(self):
        self.cand_hist, self.hist_markers, self.hist_cards, self.hist_trades = [], [], {}, []
        self.prev_day = {}
        if self.df.empty:
            return
        df = self.df
        today = self.today.strftime("%Y-%m-%d")
        days = [d for d in df["date_str"].unique() if d != today]
        byday = {d: df[df["date_str"] == d].reset_index(drop=True) for d in days}
        cands = extract_entry_candidates(byday)
        self.cand_hist = cands
        log(f"[History] {len(days)} past sessions, {len(cands)} structural ORB candidates")
        if not cands or ml_model is None:
            return
        cdf = pd.DataFrame(cands)
        cdf["pred"] = ml_predict(cdf)
        valid = cdf[cdf["pred"] > ml_cutoff]
        arrays = {}
        for d, g in byday.items():
            mod = (g["dt"].dt.hour * 60 + g["dt"].dt.minute).to_numpy()
            arrays[d] = (g["time"].to_numpy(), g["open"].to_numpy(float), g["high"].to_numpy(float),
                         g["low"].to_numpy(float), g["close"].to_numpy(float), mod)
        for r in valid.itertuples(index=False):
            side, ce = r.side, r.side == "CE"
            entry = float(r.entry_price)
            res = replay_trade(arrays[r.day], int(pd.Timestamp(r.entry_time).timestamp()), side, entry)
            if res is None:
                continue
            e_ep = int(pd.Timestamp(r.entry_time).timestamp())
            self._add_trade_visuals(
                self.hist_markers, self.hist_cards, r.day, side, e_ep, entry, res["exit_epoch"], res["exit_price"],
                res["pts"], int(round(res["pts"] * 0.50 * LOT_SIZE_QTY)), res["reason"],
                entry - 40.0 if ce else entry + 40.0,
                entry + res["max_locked"] if ce else entry - res["max_locked"], "ML-ORB (backtest)", float(r.pred))
        self.hist_trades = [t for t in self._trade_rows_from_cards(self.hist_cards)]
        if self.hist_trades:
            self.prev_day = dict(self.hist_trades[-1])

    @staticmethod
    def _add_trade_visuals(markers, cards, day, side, e_ep, entry, x_ep, x_price, pts, inr, reason, init_sl, trail_sl,
                           title, pred=None):
        ce = side == "CE"
        sign = "+" if pts >= 0 else ""
        markers.append({"time": e_ep, "position": "belowBar" if ce else "aboveBar",
                        "color": "#00e676" if ce else "#ff5252", "shape": "arrowUp" if ce else "arrowDown",
                        "text": f"BUY {side}"})
        card = {"title": f"{title} {side} ({day})", "type": side, "day": day, "entry_time": hhmm(e_ep), "entry": round(entry, 2),
                "exit_time": hhmm(x_ep) if x_ep else "-", "exit": round(x_price, 2) if x_price else None,
                "pts": round(pts, 1), "inr": inr, "reason": reason, "sl": round(init_sl, 2),
                "max_trail_sl": round(trail_sl, 2), "pred": None if pred is None else round(pred, 2)}
        cards[str(e_ep)] = card
        if x_ep:
            markers.append({"time": x_ep, "position": "aboveBar" if ce else "belowBar",
                            "color": "#ffb74d", "shape": "arrowDown" if ce else "arrowUp",
                            "text": f"EXIT {sign}{pts:.0f}p"})
            cards[str(x_ep)] = card

    @staticmethod
    def _trade_rows_from_cards(cards):
        seen, rows = set(), []
        for c in cards.values():
            k = (c["day"], c["entry_time"])
            if k in seen:
                continue
            seen.add(k)
            rows.append({"date": c["day"], "side": c["type"], "entry_time": c["entry_time"], "entry": c["entry"],
                         "exit_time": c["exit_time"], "exit": c["exit"], "pts": c["pts"], "inr": c["inr"],
                         "reason": c["reason"], "final_pts": c["pts"], "final_inr": c["inr"],
                         "exit_reason": c["reason"], "init_sl": c["sl"], "final_trail_sl": c["max_trail_sl"],
                         "entry_spot": c["entry"], "exit_spot": c["exit"], "regime": "ML-ORB"})
        rows.sort(key=lambda r: (r["date"], r["entry_time"]))
        return rows

    # ---------- option contract (preview + actual)
    def option_ltps(self, rows):
        """One batched call if the SmartApi version supports it, else slow one-by-one (rate-limit safe)."""
        out = {}
        try:
            toks = [str(r["token"]) for r in rows]
            res = self.api.getMarketData("LTP", {"NFO": toks})
            for x in (res or {}).get("data", {}).get("fetched", []):
                out[str(x.get("symbolToken"))] = float(x.get("ltp", 0))
            if out:
                return out
        except Exception:
            pass
        for r in rows:
            try:
                res = self.api.ltpData("NFO", r["symbol"], str(r["token"]))
                if isinstance(res, dict) and res.get("status") and res.get("data"):
                    out[str(r["token"])] = float(res["data"].get("ltp", 0.0))
            except Exception:
                log_exc("opt-ltp")
            time.sleep(0.4)           # Angel allows ~1 ltp request / sec
        return out

    def find_budget_strike(self, spot, is_ce):
        budget = get_budget()
        atm = int(round(spot / 50.0) * 50)
        t = "CE" if is_ce else "PE"
        fallback = {"strike": atm, "symbol": f"NIFTY {atm} {t}", "token": "", "delta": 0.5, "days": 4, "ltp": 75.0}
        if self.nfo_df.empty:
            return fallback
        fut = self.nfo_df[self.nfo_df["exp_dt"].dt.date >= self.today].sort_values("exp_dt")
        if fut.empty:
            return fallback
        exp = fut["expiry"].iloc[0]
        sl = fut[fut["expiry"] == exp]
        days = max(1, (sl.iloc[0]["exp_dt"].date() - self.today).days)
        rows = []
        for off in (range(0, 7) if is_ce else range(0, -7, -1)):
            k = atm + off * 50
            m = sl[(sl["strike_num"] == float(k)) & (sl["symbol"].str.endswith(t))]
            if not m.empty:
                rows.append({"strike": k, "symbol": str(m.iloc[0]["symbol"]), "token": str(m.iloc[0]["token"])})
        if not rows:
            return fallback
        ltps = self.option_ltps(rows)
        cands = [dict(r, ltp=ltps.get(r["token"], 0.0)) for r in rows if ltps.get(r["token"], 0.0) > 0]
        if not cands:
            log("[Strike] no live option prices (rate-limit / market closed) -> using fallback")
            r0 = rows[0]
            return dict(r0, delta=contract_delta(spot, r0["strike"], days, is_ce), days=days, ltp=0.0)
        ok = [c for c in cands if c["ltp"] <= budget]
        pick = max(ok, key=lambda c: c["ltp"]) if ok else min(cands, key=lambda c: c["ltp"])
        pick["delta"] = contract_delta(spot, pick["strike"], days, is_ce)
        pick["days"] = days
        return pick

    def targets(self, entry, is_ce, t1):
        p = self.pivots or {}
        c = [t1]
        keys = ["R1", "R2", "R3", "PDH"] if is_ce else ["S1", "S2", "S3", "PDL"]
        for k in keys:
            v = float(p.get(k, 0.0))
            if v and ((is_ce and v > entry + 15) or ((not is_ce) and v < entry - 15)):
                c.append(v)
        c = sorted({round(x, 1) for x in c if (x > entry if is_ce else x < entry)}, reverse=not is_ce)
        while len(c) < 5:
            last = c[-1] if c else entry
            c.append(round(last + 30.0 if is_ce else last - 30.0, 1))
        return c[:6]

    def refresh_preview(self):
        try:
            spot = self.ltp or (float(self.candles[-1]["close"]) if self.candles else 0.0)
            if spot <= 0:
                return
            oh, ol = self.orb.get("orb_h", spot), self.orb.get("orb_l", spot)
            out = {}
            for side, is_ce in (("CE", True), ("PE", False)):
                s = self.find_budget_strike(spot, is_ce)
                entry = oh + 1.0 if is_ce else ol - 1.0
                sl = (ol - 40.0) if is_ce else (oh + 40.0)
                ltp = s["ltp"] if s["ltp"] > 0 else 75.0
                out[side] = {"symbol": s["symbol"], "token": s["token"], "strike": s["strike"], "delta": s["delta"],
                             "opt_ltp": ltp, "exp_entry_spot": round(entry, 1), "exp_sl_spot": round(sl, 1),
                             "exp_sl_opt": max(0.5, round(ltp - abs(entry - sl) * s["delta"], 2)),
                             "targets": self.targets(entry, is_ce, entry + 100.0 if is_ce else entry - 100.0)}
            with self.lock:
                self.preview = out
                self.touch()
        except Exception:
            log_exc("preview")

    # ---------- state persistence
    def restore_state(self):
        st = redis_get("nifty_live_state") or {}
        if st.get("trade_date") == self.today.strftime("%Y-%m-%d"):
            self.trades_taken_today = st.get("trades_taken_today", 0)
            self.trade = st.get("trade")
            self.signal_checked_today = bool(st.get("signal_checked_today", False)) or self.trades_taken_today > 0
            log(f"[Restore] trades_taken={self.trades_taken_today} trade={'yes' if self.trade else 'no'}")

    def push_state(self):
        redis_set("nifty_live_state", {"trade_date": self.today.strftime("%Y-%m-%d"),
                                       "trades_taken_today": self.trades_taken_today,
                                       "signal_checked_today": self.signal_checked_today,
                                       "trade": self.trade, "ts": time.time()})
        self.last_state_push = time.time()

    # ---------- refresh everything (start-up and every morning)
    def full_refresh(self, first=False):
        self.reload_candles()
        self.fetch_pivots()
        self.simulate_history()
        # if a valid signal already happened earlier today (e.g. restart mid-day) don't trade it late
        if not self.df.empty and not self.signal_checked_today:
            today = self.today.strftime("%Y-%m-%d")
            g = self.df[self.df["date_str"] == today].reset_index(drop=True)
            if len(g) >= 5 and (extract_entry_candidates({today: g}) or live_candidate(g)):
                self.signal_checked_today = True
                self.ml_note = "Signal already happened before worker start - skipped"
                log("[Entry] earlier signal found today -> marked as checked")
        self.touch()
        bg(self.refresh_preview)
        self.publish_live(force=True)

    def daily_reset(self):
        with self.lock:
            self.today = ist_now().date()
            self.trade, self.trades_taken_today = None, 0
            self.signal_checked_today, self.ml_note = False, ""
            self.active_bar = None
        if self.login():
            self.need_ws_restart = True          # new feed token -> supervisor reconnects websocket
        self.nfo_df = self.load_scrip_master()
        self.full_refresh()
        self.push_state()

    # ---------- ticks
    def on_tick(self, ltp):
        try:
            if ltp <= 0:
                return
            self.last_tick_ts = time.time()
            self.ltp = ltp
            now = ist_now()
            if not market_open(now):
                return
            with self.lock:
                t = bucket_epoch(now)
                ab = self.active_bar
                if ab is None or ab["time"] != t:
                    if ab is not None:
                        self.close_bar(ab, ltp, now)
                    self.active_bar = {"time": t, "open": ltp, "high": ltp, "low": ltp, "close": ltp,
                                       "dt": now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)}
                else:
                    ab["high"], ab["low"], ab["close"] = max(ab["high"], ltp), min(ab["low"], ltp), ltp
                self.manage_trade(ltp, now)
                self.publish_live()
        except Exception:
            log_exc("on_tick")

    def close_bar(self, ab, ltp, now):
        last_t = self.candles[-1]["time"] if self.candles else 0
        same_day = bool(self.candles) and self.candles[-1]["dt"].date() == ab["dt"].date()
        if last_t and same_day and ab["time"] - last_t > 300:          # missed bars (ws drop / restart) -> back-fill from API
            log("[Bars] gap detected, back-filling from API")
            self.reload_candles()
        if not self.candles or ab["time"] > self.candles[-1]["time"]:
            self.candles.append(dict(ab))
            self.rebuild()
        self.check_entry(ltp, now)
        self.touch()
        bg(self.refresh_preview)

    # ---------- entry
    def check_entry(self, ltp, now):
        if (self.trade or self.trades_taken_today >= 1 or self.signal_checked_today or ml_model is None
                or self.df.empty or now.time() > dtime(14, 40)):
            return
        today = self.today.strftime("%Y-%m-%d")
        g = self.df[self.df["date_str"] == today].reset_index(drop=True)
        cand = live_candidate(g)
        if cand is None:
            return
        self.signal_checked_today = True                  # only the FIRST structural signal of the day counts (as in backtest)
        cand["entry_price"] = float(ltp)
        cdf = pd.DataFrame(self.cand_hist + [cand])
        pred = float(ml_predict(cdf)[-1])
        side = cand["side"]
        log(f"[Signal] {side} pred={pred:.2f} cutoff={ml_cutoff:.2f}")
        if pred <= ml_cutoff:
            self.ml_note = f"{side} signal skipped by ML (pred {pred:.1f} <= cutoff {ml_cutoff:.1f})"
            bg(self.push_state)
            return
        self.ml_note = f"{side} signal accepted (pred {pred:.1f})"
        self.open_trade(side, ltp, now, pred)

    def open_trade(self, side, spot, now, pred):
        ce = side == "CE"
        pv = self.preview.get(side) or {}
        delta = float(pv.get("delta", 0.5))
        opt = float(pv.get("opt_ltp", 75.0))
        init_sl = spot - 40.0 if ce else spot + 40.0
        opt_sl = max(5.0, round(opt - 40.0 * delta, 2))
        bar_t = self.active_bar["time"] if self.active_bar else int(now.timestamp())
        self.trade = {
            "type": side, "mode": "ML-ORB", "symbol": pv.get("symbol", f"NIFTY {side}"), "token": pv.get("token", ""),
            "entry": float(spot), "entry_epoch": bar_t, "entry_time": now.strftime("%H:%M:%S"), "init_sl": init_sl,
            "current_sl": init_sl, "peak": spot, "locked_pts": -40.0, "delta": delta, "opt_entry_ltp": opt,
            "opt_sl": opt_sl, "target_p": spot + 100.0 if ce else spot - 100.0,
            "targets": pv.get("targets", []), "pred": pred, "closed": False,
        }
        self.trades_taken_today = 1
        self.touch()
        stages = "+50 (+5) → +75 (+30) → +100 (+65) → +125 (+90) → +150 (+125) → +200+"
        bg(send_telegram,
           f"⚡ <b>ML-ORB: BUY {side}</b>\n━━━━━━━━━━━━━━━━━\n"
           f"📦 <b>{self.trade['symbol']}</b> (Qty {LOT_SIZE_QTY}, Δ {delta:.2f})\n"
           f"• <b>ENTRY (spot):</b> {spot:.1f} | ≈ ₹{opt:.2f}\n"
           f"• <b>INITIAL SL:</b> {init_sl:.1f} | ≈ ₹{opt_sl:.2f}\n"
           f"• <b>TRAIL:</b> {stages}\n⏰ {now.strftime('%H:%M:%S')}")
        bg(self.push_state)
        self.publish_live(force=True)

    # ---------- trail / exit
    def manage_trade(self, ltp, now):
        tr = self.trade
        if not tr or tr["closed"]:
            return
        ce = tr["type"] == "CE"
        entry = tr["entry"]
        if now.time() >= EOD_EXIT:
            self.close_trade(ltp, "EOD SQUARE-OFF 15:10", now, eod=True)
            return
        stop = tr["current_sl"]
        if (ce and ltp <= stop) or ((not ce) and ltp >= stop):
            self.close_trade(stop, "TRAILING SL HIT" if tr["locked_pts"] > 0 else "INITIAL SL HIT", now)
            return
        tr["peak"] = max(tr["peak"], ltp) if ce else min(tr["peak"], ltp)
        fav = (tr["peak"] - entry) if ce else (entry - tr["peak"])
        tier = trail_tier(fav)
        if tier is not None and tier > tr["locked_pts"]:
            tr["locked_pts"] = tier
            tr["current_sl"] = entry + tier if ce else entry - tier
            d = tr["delta"]
            opt_sl = max(0.5, round(tr["opt_entry_ltp"] + tier * d, 2))
            stage = TRAIL_STAGES.get(tier, f"+{fav:.0f} ({tier:+.0f})")
            log(f"[Trail] {stage} SL -> {tr['current_sl']:.1f}")
            bg(send_telegram,
               f"🔒 <b>TRAIL SL MOVED  [{stage}]</b>\n━━━━━━━━━━━━━━━━━\n"
               f"📦 {tr['symbol']}\n• <b>New SL (spot):</b> {tr['current_sl']:.1f}  |  ≈ ₹{opt_sl:.2f}\n"
               f"• <b>Locked:</b> {tier:+.0f} pts (₹{int(round(tier * d * LOT_SIZE_QTY))})\n"
               f"• Spot now: {ltp:.1f}\n⏰ {now.strftime('%H:%M:%S')}")
            self.touch()
            bg(self.push_state)

    def close_trade(self, exit_spot, reason, now, eod=False):
        tr = self.trade
        ce = tr["type"] == "CE"
        pts = ((exit_spot - tr["entry"]) if ce else (tr["entry"] - exit_spot)) if eod else float(tr["locked_pts"])
        inr = int(round(pts * float(tr["delta"]) * LOT_SIZE_QTY))
        sign = "+" if pts >= 0 else ""
        tr.update(closed=True, exit_spot=round(float(exit_spot), 2), exit_reason=reason, exit_epoch=bucket_epoch(now),
                  exit_time=now.strftime("%H:%M:%S"), final_pts=pts, final_inr=inr)
        self.touch()
        bg(send_telegram,
           f"🏁 <b>TRADE CLOSED ({reason})</b>\n━━━━━━━━━━━━━━━━━\n"
           f"🚪 <b>Exit (spot):</b> {exit_spot:.1f}\n💰 <b>Net:</b> {sign}{pts:.1f} pts (₹{sign}{inr})\n⏰ {now.strftime('%H:%M:%S')}")
        bg(self.push_state)
        self.publish_live(force=True)

    # ---------- publishing to dashboard
    def touch(self):
        self.ver += 1
        self._static_dirty = True

    def today_visuals(self):
        markers, cards = [], {}
        tr = self.trade
        if tr:
            ce = tr["type"] == "CE"
            pts = tr.get("final_pts", tr["locked_pts"]) if tr["closed"] else tr["locked_pts"]
            inr = tr.get("final_inr", int(round(pts * tr["delta"] * LOT_SIZE_QTY)))
            self._add_trade_visuals(
                markers, cards, self.today.strftime("%Y-%m-%d"), tr["type"], tr["entry_epoch"], tr["entry"],
                tr.get("exit_epoch") if tr["closed"] else None, tr.get("exit_spot"), pts, inr,
                tr.get("exit_reason", "ACTIVE"), tr["init_sl"], tr["current_sl"], "ML-ORB LIVE", tr.get("pred"))
        return markers, cards

    def build_static(self):
        tm, tc = self.today_visuals()
        markers = sorted(self.hist_markers + tm, key=lambda m: m["time"])
        cards = dict(self.hist_cards)
        cards.update(tc)
        trades = list(self.hist_trades)
        if self.trade:
            tr = self.trade
            pts = tr.get("final_pts", tr["locked_pts"])
            trades.append({"date": self.today.strftime("%Y-%m-%d"), "side": tr["type"], "entry_time": tr["entry_time"][:5],
                           "entry": round(tr["entry"], 2), "exit_time": tr.get("exit_time", "OPEN")[:5] if tr["closed"] else "OPEN",
                           "exit": tr.get("exit_spot"), "pts": round(pts, 1),
                           "inr": tr.get("final_inr", int(round(pts * tr["delta"] * LOT_SIZE_QTY))),
                           "reason": tr.get("exit_reason", "ACTIVE")})
        if not self.trade:
            status = self.ml_note or ("Waiting for 09:30-14:30 ORB signal" if market_open() else "Market closed")
        elif self.trade["closed"]:
            status = f"Trade closed: {self.trade['exit_reason']}"
        else:
            status = "1/1 trade ACTIVE - trailing"
        return {"ver": self.ver, "pivots": self.pivots, "orb": self.orb, "fibs": self.fibs, "markers": markers,
                "cards": cards, "trades": trades, "preview": self.preview, "prev_day": self.prev_day,
                "status": status, "cutoff": round(ml_cutoff, 3), "budget": get_budget(),
                "date": self.today.strftime("%Y-%m-%d")}

    def publish_static(self):
        s = self.build_static()
        STATE["static"] = dumps(s).encode()
        cs = self.df.tail(HISTORY_SESSIONS * 76)
        candles = [{"time": int(r.time), "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                    "vwap": float(r.vwap), "ema20": round(float(r.ema20), 2)} for r in cs.itertuples()] if not cs.empty else []
        bg(redis_set, "nifty_hist", {"candles": candles, "static": s, "ts": time.time()})
        self._static_dirty = False

    def publish_live(self, force=False):
        nowt = time.time()
        if not force and nowt - self.last_live_ts < 0.5:
            return
        self.last_live_ts = nowt
        if getattr(self, "_static_dirty", True):
            self.publish_static()
        ab = self.active_bar
        hud = None
        tr = self.trade
        if tr:
            ce = tr["type"] == "CE"
            ltp = self.ltp or tr["entry"]
            closed = tr["closed"]
            pts_now = tr["final_pts"] if closed else ((ltp - tr["entry"]) if ce else (tr["entry"] - ltp))
            opt_now = (self.opt_ltp_polled if self.opt_ltp_polled > 0 else
                       max(0.5, round(tr["opt_entry_ltp"] + pts_now * tr["delta"], 2)))
            fav = (tr["peak"] - tr["entry"]) if ce else (tr["entry"] - tr["peak"])
            nthr = next_trail_threshold(max(fav, 0.0))
            hud = {"type": tr["type"], "symbol": tr["symbol"], "closed": closed, "entry": tr["entry"],
                   "entry_opt": tr["opt_entry_ltp"], "init_sl": tr["init_sl"], "init_sl_opt": tr["opt_sl"],
                   "cur_sl": tr["current_sl"], "locked": tr["locked_pts"], "delta": tr["delta"],
                   "cur_sl_opt": max(0.5, round(tr["opt_entry_ltp"] + tr["locked_pts"] * tr["delta"], 2)),
                   "pts_now": round(pts_now, 1), "inr_now": int(round(pts_now * tr["delta"] * LOT_SIZE_QTY)),
                   "opt_now": opt_now, "next_trail_at": round(tr["entry"] + nthr if ce else tr["entry"] - nthr, 1),
                   "targets": tr.get("targets", []), "exit_spot": tr.get("exit_spot"), "exit_reason": tr.get("exit_reason"),
                   "exit_time": tr.get("exit_time")}
        last = self.candles[-1] if self.candles else None
        live = {"ts": nowt, "ver": self.ver, "ltp": self.ltp, "mkt": market_open(),
                "tick_age": round(nowt - self.last_tick_ts, 1) if self.last_tick_ts else None,
                "bar": {k: ab[k] for k in ("time", "open", "high", "low", "close")} if ab else None,
                "last": {k: last[k] for k in ("time", "open", "high", "low", "close")} if last else None,
                "hud": hud}
        STATE["live"] = dumps(live).encode()

    # ---------- background threads
    def option_poller(self):
        while True:
            time.sleep(10)
            try:
                tr = self.trade
                if market_open() and tr and not tr["closed"] and tr.get("token"):
                    res = self.api.ltpData("NFO", tr["symbol"], tr["token"])
                    if isinstance(res, dict) and res.get("status") and res.get("data"):
                        self.opt_ltp_polled = float(res["data"].get("ltp", 0.0))
            except Exception:
                log_exc("opt-poller")

    def rest_fallback_poller(self):
        """Only used when the websocket is silent for > 6 s."""
        warned = False
        while True:
            time.sleep(2)
            try:
                if market_open() and time.time() - self.last_tick_ts > 6:
                    if not warned:
                        log("[Feed] websocket silent -> REST fallback active")
                        warned = True
                    res = self.api.ltpData("NSE", "Nifty 50", INDEX_TOKEN)
                    if isinstance(res, dict) and res.get("status") and res.get("data"):
                        self.on_tick(float(res["data"].get("ltp", 0.0)))
                else:
                    warned = False
            except Exception:
                log_exc("rest-fallback")

    def scheduler(self):
        last_hb = 0.0
        while True:
            time.sleep(20)
            try:
                now = ist_now()
                if self.last_login_date != now.date() and now.time() >= dtime(8, 30):
                    log("[Daily] new day -> re-login & refresh")
                    self.daily_reset()
                b = get_budget()
                if self.last_budget is not None and b != self.last_budget:
                    bg(self.refresh_preview)
                self.last_budget = b
                if time.time() - self.last_state_push > 120:
                    self.push_state()
                if time.time() - last_hb > 300:
                    last_hb = time.time()
                    age = round(time.time() - self.last_tick_ts) if self.last_tick_ts else None
                    log(f"[HB] ltp={self.ltp} tick_age={age}s bars={len(self.candles)} "
                        f"trade={'OPEN' if self.trade and not self.trade['closed'] else ('closed' if self.trade else 'none')}")
                if market_open(now) or now.time() < dtime(15, 45):
                    self.publish_live(force=True)
            except Exception:
                log_exc("scheduler")

    def start_threads(self):
        for fn in (self.option_poller, self.rest_fallback_poller, self.scheduler):
            threading.Thread(target=fn, daemon=True).start()


# ------------------------------------------------------------------ websocket supervisor (engine is created ONCE)
def run_ws_forever(engine):
    while True:
        try:
            engine.need_ws_restart = False
            sws = SmartWebSocketV2(engine.auth_token, API_KEY, CLIENT_CODE, engine.feed_token)

            def on_data(wsapp, msg):
                if isinstance(msg, dict) and "last_traded_price" in msg:
                    engine.on_tick(float(msg["last_traded_price"]) / 100.0)

            def on_open(wsapp):
                log("[WS] connected, subscribing Nifty index")
                sws.subscribe("nifty_1", 1, [{"exchangeType": 1, "tokens": [INDEX_TOKEN]}])

            def on_error(wsapp, error=None):
                log(f"[WS error] {error}")

            def on_close(wsapp, *a):
                log("[WS] closed")

            sws.on_open, sws.on_data, sws.on_error, sws.on_close = on_open, on_data, on_error, on_close
            sws._on_error, sws._on_close = on_error, on_close

            def watch():
                while not engine.need_ws_restart:
                    time.sleep(5)
                try:
                    sws.close_connection()
                except Exception:
                    pass

            threading.Thread(target=watch, daemon=True).start()
            sws.connect()                        # blocks until closed
            log("[WS] connect() returned")
        except Exception:
            log_exc("ws")
        time.sleep(5)
        if not engine.auth_token:
            engine.login()


def main():
    threading.Thread(target=serve_http, daemon=True).start()      # port opens immediately -> Render health check passes
    threading.Thread(target=keepalive_loop, daemon=True).start()
    log(f"[Boot] http on :{PORT}")
    while True:
        try:
            engine = Engine()
            break
        except Exception:
            log_exc("engine-init")
            time.sleep(15)
    engine.start_threads()
    run_ws_forever(engine)


if __name__ == "__main__":
    main()
