#!/usr/bin/env python3
"""
Controller - 24/7 Telegram controller voor Listing Sniper

Commands:
    /start   - Start sniper bot
    /stop    - Stop sniper bot
    /status  - Bot status + posities
    /pnl     - Live P&L met huidige prijs
    /balance - Wallet balance
    /logs    - Laatste logs
    /config  - Toon config
    /test    - Test buy AVAXUSDT ($5)
    /sell    - Verkoop open posities
    /info    - Commands lijst
"""

import os
import sys
import time
import signal
import subprocess
import threading
import json
import math
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

try:
    import yaml
except:
    yaml = None

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
_config = None

# Test position tracking (voor /test en /sell)
_test_position = None  # {symbol, qty, entry_price, entry_time, trailing_set}

LOG_FILE = "data/logs/sniper.log"
TRADES_FILE = "data/trades.json"


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [Controller] {msg}")


def setup(env_path: str = "input.env") -> bool:
    """Load credentials"""
    global _BOT_TOKEN, _CHAT_ID, _client, _config
    
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
    
    # Load config
    if yaml and Path('config.yaml').exists():
        with open('config.yaml', 'r') as f:
            _config = yaml.safe_load(f)
        _log("Config loaded")
    
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
                stdout=None,
                stderr=None,
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
    """Show live P&L - check Bybit for actual positions"""
    if not _client:
        return "❌ Client niet beschikbaar"
    
    try:
        # Get actual positions from Bybit
        resp = _client.get_positions()
        if resp.get('retCode') != 0:
            return f"❌ Positions error: {resp.get('retMsg')}"
        
        positions = resp.get('result', {}).get('list', [])
        active = [p for p in positions if float(p.get('size', 0)) > 0]
        
        if not active:
            # No open positions - show historical
            if Path(TRADES_FILE).exists():
                with open(TRADES_FILE, 'r') as f:
                    trades = json.load(f)
                
                if trades:
                    total_net = sum(t.get('net_pnl', 0) for t in trades)
                    winners = len([t for t in trades if t.get('net_pnl', 0) > 0])
                    return f"""<b>📈 P&L Summary</b>

Geen open posities

<b>Historisch:</b>
Trades: {len(trades)}
Winners: {winners} ({winners/len(trades)*100:.0f}%)
Net P&L: ${total_net:.2f}"""
            
            return "📊 Geen open posities en geen historische trades"
        
        # Get instrument info for launch times
        inst_resp = _client.get_instruments_info(category='linear')
        instruments = {}
        if inst_resp.get('retCode') == 0:
            for i in inst_resp.get('result', {}).get('list', []):
                instruments[i.get('symbol')] = i
        
        # Show live positions
        lines = ["<b>📈 Live P&L</b>\n"]
        
        total_unrealized = 0
        
        for pos in active:
            symbol = pos.get('symbol', '')
            side = pos.get('side', '')
            size = float(pos.get('size', 0))
            entry_price = float(pos.get('avgPrice', 0))
            mark_price = float(pos.get('markPrice', 0))
            unrealized_pnl = float(pos.get('unrealisedPnl', 0))
            position_value = float(pos.get('positionValue', 0))
            trailing_stop = pos.get('trailingStop', '0')
            
            # Calculate %
            if position_value > 0:
                pnl_pct = (unrealized_pnl / position_value) * 100
            else:
                pnl_pct = 0
            
            # Get token launch time from instruments
            token_launch_str = "?"
            if symbol in instruments:
                launch_time = int(instruments[symbol].get('launchTime', 0))
                if launch_time > 0:
                    launch_date = datetime.fromtimestamp(launch_time / 1000)
                    days_live = (datetime.now() - launch_date).days
                    token_launch_str = f"{days_live}d (sinds {launch_date.strftime('%Y-%m-%d')})"
            
            # Get trade open time from executions
            trade_open_str = "?"
            exec_resp = _client.get_executions(symbol=symbol, limit=20)
            if exec_resp.get('retCode') == 0:
                executions = exec_resp.get('result', {}).get('list', [])
                # Find earliest execution for current position (same side)
                entry_side = 'Buy' if side == 'Buy' else 'Sell'
                for ex in reversed(executions):
                    if ex.get('side') == entry_side:
                        exec_time = int(ex.get('execTime', 0))
                        if exec_time > 0:
                            trade_open_sec = time.time() - (exec_time / 1000)
                            if trade_open_sec > 86400:
                                trade_open_str = f"{trade_open_sec/86400:.1f}d"
                            elif trade_open_sec > 3600:
                                trade_open_str = f"{trade_open_sec/3600:.1f}h"
                            elif trade_open_sec > 60:
                                trade_open_str = f"{trade_open_sec/60:.0f}m"
                            else:
                                trade_open_str = f"{trade_open_sec:.0f}s"
                            break
            
            # Trailing status
            trailing_active = float(trailing_stop) > 0
            if trailing_active and entry_price > 0:
                trailing_pct = (float(trailing_stop) / entry_price) * 100
                trailing_str = f"✅ {trailing_stop} ({trailing_pct:.2f}%)"
            elif trailing_active:
                trailing_str = f"✅ {trailing_stop}"
            else:
                trailing_str = "❌"
            
            # PnL emoji
            pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
            
            total_unrealized += unrealized_pnl
            
            lines.append(f"""<b>{symbol}</b> {side.upper()}
   Qty: {size}
   Entry: ${entry_price:.6f}
   Current: ${mark_price:.6f}
   {pnl_emoji} P&L: ${unrealized_pnl:.2f} ({pnl_pct:+.2f}%)
   Trailing: {trailing_str}
   Token live: {token_launch_str}
   Trade open: {trade_open_str}
""")
        
        # Total
        total_emoji = "🟢" if total_unrealized >= 0 else "🔴"
        lines.append(f"<b>{total_emoji} Total Unrealized: ${total_unrealized:.2f}</b>")
        
        return "\n".join(lines)
        
    except Exception as e:
        return f"❌ Error: {e}"


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


