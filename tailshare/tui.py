"""Textual TUI interface for tailshare.

This module implements the terminal user interface with:
- Device list display
- File browser
- Transfer queue and progress
- Status messages
"""

import os
import threading
from functools import partial
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.events import Resize
from textual.message import Message
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from tailshare import __version__
from tailshare.config import setup_logging
from tailshare.devices import Device, DeviceDiscovery, TailscaleNotRunningError
from tailshare.transfer import (
    SFTPClient,
    TransferDirection,
    TransferError,
    TransferManager,
    TransferTask,
)


class FileSelected(Message):
    """Message sent when a file is selected."""
    def __init__(self, dir_path: str, file_path: str) -> None:
        super().__init__()
        self.dir_path = dir_path
        self.file_path = file_path


class RemoteFileSelected(Message):
    """Message sent when a remote file is selected."""
    def __init__(self, dir_path: str, file_path: str) -> None:
        super().__init__()
        self.dir_path = dir_path
        self.file_path = file_path


class FileBrowser(Vertical):
    """File browser widget for selecting files/folders to transfer."""

    can_focus = True

    BINDINGS = [
        Binding("j", "navigate_down", "Down", show=False),
        Binding("k", "navigate_up", "Up", show=False),
        Binding("Enter", "select", "Select", show=True),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(
        self,
        *args: Any,
        path: str = "/",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._path = path
        self._entries: list[tuple[str, bool]] = []  # (name, is_directory)
        self._name_column_key: Any = None

    @property
    def path(self) -> str:
        """Current directory path."""
        return self._path

    def compose(self) -> ComposeResult:
        """Compose the file browser layout."""
        yield Label(id="path-label")
        yield DataTable(id="file-table")

    def on_mount(self) -> None:
        """Refresh file list when mounted."""
        self._refresh_entries()
        self.call_after_refresh(self._set_column_width)

    def on_resize(self, event: Resize) -> None:
        """Handle resize to update column width."""
        self._set_column_width()

    def _set_column_width(self) -> None:
        """Set the Name column width to fill the table."""
        table = self.query_one("#file-table", DataTable)
        if table.columns and self._name_column_key:
            column = table.columns.get(self._name_column_key)
            if column:
                available = table.container_size.width - 2 * table.cell_padding
                column.width = max(available, 10)
                column.auto_width = False
                table.refresh()

    def _refresh_entries(self) -> None:
        """Refresh the file/directory listing."""
        self._entries = []

        try:
            path = Path(self._path)

            if str(path) != "/":
                # Add parent directory entry
                self._entries.append(("..", True))

            # Get directory contents
            entries = sorted(
                path.iterdir(),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )

            for entry in entries:
                self._entries.append((entry.name, entry.is_dir()))

        except PermissionError:
            self._entries = [("<Permission Denied>", True)]
        except Exception:
            self._entries = [("<Invalid Path>", True)]

        self._update_table()

    def _update_table(self) -> None:
        """Update the DataTable with current entries."""
        table = self.query_one("#file-table", DataTable)
        table.clear()

        if not table.columns:
            self._name_column_key = table.add_column("Name")

        for name, _ in self._entries:
            table.add_row(name, key=name)

        self.query_one("#path-label", Label).update(f"[b]Path:[/b] {self._path}")

    def action_navigate_down(self) -> None:
        """Navigate selection down."""
        table = self.query_one("#file-table", DataTable)
        table.action_cursor_down()

    def action_navigate_up(self) -> None:
        """Navigate selection up."""
        table = self.query_one("#file-table", DataTable)
        table.action_cursor_up()

    def action_select(self) -> None:
        """Select current entry via keyboard."""
        table = self.query_one("#file-table", DataTable)
        if table.cursor_row is not None:
            row_index = table.cursor_row
            if 0 <= row_index < len(self._entries):
                name, _ = self._entries[row_index]
                self._handle_selection(name)

    def action_refresh(self) -> None:
        """Refresh file list."""
        self._refresh_entries()
        self.app.notify("Directory refreshed")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection in the file table."""
        self._handle_selection(str(event.row_key.value))
        event.stop()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle cell selection in the file table."""
        self._handle_selection(str(event.cell_key.row_key.value))
        event.stop()

    def _handle_selection(self, name: str) -> None:
        """Process selection of a file or directory by name."""
        try:
            # Find entry index by name
            index = next(i for i, (n, _) in enumerate(self._entries) if n == name)
            _, is_dir = self._entries[index]

            if name == "..":
                # Go to parent directory
                self._path = str(Path(self._path).parent)
                self._refresh_entries()
            elif is_dir:
                # Enter directory
                self._path = str(Path(self._path) / name)
                self._refresh_entries()
            else:
                # Select file - notify parent
                self.post_message(
                    FileSelected(self._path, str(Path(self._path) / name))
                )
        except StopIteration:
            pass

    def on_click(self, event: events.Click) -> None:
        """Handle click events to select entries."""
        self.focus()


def remote_parent(path: str) -> str | None:
    """Parent of a remote path; None when already at the root.

    Shared by the destination and fetch browsers. The filesystem root
    is '/'; '~' (home) is a regular directory whose parent is '/'.
    """
    if path == "/":
        return None
    if path in ("", "~", "."):
        return "/"
    if path.startswith("~"):
        parent = str(Path(path[1:]).parent)
        if parent in ("", ".", "/"):
            return "~"
        return f"~{parent}"
    if path.startswith("/"):
        return str(Path(path).parent)
    parent = str(Path(path).parent)
    if parent == ".":
        return "~"
    return parent


def _error_message(error: Exception) -> str:
    """Human-readable text for a background-worker failure."""
    text = str(error).strip().rstrip(":").strip()
    return text or "Connection lost"


def _notify_no_parent_above_home(widget: Any, canonical: str | None) -> None:
    """Explain why '..' cannot leave home on this remote.

    canonical == '/' means the session's home *is* the root of its
    filesystem namespace (chroot-jailed account or HOME=/); None means
    the server refused to resolve it.
    """
    if canonical == "/":
        message = (
            "No folder above home: on this remote, home is the root of "
            "its filesystem (chroot-jailed account or HOME=/). There is "
            "nothing to browse above it."
        )
    else:
        message = (
            "Could not determine the folder above home on this remote "
            "(the server refused to resolve it)."
        )
    widget.app.notify(message, title="No Parent Folder")


class RemoteFileBrowser(Vertical):
    """Remote file browser widget for selecting files/folders to fetch."""

    can_focus = True

    BINDINGS = [
        Binding("j", "navigate_down", "Down", show=False),
        Binding("k", "navigate_up", "Up", show=False),
        Binding("Enter", "select", "Select", show=True),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(
        self,
        *args: Any,
        sftp_client: Any = None,
        path: str = "~",
        app: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._sftp_client = sftp_client
        self._path = path
        self._entries: list[tuple[str, bool]] = []  # (name, is_directory)
        self._name_column_key: Any = None
        self._app = app
        self._home_canonical: str | None = None
        self._refresh_generation = 0

    @property
    def path(self) -> str:
        """Current remote directory path."""
        return self._path

    def compose(self) -> ComposeResult:
        """Compose the remote file browser layout."""
        yield Label(id="remote-path-label")
        yield DataTable(id="remote-file-table")

    def on_mount(self) -> None:
        """Refresh file list when mounted."""
        self._refresh_entries()
        self.call_after_refresh(self._set_column_width)

    def on_resize(self, event: Resize) -> None:
        """Handle resize to update column width."""
        self._set_column_width()

    def _set_column_width(self) -> None:
        """Set the Name column width to fill the table."""
        table = self.query_one("#remote-file-table", DataTable)
        if table.columns and self._name_column_key:
            column = table.columns.get(self._name_column_key)
            if column:
                available = table.container_size.width - 2 * table.cell_padding
                column.width = max(available, 10)
                column.auto_width = False
                table.refresh()

    def _refresh_entries(self) -> None:
        """Refresh the remote listing without blocking the UI thread."""
        # Get SFTP client - either from direct reference or app
        sftp_client = self._sftp_client
        if sftp_client is None and self._app is not None:
            sftp_client = getattr(self._app, "_remote_sftp_client", None)

        if sftp_client is None:
            self._home_canonical = None
            self._entries = [("<Not Connected>", False)]
            self._update_table()
            return

        path = self._path

        # Guard against stale results when refreshes overlap
        self._refresh_generation += 1
        generation = self._refresh_generation

        def on_listed(
            entries: list[tuple[str, bool]], home_canonical: str | None
        ) -> None:
            if generation == self._refresh_generation:
                self._show_entries(path, entries, home_canonical)

        def on_error(message: str) -> None:
            if generation == self._refresh_generation:
                self._home_canonical = None
                self._show_entries(path, [(message, False)])

        self.run_worker(
            partial(self._list_remote_dir, sftp_client, path, on_listed, on_error),
            thread=True,
        )

    def set_connect_state(
        self, status: str, detail: str, device_name: str | None = None
    ) -> None:
        """Mirror the app's connection state in the listing.

        'connecting' and 'failed' show a static message; 'idle' falls
        back to the normal refresh (a listing when a client exists,
        '<Not Connected>' otherwise). A live listing for the same
        device is kept while a reconnect (e.g. the Refresh button) is
        in flight.
        """
        if status == "idle":
            self._refresh_entries()
            return
        if status == "connecting":
            current = getattr(self._sftp_client, "device", None)
            if current is not None and (
                device_name is None or device_name == current.name
            ):
                return
        self._home_canonical = None
        self._entries = [(detail, False)]
        self._update_table()

    def _list_remote_dir(
        self,
        sftp_client: Any,
        path: str,
        on_listed: Any,
        on_error: Any,
    ) -> None:
        """List a remote directory in a background thread."""
        current = (
            self._app._remote_sftp_client if self._app is not None else sftp_client
        )
        if sftp_client is not current:
            # The device was switched while this refresh was in flight.
            return
        try:
            entries = sftp_client.list_remote_dir(path)
            home_canonical: str | None = None
            if path in ("~", ".", ""):
                home_canonical = sftp_client.canonicalize(".")
        except Exception as e:
            self.call_later(on_error, _error_message(e))
            return
        self.call_later(on_listed, entries, home_canonical)

    def _show_entries(
        self,
        path: str,
        entries: list[tuple[str, bool]],
        home_canonical: str | None = None,
    ) -> None:
        """Apply a directory listing to the table (main thread)."""
        self._entries = []

        if path in ("~", ".", ""):
            # '..' is always offered at home; when home has no parent
            # above it, selecting it explains why.
            self._home_canonical = home_canonical
            self._entries.append(("..", True))
        else:
            self._home_canonical = None
            if path != "/":
                # Add parent directory entry; '/' is the true root
                self._entries.append(("..", True))

        self._entries.extend(entries)
        self._update_table()

    def _update_table(self) -> None:
        """Update the DataTable with current entries."""
        table = self.query_one("#remote-file-table", DataTable)
        table.clear()

        if not table.columns:
            self._name_column_key = table.add_column("Name")

        for name, _ in self._entries:
            table.add_row(name, key=name)

        display_path = self._path if self._path != "." else "~"
        self.query_one("#remote-path-label", Label).update(f"[b]Remote Path:[/b] {display_path}")

    def action_navigate_down(self) -> None:
        """Navigate selection down."""
        table = self.query_one("#remote-file-table", DataTable)
        table.action_cursor_down()

    def action_navigate_up(self) -> None:
        """Navigate selection up."""
        table = self.query_one("#remote-file-table", DataTable)
        table.action_cursor_up()

    def action_select(self) -> None:
        """Select current entry via keyboard."""
        table = self.query_one("#remote-file-table", DataTable)
        if table.cursor_row is not None:
            row_index = table.cursor_row
            if 0 <= row_index < len(self._entries):
                name, _ = self._entries[row_index]
                self._handle_selection(name)

    def action_refresh(self) -> None:
        """Refresh file list."""
        self._refresh_entries()
        self.app.notify("Directory refreshed")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection in the file table."""
        self._handle_selection(str(event.row_key.value))
        event.stop()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle cell selection in the file table."""
        self._handle_selection(str(event.cell_key.row_key.value))
        event.stop()

    def _handle_selection(self, name: str) -> None:
        """Process selection of a file or directory by name."""
        try:
            index = next(i for i, (n, _) in enumerate(self._entries) if n == name)
            _, is_dir = self._entries[index]

            if name == "..":
                # Go to parent directory
                current_path = self._path
                if current_path in ("~", ".", ""):
                    canonical = self._home_canonical
                    parent = remote_parent(canonical) if canonical else None
                    if parent is None:
                        _notify_no_parent_above_home(self, canonical)
                        return
                else:
                    parent = str(Path(current_path).parent)
                    if not parent:
                        return
                self._path = parent
                self._refresh_entries()
            elif is_dir:
                # Enter directory
                new_path = str(Path(self._path) / name) if self._path != "~" else name
                self._path = new_path
                self._refresh_entries()
            else:
                # Select file - notify parent
                self.post_message(
                    RemoteFileSelected(self._path, str(Path(self._path) / name))
                )

        except StopIteration:
            pass

    def on_click(self, event: events.Click) -> None:
        """Handle click events to select entries."""
        self.focus()


class RemoteDestinationBrowser(Vertical):
    """Remote destination picker for the Send tab.

    Tracks a *destination* path - the place files will be sent to -
    which may be an existing directory, an existing file (overwrite),
    or a path that does not exist yet (created at send time). The
    listing always shows the deepest existing directory at or above
    the destination, and the path label shows the exact destination
    plus a note when the listing is of an ancestor.

    Browsing starts at '~' (home) but is not limited to it: '..' from
    home goes to the parent of home (e.g. /home) and keeps climbing to
    '/'. When home has no parent above it (chroot-jailed account or
    HOME=/), the home listing is marked '[home is root]' and selecting
    '..' explains that there is nothing above. A destination that is
    access-denied, or whose ancestors cannot be listed at all, is
    flagged with a '[not accessible]' note instead of silently showing
    another directory.

    Navigating the listing updates the destination and reports it via
    the ``on_destination`` callback so the remote path input stays in
    sync; the input points the browser back via ``set_destination``.
    Both views derive from the same destination string, so they can
    never disagree about which destination is real.
    """

    can_focus = True

    BINDINGS = [
        Binding("j", "navigate_down", "Down", show=False),
        Binding("k", "navigate_up", "Up", show=False),
        Binding("Enter", "select", "Select", show=True),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(
        self,
        *args: Any,
        sftp_client: Any = None,
        path: str = "~",
        app: Any = None,
        on_destination: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._sftp_client = sftp_client
        self._destination = path
        self._path = path  # directory currently listed
        self._note = ""
        self._entries: list[tuple[str, bool]] = []  # (name, is_directory)
        self._name_column_key: Any = None
        self._app = app
        self._on_destination = on_destination
        self._home_canonical: str | None = None
        self._refresh_generation = 0

    @property
    def destination(self) -> str:
        """Current destination path."""
        return self._destination

    def compose(self) -> ComposeResult:
        """Compose the destination browser layout."""
        yield Label(id="destination-path-label")
        yield DataTable(id="destination-file-table")

    def on_mount(self) -> None:
        """Refresh the listing when mounted."""
        self._refresh()
        self.call_after_refresh(self._set_column_width)

    def on_resize(self, event: Resize) -> None:
        """Handle resize to update column width."""
        self._set_column_width()

    def _set_column_width(self) -> None:
        """Set the Name column width to fill the table."""
        table = self.query_one("#destination-file-table", DataTable)
        if table.columns and self._name_column_key:
            column = table.columns.get(self._name_column_key)
            if column:
                available = table.container_size.width - 2 * table.cell_padding
                column.width = max(available, 10)
                column.auto_width = False
                table.refresh()

    @staticmethod
    def _normalize_destination(path: str) -> str:
        """Canonical form of a destination path.

        Whitespace is stripped, an empty value becomes '~', and a
        trailing slash is dropped (except for the root '/') so that
        '/mnt/hdd/' and '/mnt/hdd' name the same destination.
        """
        path = path.strip() or "~"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        return path

    def set_destination(self, path: str) -> None:
        """Point the browser at a destination (from the path input)."""
        path = self._normalize_destination(path)
        if path == self._destination:
            return
        self._destination = path
        self._refresh()

    def _refresh(self) -> None:
        """Resolve and list the destination without blocking the UI."""
        sftp_client = self._sftp_client
        if sftp_client is None and self._app is not None:
            sftp_client = getattr(self._app, "_remote_sftp_client", None)

        if sftp_client is None:
            self._path = self._destination
            self._note = ""
            self._home_canonical = None
            self._entries = [("<Not Connected>", False)]
            self._update_table()
            return

        destination = self._destination

        # Guard against stale results when refreshes overlap
        self._refresh_generation += 1
        generation = self._refresh_generation

        def on_resolved(
            path: str, entries: list, note: str, home_canonical: str | None
        ) -> None:
            if generation == self._refresh_generation:
                self._path = path
                self._entries = []
                if path in ("~", ".", ""):
                    # '..' is always offered at home; when home has no
                    # parent above it, selecting it explains why.
                    self._home_canonical = home_canonical
                    self._entries.append(("..", True))
                else:
                    self._home_canonical = None
                    if path != "/":
                        # Add parent directory entry; '/' is the root
                        self._entries.append(("..", True))
                self._entries.extend(entries)
                self._note = note
                self._update_table()

        def on_error(message: str) -> None:
            if generation == self._refresh_generation:
                self._path = destination
                self._note = ""
                self._home_canonical = None
                self._entries = [(message, False)]
                self._update_table()

        self.run_worker(
            partial(
                self._resolve_listing,
                sftp_client,
                destination,
                on_resolved,
                on_error,
            ),
            thread=True,
        )

    def set_connect_state(
        self, status: str, detail: str, device_name: str | None = None
    ) -> None:
        """Mirror the app's connection state in the listing.

        'connecting' and 'failed' show a static message; 'idle' falls
        back to the normal refresh (a listing when a client exists,
        '<Not Connected>' otherwise). A live listing for the same
        device is kept while a reconnect (e.g. the Refresh button) is
        in flight.
        """
        if status == "idle":
            self._refresh()
            return
        if status == "connecting":
            current = getattr(self._sftp_client, "device", None)
            if current is not None and (
                device_name is None or device_name == current.name
            ):
                return
        self._path = self._destination
        self._note = ""
        self._home_canonical = None
        self._entries = [(detail, False)]
        self._update_table()

    def _resolve_listing(
        self,
        sftp_client: Any,
        destination: str,
        on_resolved: Any,
        on_error: Any,
    ) -> None:
        """Find and list the destination's deepest existing directory."""
        current = (
            self._app._remote_sftp_client if self._app is not None else sftp_client
        )
        if sftp_client is not current:
            # The device was switched while this refresh was in flight.
            return
        try:
            path, entries, note = self._find_listing(sftp_client, destination)
            home_canonical: str | None = None
            if path in ("~", ".", "", "/"):
                home_canonical = sftp_client.canonicalize(".")
            if (
                note == ""
                and path in ("~", ".", "", "/")
                and home_canonical == "/"
            ):
                # Home *is* the root of this remote's filesystem
                # namespace; say so instead of looking empty.
                note = "  [home is root]"
        except Exception as e:
            # A dropped connection (paramiko SSHException), a TransferError,
            # or anything else the remote throws must surface as an entry,
            # never as a worker crash.
            self.call_later(on_error, _error_message(e))
            return
        self.call_later(on_resolved, path, entries, note, home_canonical)

    @staticmethod
    def _find_listing(
        sftp_client: Any, destination: str
    ) -> tuple[str, list[tuple[str, bool]], str]:
        """Return (listing_path, entries, note) for a destination.

        If the destination itself is a directory it is listed directly.
        Otherwise the walk goes up to the nearest existing directory,
        which is listed instead, with a note explaining the difference:
        '[file]' for an existing file destination, '[will be created]'
        for a missing one, '[not accessible]' when the destination is
        access-denied or no ancestor can be listed at all (jailed or
        permission-limited remote).
        """
        dest = RemoteDestinationBrowser._normalize_destination(destination)
        dest_state = sftp_client.probe_remote(dest)
        if dest_state == "dir":
            return dest, sftp_client.list_remote_dir(dest), ""

        listing = dest
        state = dest_state
        while state != "dir":
            parent = remote_parent(listing)
            if parent is None or parent == listing:
                break
            listing = parent
            state = sftp_client.probe_remote(listing)

        if state != "dir":
            return listing, [], "  [not accessible]"

        if dest_state == "file":
            note = "  [file]"
        elif dest_state == "denied":
            note = "  [not accessible]"
        else:
            note = "  [will be created]"
        return listing, sftp_client.list_remote_dir(listing), note

    # Shared path algebra (also used by RemoteFileBrowser); kept here
    # as an alias for backward compatibility with tests.
    _parent_remote = staticmethod(remote_parent)

    @staticmethod
    def _join(path: str, name: str) -> str:
        """Join a remote path and an entry name, preserving '~' form.

        The home root ('~', '.' or '') yields a home-relative '~/name'.
        """
        if path in ("", "~", "."):
            return f"~/{name}"
        return str(Path(path) / name)

    def _navigate(self, new_destination: str) -> None:
        """Set the destination from a navigation action and report it."""
        if new_destination == self._destination:
            return
        self._destination = new_destination
        if self._on_destination is not None:
            self._on_destination(new_destination)
        self._refresh()

    def _update_table(self) -> None:
        """Update the DataTable with current entries."""
        table = self.query_one("#destination-file-table", DataTable)
        table.clear()

        if not table.columns:
            self._name_column_key = table.add_column("Name")

        for name, _ in self._entries:
            table.add_row(name, key=name)

        display_path = self._destination if self._destination != "." else "~"
        # A Text object keeps the path and note literal; a markup string
        # would interpret '[' as a style tag.
        label = Text()
        label.append("Remote Path: ", style="bold")
        label.append(display_path + self._note)
        self.query_one("#destination-path-label", Label).update(label)

    def action_navigate_down(self) -> None:
        """Navigate selection down."""
        table = self.query_one("#destination-file-table", DataTable)
        table.action_cursor_down()

    def action_navigate_up(self) -> None:
        """Navigate selection up."""
        table = self.query_one("#destination-file-table", DataTable)
        table.action_cursor_up()

    def action_select(self) -> None:
        """Select current entry via keyboard."""
        table = self.query_one("#destination-file-table", DataTable)
        if table.cursor_row is not None:
            row_index = table.cursor_row
            if 0 <= row_index < len(self._entries):
                name, _ = self._entries[row_index]
                self._handle_selection(name)

    def action_refresh(self) -> None:
        """Refresh the destination listing."""
        self._refresh()
        self.app.notify("Destination refreshed")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection in the destination table."""
        self._handle_selection(str(event.row_key.value))
        event.stop()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle cell selection in the destination table."""
        self._handle_selection(str(event.cell_key.row_key.value))
        event.stop()

    def _handle_selection(self, name: str) -> None:
        """Process selection of an entry: it becomes the new destination.

        Directories and files alike - a file destination overwrites that
        exact remote file at send time. ".." goes to the parent of the
        directory currently listed.
        """
        if not any(entry[0] == name for entry in self._entries):
            return

        if name == "..":
            if self._path in ("~", ".", ""):
                canonical = self._home_canonical
                parent = remote_parent(canonical) if canonical else None
                if parent is None:
                    _notify_no_parent_above_home(self, canonical)
                    return
            else:
                parent = remote_parent(self._path)
                if parent is None:
                    return
            self._navigate(parent)
        else:
            self._navigate(self._join(self._path, name))

    def on_click(self, event: events.Click) -> None:
        """Handle click events to select entries."""
        self.focus()


class TransferQueueTable(DataTable):
    """Transfer queue with per-row remove/cancel support.

    Each row shows one queued task. The trailing column renders an "✕"
    for tasks that can still be stopped. Clicking it (or focusing the
    row and pressing x / delete) removes the task from the queue; a
    task that is already transferring is cancelled instead, stopping
    at the next chunk boundary.
    """

    CANCEL_COLUMN = "cancel"

    BINDINGS = [
        Binding("x", "remove_job", "Remove job"),
        Binding("delete", "remove_job", "Remove job", show=False),
    ]

    STATUS_ICONS = {
        "pending": "⏳",
        "transferring": "▶",
        "completed": "✓",
        "failed": "✗",
        "cancelled": "⊘",
    }

    def __init__(
        self,
        manager: TransferManager,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._manager = manager
        self._row_keys: list[str] = []
        self._row_tasks: dict[str, TransferTask] = {}

    def on_mount(self) -> None:
        """Add the queue columns."""
        self._add_columns()
        self.call_after_refresh(self._set_name_width)

    def on_resize(self, event: Resize) -> None:
        """Keep the Name column sized to the table width."""
        self._set_name_width()

    def _add_columns(self) -> None:
        """Add columns once (idempotent).

        Fixed columns are sized to fit their header and the widest
        content they can hold (status icons, progress text or the
        18-char truncated error text, and the cancel glyph).
        """
        if self.columns:
            return
        self.add_column("Status", key="status", width=6)
        self.add_column("Name", key="name")
        self.add_column("Progress", key="progress", width=18)
        self.add_column("✕", key=self.CANCEL_COLUMN, width=2)

    def _set_name_width(self) -> None:
        """Size the Name column to the remaining table width."""
        self._add_columns()
        column = self.columns.get("name")
        if column is None:
            return
        # Fixed contribution of the other columns, padding and row label
        fixed = 6 + 18 + 2 + 2 * self.cell_padding * 4 + 4
        column.width = max(self.container_size.width - fixed, 10)
        column.auto_width = False

    @staticmethod
    def _progress_text(task: TransferTask) -> str:
        """Short progress/error text for the Progress column."""
        if task.status == "transferring":
            return f"{task.progress.percentage:.0f}%"
        if task.status == "failed":
            return (task.error or "error")[:18]
        return {
            "pending": "",
            "completed": "done",
            "cancelled": "cancelled",
        }.get(task.status, "")

    def _row_values(self, task: TransferTask) -> tuple[str, str, str, str]:
        """Cell values for one task row."""
        cancellable = task.status in ("pending", "transferring")
        return (
            self.STATUS_ICONS.get(task.status, "?"),
            task.progress.filename,
            self._progress_text(task),
            "✕" if cancellable else "",
        )

    def update_tasks(self, tasks: list[TransferTask]) -> None:
        """Update the table from the manager's task list.

        Rows are rebuilt only when the set of tasks changed; otherwise
        just the status/progress cells are refreshed so the cursor
        position survives the periodic updates.
        """
        keys = [task.task_id for task in tasks]
        if keys != self._row_keys:
            self._add_columns()
            self.clear()
            self._row_tasks = {}
            self._row_keys = []
            for task in tasks:
                self.add_row(*self._row_values(task), key=task.task_id)
                self._row_tasks[task.task_id] = task
                self._row_keys.append(task.task_id)
            return

        for task in tasks:
            icon, _name, progress_text, cancel = self._row_values(task)
            self.update_cell(task.task_id, "status", icon)
            self.update_cell(task.task_id, "progress", progress_text)
            self.update_cell(task.task_id, self.CANCEL_COLUMN, cancel)

    def action_remove_job(self) -> None:
        """Remove the task under the cursor from the queue."""
        if not 0 <= self.cursor_row < len(self._row_keys):
            return
        self._remove_task(self._row_keys[self.cursor_row])

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle clicks on the cancel column."""
        if event.cell_key.column_key == self.CANCEL_COLUMN:
            self._remove_task(str(event.cell_key.row_key.value))
        event.stop()

    def _remove_task(self, task_id: str) -> None:
        """Cancel/remove one task and refresh the display."""
        task = self._row_tasks.get(task_id)
        if task is None:
            return

        was_transferring = task.status == "transferring"
        self._manager.cancel_task(task)
        if was_transferring:
            self.app.notify(
                f"Cancelling {task.progress.filename}", title="Transfer"
            )
        else:
            self.app.notify(
                f"Removed {task.progress.filename} from queue", title="Transfer"
            )
        self.app._update_queue_display()


class TailshareApp(App[None]):
    """Main application for tailshare file sharing.

    Provides a TUI for discovering Tailscale devices and
    transferring files via SFTP.
    """

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-container {
        width: 100%;
        height: 1fr;
        layout: grid;
        grid-size: 2 1;
    }

    #left-panel {
        width: 1fr;
        height: 1fr;
        border: solid $primary;
        margin: 1;
    }

    #device-auth {
        margin: 0;
        height: auto;
    }

    #device-controls {
        margin: 0;
        height: auto;
        align-horizontal: center;
    }

    #transfer-tabs {
        width: 1fr;
        height: 1fr;
        border: solid $primary;
        margin: 1;
    }

    #device-list {
        height: 1fr;
    }

    #device-list > .data-table__row--selected {
        background: $accent;
        color: $text;
        text-style: bold;
    }

    #browser-row {
        height: 2fr;
        width: 100%;
    }

    #local-browser-column,
    #destination-column {
        width: 1fr;
        height: 1fr;
    }

    #file-browser-container {
        height: 1fr;
    }

    #file-table {
        height: 1fr;
    }

    #remote-destination-container {
        height: 1fr;
    }

    #destination-file-table {
        height: 1fr;
    }

    #path-spacer {
        height: 1;
    }

    #transfer-queue-container {
        height: 4fr;
    }

    #transfer-queue {
        height: 1fr;
    }

    #remote-file-browser-container {
        height: 2fr;
    }

    #remote-file-table {
        height: 1fr;
    }

    #path-spacer-fetch {
        height: 1;
    }

    #transfer-queue-container-fetch {
        height: 4fr;
    }

    #transfer-queue-fetch {
        height: 1fr;
    }

    #transfer-queue > .datatable--hover,
    #transfer-queue > .datatable--header-hover,
    #transfer-queue-fetch > .datatable--hover,
    #transfer-queue-fetch > .datatable--header-hover {
        background: transparent;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "refresh_devices", "Refresh Devices"),
        Binding("s", "send", "Send"),
        Binding("f", "fetch", "Fetch"),
        Binding("c", "clear_queue", "Clear Queue"),
        Binding("r", "refresh_files", "Refresh Files"),
        Binding("R", "refresh_remote", "Refresh Remote"),
    ]

    TITLE = "Tailshare"
    VERSION = __version__
    SUB_TITLE = f"File Sharing over Tailscale ({VERSION})"

    def __init__(self) -> None:
        super().__init__()
        self._device_discovery = DeviceDiscovery()
        self._transfer_manager = TransferManager()
        self._transfer_manager.set_progress_callback(self._on_transfer_progress)
        self._selected_device: Device | None = None
        self._selected_device_name: str | None = None
        self._selected_path: str = ""
        self._selected_remote_path: str = ""
        self._remote_destination: str = "~"
        self._worker_running = False
        self._worker_lock = threading.Lock()
        self._queue_update_enabled: bool = False
        self._interval_token: Any = None
        self._remote_sftp_client: Any = None
        self._connect_in_progress: bool = False
        self._last_connect_error: str | None = None

    def compose(self) -> ComposeResult:
        """Compose the UI layout."""
        yield Header()

        with Horizontal(id="main-container"):
            with Vertical(id="left-panel"):
                yield Label("[b]Tailscale Devices[/b]", id="device-header")
                yield DataTable(id="device-list")
                with Vertical(id="device-auth"):
                    yield Input(
                        placeholder="Username (optional)",
                        id="remote-user",
                    )
                    yield Input(
                        placeholder="Password (optional)",
                        id="remote-password",
                        password=True,
                    )
                with Horizontal(id="device-controls"):
                    yield Button("Refresh", id="btn-refresh-devices", variant="primary")
                    yield Button("Test", id="btn-test-device", variant="default")

            with TabbedContent(id="transfer-tabs"):
                with TabPane("Send", id="send-tab"), Vertical(id="right-panel"):
                    with Horizontal(id="browser-row"):
                        with Vertical(id="local-browser-column"):
                            yield Label("[b]File Browser[/b]", id="file-header")
                            with ScrollableContainer(id="file-browser-container"):
                                yield FileBrowser(id="file-browser", path="/")
                        with Vertical(id="destination-column"):
                            yield Label(
                                "[b]Destination[/b]", id="destination-header"
                            )
                            with ScrollableContainer(
                                id="remote-destination-container"
                            ):
                                yield RemoteDestinationBrowser(
                                    id="remote-destination",
                                    app=self,
                                    on_destination=self._on_destination_changed,
                                )

                    yield Static(id="path-spacer")

                    with Horizontal(id="transfer-controls"):
                        yield Input(
                            placeholder="Remote path (default: ~)",
                            id="remote-path",
                        )
                        yield Button("Send", id="btn-send", variant="primary")

                    with ScrollableContainer(id="transfer-queue-container"):
                        yield Label("", id="send-status-chip")
                        yield TransferQueueTable(
                            self._transfer_manager, id="transfer-queue"
                        )

                with TabPane("Fetch", id="fetch-tab"), Vertical(id="right-panel-fetch"):
                        yield Label("[b]Remote File Browser[/b]", id="remote-file-header")
                        with ScrollableContainer(id="remote-file-browser-container"):
                            yield RemoteFileBrowser(id="remote-file-browser", path="~", app=self)

                        yield Static(id="path-spacer-fetch")

                        with Horizontal(id="fetch-controls"):
                            yield Input(
                                placeholder="Local path (default: ~)",
                                id="local-path",
                            )
                            yield Button("Fetch", id="btn-fetch", variant="primary")

                        with ScrollableContainer(id="transfer-queue-container-fetch"):
                            yield Label("", id="fetch-status-chip")
                            yield TransferQueueTable(
                                self._transfer_manager,
                                id="transfer-queue-fetch",
                            )

        yield Footer()

    def on_mount(self) -> None:
        """Initialize app when mounted."""
        setup_logging()
        device_list = self.query_one("#device-list", DataTable)
        device_list.add_columns("Name", "IP", "Status")
        self._refresh_device_list()

    def on_unmount(self) -> None:
        """Release the persistent SFTP client when the app exits."""
        if self._remote_sftp_client is not None:
            self._remote_sftp_client.disconnect()
            self._remote_sftp_client = None

    def on_key(self, event: events.Key) -> None:
        """Handle key events."""
        if event.key == "escape":
            self.query_one("#remote-path", Input).value = ""
            self.query_one("#local-path", Input).value = ""
            # Reset the destination to the default and point the browser back
            self._remote_destination = "~"
            self.query_one("#remote-destination", RemoteDestinationBrowser).set_destination(
                "~"
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id

        if button_id == "btn-refresh-devices":
            self._refresh_device_list()
            self._connect_for_remote_browser()
        elif button_id == "btn-test-device":
            self._test_selected_device()
        elif button_id == "btn-send":
            self._send_files()
        elif button_id == "btn-fetch":
            self._fetch_files()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle device selection via row selection."""
        if event.data_table.id == "device-list":
            self._select_device_by_key(str(event.row_key.value))

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle device selection via cell click."""
        if event.data_table.id == "device-list":
            self._select_device_by_key(str(event.cell_key.row_key.value))

    def _select_device_by_key(self, key: str) -> None:
        """Select the device identified by its row key (machine id or IP)."""
        device = next(
            (
                d
                for d in self._device_discovery.get_devices()
                if (d.machine_id or d.ip) == key
            ),
            None,
        )
        if device:
            self._select_device(device)
        else:
            self.notify(
                f"Could not find device: {key}",
                title="Error",
                severity="error",
            )

    def _select_device(self, device: Device) -> None:
        """Select a device and populate the remote browser."""
        self._selected_device = device
        self._selected_device_name = device.name
        self._refresh_device_list()

        # Try to connect to populate remote browser
        self._connect_for_remote_browser()

        self.notify(
            f"Selected {device.name}",
            title="Device Selected",
        )

    def on_file_selected(self, event: FileSelected) -> None:
        """Handle file selection from browser (Send tab)."""
        self._selected_path = event.file_path
        if self._selected_device:
            self._send_files()
        else:
            self.notify(
                f"Selected: {event.file_path}. Please select a target device to queue.",
                title="File Selected",
            )

    def on_remote_file_selected(self, event: RemoteFileSelected) -> None:
        """Handle file selection from remote browser (Fetch tab)."""
        self._selected_remote_path = event.file_path
        if self._selected_device:
            self._fetch_files()
        else:
            self.notify(
                f"Selected: {event.file_path}. Please select a target device to queue.",
                title="File Selected",
            )

    def _on_destination_changed(self, path: str) -> None:
        """Destination browser moved; mirror it into the path input."""
        self._remote_destination = path
        self.query_one("#remote-path", Input).value = path

    def _commit_remote_path_input(self) -> None:
        """Commit the remote path input's value to the destination."""
        value = RemoteDestinationBrowser._normalize_destination(
            self.query_one("#remote-path", Input).value
        )
        if value == self._remote_destination:
            return
        self._remote_destination = value
        self.query_one("#remote-path", Input).value = value
        self.query_one("#remote-destination", RemoteDestinationBrowser).set_destination(
            value
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Commit the remote path when Enter is pressed in the input."""
        if event.input.id == "remote-path":
            self._commit_remote_path_input()

    def on_input_blurred(self, event: Input.Blurred) -> None:
        """Commit the remote path when the input loses focus."""
        if event.input.id == "remote-path":
            self._commit_remote_path_input()

    def action_refresh_devices(self) -> None:
        """Refresh device list."""
        self._refresh_device_list()

    def action_refresh_files(self) -> None:
        """Refresh file browser."""
        browser = self.query_one("#file-browser", FileBrowser)
        browser.action_refresh()

    def action_send(self) -> None:
        """Send selected files."""
        self._send_files()

    def action_fetch(self) -> None:
        """Fetch selected files from remote device."""
        self._fetch_files()

    def action_refresh_remote(self) -> None:
        """Refresh both remote browsers (fetch source and send destination)."""
        self.query_one("#remote-file-browser", RemoteFileBrowser).action_refresh()
        self.query_one("#remote-destination", RemoteDestinationBrowser).action_refresh()

    def action_clear_queue(self) -> None:
        """Clear completed transfers."""
        self._transfer_manager.clear_completed()
        self._update_queue_display()

    def action_quit(self) -> None:
        """Quit the application."""
        self.exit()

    def _refresh_device_list(self) -> None:
        """Refresh the list of Tailscale devices."""
        self.run_worker(self._discover_devices, thread=True)

    def _discover_devices(self) -> None:
        """Discover devices in a background thread.

        Performs no DOM access; the table update is scheduled on the
        main thread via call_later.
        """
        rows: list[tuple] = []

        try:
            devices = self._device_discovery.discover()

            rows = []
            for device in devices:
                status = "Online" if device.online else "Offline"
                style_prefix = ""
                style_suffix = ""
                if self._selected_device_name == device.name:
                    style_prefix = "[background=$primary][color=$text][b]"
                    style_suffix = "[/]"
                # Row key must be unique per device: use the machine id
                # (falling back to IP) so name collisions cannot crash
                # the table with a DuplicateKey error.
                rows.append(
                    (
                        f"{style_prefix}{device.name}{style_suffix}",
                        f"{style_prefix}{device.ip}{style_suffix}",
                        f"{style_prefix}{status}{style_suffix}",
                        device.machine_id or device.ip,
                    )
                )

            if not devices:
                self.call_later(
                    lambda: self.notify(
                        "No Tailscale devices found",
                        title="Warning",
                        severity="warning",
                    )
                )

        except TailscaleNotRunningError as e:
            # Capture the message before scheduling: the except variable is
            # deleted when the block exits, but the lambda runs later.
            msg = str(e)
            self.call_later(
                lambda: self.notify(
                    msg,
                    title="Tailscale Not Running",
                    severity="error",
                )
            )
            rows = [("[Tailscale Not Running]", "-", "Error", "error")]
        except Exception as e:
            msg = f"Device discovery failed: {e}"
            self.call_later(
                lambda: self.notify(
                    msg,
                    title="Error",
                    severity="error",
                )
            )
            rows = [("[Discovery Failed]", "-", "Error", "error")]

        # Populate table on the main thread
        self.call_later(self._populate_device_table, rows)

    def _populate_device_table(self, rows: list[tuple]) -> None:
        """Clear and populate the device list (must run on main thread)."""
        device_list = self.query_one("#device-list", DataTable)
        device_list.clear()
        for row in rows:
            device_list.add_row(row[0], row[1], row[2], key=row[3])

    def _test_selected_device(self) -> None:
        """Test connection to the selected device (off the UI thread)."""
        if not self._selected_device:
            self.notify("No device selected", title="Warning", severity="warning")
            return

        device = self._selected_device
        user = self.query_one("#remote-user", Input).value.strip() or None
        password = self.query_one("#remote-password", Input).value.strip() or None

        self.run_worker(
            partial(self._test_connection_in_thread, device, user, password),
            thread=True,
        )

    def _test_connection_in_thread(
        self, device: Device, user: str | None, password: str | None
    ) -> None:
        """Run a blocking connection test in a background thread."""
        success, message = self._transfer_manager.test_device_connection(
            device,
            username=user,
            password=password,
        )
        if success:
            msg = f"Connection to {device.name} successful"
            self.call_later(lambda: self.notify(msg, title="Connection Test"))
        else:
            msg = f"Connection failed: {message}"
            self.call_later(
                lambda: self.notify(msg, title="Connection Test", severity="error")
            )

    def _send_files(self) -> None:
        """Queue files for transfer."""
        if not self._selected_device:
            self.notify(
                "Please select a target device",
                title="Warning",
                severity="warning",
            )
            return

        if not self._selected_path:
            self.notify(
                "Please select files to transfer",
                title="Warning",
                severity="warning",
            )
            return

        if not os.path.exists(self._selected_path):
            self.notify(
                f"Path not found: {self._selected_path}",
                title="Error",
                severity="error",
            )
            return

        # Get credentials and remote path. Commit the input first so the
        # destination browser and state agree even if the blur has not
        # been processed yet (Send pressed right after typing), then use
        # the canonical (normalized) destination.
        self._commit_remote_path_input()

        user = self.query_one("#remote-user", Input).value.strip() or None
        password = self.query_one("#remote-password", Input).value.strip() or None

        remote_path = self._remote_destination

        # Queue the transfer
        try:
            task = self._transfer_manager.queue_transfer(
                self._selected_path,
                remote_path,
                self._selected_device,
                username=user,
                password=password,
            )

            self.notify(
                f"Queued: {task.progress.filename}",
                title="Transfer Queued",
            )

            self._update_queue_display()

            # Start transfer worker if not already running
            self._start_transfer_worker()

        except TransferError as e:
            self.notify(
                str(e),
                title="Transfer Error",
                severity="error",
            )

    def _start_transfer_worker(self) -> None:
        """Start the transfer worker if not already running."""
        with self._worker_lock:
            if self._worker_running:
                return
            self._worker_running = True
            self.run_worker(
                self._execute_transfers,
                thread=True,
            )
            self._queue_update_enabled = True
            self._interval_token = self.set_interval(
                0.5, self._update_queue_display
            )

    def _fetch_files(self) -> None:
        """Queue files for fetching."""
        if not self._selected_device:
            self.notify(
                "Please select a target device",
                title="Warning",
                severity="warning",
            )
            return

        if not self._selected_remote_path:
            self.notify(
                "Please select files to fetch",
                title="Warning",
                severity="warning",
            )
            return

        # Get credentials and local path
        user = self.query_one("#remote-user", Input).value.strip() or None
        password = self.query_one("#remote-password", Input).value.strip() or None

        local_path_input = self.query_one("#local-path", Input)
        local_path = local_path_input.value.strip()
        if not local_path:
            local_path = "~"

        # Queue the fetch
        try:
            task = self._transfer_manager.queue_transfer(
                self._selected_remote_path,
                local_path,
                self._selected_device,
                username=user,
                password=password,
                direction=TransferDirection.FETCH,
            )

            self.notify(
                f"Queued: {task.progress.filename}",
                title="Fetch Queued",
            )

            self._update_queue_display()

            # Start transfer worker if not already running
            self._start_transfer_worker()

        except TransferError as e:
            self.notify(
                str(e),
                title="Fetch Error",
                severity="error",
            )

    def _connect_for_remote_browser(self) -> None:
        """Start connecting to the selected device for the remote browser."""
        if not self._selected_device:
            return

        device = self._selected_device
        user = self.query_one("#remote-user", Input).value.strip() or None
        password = self.query_one("#remote-password", Input).value.strip() or None

        self._connect_in_progress = True
        self._last_connect_error = None
        self._publish_connect_state()

        # The SSH connection is blocking; run it off the UI thread.
        self.run_worker(
            partial(self._connect_in_thread, device, user, password),
            thread=True,
        )

    def _connect_in_thread(
        self, device: Device, user: str | None, password: str | None
    ) -> None:
        """Connect in a background thread and hand the client back.

        Any failure - TransferError or otherwise - is reported to the
        main thread as a visible state; an unhandled worker exception
        would take the whole app down.
        """
        client = SFTPClient(device)
        try:
            client.connect(username=user, password=password)
        except Exception as e:
            client.disconnect()
            reason = str(e).strip() or type(e).__name__
            self.call_later(self._on_connect_failed, device, reason)
            return
        self.call_later(self._adopt_remote_client, client)

    def _on_connect_failed(self, device: Device, reason: str) -> None:
        """Record a failed connection attempt (main thread)."""
        if (
            self._selected_device is not None
            and device.ip != self._selected_device.ip
        ):
            # A newer selection owns the connection state now.
            return
        self._connect_in_progress = False
        self._last_connect_error = f"Cannot connect to {device.name}: {reason}"
        self.notify(
            self._last_connect_error, title="Connection Failed", severity="error"
        )
        self._publish_connect_state()

    def _adopt_remote_client(self, client: SFTPClient) -> None:
        """Install a connected client, releasing the previous one (main thread)."""
        if (
            self._selected_device is None
            or client.device.ip != self._selected_device.ip
        ):
            # The selection changed while the connection was in flight.
            client.disconnect()
            return

        if (
            self._remote_sftp_client is not None
            and self._remote_sftp_client is not client
        ):
            self._remote_sftp_client.disconnect()

        self._remote_sftp_client = client
        self._connect_in_progress = False
        self._last_connect_error = None

        remote_browser = self.query_one("#remote-file-browser", RemoteFileBrowser)
        remote_browser._sftp_client = client

        destination_browser = self.query_one(
            "#remote-destination", RemoteDestinationBrowser
        )
        destination_browser._sftp_client = client

        self._publish_connect_state()

    def _publish_connect_state(self) -> None:
        """Push the connection state to both remote browsers (main thread).

        While a connection is in flight the browsers say so; after a
        failure they keep showing the reason (a toast alone is easy to
        miss); otherwise they refresh normally (a listing when a client
        exists, '<Not Connected>' when none does).
        """
        name = self._selected_device.name if self._selected_device else None
        if self._connect_in_progress:
            status = "connecting"
            detail = f"Connecting to {name or 'device'}..."
        elif self._last_connect_error:
            status = "failed"
            detail = self._last_connect_error
        else:
            status = "idle"
            detail = ""

        self.query_one("#remote-file-browser", RemoteFileBrowser).set_connect_state(
            status, detail, name
        )
        self.query_one(
            "#remote-destination", RemoteDestinationBrowser
        ).set_connect_state(status, detail, name)

    def _execute_transfers(self) -> None:
        """Execute queued transfers in background (runs in worker thread).

        execute_queue() itself loops until the queue is empty, picking
        up tasks queued while it runs.
        """
        try:
            self._transfer_manager.execute_queue()
        finally:
            self.call_later(self._on_worker_finished)

    def _on_worker_finished(self) -> None:
        """Clean up after the transfer worker finishes (runs on main thread)."""
        with self._worker_lock:
            self._worker_running = False
            self._queue_update_enabled = False
        if self._interval_token is not None:
            self._interval_token.stop()
            self._interval_token = None

        # Refresh the display one last time, then restart the worker if
        # a task slipped into the queue while it was winding down.
        self._update_queue_display()
        if self._transfer_manager.get_pending_tasks():
            self._start_transfer_worker()

    def _on_transfer_progress(self, task: TransferTask) -> None:
        """Update queue display when transfer progress changes.

        Args:
            task: Task with updated progress
        """
        self.call_later(self._update_queue_display)

    @staticmethod
    def _format_status_chip(tasks: list[TransferTask]) -> str:
        """Format a per-tab status chip from its task list."""
        if not tasks:
            return "[dim]Idle[/dim]"
        active = sum(1 for t in tasks if t.status in ("pending", "transferring"))
        done = sum(1 for t in tasks if t.status == "completed")
        failed = sum(1 for t in tasks if t.status == "failed")
        parts: list[str] = []
        if active:
            parts.append(f"[blue]⏳ {active} active[/blue]")
        if done:
            parts.append(f"[green]✓ {done} done[/green]")
        if failed:
            parts.append(f"[red]✗ {failed} failed[/red]")
        return "  ".join(parts)

    def _update_queue_display(self) -> None:
        """Update the transfer queue display and per-tab status chips."""
        all_tasks = self._transfer_manager.get_all_tasks()
        send_tasks = [
            t for t in all_tasks if t.direction is TransferDirection.SEND
        ]
        fetch_tasks = [
            t for t in all_tasks if t.direction is TransferDirection.FETCH
        ]

        # Status chips update regardless of worker state
        self.query_one("#send-status-chip", Label).update(
            self._format_status_chip(send_tasks)
        )
        self.query_one("#fetch-status-chip", Label).update(
            self._format_status_chip(fetch_tasks)
        )

        if not self._queue_update_enabled:
            return

        # Each tab shows only its own direction
        self.query_one("#transfer-queue", TransferQueueTable).update_tasks(
            send_tasks
        )
        self.query_one(
            "#transfer-queue-fetch", TransferQueueTable
        ).update_tasks(fetch_tasks)


def run_app() -> None:
    """Run the tailshare application."""
    app = TailshareApp()
    app.run()
