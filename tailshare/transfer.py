"""SFTP file transfer module.

This module handles:
- SSH/SFTP connections to target devices
- File and folder transfers with progress tracking
- Transfer queue management
- Error handling and retry logic
"""

import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import paramiko

from tailshare.devices import Device
from tailshare.config import get_config, validate_file_path, expand_path


@dataclass
class TransferProgress:
    """Tracks progress of a file transfer.
    
    Attributes:
        filename: Name of file being transferred
        total_size: Total size in bytes
        transferred: Bytes transferred so far
        speed_bps: Current transfer speed in bytes/second
        eta_seconds: Estimated time remaining in seconds
        percentage: Transfer percentage (0-100)
    """
    
    filename: str
    total_size: int = 0
    transferred: int = 0
    speed_bps: float = 0.0
    eta_seconds: float = 0.0
    percentage: float = 0.0
    
    def update(self, transferred: int, total_size: int | None = None) -> None:
        """Update progress with new transferred bytes.
        
        Args:
            transferred: New total transferred bytes
            total_size: Total file size (if known)
        """
        self.transferred = transferred
        if total_size:
            self.total_size = total_size
        
        if self.total_size > 0:
            self.percentage = (self.transferred / self.total_size) * 100
    
    def update_speed(self, elapsed_seconds: float) -> None:
        """Calculate transfer speed and ETA.
        
        Args:
            elapsed_seconds: Time elapsed since transfer started
        """
        if elapsed_seconds > 0:
            self.speed_bps = self.transferred / elapsed_seconds
            remaining = self.total_size - self.transferred
            if self.speed_bps > 0:
                self.eta_seconds = remaining / self.speed_bps


class TransferDirection(str, Enum):
    """Transfer direction constants."""
    SEND = "send"
    FETCH = "fetch"


@dataclass
class TransferTask:
    """Represents a file transfer task.
    
    Attributes:
        source_path: Local path to file/folder
        target_path: Remote path on target device
        device: Target device
        progress: Transfer progress tracker
        status: Current status (pending, transferring, completed, failed)
        error: Error message if failed
        started_at: Transfer start time
        completed_at: Transfer completion time
        username: SSH username for authentication
        password: SSH password for authentication
        direction: Transfer direction (send or fetch)
    """
    
    source_path: str
    target_path: str
    device: Device
    progress: TransferProgress
    status: str = "pending"
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    username: str | None = None
    password: str | None = None
    direction: TransferDirection = TransferDirection.SEND
    
    def start(self) -> None:
        """Mark transfer as started."""
        self.status = "transferring"
        self.started_at = time.time()
    
    def complete(self) -> None:
        """Mark transfer as completed."""
        self.status = "completed"
        self.completed_at = time.time()
        self.progress.percentage = 100.0
    
    def fail(self, error: str) -> None:
        """Mark transfer as failed.
        
        Args:
            error: Error message
        """
        self.status = "failed"
        self.error = error
        self.completed_at = time.time()


class TransferError(Exception):
    """Exception raised when transfer fails."""
    pass