def cmd_test(args: str) -> str:
    """Test buy AVAXUSDT for ~$5 with trailing stop"""
    global _test_position
    
    if not _client:
        return "❌ Client niet beschikbaar"
    
    symbol = "AVAXUSDT"
    budget = 5.0
    
    try:
        # Get instrument info
        inst_resp = _client.get_instruments_info(category='linear')
        if inst_resp.get('retCode') != 0:
            return f"❌ Instruments error: {inst_resp.get('retMsg')}"
        
        instrument = None
        for i in inst_resp.get('result', {}).get('list', []):
            if i.get('symbol') == symbol:
                instrument = i
                break
        
        if not instrument:
            return f"❌ {symbol} niet gevonden"
        
        # Get qty step and min qty
        qty_step = float(instrument.get('lotSizeFilter', {}).get('qtyStep', '0.1'))
        min_qty = float(instrument.get('lotSizeFilter', {}).get('minOrderQty', '0.1'))
        min_notional = float(instrument.get('lotSizeFilter', {}).get('minNotionalValue', '5'))
        tick_size = instrument.get('priceFilter', {}).get('tickSize', '0.01')
        
        # Get current price
        ticker_resp = _client.get_tickers(category='linear', symbol=symbol)
        if ticker_resp.get('retCode') != 0:
            return f"❌ Ticker error: {ticker_resp.get('retMsg')}"
        
        ticker = ticker_resp.get('result', {}).get('list', [{}])[0]
        ask_price = float(ticker.get('ask1Price', 0))
        
        if ask_price <= 0:
            return "❌ Geen ask price beschikbaar"
        
        # Calculate qty (round UP to meet minimum)
        raw_qty = budget / ask_price
        
        # Round up to qty_step
        qty = math.ceil(raw_qty / qty_step) * qty_step
        
        # Ensure minimum
        if qty < min_qty:
            qty = min_qty
        
        # Check notional
        notional = qty * ask_price
        if notional < min_notional:
            qty = math.ceil(min_notional / ask_price / qty_step) * qty_step
            notional = qty * ask_price
        
        # Format qty
        if qty_step >= 1:
            qty_str = str(int(qty))
        else:
            decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
            qty_str = f"{qty:.{decimals}f}"
        
        # Add slippage to price (0.1%)
        limit_price = ask_price * 1.001
        price_decimals = len(tick_size.split('.')[1]) if '.' in tick_size else 2
        limit_price_str = f"{limit_price:.{price_decimals}f}"
        
        _log(f"TEST BUY: {symbol} qty={qty_str} @ {limit_price_str} (ask={ask_price})")
        
        # Place IOC order
        order_link_id = f"TEST_{int(time.time()*1000)}"
        
        resp = _client.place_order(
            symbol=symbol,
            side='Buy',
            qty=qty_str,
            order_type='Limit',
            price=limit_price_str,
            time_in_force='IOC',
            order_link_id=order_link_id
        )
        
        if resp.get('retCode') != 0:
            return f"❌ Order failed: {resp.get('retMsg')}"
        
        order_id = resp.get('result', {}).get('orderId', '')
        
        # Wait for fill to process
        time.sleep(1.0)
        
        # Check position directly (more reliable than order history)
        pos_resp = _client.get_positions(symbol=symbol)
        filled_qty = 0
        avg_price = 0
        
        if pos_resp.get('retCode') == 0:
            for pos in pos_resp.get('result', {}).get('list', []):
                if pos.get('symbol') == symbol and float(pos.get('size', 0)) > 0:
                    filled_qty = float(pos.get('size', 0))
                    avg_price = float(pos.get('avgPrice', 0))
                    break
        
        if filled_qty <= 0:
            # Fallback: check order history
            history_resp = _client.get_order_history(symbol=symbol, limit=10)
            if history_resp.get('retCode') == 0:
                for order in history_resp.get('result', {}).get('list', []):
                    if order.get('orderLinkId') == order_link_id:
                        filled_qty = float(order.get('cumExecQty', 0))
                        avg_price = float(order.get('avgPrice', 0))
                        break
        
        if filled_qty <= 0:
            return f"❌ Geen fill ontvangen (order: {order_id})\n\nCheck /status of /pnl voor positie status"
        
        filled_value = filled_qty * avg_price
        
        # Set trailing stop
        trailing_pct = _config.get('trailing', {}).get('distance_pct', 4.0) if _config else 4.0
        trailing_value = str(round(avg_price * trailing_pct / 100, 6))
        
        trailing_resp = _client.set_trading_stop(
            symbol=symbol,
            trailing_stop=trailing_value
        )
        
        trailing_ok = trailing_resp.get('retCode') == 0
        
        # Save test position
        _test_position = {
            'symbol': symbol,
            'qty': filled_qty,
            'entry_price': avg_price,
            'entry_time': time.time(),
            'trailing_set': trailing_ok
        }
        
        _log(f"TEST BUY SUCCESS: {filled_qty} @ ${avg_price:.4f} = ${filled_value:.2f}")
        
        return f"""✅ <b>TEST BUY SUCCES</b>

<b>{symbol}</b>
Qty: {filled_qty}
Entry: ${avg_price:.4f}
Value: ${filled_value:.2f}

Trailing Stop: {"✅ " + str(trailing_pct) + "%" if trailing_ok else "❌ Failed"}

<i>Gebruik /pnl voor live P&L
Gebruik /sell om te verkopen</i>"""

    except Exception as e:
        _log(f"TEST ERROR: {e}")
        import traceback
        traceback.print_exc()
        return f"❌ Error: {e}"


