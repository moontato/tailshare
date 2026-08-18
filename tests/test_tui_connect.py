"""TUI tests for device selection, async SFTP connect, and client lifecycle."""

import threading
import time

import pytest
from paramiko.ssh_exception import SSHException
from textual.widgets import DataTable

import tailshare.config as config_module
import tailshare.tui as tui_mod
from tailshare.devices import Device
from tailshare.transfer import TransferError
from tailshare.tui import TailshareApp


class FakeSFTPClient:
    """Minimal stand-in for SFTPClient covering the TUI's usage."""

    def __init__(self, device: Device) -> None:
        self.device = device
        self.connected = False
        self.disconnected = False

    def connect(self, username: str | None = None, password: str | None = None) -> None:
        self.connected = True

    def disconnect(self) -> None:
        if self.connected:
            self.disconnected = True
        self.connected = False

    def list_remote_dir(self, path: str) -> list[tuple[str, bool]]:
        return [("docs", True), ("file.txt", False)]

    def is_remote_dir(self, path: str) -> bool | None:
        return path == "docs"

    def probe_remote(self, path: str) -> str:
        if path in ("~", ".", ""):
            return "dir"
        if path == "docs":
            return "dir"
        if path == "file.txt":
            return "file"
        return "missing"

    def canonicalize(self, path: str) -> str | None:
        # Model a jailed remote: the home root has no parent above it.
        return "/"


