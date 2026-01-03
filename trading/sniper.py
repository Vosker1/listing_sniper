"""
Sniper - IOC Ladder Execution
"""

import time
import math
from typing import Dict, Optional, List
from dataclasses import dataclass, field

from utils.logger import log_info, log_warn, log_debug, log_error


@dataclass
class SnipeResult:
    """Result of a snipe attempt"""
    symbol: str
    success: bool
    filled_qty: float = 0.0
    filled_value: float = 0.0
    avg_entry: float = 0.0
    orders_sent: int = 0
    fills: List[Dict] = field(default_factory=list)
    error: str = ""


class Sniper:
    """Executes IOC ladder buys on new listings"""
    
    def __init__(self, client, ws_manager, config):
        self.client = client
        self.ws = ws_manager
        self.config = config
        
        # Track fills via WS
        self.pending_fills: Dict[str, Dict] = {}
        self.order_fills: List[Dict] = []
        
        # Register order callback
        self.ws.on('order', self._on_order_update)
    
    def _on_order_update(self, data: Dict):
        """Handle order updates from WS"""
        try:
            orders = data.get('data', [])
            for order in orders:
                order_link_id = order.get('orderLinkId', '')
                if order_link_id.startswith('SNIPE_'):
                    cum_qty = float(order.get('cumExecQty', 0))
                    avg_price = float(order.get('avgPrice', 0))
                    status = order.get('orderStatus', '')
                    
                    if cum_qty > 0:
                        self.order_fills.append({
                            'order_link_id': order_link_id,
                            'qty': cum_qty,
                            'avg_price': avg_price,
                            'status': status
                        })
                        log_debug(f"Fill: {cum_qty} @ {avg_price} ({status})", "Sniper")
        except Exception as e:
            log_error(f"Order update error: {e}", "Sniper")
    
    def get_price_precision(self, instrument: Dict) -> int:
        """Get price precision from instrument info"""
        tick_size = instrument.get('priceFilter', {}).get('tickSize', '0.0001')
        try:
            if '.' in tick_size:
                return len(tick_size.split('.')[1].rstrip('0'))
            return 0
        except:
            return 4
    
    def get_qty_step(self, instrument: Dict) -> float:
        """Get quantity step from instrument info"""
        qty_step = instrument.get('lotSizeFilter', {}).get('qtyStep', '1')
        return float(qty_step)
    
    def round_qty(self, qty: float, step: float) -> float:
        """Round qty to valid step"""
        if step >= 1:
            return math.floor(qty)
        decimals = len(str(step).split('.')[-1]) if '.' in str(step) else 0
        return math.floor(qty / step) * step
    
    def round_price(self, price: float, precision: int) -> str:
        """Round price to valid precision"""
        return f"{price:.{precision}f}"
    
    def execute_snipe(self, symbol: str, instrument: Dict) -> SnipeResult:
        """
        Execute IOC ladder buy
        
        Uses ASK price (we're buying) + slippage premium
        """
        result = SnipeResult(symbol=symbol, success=False)
        
        try:
            budget = self.config.sniper.budget_usdt
            ladder_steps = self.config.ladder.steps
            repeat_per_step = self.config.ladder.repeat_per_step
            order_interval_ms = self.config.ladder.order_interval_ms
            max_orders = self.config.ladder.max_orders
            
            price_precision = self.get_price_precision(instrument)
            qty_step = self.get_qty_step(instrument)
            min_qty = float(instrument.get('lotSizeFilter', {}).get('minOrderQty', 1))
            min_notional = float(instrument.get('lotSizeFilter', {}).get('minNotionalValue', 5))
            
            log_info(f"Starting snipe: {symbol} | Budget: ${budget}", "Sniper")
            log_info(f"Price precision: {price_precision} | Qty step: {qty_step}", "Sniper")
            
            # Subscribe to ticker
            self.ws.subscribe_public([f'tickers.{symbol}'])
            time.sleep(0.3)  # Wait for first ticker
            
            # Clear previous fills
            self.order_fills = []
            
            filled_value = 0.0
            filled_qty = 0.0
            orders_sent = 0
            ladder_idx = 0
            repeat_count = 0
            
            while filled_value < budget and orders_sent < max_orders:
                # Get current ask price from WS
                ticker = self.ws.get_ticker(symbol)
                
                if not ticker:
                    # Fallback to REST
                    ticker_resp = self.client.get_tickers(category='linear', symbol=symbol)
                    if ticker_resp.get('retCode') == 0:
                        ticker_list = ticker_resp.get('result', {}).get('list', [])
                        if ticker_list:
                            ticker = ticker_list[0]
                
                if not ticker:
                    log_warn("No ticker data available", "Sniper")
                    time.sleep(0.1)
                    continue
                
                # Get ASK price (we're BUYING)
                ask_price = float(ticker.get('ask1Price', 0))
                
                if ask_price <= 0:
                    log_warn("Invalid ask price", "Sniper")
                    time.sleep(0.05)
                    continue
                
                # Calculate limit price with slippage
                slippage = ladder_steps[ladder_idx] if ladder_idx < len(ladder_steps) else ladder_steps[-1]
                limit_price = ask_price * (1 + slippage)
                limit_price_str = self.round_price(limit_price, price_precision)
                
                # Calculate qty for remaining budget
                remaining = budget - filled_value
                qty = remaining / float(limit_price_str)
                qty = self.round_qty(qty, qty_step)
                
                # Check minimums
                if qty < min_qty:
                    qty = min_qty
                
                notional = qty * float(limit_price_str)
                if notional < min_notional:
                    log_info(f"Notional ${notional:.2f} < min ${min_notional}, done", "Sniper")
                    break
                
                # Place IOC order
                order_link_id = f"SNIPE_{symbol}_{orders_sent}_{int(time.time()*1000)}"
                
                try:
                    resp = self.client.place_order(
                        symbol=symbol,
                        side='Buy',
                        qty=str(qty),
                        order_type='Limit',
                        price=limit_price_str,
                        time_in_force='IOC',
                        order_link_id=order_link_id
                    )
                    
                    orders_sent += 1
                    
                    if resp.get('retCode') == 0:
                        log_debug(f"Order {orders_sent}: {qty} @ {limit_price_str} (slip: {slippage*100:.2f}%)", "Sniper")
                    else:
                        log_warn(f"Order failed: {resp.get('retMsg')}", "Sniper")
                    
                except Exception as e:
                    log_error(f"Order error: {e}", "Sniper")
                
                # Wait for fill via WS
                time.sleep(order_interval_ms / 1000)
                
                # Check fills
                for fill in self.order_fills:
                    if fill.get('order_link_id') == order_link_id:
                        fill_qty = fill.get('qty', 0)
                        fill_price = fill.get('avg_price', 0)
                        if fill_qty > 0 and fill_price > 0:
                            fill_value = fill_qty * fill_price
                            filled_qty += fill_qty
                            filled_value += fill_value
                            result.fills.append(fill)
                            log_info(f"✓ Filled: {fill_qty} @ {fill_price} (${fill_value:.2f})", "Sniper")
                
                # Ladder progression
                repeat_count += 1
                if repeat_count >= repeat_per_step:
                    repeat_count = 0
                    ladder_idx += 1
                    if ladder_idx < len(ladder_steps):
                        log_debug(f"Ladder up: {ladder_steps[ladder_idx]*100:.2f}%", "Sniper")
            
            # Calculate results
            result.orders_sent = orders_sent
            result.filled_qty = filled_qty
            result.filled_value = filled_value
            
            if filled_qty > 0:
                result.avg_entry = filled_value / filled_qty
                result.success = True
                log_info(f"✅ SNIPE COMPLETE: {filled_qty} @ ${result.avg_entry:.6f} (${filled_value:.2f})", "Sniper")
            else:
                log_warn(f"No fills after {orders_sent} orders", "Sniper")
            
            return result
            
        except Exception as e:
            log_error(f"Snipe error: {e}", "Sniper")
            result.error = str(e)
            return result
    
    def set_trailing_stop(self, symbol: str, qty: float, entry_price: float) -> bool:
        """Set trailing stop on position"""
        try:
            trailing_pct = self.config.trailing.distance_pct
            trailing_value = str(round(entry_price * trailing_pct / 100, 6))
            
            log_info(f"Setting trailing stop: {trailing_pct}% = {trailing_value}", "Sniper")
            
            # Use trading-stop endpoint for existing position
            resp = self.client.set_trading_stop(
                symbol=symbol,
                trailing_stop=trailing_value
            )
            
            if resp.get('retCode') == 0:
                log_info(f"✅ Trailing stop set: {trailing_pct}%", "Sniper")
                return True
            else:
                log_warn(f"Trailing stop failed: {resp.get('retMsg')}", "Sniper")
                return False
                
        except Exception as e:
            log_error(f"Trailing stop error: {e}", "Sniper")
            return False
