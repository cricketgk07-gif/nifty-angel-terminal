import datetime
import html
import http.server
import json
import math
import os
import socketserver
import threading
import time
import numpy as np
import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

INDEX_TOKEN = "99926000"
LOT_SIZE_QTY = 65
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

API_KEY = os.getenv("API_KEY", "")
CLIENT_CODE = os.getenv("CLIENT_CODE", "")
PIN = os.getenv("PIN", "")
TOTP_SECRET = os.getenv("TOTP_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8925956740:AAHHo18Gp-rwlsGlnsyS5mz40RbvIN_tXqw")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6898891302")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
BUDGET_CEILING = float(os.getenv("BUDGET_CEILING", "80.0"))


# ==========================================
# 1. Health Ping Server for Render
# ==========================================
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"WORKER_RUNNING_OK")

    def log_message(self, format, *args):
        pass


def run_ping_server():
    port = int(os.getenv("PORT", 10000))
    with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
        httpd.serve_forever()


threading.Thread(target=run_ping_server, daemon=True).start()


# ==========================================
# 2. Mathematical & Network Helpers
# ==========================================
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def compute_contract_delta(s, k, days_to_exp, is_ce=True, iv=0.14, r=0.07):
    tau = max(days_to_exp / 365.0, 0.001)
    d1 = (math.log(s / k) + (r + 0.5 * iv ** 2) * tau) / (iv * math.sqrt(tau))
    ce_delta = norm_cdf(d1)
    return round(ce_delta if is_ce else (1.0 - ce_delta), 2)


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram Error]: Bot token or Chat ID is missing!")
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
        print(f"[Telegram Response {r.status_code}]: {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"[Telegram Exception]: {e}")
        return False


def push_to_redis(key, data_obj):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return
    url = f"{UPSTASH_REDIS_REST_URL}/set/{key}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        requests.post(url, headers=headers, data=json.dumps(data_obj), timeout=3)
    except Exception as e:
        print(f"Redis write error: {e}")


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
        self.api = SmartConnect(API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        self.session = self.api.generateSession(CLIENT_CODE, PIN, totp)
        self.auth_token = self.session["data"]["jwtToken"]
        self.feed_token = self.api.getfeedToken()

        self.candles = []
        self.active_bar = None
        self.today = datetime.datetime.now(IST).date()
        self.markers = []
        self.history_cards = {}
        self.pivot_lines = {}
        self.fib_lines = {}
        self.orb_levels = {}
        self.hud_data = {}

        saved_state = read_from_redis("nifty_live_state")
        self.locked_trade = saved_state.get("locked_trade") if saved_state else None

        self.nfo_df = self.load_scrip_master()
        self.init_historical_candles()
        self.run_full_strategy_pipeline()

        if self.candles:
            self.sync_state(self.candles[-1]["close"])

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
        from_str = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d 09:15")
        to_str = now.strftime("%Y-%m-%d %H:%M")
        try:
            resp = self.api.getCandleData({
                "exchange": "NSE",
                "symboltoken": INDEX_TOKEN,
                "interval": "FIVE_MINUTE",
                "fromdate": from_str,
                "todate": to_str,
            })
            if resp and resp.get("status") and resp.get("data"):
                for b in resp["data"]:
                    dt = pd.to_datetime(b[0])
                    t_sec = int((dt.tz_localize(None) - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1))
                    self.candles.append({
                        "time": t_sec,
                        "open": float(b[1]),
                        "high": float(b[2]),
                        "low": float(b[3]),
                        "close": float(b[4]),
                        "dt": dt,
                    })
        except Exception as e:
            print(f"Candle fetch notice: {e}")

    def compute_all_confluence_targets(self, df_h, entry_p, is_ce):
        high_low = df_h["high"] - df_h["low"]
        high_close = (df_h["high"] - df_h["close"].shift()).abs()
        low_close = (df_h["low"] - df_h["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr14 = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
        atr3_val = 3.0 * max(10.0, atr14)

        recent_swing_h = float(df_h["high"].iloc[-20:].max())
        recent_swing_l = float(df_h["low"].iloc[-20:].min())

        orb_h = self.orb_levels.get("orb_h", entry_p)
        orb_l = self.orb_levels.get("orb_l", entry_p)
        orb_rng = self.orb_levels.get("range", 25.0)

        p = self.pivot_lines
        min_filter = 25.0

        if is_ce:
            candidates = [
                orb_h + (1.000 * orb_rng),
                orb_h + (1.618 * orb_rng),
                orb_h + (2.618 * orb_rng),
                p.get("P"),
                p.get("R1"),
                p.get("R2"),
                p.get("R3"),
                p.get("PDH"),
                recent_swing_h,
                entry_p + atr3_val,
            ]
            valid = sorted(list(set([round(x, 1) for x in candidates if x is not None and x >= entry_p + min_filter])))
            if not valid:
                valid = [round(entry_p + 40.0, 1), round(entry_p + 75.0, 1), round(entry_p + 120.0, 1)]
        else:
            candidates = [
                orb_l - (1.000 * orb_rng),
                orb_l - (1.618 * orb_rng),
                orb_l - (2.618 * orb_rng),
                p.get("P"),
                p.get("S1"),
                p.get("S2"),
                p.get("S3"),
                p.get("PDL"),
                recent_swing_l,
                entry_p - atr3_val,
            ]
            valid = sorted(list(set([round(x, 1) for x in candidates if x is not None and x <= entry_p - min_filter])), reverse=True)
            if not valid:
                valid = [round(entry_p - 40.0, 1), round(entry_p - 75.0, 1), round(entry_p - 120.0, 1)]

        return valid

    def run_full_strategy_pipeline(self):
        if not self.candles:
            return

        df_h = pd.DataFrame(self.candles)
        df_h["date_str"] = df_h["dt"].dt.strftime("%Y-%m-%d")

        candle_spread = (df_h["high"] - df_h["low"]) + (df_h["close"] - df_h["open"]).abs()
        df_h["calc_vol"] = candle_spread * 1250.0 + 500.0
        df_h["tp"] = (df_h["high"] + df_h["low"] + df_h["close"]) / 3.0
        df_h["pv"] = df_h["tp"] * df_h["calc_vol"]

        df_h["cum_vol"] = df_h.groupby("date_str")["calc_vol"].cumsum()
        df_h["cum_pv"] = df_h.groupby("date_str")["pv"].cumsum()
        df_h["vwap"] = (df_h["cum_pv"] / df_h["cum_vol"]).round(2)
        df_h["ema9"] = df_h["close"].ewm(span=9, adjust=False).mean().round(2)

        high_low = df_h["high"] - df_h["low"]
        high_close = (df_h["high"] - df_h["close"].shift()).abs()
        low_close = (df_h["low"] - df_h["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df_h["atr14"] = tr.rolling(14, min_periods=1).mean().round(2)

        for i in range(len(self.candles)):
            self.candles[i]["vwap"] = float(df_h.iloc[i]["vwap"])
            self.candles[i]["ema9"] = float(df_h.iloc[i]["ema9"])
            self.candles[i]["atr14"] = float(df_h.iloc[i]["atr14"])

        unique_days = list(df_h["date_str"].unique())
        if len(unique_days) >= 2:
            prev_day_df = df_h[df_h["date_str"] == unique_days[-2]]
            if len(prev_day_df) > 0:
                pdh = round(float(prev_day_df["high"].max()), 2)
                pdl = round(float(prev_day_df["low"].min()), 2)
                pdc = round(float(prev_day_df["close"].iloc[-1]), 2)
                P = round((pdh + pdl + pdc) / 3.0, 2)
                self.pivot_lines = {
                    "P": P,
                    "R1": round((2.0 * P) - pdl, 2),
                    "S1": round((2.0 * P) - pdh, 2),
                    "R2": round(P + (pdh - pdl), 2),
                    "S2": round(P - (pdh - pdl), 2),
                    "PDH": pdh,
                    "PDL": pdl,
                }

        today_bars = df_h[df_h["date_str"] == unique_days[-1]]
        orb_window = today_bars[(today_bars["dt"].dt.time >= datetime.time(9, 15)) & (today_bars["dt"].dt.time < datetime.time(9, 30))]

        if len(orb_window) >= 3:
            curr_orb_h = round(float(orb_window["high"].max()), 2)
            curr_orb_l = round(float(orb_window["low"].min()), 2)
        else:
            first_three = today_bars.iloc[0:3] if len(today_bars) >= 3 else today_bars
            curr_orb_h = round(float(first_three["high"].max()), 2)
            curr_orb_l = round(float(first_three["low"].min()), 2)

        orb_range_val = max(15.0, round(curr_orb_h - curr_orb_l, 2))
        self.orb_levels = {"orb_h": curr_orb_h, "orb_l": curr_orb_l, "range": orb_range_val}

        self.markers = []
        self.history_cards = {}
        last_found_trade = None

        # Rigorous bar-by-bar backtest to verify if trades survived or hit their Stop Loss
        for d_str, day_df in df_h.groupby("date_str"):
            ow = day_df[(day_df["dt"].dt.time >= datetime.time(9, 15)) & (day_df["dt"].dt.time < datetime.time(9, 30))]
            if len(ow) < 3:
                continue

            d_h = float(ow["high"].max())
            d_l = float(ow["low"].min())
            sim_trade = None

            for idx in range(len(day_df)):
                r = day_df.iloc[idx]
                t = r["dt"].time()
                c = float(r["close"])
                h_bar = float(r["high"])
                l_bar = float(r["low"])
                vw = float(r["vwap"])
                em = float(r["ema9"])

                # Check if active simulated trade hit stop-loss FIRST
                if sim_trade and not sim_trade.get("closed"):
                    if sim_trade["type"] == "CE" and l_bar <= sim_trade["sl"]:
                        sim_trade["closed"] = True
                        sim_trade["outcome"] = "STOP HIT (LOSS)"
                        sim_trade["exit_price"] = sim_trade["sl"]
                    elif sim_trade["type"] == "PE" and h_bar >= sim_trade["sl"]:
                        sim_trade["closed"] = True
                        sim_trade["outcome"] = "STOP HIT (LOSS)"
                        sim_trade["exit_price"] = sim_trade["sl"]

                # Entry trigger condition
                if datetime.time(9, 30) <= t <= datetime.time(11, 30) and not sim_trade:
                    if c > d_h and c > vw and c > em:
                        sl_val = round(float(r["low"]) - 5.0, 1)
                        t_list = self.compute_all_confluence_targets(df_h.iloc[:idx+1], c, is_ce=True)
                        t_sec = int(r["time"])

                        self.markers.append({
                            "time": t_sec,
                            "position": "belowBar",
                            "color": "#00bfa5",
                            "shape": "arrowUp",
                            "text": f"BUY CE @ {c:.1f}",
                        })
                        sim_trade = {
                            "title": "BUY CE (ORB BREAKOUT)",
                            "entry": round(c, 1),
                            "sl": sl_val,
                            "targets": t_list,
                            "type": "CE",
                            "time_str": r["dt"].strftime("%H:%M"),
                            "date": d_str,
                            "date_readable": r["dt"].strftime("%d-%b-%Y (%A)"),
                            "closed": False,
                            "outcome": "ACTIVE / RUNNING",
                        }
                        self.history_cards[str(t_sec)] = sim_trade
                        self.history_cards[t_sec] = sim_trade
                        last_found_trade = sim_trade

                    elif c < d_l and c < vw and c < em:
                        sl_val = round(float(r["high"]) + 5.0, 1)
                        t_list = self.compute_all_confluence_targets(df_h.iloc[:idx+1], c, is_ce=False)
                        t_sec = int(r["time"])

                        self.markers.append({
                            "time": t_sec,
                            "position": "aboveBar",
                            "color": "#f23645",
                            "shape": "arrowDown",
                            "text": f"BUY PE @ {c:.1f}",
                        })
                        sim_trade = {
                            "title": "BUY PE (ORB BREAKDOWN)",
                            "entry": round(c, 1),
                            "sl": sl_val,
                            "targets": t_list,
                            "type": "PE",
                            "time_str": r["dt"].strftime("%H:%M"),
                            "date": d_str,
                            "date_readable": r["dt"].strftime("%d-%b-%Y (%A)"),
                            "closed": False,
                            "outcome": "ACTIVE / RUNNING",
                        }
                        self.history_cards[str(t_sec)] = sim_trade
                        self.history_cards[t_sec] = sim_trade
                        last_found_trade = sim_trade

        last_close = float(self.candles[-1]["close"])
        active_t = self.locked_trade

        if not active_t and last_found_trade and last_found_trade["date"] == unique_days[-1]:
            # If the trade already hit its SL on that day, display the stop hit state
            active_t = {
                "type": last_found_trade["type"],
                "entry": last_found_trade["entry"],
                "init_sl": last_found_trade["sl"],
                "current_sl": last_found_trade.get("exit_price", last_found_trade["sl"]),
                "last_alerted_sl": last_found_trade["sl"],
                "targets": last_found_trade["targets"],
                "symbol": f"NIFTY {int(round(last_found_trade['entry'] / 50.0) * 50)} {last_found_trade['type']}",
                "delta": 0.50,
                "opt_entry_ltp": 75.0,
                "date_readable": last_found_trade.get("date_readable", str(self.today)),
                "closed": last_found_trade.get("closed", False),
            }

        if active_t:
            t_type = active_t["type"]
            entry_p = float(active_t["entry"])
            sl_p = float(active_t["init_sl"])
            trail_p = float(active_t.get("current_sl", sl_p))
            target_list = active_t.get("targets", [])
            opt_entry = float(active_t.get("opt_entry_ltp", 75.0))
            delta_v = float(active_t.get("delta", 0.50))
            sym = active_t.get("symbol", f"NIFTY {int(round(entry_p / 50.0) * 50)} {t_type}")
        else:
            t_type = "CE" if last_close >= float(df_h.iloc[-1]["vwap"]) else "PE"
            entry_p = curr_orb_h if t_type == "CE" else curr_orb_l
            sl_p = round(curr_orb_h - 20.0 if t_type == "CE" else curr_orb_l + 20.0, 1)
            trail_p = sl_p
            target_list = self.compute_all_confluence_targets(df_h, entry_p, is_ce=(t_type == "CE"))
            opt_entry = 75.0
            delta_v = 0.50
            sym = f"NIFTY {int(round(last_close / 50.0) * 50)} {t_type}"

        if t_type == "CE":
            self.fib_lines = {
                "fib_100": round(curr_orb_h + (1.000 * orb_range_val), 1),
                "fib_161": round(curr_orb_h + (1.618 * orb_range_val), 1),
                "fib_261": round(curr_orb_h + (2.618 * orb_range_val), 1),
            }
        else:
            self.fib_lines = {
                "fib_100": round(curr_orb_l - (1.000 * orb_range_val), 1),
                "fib_161": round(curr_orb_l - (1.618 * orb_range_val), 1),
                "fib_261": round(curr_orb_l - (2.618 * orb_range_val), 1),
            }

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
            "date_readable": active_t.get("date_readable", str(self.today)) if active_t else str(self.today),
            "trade_closed": active_t.get("closed", False) if active_t else False,
        }

    def fetch_option_ltp(self, sym, tok):
        try:
            res = self.api.ltpData("NFO", sym, tok)
            if isinstance(res, dict) and res.get("status") and res.get("data"):
                return float(res["data"].get("ltp", 0.0))
        except Exception:
            pass
        return 0.0

    def find_budget_strike(self, spot_ref, is_ce, budget_target=None):
        if budget_target is None:
            budget_target = get_dynamic_budget()

        atm = int(round(spot_ref / 50.0) * 50)
        opt_type = "CE" if is_ce else "PE"

        if self.nfo_df.empty:
            return atm, f"NIFTY {atm} {opt_type}", "", 0.50, 4

        future_exp = self.nfo_df[self.nfo_df["exp_dt"].dt.date > self.today].sort_values("exp_dt")
        if future_exp.empty:
            future_exp = self.nfo_df[self.nfo_df["exp_dt"].dt.date >= self.today].sort_values("exp_dt")

        if future_exp.empty:
            return atm, f"NIFTY {atm} {opt_type}", "", 0.50, 4

        curr_exp = future_exp["expiry"].iloc[0]
        exp_slice = future_exp[future_exp["expiry"] == curr_exp]
        days_to_exp = max(1, (exp_slice.iloc[0]["exp_dt"].date() - self.today).days)

        candidates = []
        for offset in range(-5, 6):
            k = atm + (offset * 50)
            row = exp_slice[(exp_slice["strike_num"] == float(k)) & (exp_slice["symbol"].str.endswith(opt_type))]
            if not row.empty:
                sym = str(row.iloc[0]["symbol"])
                tok = str(row.iloc[0]["token"])
                ltp = self.fetch_option_ltp(sym, tok)
                if ltp > 0:
                    candidates.append((k, sym, tok, ltp))

        chosen_strike = atm
        chosen_sym = f"NIFTY {atm} {opt_type}"
        chosen_tok = ""

        valid = [c for c in candidates if c[3] <= budget_target + 8.0]
        if valid:
            valid.sort(key=lambda x: abs(x[3] - budget_target))
            chosen_strike, chosen_sym, chosen_tok = valid[0][0], valid[0][1], valid[0][2]
        elif candidates:
            candidates.sort(key=lambda x: abs(x[3] - budget_target))
            chosen_strike, chosen_sym, chosen_tok = candidates[0][0], candidates[0][1], candidates[0][2]

        delta = compute_contract_delta(spot_ref, chosen_strike, days_to_exp, is_ce=is_ce)
        return chosen_strike, chosen_sym, chosen_tok, delta, days_to_exp

    def on_tick(self, ltp):
        now = datetime.datetime.now(IST)
        bucket_min = (now.minute // 5) * 5
        bar_dt = now.replace(minute=bucket_min, second=0, microsecond=0)
        t_sec = int((bar_dt.tz_localize(None) - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1))

        if self.active_bar is None or self.active_bar["time"] != t_sec:
            if self.active_bar:
                self.candles.append(self.active_bar.copy())
                self.run_full_strategy_pipeline()
                self.evaluate_live_breakout()
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
        if self.locked_trade and self.locked_trade.get("closed"):
            return

        last_c = self.candles[-1]
        t = last_c["dt"].time()
        close_p = last_c["close"]

        orb_h = self.orb_levels.get("orb_h", 999999)
        orb_l = self.orb_levels.get("orb_l", 0)

        df_curr = pd.DataFrame(self.candles)
        date_readable = last_c["dt"].strftime("%d-%b-%Y (%A)")

        if datetime.time(9, 30) <= t <= datetime.time(11, 30) and not self.locked_trade:
            # CE Breakout
            if close_p > orb_h:
                strike, sym, tok, delta, days_exp = self.find_budget_strike(close_p, is_ce=True)
                init_sl = round(last_c["low"] - 5.0, 1)
                opt_ltp = self.fetch_option_ltp(sym, tok) if tok else 75.0
                risk_pts = round(close_p - init_sl, 1)
                opt_sl = max(0.5, round(opt_ltp - (risk_pts * delta), 2))

                targets = self.compute_all_confluence_targets(df_curr, close_p, is_ce=True)

                self.locked_trade = {
                    "type": "CE",
                    "strike": strike,
                    "entry": close_p,
                    "init_sl": init_sl,
                    "current_sl": init_sl,
                    "last_alerted_sl": init_sl,
                    "targets": targets,
                    "trail_stage": 0,
                    "peak_extreme": close_p,
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

                tg_lines = ""
                for i_tg, tg_val in enumerate(targets, start=1):
                    pts_d = round(abs(tg_val - close_p), 1)
                    opt_d = round(opt_ltp + (pts_d * delta), 2)
                    inr_d = int(round(pts_d * delta * LOT_SIZE_QTY))
                    tg_lines += f"• <b>TARGET {i_tg}:</b> {tg_val:.1f} (+{pts_d:.1f} pts) | <b>₹{opt_d:.2f}</b> (+₹{inr_d})\n"

                send_telegram_alert(
                    f"🚀 <b>ORB BREAKOUT TRIGGERED: BUY CE</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>CONTRACT:</b> {sym}\n"
                    f"<b>MODE:</b> Budget (&lt; ₹{budget_val}) | <b>QTY:</b> {LOT_SIZE_QTY} (Δ: {delta:.2f})\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>LEVEL | INDEX (SPOT) | OPTION (₹)</b>\n"
                    f"• <b>ENTRY:</b> {close_p:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                    f"• <b>STOP LOSS:</b> {init_sl:.1f} (-{risk_pts:.1f} pts) | <b>₹{opt_sl:.2f}</b> (-₹{total_risk_inr})\n"
                    f"{tg_lines}"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🛡 <b>Initial SL:</b> Spot: {init_sl:.1f} | Option: ₹{opt_sl:.2f}\n"
                    f"📅 <b>Session Date:</b> {date_readable}\n"
                    f"⏰ <b>Trigger Time:</b> {last_c['dt'].strftime('%H:%M:%S')}"
                )

            # PE Breakdown
            elif close_p < orb_l:
                strike, sym, tok, delta, days_exp = self.find_budget_strike(close_p, is_ce=False)
                init_sl = round(last_c["high"] + 5.0, 1)
                opt_ltp = self.fetch_option_ltp(sym, tok) if tok else 75.0
                risk_pts = round(init_sl - close_p, 1)
                opt_sl = max(0.5, round(opt_ltp - (risk_pts * delta), 2))

                targets = self.compute_all_confluence_targets(df_curr, close_p, is_ce=False)

                self.locked_trade = {
                    "type": "PE",
                    "strike": strike,
                    "entry": close_p,
                    "init_sl": init_sl,
                    "current_sl": init_sl,
                    "last_alerted_sl": init_sl,
                    "targets": targets,
                    "trail_stage": 0,
                    "peak_extreme": close_p,
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

                tg_lines = ""
                for i_tg, tg_val in enumerate(targets, start=1):
                    pts_d = round(abs(close_p - tg_val), 1)
                    opt_d = round(opt_ltp + (pts_d * delta), 2)
                    inr_d = int(round(pts_d * delta * LOT_SIZE_QTY))
                    tg_lines += f"• <b>TARGET {i_tg}:</b> {tg_val:.1f} (+{pts_d:.1f} pts) | <b>₹{opt_d:.2f}</b> (+₹{inr_d})\n"

                send_telegram_alert(
                    f"🚀 <b>ORB BREAKDOWN TRIGGERED: BUY PE</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>CONTRACT:</b> {sym}\n"
                    f"<b>MODE:</b> Budget (&lt; ₹{budget_val}) | <b>QTY:</b> {LOT_SIZE_QTY} (Δ: {delta:.2f})\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>LEVEL | INDEX (SPOT) | OPTION (₹)</b>\n"
                    f"• <b>ENTRY:</b> {close_p:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                    f"• <b>STOP LOSS:</b> {init_sl:.1f} (-{risk_pts:.1f} pts) | <b>₹{opt_sl:.2f}</b> (-₹{total_risk_inr})\n"
                    f"{tg_lines}"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🛡 <b>Initial SL:</b> Spot: {init_sl:.1f} | Option: ₹{opt_sl:.2f}\n"
                    f"📅 <b>Session Date:</b> {date_readable}\n"
                    f"⏰ <b>Trigger Time:</b> {last_c['dt'].strftime('%H:%M:%S')}"
                )

    def evaluate_live_trail_and_exit(self, live_spot):
        """
        STRICT EXECUTION ORDER:
        1. Check Stop-Loss FIRST. If price crossed current_sl -> EXIT IMMEDIATELY AS STOP HIT.
        2. Evaluate targets and trailing updates ONLY if stop loss is untouched.
        """
        if not self.locked_trade or self.locked_trade.get("closed"):
            return

        tr = self.locked_trade
        is_ce = tr["type"] == "CE"
        entry_p = tr["entry"]
        delta_v = tr.get("delta", 0.50)
        opt_entry = tr.get("opt_entry_ltp", 75.0)
        targets = tr.get("targets", [])
        current_stage = tr.get("trail_stage", 0)
        atr_val = max(12.0, float(self.candles[-1].get("atr14", 15.0)))
        t_now = datetime.datetime.now(IST).strftime("%H:%M:%S")
        date_readable = tr.get("date_readable", datetime.datetime.now(IST).strftime("%d-%b-%Y (%A)"))

        # =================================================================
        # CRITICAL FIX STEP 1: EVALUATE STOP-LOSS HIT BEFORE UPDATING TRAILS
        # =================================================================
        stop_hit = (live_spot <= tr["current_sl"]) if is_ce else (live_spot >= tr["current_sl"])
        if stop_hit:
            tr["closed"] = True
            exit_spot = tr["current_sl"]
            final_pts = round((exit_spot - entry_p) if is_ce else (entry_p - exit_spot), 1)
            final_inr = int(round(final_pts * delta_v * LOT_SIZE_QTY))
            sign = "+" if final_pts >= 0 else ""
            
            # Send accurate Stop Hit Alert (displays actual loss if stopped out)
            send_telegram_alert(
                f"🏁 <b>TRADE CLOSED (NIFTY {tr['type']} - STOP HIT)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🚪 <b>Exit Price (Spot):</b> {exit_spot:.1f}\n"
                f"💰 <b>Final Net Spot:</b> {sign}{final_pts:.1f} pts\n"
                f"💵 <b>Final Net Option:</b> ₹{sign}{final_inr}\n"
                f"📅 <b>Date:</b> {date_readable} | ⏰ <b>Exit Time:</b> {t_now}"
            )
            return

        # =================================================================
        # STEP 2: EVALUATE TARGETS & TRAILS ONLY WHEN ACTIVE AND UNSTOPPED
        # =================================================================
        if is_ce:
            tr["peak_extreme"] = max(tr.get("peak_extreme", entry_p), live_spot)
            peak_gain = tr["peak_extreme"] - entry_p
        else:
            tr["peak_extreme"] = min(tr.get("peak_extreme", entry_p), live_spot)
            peak_gain = entry_p - tr["peak_extreme"]

        sl_updated = False
        update_reason = ""

        # Check Milestone Target Crossings
        for idx, tg_p in enumerate(targets, start=1):
            if current_stage < idx:
                crossed = (live_spot >= tg_p) if is_ce else (live_spot <= tg_p)
                if crossed:
                    tr["trail_stage"] = idx
                    if idx == 1:
                        cost_buffer = 2.0 if is_ce else -2.0
                        new_sl = round(entry_p + cost_buffer, 1)
                        tr["current_sl"] = new_sl
                        update_reason = f"TARGET {idx} MET (COST LOCKED)"
                        sl_updated = True
                    else:
                        prev_tg = targets[idx - 2]
                        new_sl = prev_tg
                        tr["current_sl"] = new_sl
                        update_reason = f"TARGET {idx} MET"
                        sl_updated = True

        # Progressive Dynamic Ratchet (Runs when peak gain >= 60 pts)
        if peak_gain >= 60.0:
            cushion = max(10.0, 0.75 * atr_val) if peak_gain >= 90.0 else max(13.0, 1.00 * atr_val)

            prev_candle_extreme = None
            if len(self.candles) >= 2:
                if is_ce:
                    prev_candle_extreme = float(self.candles[-2]["low"]) - 3.0
                else:
                    prev_candle_extreme = float(self.candles[-2]["high"]) + 3.0

            if is_ce:
                candidate_trail = round(tr["peak_extreme"] - cushion, 1)
                if prev_candle_extreme and peak_gain >= 90.0:
                    candidate_trail = max(candidate_trail, prev_candle_extreme)

                if candidate_trail > tr["current_sl"] + 4.0:
                    tr["current_sl"] = candidate_trail
                    update_reason = f"RATCHETED HIGHER (PEAK: {tr['peak_extreme']:.1f})"
                    sl_updated = True
            else:
                candidate_trail = round(tr["peak_extreme"] + cushion, 1)
                if prev_candle_extreme and peak_gain >= 90.0:
                    candidate_trail = min(candidate_trail, prev_candle_extreme)

                if candidate_trail < tr["current_sl"] - 4.0:
                    tr["current_sl"] = candidate_trail
                    update_reason = f"RATCHETED HIGHER (PEAK: {tr['peak_extreme']:.1f})"
                    sl_updated = True

        # DISPATCH TRAILING SL ALERT (Fires whenever SL changes)
        last_alerted = tr.get("last_alerted_sl", tr["init_sl"])
        if sl_updated or abs(tr["current_sl"] - last_alerted) >= 4.0:
            tr["last_alerted_sl"] = tr["current_sl"]
            cur_sl = tr["current_sl"]

            locked_pts = round((cur_sl - entry_p) if is_ce else (entry_p - cur_sl), 1)
            locked_inr = int(round(locked_pts * delta_v * LOT_SIZE_QTY))
            gtt_opt = max(0.5, round(opt_entry + (locked_pts * delta_v), 2))
            sign = "+" if locked_pts >= 0 else ""

            send_telegram_alert(
                f"🔔 <b>NIFTY {tr['type']} TRAILING SL UPDATED ({update_reason})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>CONTRACT:</b> {tr['symbol']} (Qty: {LOT_SIZE_QTY})\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡 <b>UPDATE STOP LOSS (GTT) TO:</b>\n"
                f"• <b>INDEX (SPOT):</b> {cur_sl:.1f}\n"
                f"• <b>OPTION (GTT ₹):</b> ₹{gtt_opt:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔒 <b>LOCKED PROFIT:</b> {sign}{locked_pts:.1f} pts (₹{sign}{locked_inr})\n"
                f"📍 <b>Status:</b> RUNNER TIGHTENED\n"
                f"📅 <b>Date:</b> {date_readable} | ⏰ <b>Time:</b> {t_now}"
            )

    def sync_state(self, live_ltp):
        opt_ltp = 0.0
        if self.locked_trade and self.locked_trade.get("token"):
            opt_ltp = self.fetch_option_ltp(self.locked_trade["symbol"], self.locked_trade["token"])
        elif self.locked_trade:
            opt_ltp = self.locked_trade.get("opt_entry_ltp", 0.0)

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
            for c in self.candles[-500:]
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

        payload = {
            "ltp": live_ltp,
            "opt_ltp": opt_ltp,
            "candles": recent_candles,
            "locked_trade": self.locked_trade,
            "markers": self.markers,
            "history_cards": self.history_cards,
            "pivots": self.pivot_lines,
            "orb_levels": self.orb_levels,
            "fib_lines": self.fib_lines,
            "hud": self.hud_data,
            "updated_at": time.time(),
        }
        push_to_redis("nifty_live_state", payload)


# ==========================================
# 4. Resilient Worker Loop
# ==========================================
def run_worker():
    while True:
        try:
            print("Initializing Dedicated Trading Worker in LIVE PRODUCTION MODE...")
            engine = DedicatedTradingWorker()
            sws = SmartWebSocketV2(engine.auth_token, API_KEY, CLIENT_CODE, engine.feed_token)

            def on_data(wsapp, msg):
                if isinstance(msg, dict) and "last_traded_price" in msg:
                    ltp = float(msg["last_traded_price"]) / 100.0
                    engine.on_tick(ltp)

            def on_open(wsapp):
                print("WebSocket Connected Successfully to Angel One")
                sws.subscribe("correl_id_1", 1, [{"exchangeType": 1, "tokens": [INDEX_TOKEN]}])

            def on_error(*args, **kwargs):
                print("WebSocket Notice/Error:", args)

            def on_close(*args, **kwargs):
                print("WebSocket Connection Closed:", args)

            sws.on_open = on_open
            sws.on_data = on_data
            sws.on_error = on_error
            sws.on_close = on_close

            sws._on_error = on_error
            sws._on_close = on_close

            sws.connect()
        except Exception as e:
            print(f"Connection paused or reconnecting: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run_worker()
