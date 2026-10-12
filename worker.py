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
# 1. Keep-Alive Health Server for Render
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
# 2. Redis, Math & Telegram Helpers
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
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=3)
    except Exception:
        pass


def push_to_redis(key, data_obj):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return
    url = f"{UPSTASH_REDIS_REST_URL}/set/{key}"
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        requests.post(url, headers=headers, data=json.dumps(data_obj), timeout=3)
    except Exception:
        pass


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
# 3. Core Engine with Historical Signal Engine
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
        self.locked_trade = None
        self.markers = []
        self.history_cards = {}

        self.nfo_df = self.load_scrip_master()
        self.init_historical_candles()
        self.backtest_historical_signals()

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
        except Exception:
            pass

    def backtest_historical_signals(self):
        """Worker evaluates ORB strategy on all loaded historical candles."""
        if not self.candles:
            return

        df_h = pd.DataFrame(self.candles)
        df_h["date"] = df_h["dt"].dt.date

        # Compute VWAP and EMA9 for signal evaluation
        candle_spread = (df_h["high"] - df_h["low"]) + (df_h["close"] - df_h["open"]).abs()
        calc_vol = candle_spread * 1250.0 + 500.0
        tp = (df_h["high"] + df_h["low"] + df_h["close"]) / 3.0
        df_h["cum_vol"] = df_h.groupby("date")[calc_vol].cumsum() if calc_vol is not None else 1
        df_h["cum_vp"] = df_h.groupby("date")[tp * calc_vol].cumsum()
        df_h["vwap"] = df_h["cum_vp"] / df_h["cum_vol"]
        df_h["vwap"] = df_h["vwap"].fillna(tp)
        df_h["ema9"] = df_h["close"].ewm(span=9, adjust=False).mean()

        self.markers = []
        self.history_cards = {}

        for session_date, day_df in df_h.groupby("date"):
            orb_window = day_df[(day_df["dt"].dt.time >= datetime.time(9, 15)) & (day_df["dt"].dt.time <= datetime.time(9, 30))]
            if len(orb_window) < 3:
                continue

            orb_h = float(orb_window["high"].max())
            orb_l = float(orb_window["low"].min())
            orb_range = max(10.0, orb_h - orb_l)
            executed = False

            for idx in range(len(day_df)):
                r = day_df.iloc[idx]
                t = r["dt"].time()
                c = float(r["close"])
                vwap_val = float(r["vwap"])
                ema_val = float(r["ema9"])

                if datetime.time(9, 30) < t <= datetime.time(10, 30) and not executed:
                    if c > orb_h and c > vwap_val and c > ema_val:
                        sl_val = round(float(r["low"]) - 5.0, 1)
                        t_sec = int(r["time"])
                        self.markers.append({
                            "time": t_sec,
                            "position": "belowBar",
                            "color": "#00bfa5",
                            "shape": "arrowUp",
                            "text": f"BUY CE @ {c:.1f}",
                        })
                        self.history_cards[t_sec] = {
                            "title": "BUY CE (ORB BREAKOUT)",
                            "entry": round(c, 1),
                            "sl": sl_val,
                            "target1": round(orb_h + (1.000 * orb_range), 1),
                            "target_mid": round(orb_h + (1.618 * orb_range), 1),
                            "target_final": round(orb_h + (2.618 * orb_range), 1),
                            "type": "CE",
                            "time_str": r["dt"].strftime("%H:%M"),
                            "date": str(session_date),
                        }
                        executed = True

                    elif c < orb_l and c < vwap_val and c < ema_val:
                        sl_val = round(float(r["high"]) + 5.0, 1)
                        t_sec = int(r["time"])
                        self.markers.append({
                            "time": t_sec,
                            "position": "aboveBar",
                            "color": "#f23645",
                            "shape": "arrowDown",
                            "text": f"BUY PE @ {c:.1f}",
                        })
                        self.history_cards[t_sec] = {
                            "title": "BUY PE (ORB BREAKDOWN)",
                            "entry": round(c, 1),
                            "sl": sl_val,
                            "target1": round(orb_l - (1.000 * orb_range), 1),
                            "target_mid": round(orb_l - (1.618 * orb_range), 1),
                            "target_final": round(orb_l - (2.618 * orb_range), 1),
                            "type": "PE",
                            "time_str": r["dt"].strftime("%H:%M"),
                            "date": str(session_date),
                        }
                        executed = True

        print(f"Worker historical scan complete: {len(self.markers)} breakout signals detected across history.")

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

        # Strictly skip today's expiry
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
                self.evaluate_strategy()
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

        self.sync_state(ltp)

    def evaluate_strategy(self):
        if self.locked_trade and self.locked_trade.get("closed"):
            return

        today_bars = [c for c in self.candles if c["dt"].date() == self.today]
        orb_bars = [c for c in today_bars if datetime.time(9, 15) <= c["dt"].time() <= datetime.time(9, 30)]
        if len(orb_bars) < 3:
            return

        orb_h = max(c["high"] for c in orb_bars)
        orb_l = min(c["low"] for c in orb_bars)
        orb_range = max(10.0, orb_h - orb_l)

        last_c = self.candles[-1]
        t = last_c["dt"].time()
        close_p = last_c["close"]

        if datetime.time(9, 30) < t <= datetime.time(10, 30) and not self.locked_trade:
            if close_p > orb_h:
                strike, sym, tok, delta, days_exp = self.find_budget_strike(close_p, is_ce=True)
                init_sl = round(last_c["low"] - 5.0, 1)
                opt_ltp = self.fetch_option_ltp(sym, tok) if tok else 0.0
                if opt_ltp <= 0.0:
                    diff = max(0.0, close_p - strike)
                    opt_ltp = max(1.5, round(diff + (math.sqrt(days_exp) * 22.0) - (abs(close_p - strike) * 0.08), 2))

                risk_pts = round(close_p - init_sl, 1)
                opt_sl = max(0.5, round(opt_ltp - (risk_pts * delta), 1))
                t1_p = round(orb_h + (1.000 * orb_range), 1)
                tmid_p = round(orb_h + (1.618 * orb_range), 1)
                tfinal_p = round(orb_h + (2.618 * orb_range), 1)

                self.locked_trade = {
                    "type": "CE",
                    "strike": strike,
                    "entry": close_p,
                    "init_sl": init_sl,
                    "current_sl": init_sl,
                    "tp1": t1_p,
                    "tp_mid": tmid_p,
                    "tp_final": tfinal_p,
                    "closed": False,
                    "symbol": sym,
                    "token": tok,
                    "delta": delta,
                    "opt_entry_ltp": opt_ltp,
                    "time": last_c["time"],
                }

                # Add live marker immediately
                new_marker = {
                    "time": last_c["time"],
                    "position": "belowBar",
                    "color": "#00bfa5",
                    "shape": "arrowUp",
                    "text": f"BUY CE @ {close_p:.1f}",
                }
                self.markers.append(new_marker)
                self.history_cards[last_c["time"]] = {
                    "title": "BUY CE (ORB BREAKOUT)",
                    "entry": round(close_p, 1),
                    "sl": init_sl,
                    "target1": t1_p,
                    "target_mid": tmid_p,
                    "target_final": tfinal_p,
                    "type": "CE",
                    "time_str": last_c["dt"].strftime("%H:%M"),
                    "date": str(self.today),
                }

                send_telegram_alert(
                    f"🚀 <b>ORB BREAKOUT TRIGGERED: BUY {sym}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>CONTRACT:</b> {sym} (Δ: {delta:.2f})\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>ENTRY:</b> {close_p:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                    f"• <b>STOP LOSS:</b> {init_sl:.1f} (-{risk_pts:.1f} pts) | <b>₹{opt_sl:.2f}</b>\n"
                    f"• <b>TARGET 1:</b> {t1_p:.1f} | <b>TARGET MAX:</b> {tfinal_p:.1f}\n"
                    f"⏰ <b>Trigger Time:</b> {last_c['dt'].strftime('%H:%M:%S')}"
                )

            elif close_p < orb_l:
                strike, sym, tok, delta, days_exp = self.find_budget_strike(close_p, is_ce=False)
                init_sl = round(last_c["high"] + 5.0, 1)
                opt_ltp = self.fetch_option_ltp(sym, tok) if tok else 0.0
                if opt_ltp <= 0.0:
                    diff = max(0.0, strike - close_p)
                    opt_ltp = max(1.5, round(diff + (math.sqrt(days_exp) * 22.0) - (abs(strike - close_p) * 0.08), 2))

                risk_pts = round(init_sl - close_p, 1)
                opt_sl = max(0.5, round(opt_ltp - (risk_pts * delta), 1))
                t1_p = round(orb_l - (1.000 * orb_range), 1)
                tmid_p = round(orb_l - (1.618 * orb_range), 1)
                tfinal_p = round(orb_l - (2.618 * orb_range), 1)

                self.locked_trade = {
                    "type": "PE",
                    "strike": strike,
                    "entry": close_p,
                    "init_sl": init_sl,
                    "current_sl": init_sl,
                    "tp1": t1_p,
                    "tp_mid": tmid_p,
                    "tp_final": tfinal_p,
                    "closed": False,
                    "symbol": sym,
                    "token": tok,
                    "delta": delta,
                    "opt_entry_ltp": opt_ltp,
                    "time": last_c["time"],
                }

                new_marker = {
                    "time": last_c["time"],
                    "position": "aboveBar",
                    "color": "#f23645",
                    "shape": "arrowDown",
                    "text": f"BUY PE @ {close_p:.1f}",
                }
                self.markers.append(new_marker)
                self.history_cards[last_c["time"]] = {
                    "title": "BUY PE (ORB BREAKDOWN)",
                    "entry": round(close_p, 1),
                    "sl": init_sl,
                    "target1": t1_p,
                    "target_mid": tmid_p,
                    "target_final": tfinal_p,
                    "type": "PE",
                    "time_str": last_c["dt"].strftime("%H:%M"),
                    "date": str(self.today),
                }

                send_telegram_alert(
                    f"🚀 <b>ORB BREAKDOWN TRIGGERED: BUY {sym}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>CONTRACT:</b> {sym} (Δ: {delta:.2f})\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>ENTRY:</b> {close_p:.1f} | <b>₹{opt_ltp:.2f}</b>\n"
                    f"• <b>STOP LOSS:</b> {init_sl:.1f} (-{risk_pts:.1f} pts) | <b>₹{opt_sl:.2f}</b>\n"
                    f"• <b>TARGET 1:</b> {t1_p:.1f} | <b>TARGET MAX:</b> {tfinal_p:.1f}\n"
                    f"⏰ <b>Trigger Time:</b> {last_c['dt'].strftime('%H:%M:%S')}"
                )

        if self.locked_trade and not self.locked_trade.get("closed"):
            trade = self.locked_trade
            if trade["type"] == "CE" and last_c["low"] <= trade["current_sl"]:
                trade["closed"] = True
                send_telegram_alert(f"🏁 <b>STOP HIT: {trade['symbol']} CLOSED @ {trade['current_sl']:.1f}</b>")
            elif trade["type"] == "PE" and last_c["high"] >= trade["current_sl"]:
                trade["closed"] = True
                send_telegram_alert(f"🏁 <b>STOP HIT: {trade['symbol']} CLOSED @ {trade['current_sl']:.1f}</b>")

    def sync_state(self, live_ltp):
        opt_ltp = 0.0
        if self.locked_trade and self.locked_trade.get("token"):
            opt_ltp = self.fetch_option_ltp(self.locked_trade["symbol"], self.locked_trade["token"])
        elif self.locked_trade:
            opt_ltp = self.locked_trade.get("opt_entry_ltp", 0.0)

        recent_candles = [
            {"time": c["time"], "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
            for c in self.candles[-500:]
        ]
        if self.active_bar:
            recent_candles.append({
                "time": self.active_bar["time"],
                "open": self.active_bar["open"],
                "high": self.active_bar["high"],
                "low": self.active_bar["low"],
                "close": self.active_bar["close"],
            })

        payload = {
            "ltp": live_ltp,
            "opt_ltp": opt_ltp,
            "candles": recent_candles,
            "locked_trade": self.locked_trade,
            "markers": self.markers,
            "history_cards": self.history_cards,
            "updated_at": time.time(),
        }
        push_to_redis("nifty_live_state", payload)


# ==========================================
# 4. Resilient Worker Loop
# ==========================================
def run_worker():
    while True:
        try:
            print("Initializing Dedicated Trading Worker...")
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
