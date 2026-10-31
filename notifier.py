# notifier.py
import os
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def _send_message(text: str):
    """Core function to send Telegram notifications safely."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing in .env")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        return response.status_code == 200
    except Exception as e:
        print(f"Failed to send Telegram alert: {e}")
        return False

def send_entry_alert(action_type, contract, qty, delta, entry_spot, entry_price, init_sl_spot, init_sl_pts, init_sl_price, init_sl_pnl, stages_str, trigger_time):
    """Matches your exact Entry Telegram format."""
    msg = (
        f"⚡ **GOLDEN LOCKED STRATEGY: {action_type}**\n\n"
        f"📦 **CONTRACT:** {contract} (Qty: {qty}, Δ: {delta})\n"
        f"• **ENTRY (SPOT):** {entry_spot} | ₹{entry_price}\n"
        f"• **INITIAL SL:** {init_sl_spot} ({init_sl_pts} pts) | ₹{init_sl_price} (₹{init_sl_pnl})\n"
        f"• **TRAILING STAGES:** {stages_str}\n"
        f"⏰ **Trigger Time:** {trigger_time}"
    )
    return _send_message(msg)

def send_trailing_sl_alert(option_type, stage_name, contract, index_spot, option_gtt_price, locked_pts, locked_inr, update_time):
    """Matches your exact Trailing SL Update Telegram format."""
    msg = (
        f"🔔 **NIFTY {option_type} TRAILING SL UPDATED**\n\n"
        f"**STAGE:** {stage_name}\n"
        f"**CONTRACT:** {contract}\n"
        f"🛡️ **UPDATE STOP LOSS (GTT) TO:**\n"
        f"• **INDEX (SPOT):** {index_spot}\n"
        f"• **OPTION (GTT ₹):** ₹{option_gtt_price}\n"
        f"🔒 **LOCKED PROFIT:** +{locked_pts} pts (₹+{locked_inr})\n"
        f"⏰ **Time:** {update_time}"
    )
    return _send_message(msg)

def send_trade_closed_alert(exit_reason, exit_spot, net_pts, net_inr, daily_limit_str, exit_time):
    """Matches your exact Trade Closed Telegram format."""
    msg = (
        f"🏁 **TRADE CLOSED ({exit_reason})**\n\n"
        f"🚪 **Exit Spot:** {exit_spot}\n"
        f"💲 **Net PnL:** {net_pts} pts (₹{net_inr})\n"
        f"📋 **Daily Limit:** {daily_limit_str}\n"
        f"⏰ **Exit Time:** {exit_time}"
    )
    return _send_message(msg)