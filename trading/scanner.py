"""
Instrument Scanner - Polls for new listings
"""

import time
from typing import Set, Dict, Optional, List
from datetime import datetime

from utils.logger import log_info, log_warn, log_debug


class InstrumentScanner:
    """Scans for new USDT perpetual listings"""
    
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.known_symbols: Set[str] = set()
        self.instrument_info: Dict[str, Dict] = {}
    
    def get_usdt_perpetuals(self) -> List[Dict]:
        """Get all USDT perpetual instruments"""
        try:
            response = self.client.get_instruments_info(category='linear')
            
            if response.get('retCode') != 0:
                log_warn(f"Failed to get instruments: {response}", "Scanner")
                return []
            
            instruments = response.get('result', {}).get('list', [])
            
            # Filter: USDT perpetuals only
            perps = [
                i for i in instruments
                if i['symbol'].endswith('USDT')
                and i.get('contractType') == 'LinearPerpetual'
            ]
            
            return perps
            
        except Exception as e:
            log_warn(f"Error getting instruments: {e}", "Scanner")
            return []
    
    def initialize(self) -> int:
        """Initialize with current symbols"""
        perps = self.get_usdt_perpetuals()
        
        for p in perps:
            symbol = p['symbol']
            self.known_symbols.add(symbol)
            self.instrument_info[symbol] = p
        
        log_info(f"Initialized with {len(self.known_symbols)} USDT perpetuals", "Scanner")
        return len(self.known_symbols)
    
    def scan_for_new(self) -> List[Dict]:
        """Scan for new listings, returns list of new instruments"""
        perps = self.get_usdt_perpetuals()
        current_symbols = {p['symbol'] for p in perps}
        
        # Find new symbols
        new_symbols = current_symbols - self.known_symbols
        
        new_instruments = []
        
        for p in perps:
            symbol = p['symbol']
            
            if symbol in new_symbols:
                # Validate launch time
                launch_time = int(p.get('launchTime', 0))
                now_ms = int(time.time() * 1000)
                age_sec = (now_ms - launch_time) / 1000
                
                max_age = self.config.sniper.max_launch_age_sec
                
                if age_sec < max_age:
                    log_info(f"🎯 NEW LISTING: {symbol} (age: {age_sec:.0f}s)", "Scanner")
                    new_instruments.append(p)
                    self.instrument_info[symbol] = p
                else:
                    log_debug(f"Skipping {symbol} - too old ({age_sec:.0f}s > {max_age}s)", "Scanner")
                
                # Add to known either way
                self.known_symbols.add(symbol)
        
        return new_instruments
    
    def get_instrument_info(self, symbol: str) -> Optional[Dict]:
        """Get cached instrument info"""
        return self.instrument_info.get(symbol)
