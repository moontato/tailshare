"""Configuration and utilities for tailshare.

This module handles:
- Application paths (config, data, logs)
- Configuration file management (YAML-based)
- Logging setup
- Utility functions
"""

import copy
import getpass
import logging
import os
from pathlib import Path
from typing import Any

import yaml


class Config:
    """Application configuration manager.

    Handles loading, saving, and accessing configuration settings.
    Configuration is stored in ~/.config/tailshare/config.yaml
    """

    DEFAULT_CONFIG: dict[str, Any] = {
        "ssh": {
            "key_paths": [],  # Empty list means use default SSH keys
            "user": None,     # Remote SSH username (None uses local user)
            "timeout": 30,
            "port": 22,
        },
        "ui": {
            "refresh_interval": 5,  # seconds
        },
        "transfer": {
            "show_hidden_files": False,
        },
    }

    def __init__(self, config_path: str | None = None) -> None:
        """Initialize configuration with defaults.

        Args:
            config_path: Custom path to config file. If None, uses default.
        """
        self._config: dict[str, Any] = {}
        self._config_path: Path = (
            Path(config_path) if config_path else self._get_config_path()
        )
        self._load_config()

    def _get_config_path(self) -> Path:
        """Get the configuration file path.

        Returns:
            Path to the config file (default: ~/.config/tailshare/config.yaml)
        """
        config_dir = Path.home() / ".config" / "tailshare"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "config.yaml"

    def get_log_path(self) -> Path:
        """Get the log file path.

        Returns:
            Path to the log file (default: ~/.tailscale_share/log.txt)
        """
        log_dir = Path.home() / ".tailscale_share"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "log.txt"

    def _load_config(self) -> None:
        """Load configuration from file or use defaults."""
        self._config = copy.deepcopy(self.DEFAULT_CONFIG)

        if self._config_path.exists():
            try:
                with open(self._config_path) as f:
                    file_config = yaml.safe_load(f)
                    if file_config:
                        self._merge_config(self._config, file_config)
            except (yaml.YAMLError, OSError) as e:
                logging.warning(f"Failed to load config file: {e}. Using defaults.")

    def _merge_config(self, base: dict[str, Any], override: dict[str, Any]) -> None:
        """Recursively merge override config into base config.

        Args:
            base: Base configuration dictionary (modified in place)
            override: Override configuration dictionary
        """
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge_config(base[key], value)
            else:
                base[key] = value

    def save_config(self) -> None:
        """Save current configuration to file."""
        try:
            with open(self._config_path, "w") as f:
                yaml.dump(self._config, f, default_flow_style=False, sort_keys=False)
        except OSError as e:
            logging.error(f"Failed to save config file: {e}")
            raise

    def get(self, *keys: str, default: Any = None) -> Any:
        """Get a configuration value by key path.

        Args:
            *keys: Dot-separated key path (e.g., "ssh", "key_paths")
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        value: Any = self._config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, key_path: str, value: Any) -> None:
        """Set a configuration value by key path.

        Args:
            key_path: Dot-separated key path (e.g., "ssh.key_paths")
            value: Value to set
        """
        keys = key_path.split(".")
        current = self._config
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value

    def get_ssh_user(self) -> str:
        """Get the SSH username from configuration.

        Returns:
            SSH username (falls back to current local user)
        """
        user = self.get("ssh", "user", default=None)
        return user if user else getpass.getuser()

    def get_ssh_key_paths(self) -> list[str]:
        """Get SSH key paths from configuration.

        Returns empty list if no custom keys configured, indicating
        that default SSH keys should be used.

        Returns:
            List of SSH key paths (empty for defaults)
        """
        return self.get("ssh", "key_paths", default=[])

    def get_ssh_timeout(self) -> int:
        """Get SSH connection timeout in seconds.

        Returns:
            SSH timeout in seconds (default: 30)
        """
        return self.get("ssh", "timeout", default=30)

    def get_ssh_port(self) -> int:
        """Get SSH port number.

        Returns:
            SSH port number (default: 22)
        """
        return self.get("ssh", "port", default=22)

    def get_refresh_interval(self) -> int:
        """Get UI refresh interval in seconds.

        Returns:
            Refresh interval in seconds (default: 5)
        """
        return self.get("ui", "refresh_interval", default=5)

    def should_show_hidden_files(self) -> bool:
        """Check if hidden files should be shown in file browser.

        Returns:
            True if hidden files should be shown
        """
        return self.get("transfer", "show_hidden_files", default=False)


# Global config instance
_config: Config | None = None


def get_config(config_path: str | None = None) -> Config:
    """Get the global configuration instance.

    Args:
        config_path: Custom path to config file. Only used on first call.

    Returns:
        Global Config instance (creates if needed)
    """
    global _config  # noqa: PLW0603 - module-level singleton by design
    if _config is None:
        _config = Config(config_path=config_path)
    return _config


def setup_logging() -> None:
    """Set up application logging.

    Creates log file at ~/.tailscale_share/log.txt and configures
    logging to write to both file and console.
    """
    log_path = get_config().get_log_path()

    # Create log directory if needed
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Guard against duplicate handlers if called multiple times
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path),
        ],
    )

    # Reduce verbosity for external libraries
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def validate_file_path(path: str, is_local: bool = True) -> str:
    """Validate a file path to prevent directory traversal attacks.

    Args:
        path: File path to validate
        is_local: Whether the path is on the local filesystem.
                  If False, os.path.abspath is not called.

    Returns:
        Normalized path

    Raises:
        ValueError: If path contains directory traversal sequences
    """
    # Check for directory traversal on the RAW path before normalization,
    # because os.path.normpath resolves ".." and would hide the attack.
    raw_parts = path.replace("\\", "/").split("/")
    if ".." in raw_parts:
        raise ValueError(f"Invalid path: directory traversal detected: {path}")

    # Normalize path
    normalized = os.path.normpath(path)

    # Convert to absolute path only if it's local
    if is_local and not os.path.isabs(normalized):
        normalized = os.path.abspath(normalized)

    return normalized


def expand_path(path: str) -> str:
    """Expand a path, handling ~ and environment variables.

    Args:
        path: Path to expand

    Returns:
        Expanded absolute path
    """
    # Expand ~ and environment variables
    expanded = os.path.expanduser(os.path.expandvars(path))

    # Normalize the path
    normalized = os.path.normpath(expanded)

    return normalized
