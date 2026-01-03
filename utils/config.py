"""
Configuration Loader
"""

import os
import yaml
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SniperConfig:
    budget_usdt: float = 100
    poll_interval_sec: int = 5
    max_launch_age_sec: int = 3600
    poll_offset_ms: int = 100


@dataclass
class LadderConfig:
    steps: List[float] = field(default_factory=lambda: [0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003])
    repeat_per_step: int = 3
    order_interval_ms: int = 50
    max_orders: int = 100


@dataclass
class TrailingConfig:
    distance_pct: float = 4.0
    activation_pct: float = 0.0


@dataclass
class FeesConfig:
    taker_pct: float = 0.055


@dataclass
class WebSocketConfig:
    ping_interval_s: int = 20
    pong_timeout_s: int = 30
    reconnect_delay_s: int = 1
    max_reconnect_delay_s: int = 60


@dataclass
class LoggingConfig:
    level: str = 'INFO'
    console_enabled: bool = True
    file_enabled: bool = True
    output_dir: str = './data/logs'


@dataclass
class TelegramConfig:
    enabled: bool = True


@dataclass
class Config:
    api_key: str = ''
    api_secret: str = ''
    sniper: SniperConfig = field(default_factory=SniperConfig)
    ladder: LadderConfig = field(default_factory=LadderConfig)
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    fees: FeesConfig = field(default_factory=FeesConfig)
    websocket: WebSocketConfig = field(default_factory=WebSocketConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    @classmethod
    def load(cls, config_path: str = 'config.yaml', env_path: str = 'input.env') -> 'Config':
        """Load configuration from YAML and environment files"""
        base_dir = Path(__file__).parent.parent
        
        # Load environment variables
        env_file = base_dir / env_path
        if env_file.exists():
            load_dotenv(env_file)
        
        # Load YAML config
        config_file = base_dir / config_path
        yaml_config = {}
        if config_file.exists():
            with open(config_file, 'r') as f:
                yaml_config = yaml.safe_load(f) or {}
        
        # Build config object
        config = cls(
            api_key=os.getenv('BYBIT_API_KEY', ''),
            api_secret=os.getenv('BYBIT_API_SECRET', ''),
            sniper=SniperConfig(**yaml_config.get('sniper', {})),
            ladder=LadderConfig(**yaml_config.get('ladder', {})),
            trailing=TrailingConfig(**yaml_config.get('trailing', {})),
            fees=FeesConfig(**yaml_config.get('fees', {})),
            websocket=WebSocketConfig(**yaml_config.get('websocket', {})),
            logging=LoggingConfig(**yaml_config.get('logging', {})),
            telegram=TelegramConfig(**yaml_config.get('telegram', {})),
        )
        
        config.validate()
        return config

    def validate(self):
        """Validate configuration"""
        assert self.api_key, "BYBIT_API_KEY required in input.env"
        assert self.api_secret, "BYBIT_API_SECRET required in input.env"
        assert self.sniper.budget_usdt > 0, "Budget must be > 0"
        assert self.trailing.distance_pct > 0, "Trailing distance must be > 0"