def cmd_sell(args: str) -> str:
    """Sell open positions"""
    global _test_position
    
    if not _client:
        return "❌ Client niet beschikbaar"
    
    try:
        # Get actual positions from Bybit
        resp = _client.get_positions()
        if resp.get('retCode') != 0:
            return f"❌ Positions error: {resp.get('retMsg')}"
        
        positions = resp.get('result', {}).get('list', [])
        active = [p for p in positions if float(p.get('size', 0)) > 0]
        
        if not active:
            _test_position = None
            return "📊 Geen open posities om te verkopen"
        
        # If no args, show positions
        if not args.strip():
            lines = ["<b>📤 Open Posities</b>\n"]
            for i, pos in enumerate(active, 1):
                symbol = pos.get('symbol', '')
                side = pos.get('side', '')
                size = float(pos.get('size', 0))
                entry = float(pos.get('avgPrice', 0))
                unrealized = float(pos.get('unrealisedPnl', 0))
                
                lines.append(f"{i}. <b>{symbol}</b> {side}")
                lines.append(f"   Size: {size} @ ${entry:.4f}")
                lines.append(f"   P&L: ${unrealized:.2f}")
            
            lines.append("\n<i>Gebruik: /sell 1 of /sell AVAXUSDT of /sell all</i>")
            return "\n".join(lines)
        
        # Process sell command
        selection = args.strip().lower()
        
        symbols_to_sell = []
        
        if selection == 'all':
            symbols_to_sell = [(p.get('symbol'), p.get('side'), float(p.get('size', 0))) for p in active]
        elif selection.isdigit():
            idx = int(selection) - 1
            if 0 <= idx < len(active):
                p = active[idx]
                symbols_to_sell = [(p.get('symbol'), p.get('side'), float(p.get('size', 0)))]
            else:
                return "❌ Ongeldig nummer"
        else:
            symbol = selection.upper()
            if not symbol.endswith('USDT'):
                symbol += 'USDT'
            
            for p in active:
                if p.get('symbol') == symbol:
                    symbols_to_sell = [(symbol, p.get('side'), float(p.get('size', 0)))]
                    break
            
            if not symbols_to_sell:
                return f"❌ {symbol} niet gevonden in open posities"
        
        # Execute sells
        results = []
        for symbol, side, size in symbols_to_sell:
            # Determine close side
            close_side = 'Sell' if side == 'Buy' else 'Buy'
            
            # Get instrument for qty formatting
            inst_resp = _client.get_instruments_info(category='linear')
            qty_step = 0.1
            for i in inst_resp.get('result', {}).get('list', []):
                if i.get('symbol') == symbol:
                    qty_step = float(i.get('lotSizeFilter', {}).get('qtyStep', '0.1'))
                    break
            
            # Format qty
            if qty_step >= 1:
                qty_str = str(int(size))
            else:
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 1
                qty_str = f"{size:.{decimals}f}"
            
            _log(f"SELLING: {symbol} {close_side} {qty_str}")
            
            # Market sell with reduce_only
            sell_resp = _client.place_order(
                symbol=symbol,
                side=close_side,
                qty=qty_str,
                order_type='Market',
                time_in_force='IOC',
                reduce_only=True
            )
            
            if sell_resp.get('retCode') == 0:
                order_id = sell_resp.get('result', {}).get('orderId', '')
                
                # Wait for fill
                time.sleep(0.5)
                
                # Get fill info
                exec_resp = _client.get_executions(symbol=symbol, limit=5)
                fill_price = 0
                
                if exec_resp.get('retCode') == 0:
                    for ex in exec_resp.get('result', {}).get('list', []):
                        if ex.get('orderId') == order_id:
                            fill_price = float(ex.get('execPrice', 0))
                            break
                
                results.append(f"✅ {symbol}: Sold {qty_str} @ ${fill_price:.4f}")
                
                # Clear test position if it matches
                if _test_position and _test_position.get('symbol') == symbol:
                    entry = _test_position.get('entry_price', 0)
                    pnl = (fill_price - entry) * size
                    results.append(f"   P&L: ${pnl:.2f}")
                    _test_position = None
            else:
                results.append(f"❌ {symbol}: {sell_resp.get('retMsg')}")
        
        return "<b>📤 Verkoop Resultaat</b>\n\n" + "\n".join(results)
        
    except Exception as e:
        _log(f"SELL ERROR: {e}")
        return f"❌ Error: {e}"


