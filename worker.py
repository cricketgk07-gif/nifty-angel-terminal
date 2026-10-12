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


# --- Keep-Alive Health Server for Render ---
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


# --- Trading Engine ---
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
        self.nfo_df = self.load_scrip_master()
        self.init_historical_candles()

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
        from_str = (now - datetime.timedelta(days=6)).strftime("%Y-%m-%d 09:15")
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
                        "time": t_sec, "open": float(b[1]), "high": float(b[2]),
                        "low": float(b[3]), "close": float(b[4]), "dt": dt
                    })
        except Exception:
            pass

    def fetch_option_ltp(self, sym, tok):
        try:
            res = self.api.ltpData("NFO", sym, tok)
            if isinstance(res, dict) and res.get("status") and res.get("data"):
                return float(res["data"].get("ltp", 0.0))
        except Exception:
            pass
        return 0.0

    def find_budget_strike(self, spot_ref, is_ce, budget_target=BUDGET_CEILING):
        atm = int(round(spot_ref / 50.0) * 50)
        opt_type = "CE" if is_ce else "PE"

        if self.nfo_df.empty:
            return atm, f"NIFTY {atm} {opt_type}", ""

        future_exp = self.nfo_df[self.nfo_df["exp_dt"].dt.date >= self.today].sort_values("exp_dt")
        if future_exp.empty:
            return atm, f"NIFTY {atm} {opt_type}", ""
        curr_exp = future_exp["expiry"].iloc[0]
        exp_slice = future_exp[future_exp["expiry"] == curr_exp]

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

        valid = [c for c in candidates if c[3] <= budget_target + 8.0]
        if valid:
            valid.sort(key=lambda x: abs(x[3] - budget_target))
            return valid[0][0], valid[0][1], valid[0][2]

        return atm, f"NIFTY {atm} {opt_type}", ""

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
                "time": t_sec, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "dt": bar_dt
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

        last_c = self.candles[-1]
        t = last_c["dt"].time()
        close_p = last_c["close"]

        # 09:30 - 10:30 ORB Breakout Rule
        if datetime.time(9, 30) < t <= datetime.time(10, 30) and not self.locked_trade:
            if close_p > orb_h:
                strike, sym, tok = self.find_budget_strike(close_p, is_ce=True, budget_target=BUDGET_CEILING)
                init_sl = round(last_c["low"] - 5.0, 1)
                opt_ltp = self.fetch_option_ltp(sym, tok) if tok else 0.0

                self.locked_trade = {
                    "type": "CE", "strike": strike, "entry": close_p,
                    "init_sl": init_sl, "current_sl": init_sl, "closed": False,
                    "symbol": sym, "token": tok, "opt_entry_ltp": opt_ltp,
                    "time": last_c["time"]
                }
                send_telegram_alert(
                    f"🚀 <b>ORB BREAKOUT TRIGGERED: BUY {sym}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>ENTRY (Spot):</b> <b>{close_p:.1f}</b> | <b>Option:</b> ₹{opt_ltp:.2f}\n"
                    f"• <b>STOP LOSS:</b> <b>{init_sl:.1f}</b>\n"
                    f"⏰ <b>Time:</b> {last_c['dt'].strftime('%H:%M:%S')}"
                )

            elif close_p < orb_l:
                strike, sym, tok = self.find_budget_strike(close_p, is_ce=False, budget_target=BUDGET_CEILING)
                init_sl = round(last_c["high"] + 5.0, 1)
                opt_ltp = self.fetch_option_ltp(sym, tok) if tok else 0.0

                self.locked_trade = {
                    "type": "PE", "strike": strike, "entry": close_p,
                    "init_sl": init_sl, "current_sl": init_sl, "closed": False,
                    "symbol": sym, "token": tok, "opt_entry_ltp": opt_ltp,
                    "time": last_c["time"]
                }
                send_telegram_alert(
                    f"🚀 <b>ORB BREAKDOWN TRIGGERED: BUY {sym}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>ENTRY (Spot):</b> <b>{close_p:.1f}</b> | <b>Option:</b> ₹{opt_ltp:.2f}\n"
                    f"• <b>STOP LOSS:</b> <b>{init_sl:.1f}</b>\n"
                    f"⏰ <b>Time:</b> {last_c['dt'].strftime('%H:%M:%S')}"
                )

        # Stop-Loss Hit Trigger
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
            for c in self.candles[-300:]
        ]
        if self.active_bar:
            recent_candles.append({
                "time": self.active_bar["time"], "open": self.active_bar["open"],
                "high": self.active_bar["high"], "low": self.active_bar["low"],
                "close": self.active_bar["close"]
            })

        payload = {
            "ltp": live_ltp,
            "opt_ltp": opt_ltp,
            "candles": recent_candles,
            "locked_trade": self.locked_trade,
            "updated_at": time.time()
        }
        push_to_redis("nifty_live_state", payload)


def run_worker():
    engine = DedicatedTradingWorker()
    sws = SmartWebSocketV2(engine.auth_token, API_KEY, CLIENT_CODE, engine.feed_token)

    def on_data(wsapp, msg):
        if "last_traded_price" in msg:
            ltp = float(msg["last_traded_price"]) / 100.0
            engine.on_tick(ltp)

    def on_open(wsapp):
        sws.subscribe("correl_id_1", 1, [{"exchangeType": 1, "tokens": [INDEX_TOKEN]}])

    sws.on_open = on_open
    sws.on_data = on_data
    sws.connect()


if __name__ == "__main__":
    run_worker()
