"""
Listing Sniper Bot - Main Entry Point
"""

import os
import sys
import time
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from utils.config import Config
from utils.logger import init_logger, log_info, log_warn, log_error
from utils import telegram
from bybit.client import BybitClient
from bybit.websocket import WebSocketManager
from trading.scanner import InstrumentScanner
from trading.sniper import Sniper
from trading.position import PositionManager


class ListingSniperBot:
    """Main bot class"""
    
    def __init__(self):
        self.config: Config = None
        self.client: BybitClient = None
        self.ws: WebSocketManager = None
        self.scanner: InstrumentScanner = None
        self.sniper: Sniper = None
        self.position_mgr: PositionManager = None
        
        self.running = False
        self.paused = False
        
        # Stats
        self.snipes_attempted = 0
        self.snipes_successful = 0
        self.start_time = None
    
    def initialize(self) -> bool:
        """Initialize all components"""
        try:
            # Load config
            self.config = Config.load()
            log_info("Config loaded", "Main")
            
            # Init logger
            init_logger(
                output_dir=self.config.logging.output_dir,
                level=self.config.logging.level,
                console_enabled=self.config.logging.console_enabled,
                file_enabled=self.config.logging.file_enabled
            )
            
            # Init telegram
            if self.config.telegram.enabled:
                telegram.setup_from_env('input.env')
            
            # Init Bybit client
            self.client = BybitClient(
                self.config.api_key,
                self.config.api_secret
            )
            log_info("Bybit client initialized", "Main")
            
            # Init WebSocket
            self.ws = WebSocketManager(
                self.config.api_key,
                self.config.api_secret,
                ping_interval_s=self.config.websocket.ping_interval_s,
                pong_timeout_s=self.config.websocket.pong_timeout_s
            )
            
            # Connect WebSocket
            log_info("Connecting WebSocket...", "Main")
            self.ws.connect_all()
            
            if not self.ws.wait_for_connection(timeout_s=30):
                log_error("WebSocket connection timeout", "Main")
                return False
            
            log_info("WebSocket connected", "Main")
            
            # Subscribe to order updates
            self.ws.subscribe_private(['order', 'execution'])
            
            # Init scanner
            self.scanner = InstrumentScanner(self.client, self.config)
            self.scanner.initialize()
            
            # Init sniper
            self.sniper = Sniper(self.client, self.ws, self.config)
            
            # Init position manager
            self.position_mgr = PositionManager(self.client, self.ws, self.config)
            self.position_mgr.on_exit_callback = self._on_position_exit
            
            log_info("Initialization complete", "Main")
            return True
            
        except Exception as e:
            log_error(f"Initialization failed: {e}", "Main")
            import traceback
            traceback.print_exc()
            return False
    
    def _on_position_exit(self, trade):
        """Handle position exit - send telegram"""
        if telegram.enabled():
            msg = f"""🔔 TRADE CLOSED: {trade.symbol}

Entry: ${trade.entry_price:.6f}
Exit: ${trade.exit_price:.6f}
Duration: {trade.duration_sec:.0f}s

Gross P&L: ${trade.gross_pnl:.2f}
Fees: ${trade.fees:.4f}
Net P&L: ${trade.net_pnl:.2f} ({trade.roi_pct:.2f}%)
"""
            telegram.send_alert(msg)
    
    def _get_next_poll_time(self) -> float:
        """
        Calculate next poll time synchronized with clock
        Polls at XX:00.100, XX:05.100, XX:10.100, etc.
        """
        now = time.time()
        interval = self.config.sniper.poll_interval_sec
        offset_ms = self.config.sniper.poll_offset_ms
        
        # Current second within interval cycle
        current_sec = now % interval
        
        # Next interval boundary
        next_boundary = now - current_sec + interval
        
        # Add offset
        next_poll = next_boundary + (offset_ms / 1000)
        
        return next_poll
    
    def run(self):
        """Main run loop"""
        self.running = True
        self.start_time = time.time()
        
        log_info("="*50, "Main")
        log_info("  LISTING SNIPER BOT STARTED", "Main")
        log_info("="*50, "Main")
        
        if telegram.enabled():
            telegram.send_alert(f"🚀 Listing Sniper gestart\n\nBudget: ${self.config.sniper.budget_usdt}\nTrailing: {self.config.trailing.distance_pct}%")
        
        # Calculate first poll time
        next_poll = self._get_next_poll_time()
        
        while self.running:
            try:
                # Wait until next poll time
                now = time.time()
                if now < next_poll:
                    sleep_time = next_poll - now
                    if sleep_time > 0:
                        time.sleep(min(sleep_time, 1.0))  # Max 1s sleep for responsiveness
                    continue
                
                # Calculate next poll
                next_poll = self._get_next_poll_time()
                
                if self.paused:
                    continue
                
                # Scan for new listings
                new_listings = self.scanner.scan_for_new()
                
                for instrument in new_listings:
                    symbol = instrument['symbol']
                    
                    # Skip if already have position
                    if self.position_mgr.has_position(symbol):
                        log_info(f"Already have position in {symbol}, skipping", "Main")
                        continue
                    
                    # Execute snipe
                    self.snipes_attempted += 1
                    
                    if telegram.enabled():
                        telegram.send_alert(f"🎯 NIEUWE LISTING: {symbol}\n\nStarting snipe...")
                    
                    result = self.sniper.execute_snipe(symbol, instrument)
                    
                    if result.success:
                        self.snipes_successful += 1
                        
                        # Add position
                        self.position_mgr.add_position(
                            symbol=symbol,
                            side='Buy',
                            qty=result.filled_qty,
                            entry_price=result.avg_entry
                        )
                        
                        # Set trailing stop
                        self.sniper.set_trailing_stop(symbol, result.filled_qty, result.avg_entry)
                        
                        # Update position
                        pos = self.position_mgr.get_position(symbol)
                        if pos:
                            pos.trailing_set = True
                        
                        if telegram.enabled():
                            telegram.send_alert(f"""✅ SNIPE SUCCESS: {symbol}

Qty: {result.filled_qty}
Avg Entry: ${result.avg_entry:.6f}
Value: ${result.filled_value:.2f}
Orders: {result.orders_sent}

Trailing stop: {self.config.trailing.distance_pct}%
""")
                    else:
                        if telegram.enabled():
                            telegram.send_alert(f"❌ SNIPE FAILED: {symbol}\n\n{result.error or 'No fills'}")
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                log_error(f"Main loop error: {e}", "Main")
                import traceback
                traceback.print_exc()
                time.sleep(1)
        
        self.shutdown()
    
    def shutdown(self):
        """Shutdown bot"""
        log_info("Shutting down...", "Main")
        self.running = False
        
        if self.ws:
            self.ws.close_all()
        
        if telegram.enabled():
            pnl = self.position_mgr.get_total_pnl() if self.position_mgr else {}
            uptime = time.time() - self.start_time if self.start_time else 0
            
            telegram.send_message(f"""🛑 Listing Sniper gestopt

Uptime: {uptime/3600:.1f}h
Snipes: {self.snipes_attempted} ({self.snipes_successful} successful)
Net P&L: ${pnl.get('total_net_pnl', 0):.2f}
""")
            telegram.shutdown()
        
        log_info("Shutdown complete", "Main")
    
    def pause(self):
        """Pause scanning"""
        self.paused = True
        log_info("Bot paused", "Main")
    
    def resume(self):
        """Resume scanning"""
        self.paused = False
        log_info("Bot resumed", "Main")
    
    def get_status(self) -> Dict:
        """Get current status"""
        uptime = time.time() - self.start_time if self.start_time else 0
        positions = self.position_mgr.get_all_positions() if self.position_mgr else {}
        pnl = self.position_mgr.get_total_pnl() if self.position_mgr else {}
        
        return {
            'running': self.running,
            'paused': self.paused,
            'uptime_hours': uptime / 3600,
            'snipes_attempted': self.snipes_attempted,
            'snipes_successful': self.snipes_successful,
            'open_positions': len(positions),
            'positions': positions,
            'pnl': pnl,
            'clock_offset_ms': self.ws.clock_offset_ms if self.ws else 0
        }


# Global bot instance (for controller)
_bot: ListingSniperBot = None


def get_bot() -> ListingSniperBot:
    """Get global bot instance"""
    global _bot
    if _bot is None:
        _bot = ListingSniperBot()
    return _bot


def main():
    """Main entry point"""
    bot = get_bot()
    
    # Signal handlers
    def signal_handler(signum, frame):
        log_info("Signal received, shutting down...", "Main")
        bot.running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize
    if not bot.initialize():
        log_error("Failed to initialize bot", "Main")
        sys.exit(1)
    
    # Run
    bot.run()


if __name__ == '__main__':
    main()