import datetime
import html
import http.server
import json
import math
import os
import socketserver
import threading
import time
import pandas as pd
import pyotp
import requests
import zoneinfo
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

INDEX_TOKEN = "99926000"
LOT_SIZE_QTY = 65
IST = zoneinfo.ZoneInfo("Asia/Kolkata")

API_KEY = os.getenv("API_KEY", "")
CLIENT_CODE = os.getenv("CLIENT_CODE", "")
PIN = os.getenv("PIN", "")
TOTP_SECRET = os.getenv("TOTP_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8925956740:AAHHo18Gp-rwlsGlnsyS5mz40RbvIN_tXqw")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6898891302")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
BUDGET_CEILING = float(os.getenv("BUDGET_CEILING", "80.0"))
PORT = int(os.getenv("PORT", 10000))


# ==========================================
# 1. Health Server & Keepalive
# ==========================================
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"WORKER_RUNNING_OK")

    def log_message(self, format, *args):
        pass


def run_ping_server():
    with socketserver.TCPServer(("0.0.0.0", PORT), HealthHandler) as httpd:
        httpd.serve_forever()


threading.Thread(target=run_ping_server, daemon=True).start()


def self_ping_keepalive():
    while True:
        time.sleep(480)
        try:
            requests.get(f"http://127.0.0.1:{PORT}/", timeout=5)
        except Exception:
            pass


threading.Thread(target=self_ping_keepalive, daemon=True).start()


# ==========================================
# 2. Math & Redis Helpers
# ==========================================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def compute_contract_delta(s, k, days_to_exp, is_ce=True, iv=0.14, r=0.07):
    tau = max(days_to_exp / 365.0, 0.001)
    d1 = (math.log(s / k) + (r + 0.5 * iv ** 2) * tau) / (iv * math.sqrt(tau))
    ce_delta = norm_cdf(d1)
    return round(ce_delta if is_ce else (1.0 - ce_delta), 2)


def is_market_hours():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    return datetime.time(9, 15) <= now.time() <= datetime.time(15, 30)


def send_telegram_alert(message: str):
    if not is_market_hours():
        return False

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=6)
        return r.status_code == 200
    except Exception:
        return False


def push_to_redis(key, data_obj):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return
    url = f"{UPSTASH_REDIS_REST_URL}/set/{key}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        requests.post(url, headers=headers, data=json.dumps(data_obj), timeout=3)
    except Exception:
        pass


def read_from_redis(key="nifty_live_state"):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    url = f"{UPSTASH_REDIS_REST_URL}/get/{key}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=2)
        if r.status_code == 200:
            val = r.json().get("result")
            if val:
                return json.loads(val)
    except Exception:
        pass
    return None


def get_dynamic_budget():
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return BUDGET_CEILING
    try:
        url = f"{UPSTASH_REDIS_REST_URL}/get/user_selected_budget"
        headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=2)
        if r.status_code == 200:
            val = r.json().get("result")
            if val:
                return float(val)
    except Exception:
        pass
    return BUDGET_CEILING