def cmd_trailing(args: str) -> str:
    """Set trailing stop on open position"""
    if not _client:
        return "❌ Client niet beschikbaar"
    
    try:
        # Get positions
        resp = _client.get_positions()
        if resp.get('retCode') != 0:
            return f"❌ Positions error: {resp.get('retMsg')}"
        
        positions = resp.get('result', {}).get('list', [])
        active = [p for p in positions if float(p.get('size', 0)) > 0]
        
        if not active:
            return "📊 Geen open posities"
        
        # If no args, show positions with trailing status
        if not args.strip():
            lines = ["<b>🎯 Trailing Stop Status</b>\n"]
            for i, pos in enumerate(active, 1):
                symbol = pos.get('symbol', '')
                trailing = pos.get('trailingStop', '0')
                trailing_active = float(trailing) > 0
                entry = float(pos.get('avgPrice', 0))
                
                status = f"✅ {trailing}" if trailing_active else "❌ Niet actief"
                lines.append(f"{i}. <b>{symbol}</b>")
                lines.append(f"   Entry: ${entry:.4f}")
                lines.append(f"   Trailing: {status}")
            
            lines.append("\n<i>Gebruik: /trailing 1 of /trailing AVAXUSDT</i>")
            return "\n".join(lines)
        
        # Find position to set trailing
        selection = args.strip().lower()
        target_pos = None
        
        if selection.isdigit():
            idx = int(selection) - 1
            if 0 <= idx < len(active):
                target_pos = active[idx]
        else:
            symbol = selection.upper()
            if not symbol.endswith('USDT'):
                symbol += 'USDT'
            for p in active:
                if p.get('symbol') == symbol:
                    target_pos = p
                    break
        
        if not target_pos:
            return "❌ Positie niet gevonden"
        
        symbol = target_pos.get('symbol')
        entry_price = float(target_pos.get('avgPrice', 0))
        
        # Calculate trailing stop
        trailing_pct = _config.get('trailing', {}).get('distance_pct', 4.0) if _config else 4.0
        trailing_value = str(round(entry_price * trailing_pct / 100, 6))
        
        _log(f"Setting trailing stop on {symbol}: {trailing_pct}% = {trailing_value}")
        
        trailing_resp = _client.set_trading_stop(
            symbol=symbol,
            trailing_stop=trailing_value
        )
        
        if trailing_resp.get('retCode') == 0:
            return f"""✅ <b>Trailing Stop Gezet</b>

<b>{symbol}</b>
Entry: ${entry_price:.4f}
Trailing: {trailing_pct}% = ${float(trailing_value):.4f}"""
        else:
            return f"❌ Trailing stop failed: {trailing_resp.get('retMsg')}"
        
    except Exception as e:
        _log(f"TRAILING ERROR: {e}")
        return f"❌ Error: {e}"