class SFTPClient:
    """SFTP client for file transfers.
    
    Handles SSH connections and SFTP operations for file transfers
    to Tailscale devices.
    
    Attributes:
        device: Target device
        ssh_client: SSH client connection
        sftp_client: SFTP client connection
    """
    
    def __init__(self, device: Device) -> None:
        """Initialize SFTP client.
        
        Args:
            device: Target device to connect to
        """
        self._device = device
        self._ssh_client: paramiko.SSHClient | None = None
        self._sftp_client: paramiko.SFTPClient | None = None
        self._logger = logging.getLogger(__name__)
        self._config = get_config()
    
    def connect(self, username: str | None = None, password: str | None = None) -> None:
        """Establish SSH/SFTP connection.
        
        Args:
            username: SSH username to use. If None, fallback to config/local.
            password: SSH password to use. If None, use key/agent.
        
        Raises:
            TransferError: If connection fails
        """
        try:
            self._ssh_client = paramiko.SSHClient()
            self._ssh_client.set_missing_host_key_policy(
                paramiko.AutoAddPolicy()
            )
            
            # Determine username
            ssh_user = username or self._config.get_ssh_user()
            
            # Prepare authentication
            auth_kwargs: dict[str, Any] = {"username": ssh_user}
            
            # Only set key_filename if explicitly provided in config.
            key_paths = self._config.get_ssh_key_paths()
            if key_paths:
                existing_keys = []
                for key_path in key_paths:
                    expanded_path = expand_path(key_path)
                    if os.path.exists(expanded_path):
                        existing_keys.append(expanded_path)
                if existing_keys:
                    auth_kwargs["key_filename"] = existing_keys
            
            if password:
                auth_kwargs["password"] = password

            # Logging authentication attempt (masking password)
            auth_method = "Password" if password else "Key/Agent"
            self._logger.info(
                f"Attempting connection to {self._device.ip} as '{ssh_user}' "
                f"using {auth_method}"
            )
            
            self._ssh_client.connect(
                hostname=self._device.ip,
                port=self._config.get_ssh_port(),
                timeout=self._config.get_ssh_timeout(),
                allow_agent=True,
                look_for_keys=True,
                **auth_kwargs,
            )
            
            self._sftp_client = self._ssh_client.open_sftp()
            
            self._logger.info(
                f"Connected to {self._device.name} at {self._device.ip}"
            )
            
        except paramiko.SSHException as e:
            raise TransferError(f"SSH connection failed: {e}")
        except ConnectionRefusedError:
            raise TransferError(
                f"Connection refused by {self._device.ip}. "
                "Ensure SSH is running on the target device."
            )
        except OSError as e:
            raise TransferError(f"Connection error: {e}")
    
    def disconnect(self) -> None:
        """Close SSH/SFTP connections."""
        if self._sftp_client:
            self._sftp_client.close()
            self._sftp_client = None
        if self._ssh_client:
            self._ssh_client.close()
            self._ssh_client = None
    
    def transfer_file(
        self,
        source_path: str,
        target_path: str,
        progress_callback: Callable[[TransferProgress], None] | None = None,
    ) -> None:
        """Transfer a single file via SFTP.
        
        Args:
            source_path: Local file path
            target_path: Remote file path
            progress_callback: Optional callback for progress updates
            
        Raises:
            TransferError: If transfer fails
        """
        if not self._sftp_client:
            raise TransferError("Not connected. Call connect() first.")
        
        # Validate source path
        source_path = validate_file_path(source_path)
        
        # Use remote path as-is, but handle '~' as current remote directory
        if target_path == "~":
            target_path = "."
        elif target_path.startswith("~"):
            target_path = target_path.replace("~", ".", 1)
            
        if not os.path.exists(source_path):
            raise TransferError(f"Source file not found: {source_path}")
        
        if not os.path.isfile(source_path):
            raise TransferError(f"Source is not a file: {source_path}")
        
        # Get file size
        file_size = os.path.getsize(source_path)
        progress = TransferProgress(filename=os.path.basename(source_path))
        
        self._logger.debug(
            f"Transferring {source_path} to {target_path} ({file_size} bytes)"
        )
        
        # Create target directory if needed
        target_dir = os.path.dirname(target_path)
        if target_dir:
            self._ensure_remote_dir(target_dir)
        
        # If target_path is a directory, append the filename to it
        try:
            sftp_stat = self._sftp_client.stat(target_path)
            if stat.S_ISDIR(sftp_stat.st_mode):
                target_path = os.path.join(target_path, os.path.basename(source_path))
                self._logger.info(f"Target is directory, updating path to: {target_path}")
        except (FileNotFoundError, IOError):
            pass
        
        # Transfer file with progress tracking
        start_time = time.time()
        
        self._logger.info(f"SFTP PUT: {source_path} -> {target_path}")
        
        def progress_hook(transferred: int, total: int = None) -> None:
            progress.update(transferred, file_size)
            progress.update_speed(time.time() - start_time)
            if progress_callback:
                progress_callback(progress)
        
        self._sftp_client.put(
            source_path,
            target_path,
            callback=progress_hook,
        )
        
        progress.update(file_size, file_size)
        if progress_callback:
            progress_callback(progress)
        
        self._logger.debug(f"File transfer complete: {target_path}")
    
    def transfer_folder(
        self,
        source_path: str,
        target_path: str,
        progress_callback: Callable[[TransferProgress], None] | None = None,
    ) -> None:
        """Transfer a folder recursively via SFTP.

        Args:
            source_path: Local folder path
            target_path: Remote folder path
            progress_callback: Optional callback for progress updates

        Raises:
            TransferError: If transfer fails
        """
        if not self._sftp_client:
            raise TransferError("Not connected. Call connect() first.")

        # Validate source path
        source_path = validate_file_path(source_path)

        if not os.path.exists(source_path):
            raise TransferError(f"Source folder not found: {source_path}")

        if not os.path.isdir(source_path):
            raise TransferError(f"Source is not a folder: {source_path}")

        self._logger.debug(
            f"Transferring folder {source_path} to {target_path}"
        )

        # Create target directory
        self._ensure_remote_dir(target_path)

        # Calculate total size for aggregate progress
        total_size = 0
        files_to_transfer: list[tuple[str, str]] = []
        for root, dirs, files in os.walk(source_path):
            rel_path = os.path.relpath(root, source_path)
            if rel_path == ".":
                remote_dir = target_path
            else:
                remote_dir = os.path.join(target_path, rel_path)

            self._ensure_remote_dir(remote_dir)

            for filename in files:
                source_file = os.path.join(root, filename)
                target_file = os.path.join(remote_dir, filename)
                files_to_transfer.append((source_file, target_file))
                total_size += os.path.getsize(source_file)

        # Transfer files with aggregate progress
        folder_progress = TransferProgress(
            filename=os.path.basename(source_path),
            total_size=total_size,
        )
        start_time = time.time()
        transferred = 0

        for source_file, target_file in files_to_transfer:
            file_size = os.path.getsize(source_file)

            def make_callback(
                fp=folder_progress, st=start_time
            ) -> Callable[[TransferProgress], None]:
                def callback(file_progress: TransferProgress) -> None:
                    nonlocal transferred
                    # Update aggregate progress
                    fp.transferred = transferred + file_progress.transferred
                    if fp.total_size > 0:
                        fp.percentage = (fp.transferred / fp.total_size) * 100
                    fp.update_speed(time.time() - st)
                    if progress_callback:
                        progress_callback(fp)

                    # Also report individual file progress
                    if progress_callback:
                        progress_callback(file_progress)
                    return

                return callback

            self.transfer_file(
                source_file,
                target_file,
                make_callback(),
            )
            transferred += file_size

        # Final progress update
        folder_progress.update(total_size, total_size)
        if progress_callback:
            progress_callback(folder_progress)
    
    def list_remote_dir(self, path: str) -> list[tuple[str, bool]]:
        """List contents of a remote directory.
        
        Args:
            path: Remote directory path
            
        Returns:
            List of (name, is_directory) tuples
            
        Raises:
            TransferError: If directory cannot be listed
        """
        if not self._sftp_client:
            raise TransferError("Not connected. Call connect() first.")
        
        # Handle ~ in remote path (SFTP doesn't support shell expansion)
        if path == "~":
            path = "."
        elif path.startswith("~"):
            path = path.replace("~", ".", 1)
        
        try:
            entries = []
            for attr in self._sftp_client.listdir_attr(path):
                name = attr.filename
                is_dir = stat.S_ISDIR(attr.st_mode)
                entries.append((name, is_dir))
            
            return sorted(entries, key=lambda e: (not e[1], e[0]))
            
        except (IOError, OSError) as e:
            raise TransferError(f"Cannot list directory {path}: {e}")
    
    def fetch_file(
        self,
        source_path: str,
        target_path: str,
        progress_callback: Callable[[TransferProgress], None] | None = None,
    ) -> None:
        """Fetch a single file via SFTP.
        
        Args:
            source_path: Remote file path
            target_path: Local file path
            progress_callback: Optional callback for progress updates
            
        Raises:
            TransferError: If fetch fails
        """
        if not self._sftp_client:
            raise TransferError("Not connected. Call connect() first.")
        
        # Handle ~ in remote path
        if source_path == "~":
            source_path = "."
        elif source_path.startswith("~"):
            source_path = source_path.replace("~", ".", 1)
        
        # Ensure local target directory exists
        target_dir = os.path.dirname(target_path)
        if target_dir:
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as e:
                raise TransferError(f"Cannot create local directory: {e}")
        
        # If target_path is a directory, append the filename to it
        if os.path.isdir(target_path):
            target_path = os.path.join(target_path, os.path.basename(source_path))
            self._logger.info(f"Local target is directory, updating path to: {target_path}")
        
        # Get remote file info
        try:
            remote_stat = self._sftp_client.stat(source_path)

        except (IOError, OSError) as e:
            raise TransferError(f"Remote file not found: {source_path}: {e}")
        
        file_size = remote_stat.st_size
        progress = TransferProgress(filename=os.path.basename(source_path))
        
        self._logger.debug(
            f"Fetching {source_path} to {target_path} ({file_size} bytes)"
        )
        
        start_time = time.time()
        
        self._logger.info(f"SFTP GET: {source_path} -> {target_path}")
        
        def progress_hook(transferred: int, total: int = None) -> None:
            progress.update(transferred, file_size)
            progress.update_speed(time.time() - start_time)
            if progress_callback:
                progress_callback(progress)
        
        self._sftp_client.get(
            source_path,
            target_path,
            callback=progress_hook,
        )
        
        progress.update(file_size, file_size)
        if progress_callback:
            progress_callback(progress)
        
        self._logger.debug(f"File fetch complete: {target_path}")
    
    def fetch_folder(
        self,
        source_path: str,
        target_path: str,
        progress_callback: Callable[[TransferProgress], None] | None = None,
    ) -> None:
        """Fetch a folder recursively via SFTP.
        
        Args:
            source_path: Remote folder path
            target_path: Local folder path
            progress_callback: Optional callback for progress updates
            
        Raises:
            TransferError: If fetch fails
        """
        if not self._sftp_client:
            raise TransferError("Not connected. Call connect() first.")
        
        # Handle ~ in remote path
        if source_path == "~":
            source_path = "."
        elif source_path.startswith("~"):
            source_path = source_path.replace("~", ".", 1)
        
        self._logger.debug(
            f"Fetching folder {source_path} to {target_path}"
        )
        
        # Create local target directory
        try:
            os.makedirs(target_path, exist_ok=True)
        except OSError as e:
            raise TransferError(f"Cannot create local directory: {e}")
        
        # List remote directory
        try:
            entries = self.list_remote_dir(source_path)
        except TransferError as e:
            raise TransferError(f"Cannot browse remote directory: {e}")
        
        # Create local directory
        os.makedirs(target_path, exist_ok=True)
        
        # Process entries
        for name, is_dir in entries:
            remote_entry = os.path.join(source_path, name)
            local_entry = os.path.join(target_path, name)
            
            if is_dir:
                self.fetch_folder(
                    remote_entry,
                    local_entry,
                    progress_callback,
                )
            else:
                self.fetch_file(
                    remote_entry,
                    local_entry,
                    progress_callback,
                )
    
    def _ensure_remote_dir(self, path: str) -> None:
        """Ensure remote directory exists, creating if needed.
        
        Args:
            path: Remote directory path
        """
        if not self._sftp_client:
            return
        
        try:
            self._sftp_client.stat(path)
        except (FileNotFoundError, IOError):
            # Create parent directories first
            parent = os.path.dirname(path)
            if parent and parent != path:
                self._ensure_remote_dir(parent)
            
            try:
                self._sftp_client.mkdir(path)
            except IOError as e:
                self._logger.debug(f"mkdir failed for {path}: {e}")
                # It might already exist or be a file; we'll let put() fail if so
                pass
    
    def test_connection(self) -> tuple[bool, str]:
        """Test if connection to device is possible.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            self.connect()
            self.disconnect()
            return True, "Connection successful"
        except TransferError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Unexpected error: {e}"


class TransferManager:
    """Manages file transfer tasks.
    
    Handles queuing, executing, and monitoring file transfers
    to Tailscale devices.
    
    Attributes:
        tasks: List of pending/active transfer tasks
    """
    
    def __init__(self) -> None:
        """Initialize transfer manager."""
        self._tasks: list[TransferTask] = []
        self._logger = logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._progress_callback: Any = None
    
    def queue_transfer(
        self,
        source_path: str,
        target_path: str,
        device: Device,
        is_folder: bool = False,
        username: str | None = None,
        password: str | None = None,
        direction: TransferDirection = TransferDirection.SEND,
    ) -> TransferTask:
        """Queue a file/folder transfer.
        
        Args:
            source_path: Local path to transfer
            target_path: Remote path destination
            device: Target device
            is_folder: True if transferring a folder
            username: SSH username for authentication
            password: SSH password for authentication
            direction: Transfer direction (send or fetch)
            
        Returns:
            Created transfer task
        """
        if direction == TransferDirection.SEND:
            source_path = validate_file_path(source_path, is_local=True)
            # Target path should not be expanded locally as it's for the remote system
            target_path = target_path
        else:
            # For fetch, source_path is remote, validate without abspath
            source_path = validate_file_path(source_path, is_local=False)
            # target_path is local and should be expanded and validated
            target_path = validate_file_path(expand_path(target_path), is_local=True)
        
        progress = TransferProgress(
            filename=os.path.basename(source_path),
        )
        
        task = TransferTask(
            source_path=source_path,
            target_path=target_path,
            device=device,
            progress=progress,
            username=username,
            password=password,
            direction=direction,
        )
        
        with self._lock:
            self._tasks.append(task)
        
        self._logger.info(
            f"Queued transfer: {source_path} -> {device.name}:{target_path}"
        )
        
        return task
    
    def cancel_task(self, task: TransferTask) -> None:
        """Cancel a pending transfer task.
        
        Args:
            task: Task to cancel
        """
        with self._lock:
            if task in self._tasks:
                self._tasks.remove(task)
                self._logger.info(f"Cancelled transfer: {task.source_path}")
    
    def clear_completed(self) -> None:
        """Remove completed and failed tasks from queue."""
        with self._lock:
            self._tasks = [
                t for t in self._tasks
                if t.status not in ("completed", "failed")
            ]
    
    def get_pending_tasks(self) -> list[TransferTask]:
        """Get list of pending/active tasks.
        
        Returns:
            List of tasks not yet completed or failed
        """
        with self._lock:
            return [
                t for t in self._tasks
                if t.status not in ("completed", "failed")
            ]
    
    def get_all_tasks(self) -> list[TransferTask]:
        """Get all tasks.
        
        Returns:
            List of all transfer tasks
        """
        with self._lock:
            return self._tasks.copy()
    
    def set_progress_callback(self, callback: Any) -> None:
        """Set callback to invoke when progress changes.
        
        Args:
            callback: Function to call with the updated task
        """
        self._progress_callback = callback
    
    def execute_queue(self) -> None:
        """Execute all pending transfers in queue.
        
        Processes transfers sequentially, connecting to each device
        as needed. Loops until no pending tasks remain, so tasks
        queued during execution are picked up automatically.
        """
        while True:
            pending = self.get_pending_tasks()
            
            if not pending:
                break
            
            # Group tasks by device for efficiency
            device_tasks: dict[str, list[TransferTask]] = {}
            for task in pending:
                device_key = task.device.ip
                if device_key not in device_tasks:
                    device_tasks[device_key] = []
                device_tasks[device_key].append(task)
            
            for device_ip, tasks in device_tasks.items():
                # Find the device object
                device = tasks[0].device
                
                self._logger.info(
                    f"Starting transfers to {device.name} ({len(tasks)} tasks)"
                )
                
                client = SFTPClient(device)
                
                try:
                    # Connect using the first task's credentials for this device
                    first_task = tasks[0]
                    try:
                        client.connect(
                            username=first_task.username, 
                            password=first_task.password
                        )
                    except TransferError as e:
                        self._logger.error(f"Connection failed for {device.name}: {e}")
                        for task in tasks:
                            task.fail(f"Connection failed: {e}")
                        continue
                    
                    for task in tasks:
                        try:
                            task.start()
                            
                            if task.direction == TransferDirection.FETCH:
                                if task.source_path == "~":
                                    remote_path = "."
                                elif task.source_path.startswith("~"):
                                    remote_path = task.source_path.replace("~", ".", 1)
                                else:
                                    remote_path = task.source_path
                                
                                try:
                                    stat_result = client._sftp_client.stat(remote_path)
                                    is_dir = stat.S_ISDIR(stat_result.st_mode)
                                except (IOError, OSError):
                                    is_dir = False
                                
                                if is_dir:
                                    client.fetch_folder(
                                        remote_path,
                                        task.target_path,
                                        lambda p, t=task: self._update_progress(t, p),
                                    )
                                else:
                                    client.fetch_file(
                                        remote_path,
                                        task.target_path,
                                        lambda p, t=task: self._update_progress(t, p),
                                    )
                            else:
                                if os.path.isdir(task.source_path):
                                    client.transfer_folder(
                                        task.source_path,
                                        task.target_path,
                                        lambda p, t=task: self._update_progress(t, p),
                                    )
                                else:
                                    client.transfer_file(
                                        task.source_path,
                                        task.target_path,
                                        lambda p, t=task: self._update_progress(t, p),
                                    )
                            
                            task.complete()
                            self._logger.info(
                                f"Transfer complete: {task.source_path}"
                            )
                            
                        except TransferError as e:
                            task.fail(str(e))
                            self._logger.error(
                                f"Transfer failed: {task.source_path} - {e}"
                            )
                        except Exception as e:
                            error_msg = f"Unexpected error ({type(e).__name__}): {e}"
                            task.fail(error_msg)
                            self._logger.error(
                                f"Transfer error: {task.source_path} - {error_msg}"
                            )
                
                finally:
                    client.disconnect()
    
    def _update_progress(
        self,
        task: TransferTask,
        progress: TransferProgress,
    ) -> None:
        """Update task progress.
        
        Args:
            task: Transfer task
            progress: New progress state
        """
        task.progress = progress
        if self._progress_callback:
            self._progress_callback(task)
    
    def test_device_connection(
        self, 
        device: Device, 
        username: str | None = None, 
        password: str | None = None
    ) -> tuple[bool, str]:
        """Test connection to a device.
        
        Args:
            device: Device to test
            username: SSH username for authentication
            password: SSH password for authentication
            
        Returns:
            Tuple of (success, message)
        """
        client = SFTPClient(device)
        try:
            client.connect(username=username, password=password)
            client.disconnect()
            return True, "Connection successful"
        except TransferError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Unexpected error: {e}"
