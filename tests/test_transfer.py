"""Tests for transfer module."""


from tailshare.devices import Device
from tailshare.transfer import (
    TransferManager,
    TransferProgress,
    TransferTask,
)


class TestTransferProgress:
    """Tests for TransferProgress class."""

    def test_initial_state(self) -> None:
        """Test initial progress state."""
        progress = TransferProgress(filename="test.txt")

        assert progress.filename == "test.txt"
        assert progress.total_size == 0
        assert progress.transferred == 0
        assert progress.percentage == 0.0

    def test_update_progress(self) -> None:
        """Test updating progress."""
        progress = TransferProgress(filename="test.txt")

        progress.update(transferred=500, total_size=1000)

        assert progress.transferred == 500
        assert progress.total_size == 1000
        assert progress.percentage == 50.0

    def test_update_speed(self) -> None:
        """Test speed calculation."""
        progress = TransferProgress(filename="test.txt")
        progress.total_size = 10000
        progress.transferred = 5000

        progress.update_speed(elapsed_seconds=10.0)

        assert progress.speed_bps == 500.0
        assert progress.eta_seconds == 10.0


class TestTransferTask:
    """Tests for TransferTask class."""

    def test_task_creation(self) -> None:
        """Test creating a transfer task."""
        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )
        progress = TransferProgress(filename="test.txt")

        task = TransferTask(
            source_path="/local/test.txt",
            target_path="/remote/test.txt",
            device=device,
            progress=progress,
        )

        assert task.source_path == "/local/test.txt"
        assert task.status == "pending"
        assert task.error is None

    def test_task_start(self) -> None:
        """Test marking task as started."""
        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )
        progress = TransferProgress(filename="test.txt")

        task = TransferTask(
            source_path="/local/test.txt",
            target_path="/remote/test.txt",
            device=device,
            progress=progress,
        )

        task.start()

        assert task.status == "transferring"
        assert task.started_at is not None

    def test_task_complete(self) -> None:
        """Test marking task as completed."""
        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )
        progress = TransferProgress(filename="test.txt")

        task = TransferTask(
            source_path="/local/test.txt",
            target_path="/remote/test.txt",
            device=device,
            progress=progress,
        )

        task.complete()

        assert task.status == "completed"
        assert task.completed_at is not None
        assert progress.percentage == 100.0

    def test_task_fail(self) -> None:
        """Test marking task as failed."""
        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )
        progress = TransferProgress(filename="test.txt")

        task = TransferTask(
            source_path="/local/test.txt",
            target_path="/remote/test.txt",
            device=device,
            progress=progress,
        )

        task.fail("Connection lost")

        assert task.status == "failed"
        assert task.error == "Connection lost"
        assert task.completed_at is not None


class TestTransferManager:
    """Tests for TransferManager class."""

    def test_queue_transfer(self) -> None:
        """Test queuing a transfer."""
        manager = TransferManager()

        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )

        task = manager.queue_transfer(
            source_path="/local/test.txt",
            target_path="/remote/test.txt",
            device=device,
        )

        assert task.status == "pending"
        assert len(manager.get_pending_tasks()) == 1

    def test_cancel_task(self) -> None:
        """Test cancelling a pending transfer."""
        manager = TransferManager()

        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )

        task = manager.queue_transfer(
            source_path="/local/test.txt",
            target_path="/remote/test.txt",
            device=device,
        )

        manager.cancel_task(task)

        assert len(manager.get_pending_tasks()) == 0

    def test_clear_completed(self) -> None:
        """Test clearing completed tasks."""
        manager = TransferManager()

        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )

        task = manager.queue_transfer(
            source_path="/local/test.txt",
            target_path="/remote/test.txt",
            device=device,
        )
        task.complete()

        manager.clear_completed()

        assert len(manager.get_all_tasks()) == 0

    def test_get_pending_tasks(self) -> None:
        """Test getting only pending tasks."""
        manager = TransferManager()

        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )

        # Add pending task
        pending_task = manager.queue_transfer(
            source_path="/local/pending.txt",
            target_path="/remote/pending.txt",
            device=device,
        )

        # Add completed task
        completed_task = manager.queue_transfer(
            source_path="/local/completed.txt",
            target_path="/remote/completed.txt",
            device=device,
        )
        completed_task.complete()

        pending = manager.get_pending_tasks()

        assert len(pending) == 1
        assert pending[0] == pending_task
