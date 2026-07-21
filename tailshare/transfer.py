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
from pathlib import Path
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
                for key_path in key_paths:
                    expanded_path = expand_path(key_path)
                    if os.path.exists(expanded_path):
                        auth_kwargs["key_filename"] = expanded_path
                        break
            
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
        
        # Transfer file with progress tracking
        start_time = time.time()
        
        def progress_hook(transferred: int) -> None:
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
        
        # Walk through source folder
        for root, dirs, files in os.walk(source_path):
            # Calculate relative path
            rel_path = os.path.relpath(root, source_path)
            if rel_path == ".":
                remote_dir = target_path
            else:
                remote_dir = os.path.join(target_path, rel_path)
            
            # Create remote directory
            self._ensure_remote_dir(remote_dir)
            
            # Transfer files
            for filename in files:
                source_file = os.path.join(root, filename)
                target_file = os.path.join(remote_dir, filename)
                
                self.transfer_file(
                    source_file,
                    target_file,
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
        except FileNotFoundError:
            # Create parent directories first
            parent = os.path.dirname(path)
            if parent and parent != path:
                self._ensure_remote_dir(parent)
            
            self._sftp_client.mkdir(path)
    
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
        self._current_client: SFTPClient | None = None
    
    def queue_transfer(
        self,
        source_path: str,
        target_path: str,
        device: Device,
        is_folder: bool = False,
        username: str | None = None,
        password: str | None = None,
    ) -> TransferTask:
        """Queue a file/folder transfer.
        
        Args:
            source_path: Local path to transfer
            target_path: Remote path destination
            device: Target device
            is_folder: True if transferring a folder
            username: SSH username for authentication
            password: SSH password for authentication
            
        Returns:
            Created transfer task
        """
        source_path = validate_file_path(source_path)
        target_path = expand_path(target_path)
        
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
    
    def execute_queue(self) -> None:
        """Execute all pending transfers in queue.
        
        Processes transfers sequentially, connecting to each device
        as needed.
        """
        pending = self.get_pending_tasks()
        
        if not pending:
            return
        
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
                # (Assuming same credentials for all tasks to the same device in one batch)
                first_task = tasks[0]
                client.connect(
                    username=first_task.username, 
                    password=first_task.password
                )
                
                for task in tasks:
                    try:
                        task.start()
                        
                        if os.path.isdir(task.source_path):
                            client.transfer_folder(
                                task.source_path,
                                task.target_path,
                                lambda p: self._update_progress(task, p),
                            )
                        else:
                            client.transfer_file(
                                task.source_path,
                                task.target_path,
                                lambda p: self._update_progress(task, p),
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
                        task.fail(f"Unexpected error: {e}")
                        self._logger.error(
                            f"Transfer error: {task.source_path} - {e}"
                        )
                
            finally:
                client.disconnect()
        
        self._logger.info("All transfers completed")
    
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
