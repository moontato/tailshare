"""TUI tests for the device list: ordering and offline dimming."""

import pytest
from textual.widgets import DataTable

import tailshare.config as config_module
from tailshare.devices import Device
from tailshare.tui import TailshareApp


def _device(name: str, ip: str, online: bool) -> Device:
    return Device(
        name=name,
        hostname=f"{name}.test.ts.net",
        ip=ip,
        online=online,
        last_seen="now" if online else "3h ago",
        machine_id=f"m-{name}",
    )


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Reset the config singleton and isolate HOME for each test."""
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


async def _wait_until(pilot, predicate, attempts: int = 200) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause()
    return False


def _names(table: DataTable) -> list[str]:
    return [table.get_cell_at((i, 0)) for i in range(table.row_count)]


class TestDeviceListDisplay:
    async def test_online_first_offline_dimmed(self, monkeypatch) -> None:
        """Online devices list first, then offline, alpha within each group.

        Discovery order is deliberately NOT the expected display order, so a
        no-op sort would fail. Offline rows stay present (not hidden) but
        carry dim markup; online rows do not.
        """
        # Input order is unsorted: online and offline interleaved.
        devices = [
            _device("zeta", "100.64.0.9", online=True),
            _device("alpha", "100.64.0.1", online=True),
            _device("mid", "100.64.0.5", online=False),
            _device("omega", "100.64.0.7", online=False),
        ]
        app = TailshareApp()
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            device_list = app.query_one("#device-list", DataTable)
            assert await _wait_until(pilot, lambda: device_list.row_count == 4)
            names = _names(device_list)

            assert names[0].startswith("alpha") and "[dim]" not in names[0]
            assert names[1].startswith("zeta") and "[dim]" not in names[1]
            assert "mid" in names[2] and "[dim]" in names[2]
            assert "omega" in names[3] and "[dim]" in names[3]

            await pilot.press("q")
            await pilot.pause()

    async def test_selected_offline_device_keeps_highlight(self, monkeypatch) -> None:
        """A selected device that is offline keeps its selection highlight
        rather than being dimmed, and remains present in the list."""
        devices = [
            _device("server", "100.64.0.30", online=False),
            _device("laptop", "100.64.0.40", online=True),
        ]
        app = TailshareApp()
        monkeypatch.setattr(app._device_discovery, "discover", lambda: devices)
        monkeypatch.setattr(app._device_discovery, "get_devices", lambda: devices)
        monkeypatch.setattr(app, "_connect_for_remote_browser", lambda: None)

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            device_list = app.query_one("#device-list", DataTable)
            assert await _wait_until(pilot, lambda: device_list.row_count == 2)

            # Select the offline device through the real selection path.
            app._select_device_by_key("m-server")
            assert await _wait_until(
                pilot,
                lambda: any("[background" in c for c in _names(device_list)),
            )
            names = _names(device_list)

            # Still two rows (offline not hidden), order preserved: laptop
            # (online) first, server (offline) second.
            assert device_list.row_count == 2
            assert "laptop" in names[0] and "[dim]" not in names[0]
            assert "server" in names[1]
            # Selected offline row: highlighted, not dimmed.
            assert "[background" in names[1]
            assert "[dim]" not in names[1]

            await pilot.press("q")
            await pilot.pause()
