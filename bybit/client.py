"""
Bybit REST API Client
"""

import time
import hmac
import hashlib
import json
import requests
from typing import Dict, List, Optional, Any

from utils.logger import log_debug, log_info, log_warn, log_error


class BybitClient:
    """Bybit V5 REST API Client"""
    
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = 'https://api.bybit.com'
        self.recv_window = '5000'
        self.session = requests.Session()
    
    def _sign(self, timestamp: str, params: Any) -> str:
        """Generate HMAC SHA256 signature"""
        if isinstance(params, dict):
            param_str = json.dumps(params, separators=(',', ':'))
        else:
            param_str = str(params) if params else ''
        
        sign_str = timestamp + self.api_key + self.recv_window + param_str
        
        return hmac.new(
            self.api_secret.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                 signed: bool = True) -> Dict:
        """Make HTTP request"""
        url = self.base_url + endpoint
        timestamp = str(int(time.time() * 1000))
        
        headers = {'Content-Type': 'application/json'}
        
        if signed:
            headers['X-BAPI-API-KEY'] = self.api_key
            headers['X-BAPI-TIMESTAMP'] = timestamp
            headers['X-BAPI-RECV-WINDOW'] = self.recv_window
            if method == 'POST':
                headers['X-BAPI-SIGN'] = self._sign(timestamp, params)
        
        try:
            if method == 'POST':
                body = json.dumps(params, separators=(',', ':')) if params else ''
                response = self.session.post(url, data=body, headers=headers, timeout=10)
            else:
                if signed and params:
                    str_params = {k: str(v) for k, v in params.items()}
                    sorted_params = sorted(str_params.items())
                    query_str = '&'.join([f'{k}={v}' for k, v in sorted_params])
                    sign_str = timestamp + self.api_key + self.recv_window + query_str
                    headers['X-BAPI-SIGN'] = hmac.new(
                        self.api_secret.encode('utf-8'),
                        sign_str.encode('utf-8'),
                        hashlib.sha256
                    ).hexdigest()
                    response = self.session.get(url + '?' + query_str, headers=headers, timeout=10)
                else:
                    response = self.session.get(url, params=params, headers=headers, timeout=10)
            
            return response.json()
            
        except Exception as e:
            log_error(f"Request failed: {endpoint} - {e}", "BybitClient")
            raise
    
    # === Market Data ===
    
    def get_instruments_info(self, category: str = 'linear') -> Dict:
        """Get all instruments info"""
        params = {'category': category, 'limit': 1000}
        return self._request('GET', '/v5/market/instruments-info', params, signed=False)
    
    def get_tickers(self, category: str = 'linear', symbol: str = None) -> Dict:
        """Get ticker info"""
        params = {'category': category}
        if symbol:
            params['symbol'] = symbol
        return self._request('GET', '/v5/market/tickers', params, signed=False)
    
    # === Trading ===
    
    def place_order(self, symbol: str, side: str, qty: str, order_type: str = 'Limit',
                    price: str = None, time_in_force: str = 'IOC',
                    reduce_only: bool = False, order_link_id: str = None) -> Dict:
        """Place order"""
        params = {
            'category': 'linear',
            'symbol': symbol,
            'side': side,
            'orderType': order_type,
            'qty': qty,
            'timeInForce': time_in_force
        }
        
        if price:
            params['price'] = price
        if reduce_only:
            params['reduceOnly'] = True
        if order_link_id:
            params['orderLinkId'] = order_link_id
        
        return self._request('POST', '/v5/order/create', params)
    
    def place_trailing_stop(self, symbol: str, side: str, qty: str,
                            trailing_stop: str, activation_price: str = None) -> Dict:
        """Place trailing stop order"""
        params = {
            'category': 'linear',
            'symbol': symbol,
            'side': side,
            'orderType': 'Market',
            'qty': qty,
            'timeInForce': 'GTC',
            'reduceOnly': True,
            'trailingStop': trailing_stop
        }
        
        if activation_price:
            params['activePrice'] = activation_price
        
        return self._request('POST', '/v5/order/create', params)
    
    def set_trading_stop(self, symbol: str, trailing_stop: str, position_idx: int = 0) -> Dict:
        """Set trailing stop on existing position"""
        params = {
            'category': 'linear',
            'symbol': symbol,
            'trailingStop': trailing_stop,
            'positionIdx': position_idx
        }
        return self._request('POST', '/v5/position/trading-stop', params)
    
    def cancel_all_orders(self, symbol: str) -> Dict:
        """Cancel all open orders"""
        params = {'category': 'linear', 'symbol': symbol}
        return self._request('POST', '/v5/order/cancel-all', params)
    
    # === Position ===
    
    def get_positions(self, symbol: str = None) -> Dict:
        """Get positions"""
        params = {'category': 'linear', 'settleCoin': 'USDT'}
        if symbol:
            params['symbol'] = symbol
        return self._request('GET', '/v5/position/list', params)
    
    # === Account ===
    
    def get_wallet_balance(self) -> Dict:
        """Get wallet balance"""
        params = {'accountType': 'UNIFIED'}
        return self._request('GET', '/v5/account/wallet-balance', params)
    
    def get_executions(self, symbol: str, limit: int = 50) -> Dict:
        """Get recent executions"""
        params = {'category': 'linear', 'symbol': symbol, 'limit': limit}
        return self._request('GET', '/v5/execution/list', params)
    
    def get_order_history(self, symbol: str, limit: int = 50) -> Dict:
        """Get order history"""
        params = {'category': 'linear', 'symbol': symbol, 'limit': limit}
        return self._request('GET', '/v5/order/history', params)
    
    def get_server_time(self) -> int:
        """Get server time in milliseconds"""
        result = self._request('GET', '/v5/market/time', signed=False)
        return int(result.get('result', {}).get('timeNano', 0)) // 1_000_000
