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

VOL_THRESHOLD = 229.0
MAX_BREACH_MULT = 0.5
ATR_TARGET_MULT = 3.0
SL_BUFFER = 5.0


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
    if not is_market_hours() or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
    except Exception as e:
        print(f"[Redis Push Error]: {e}")


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
        self.contract_preview = {}
        self.prev_day_summary = {}

        self.locked_trade = None
        self.today_trade_record = None  # Permanent container for today's trade
        self.trades_taken_today = 0
        self.trade_history_today = []

        self.daily_atr10 = 150.0
        self.current_atr14 = 14.0
        self.is_high_vol = False
        self.pierced_orbH = False
        self.pierced_orbL = False
        self.max_high_outside = 0.0
        self.min_low_outside = 999999.0

        self.auth_token = None
        self.feed_token = None
        self.last_login_date = None
        self.last_polled_opt_ltp = 0.0
        self.last_push_time = 0.0

        self.authenticate_angel_one()

        today_str = self.today.strftime("%Y-%m-%d")
        saved_state = read_from_redis("nifty_live_state") or {}

        # Restore today's state if re-starting mid-day
        if saved_state.get("trade_date") == today_str:
            self.trades_taken_today = saved_state.get("trades_taken_today", 0)
            self.today_trade_record = saved_state.get("today_trade_record")
            if saved_state.get("locked_trade") and not saved_state.get("locked_trade", {}).get("closed", False):
                self.locked_trade = saved_state.get("locked_trade")
        else:
            self.trades_taken_today = 0
            self.today_trade_record = None

        self.nfo_df = self.load_scrip_master()
        self.fetch_daily_atr_and_levels()
        self.init_historical_candles()
        self.run_full_strategy_pipeline()
        self.simulate_previous_day_trades()
        self.update_pretrade_contract_forecast()

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

    def fetch_daily_atr_and_levels(self):
        now = datetime.datetime.now(IST)
        from_str = (now - datetime.timedelta(days=25)).strftime("%Y-%m-%d 09:15")
        to_str = now.strftime("%Y-%m-%d 15:30")
        try:
            resp = self.api.getCandleData({
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "ONE_DAY",
                "fromdate": from_str,
                "todate": to_str,
            })
            if resp and resp.get("status") and resp.get("data") and len(resp["data"]) >= 11:
                df_d = pd.DataFrame(resp["data"], columns=["time", "open", "high", "low", "close", "vol"])
                df_d["high"] = df_d["high"].astype(float)
                df_d["low"] = df_d["low"].astype(float)
                df_d["close"] = df_d["close"].astype(float)

                df_closed = df_d.iloc[:-1].copy()

                tr1 = df_closed["high"] - df_closed["low"]
                tr2 = (df_closed["high"] - df_closed["close"].shift(1)).abs()
                tr3 = (df_closed["low"] - df_closed["close"].shift(1)).abs()
                df_closed["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

                atr_series = df_closed["tr"].rolling(10).mean()
                self.daily_atr10 = round(float(atr_series.iloc[-1]), 1)
                self.is_high_vol = (self.daily_atr10 >= VOL_THRESHOLD)

                prev_bar = df_closed.iloc[-1]
                pdh = round(float(prev_bar["high"]), 2)
                pdl = round(float(prev_bar["low"]), 2)
                pdc = round(float(prev_bar["close"]), 2)
                P = round((pdh + pdl + pdc) / 3.0, 2)
                rng = round(pdh - pdl, 2)

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

                p_mid = round((pdh + pdl) / 2.0, 2)
                self.fib_lines = {
                    "ORB_MID": float(p_mid),
                    "fib_50": float(round(pdh + (0.500 * rng), 2)),
                    "fib_100": float(round(pdh + (1.000 * rng), 2)),
                    "fib_161": float(round(pdh + (1.618 * rng), 2)),
                    "direction": "UP",
                }
                print(f"[Daily ATR Loaded]: 10-ATR={self.daily_atr10} | Mode={'BO' if self.is_high_vol else 'FADE'}")
                return
        except Exception as e:
            print(f"[Daily ATR Error]: {e}")
            self.daily_atr10 = 160.0
            self.is_high_vol = False

    def init_historical_candles(self):
        now = datetime.datetime.now(IST)
        for days_back in [5, 3]:
            from_dt = now - datetime.timedelta(days=days_back)
            from_str = from_dt.strftime("%Y-%m-%d 09:15")
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
                        dt_ist = dt_parsed.tz_localize(IST) if dt_parsed.tzinfo is None else dt_parsed.astimezone(IST)
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
                    print(f"[Candles Loaded]: {len(self.candles)} candles initialized.")
                    break
            except Exception as e:
                print(f"[Candles Error]: {e}")

    def find_budget_strike(self, spot_ref, is_ce, budget_target=None):
        if budget_target is None:
            budget_target = get_dynamic_budget()

        atm = int(round(spot_ref / 50.0) * 50)
        opt_type = "CE" if is_ce else "PE"

        if self.nfo_df.empty:
            return atm, f"NIFTY {atm} {opt_type}", "", 0.50, 4, 75.0

        future_exp = self.nfo_df[self.nfo_df["exp_dt"].dt.date >= self.today].sort_values("exp_dt")
        if future_exp.empty:
            return atm, f"NIFTY {atm} {opt_type}", "", 0.50, 4, 75.0

        curr_exp = future_exp["expiry"].iloc[0]
        exp_slice = future_exp[future_exp["expiry"] == curr_exp]
        days_to_exp = max(1, (exp_slice.iloc[0]["exp_dt"].date() - self.today).days)

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
            chosen_ltp = 75.0

        delta = compute_contract_delta(spot_ref, chosen_strike, days_to_exp, is_ce=is_ce)
        return chosen_strike, chosen_sym, chosen_tok, delta, days_to_exp, chosen_ltp

    def simulate_previous_day_trades(self):
        if not self.candles:
            return

        df_h = pd.DataFrame(self.candles)
        df_h["date_str"] = df_h["dt"].apply(lambda d: d.astimezone(IST).strftime("%Y-%m-%d"))
        unique_days = list(df_h["date_str"].unique())

        if len(unique_days) < 2:
            return

        prev_day_str = unique_days[-2]
        prev_bars = df_h[df_h["date_str"] == prev_day_str].sort_values("time").reset_index(drop=True)

        if len(prev_bars) < 4:
            return

        ow = prev_bars.iloc[0:3]
        p_orb_h = round(float(ow["high"].max()), 2)
        p_orb_l = round(float(ow["low"].min()), 2)

        p_pierced_h = False
        p_pierced_l = False
        p_max_h_outside = p_orb_h
        p_min_l_outside = p_orb_l

        sim_trade = None
        trade_fired = False

        for idx in range(3, len(prev_bars)):
            r = prev_bars.iloc[idx]
            b_time = r["dt"].astimezone(IST).time()
            c = float(r["close"])
            o = float(r["open"])
            h = float(r["high"])
            l = float(r["low"])
            vw = float(r["vwap"])
            bar_atr = float(r.get("atr14", 14.0))
            t_sec = int(r["time"])
            time_label = (r["dt"].astimezone(IST) + datetime.timedelta(minutes=5)).strftime("%H:%M")

            if sim_trade is not None:
                is_ce = (sim_trade["type"] == "CE")
                entry_p = sim_trade["entry"]
                cur_sl = sim_trade["current_sl"]
                target_p = sim_trade["target_p"]
                mode = sim_trade["mode"]
                trade_atr = sim_trade["atr"]

                exit_triggered = False
                exit_price = 0.0
                exit_reason = ""

                if mode == "FADE":
                    if (is_ce and h >= target_p) or ((not is_ce) and l <= target_p):
                        exit_triggered = True
                        exit_price = target_p
                        exit_reason = "TARGET HIT (VWAP)"

                if not exit_triggered:
                    if (is_ce and l <= cur_sl) or ((not is_ce) and h >= cur_sl):
                        exit_triggered = True
                        exit_price = cur_sl
                        exit_reason = "STOP LOSS HIT"

                if not exit_triggered and b_time >= datetime.time(15, 15):
                    exit_triggered = True
                    exit_price = c
                    exit_reason = "15:15 EOD SQUARE-OFF"

                if exit_triggered:
                    final_pts = round((exit_price - entry_p) if is_ce else (entry_p - exit_price), 1)
                    final_inr = int(round(final_pts * sim_trade["delta"] * LOT_SIZE_QTY))
                    sign = "+" if final_pts >= 0 else ""

                    self.markers.append({
                        "time": t_sec,
                        "position": "aboveBar" if is_ce else "belowBar",
                        "color": "#ffa726",
                        "shape": "square",
                        "text": f"EXIT {sim_trade['type']}: {sign}{final_pts}p",
                    })

                    self.prev_day_summary = {
                        "date": prev_day_str,
                        "regime": "BREAKOUT" if self.is_high_vol else "FADE",
                        "type": sim_trade["type"],
                        "entry_time": sim_trade["time_label"],
                        "entry_spot": entry_p,
                        "opt_entry": sim_trade["opt_entry"],
                        "init_sl": sim_trade["init_sl"],
                        "final_trail_sl": cur_sl,
                        "target_p": target_p,
                        "exit_time": time_label,
                        "exit_spot": exit_price,
                        "final_pts": final_pts,
                        "final_inr": final_inr,
                        "exit_reason": exit_reason,
                    }
                    sim_trade = None
                    break

                if mode == "BREAKOUT":
                    old_sl = sim_trade["current_sl"]
                    if is_ce:
                        sim_trade["peak"] = max(sim_trade["peak"], h)
                        gain_l = sim_trade["peak"] - entry_p
                        if gain_l >= sim_trade["bank_dist"]:
                            sim_trade["bank_achieved"] = True
                        if sim_trade["bank_achieved"]:
                            fib_tight_sl = (sim_trade["peak"] - (0.236 * gain_l)) - (0.3 * trade_atr)
                            min_guar_sl = entry_p + sim_trade["bank_dist"] - (0.3 * trade_atr)
                            prev_bar_sl = float(prev_bars.iloc[idx - 1]["low"]) + 2.0
                            sim_trade["current_sl"] = max(sim_trade["current_sl"], max(fib_tight_sl, max(min_guar_sl, prev_bar_sl)))
                    else:
                        sim_trade["peak"] = min(sim_trade["peak"], l)
                        gain_s = entry_p - sim_trade["peak"]
                        if gain_s >= sim_trade["bank_dist"]:
                            sim_trade["bank_achieved"] = True
                        if sim_trade["bank_achieved"]:
                            fib_tight_sl = (sim_trade["peak"] + (0.236 * gain_s)) + (0.3 * trade_atr)
                            min_guar_sl = entry_p - sim_trade["bank_dist"] + (0.3 * trade_atr)
                            prev_bar_sl = float(prev_bars.iloc[idx - 1]["high"]) - 2.0
                            sim_trade["current_sl"] = min(sim_trade["current_sl"], min(fib_tight_sl, min(min_guar_sl, prev_bar_sl)))

                    if abs(sim_trade["current_sl"] - old_sl) >= 4.0:
                        locked_pts = round((sim_trade["current_sl"] - entry_p) if is_ce else (entry_p - sim_trade["current_sl"]), 1)
                        self.markers.append({
                            "time": t_sec,
                            "position": "belowBar" if is_ce else "aboveBar",
                            "color": "#00e676",
                            "shape": "circle",
                            "text": f"TRAIL SL: {sim_trade['current_sl']:.1f} (+{locked_pts}p)",
                        })
                continue

            if not trade_fired and datetime.time(9, 30) <= b_time < datetime.time(10, 0):
                if h > p_orb_h:
                    p_pierced_h = True
                    p_max_h_outside = max(p_max_h_outside, h)
                if l < p_orb_l:
                    p_pierced_l = True
                    p_min_l_outside = min(p_min_l_outside, l)

                fire_ce_bo = False
                fire_pe_bo = False
                fire_pe_fd = False
                fire_ce_fd = False

                if self.is_high_vol:
                    if c > p_orb_h and c > vw and c > o:
                        fire_ce_bo = True
                    elif c < p_orb_l and c < vw and c < o:
                        fire_pe_bo = True
                else:
                    shallow_h = (p_max_h_outside - p_orb_h) <= (MAX_BREACH_MULT * bar_atr)
                    shallow_l = (p_orb_l - p_min_l_outside) <= (MAX_BREACH_MULT * bar_atr)
                    decisive_pe = c <= (p_orb_h - (0.2 * bar_atr)) and c < o
                    decisive_ce = c >= (p_orb_l + (0.2 * bar_atr)) and c > o

                    if p_pierced_h and shallow_h and decisive_pe:
                        fire_pe_fd = True
                    if p_pierced_l and shallow_l and decisive_ce:
                        fire_ce_fd = True

                trade_type = None
                mode = ""
                sl_val = 0.0
                tg_val = 0.0
                bank_d = bar_atr * ATR_TARGET_MULT

                if fire_ce_bo:
                    trade_type = "CE"
                    mode = "BREAKOUT"
                    sl_val = round(l - SL_BUFFER, 1)
                    tg_val = round(c + bank_d, 1)
                elif fire_pe_bo:
                    trade_type = "PE"
                    mode = "BREAKOUT"
                    sl_val = round(h + SL_BUFFER, 1)
                    tg_val = round(c - bank_d, 1)
                elif fire_pe_fd:
                    trade_type = "PE"
                    mode = "FADE"
                    sl_val = round(p_max_h_outside + SL_BUFFER, 1)
                    tg_val = round(vw, 1)
                elif fire_ce_fd:
                    trade_type = "CE"
                    mode = "FADE"
                    sl_val = round(p_min_l_outside - SL_BUFFER, 1)
                    tg_val = round(vw, 1)

                if trade_type:
                    trade_fired = True
                    is_ce = (trade_type == "CE")
                    delta = 0.50
                    est_opt = 75.0

                    self.markers.append({
                        "time": t_sec,
                        "position": "belowBar" if is_ce else "aboveBar",
                        "color": "#00e676" if is_ce else "#f23645",
                        "shape": "arrowUp" if is_ce else "arrowDown",
                        "text": f"PREV DAY {mode} {trade_type} @ {c:.1f}",
                    })

                    sim_trade = {
                        "type": trade_type,
                        "mode": mode,
                        "entry": c,
                        "init_sl": sl_val,
                        "current_sl": sl_val,
                        "target_p": tg_val,
                        "bank_dist": bank_d,
                        "bank_achieved": False,
                        "peak": c,
                        "atr": bar_atr,
                        "delta": delta,
                        "opt_entry": est_opt,
                        "time_label": time_label,
                    }

    def update_pretrade_contract_forecast(self):
        if not self.candles:
            return

        ref_spot = float(self.candles[-1]["close"])
        orb_h = self.orb_levels.get("orb_h", ref_spot)
        orb_l = self.orb_levels.get("orb_l", ref_spot)
        vwap_val = float(self.candles[-1].get("vwap", ref_spot))
        atr_val = self.current_atr14

        k_ce, sym_ce, tok_ce, d_ce, _, ltp_ce = self.find_budget_strike(ref_spot, is_ce=True)
        k_pe, sym_pe, tok_pe, d_pe, _, ltp_pe = self.find_budget_strike(ref_spot, is_ce=False)

        if self.is_high_vol:
            ce_entry = orb_h + 1.0
            ce_sl = orb_l
            ce_tg = ce_entry + (atr_val * ATR_TARGET_MULT)

            pe_entry = orb_l - 1.0
            pe_sl = orb_h
            pe_tg = pe_entry - (atr_val * ATR_TARGET_MULT)
        else:
            ce_entry = orb_l + 3.0
            ce_sl = orb_l - (0.5 * atr_val) - SL_BUFFER
            ce_tg = vwap_val

            pe_entry = orb_h - 3.0
            pe_sl = orb_h + (0.5 * atr_val) + SL_BUFFER
            pe_tg = vwap_val

        self.contract_preview = {
            "CE": {
                "symbol": sym_ce,
                "delta": d_ce,
                "opt_ltp": ltp_ce,
                "exp_entry_spot": round(ce_entry, 1),
                "exp_sl_spot": round(ce_sl, 1),
                "exp_sl_opt": max(0.5, round(ltp_ce - (abs(ce_entry - ce_sl) * d_ce), 2)),
                "exp_target_spot": round(ce_tg, 1),
                "exp_target_opt": max(0.5, round(ltp_ce + (abs(ce_tg - ce_entry) * d_ce), 2)),
            },
            "PE": {
                "symbol": sym_pe,
                "delta": d_pe,
                "opt_ltp": ltp_pe,
                "exp_entry_spot": round(pe_entry, 1),
                "exp_sl_spot": round(pe_sl, 1),
                "exp_sl_opt": max(0.5, round(ltp_pe - (abs(pe_sl - pe_entry) * d_pe), 2)),
                "exp_target_spot": round(pe_tg, 1),
                "exp_target_opt": max(0.5, round(ltp_pe + (abs(pe_entry - pe_tg) * d_pe), 2)),
            },
        }

    def run_full_strategy_pipeline(self):
        if not self.candles:
            return

        df_h = pd.DataFrame(self.candles)
        df_h["date_str"] = df_h["dt"].apply(lambda d: d.astimezone(IST).strftime("%Y-%m-%d"))

        df_h["hlc3"] = (df_h["high"] + df_h["low"] + df_h["close"]) / 3.0
        candle_spread = (df_h["high"] - df_h["low"]) + (df_h["close"] - df_h["open"]).abs()
        df_h["calc_vol"] = candle_spread * 1250.0 + 500.0
        df_h["pv"] = df_h["hlc3"] * df_h["calc_vol"]
        df_h["cum_vol"] = df_h.groupby("date_str")["calc_vol"].cumsum()
        df_h["cum_pv"] = df_h.groupby("date_str")["pv"].cumsum()
        df_h["vwap"] = (df_h["cum_pv"] / df_h["cum_vol"]).round(2)

        tr1 = df_h["high"] - df_h["low"]
        tr2 = (df_h["high"] - df_h["close"].shift(1)).abs()
        tr3 = (df_h["low"] - df_h["close"].shift(1)).abs()
        df_h["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df_h["atr14"] = df_h["tr"].rolling(14).mean().fillna(14.0).round(2)
        self.current_atr14 = float(df_h["atr14"].iloc[-1])

        for i in range(len(self.candles)):
            self.candles[i]["vwap"] = float(df_h.iloc[i]["vwap"])
            self.candles[i]["atr14"] = float(df_h.iloc[i]["atr14"])

        unique_days = list(df_h["date_str"].unique())
        today_bars = df_h[df_h["date_str"] == unique_days[-1]].sort_values("time").reset_index(drop=True)

        orb_bars = today_bars.iloc[0:3] if len(today_bars) >= 3 else today_bars
        curr_orb_h = round(float(orb_bars["high"].max()), 2)
        curr_orb_l = round(float(orb_bars["low"].min()), 2)
        orb_range_val = max(15.0, round(curr_orb_h - curr_orb_l, 2))
        self.orb_levels = {"orb_h": curr_orb_h, "orb_l": curr_orb_l, "range": orb_range_val}

        orb_mid = round((curr_orb_h + curr_orb_l) / 2.0, 2)
        curr_price = float(today_bars.iloc[-1]["close"]) if len(today_bars) > 0 else curr_orb_h
        is_bullish_bias = (curr_price >= orb_mid)

        if is_bullish_bias:
            self.fib_lines = {
                "ORB_MID": float(orb_mid),
                "fib_50": float(round(curr_orb_h + (0.500 * orb_range_val), 2)),
                "fib_100": float(round(curr_orb_h + (1.000 * orb_range_val), 2)),
                "fib_161": float(round(curr_orb_h + (1.618 * orb_range_val), 2)),
                "direction": "UP",
            }
        else:
            self.fib_lines = {
                "ORB_MID": float(orb_mid),
                "fib_50": float(round(curr_orb_l - (0.500 * orb_range_val), 2)),
                "fib_100": float(round(curr_orb_l - (1.000 * orb_range_val), 2)),
                "fib_161": float(round(curr_orb_l - (1.618 * orb_range_val), 2)),
                "direction": "DOWN",
            }

        self.pierced_orbH = False
        self.pierced_orbL = False
        self.max_high_outside = curr_orb_h
        self.min_low_outside = curr_orb_l

        for idx in range(3, len(today_bars)):
            bar = today_bars.iloc[idx]
            b_time = bar["dt"].astimezone(IST).time()
            if datetime.time(9, 30) <= b_time < datetime.time(10, 0):
                if bar["high"] > curr_orb_h:
                    self.pierced_orbH = True
                    self.max_high_outside = max(self.max_high_outside, float(bar["high"]))
                if bar["low"] < curr_orb_l:
                    self.pierced_orbL = True
                    self.min_low_outside = min(self.min_low_outside, float(bar["low"]))

    def background_option_poller(self):
        while True:
            time.sleep(15)
            # Poll if today's trade exists (active or completed)
            contract_sym = None
            contract_tok = None
            if self.locked_trade and self.locked_trade.get("token"):
                contract_sym = self.locked_trade["symbol"]
                contract_tok = self.locked_trade["token"]
            elif self.today_trade_record and self.today_trade_record.get("token"):
                contract_sym = self.today_trade_record["symbol"]
                contract_tok = self.today_trade_record["token"]

            if is_market_hours() and contract_tok:
                try:
                    res = self.api.ltpData("NFO", contract_sym, contract_tok)
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

        # 15:15 IST Mandatory EOD Square-Off
        if now.time() >= datetime.time(15, 15) and self.locked_trade and not self.locked_trade.get("closed"):
            self.execute_eod_exit(ltp)
            return

        if self.active_bar is None or self.active_bar["time"] != t_sec:
            if self.active_bar:
                self.candles.append(self.active_bar.copy())
                self.run_full_strategy_pipeline()
                self.evaluate_dual_regime_entry()
                self.update_pretrade_contract_forecast()
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

    def evaluate_dual_regime_entry(self):
        # Strictly enforce 1 trade per day, entry window strictly 09:30 - 10:00 AM IST
        if not is_market_hours() or self.locked_trade or self.trades_taken_today >= 1 or len(self.candles) < 4:
            return

        last_c = self.candles[-1]
        t_open = last_c["dt"].astimezone(IST).time()
        close_p = last_c["close"]
        open_p = last_c["open"]
        high_p = last_c["high"]
        low_p = last_c["low"]
        vwap_val = last_c.get("vwap", close_p)
        atr_val = last_c.get("atr14", self.current_atr14)

        in_window = datetime.time(9, 30) <= t_open < datetime.time(10, 0)
        if not in_window:
            return

        orb_h = self.orb_levels.get("orb_h", 999999)
        orb_l = self.orb_levels.get("orb_l", 0)

        fire_ce_bo = False
        fire_pe_bo = False
        fire_pe_fade = False
        fire_ce_fade = False

        if self.is_high_vol:
            if close_p > orb_h and close_p > vwap_val and close_p > open_p:
                fire_ce_bo = True
            elif close_p < orb_l and close_p < vwap_val and close_p < open_p:
                fire_pe_bo = True
        else:
            shallow_high_breach = (self.max_high_outside - orb_h) <= (MAX_BREACH_MULT * atr_val)
            shallow_low_breach = (orb_l - self.min_low_outside) <= (MAX_BREACH_MULT * atr_val)

            decisive_pe_close = close_p <= (orb_h - (0.2 * atr_val)) and close_p < open_p
            decisive_ce_close = close_p >= (orb_l + (0.2 * atr_val)) and close_p > open_p

            if self.pierced_orbH and shallow_high_breach and decisive_pe_close:
                fire_pe_fade = True
            if self.pierced_orbL and shallow_low_breach and decisive_ce_close:
                fire_ce_fade = True

        trade_type = None
        current_mode = ""
        sl_val = 0.0
        target_val = 0.0
        bank_dist = atr_val * ATR_TARGET_MULT

        if fire_ce_bo:
            trade_type = "CE"
            current_mode = "BREAKOUT"
            sl_val = round(low_p - SL_BUFFER, 1)
            target_val = round(close_p + bank_dist, 1)
        elif fire_pe_bo:
            trade_type = "PE"
            current_mode = "BREAKOUT"
            sl_val = round(high_p + SL_BUFFER, 1)
            target_val = round(close_p - bank_dist, 1)
        elif fire_pe_fade:
            trade_type = "PE"
            current_mode = "FADE"
            sl_val = round(self.max_high_outside + SL_BUFFER, 1)
            target_val = round(vwap_val, 1)
        elif fire_ce_fade:
            trade_type = "CE"
            current_mode = "FADE"
            sl_val = round(self.min_low_outside - SL_BUFFER, 1)
            target_val = round(vwap_val, 1)

        if trade_type:
            self.trades_taken_today += 1
            is_ce = (trade_type == "CE")
            strike, sym, tok, delta, days_exp, chosen_ltp = self.find_budget_strike(close_p, is_ce=is_ce)
            opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else chosen_ltp
            risk_pts = round(abs(close_p - sl_val), 1)
            opt_sl = max(0.5, round(opt_ltp - (risk_pts * delta), 2))
            opt_target = max(0.5, round(opt_ltp + (abs(target_val - close_p) * delta), 2))

            date_readable = last_c["dt"].astimezone(IST).strftime("%d-%b-%Y (%A)")
            close_time_str = (last_c["dt"].astimezone(IST) + datetime.timedelta(minutes=5)).strftime("%H:%M:%S")

            trade_dict = {
                "type": trade_type,
                "mode": current_mode,
                "entry": close_p,
                "init_sl": sl_val,
                "current_sl": sl_val,
                "last_alerted_sl": sl_val,
                "target_p": target_val,
                "opt_target": opt_target,
                "bank_dist": bank_dist,
                "bank_achieved": False,
                "peak": close_p,
                "entry_atr": atr_val,
                "closed": False,
                "symbol": sym,
                "token": tok,
                "delta": delta,
                "opt_entry_ltp": opt_ltp,
                "opt_sl": opt_sl,
                "time": last_c["time"],
                "trigger_time": close_time_str,
                "date_readable": date_readable,
                "exit_spot": None,
                "exit_reason": None,
                "final_pts": 0.0,
                "final_inr": 0,
            }

            self.locked_trade = trade_dict
            self.today_trade_record = trade_dict.copy()

            regime_type_str = f"BREAKOUT DAY (10-ATR: {self.daily_atr10:.0f} ≥ {VOL_THRESHOLD:.0f})" if self.is_high_vol else f"FADE DAY (10-ATR: {self.daily_atr10:.0f} < {VOL_THRESHOLD:.0f})"
            tg_target_label = "3x ATR Runner Target" if current_mode == "BREAKOUT" else "Session VWAP Target"
            total_risk_inr = int(round(risk_pts * delta * LOT_SIZE_QTY))

            self.markers.append({
                "time": last_c["time"],
                "position": "belowBar" if is_ce else "aboveBar",
                "color": "#00bfa5" if is_ce else "#f23645",
                "shape": "arrowUp" if is_ce else "arrowDown",
                "text": f"{current_mode} {trade_type} @ {close_p:.1f}",
            })

            send_telegram_alert(
                f"⚡ <b>NIFTY DUAL-REGIME: BUY {trade_type} ({current_mode})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>REGIME:</b> <b>{regime_type_str}</b>\n"
                f"🎯 <b>LOGIC:</b> {'09:30-10:00 Momentum Breakout' if current_mode == 'BREAKOUT' else '0.5 ATR Sweep False Breakout Fade'}\n"
                f"📦 <b>CONTRACT:</b> {sym} (Qty: {LOT_SIZE_QTY}, Δ: {delta:.2f})\n"
                f"• <b>ENTRY (SPOT):</b> {close_p:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                f"• <b>INITIAL SL:</b> {sl_val:.1f} (-{risk_pts:.1f} pts) | <b>₹{opt_sl:.2f}</b> (-₹{total_risk_inr})\n"
                f"• <b>TARGET ({tg_target_label}):</b> {target_val:.1f} (+{abs(target_val-close_p):.1f} pts) | <b>₹{opt_target:.2f}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ <b>Trigger Time:</b> {close_time_str}"
            )

    def evaluate_live_trail_and_exit(self, live_spot):
        if not is_market_hours() or not self.locked_trade or self.locked_trade.get("closed"):
            return

        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        mode = tr.get("mode", "BREAKOUT")
        entry_p = tr["entry"]
        target_p = tr.get("target_p", entry_p)
        atr_val = tr.get("entry_atr", self.current_atr14)

        if mode == "FADE":
            fade_hit = (live_spot >= target_p) if is_ce else (live_spot <= target_p)
            if fade_hit:
                self.close_trade(target_p, "TARGET HIT (VWAP REACHED)")
                return

        stop_hit = (live_spot <= tr["current_sl"]) if is_ce else (live_spot >= tr["current_sl"])
        if stop_hit:
            self.close_trade(tr["current_sl"], "STOP LOSS HIT")
            return

        if mode == "BREAKOUT":
            if is_ce:
                tr["peak"] = max(tr.get("peak", entry_p), live_spot)
                gain_l = tr["peak"] - entry_p

                if gain_l >= tr["bank_dist"]:
                    tr["bank_achieved"] = True

                if tr["bank_achieved"]:
                    fib_tight_sl = (tr["peak"] - (0.236 * gain_l)) - (0.3 * atr_val)
                    min_guaranteed_sl = entry_p + tr["bank_dist"] - (0.3 * atr_val)
                    prev_low = float(self.candles[-1]["low"]) if self.candles else entry_p
                    bar_trail_sl = prev_low + 2.0

                    target_banked_sl = max(fib_tight_sl, max(min_guaranteed_sl, bar_trail_sl))
                    if target_banked_sl > tr["current_sl"]:
                        tr["current_sl"] = round(target_banked_sl, 1)
                        if self.today_trade_record:
                            self.today_trade_record["current_sl"] = tr["current_sl"]
                        self.alert_sl_update(tr, "BREAKOUT RUNNER TIGHTENED")

            else:
                tr["peak"] = min(tr.get("peak", entry_p), live_spot)
                gain_s = entry_p - tr["peak"]

                if gain_s >= tr["bank_dist"]:
                    tr["bank_achieved"] = True

                if tr["bank_achieved"]:
                    fib_tight_sl = (tr["peak"] + (0.236 * gain_s)) + (0.3 * atr_val)
                    min_guaranteed_sl = entry_p - tr["bank_dist"] + (0.3 * atr_val)
                    prev_high = float(self.candles[-1]["high"]) if self.candles else entry_p
                    bar_trail_sl = prev_high - 2.0

                    target_banked_sl = min(fib_tight_sl, min(min_guaranteed_sl, bar_trail_sl))
                    if target_banked_sl < tr["current_sl"]:
                        tr["current_sl"] = round(target_banked_sl, 1)
                        if self.today_trade_record:
                            self.today_trade_record["current_sl"] = tr["current_sl"]
                        self.alert_sl_update(tr, "BREAKOUT RUNNER TIGHTENED")

    def alert_sl_update(self, tr, reason):
        last_alerted = tr.get("last_alerted_sl", tr["init_sl"])
        if abs(tr["current_sl"] - last_alerted) >= 3.0:
            tr["last_alerted_sl"] = tr["current_sl"]
            entry_p = tr["entry"]
            is_ce = tr["type"] == "CE"
            cur_sl = tr["current_sl"]
            delta_v = tr.get("delta", 0.50)
            opt_entry = tr.get("opt_entry_ltp", 75.0)

            locked_pts = round((cur_sl - entry_p) if is_ce else (entry_p - cur_sl), 1)
            locked_inr = int(round(locked_pts * delta_v * LOT_SIZE_QTY))
            gtt_opt = max(0.5, round(opt_entry + (locked_pts * delta_v), 2))
            sign = "+" if locked_pts >= 0 else ""
            t_now = datetime.datetime.now(IST).strftime("%H:%M:%S")

            send_telegram_alert(
                f"🔔 <b>NIFTY {tr['type']} TRAILING SL UPDATED</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>STAGE:</b> {reason}\n"
                f"<b>CONTRACT:</b> {tr['symbol']}\n"
                f"🛡 <b>UPDATE STOP LOSS (GTT) TO:</b>\n"
                f"• <b>INDEX (SPOT):</b> {cur_sl:.1f}\n"
                f"• <b>OPTION (GTT ₹):</b> ₹{gtt_opt:.2f}\n"
                f"🔒 <b>LOCKED PROFIT:</b> {sign}{locked_pts:.1f} pts (₹{sign}{locked_inr})\n"
                f"⏰ <b>Time:</b> {t_now}"
            )

    def close_trade(self, exit_spot, reason):
        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        entry_p = tr["entry"]
        delta_v = tr.get("delta", 0.50)
        final_pts = round((exit_spot - entry_p) if is_ce else (entry_p - exit_spot), 1)
        final_inr = int(round(final_pts * delta_v * LOT_SIZE_QTY))
        sign = "+" if final_pts >= 0 else ""
        outcome_str = "PROFIT" if final_pts > 0 else "LOSS"

        t_now = datetime.datetime.now(IST).strftime("%H:%M:%S")

        tr["closed"] = True
        tr["exit_spot"] = exit_spot
        tr["exit_reason"] = reason
        tr["exit_time"] = t_now
        tr["final_pts"] = final_pts
        tr["final_inr"] = final_inr
        tr["outcome"] = outcome_str

        # Lock permanently into today's record so Streamlit displays it all day
        self.today_trade_record = tr.copy()
        self.locked_trade = None

        send_telegram_alert(
            f"🏁 <b>TRADE CLOSED ({reason})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚪 <b>Exit Price (Spot):</b> {exit_spot:.1f}\n"
            f"💰 <b>Net PnL:</b> {sign}{final_pts:.1f} pts (₹{sign}{final_inr})\n"
            f"📋 <b>Daily Limit:</b> 1/1 Trade Completed for Today.\n"
            f"⏰ <b>Exit Time:</b> {t_now}"
        )

    def execute_eod_exit(self, ltp):
        self.close_trade(ltp, "MANDATORY 15:15 EOD SQUARE-OFF")

    def sync_state(self, live_ltp, force=False):
        now_ts = time.time()
        if not force and (now_ts - self.last_push_time < 1.0):
            return
        self.last_push_time = now_ts

        # Active or retained trade record
        tr_source = self.locked_trade or self.today_trade_record

        opt_ltp = 0.0
        hud_payload = {}

        if tr_source:
            is_ce = (tr_source["type"] == "CE")
            delta_val = float(tr_source.get("delta", 0.50))
            entry_p = float(tr_source["entry"])
            opt_entry = float(tr_source.get("opt_entry_ltp", 75.0))
            cur_trail = float(tr_source.get("current_sl", tr_source["init_sl"]))

            if self.locked_trade:
                pts_gain = (live_ltp - entry_p) if is_ce else (entry_p - live_ltp)
                opt_ltp = max(0.5, round(opt_entry + (pts_gain * delta_val), 2))
                trail_pts = round((cur_trail - entry_p) if is_ce else (entry_p - cur_trail), 1)
                trail_opt = max(0.5, round(opt_entry + (trail_pts * delta_val), 2))
                is_closed = False
            else:
                opt_ltp = self.last_polled_opt_ltp if self.last_polled_opt_ltp > 0 else opt_entry
                trail_pts = float(tr_source.get("final_pts", 0.0))
                trail_opt = float(tr_source.get("opt_sl", 0.0))
                is_closed = True

            hud_payload = {
                "type": tr_source["type"],
                "mode": tr_source.get("mode"),
                "symbol": tr_source["symbol"],
                "entry_spot": entry_p,
                "entry_opt": opt_entry,
                "sl_spot": float(tr_source["init_sl"]),
                "sl_opt": float(tr_source.get("opt_sl", 0.0)),
                "trail_spot": cur_trail,
                "trail_opt": trail_opt,
                "target_p": float(tr_source.get("target_p", 0.0)),
                "opt_target": float(tr_source.get("opt_target", 0.0)),
                "delta": delta_val,
                "secured_pts": trail_pts,
                "trade_closed": is_closed,
                "exit_spot": tr_source.get("exit_spot"),
                "exit_reason": tr_source.get("exit_reason"),
                "exit_time": tr_source.get("exit_time"),
                "final_pts": tr_source.get("final_pts", 0.0),
                "final_inr": tr_source.get("final_inr", 0),
            }

        raw_list = list(self.candles)
        if self.active_bar:
            raw_list.append(self.active_bar)

        clean_map = {}
        for c in raw_list:
            t = int(c.get("time", 0))
            if t > 0:
                clean_map[t] = {
                    "time": t,
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "vwap": float(c.get("vwap", c["close"])),
                }

        sorted_times = sorted(clean_map.keys())
        recent_candles = [clean_map[t] for t in sorted_times][-240:]

        payload = {
            "ltp": float(live_ltp),
            "opt_ltp": float(opt_ltp),
            "candles": recent_candles,
            "locked_trade": self.locked_trade,
            "today_trade_record": self.today_trade_record,
            "trades_taken_today": self.trades_taken_today,
            "trade_date": self.today.strftime("%Y-%m-%d"),
            "daily_atr10": self.daily_atr10,
            "is_high_vol": self.is_high_vol,
            "vol_threshold": VOL_THRESHOLD,
            "orb_levels": self.orb_levels,
            "fib_lines": self.fib_lines,
            "pivots": self.pivot_lines,
            "markers": self.markers,
            "history_cards": self.history_cards,
            "contract_preview": self.contract_preview,
            "prev_day_summary": self.prev_day_summary,
            "hud": hud_payload,
            "updated_at": time.time(),
        }
        push_to_redis("nifty_live_state", payload)


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
                        engine.today_trade_record = None
                        engine.trades_taken_today = 0
                        engine.nfo_df = engine.load_scrip_master()
                        engine.fetch_daily_atr_and_levels()
                        engine.init_historical_candles()
                        engine.run_full_strategy_pipeline()
                        engine.simulate_previous_day_trades()
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
