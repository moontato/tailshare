"""Smoke tests for the Textual TUI using App.run_test."""

import subprocess
from unittest.mock import patch

import pytest
from textual.widgets import DataTable, Label, TabbedContent

import tailshare.config as config_module
from tailshare.devices import Device
from tailshare.transfer import TransferDirection
from tailshare.tui import TailshareApp, TransferQueue


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Reset the config singleton and isolate HOME for each test."""
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


async def wait_for(pilot, predicate, attempts: int = 100) -> bool:
    """Poll a predicate, giving the app a chance to process messages."""
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause()
    return False


class TestAppSmoke:
    async def test_boot_and_quit(self) -> None:
        """The app boots, shows its title, and quits on 'q'."""
        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                assert app.title == "Tailshare"
                await pilot.press("q")
                await pilot.pause()

    async def test_tailscale_unavailable_does_not_crash(self, monkeypatch) -> None:
        """Regression for B1: with tailscale missing, the error notification
        is shown and the app keeps running.

        Previously the lambda scheduled via call_later captured the except
        variable, which Python deletes when the except block exits, so the
        app crashed with NameError in the message pump.
        """

        def tailscale_missing(*args, **kwargs):
            raise FileNotFoundError("tailscale")

        monkeypatch.setattr(subprocess, "run", tailscale_missing)

        app = TailshareApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            device_list = app.query_one("#device-list", DataTable)

            populated = await wait_for(pilot, lambda: device_list.row_count > 0)
            assert populated, "device table never populated after discovery failure"
            assert device_list.row_count == 1

            assert any(
                n.title == "Tailscale Not Running" for n in app._notifications
            )

            # The app must still be responsive after the failure.
            await pilot.press("q")
            await pilot.pause()

    async def test_all_keybindings_are_safe_with_no_device(self) -> None:
        """Global keybindings must not raise when no device is selected."""
        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                for key in ("d", "s", "f", "c", "r", "R", "escape", "j", "k"):
                    await pilot.press(key)
                    await pilot.pause()
                await pilot.press("q")
                await pilot.pause()

    async def test_queue_tabs_filter_by_direction_with_chips(self, monkeypatch) -> None:
        """Each queue panel shows only its own direction, and the status
        chips update even before a worker has started."""

        def tailscale_missing(*args, **kwargs):
            raise FileNotFoundError("tailscale")

        monkeypatch.setattr(subprocess, "run", tailscale_missing)

        device = Device(
            name="dev",
            hostname="dev",
            ip="100.64.0.5",
            online=True,
            last_seen=None,
            machine_id="m-dev",
        )

        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause()

                app._transfer_manager.queue_transfer(
                    "/tmp/a.txt", "remote/a.txt", device,
                    direction=TransferDirection.SEND,
                )
                app._transfer_manager.queue_transfer(
                    "remote/f.bin", "/tmp/out", device,
                    direction=TransferDirection.FETCH,
                )

                # First refresh before a worker exists: chips update,
                # the queue panel waits for the worker (by design)
                app._update_queue_display()
                await pilot.pause()

                # Simulate the worker being active, then refresh
                app._queue_update_enabled = True
                app._update_queue_display()
                await pilot.pause()

                send_chip = app.query_one("#send-status-chip", Label)
                fetch_chip = app.query_one("#fetch-status-chip", Label)
                assert "1 active" in str(send_chip.render())
                assert "1 active" in str(fetch_chip.render())

                # Send tab (active by default) shows only send tasks
                send_queue = app.query_one("#transfer-queue", TransferQueue)
                assert len(send_queue._tasks) == 1
                assert all(
                    t.direction is TransferDirection.SEND for t in send_queue._tasks
                )

                # Switch to the fetch tab: its panel shows only fetch tasks
                tabs = app.query_one("#transfer-tabs", TabbedContent)
                tabs.active = "fetch-tab"
                app._update_queue_display()
                await pilot.pause()

                fetch_queue = app.query_one("#transfer-queue-fetch", TransferQueue)
                assert len(fetch_queue._tasks) == 1
                assert all(
                    t.direction is TransferDirection.FETCH for t in fetch_queue._tasks
                )

                await pilot.press("q")
                await pilot.pause()
