"""Tests for path validation and queue_transfer across all directions (H4)."""

import os

import pytest

from tailshare.config import validate_file_path
from tailshare.devices import Device
from tailshare.transfer import (
    TransferDirection,
    TransferManager,
)


def make_device() -> Device:
    return Device(
        name="test-pc",
        hostname="test-pc",
        ip="100.64.0.1",
        online=True,
        last_seen=None,
        machine_id="",
    )


class TestValidateFilePath:
    """Tests for validate_file_path (config.py)."""

    def test_local_absolute_passthrough(self, tmp_path) -> None:
        path = str(tmp_path / "sub" / "file.txt")
        assert validate_file_path(path) == path

    def test_local_relative_becomes_absolute(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert validate_file_path("sub/file.txt") == str(tmp_path / "sub" / "file.txt")

    def test_local_traversal_rejected(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="traversal"):
            validate_file_path(f"{tmp_path}/../secrets")

    def test_local_traversal_rejected_even_if_normalizing_back_inside(
        self, tmp_path
    ) -> None:
        # Textually resolves back inside tmp_path, but the ".." must still
        # be rejected (the raw check runs before normpath).
        sneaky = f"{tmp_path}/sub/../../{os.path.basename(tmp_path)}/file"
        with pytest.raises(ValueError, match="traversal"):
            validate_file_path(sneaky)

    def test_local_tilde_and_env_expansion(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert (
            validate_file_path("~/docs", is_local=True, expand=True)
            == str(tmp_path / "docs")
        )
        monkeypatch.setenv("TAILSHARE_TEST_DIR", str(tmp_path))
        assert (
            validate_file_path(
                "$TAILSHARE_TEST_DIR/x", is_local=True, expand=True
            )
            == str(tmp_path / "x")
        )

    def test_remote_relative_stays_relative(self) -> None:
        # is_local=False: no abspath, remote paths stay relative
        assert (
            validate_file_path("downloads/file.bin", is_local=False)
            == "downloads/file.bin"
        )

    def test_remote_tilde_stays_literal(self, monkeypatch) -> None:
        monkeypatch.setenv("HOME", "/home/someone")
        # Remote paths are never expanded: '~' stays literal (the SFTP layer
        # maps it to the remote home directory itself).
        assert validate_file_path("~/file.bin", is_local=False) == "~/file.bin"

    def test_remote_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            validate_file_path("../../etc/shadow", is_local=False)

    def test_traversal_rejected_before_expansion(self, tmp_path, monkeypatch) -> None:
        # Regression: expansion (normpath) must not run before the raw
        # check, or ".." sequences would be resolved away and hidden.
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(ValueError, match="traversal"):
            validate_file_path("~/x/../../etc/shadow", is_local=True, expand=True)


class TestQueueTransferValidation:
    """queue_transfer must validate all four path roles (H4)."""

    def test_send_local_source_traversal_rejected(self, tmp_path) -> None:
        manager = TransferManager()
        with pytest.raises(ValueError, match="traversal"):
            manager.queue_transfer(
                f"{tmp_path}/../etc",
                "remote/dir",
                make_device(),
                direction=TransferDirection.SEND,
            )

    def test_send_remote_target_traversal_rejected(self, tmp_path) -> None:
        manager = TransferManager()
        with pytest.raises(ValueError, match="traversal"):
            manager.queue_transfer(
                str(tmp_path / "a.txt"),
                "../../etc/cron.d/evil",
                make_device(),
                direction=TransferDirection.SEND,
            )

    def test_send_paths_normalized(self, tmp_path) -> None:
        manager = TransferManager()
        task = manager.queue_transfer(
            str(tmp_path / "a.txt"),
            "remote/dir/a.txt",
            make_device(),
            direction=TransferDirection.SEND,
        )
        assert task.source_path == str(tmp_path / "a.txt")
        # Remote target is never expanded or absolutized
        assert task.target_path == "remote/dir/a.txt"
        assert task.direction is TransferDirection.SEND
        assert task in manager.get_pending_tasks()

    def test_fetch_remote_source_traversal_rejected(self, tmp_path) -> None:
        manager = TransferManager()
        with pytest.raises(ValueError, match="traversal"):
            manager.queue_transfer(
                "../../etc/passwd",
                str(tmp_path / "out"),
                make_device(),
                direction=TransferDirection.FETCH,
            )

    def test_fetch_local_target_traversal_rejected_after_expansion(
        self, tmp_path, monkeypatch
    ) -> None:
        # Regression: the old code ran expand_path() (which normpath's)
        # before the raw traversal check, hiding ".." sequences entirely.
        monkeypatch.setenv("HOME", str(tmp_path))
        manager = TransferManager()
        with pytest.raises(ValueError, match="traversal"):
            manager.queue_transfer(
                "remote/file.bin",
                "~/x/../../etc/shadow",
                make_device(),
                direction=TransferDirection.FETCH,
            )

    def test_fetch_local_target_tilde_expanded(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        manager = TransferManager()
        task = manager.queue_transfer(
            "remote/file.bin",
            "~/downloads",
            make_device(),
            direction=TransferDirection.FETCH,
        )
        assert task.target_path == str(tmp_path / "downloads")
        assert task.direction is TransferDirection.FETCH
