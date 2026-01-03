"""
Position Manager - Track positions and calculate P&L
"""

import time
import json
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from utils.logger import log_info, log_warn, log_error


@dataclass
class Position:
    """Active position"""
    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_value: float
    entry_time: float
    trailing_set: bool = False
    
    def unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L"""
        if self.side == 'Buy':
            return (current_price - self.entry_price) * self.qty
        else:
            return (self.entry_price - current_price) * self.qty
    
    def unrealized_pnl_pct(self, current_price: float) -> float:
        """Calculate unrealized P&L percentage"""
        if self.entry_value == 0:
            return 0
        return (self.unrealized_pnl(current_price) / self.entry_value) * 100


@dataclass
class TradeResult:
    """Completed trade result"""
    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    entry_time: float
    exit_time: float
    entry_value: float
    exit_value: float
    gross_pnl: float
    fees: float
    net_pnl: float
    roi_pct: float
    duration_sec: float


class PositionManager:
    """Manages positions and calculates P&L"""
    
    def __init__(self, client, ws_manager, config):
        self.client = client
        self.ws = ws_manager
        self.config = config
        
        self.positions: Dict[str, Position] = {}
        self.completed_trades: List[TradeResult] = []
        
        # Trades file
        self.trades_file = Path('data/trades.json')
        self.trades_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing trades
        self._load_trades()
        
        # Register callbacks
        self.ws.on('execution', self._on_execution)
        self.ws.on('order', self._on_order)
        
        # Exit callback
        self.on_exit_callback = None
    
    def _load_trades(self):
        """Load trades from file"""
        try:
            if self.trades_file.exists():
                with open(self.trades_file, 'r') as f:
                    data = json.load(f)
                    # Convert to TradeResult objects
                    for t in data:
                        self.completed_trades.append(TradeResult(**t))
        except Exception as e:
            log_warn(f"Could not load trades: {e}", "Position")
    
    def _save_trades(self):
        """Save trades to file"""
        try:
            data = [
                {
                    'symbol': t.symbol,
                    'side': t.side,
                    'qty': t.qty,
                    'entry_price': t.entry_price,
                    'exit_price': t.exit_price,
                    'entry_time': t.entry_time,
                    'exit_time': t.exit_time,
                    'entry_value': t.entry_value,
                    'exit_value': t.exit_value,
                    'gross_pnl': t.gross_pnl,
                    'fees': t.fees,
                    'net_pnl': t.net_pnl,
                    'roi_pct': t.roi_pct,
                    'duration_sec': t.duration_sec
                }
                for t in self.completed_trades
            ]
            with open(self.trades_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log_error(f"Could not save trades: {e}", "Position")
    
    def _on_execution(self, data: Dict):
        """Handle execution updates"""
        try:
            executions = data.get('data', [])
            for exec in executions:
                symbol = exec.get('symbol', '')
                side = exec.get('side', '')
                qty = float(exec.get('execQty', 0))
                price = float(exec.get('execPrice', 0))
                
                if symbol in self.positions and side == 'Sell':
                    # Position closed
                    self._handle_exit(symbol, qty, price)
        except Exception as e:
            log_error(f"Execution error: {e}", "Position")
    
    def _on_order(self, data: Dict):
        """Handle order updates for trailing stop triggers"""
        try:
            orders = data.get('data', [])
            for order in orders:
                symbol = order.get('symbol', '')
                status = order.get('orderStatus', '')
                stop_order_type = order.get('stopOrderType', '')
                
                # Trailing stop filled
                if status == 'Filled' and 'Trailing' in stop_order_type:
                    if symbol in self.positions:
                        avg_price = float(order.get('avgPrice', 0))
                        qty = float(order.get('cumExecQty', 0))
                        if avg_price > 0 and qty > 0:
                            self._handle_exit(symbol, qty, avg_price)
        except Exception as e:
            log_error(f"Order update error: {e}", "Position")
    
    def _handle_exit(self, symbol: str, qty: float, exit_price: float):
        """Handle position exit"""
        if symbol not in self.positions:
            return
        
        pos = self.positions[symbol]
        exit_time = time.time()
        
        # Calculate P&L
        exit_value = qty * exit_price
        entry_value = qty * pos.entry_price
        gross_pnl = exit_value - entry_value
        
        # Fees (entry + exit)
        taker_fee = self.config.fees.taker_pct / 100
        fees = (entry_value + exit_value) * taker_fee
        
        net_pnl = gross_pnl - fees
        roi_pct = (net_pnl / entry_value) * 100 if entry_value > 0 else 0
        duration = exit_time - pos.entry_time
        
        # Create trade result
        trade = TradeResult(
            symbol=symbol,
            side=pos.side,
            qty=qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            entry_value=entry_value,
            exit_value=exit_value,
            gross_pnl=gross_pnl,
            fees=fees,
            net_pnl=net_pnl,
            roi_pct=roi_pct,
            duration_sec=duration
        )
        
        self.completed_trades.append(trade)
        self._save_trades()
        
        # Remove position
        del self.positions[symbol]
        
        log_info(f"=== TRADE CLOSED: {symbol} ===", "Position")
        log_info(f"Entry: ${pos.entry_price:.6f} | Exit: ${exit_price:.6f}", "Position")
        log_info(f"Gross P&L: ${gross_pnl:.2f} | Fees: ${fees:.4f}", "Position")
        log_info(f"Net P&L: ${net_pnl:.2f} ({roi_pct:.2f}%)", "Position")
        log_info(f"Duration: {duration:.0f}s", "Position")
        
        # Callback
        if self.on_exit_callback:
            self.on_exit_callback(trade)
    
    def add_position(self, symbol: str, side: str, qty: float, entry_price: float):
        """Add new position"""
        entry_value = qty * entry_price
        
        self.positions[symbol] = Position(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            entry_value=entry_value,
            entry_time=time.time()
        )
        
        log_info(f"Position added: {symbol} {side} {qty} @ ${entry_price:.6f}", "Position")
    
    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position by symbol"""
        return self.positions.get(symbol)
    
    def has_position(self, symbol: str) -> bool:
        """Check if position exists"""
        return symbol in self.positions
    
    def get_all_positions(self) -> Dict[str, Position]:
        """Get all positions"""
        return self.positions.copy()
    
    def get_total_pnl(self) -> Dict:
        """Get total P&L summary"""
        total_net = sum(t.net_pnl for t in self.completed_trades)
        total_gross = sum(t.gross_pnl for t in self.completed_trades)
        total_fees = sum(t.fees for t in self.completed_trades)
        
        winners = [t for t in self.completed_trades if t.net_pnl > 0]
        losers = [t for t in self.completed_trades if t.net_pnl <= 0]
        
        return {
            'total_trades': len(self.completed_trades),
            'winners': len(winners),
            'losers': len(losers),
            'win_rate': len(winners) / len(self.completed_trades) * 100 if self.completed_trades else 0,
            'total_net_pnl': total_net,
            'total_gross_pnl': total_gross,
            'total_fees': total_fees
        }
    
    def format_pnl_summary(self) -> str:
        """Format P&L summary as string"""
        pnl = self.get_total_pnl()
        
        return f"""=== P&L SUMMARY ===
Total Trades: {pnl['total_trades']}
Winners: {pnl['winners']} | Losers: {pnl['losers']}
Win Rate: {pnl['win_rate']:.1f}%

Gross P&L: ${pnl['total_gross_pnl']:.2f}
Fees: ${pnl['total_fees']:.4f}
Net P&L: ${pnl['total_net_pnl']:.2f}
"""
