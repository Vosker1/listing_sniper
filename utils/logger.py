"""
Logger Module - Simplified for Listing Sniper
"""

import os
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional


class Logger:
    """Main logger with console and file support"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, output_dir: str = './data/logs', level: str = 'INFO',
                 console_enabled: bool = True, file_enabled: bool = True):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Setup Python logger
        self.logger = logging.getLogger('ListingSniper')
        self.logger.setLevel(getattr(logging, level.upper()))
        self.logger.handlers.clear()
        
        # Formatter
        formatter = logging.Formatter(
            '[%(asctime)s.%(msecs)03d] [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console handler
        if console_enabled:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)
        
        # File handler
        if file_enabled:
            log_file = self.output_dir / 'sniper.log'
            file_handler = logging.FileHandler(log_file, mode='a')
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
    
    def debug(self, msg: str, module: str = 'Main'):
        self.logger.debug(f'[{module}] {msg}')
    
    def info(self, msg: str, module: str = 'Main'):
        self.logger.info(f'[{module}] {msg}')
    
    def warn(self, msg: str, module: str = 'Main'):
        self.logger.warning(f'[{module}] {msg}')
    
    def error(self, msg: str, module: str = 'Main'):
        self.logger.error(f'[{module}] {msg}')


# Global logger instance
_logger: Optional[Logger] = None


def init_logger(output_dir: str = './data/logs', level: str = 'INFO',
                console_enabled: bool = True, file_enabled: bool = True) -> Logger:
    """Initialize global logger"""
    global _logger
    _logger = Logger(output_dir, level, console_enabled, file_enabled)
    return _logger


def get_logger() -> Logger:
    """Get global logger instance"""
    global _logger
    if _logger is None:
        _logger = Logger()
    return _logger


# Convenience functions
def log_debug(msg: str, module: str = 'Main'):
    get_logger().debug(msg, module)

def log_info(msg: str, module: str = 'Main'):
    get_logger().info(msg, module)

def log_warn(msg: str, module: str = 'Main'):
    get_logger().warn(msg, module)

def log_error(msg: str, module: str = 'Main'):
    get_logger().error(msg, module)
