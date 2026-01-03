#!/usr/bin/env python3
"""
Controller - 24/7 Telegram controller voor Listing Sniper

Commands:
    /start   - Start sniper bot
    /stop    - Stop sniper bot
    /status  - Bot status + posities
    /pnl     - P&L overzicht
    /balance - Wallet balance
    /logs    - Laatste logs
    /config  - Toon config
    /info    - Commands lijst
"""

import os
import sys
import time
import signal
import subprocess
import threading
import json
from pathlib import Path
from datetime import datetime

# Force IPv4
import socket
_original_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
except:
    def load_dotenv(*args, **kwargs):
        return False

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from bybit.client import BybitClient

# Session with retry
_session = requests.Session()
_retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
_session.mount("https://", HTTPAdapter(max_retries=_retry))

# Globals
_BOT_TOKEN = None
_CHAT_ID = None
_last_update_id = 0
_running = True
_trading_process = None
_process_lock = threading.Lock()
_client = None

LOG_FILE = "data/logs/sniper.log"
TRADES_FILE = "data/trades.json"


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [Controller] {msg}")


def setup(env_path: str = "input.env") -> bool:
    """Load credentials"""
    global _BOT_TOKEN, _CHAT_ID, _client
    
    p = Path(env_path)
    if p.exists():
        load_dotenv(env_path)
    
    _BOT_TOKEN = os.getenv("BOT_TOKEN")
    _CHAT_ID = os.getenv("CHAT_ID")
    
    if not _BOT_TOKEN or not _CHAT_ID:
        _log("Missing BOT_TOKEN or CHAT_ID")
        return False
    
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    
    if api_key and api_secret:
        _client = BybitClient(api_key, api_secret)
        _log("Bybit client initialized")
    
    return True


def get_updates():
    """Get Telegram updates"""
    global _last_update_id
    try:
        url = f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates"
        params = {"offset": _last_update_id + 1, "timeout": 30}
        r = _session.get(url, params=params, timeout=35)
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                return data.get("result", [])
    except Exception as e:
        _log(f"Get updates error: {e}")
    return []


def send_message(text: str):
    """Send Telegram message"""
    try:
        url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"}
        _session.post(url, json=payload, timeout=10)
    except Exception as e:
        _log(f"Send error: {e}")


def is_bot_running() -> bool:
    with _process_lock:
        if _trading_process and _trading_process.poll() is None:
            return True
    return False


