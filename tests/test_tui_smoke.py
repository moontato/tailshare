"""Smoke tests for the Textual TUI using App.run_test."""

import subprocess
from unittest.mock import patch

import pytest
from textual.widgets import DataTable, Label, TabbedContent
from textual.widgets._data_table import CellKey, ColumnKey, RowKey

import tailshare.config as config_module
from tailshare.devices import Device
from tailshare.transfer import TransferDirection
from tailshare.tui import TailshareApp, TransferQueueTable


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
                send_queue = app.query_one(
                    "#transfer-queue", TransferQueueTable
                )
                assert send_queue.row_count == 1
                assert all(
                    t.direction is TransferDirection.SEND
                    for t in send_queue._row_tasks.values()
                )

                # Switch to the fetch tab: its panel shows only fetch tasks
                tabs = app.query_one("#transfer-tabs", TabbedContent)
                tabs.active = "fetch-tab"
                app._update_queue_display()
                await pilot.pause()

                fetch_queue = app.query_one(
                    "#transfer-queue-fetch", TransferQueueTable
                )
                assert fetch_queue.row_count == 1
                assert all(
                    t.direction is TransferDirection.FETCH
                    for t in fetch_queue._row_tasks.values()
                )

                await pilot.press("q")
                await pilot.pause()


class TestQueueRemoval:
    """Removing queued jobs from the queue tables (x / delete / click)."""

    @staticmethod
    def _device() -> Device:
        return Device(
            name="dev",
            hostname="dev",
            ip="100.64.0.5",
            online=True,
            last_seen=None,
            machine_id="m-dev",
        )

    @staticmethod
    def _cancel_cell_event(
        table: TransferQueueTable, task
    ) -> DataTable.CellSelected:
        """Build a CellSelected event for the row's cancel column."""
        return DataTable.CellSelected(
            table,
            "✕",
            (0, 3),
            CellKey(RowKey(task.task_id), ColumnKey(TransferQueueTable.CANCEL_COLUMN)),
        )

    async def test_queue_table_lists_job_with_cancel_column(self, monkeypatch):
        def tailscale_missing(*args, **kwargs):
            raise FileNotFoundError("tailscale")

        monkeypatch.setattr(subprocess, "run", tailscale_missing)

        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause()

                task = app._transfer_manager.queue_transfer(
                    "/tmp/a.txt", "remote/a.txt", self._device(),
                    direction=TransferDirection.SEND,
                )
                app._queue_update_enabled = True
                app._update_queue_display()
                await pilot.pause()

                send_queue = app.query_one("#transfer-queue", TransferQueueTable)
                assert send_queue.row_count == 1
                assert send_queue.get_cell_at((0, 1)) == "a.txt"
                assert send_queue.get_cell_at((0, 3)) == "✕"
                assert task.task_id in send_queue._row_tasks

                # Fixed columns are wide enough for header and content
                assert send_queue.columns["status"].width == 6
                assert send_queue.columns["progress"].width == 18
                assert send_queue.columns["cancel"].width == 2
                assert send_queue.columns["name"].width >= 10

                # Hover highlighting is suppressed on the queue tables
                for pseudo in ("datatable--hover", "datatable--header-hover"):
                    styles = send_queue._component_styles[pseudo]
                    assert styles.base.background.is_transparent

                await pilot.press("q")
                await pilot.pause()

    async def test_press_x_removes_selected_job(self, monkeypatch):
        def tailscale_missing(*args, **kwargs):
            raise FileNotFoundError("tailscale")

        monkeypatch.setattr(subprocess, "run", tailscale_missing)

        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause()

                app._transfer_manager.queue_transfer(
                    "/tmp/a.txt", "remote/a.txt", self._device(),
                    direction=TransferDirection.SEND,
                )
                app._queue_update_enabled = True
                app._update_queue_display()
                await pilot.pause()

                send_queue = app.query_one("#transfer-queue", TransferQueueTable)
                send_queue.focus()
                send_queue.move_cursor(row=0, column=0)
                await pilot.press("x")
                await pilot.pause()

                assert app._transfer_manager.get_all_tasks() == []
                assert send_queue.row_count == 0
                assert any(
                    "Removed a.txt from queue" in n.message
                    for n in app._notifications
                )

                await pilot.press("q")
                await pilot.pause()

    async def test_clicking_cancel_cell_removes_job(self, monkeypatch):
        def tailscale_missing(*args, **kwargs):
            raise FileNotFoundError("tailscale")

        monkeypatch.setattr(subprocess, "run", tailscale_missing)

        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause()

                task = app._transfer_manager.queue_transfer(
                    "/tmp/a.txt", "remote/a.txt", self._device(),
                    direction=TransferDirection.SEND,
                )
                app._queue_update_enabled = True
                app._update_queue_display()
                await pilot.pause()

                send_queue = app.query_one("#transfer-queue", TransferQueueTable)
                send_queue.post_message(
                    self._cancel_cell_event(send_queue, task)
                )
                await pilot.pause()

                assert app._transfer_manager.get_all_tasks() == []
                assert send_queue.row_count == 0

                await pilot.press("q")
                await pilot.pause()

    async def test_clicking_other_cell_does_not_remove(self, monkeypatch):
        def tailscale_missing(*args, **kwargs):
            raise FileNotFoundError("tailscale")

        monkeypatch.setattr(subprocess, "run", tailscale_missing)

        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause()

                task = app._transfer_manager.queue_transfer(
                    "/tmp/a.txt", "remote/a.txt", self._device(),
                    direction=TransferDirection.SEND,
                )
                app._queue_update_enabled = True
                app._update_queue_display()
                await pilot.pause()

                send_queue = app.query_one("#transfer-queue", TransferQueueTable)
                # Click on the Name column: selects but does not remove
                send_queue.post_message(
                    DataTable.CellSelected(
                        send_queue,
                        "a.txt",
                        (0, 1),
                        CellKey(RowKey(task.task_id), ColumnKey("name")),
                    )
                )
                await pilot.pause()

                assert len(app._transfer_manager.get_all_tasks()) == 1
                assert send_queue.row_count == 1

                await pilot.press("q")
                await pilot.pause()

    async def test_cancelling_transferring_job_sets_cancel_event(self, monkeypatch):
        def tailscale_missing(*args, **kwargs):
            raise FileNotFoundError("tailscale")

        monkeypatch.setattr(subprocess, "run", tailscale_missing)

        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause()

                task = app._transfer_manager.queue_transfer(
                    "/tmp/a.txt", "remote/a.txt", self._device(),
                    direction=TransferDirection.SEND,
                )
                task.start()  # simulate an in-flight transfer
                app._queue_update_enabled = True
                app._update_queue_display()
                await pilot.pause()

                send_queue = app.query_one("#transfer-queue", TransferQueueTable)
                send_queue.post_message(
                    self._cancel_cell_event(send_queue, task)
                )
                await pilot.pause()

                assert task.cancel_event.is_set()
                assert task not in app._transfer_manager.get_all_tasks()
                assert any(
                    "Cancelling a.txt" in n.message for n in app._notifications
                )

                await pilot.press("q")
                await pilot.pause()
