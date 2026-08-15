"""Tests for the CLI entry point."""

import pytest

import tailshare.__main__ as main_module
import tailshare.config as config_module
from tailshare.config import get_config


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Reset the config singleton and isolate HOME for each test."""
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


class TestMain:
    def test_config_path_is_applied(self, monkeypatch, tmp_path) -> None:
        """--config must take effect (regression for B3).

        setup_logging() initializes the config singleton; if it runs before
        the --config path is applied, the custom path is silently ignored.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text("ssh:\n  timeout: 7\n")

        observed: dict = {}

        def fake_run_app() -> None:
            observed["config"] = get_config()

        monkeypatch.setattr(main_module, "run_app", fake_run_app)

        main_module.main(["--config", str(config_file)])

        config = observed["config"]
        assert config._config_path == config_file
        assert config.get_ssh_timeout() == 7

    def test_default_config_path_without_flag(self, monkeypatch, tmp_path) -> None:
        def fake_run_app() -> None:
            observed["config"] = get_config()

        observed: dict = {}
        monkeypatch.setattr(main_module, "run_app", fake_run_app)

        main_module.main([])

        assert observed["config"]._config_path == (
            tmp_path / ".config" / "tailshare" / "config.yaml"
        )

    def test_verbose_unpins_library_loggers(self, monkeypatch) -> None:
        """--verbose must lift the WARNING pin on paramiko/asyncio set by
        setup_logging, otherwise verbose mode shows no library detail."""
        import logging

        monkeypatch.setattr(main_module, "run_app", lambda: None)

        paramiko_level = logging.getLogger("paramiko").level
        asyncio_level = logging.getLogger("asyncio").level
        try:
            main_module.main(["--verbose"])
            assert logging.getLogger("paramiko").level == logging.DEBUG
            assert logging.getLogger("asyncio").level == logging.DEBUG
        finally:
            logging.getLogger("paramiko").setLevel(paramiko_level)
            logging.getLogger("asyncio").setLevel(asyncio_level)

    def test_version_matches_package(self, capsys) -> None:
        from tailshare import __version__

        with pytest.raises(SystemExit):
            main_module.parse_args(["--version"])

        out = capsys.readouterr().out
        assert out == f"tailshare {__version__}\n"
