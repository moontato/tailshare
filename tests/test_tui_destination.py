"""Tests for the Send tab's remote destination browser and path sync.

The destination browser and the remote path input must always agree on
the destination: browsing updates the input, and committing the input
re-points the browser.
"""

from unittest.mock import patch

import pytest
from textual.widgets import DataTable, Input, Label
from textual.widgets._data_table import RowKey

import tailshare.config as config_module
import tailshare.tui as tui_mod
from tailshare.devices import Device
from tailshare.transfer import TransferError
from tailshare.tui import RemoteDestinationBrowser, TailshareApp


class FakeRemoteFS:
    """In-memory remote tree (home-relative, '~' root) mimicking SFTPClient."""

    DIRS = {
        "~",
        "~/docs",
        "~/incoming",
        "~/incoming/2025",
        "~/incoming/2026",
    }
    FILES = {"~/report.txt", "~/docs/notes.md", "~/incoming/2025/old.pdf"}

    def __init__(self, device: Device) -> None:
        self.device = device
        self.disconnected = False

    def connect(self, username: str | None = None, password: str | None = None) -> None:
        pass

    def disconnect(self) -> None:
        self.disconnected = True

    @staticmethod
    def _normalize(path: str) -> str:
        if path in ("", ".", "~"):
            return "~"
        if path.startswith("~"):
            return path
        if path.startswith("/"):
            return path
        return f"~/{path}"

    def is_remote_dir(self, path: str) -> bool | None:
        p = self._normalize(path)
        if p in self.DIRS:
            return True
        if p in self.FILES:
            return False
        return None

    def list_remote_dir(self, path: str) -> list[tuple[str, bool]]:
        p = self._normalize(path)
        if p not in self.DIRS:
            raise TransferError(f"Cannot list directory {path}: no such directory")
        entries: dict[str, bool] = {}
        for other in sorted(self.DIRS | self.FILES):
            if other == p or not other.startswith(p + "/"):
                continue
            name = other[len(p) + 1 :].split("/", 1)[0]
            if name not in entries:
                entries[name] = f"{p}/{name}" in self.DIRS
        return sorted(entries.items(), key=lambda e: (not e[1], e[0]))


