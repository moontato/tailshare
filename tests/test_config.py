"""Configuration management and logging setup tests."""

import logging

import pytest

import tailshare.config as config_module
from tailshare.config import Config, get_config, setup_logging


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Reset the config singleton and isolate HOME for each test."""
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


class TestConfig:
    def test_defaults_when_no_file(self) -> None:
        config = Config(config_path="/nonexistent/config.yaml")

        assert config.get_ssh_port() == 22
        assert config.get_ssh_timeout() == 30
        assert config.get_ssh_key_paths() == []
        # only the ssh section exists; phantom sections must not appear
        assert config.get("ui") is None
        assert config.get("transfer") is None

    def test_file_overrides_defaults_with_deep_merge(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("ssh:\n  port: 2222\n")
        config = Config(config_path=str(path))

        assert config.get_ssh_port() == 2222
        # untouched keys keep their defaults (recursive merge, not replace)
        assert config.get_ssh_timeout() == 30
        assert config.get_ssh_key_paths() == []

    def test_invalid_yaml_falls_back_to_defaults(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("ssh: [unclosed\n  bad")
        config = Config(config_path=str(path))

        assert config.get_ssh_port() == 22

    def test_get_and_set(self) -> None:
        config = Config(config_path="/nonexistent/config.yaml")

        assert config.get("ssh", "port") == 22
        assert config.get("does", "not", "exist", default=42) == 42

        config.set("custom.nested.value", 7)
        assert config.get("custom", "nested", "value") == 7
        # instance mutations must not leak into the class-level defaults
        assert "custom" not in Config.DEFAULT_CONFIG

    def test_get_ssh_user_falls_back_to_local_user(self, monkeypatch) -> None:
        monkeypatch.setattr(
            config_module.getpass, "getuser", lambda: "localuser"
        )
        config = Config(config_path="/nonexistent/config.yaml")

        assert config.get_ssh_user() == "localuser"

        config.set("ssh.user", "remoteuser")
        assert config.get_ssh_user() == "remoteuser"

    def test_get_config_is_a_singleton(self) -> None:
        assert get_config() is get_config()


class TestSetupLogging:
    def test_creates_log_file_without_duplicate_handlers(self, tmp_path) -> None:
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            setup_logging()
            log_path = tmp_path / "home" / ".tailscale_share" / "log.txt"

            assert log_path.exists()
            assert len(root.handlers) == 1

            # calling again must not pile on duplicate handlers
            setup_logging()
            assert len(root.handlers) == 1
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers = original_handlers