def cmd_prelisting(args: str) -> str:
    """Check for upcoming pre-listings"""
    if not _client:
        return "❌ Client niet beschikbaar"
    
    try:
        inst_resp = _client.get_instruments_info(category='linear')
        if inst_resp.get('retCode') != 0:
            return f"❌ Error: {inst_resp.get('retMsg')}"
        
        instruments = inst_resp.get('result', {}).get('list', [])
        prelistings = [i for i in instruments if i.get('isPreListing') == True]
        
        if not prelistings:
            return "📋 Geen pre-listings op dit moment"
        
        lines = [f"<b>🚀 Pre-listings: {len(prelistings)}</b>\n"]
        
        for p in prelistings:
            symbol = p.get('symbol', '')
            launch_time = int(p.get('launchTime', 0))
            pre_info = p.get('preListingInfo', {})
            
            # Calculate time until launch
            if launch_time > 0:
                now_ms = int(time.time() * 1000)
                diff_sec = (launch_time - now_ms) / 1000
                
                if diff_sec > 0:
                    if diff_sec > 86400:
                        time_str = f"in {diff_sec/86400:.1f}d"
                    elif diff_sec > 3600:
                        time_str = f"in {diff_sec/3600:.1f}h"
                    elif diff_sec > 60:
                        time_str = f"in {diff_sec/60:.0f}m"
                    else:
                        time_str = f"in {diff_sec:.0f}s"
                else:
                    time_str = "LIVE!"
                
                launch_date = datetime.fromtimestamp(launch_time / 1000)
                launch_str = launch_date.strftime('%Y-%m-%d %H:%M')
            else:
                time_str = "TBD"
                launch_str = "?"
            
            lines.append(f"<b>{symbol}</b>")
            lines.append(f"   Launch: {launch_str} ({time_str})")
            
            if pre_info:
                lines.append(f"   Info: {pre_info}")
            
            lines.append("")
        
        return "\n".join(lines)
        
    except Exception as e:
        return f"❌ Error: {e}"


def cmd_info() -> str:
    return """<b>📋 Listing Sniper Controller</b>

<b>Bot Control:</b>
/start - Start bot
/stop - Stop bot

<b>Testing:</b>
/test - Test buy AVAXUSDT ($5)
/sell - Verkoop open posities
/trailing - Zet trailing stop

<b>Info:</b>
/status - Bot status + posities
/pnl - Live P&amp;L overzicht
/balance - Wallet balance
/logs - Laatste logs
/config - Toon config
/prelisting - Check upcoming listings

/info - Deze lijst"""


COMMANDS = {
    '/start': lambda args: cmd_start(),
    '/stop': lambda args: cmd_stop(),
    '/status': lambda args: cmd_status(),
    '/pnl': lambda args: cmd_pnl(),
    '/balance': lambda args: cmd_balance(),
    '/logs': lambda args: cmd_logs(),
    '/config': lambda args: cmd_config(),
    '/test': lambda args: cmd_test(args),
    '/sell': lambda args: cmd_sell(args),
    '/trailing': lambda args: cmd_trailing(args),
    '/prelisting': lambda args: cmd_prelisting(args),
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
