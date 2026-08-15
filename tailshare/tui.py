"""Textual TUI interface for tailshare.

This module implements the terminal user interface with:
- Device list display
- File browser
- Transfer queue and progress
- Status messages
"""

import os
import stat
import threading
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Header,
    Footer,
    Label,
    Button,
    Static,
    DataTable,
    Input,
    TabbedContent,
    TabPane,
)
from textual.binding import Binding
from textual import events
from textual.message import Message
from textual.events import Resize

class FileSelected(Message):
    """Message sent when a file is selected."""
    def __init__(self, dir_path: str, file_path: str) -> None:
        super().__init__()
        self.dir_path = dir_path
        self.file_path = file_path
    def __hash__(self) -> int:
        return hash(self.file_path)

class RemoteFileSelected(Message):
    """Message sent when a file is selected."""
    def __init__(self, dir_path: str, file_path: str) -> None:
        super().__init__()
        self.dir_path = dir_path
        self.file_path = file_path
    def __hash__(self) -> int:
        return hash(self.file_path)

from tailshare.devices import Device, DeviceDiscovery, TailscaleNotRunningError
from tailshare.transfer import (
    TransferManager,
    TransferTask,
    TransferError,
    TransferDirection,
    SFTPClient,
)
from tailshare.config import get_config, setup_logging


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
        
        for name, is_dir in self._entries:
            table.add_row(name, key=name)
            
        self.query_one("#path-label", Label).update(f"[b]Path:[/b] {self._path}")
    
    def navigate_down(self) -> None:
        """Navigate selection down."""
        table = self.query_one("#file-table", DataTable)
        table.action_cursor_down()
    
    def navigate_up(self) -> None:
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
        """Refresh the remote file/directory listing."""
        self._entries = []
        
        try:
            path = self._path
            
            if path != "~" and path != ".":
                # Add parent directory entry
                self._entries.append(("..", True))
            
            # Get SFTP client - either from direct reference or app
            sftp_client = self._sftp_client
            if sftp_client is None and self._app is not None:
                sftp_client = getattr(self._app, "_remote_sftp_client", None)
            
            # Get directory contents via SFTP
            if sftp_client is not None:
                entries = sftp_client.list_remote_dir(path)
                self._entries.extend(entries)
            else:
                self._entries = [("<Not Connected>", True)]
                
        except Exception:
            self._entries = [("<Permission Denied>", True)]
            
        self._update_table()
    
    def _update_table(self) -> None:
        """Update the DataTable with current entries."""
        table = self.query_one("#remote-file-table", DataTable)
        table.clear()
        
        if not table.columns:
            self._name_column_key = table.add_column("Name")
        
        for name, is_dir in self._entries:
            table.add_row(name, key=name)
            
        display_path = self._path if self._path != "." else "~"
        self.query_one("#remote-path-label", Label).update(f"[b]Remote Path:[/b] {display_path}")
    
    def navigate_down(self) -> None:
        """Navigate selection down."""
        table = self.query_one("#remote-file-table", DataTable)
        table.action_cursor_down()
    
    def navigate_up(self) -> None:
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
                if current_path == ".":
                    parent = "~"
                elif current_path == "~":
                    parent = "~"
                else:
                    parent = str(Path(current_path).parent)
                self._path = parent if parent else "~"
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