def make_devices() -> list[Device]:
    return [
        Device(
            name="dest-pc",
            hostname="dest-pc.tailnet.ts.net",
            ip="100.64.0.20",
            online=True,
            last_seen="2m ago",
            machine_id="m-dest",
        ),
        Device(
            name="server",
            hostname="server.tailnet.ts.net",
            ip="100.64.0.30",
            online=False,
            last_seen="3h ago",
            machine_id="m-server",
        ),
    ]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Reset the config singleton and isolate HOME for each test."""
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


async def _wait_until(pilot, predicate, attempts: int = 100) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause()
    return False


def _rows(table: DataTable) -> list[str]:
    return [table.get_cell_at((i, 0)) for i in range(table.row_count)]


def _rows_contain(table: DataTable, needle: str) -> bool:
    """True when some row contains needle (rows may carry extra text)."""
    return any(needle in row for row in _rows(table))


class TestRemoteConnection:
    async def test_select_device_connects_and_browses(self, monkeypatch) -> None:
        """Selecting a device connects off-thread and populates the
        remote browser; exit teardown releases the client."""
        devices = make_devices()
        app = TailshareApp()
        monkeypatch.setattr(tui_mod, "SFTPClient", FakeSFTPClient)
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            device_list = app.query_one("#device-list", DataTable)
            assert await _wait_until(pilot, lambda: device_list.row_count == 2)

            app._select_device_by_key("m-dest")
            assert await _wait_until(
                pilot, lambda: app._remote_sftp_client is not None
            )
            assert app._remote_sftp_client.device.name == "dest-pc"

            remote_table = app.query_one("#remote-file-table", DataTable)
            assert await _wait_until(pilot, lambda: remote_table.row_count >= 2)
            rows = [
                remote_table.get_cell_at((i, 0))
                for i in range(remote_table.row_count)
            ]
            assert rows == ["..", "docs", "file.txt"]

            await pilot.press("q")
            await pilot.pause()

        assert app._remote_sftp_client is None or app._remote_sftp_client.disconnected

    async def test_switching_devices_releases_previous_client(self, monkeypatch) -> None:
        """Switching devices must disconnect the previous client (no leak)."""
        devices = make_devices()
        app = TailshareApp()
        monkeypatch.setattr(tui_mod, "SFTPClient", FakeSFTPClient)
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()

            app._select_device_by_key("m-dest")
            assert await _wait_until(
                pilot, lambda: app._remote_sftp_client is not None
            )
            first = app._remote_sftp_client

            app._select_device_by_key("m-server")
            assert await _wait_until(
                pilot, lambda: app._remote_sftp_client is not first
            )

            assert first.disconnected
            assert app._remote_sftp_client.device.name == "server"

            await pilot.press("q")
            await pilot.pause()

    async def test_connect_failure_notifies_and_keeps_app(self, monkeypatch) -> None:
        """A failed connection notifies the user without crashing."""

        class FailingClient(FakeSFTPClient):
            def connect(
                self, username: str | None = None, password: str | None = None
            ) -> None:
                raise TransferError("SSH connection failed: connection refused")

        devices = make_devices()
        app = TailshareApp()
        monkeypatch.setattr(tui_mod, "SFTPClient", FailingClient)
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()

            app._select_device_by_key("m-dest")
            assert await _wait_until(
                pilot,
                lambda: any(
                    n.title == "Connection Failed" for n in app._notifications
                ),
            )
            assert app._remote_sftp_client is None

            # The reason is shown persistently in both remote browsers,
            # not just in the transient toast.
            dest_table = app.query_one("#destination-file-table", DataTable)
            fetch_table = app.query_one("#remote-file-table", DataTable)
            assert await _wait_until(
                pilot,
                lambda: _rows_contain(dest_table, "Cannot connect to dest-pc")
                and _rows_contain(fetch_table, "Cannot connect to dest-pc"),
            )

            # The app must remain responsive after the failure.
            await pilot.press("q")
            await pilot.pause()

    async def test_connection_drop_shows_error_and_survives(self, monkeypatch) -> None:
        """A dropped SFTP connection surfaces an error entry, never a crash."""

        class DroppingClient(FakeSFTPClient):
            def probe_remote(self, path: str) -> str:
                raise SSHException("Server connection dropped: ")

            def list_remote_dir(self, path: str) -> list[tuple[str, bool]]:
                raise SSHException("Server connection dropped: ")

        devices = make_devices()
        app = TailshareApp()
        monkeypatch.setattr(tui_mod, "SFTPClient", DroppingClient)
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_device_by_key("m-dest")
            assert await _wait_until(
                pilot, lambda: app._remote_sftp_client is not None
            )

            dest_table = app.query_one("#destination-file-table", DataTable)
            fetch_table = app.query_one("#remote-file-table", DataTable)
            assert await _wait_until(
                pilot,
                lambda: "Server connection dropped" in _rows(dest_table)
                and "Server connection dropped" in _rows(fetch_table),
            )

            # The app must remain responsive after the failure.
            await pilot.press("q")
            await pilot.pause()

    async def test_connecting_state_shown_while_connecting(self, monkeypatch) -> None:
        """While a connection is in flight the browsers say so."""

        class GatedClient(FakeSFTPClient):
            def __init__(self, device: Device) -> None:
                super().__init__(device)
                self.gate = threading.Event()

            def connect(
                self, username: str | None = None, password: str | None = None
            ) -> None:
                assert self.gate.wait(5)
                self.connected = True

        clients: list[GatedClient] = []

        def factory(device: Device) -> GatedClient:
            client = GatedClient(device)
            clients.append(client)
            return client

        devices = make_devices()
        app = TailshareApp()
        monkeypatch.setattr(tui_mod, "SFTPClient", factory)
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_device_by_key("m-dest")

            dest_table = app.query_one("#destination-file-table", DataTable)
            fetch_table = app.query_one("#remote-file-table", DataTable)
            assert await _wait_until(
                pilot,
                lambda: bool(clients)
                and "Connecting to dest-pc..." in _rows(dest_table)
                and "Connecting to dest-pc..." in _rows(fetch_table),
            )

            clients[0].gate.set()
            assert await _wait_until(
                pilot,
                lambda: "docs" in _rows(dest_table) and "docs" in _rows(fetch_table),
            )

            await pilot.press("q")
            await pilot.pause()

    async def test_unexpected_connect_error_is_visible(self, monkeypatch) -> None:
        """A non-TransferError from connect() is shown, never a crash."""

        class BoomClient(FakeSFTPClient):
            def connect(
                self, username: str | None = None, password: str | None = None
            ) -> None:
                raise RuntimeError("socket exploded")

        devices = make_devices()
        app = TailshareApp()
        monkeypatch.setattr(tui_mod, "SFTPClient", BoomClient)
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_device_by_key("m-dest")

            dest_table = app.query_one("#destination-file-table", DataTable)
            fetch_table = app.query_one("#remote-file-table", DataTable)
            assert await _wait_until(
                pilot,
                lambda: _rows_contain(
                    dest_table, "Cannot connect to dest-pc: socket exploded"
                )
                and _rows_contain(
                    fetch_table, "Cannot connect to dest-pc: socket exploded"
                ),
            )

            # The app must remain responsive after the failure.
            await pilot.press("q")
            await pilot.pause()

    async def test_stale_connect_failure_does_not_clobber_newer_selection(
        self, monkeypatch
    ) -> None:
        """A late failure for an abandoned device must not clobber the
        state of the device selected in its place."""

        class FailingDelayedClient(FakeSFTPClient):
            def __init__(self, device: Device) -> None:
                super().__init__(device)
                self.failed = threading.Event()

            def connect(
                self, username: str | None = None, password: str | None = None
            ) -> None:
                time.sleep(0.3)
                self.failed.set()
                raise TransferError("connection refused")

        clients: list[FailingDelayedClient] = []

        def factory(device: Device) -> FakeSFTPClient:
            if device.machine_id == "m-dest":
                client = FailingDelayedClient(device)
                clients.append(client)
                return client
            return FakeSFTPClient(device)

        devices = make_devices()
        app = TailshareApp()
        monkeypatch.setattr(tui_mod, "SFTPClient", factory)
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_device_by_key("m-dest")
            await pilot.pause()
            # Abandon the slow, failing device for one that connects.
            app._select_device_by_key("m-server")
            assert await _wait_until(
                pilot,
                lambda: app._remote_sftp_client is not None
                and app._remote_sftp_client.device.name == "server",
            )

            # Let the late failure for the abandoned device arrive.
            assert await _wait_until(
                pilot, lambda: bool(clients) and clients[0].failed.is_set()
            )
            for _ in range(20):
                await pilot.pause()

            dest_table = app.query_one("#destination-file-table", DataTable)
            fetch_table = app.query_one("#remote-file-table", DataTable)
            assert not _rows_contain(dest_table, "Cannot connect to dest-pc")
            assert not _rows_contain(fetch_table, "Cannot connect to dest-pc")
            assert "docs" in _rows(dest_table)
            assert "docs" in _rows(fetch_table)

            await pilot.press("q")
            await pilot.pause()