def make_device() -> Device:
    return Device(
        name="dest-pc",
        hostname="dest-pc.tailnet.ts.net",
        ip="100.64.0.20",
        online=True,
        last_seen="2m ago",
        machine_id="m-dest",
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


async def _select_and_connect(app: TailshareApp, monkeypatch, pilot) -> FakeRemoteFS:
    """Wire the fake SFTP client in, select the device, wait for adoption."""
    device = make_device()
    fake = FakeRemoteFS(device)
    monkeypatch.setattr(tui_mod, "SFTPClient", lambda d: fake)
    monkeypatch.setattr(app._device_discovery, "discover", lambda: [device])
    monkeypatch.setattr(app._device_discovery, "get_devices", lambda: [device])

    app._select_device_by_key("m-dest")
    assert await _wait_until(
        pilot, lambda: app._remote_sftp_client is not None
    )
    return fake


def _rows(table: DataTable) -> list[str]:
    return [table.get_cell_at((i, 0)) for i in range(table.row_count)]


class TestDestinationBrowserUnit:
    """Pure logic tests for destination resolution (no app)."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("~", None),
            ("~/a", "~"),
            ("~/a/b", "~/a"),
            ("a", "~"),
            ("a/b", "a"),
            ("/abs", "/"),
            ("/abs/a", "/abs"),
            ("/", "/"),
        ],
    )
    def test_parent_remote(self, path, expected) -> None:
        assert RemoteDestinationBrowser._parent_remote(path) == expected

    @pytest.mark.parametrize(
        ("path", "name", "expected"),
        [
            ("~", "x", "~/x"),
            (".", "x", "~/x"),
            ("~/a", "b", "~/a/b"),
            ("docs", "x", "docs/x"),
            ("/abs", "b", "/abs/b"),
        ],
    )
    def test_join(self, path, name, expected) -> None:
        assert RemoteDestinationBrowser._join(path, name) == expected

    def test_find_listing_existing_dir(self) -> None:
        fake = FakeRemoteFS(make_device())
        path, entries, note = RemoteDestinationBrowser._find_listing(
            fake, "~/incoming"
        )
        assert path == "~/incoming"
        assert entries == [("2025", True), ("2026", True)]
        assert note == ""

    def test_find_listing_existing_file(self) -> None:
        fake = FakeRemoteFS(make_device())
        path, entries, note = RemoteDestinationBrowser._find_listing(
            fake, "~/report.txt"
        )
        assert path == "~"
        assert entries == [("docs", True), ("incoming", True), ("report.txt", False)]
        assert note == "  [file]"

    def test_find_listing_missing_path_walks_to_ancestor(self) -> None:
        fake = FakeRemoteFS(make_device())
        path, entries, note = RemoteDestinationBrowser._find_listing(
            fake, "~/incoming/new/backup"
        )
        assert path == "~/incoming"
        assert entries == [("2025", True), ("2026", True)]
        assert note == "  [will be created]"

    def test_find_listing_relative_path_preserves_form(self) -> None:
        """A relative destination keeps the user's exact form."""
        fake = FakeRemoteFS(make_device())
        path, entries, note = RemoteDestinationBrowser._find_listing(fake, "docs")
        assert path == "docs"
        assert entries == [("notes.md", False)]
        assert note == ""


class TestDestinationBrowserTUI:
    """Integration tests: layout, browsing, and input sync in the app."""

    async def test_send_tab_renders_destination_browser(self, monkeypatch) -> None:
        """Both browsers render side by side; unconnected shows a notice."""
        app = TailshareApp()
        with patch.object(app, "_refresh_device_list"):
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.pause()

                row = app.query_one("#browser-row")
                columns = [child.id for child in row.children]
                assert columns == ["local-browser-column", "destination-column"]

                browser = app.query_one(
                    "#remote-destination", RemoteDestinationBrowser
                )
                assert browser.destination == "~"
                table = browser.query_one("#destination-file-table", DataTable)
                assert _rows(table) == ["<Not Connected>"]
                label = str(
                    app.query_one("#destination-path-label", Label).render()
                )
                assert "Remote Path: ~" in label

                # The manual path input is still present below the browsers
                assert app.query_one("#remote-path", Input) is not None

                await pilot.press("q")
                await pilot.pause()

    async def test_connect_populates_destination_listing(self, monkeypatch) -> None:
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)

            assert await _wait_until(
                pilot,
                lambda: _rows(
                    browser.query_one("#destination-file-table", DataTable)
                )
                == ["docs", "incoming", "report.txt"],
            )
            label = str(
                app.query_one("#destination-path-label", Label).render()
            )
            assert "Remote Path: ~" in label

            await pilot.press("q")
            await pilot.pause()

    async def test_browsing_into_folder_syncs_input(self, monkeypatch) -> None:
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)

            table.post_message(
                DataTable.RowSelected(table, 1, RowKey("incoming"))
            )
            assert await _wait_until(
                pilot, lambda: app.query_one("#remote-path", Input).value == "~/incoming"
            )

            assert browser.destination == "~/incoming"
            assert app._remote_destination == "~/incoming"
            assert _rows(table) == ["..", "2025", "2026"]
            label = str(app.query_one("#destination-path-label", Label).render())
            assert "Remote Path: ~/incoming" in label

            await pilot.press("q")
            await pilot.pause()

    async def test_typed_path_points_browser(self, monkeypatch) -> None:
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)
            input_ = app.query_one("#remote-path", Input)

            input_.value = "~/incoming/2025"
            input_.post_message(Input.Blurred(input_, input_.value))
            assert await _wait_until(
                pilot,
                lambda: _rows(table) == ["..", "old.pdf"]
                and browser.destination == "~/incoming/2025",
            )
            assert app._remote_destination == "~/incoming/2025"
            label = str(app.query_one("#destination-path-label", Label).render())
            assert "Remote Path: ~/incoming/2025" in label

            await pilot.press("q")
            await pilot.pause()

    async def test_file_destination_from_input_kept_with_note(self, monkeypatch) -> None:
        """Typing a file path keeps it as the destination (overwrite)."""
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)
            input_ = app.query_one("#remote-path", Input)

            input_.value = "~/report.txt"
            input_.post_message(Input.Blurred(input_, input_.value))
            assert await _wait_until(
                pilot,
                lambda: "[file]" in str(
                    app.query_one("#destination-path-label", Label).render()
                ),
            )

            # The destination stays the exact file; the listing is its parent
            assert browser.destination == "~/report.txt"
            assert app._remote_destination == "~/report.txt"
            assert _rows(table) == ["docs", "incoming", "report.txt"]
            assert input_.value == "~/report.txt"
            label = str(app.query_one("#destination-path-label", Label).render())
            assert "Remote Path: ~/report.txt  [file]" in label

            await pilot.press("q")
            await pilot.pause()

    async def test_file_click_sets_file_destination(self, monkeypatch) -> None:
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)

            table.post_message(
                DataTable.RowSelected(table, 2, RowKey("report.txt"))
            )
            assert await _wait_until(
                pilot,
                lambda: app.query_one("#remote-path", Input).value == "~/report.txt",
            )
            assert browser.destination == "~/report.txt"
            assert "[file]" in str(
                app.query_one("#destination-path-label", Label).render()
            )

            await pilot.press("q")
            await pilot.pause()

    async def test_will_be_created_note_and_ancestor_listing(self, monkeypatch) -> None:
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)
            input_ = app.query_one("#remote-path", Input)

            input_.value = "~/incoming/new/backup"
            input_.post_message(Input.Blurred(input_, input_.value))
            assert await _wait_until(
                pilot,
                lambda: "[will be created]"
                in str(app.query_one("#destination-path-label", Label).render()),
            )

            assert browser.destination == "~/incoming/new/backup"
            assert browser._path == "~/incoming"
            assert _rows(table) == ["..", "2025", "2026"]
            label = str(app.query_one("#destination-path-label", Label).render())
            assert "Remote Path: ~/incoming/new/backup  [will be created]" in label

            await pilot.press("q")
            await pilot.pause()

    async def test_dotdot_navigates_to_parent(self, monkeypatch) -> None:
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)

            table.post_message(
                DataTable.RowSelected(table, 1, RowKey("incoming"))
            )
            assert await _wait_until(
                pilot, lambda: browser.destination == "~/incoming"
            )
            # '..' is row 0 once inside a subdirectory
            table.post_message(DataTable.RowSelected(table, 0, RowKey("..")))
            assert await _wait_until(
                pilot,
                lambda: browser.destination == "~"
                and app.query_one("#remote-path", Input).value == "~",
            )
            assert _rows(table) == ["docs", "incoming", "report.txt"]

            await pilot.press("q")
            await pilot.pause()

    async def test_keyboard_navigation_updates_destination(self, monkeypatch) -> None:
        """j/k move the cursor; Enter sets the destination."""
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)

            table.focus()
            await pilot.pause()
            table.move_cursor(row=0, column=0)
            await pilot.press("j")  # to 'incoming'
            await pilot.pause()
            assert table.cursor_row == 1
            await pilot.press("enter")
            assert await _wait_until(
                pilot,
                lambda: browser.destination == "~/incoming"
                and app.query_one("#remote-path", Input).value == "~/incoming",
            )

            await pilot.press("q")
            await pilot.pause()

    async def test_escape_resets_destination(self, monkeypatch) -> None:
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)

            table.post_message(
                DataTable.RowSelected(table, 1, RowKey("incoming"))
            )
            assert await _wait_until(
                pilot, lambda: browser.destination == "~/incoming"
            )

            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#remote-path", Input).value == ""
            assert await _wait_until(pilot, lambda: browser.destination == "~")
            assert app._remote_destination == "~"

            await pilot.press("q")
            await pilot.pause()

    async def test_refresh_key_refreshes_destination(self, monkeypatch) -> None:
        app = TailshareApp()
        async with app.run_test(size=(120, 35)) as pilot:
            await _select_and_connect(app, monkeypatch, pilot)
            browser = app.query_one("#remote-destination", RemoteDestinationBrowser)
            table = browser.query_one("#destination-file-table", DataTable)

            assert await _wait_until(
                pilot,
                lambda: _rows(table) == ["docs", "incoming", "report.txt"],
            )

            await pilot.press("R")
            await pilot.pause()
            assert _rows(table) == ["docs", "incoming", "report.txt"]
            assert any(
                n.message == "Destination refreshed" for n in app._notifications
            )

            await pilot.press("q")
            await pilot.pause()

    async def test_send_uses_uncommitted_input_value(self, monkeypatch, tmp_path) -> None:
        """Send commits the input first, even if the blur was not processed."""
        source = tmp_path / "a.txt"
        source.write_text("hello")

        app = TailshareApp()
        with patch.object(app, "_start_transfer_worker"):
            async with app.run_test(size=(120, 35)) as pilot:
                await _select_and_connect(app, monkeypatch, pilot)
                app._selected_path = str(source)
                app.query_one("#remote-path", Input).value = "~/docs"

                app._send_files()
                await pilot.pause()

                tasks = app._transfer_manager.get_all_tasks()
                assert len(tasks) == 1
                assert tasks[0].target_path == "~/docs"
                # The browser and state followed the input
                browser = app.query_one(
                    "#remote-destination", RemoteDestinationBrowser
                )
                assert await _wait_until(
                    pilot, lambda: browser.destination == "~/docs"
                )
                assert app._remote_destination == "~/docs"

                await pilot.press("q")
                await pilot.pause()