class TransferQueue(Label):
    """Widget displaying transfer queue and progress."""
    
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__("", *args, **kwargs)
        self._tasks: list[TransferTask] = []
    
    def update_tasks(self, tasks: list[TransferTask]) -> None:
        """Update the displayed tasks.

        Args:
            tasks: List of current transfer tasks
        """
        self._tasks = tasks
        if not self._tasks:
            self.update("[dim]No pending transfers[/dim]")
        else:
            lines = ["[b]Transfer Queue:[/b]", ""]
            
            for i, task in enumerate(self._tasks, 1):
                status_icon = {
                    "pending": "[yellow]⏳[/yellow]",
                    "transferring": "[blue]⏳[/blue]",
                    "completed": "[green]✓[/green]",
                    "failed": "[red]✗[/red]",
                }.get(task.status, "?")
                
                lines.append(
                    f"{status_icon} [{i}] {task.progress.filename}"
                )
                if task.direction == TransferDirection.FETCH:
                    lines.append(
                        f"    ← {task.device.name}:{task.source_path}"
                    )
                else:
                    lines.append(
                        f"    → {task.device.name}:{task.target_path}"
                    )
                
                if task.status == "transferring":
                    pct = task.progress.percentage
                    lines.append(
                        f"    [cyan]{pct:.1f}%[/cyan] "
                        f"({task.progress.transferred / 1024:.1f} KB)"
                    )
                
                if task.error:
                    lines.append(f"    [red]{task.error}[/red]")
                
                lines.append("")
            
            self.update("\n".join(lines))


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
    
    #file-browser-container {
        height: 2fr;
    }

    #file-table {
        height: 1fr;
    }
    
    #path-spacer {
        height: 1;
    }
    
    #transfer-queue-container {
        height: 4fr;
    }
    
    #transfer-queue {
        height: auto;
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
        height: auto;
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
    VERSION = "v1.0"
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
        self._worker_running = False
        self._worker_lock = threading.Lock()
        self._queue_update_enabled: bool = False
        self._interval_token: Any = None
        self._remote_sftp_client: Any = None
    
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
                with TabPane("Send", id="send-tab"):
                    with Vertical(id="right-panel"):
                        yield Label("[b]File Browser[/b]", id="file-header")
                        with ScrollableContainer(id="file-browser-container"):
                            yield FileBrowser(id="file-browser", path="/")
                        
                        yield Static(id="path-spacer")
                        
                        with Horizontal(id="transfer-controls"):
                            yield Input(
                                placeholder="Remote path (default: ~)",
                                id="remote-path",
                            )
                            yield Button("Send", id="btn-send", variant="primary")
                        
                        with ScrollableContainer(id="transfer-queue-container"):
                            yield TransferQueue(id="transfer-queue")
                
                with TabPane("Fetch", id="fetch-tab"):
                    with Vertical(id="right-panel-fetch"):
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
                            yield TransferQueue(id="transfer-queue-fetch")
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Initialize app when mounted."""
        setup_logging()
        device_list = self.query_one("#device-list", DataTable)
        device_list.add_columns("Name", "IP", "Status")
        self._refresh_device_list()
    
    def on_key(self, event: events.Key) -> None:
        """Handle key events."""
        if event.key == "escape":
            self.query_one("#remote-path", Input).value = ""
            self.query_one("#local-path", Input).value = ""
    
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
            device_name = str(event.row_key.value)
            self._select_device_by_name(device_name)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle device selection via cell click."""
        if event.data_table.id == "device-list":
            device_name = str(event.cell_key.row_key.value)
            self._select_device_by_name(device_name)

    def _select_device_by_name(self, name: str) -> None:
        """Helper to select a device and notify the user."""
        device = self._device_discovery.get_device_by_name(name)
        if device:
            self._selected_device = device
            self._selected_device_name = name
            self._refresh_device_list()
            
            # Try to connect to populate remote browser
            self._connect_for_remote_browser()
            
            self.notify(
                f"Selected {device.name}",
                title="Device Selected",
            )
        else:
            self.notify(
                f"Could not find device: {name}",
                title="Error",
                severity="error",
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
        """Refresh remote file browser."""
        browser = self.query_one("#remote-file-browser", RemoteFileBrowser)
        browser.action_refresh()
    
    def action_clear_queue(self) -> None:
        """Clear completed transfers."""
        self._transfer_manager.clear_completed()
        self._update_queue_display()
    
    def action_quit(self) -> None:
        """Quit the application."""
        self.exit()
    
    def _refresh_device_list(self) -> None:
        """Refresh the list of Tailscale devices."""
        self.run_worker(self._discover_and_populate, thread=True)

    def _discover_and_populate(self) -> None:
        """Discover devices in background and populate the list."""
        device_list = self.query_one("#device-list", DataTable)
        device_list.clear()

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
                rows.append(
                    (
                        f"{style_prefix}{device.name}{style_suffix}",
                        f"{style_prefix}{device.ip}{style_suffix}",
                        f"{style_prefix}{status}{style_suffix}",
                        device.name,
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
            self.call_later(
                lambda: self.notify(
                    str(e),
                    title="Tailscale Not Running",
                    severity="error",
                )
            )
            rows = [("[Tailscale Not Running]", "-", "Error", "error")]
        except Exception as e:
            self.call_later(
                lambda: self.notify(
                    f"Device discovery failed: {e}",
                    title="Error",
                    severity="error",
                )
            )
            rows = [("[Discovery Failed]", "-", "Error", "error")]

        # Populate table on the main thread
        self.call_later(lambda: self._populate_device_table(device_list, rows))

    def _populate_device_table(
        self, device_list: DataTable, rows: list[tuple]
    ) -> None:
        """Add rows to the device list table (must run on main thread)."""
        for row in rows:
            device_list.add_row(row[0], row[1], row[2], key=row[3])
    
    def _test_selected_device(self) -> None:
        """Test connection to selected device."""
        if not self._selected_device:
            self.notify("No device selected", title="Warning", severity="warning")
            return
        
        user = self.query_one("#remote-user", Input).value.strip() or None
        password = self.query_one("#remote-password", Input).value.strip() or None

        success, message = self._transfer_manager.test_device_connection(
            self._selected_device,
            username=user,
            password=password,
        )
        
        if success:
            self.notify(
                f"Connection to {self._selected_device.name} successful",
                title="Connection Test",
            )
        else:
            self.notify(
                f"Connection failed: {message}",
                title="Connection Test",
                severity="error",
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
        
        # Get credentials and remote path
        user = self.query_one("#remote-user", Input).value.strip() or None
        password = self.query_one("#remote-password", Input).value.strip() or None
        
        remote_path_input = self.query_one("#remote-path", Input)
        remote_path = remote_path_input.value.strip()
        if not remote_path:
            remote_path = "~"
        
        # Queue the transfer
        try:
            is_folder = os.path.isdir(self._selected_path)
            task = self._transfer_manager.queue_transfer(
                self._selected_path,
                remote_path,
                self._selected_device,
                is_folder,
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
            # Check if it's a directory using the persistent connection
            is_folder = False
            try:
                if (
                    self._remote_sftp_client
                    and self._remote_sftp_client._sftp_client
                ):
                    remote_path = self._selected_remote_path
                    if remote_path == "~":
                        remote_path = "."
                    elif remote_path.startswith("~"):
                        remote_path = remote_path.replace("~", ".", 1)

                    stat_result = self._remote_sftp_client._sftp_client.stat(
                        remote_path
                    )
                    is_folder = stat.S_ISDIR(stat_result.st_mode)
            except Exception:
                pass

            task = self._transfer_manager.queue_transfer(
                self._selected_remote_path,
                local_path,
                self._selected_device,
                is_folder,
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
        """Try to connect to selected device for remote browser."""
        if not self._selected_device:
            return

        user = self.query_one("#remote-user", Input).value.strip() or None
        password = self.query_one("#remote-password", Input).value.strip() or None

        try:
            self._remote_sftp_client = SFTPClient(self._selected_device)
            self._remote_sftp_client.connect(username=user, password=password)

            # Refresh remote browser with initial path
            remote_browser = self.query_one("#remote-file-browser", RemoteFileBrowser)
            remote_browser._refresh_entries()

        except TransferError as e:
            self._remote_sftp_client = None
            self.notify(
                f"Cannot connect to {self._selected_device.name}: {e}",
                title="Connection Failed",
                severity="error",
            )
    
    def _execute_transfers(self) -> None:
        """Execute queued transfers in background.

        Loops until the queue is empty, picking up any tasks
        queued during execution.
        """
        try:
            while True:
                self._transfer_manager.execute_queue()
                if not self._transfer_manager.get_pending_tasks():
                    break
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
    
    def _on_transfer_progress(self, task: TransferTask) -> None:
        """Update queue display when transfer progress changes.
        
        Args:
            task: Task with updated progress
        """
        self.call_later(self._update_queue_display)
    
    def _update_queue_display(self) -> None:
        """Update the transfer queue display."""
        if not self._queue_update_enabled:
            return
        
        # Update the current tab's queue
        current_tab = self.query_one("#transfer-tabs", TabbedContent).active_pane
        if current_tab.id == "send-tab":
            queue_widget = self.query_one("#transfer-queue", TransferQueue)
        else:
            queue_widget = self.query_one("#transfer-queue-fetch", TransferQueue)
        
        tasks = self._transfer_manager.get_all_tasks()
        queue_widget.update_tasks(tasks)


def run_app() -> None:
    """Run the tailshare application."""
    app = TailshareApp()
    app.run()