# ==========================================
# 3. Strategy Core Engine
# ==========================================
class DedicatedTradingWorker:
    def __init__(self):
        self.candles = []
        self.active_bar = None
        self.today = datetime.datetime.now(IST).date()
        self.markers = []
        self.history_cards = {}
        self.pivot_lines = {}
        self.fib_lines = {}
        self.orb_levels = {}
        self.hud_data = {}

        self.locked_trade = None
        self.trades_taken_today = 0
        self.trade_history_today = []
        self.daily_atr = 175.0
        self.current_atr5 = 14.0
        self.session_open_price = 0.0
        self.prev_day_close = 0.0

        self.auth_token = None
        self.feed_token = None
        self.last_login_date = None
        self.last_polled_opt_ltp = 0.0
        self.last_push_time = 0.0

        self.authenticate_angel_one()

        today_str = self.today.strftime("%Y-%m-%d")
        saved_state = read_from_redis("nifty_live_state") or {}

        self.markers = []
        self.history_cards = {}

        if is_market_hours() and saved_state.get("trade_date") == today_str:
            if saved_state.get("locked_trade") and not saved_state.get("locked_trade", {}).get("closed", False):
                self.locked_trade = saved_state.get("locked_trade")
                self.trades_taken_today = saved_state.get("trades_taken_today", 1)
                self.trade_history_today = saved_state.get("trade_history_today", [])
            else:
                self.trades_taken_today = saved_state.get("trades_taken_today", 0)
                self.trade_history_today = saved_state.get("trade_history_today", [])

        self.nfo_df = self.load_scrip_master()
        self.init_historical_candles()
        self.run_full_strategy_pipeline()

        threading.Thread(target=self.background_option_poller, daemon=True).start()

        if self.candles:
            self.sync_state(self.candles[-1]["close"], force=True)

    def authenticate_angel_one(self):
        try:
            self.api = SmartConnect(API_KEY)
            totp = pyotp.TOTP(TOTP_SECRET).now()
            self.session = self.api.generateSession(CLIENT_CODE, PIN, totp)
            self.auth_token = self.session["data"]["jwtToken"]
            self.feed_token = self.api.getfeedToken()
            self.last_login_date = datetime.datetime.now(IST).date()
            print(f"[Auth Success]: SmartAPI session generated for {self.last_login_date}")
        except Exception as e:
            print(f"[Auth Error]: Session generation failed: {e}")

    def load_scrip_master(self):
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        try:
            r = requests.get(url, timeout=15)
            data = r.json()
            nfo_items = [
                x for x in data
                if x.get("exch_seg") == "NFO"
                and x.get("name") == "NIFTY"
                and x.get("instrumenttype") == "OPTIDX"
            ]
            df = pd.DataFrame(nfo_items)
            if not df.empty:
                df["strike_num"] = pd.to_numeric(df["strike"], errors="coerce") / 100.0
                df["exp_dt"] = pd.to_datetime(df["expiry"], format="%d%b%Y", errors="coerce")
            return df
        except Exception:
            return pd.DataFrame()

    def init_historical_candles(self):
        now = datetime.datetime.now(IST)
        loaded = False

        for days_back in [5, 3]:
            from_str = (now - datetime.timedelta(days=days_back)).strftime("%Y-%m-%d 09:15")
            to_str = now.strftime("%Y-%m-%d %H:%M")
            try:
                resp = self.api.getCandleData({
                    "exchange": "NSE",
                    "symboltoken": INDEX_TOKEN,
                    "interval": "FIVE_MINUTE",
                    "fromdate": from_str,
                    "todate": to_str,
                })
                if resp and resp.get("status") and resp.get("data") and len(resp["data"]) > 10:
                    cand_list = []
                    for b in resp["data"]:
                        dt_parsed = pd.to_datetime(b[0])
                        if dt_parsed.tzinfo is None:
                            dt_ist = dt_parsed.tz_localize("Asia/Kolkata")
                        else:
                            dt_ist = dt_parsed.astimezone(IST)

                        t_sec = int(dt_ist.timestamp())
                        cand_list.append({
                            "time": t_sec,
                            "open": float(b[1]),
                            "high": float(b[2]),
                            "low": float(b[3]),
                            "close": float(b[4]),
                            "dt": dt_ist,
                        })
                    self.candles = cand_list
                    loaded = True
                    print(f"[Candles Loaded]: {len(self.candles)} candles initialized over {days_back} days.")
                    break
            except Exception as e:
                print(f"[Candles Error]: {e}")

        self.fetch_daily_fallback_levels()

    def fetch_daily_fallback_levels(self):
        now = datetime.datetime.now(IST)
        from_str = (now - datetime.timedelta(days=12)).strftime("%Y-%m-%d 09:15")
        to_str = now.strftime("%Y-%m-%d 15:30")
        try:
            resp = self.api.getCandleData({
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "ONE_DAY",
                "fromdate": from_str,
                "todate": to_str,
            })
            if resp and resp.get("status") and resp.get("data") and len(resp["data"]) >= 2:
                prev_bar = resp["data"][-2]
                pdh = round(float(prev_bar[2]), 2)
                pdl = round(float(prev_bar[3]), 2)
                pdc = round(float(prev_bar[4]), 2)
                self.prev_day_close = pdc
                P = round((pdh + pdl + pdc) / 3.0, 2)
                rng = round(pdh - pdl, 2)
                self.daily_atr = max(140.0, rng)

                self.pivot_lines = {
                    "P": P,
                    "R1": round((2.0 * P) - pdl, 2), "S1": round((2.0 * P) - pdh, 2),
                    "R2": round(P + rng, 2), "S2": round(P - rng, 2),
                    "R3": round(pdh + (2.0 * (P - pdl)), 2), "S3": round(pdl - (2.0 * (pdh - P)), 2),
                    "R4": round(round(pdh + (2.0 * (P - pdl)), 2) + rng, 2), "S4": round(round(pdl - (2.0 * (pdh - P)), 2) - rng, 2),
                    "R5": round(round(round(pdh + (2.0 * (P - pdl)), 2) + rng, 2) + rng, 2), "S5": round(round(round(pdl - (2.0 * (pdh - P)), 2) - rng, 2) - rng, 2),
                    "PDH": pdh,
                    "PDL": pdl,
                }
                print(f"[Daily Levels Verified]: PDH={pdh}, PDL={pdl}, PIVOT={P}")
        except Exception as e:
            print(f"[Daily Fallback Error]: {e}")

    def compute_all_confluence_targets(self, df_h, entry_p, is_ce, custom_pivots=None, custom_orb=None):
        recent_swing_h = float(df_h["high"].iloc[-20:].max()) if len(df_h) >= 20 else entry_p + 75.0
        recent_swing_l = float(df_h["low"].iloc[-20:].min()) if len(df_h) >= 20 else entry_p - 75.0

        orb = custom_orb or self.orb_levels
        orb_h = orb.get("orb_h", entry_p)
        orb_l = orb.get("orb_l", entry_p)
        orb_rng = orb.get("range", 25.0)

        p = custom_pivots or self.pivot_lines
        min_runway = 70.0

        if is_ce:
            candidates = [
                orb_h + (1.000 * orb_rng),
                orb_h + (1.618 * orb_rng),
                p.get("P"),
                p.get("R1"), p.get("R2"), p.get("R3"),
                p.get("PDH"),
                recent_swing_h,
            ]
            valid = sorted(list(set([round(x, 1) for x in candidates if x is not None and x >= entry_p + min_runway])))
            if not valid:
                valid = [round(entry_p + 75.0, 1), round(entry_p + 125.0, 1), round(entry_p + 175.0, 1)]
        else:
            candidates = [
                orb_l - (1.000 * orb_rng),
                orb_l - (1.618 * orb_rng),
                p.get("P"),
                p.get("S1"), p.get("S2"), p.get("S3"),
                p.get("PDL"),
                recent_swing_l,
            ]
            valid = sorted(list(set([round(x, 1) for x in candidates if x is not None and x <= entry_p - min_runway])), reverse=True)
            if not valid:
                valid = [round(entry_p - 75.0, 1), round(entry_p - 125.0, 1), round(entry_p - 175.0, 1)]

        return valid

    def find_budget_strike(self, spot_ref, is_ce, budget_target=None):
        if budget_target is None:
            budget_target = get_dynamic_budget()

        atm = int(round(spot_ref / 50.0) * 50)
        opt_type = "CE" if is_ce else "PE"

        if self.nfo_df.empty:
            return atm, f"NIFTY {atm} {opt_type}", "", 0.50, 4

        future_exp = self.nfo_df[self.nfo_df["exp_dt"].dt.date >= self.today].sort_values("exp_dt")
        if future_exp.empty:
            return atm, f"NIFTY {atm} {opt_type}", "", 0.50, 4

        curr_exp = future_exp["expiry"].iloc[0]
        exp_slice = future_exp[future_exp["expiry"] == curr_exp]
        days_to_exp = max(1, (exp_slice.iloc[0]["exp_dt"].date() - self.today).days)

        # Direction-aware candidate evaluation (strictly OTM/ATM)
        candidates = []
        strike_offsets = range(0, 8) if is_ce else range(0, -8, -1)

        for offset in strike_offsets:
            k = atm + (offset * 50)
            row = exp_slice[(exp_slice["strike_num"] == float(k)) & (exp_slice["symbol"].str.endswith(opt_type))]
            if not row.empty:
                sym = str(row.iloc[0]["symbol"])
                tok = str(row.iloc[0]["token"])

                real_ltp = 0.0
                try:
                    res = self.api.ltpData("NFO", sym, tok)
                    if isinstance(res, dict) and res.get("status") and res.get("data"):
                        real_ltp = float(res["data"].get("ltp", 0.0))
                except Exception:
                    pass

                if real_ltp <= 0.0:
                    otm_dist = (k - spot_ref) if is_ce else (spot_ref - k)
                    real_ltp = max(5.0, 110.0 - (otm_dist * 0.45))

                candidates.append((k, sym, tok, real_ltp))

        valid = [c for c in candidates if c[3] <= budget_target]
        if valid:
            valid.sort(key=lambda x: x[3], reverse=True)
            chosen_strike, chosen_sym, chosen_tok, chosen_ltp = valid[0]
        elif candidates:
            candidates.sort(key=lambda x: x[3])
            chosen_strike, chosen_sym, chosen_tok, chosen_ltp = candidates[0]
        else:
            chosen_strike = atm
            chosen_sym = f"NIFTY {atm} {opt_type}"
            chosen_tok = ""

        delta = compute_contract_delta(spot_ref, chosen_strike, days_to_exp, is_ce=is_ce)
        return chosen_strike, chosen_sym, chosen_tok, delta, days_to_exp

    def run_full_strategy_pipeline(self):
        if not self.candles:
            return

        df_h = pd.DataFrame(self.candles)
        df_h["date_str"] = df_h["dt"].apply(lambda d: d.astimezone(IST).strftime("%Y-%m-%d"))

        candle_spread = (df_h["high"] - df_h["low"]) + (df_h["close"] - df_h["open"]).abs()
        df_h["calc_vol"] = candle_spread * 1250.0 + 500.0
        df_h["tp"] = (df_h["high"] + df_h["low"] + df_h["close"]) / 3.0
        df_h["pv"] = df_h["tp"] * df_h["calc_vol"]

        df_h["cum_vol"] = df_h.groupby("date_str")["calc_vol"].cumsum()
        df_h["cum_pv"] = df_h.groupby("date_str")["pv"].cumsum()
        df_h["vwap"] = (df_h["cum_pv"] / df_h["cum_vol"]).round(2)
        df_h["ema9"] = df_h["close"].ewm(span=9, adjust=False).mean().round(2)

        tr1 = df_h["high"] - df_h["low"]
        tr2 = (df_h["high"] - df_h["close"].shift(1)).abs()
        tr3 = (df_h["low"] - df_h["close"].shift(1)).abs()
        df_h["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df_h["atr14"] = df_h["tr"].rolling(14).mean().fillna(14.0).round(2)
        self.current_atr5 = float(df_h["atr14"].iloc[-1])

        for i in range(len(self.candles)):
            self.candles[i]["vwap"] = float(df_h.iloc[i]["vwap"])
            self.candles[i]["ema9"] = float(df_h.iloc[i]["ema9"])
            self.candles[i]["atr14"] = float(df_h.iloc[i]["atr14"])

        unique_days = list(df_h["date_str"].unique())
        today_bars = df_h[df_h["date_str"] == unique_days[-1]].sort_values("time").reset_index(drop=True)
        if len(today_bars) > 0:
            self.session_open_price = float(today_bars.iloc[0]["open"])

        orb_bars = today_bars.iloc[0:3] if len(today_bars) >= 3 else today_bars
        curr_orb_h = round(float(orb_bars["high"].max()), 2)
        curr_orb_l = round(float(orb_bars["low"].min()), 2)
        orb_range_val = max(15.0, round(curr_orb_h - curr_orb_l, 2))
        self.orb_levels = {"orb_h": curr_orb_h, "orb_l": curr_orb_l, "range": orb_range_val}

        self.fib_lines = {
            "FIB_1_0_H": round(curr_orb_h + orb_range_val, 2),
            "FIB_1_618_H": round(curr_orb_h + (1.618 * orb_range_val), 2),
            "FIB_1_0_L": round(curr_orb_l - orb_range_val, 2),
            "FIB_1_618_L": round(curr_orb_l - (1.618 * orb_range_val), 2),
        }

        self.markers = []
        self.history_cards = {}

        for d_str, day_df in df_h.groupby("date_str"):
            day_bars = day_df.sort_values("time").reset_index(drop=True)
            if len(day_bars) < 4:
                continue

            ow = day_bars.iloc[0:3]
            d_h = round(float(ow["high"].max()), 2)
            d_l = round(float(ow["low"].min()), 2)
            d_rng = max(15.0, round(d_h - d_l, 2))
            day_orb = {"orb_h": d_h, "orb_l": d_l, "range": d_rng}

            day_pivots = self.pivot_lines
            day_trade_count = 0
            sim_active_trade = None

            for idx in range(3, len(day_bars)):
                r = day_bars.iloc[idx]
                prev_bar = day_bars.iloc[idx - 1]
                t_open = r["dt"].astimezone(IST).time()
                c = float(r["close"])
                h = float(r["high"])
                l = float(r["low"])
                vw = float(r["vwap"])
                bar_atr = float(r.get("atr14", 14.0))
                t_sec = int(r["time"])
                candle_close_time = (r["dt"].astimezone(IST) + datetime.timedelta(minutes=5)).strftime("%H:%M")

                # Trailing Execution Loop
                if sim_active_trade is not None:
                    is_ce_sim = (sim_active_trade["type"] == "CE")
                    entry_price = sim_active_trade["entry"]
                    trade_atr = sim_active_trade.get("entry_atr", bar_atr)

                    if is_ce_sim and l <= sim_active_trade["sl"]:
                        sim_active_trade = None
                    elif (not is_ce_sim) and h >= sim_active_trade["sl"]:
                        sim_active_trade = None

                    if sim_active_trade is not None:
                        if is_ce_sim:
                            sim_active_trade["peak"] = max(sim_active_trade.get("peak", entry_price), h)
                            peak_gain = sim_active_trade["peak"] - entry_price
                        else:
                            sim_active_trade["peak"] = min(sim_active_trade.get("peak", entry_price), l)
                            peak_gain = entry_price - sim_active_trade["peak"]

                        # Stage 1: Fib 0.500 Pullback - 0.5x ATR
                        if peak_gain >= (2.5 * trade_atr) and sim_active_trade["stage"] < 1:
                            sim_active_trade["stage"] = 1
                            if is_ce_sim:
                                fib_50 = sim_active_trade["peak"] - (0.500 * peak_gain)
                                candidate_sl = round(fib_50 - (0.5 * trade_atr), 1)
                                if candidate_sl > sim_active_trade["sl"]:
                                    sim_active_trade["sl"] = candidate_sl
                            else:
                                fib_50 = sim_active_trade["peak"] + (0.500 * peak_gain)
                                candidate_sl = round(fib_50 + (0.5 * trade_atr), 1)
                                if candidate_sl < sim_active_trade["sl"]:
                                    sim_active_trade["sl"] = candidate_sl

                        # Stage 2: Fib 0.382 Pullback - 0.5x ATR
                        if peak_gain >= (4.5 * trade_atr) and sim_active_trade["stage"] < 2:
                            sim_active_trade["stage"] = 2
                            if is_ce_sim:
                                fib_382 = sim_active_trade["peak"] - (0.382 * peak_gain)
                                candidate_sl = round(fib_382 - (0.5 * trade_atr), 1)
                                if candidate_sl > sim_active_trade["sl"]:
                                    sim_active_trade["sl"] = candidate_sl
                            else:
                                fib_382 = sim_active_trade["peak"] + (0.382 * peak_gain)
                                candidate_sl = round(fib_382 + (0.5 * trade_atr), 1)
                                if candidate_sl < sim_active_trade["sl"]:
                                    sim_active_trade["sl"] = candidate_sl

                        # Stage 3: Runner -> Prior Bar Extreme +/- 2.0 pts
                        targets = sim_active_trade["targets"]
                        t1_dist = abs(targets[0] - entry_price) if targets else 75.0
                        if peak_gain >= t1_dist:
                            if is_ce_sim:
                                candidate_sl = round(float(prev_bar["low"]) + 2.0, 1)
                                if candidate_sl > sim_active_trade["sl"]:
                                    sim_active_trade["sl"] = candidate_sl
                            else:
                                candidate_sl = round(float(prev_bar["high"]) - 2.0, 1)
                                if candidate_sl < sim_active_trade["sl"]:
                                    sim_active_trade["sl"] = candidate_sl

                        continue

                if day_trade_count >= 2:
                    continue

                in_w1 = datetime.time(9, 30) <= t_open <= datetime.time(11, 15)
                in_w2 = datetime.time(12, 45) <= t_open <= datetime.time(13, 45)

                if not (in_w1 or in_w2):
                    continue

                prev_c = float(prev_bar["close"])
                prior_bars = day_bars.iloc[:idx]
                m_hod = float(prior_bars["high"].max())
                m_lod = float(prior_bars["low"].min())

                ce_cand = False
                pe_cand = False
                sl_val = 0.0

                if in_w1:
                    if c > d_h and prev_c <= d_h:
                        ce_cand = True
                        sl_val = round(l - 5.0, 1)
                    elif (c < d_l and prev_c >= d_l) or (c < m_lod and prev_c >= m_lod and c < d_l):
                        if c < vw:
                            pe_cand = True
                            sl_val = round(h + 5.0, 1)

                elif in_w2:
                    session_high = float(day_bars.iloc[:idx+1]["high"].max())
                    session_low = float(day_bars.iloc[:idx+1]["low"].min())
                    day_spread = session_high - session_low
                    
                    # Lockout if day moved > 250 points
                    if day_spread >= 250.0:
                        continue

                    # Lockout if overextended from VWAP by > 80 points
                    if abs(c - vw) > 80.0:
                        continue

                    prior_4_high = float(day_bars.iloc[max(0, idx - 4):idx]["high"].max())
                    is_exhausted_pole = (prior_4_high - c) > 100.0

                    if c > m_hod and prev_c <= m_hod and c > d_h:
                        potential_tgs = self.compute_all_confluence_targets(day_bars.iloc[:idx+1], c, is_ce=True, custom_pivots=day_pivots, custom_orb=day_orb)
                        runway = (potential_tgs[0] - c) if potential_tgs else 0.0
                        if runway >= 70.0:
                            ce_cand = True
                            sl_val = round(l - 5.0, 1)

                    elif c < m_lod and prev_c >= m_lod and c < d_l and not is_exhausted_pole:
                        potential_tgs = self.compute_all_confluence_targets(day_bars.iloc[:idx+1], c, is_ce=False, custom_pivots=day_pivots, custom_orb=day_orb)
                        runway = (c - potential_tgs[0]) if potential_tgs else 0.0
                        if runway >= 70.0:
                            pe_cand = True
                            sl_val = round(h + 5.0, 1)

                if ce_cand:
                    day_trade_count += 1
                    t_list = self.compute_all_confluence_targets(day_bars.iloc[:idx+1], c, is_ce=True, custom_pivots=day_pivots, custom_orb=day_orb)
                    self.markers.append({
                        "time": t_sec,
                        "position": "belowBar",
                        "color": "#00bfa5",
                        "shape": "arrowUp",
                        "text": f"BUY CE @ {c:.1f}",
                    })
                    self.history_cards[str(t_sec)] = {
                        "title": f"BUY CE [TRADE #{day_trade_count}]",
                        "entry": round(c, 1),
                        "sl": sl_val,
                        "targets": t_list,
                        "type": "CE",
                        "time_str": candle_close_time,
                        "date": d_str,
                        "date_readable": r["dt"].astimezone(IST).strftime("%d-%b-%Y (%A)"),
                        "closed": True,
                        "outcome": "HISTORICAL",
                    }
                    sim_active_trade = {"type": "CE", "entry": c, "sl": sl_val, "targets": t_list, "stage": 0, "peak": c, "entry_atr": bar_atr}

                elif pe_cand:
                    day_trade_count += 1
                    t_list = self.compute_all_confluence_targets(day_bars.iloc[:idx+1], c, is_ce=False, custom_pivots=day_pivots, custom_orb=day_orb)
                    self.markers.append({
                        "time": t_sec,
                        "position": "aboveBar",
                        "color": "#f23645",
                        "shape": "arrowDown",
                        "text": f"BUY PE @ {c:.1f}",
                    })
                    self.history_cards[str(t_sec)] = {
                        "title": f"BUY PE [TRADE #{day_trade_count}]",
                        "entry": round(c, 1),
                        "sl": sl_val,
                        "targets": t_list,
                        "type": "PE",
                        "time_str": candle_close_time,
                        "date": d_str,
                        "date_readable": r["dt"].astimezone(IST).strftime("%d-%b-%Y (%A)"),
                        "closed": True,
                        "outcome": "HISTORICAL",
                    }
                    sim_active_trade = {"type": "PE", "entry": c, "sl": sl_val, "targets": t_list, "stage": 0, "peak": c, "entry_atr": bar_atr}

        # Active Trade HUD Card
        if is_market_hours() and self.locked_trade and not self.locked_trade.get("closed"):
            tr = self.locked_trade
            t_type = tr["type"]
            entry_p = float(tr["entry"])
            sl_p = float(tr["init_sl"])
            trail_p = float(tr.get("current_sl", sl_p))
            target_list = tr.get("targets", [])
            opt_entry = float(tr.get("opt_entry_ltp", 75.0))
            delta_v = float(tr.get("delta", 0.50))
            sym = tr.get("symbol", f"NIFTY {t_type}")

            risk_pts = round(abs(entry_p - sl_p), 1)
            secured_pts = round(trail_p - entry_p, 1) if t_type == "CE" else round(entry_p - trail_p, 1)

            hud_targets = []
            for idx_tg, tg_spot in enumerate(target_list, start=1):
                tg_pts = round(abs(tg_spot - entry_p), 1)
                tg_opt = round(opt_entry + (tg_pts * delta_v), 2)
                hud_targets.append({
                    "label": f"Target {idx_tg}",
                    "spot": tg_spot,
                    "pts": tg_pts,
                    "opt": tg_opt,
                })

            self.hud_data = {
                "type": t_type,
                "symbol": sym,
                "delta": delta_v,
                "entry_spot": entry_p,
                "entry_opt": opt_entry,
                "sl_spot": sl_p,
                "sl_opt": max(0.5, round(opt_entry - (risk_pts * delta_v), 2)),
                "risk_pts": risk_pts,
                "targets": hud_targets,
                "trail_spot": trail_p,
                "trail_opt": max(0.5, round(opt_entry + (secured_pts * delta_v), 2)),
                "secured_pts": secured_pts,
                "date_readable": tr.get("date_readable", str(self.today)),
                "trade_closed": False,
            }
        else:
            self.hud_data = {}

    def background_option_poller(self):
        while True:
            time.sleep(15)
            if is_market_hours() and self.locked_trade and not self.locked_trade.get("closed") and self.locked_trade.get("token"):
                try:
                    res = self.api.ltpData("NFO", self.locked_trade["symbol"], self.locked_trade["token"])
                    if isinstance(res, dict) and res.get("status") and res.get("data"):
                        self.last_polled_opt_ltp = float(res["data"].get("ltp", 0.0))
                except Exception:
                    pass

    def on_tick(self, ltp):
        if not is_market_hours():
            return

        now = datetime.datetime.now(IST)
        bucket_min = (now.minute // 5) * 5
        bar_dt = now.replace(minute=bucket_min, second=0, microsecond=0)
        t_sec = int(bar_dt.timestamp())

        if self.active_bar is None or self.active_bar["time"] != t_sec:
            if self.active_bar:
                self.candles.append(self.active_bar.copy())
                self.run_full_strategy_pipeline()
                self.evaluate_live_breakout()
                self.sync_state(ltp, force=True)

            self.active_bar = {
                "time": t_sec,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "dt": bar_dt,
            }
        else:
            self.active_bar["high"] = max(self.active_bar["high"], ltp)
            self.active_bar["low"] = min(self.active_bar["low"], ltp)
            self.active_bar["close"] = ltp

        self.evaluate_live_trail_and_exit(ltp)
        self.sync_state(ltp)

    def evaluate_live_breakout(self):
        if not is_market_hours() or self.locked_trade or self.trades_taken_today >= 2 or len(self.candles) < 4:
            return

        last_c = self.candles[-1]
        prev_c = self.candles[-2]
        t_open = last_c["dt"].astimezone(IST).time()
        close_p = last_c["close"]
        high_p = last_c["high"]
        low_p = last_c["low"]
        vwap_val = last_c.get("vwap", close_p)

        in_w1 = datetime.time(9, 30) <= t_open <= datetime.time(11, 15)
        in_w2 = datetime.time(12, 45) <= t_open <= datetime.time(13, 45)

        if not (in_w1 or in_w2):
            return

        orb_h = self.orb_levels.get("orb_h", 999999)
        orb_l = self.orb_levels.get("orb_l", 0)

        df_curr = pd.DataFrame(self.candles)
        df_prior = df_curr.iloc[:-1]
        session_hod_prior = float(df_prior["high"].max()) if len(df_prior) > 0 else orb_h
        session_lod_prior = float(df_prior["low"].min()) if len(df_prior) > 0 else orb_l

        trade_num = self.trades_taken_today + 1
        ce_candidate = False
        pe_candidate = False
        setup_title = "ORB BREAKOUT"
        sl_val = 0.0

        if in_w1:
            if close_p > orb_h and prev_c["close"] <= orb_h:
                ce_candidate = True
                setup_title = "ORB HIGH BREAKOUT"
                sl_val = round(low_p - 5.0, 1)
            elif (close_p < orb_l and prev_c["close"] >= orb_l) or (close_p < session_lod_prior and prev_c["close"] >= session_lod_prior and close_p < orb_l):
                if close_p < vwap_val:
                    pe_candidate = True
                    setup_title = "ORB LOW BREAKDOWN"
                    sl_val = round(high_p + 5.0, 1)

        elif in_w2:
            session_high = float(df_curr["high"].max())
            session_low = float(df_curr["low"].min())
            day_spread = session_high - session_low
            
            # Exhaustion Filters
            if day_spread >= 250.0:
                print(f"[W2 Blocked]: Daily spread {day_spread:.1f}p exceeds 250p exhaustion limit.")
                return

            if abs(close_p - vwap_val) > 80.0:
                print(f"[W2 Blocked]: Overextended from VWAP by {abs(close_p - vwap_val):.1f}p.")
                return

            prior_4_high = float(df_curr.iloc[-4:]["high"].max()) if len(df_curr) >= 4 else high_p
            is_exhausted_pole = (prior_4_high - close_p) > 100.0

            if close_p > session_hod_prior and prev_c["close"] <= session_hod_prior and close_p > orb_h:
                potential_targets = self.compute_all_confluence_targets(df_curr, close_p, is_ce=True)
                runway = (potential_targets[0] - close_p) if potential_targets else 0.0
                if runway < 70.0:
                    print(f"[W2 Blocked]: Target 1 runway only {runway:.1f}p (minimum 70p required).")
                    return
                ce_candidate = True
                setup_title = "HOD BREAKOUT (W2)"
                sl_val = round(low_p - 5.0, 1)

            elif close_p < session_lod_prior and prev_c["close"] >= session_lod_prior and close_p < orb_l and not is_exhausted_pole:
                potential_targets = self.compute_all_confluence_targets(df_curr, close_p, is_ce=False)
                runway = (close_p - potential_targets[0]) if potential_targets else 0.0
                if runway < 70.0:
                    print(f"[W2 Blocked]: Target 1 runway only {runway:.1f}p (minimum 70p required).")
                    return
                pe_candidate = True
                setup_title = "LOD BREAKDOWN (W2)"
                sl_val = round(high_p + 5.0, 1)

        if ce_candidate:
            self.trades_taken_today += 1
            strike, sym, tok, delta, days_exp = self.find_budget_strike(close_p, is_ce=True)
            opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else 75.0
            risk_pts = round(close_p - sl_val, 1)
            opt_sl = max(0.5, round(opt_ltp - (risk_pts * delta), 2))
            targets = self.compute_all_confluence_targets(df_curr, close_p, is_ce=True)

            date_readable = last_c["dt"].astimezone(IST).strftime("%d-%b-%Y (%A)")
            close_time_str = (last_c["dt"].astimezone(IST) + datetime.timedelta(minutes=5)).strftime("%H:%M:%S")
            window_label = "WINDOW 1 (MORNING DRIVE)" if in_w1 else "WINDOW 2 (AFTERNOON EXPANSION)"

            self.locked_trade = {
                "type": "CE",
                "trade_num": trade_num,
                "strike": strike,
                "entry": close_p,
                "init_sl": sl_val,
                "current_sl": sl_val,
                "last_alerted_sl": sl_val,
                "targets": targets,
                "trail_stage": 0,
                "peak": close_p,
                "entry_atr": self.current_atr5,
                "closed": False,
                "symbol": sym,
                "token": tok,
                "delta": delta,
                "opt_entry_ltp": opt_ltp,
                "opt_sl": opt_sl,
                "time": last_c["time"],
                "date_readable": date_readable,
            }

            budget_val = int(round(get_dynamic_budget()))
            total_risk_inr = int(round(risk_pts * delta * LOT_SIZE_QTY))
            tg_lines = "".join([f"• <b>TARGET {i}:</b> {tg:.1f} (+{abs(tg-close_p):.1f} pts) | <b>₹{round(opt_ltp+(abs(tg-close_p)*delta),2)}</b>\n" for i, tg in enumerate(targets, 1)])

            send_telegram_alert(
                f"🚀 <b>{setup_title}: BUY CE [TRADE #{trade_num}/2]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>SESSION:</b> {window_label}\n"
                f"<b>CONTRACT:</b> {sym} (Qty: {LOT_SIZE_QTY}, Δ: {delta:.2f})\n"
                f"• <b>ENTRY:</b> {close_p:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                f"• <b>STOP LOSS:</b> {sl_val:.1f} (-{risk_pts:.1f} pts) | <b>₹{opt_sl:.2f}</b> (-₹{total_risk_inr})\n"
                f"{tg_lines}"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡 <b>SL Anchor:</b> Breakout Low - 5.0 pts\n"
                f"⏰ <b>Trigger Time:</b> {close_time_str}"
            )
            return

        if pe_candidate:
            self.trades_taken_today += 1
            strike, sym, tok, delta, days_exp = self.find_budget_strike(close_p, is_ce=False)
            opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else 75.0
            risk_pts = round(sl_val - close_p, 1)
            opt_sl = max(0.5, round(opt_ltp - (risk_pts * delta), 2))
            targets = self.compute_all_confluence_targets(df_curr, close_p, is_ce=False)

            date_readable = last_c["dt"].astimezone(IST).strftime("%d-%b-%Y (%A)")
            close_time_str = (last_c["dt"].astimezone(IST) + datetime.timedelta(minutes=5)).strftime("%H:%M:%S")
            window_label = "WINDOW 1 (MORNING DRIVE)" if in_w1 else "WINDOW 2 (AFTERNOON EXPANSION)"

            self.locked_trade = {
                "type": "PE",
                "trade_num": trade_num,
                "strike": strike,
                "entry": close_p,
                "init_sl": sl_val,
                "current_sl": sl_val,
                "last_alerted_sl": sl_val,
                "targets": targets,
                "trail_stage": 0,
                "peak": close_p,
                "entry_atr": self.current_atr5,
                "closed": False,
                "symbol": sym,
                "token": tok,
                "delta": delta,
                "opt_entry_ltp": opt_ltp,
                "opt_sl": opt_sl,
                "time": last_c["time"],
                "date_readable": date_readable,
            }

            budget_val = int(round(get_dynamic_budget()))
            total_risk_inr = int(round(risk_pts * delta * LOT_SIZE_QTY))
            tg_lines = "".join([f"• <b>TARGET {i}:</b> {tg:.1f} (+{abs(close_p-tg):.1f} pts) | <b>₹{round(opt_ltp+(abs(close_p-tg)*delta),2)}</b>\n" for i, tg in enumerate(targets, 1)])

            send_telegram_alert(
                f"🚀 <b>{setup_title}: BUY PE [TRADE #{trade_num}/2]</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>SESSION:</b> {window_label}\n"
                f"<b>CONTRACT:</b> {sym} (Qty: {LOT_SIZE_QTY}, Δ: {delta:.2f})\n"
                f"• <b>ENTRY:</b> {close_p:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                f"• <b>STOP LOSS:</b> {sl_val:.1f} (-{risk_pts:.1f} pts) | <b>₹{opt_sl:.2f}</b> (-₹{total_risk_inr})\n"
                f"{tg_lines}"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡 <b>SL Anchor:</b> Breakout High + 5.0 pts\n"
                f"⏰ <b>Trigger Time:</b> {close_time_str}"
            )
            return

    def evaluate_live_trail_and_exit(self, live_spot):
        if not is_market_hours() or not self.locked_trade or self.locked_trade.get("closed"):
            return

        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        entry_p = tr["entry"]
        delta_v = tr.get("delta", 0.50)
        opt_entry = tr.get("opt_entry_ltp", 75.0)
        targets = tr.get("targets", [])
        trade_atr = tr.get("entry_atr", self.current_atr5)

        t_now = datetime.datetime.now(IST).strftime("%H:%M:%S")
        date_readable = tr.get("date_readable", datetime.datetime.now(IST).strftime("%d-%b-%Y (%A)"))

        stop_hit = (live_spot <= tr["current_sl"]) if is_ce else (live_spot >= tr["current_sl"])
        if stop_hit:
            exit_spot = tr["current_sl"]
            final_pts = round((exit_spot - entry_p) if is_ce else (entry_p - exit_spot), 1)
            final_inr = int(round(final_pts * delta_v * LOT_SIZE_QTY))
            sign = "+" if final_pts >= 0 else ""
            trade_n = tr.get("trade_num", 1)
            outcome_str = "PROFIT" if final_pts > 0 else "LOSS"

            self.trade_history_today.append({
                "trade_num": trade_n,
                "type": tr["type"],
                "outcome": outcome_str,
                "pnl_pts": final_pts,
                "pnl_inr": final_inr
            })

            tr["closed"] = True
            self.locked_trade = None
            self.hud_data = {}

            can_reenter = (self.trades_taken_today < 2)
            next_status = "Armed for Trade #2 (Window 2: 12:45-13:45)." if can_reenter else "Daily limit reached."

            send_telegram_alert(
                f"🏁 <b>TRADE #{trade_n} CLOSED (STOP HIT)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🚪 <b>Exit Price (Spot):</b> {exit_spot:.1f}\n"
                f"💰 <b>Final Net Spot:</b> {sign}{final_pts:.1f} pts (₹{sign}{final_inr})\n"
                f"📋 <b>Next Action:</b> {next_status}\n"
                f"📅 <b>Date:</b> {date_readable} | ⏰ <b>Exit Time:</b> {t_now}"
            )
            return

        if is_ce:
            tr["peak"] = max(tr.get("peak", entry_p), live_spot)
            peak_gain = tr["peak"] - entry_p
        else:
            tr["peak"] = min(tr.get("peak", entry_p), live_spot)
            peak_gain = entry_p - tr["peak"]

        sl_updated = False
        update_reason = ""

        # STAGE 1: Peak Gain >= 2.5x ATR -> Trail at 0.500 Fib Pullback - 0.5x ATR
        if peak_gain >= (2.5 * trade_atr) and tr.get("trail_stage", 0) < 1:
            tr["trail_stage"] = 1
            if is_ce:
                fib_50 = tr["peak"] - (0.500 * peak_gain)
                candidate_sl = round(fib_50 - (0.5 * trade_atr), 1)
                if candidate_sl > tr["current_sl"]:
                    tr["current_sl"] = candidate_sl
                    update_reason = f"STAGE 1 (FIB 0.500 PULLBACK + ATR): LOCKED {candidate_sl:.1f}"
                    sl_updated = True
            else:
                fib_50 = tr["peak"] + (0.500 * peak_gain)
                candidate_sl = round(fib_50 + (0.5 * trade_atr), 1)
                if candidate_sl < tr["current_sl"]:
                    tr["current_sl"] = candidate_sl
                    update_reason = f"STAGE 1 (FIB 0.500 PULLBACK + ATR): LOCKED {candidate_sl:.1f}"
                    sl_updated = True

        # STAGE 2: Peak Gain >= 4.5x ATR -> Tighten to 0.382 Fib Pullback - 0.5x ATR
        if peak_gain >= (4.5 * trade_atr) and tr.get("trail_stage", 0) < 2:
            tr["trail_stage"] = 2
            if is_ce:
                fib_382 = tr["peak"] - (0.382 * peak_gain)
                candidate_sl = round(fib_382 - (0.5 * trade_atr), 1)
                if candidate_sl > tr["current_sl"]:
                    tr["current_sl"] = candidate_sl
                    update_reason = f"STAGE 2 (FIB 0.382 PULLBACK + ATR): TIGHTENED {candidate_sl:.1f}"
                    sl_updated = True
            else:
                fib_382 = tr["peak"] + (0.382 * peak_gain)
                candidate_sl = round(fib_382 + (0.5 * trade_atr), 1)
                if candidate_sl < tr["current_sl"]:
                    tr["current_sl"] = candidate_sl
                    update_reason = f"STAGE 2 (FIB 0.382 PULLBACK + ATR): TIGHTENED {candidate_sl:.1f}"
                    sl_updated = True

        # STAGE 3: RUNNER BEYOND TARGET 1 -> PRIOR BAR LOW + 2.0 (CE) / PRIOR BAR HIGH - 2.0 (PE)
        t1_dist = abs(targets[0] - entry_p) if targets else 75.0
        if peak_gain >= t1_dist and len(self.candles) >= 2:
            prev_closed = self.candles[-1]
            if is_ce:
                candidate_sl = round(float(prev_closed["low"]) + 2.0, 1)
                if candidate_sl > tr["current_sl"]:
                    tr["current_sl"] = candidate_sl
                    update_reason = f"STAGE 3 (RUNNER): PRIOR BAR LOW + 2.0 LOCKED ({candidate_sl:.1f})"
                    sl_updated = True
            else:
                candidate_sl = round(float(prev_closed["high"]) - 2.0, 1)
                if candidate_sl < tr["current_sl"]:
                    tr["current_sl"] = candidate_sl
                    update_reason = f"STAGE 3 (RUNNER): PRIOR BAR HIGH - 2.0 LOCKED ({candidate_sl:.1f})"
                    sl_updated = True

        last_alerted = tr.get("last_alerted_sl", tr["init_sl"])
        if sl_updated or abs(tr["current_sl"] - last_alerted) >= 4.0:
            tr["last_alerted_sl"] = tr["current_sl"]
            cur_sl = tr["current_sl"]

            locked_pts = round((cur_sl - entry_p) if is_ce else (entry_p - cur_sl), 1)
            locked_inr = int(round(locked_pts * delta_v * LOT_SIZE_QTY))
            gtt_opt = max(0.5, round(opt_entry + (locked_pts * delta_v), 2))
            sign = "+" if locked_pts >= 0 else ""

            send_telegram_alert(
                f"🔔 <b>NIFTY {tr['type']} TRAILING SL UPDATED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>STAGE:</b> {update_reason}\n"
                f"<b>CONTRACT:</b> {tr['symbol']}\n"
                f"🛡 <b>UPDATE STOP LOSS (GTT) TO:</b>\n"
                f"• <b>INDEX (SPOT):</b> {cur_sl:.1f}\n"
                f"• <b>OPTION (GTT ₹):</b> ₹{gtt_opt:.2f}\n"
                f"🔒 <b>LOCKED PROFIT:</b> {sign}{locked_pts:.1f} pts (₹{sign}{locked_inr})\n"
                f"📅 <b>Date:</b> {date_readable} | ⏰ <b>Time:</b> {t_now}"
            )

    def sync_state(self, live_ltp, force=False):
        now_ts = time.time()
        if not force and (now_ts - self.last_push_time < 1.0):
            return
        self.last_push_time = now_ts

        opt_ltp = 0.0
        if is_market_hours() and self.locked_trade and not self.locked_trade.get("closed"):
            pts_gain = (live_ltp - self.locked_trade["entry"]) if self.locked_trade["type"] == "CE" else (self.locked_trade["entry"] - live_ltp)
            opt_ltp = max(0.5, round(self.locked_trade.get("opt_entry_ltp", 75.0) + (pts_gain * self.locked_trade.get("delta", 0.50)), 2))

        # Send 140 most recent bars to avoid payload drops in Upstash Redis
        recent_candles = [
            {
                "time": c["time"],
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "vwap": c.get("vwap", c["close"]),
                "ema9": c.get("ema9", c["close"]),
            }
            for c in self.candles[-140:]
        ]
        if self.active_bar:
            recent_candles.append({
                "time": self.active_bar["time"],
                "open": self.active_bar["open"],
                "high": self.active_bar["high"],
                "low": self.active_bar["low"],
                "close": self.active_bar["close"],
                "vwap": recent_candles[-1]["vwap"] if recent_candles else self.active_bar["close"],
                "ema9": recent_candles[-1]["ema9"] if recent_candles else self.active_bar["close"],
            })

        all_trades_done = (self.trades_taken_today >= 2)

        payload = {
            "ltp": live_ltp,
            "opt_ltp": opt_ltp,
            "candles": recent_candles,
            "locked_trade": self.locked_trade if (is_market_hours() and self.locked_trade and not self.locked_trade.get("closed")) else None,
            "trades_taken_today": self.trades_taken_today,
            "trade_history_today": self.trade_history_today,
            "trade_closed": all_trades_done and (self.locked_trade is None),
            "trade_date": self.today.strftime("%Y-%m-%d"),
            "markers": self.markers,
            "history_cards": self.history_cards,
            "pivots": self.pivot_lines,
            "orb_levels": self.orb_levels,
            "fib_lines": self.fib_lines,
            "hud": self.hud_data if is_market_hours() else {},
            "updated_at": time.time(),
        }
        push_to_redis("nifty_live_state", payload)


# ==========================================
# 4. Supervisor Loop
# ==========================================
def run_worker():
    while True:
        try:
            print("[Supervisor]: Initializing SmartAPI Session...")
            engine = DedicatedTradingWorker()

            def schedule_checker():
                while True:
                    time.sleep(30)
                    now = datetime.datetime.now(IST)
                    if engine.last_login_date != now.date() and now.time() >= datetime.time(8, 30):
                        engine.today = now.date()
                        engine.authenticate_angel_one()
                        engine.locked_trade = None
                        engine.trades_taken_today = 0
                        engine.trade_history_today = []
                        engine.nfo_df = engine.load_scrip_master()
                        engine.init_historical_candles()
                        engine.run_full_strategy_pipeline()
                        try:
                            sws.close_connection()
                        except Exception:
                            pass
                        break

            threading.Thread(target=schedule_checker, daemon=True).start()

            sws = SmartWebSocketV2(engine.auth_token, API_KEY, CLIENT_CODE, engine.feed_token)

            def on_data(wsapp, msg):
                if isinstance(msg, dict) and "last_traded_price" in msg:
                    ltp = float(msg["last_traded_price"]) / 100.0
                    engine.on_tick(ltp)

            def on_open(wsapp):
                print("[WebSocket]: Connected & Subscribed to Nifty Index Token 99926000")
                sws.subscribe("correl_id_1", 1, [{"exchangeType": 1, "tokens": [INDEX_TOKEN]}])

            def on_error(wsapp, error):
                print(f"[WebSocket Error]: {error}")

            def on_close(wsapp):
                print("[WebSocket Closed]: Connection dropped.")

            sws.on_open = on_open
            sws.on_data = on_data
            sws.on_error = on_error
            sws.on_close = on_close
            sws._on_error = on_error
            sws._on_close = on_close

            sws.connect()

        except Exception as e:
            print(f"[Supervisor Error]: {e}. Reconnecting in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    run_worker()