def cmd_start() -> str:
    global _trading_process
    
    if is_bot_running():
        return "⚠️ Bot draait al!"
    
    try:
        with _process_lock:
            _trading_process = subprocess.Popen(
                [sys.executable, "bot.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(Path(__file__).parent)
            )
        _log(f"Bot started (PID: {_trading_process.pid})")
        return f"✅ Bot gestart (PID: {_trading_process.pid})"
    except Exception as e:
        return f"❌ Start failed: {e}"


def cmd_stop() -> str:
    global _trading_process
    
    if not is_bot_running():
        return "⚠️ Bot draait niet"
    
    try:
        with _process_lock:
            if _trading_process:
                _trading_process.terminate()
                _trading_process.wait(timeout=10)
                _trading_process = None
        _log("Bot stopped")
        return "🛑 Bot gestopt"
    except Exception as e:
        return f"❌ Stop failed: {e}"


def cmd_status() -> str:
    running = "✅ RUNNING" if is_bot_running() else "⏹️ STOPPED"
    
    positions_str = "Geen"
    if _client:
        try:
            resp = _client.get_positions()
            if resp.get('retCode') == 0:
                positions = resp.get('result', {}).get('list', [])
                active = [p for p in positions if float(p.get('size', 0)) > 0]
                if active:
                    positions_str = "\n".join([
                        f"  {p['symbol']}: {p['size']} @ ${float(p.get('avgPrice', 0)):.6f}"
                        for p in active
                    ])
        except:
            pass
    
    return f"""<b>📊 Status</b>

Bot: {running}

<b>Posities:</b>
{positions_str}
"""


def cmd_pnl() -> str:
    try:
        if Path(TRADES_FILE).exists():
            with open(TRADES_FILE, 'r') as f:
                trades = json.load(f)
            
            if not trades:
                return "Geen trades gevonden"
            
            total_net = sum(t.get('net_pnl', 0) for t in trades)
            total_gross = sum(t.get('gross_pnl', 0) for t in trades)
            total_fees = sum(t.get('fees', 0) for t in trades)
            winners = len([t for t in trades if t.get('net_pnl', 0) > 0])
            
            return f"""<b>📈 P&L Summary</b>

Trades: {len(trades)}
Winners: {winners} ({winners/len(trades)*100:.0f}%)

Gross: ${total_gross:.2f}
Fees: ${total_fees:.4f}
<b>Net: ${total_net:.2f}</b>
"""
        return "Geen trades gevonden"
    except Exception as e:
        return f"Error: {e}"


def cmd_balance() -> str:
    if not _client:
        return "❌ Client niet beschikbaar"
    
    try:
        resp = _client.get_wallet_balance()
        if resp.get('retCode') == 0:
            accounts = resp.get('result', {}).get('list', [])
            if accounts:
                acc = accounts[0]
                equity = float(acc.get('totalEquity', 0))
                available = float(acc.get('totalAvailableBalance', 0))
                return f"""<b>💰 Wallet Balance</b>

Equity: ${equity:.2f}
Available: ${available:.2f}
"""
        return "❌ Could not get balance"
    except Exception as e:
        return f"Error: {e}"


def cmd_logs() -> str:
    try:
        if Path(LOG_FILE).exists():
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
            last_50 = lines[-50:]
            return "<b>📋 Last 50 logs:</b>\n\n<pre>" + "".join(last_50)[-3500:] + "</pre>"
        return "Log file niet gevonden"
    except Exception as e:
        return f"Error: {e}"


def cmd_config() -> str:
    try:
        import yaml
        with open('config.yaml', 'r') as f:
            config = yaml.safe_load(f)
        
        sniper = config.get('sniper', {})
        trailing = config.get('trailing', {})
        
        return f"""<b>⚙️ Config</b>

<b>Sniper:</b>
  Budget: ${sniper.get('budget_usdt', 100)}
  Poll interval: {sniper.get('poll_interval_sec', 5)}s

<b>Trailing:</b>
  Distance: {trailing.get('distance_pct', 4)}%
"""
    except Exception as e:
        return f"Error: {e}"


def cmd_info() -> str:
    return """<b>📋 Listing Sniper Controller</b>

<b>Bot Control:</b>
/start - Start bot
/stop - Stop bot

<b>Info:</b>
/status - Bot status + posities
/pnl - P&amp;L overzicht
/balance - Wallet balance
/logs - Laatste logs
/config - Toon config

/info - Deze lijst"""


COMMANDS = {
    '/start': lambda args: cmd_start(),
    '/stop': lambda args: cmd_stop(),
    '/status': lambda args: cmd_status(),
    '/pnl': lambda args: cmd_pnl(),
    '/balance': lambda args: cmd_balance(),
    '/logs': lambda args: cmd_logs(),
    '/config': lambda args: cmd_config(),
    '/info': lambda args: cmd_info(),
    '/help': lambda args: cmd_info(),
}


def handle_message(text: str):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    
    if cmd in COMMANDS:
        response = COMMANDS[cmd](args)
        send_message(response)
    elif cmd.startswith('/'):
        send_message(f"❓ Onbekend: {cmd}\nTyp /info voor commands")


def poll_loop():
    global _running, _last_update_id
    
    _log("Polling started")
    
    while _running:
        updates = get_updates()
        
        for update in updates:
            _last_update_id = update.get('update_id', _last_update_id)
            
            message = update.get('message', {})
            chat_id = str(message.get('chat', {}).get('id', ''))
            text = message.get('text', '')
            
            if chat_id == _CHAT_ID and text:
                _log(f"Received: {text}")
                handle_message(text)
        
        if not updates:
            time.sleep(1)
    
    _log("Polling stopped")


def shutdown_handler(signum, frame):
    global _running
    _log("Shutdown signal")
    _running = False
    if is_bot_running():
        cmd_stop()


def main():
    global _running
    
    print("=" * 50)
    print("  LISTING SNIPER CONTROLLER")
    print("  24/7 Telegram control")
    print("=" * 50)
    print()
    
    if not setup("input.env"):
        print("Failed to load credentials")
        sys.exit(1)
    
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    
    _log("Controller started")
    send_message("🤖 Listing Sniper Controller gestart\n\nTyp /info voor commands")
    
    try:
        poll_loop()
    except KeyboardInterrupt:
        _log("Interrupted")
    finally:
        _running = False
        if is_bot_running():
            cmd_stop()
        _log("Controller stopped")


if __name__ == "__main__":
    main()
