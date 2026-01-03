"""
WebSocket Manager with Clock Sync
"""

import json
import time
import hmac
import hashlib
import threading
from typing import Dict, List, Callable, Optional

import websocket

from utils.logger import log_debug, log_info, log_warn, log_error


class WebSocketManager:
    """
    Manages Bybit WebSocket connections
    - Public stream: tickers
    - Private stream: executions, orders
    - Clock sync via ping/pong
    """
    
    PUBLIC_URL = 'wss://stream.bybit.com/v5/public/linear'
    PRIVATE_URL = 'wss://stream.bybit.com/v5/private'
    TRADE_URL = 'wss://stream.bybit.com/v5/trade'
    
    def __init__(self, api_key: str, api_secret: str,
                 ping_interval_s: int = 20, pong_timeout_s: int = 30):
        self.api_key = api_key
        self.api_secret = api_secret
        
        self.ping_interval_s = ping_interval_s
        self.pong_timeout_s = pong_timeout_s
        
        # WebSocket connections
        self.public_ws: Optional[websocket.WebSocketApp] = None
        self.private_ws: Optional[websocket.WebSocketApp] = None
        
        # Callbacks
        self.callbacks: Dict[str, Callable] = {}
        
        # State
        self.public_connected = False
        self.private_connected = False
        self.private_authenticated = False
        
        # Ping state
        self.last_public_pong = time.time()
        self.last_private_pong = time.time()
        
        # Threads
        self.public_thread: Optional[threading.Thread] = None
        self.private_thread: Optional[threading.Thread] = None
        
        # Subscriptions
        self.public_subscriptions: List[str] = []
        self.private_subscriptions: List[str] = []
        
        # Running flag
        self.running = False
        
        # Clock sync
        self.clock_offset_ms: int = 0
        self.clock_offset_samples: List[int] = []
        self.clock_sync_lock = threading.Lock()
        self.ping_send_times: Dict[str, int] = {}
        self.ping_counter: int = 0
        
        # Latest ticker data per symbol
        self.tickers: Dict[str, Dict] = {}
        self.ticker_lock = threading.Lock()
    
    def connect_all(self):
        """Connect both streams"""
        self.running = True
        self.connect_private()
        time.sleep(0.5)
        self.connect_public()
    
    def connect_public(self):
        """Connect to public stream"""
        log_info("Connecting to public WebSocket...", "WebSocket")
        
        def on_open(ws):
            log_info("Public WebSocket connected", "WebSocket")
            self.public_connected = True
            self.last_public_pong = time.time()
            if self.public_subscriptions:
                self._subscribe_internal(ws, self.public_subscriptions)
            self._start_ping_thread('public')
        
        def on_message(ws, message):
            self._on_public_message(message)
        
        def on_close(ws, close_code, close_msg):
            log_warn(f"Public WebSocket closed: {close_code}", "WebSocket")
            self.public_connected = False
            if self.running:
                threading.Thread(target=self._reconnect_public, daemon=True).start()
        
        def on_error(ws, error):
            log_error(f"Public WebSocket error: {error}", "WebSocket")
        
        self.public_ws = websocket.WebSocketApp(
            self.PUBLIC_URL,
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error
        )
        
        self.public_thread = threading.Thread(
            target=self.public_ws.run_forever,
            daemon=True
        )
        self.public_thread.start()
    
    def connect_private(self):
        """Connect to private stream"""
        log_info("Connecting to private WebSocket...", "WebSocket")
        
        def on_open(ws):
            log_info("Private WebSocket connected", "WebSocket")
            self.private_connected = True
            self.last_private_pong = time.time()
            self._authenticate(ws)
        
        def on_message(ws, message):
            self._on_private_message(ws, message)
        
        def on_close(ws, close_code, close_msg):
            log_warn(f"Private WebSocket closed: {close_code}", "WebSocket")
            self.private_connected = False
            self.private_authenticated = False
            if self.running:
                threading.Thread(target=self._reconnect_private, daemon=True).start()
        
        def on_error(ws, error):
            log_error(f"Private WebSocket error: {error}", "WebSocket")
        
        self.private_ws = websocket.WebSocketApp(
            self.PRIVATE_URL,
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error
        )
        
        self.private_thread = threading.Thread(
            target=self.private_ws.run_forever,
            daemon=True
        )
        self.private_thread.start()
    
    def _authenticate(self, ws):
        """Send auth message"""
        expires = int((time.time() + 10) * 1000)
        sign_str = f"GET/realtime{expires}"
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        auth_msg = {'op': 'auth', 'args': [self.api_key, expires, signature]}
        ws.send(json.dumps(auth_msg))
    
    def _on_public_message(self, message: str):
        """Handle public message"""
        try:
            data = json.loads(message)
            
            if data.get('op') == 'pong' or data.get('ret_msg') == 'pong':
                self.last_public_pong = time.time()
                return
            
            if data.get('op') == 'subscribe':
                return
            
            # Handle ticker data
            topic = data.get('topic', '')
            if topic.startswith('tickers.'):
                ticker_data = data.get('data', {})
                symbol = ticker_data.get('symbol', '')
                if symbol:
                    with self.ticker_lock:
                        self.tickers[symbol] = ticker_data
                
                # Call callback
                if 'tickers' in self.callbacks:
                    self.callbacks['tickers'](data)
            
        except Exception as e:
            log_error(f"Public message error: {e}", "WebSocket")
    
    def _on_private_message(self, ws, message: str):
        """Handle private message"""
        try:
            data = json.loads(message)
            
            # Handle auth
            if data.get('op') == 'auth':
                if data.get('success') or data.get('retCode') == 0:
                    log_info("Private WebSocket authenticated", "WebSocket")
                    self.private_authenticated = True
                    
                    if self.private_subscriptions:
                        self._subscribe_internal(ws, self.private_subscriptions)
                    
                    # Start clock sync
                    threading.Thread(target=self._initial_clock_sync, args=(ws,), daemon=True).start()
                    self._start_ping_thread('private')
                else:
                    log_error(f"Auth failed: {data}", "WebSocket")
                return
            
            # Handle pong with clock sync
            if data.get('op') == 'pong':
                self.last_private_pong = time.time()
                local_recv_ms = int(time.time() * 1000)
                
                req_id = data.get('req_id', '')
                send_time_ms = self.ping_send_times.pop(req_id, 0)
                
                pong_args = data.get('args', [])
                if pong_args and send_time_ms > 0:
                    try:
                        server_ts = int(pong_args[0])
                        rtt = local_recv_ms - send_time_ms
                        one_way = rtt // 2
                        offset = (server_ts + one_way) - local_recv_ms
                        
                        with self.clock_sync_lock:
                            self.clock_offset_samples.append(offset)
                            if len(self.clock_offset_samples) > 10:
                                self.clock_offset_samples.pop(0)
                            sorted_samples = sorted(self.clock_offset_samples)
                            self.clock_offset_ms = sorted_samples[len(sorted_samples) // 2]
                    except:
                        pass
                return
            
            if data.get('op') == 'subscribe':
                return
            
            # Route to callbacks
            topic = data.get('topic', '')
            
            if 'order' in topic and 'order' in self.callbacks:
                self.callbacks['order'](data)
            elif 'execution' in topic and 'execution' in self.callbacks:
                self.callbacks['execution'](data)
            
        except Exception as e:
            log_error(f"Private message error: {e}", "WebSocket")
    
    def _initial_clock_sync(self, ws):
        """Initial clock sync with 10 pings"""
        log_info("Starting clock sync (10 pings)...", "WebSocket")
        
        for i in range(10):
            try:
                self.ping_counter += 1
                req_id = f"sync_{self.ping_counter}"
                self.ping_send_times[req_id] = int(time.time() * 1000)
                ws.send(json.dumps({'req_id': req_id, 'op': 'ping'}))
                time.sleep(0.05)
            except:
                pass
        
        time.sleep(1.0)
        
        with self.clock_sync_lock:
            if len(self.clock_offset_samples) > 2:
                valid = self.clock_offset_samples[2:]
                sorted_samples = sorted(valid)
                self.clock_offset_ms = sorted_samples[len(sorted_samples) // 2]
                log_info(f"Clock sync complete: offset={self.clock_offset_ms}ms", "WebSocket")
    
    def _start_ping_thread(self, stream_type: str):
        """Start ping thread"""
        interval = 10 if stream_type == 'private' else 20
        
        def ping_loop():
            while self.running:
                try:
                    ws = self.private_ws if stream_type == 'private' else self.public_ws
                    if ws and ws.sock and ws.sock.connected:
                        if stream_type == 'private':
                            self.ping_counter += 1
                            req_id = f"ping_{self.ping_counter}"
                            self.ping_send_times[req_id] = int(time.time() * 1000)
                            ws.send(json.dumps({'req_id': req_id, 'op': 'ping'}))
                        else:
                            ws.send(json.dumps({'op': 'ping'}))
                except:
                    break
                time.sleep(interval)
        
        threading.Thread(target=ping_loop, daemon=True).start()
    
    def _reconnect_public(self):
        time.sleep(2)
        if self.running:
            self.connect_public()
    
    def _reconnect_private(self):
        time.sleep(2)
        if self.running:
            self.connect_private()
    
    def subscribe_public(self, topics: List[str]):
        """Subscribe to public topics"""
        self.public_subscriptions.extend(topics)
        if self.public_connected and self.public_ws:
            self._subscribe_internal(self.public_ws, topics)
    
    def subscribe_private(self, topics: List[str]):
        """Subscribe to private topics"""
        self.private_subscriptions.extend(topics)
        if self.private_authenticated and self.private_ws:
            self._subscribe_internal(self.private_ws, topics)
    
    def _subscribe_internal(self, ws, topics: List[str]):
        msg = {'op': 'subscribe', 'args': topics}
        ws.send(json.dumps(msg))
        log_debug(f"Subscribed: {topics}", "WebSocket")
    
    def on(self, topic: str, callback: Callable):
        """Register callback"""
        self.callbacks[topic] = callback
    
    def get_ticker(self, symbol: str) -> Optional[Dict]:
        """Get latest ticker for symbol"""
        with self.ticker_lock:
            return self.tickers.get(symbol)
    
    def get_bybit_time_ms(self) -> int:
        """Get current Bybit time"""
        return int(time.time() * 1000) + self.clock_offset_ms
    
    def wait_for_connection(self, timeout_s: float = 30.0) -> bool:
        """Wait for connection"""
        start = time.time()
        while not (self.public_connected and self.private_authenticated):
            if time.time() - start > timeout_s:
                return False
            time.sleep(0.1)
        return True
    
    def close_all(self):
        """Close all connections"""
        self.running = False
        if self.public_ws:
            self.public_ws.close()
        if self.private_ws:
            self.private_ws.close()
        log_info("WebSocket connections closed", "WebSocket")
