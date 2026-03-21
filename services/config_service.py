import os
from typing import Any


class ConfigService:
    """Service for managing application configuration stored in .properties file."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = {}
        self._load()

    def _load(self):
        """Load configuration from .properties file."""
        if not os.path.exists(self.config_path):
            # Create with defaults
            self.config = {
                'auth.enabled': 'false',
                'session.lifetime_days': '7',
                'password.min_length': '8',
                'api.rate_limit_per_minute': '10',
            }
            self.save()
            return

        with open(self.config_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    self.config[key.strip()] = value.strip()

    def get(self, key: str, default: Any = None) -> str:
        """Get configuration value by key."""
        return self.config.get(key, str(default) if default is not None else None)

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get boolean configuration value."""
        value = self.get(key, str(default).lower())
        return value.lower() in ('true', '1', 'yes', 'on')

    def get_int(self, key: str, default: int = 0) -> int:
        """Get integer configuration value."""
        value = self.get(key, str(default))
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def set(self, key: str, value: Any):
        """Set configuration value."""
        self.config[key] = str(value)

    def save(self):
        """Save configuration to .properties file."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w') as f:
            f.write('# Beekeeper Configuration\n')
            f.write('# This file is auto-generated. Edit via admin panel.\n\n')
            for key, value in sorted(self.config.items()):
                f.write(f'{key}={value}\n')


# Global config instance (initialized in app.py)
_config: ConfigService | None = None


def init_config(beekeeper_home: str):
    """Initialize the global config service."""
    global _config
    config_path = os.path.join(beekeeper_home, 'config.properties')
    _config = ConfigService(config_path)


def get_config(key: str, default: Any = None) -> str:
    """Get configuration value."""
    if _config is None:
        raise RuntimeError("Config service not initialized")
    return _config.get(key, default)


def get_config_bool(key: str, default: bool = False) -> bool:
    """Get boolean configuration value."""
    if _config is None:
        raise RuntimeError("Config service not initialized")
    return _config.get_bool(key, default)


def get_config_int(key: str, default: int = 0) -> int:
    """Get integer configuration value."""
    if _config is None:
        raise RuntimeError("Config service not initialized")
    return _config.get_int(key, default)


def set_config(key: str, value: Any):
    """Set configuration value."""
    if _config is None:
        raise RuntimeError("Config service not initialized")
    _config.set(key, value)


def save_config():
    """Save configuration to disk."""
    if _config is None:
        raise RuntimeError("Config service not initialized")
    _config.save()


def is_auth_enabled() -> bool:
    """Check if authentication is enabled."""
    return get_config_bool('auth.enabled', False)
