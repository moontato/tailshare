"""SFTPClient and TransferManager tests against an in-memory SFTP filesystem."""

import posixpath
import threading
import types

import paramiko
import pytest

import tailshare.config as config_module
import tailshare.transfer as transfer_module
from tailshare.devices import Device
from tailshare.transfer import (
    TRANSFER_CHUNK_SIZE,
    SFTPClient,
    TransferCancelled,
    TransferDirection,
    TransferError,
    TransferManager,
)

DIR_MODE = 0o40755
FILE_MODE = 0o100644


class FakeSFTPFile:
    """In-memory stand-in for paramiko.SFTPFile (one open file).

    Writes go straight into the tree (like a real SFTP session that
    streams data to the server), so an aborted transfer leaves a
    partial file behind until it is unlinked/removed.
    """

    def __init__(self, sftp: "FakeSFTP", path: str, mode: str) -> None:
        self._sftp = sftp
        self._norm = sftp._norm(path)
        self._pos = 0
        if "w" in mode:
            sftp.tree[self._norm] = b""
        elif self._norm not in sftp.tree:
            raise FileNotFoundError(f"No such file: {path}")

    def read(self, n: int = -1) -> bytes:
        if self._sftp.read_hook:
            self._sftp.read_hook(self._norm)
        data = self._sftp.tree.get(self._norm) or b""
        end = len(data) if n is None or n < 0 else self._pos + n
        chunk = data[self._pos:end]
        self._pos += len(chunk)
        return chunk

    def write(self, data: bytes) -> int:
        if self._sftp.write_hook:
            self._sftp.write_hook(self._norm)
        existing = self._sftp.tree.get(self._norm) or b""
        self._sftp.tree[self._norm] = existing + data
        return len(data)

    def close(self) -> None:
        pass


class FakeSFTP:
    """In-memory stand-in for paramiko.SFTPClient."""

    def __init__(self) -> None:
        # path -> bytes for files, None for directories
        self.tree: dict[str, bytes | None] = {"/": None}
        self.cwd = "/"
        self.stat_calls: list[str] = []
        self.mkdir_calls: list[str] = []
        self.closed = False
        # Optional test hooks, called on every read/write with the
        # normalized path (used to simulate cancellation mid-transfer).
        self.read_hook = None
        self.write_hook = None

    # -- path helpers -------------------------------------------------
    def _norm(self, path: str) -> str:
        if path == "~":
            path = "."
        if not posixpath.isabs(path):
            path = posixpath.join(self.cwd, path)
        return posixpath.normpath(path)

    def _require(self, path: str) -> bytes | None:
        norm = self._norm(path)
        if norm not in self.tree:
            raise FileNotFoundError(f"No such file: {path}")
        return self.tree[norm]

    # -- paramiko.SFTPClient API surface used by SFTPClient -----------
    def stat(self, path: str) -> types.SimpleNamespace:
        self.stat_calls.append(self._norm(path))
        norm = self._norm(path)
        if norm not in self.tree:
            raise FileNotFoundError(f"No such file: {path}")
        is_dir = self.tree[norm] is None
        size = 0 if is_dir else len(self.tree[norm])
        return types.SimpleNamespace(
            st_mode=DIR_MODE if is_dir else FILE_MODE,
            st_size=size,
        )

    def listdir_attr(self, path: str) -> list[types.SimpleNamespace]:
        norm = self._norm(path)
        if norm not in self.tree or self.tree[norm] is not None:
            raise NotADirectoryError(f"Not a directory: {path}")
        prefix = norm + "/" if norm != "/" else ""
        entries = []
        for key, value in self.tree.items():
            if key == norm or not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if "/" in rest:
                continue  # not a direct child
            is_dir = value is None
            entries.append(
                types.SimpleNamespace(
                    filename=rest,
                    st_mode=DIR_MODE if is_dir else FILE_MODE,
                    st_size=0 if is_dir else len(value),
                )
            )
        return entries

    def mkdir(self, path: str) -> None:
        norm = self._norm(path)
        self.mkdir_calls.append(norm)
        if norm in self.tree:
            raise FileExistsError(f"Already exists: {path}")
        if self.tree.get(posixpath.dirname(norm)) is not None:
            raise NotADirectoryError(f"Parent is a file: {path}")
        self.tree[norm] = None

    def file(self, path: str, mode: str = "r", bufsize: int = -1) -> FakeSFTPFile:
        return FakeSFTPFile(self, path, mode)

    def unlink(self, path: str) -> None:
        norm = self._norm(path)
        if norm not in self.tree:
            raise FileNotFoundError(f"No such file: {path}")
        del self.tree[norm]

    def normalize(self, path: str) -> str:
        # paramiko's REALPATH: server-side absolute resolution.
        norm = self._norm(path)
        if norm not in self.tree:
            raise FileNotFoundError(f"No such file: {path}")
        return norm

    def close(self) -> None:
        self.closed = True

    # -- test helpers ---------------------------------------------------
    def add_file(self, path: str, content: bytes) -> None:
        norm = self._norm(path)
        self.tree[norm] = content
        parent = posixpath.dirname(norm)
        while parent and parent != "/" and parent not in self.tree:
            self.tree[parent] = None
            parent = posixpath.dirname(parent)

    def add_dir(self, path: str) -> None:
        norm = self._norm(path)
        parent = posixpath.dirname(norm)
        while parent and parent != "/" and parent not in self.tree:
            self.tree[parent] = None
            parent = posixpath.dirname(parent)
        self.tree[norm] = None


@pytest.fixture
def fake_sftp() -> FakeSFTP:
    return FakeSFTP()


@pytest.fixture
def client(fake_sftp: FakeSFTP) -> SFTPClient:
    device = Device(
        name="test-pc",
        hostname="test-pc",
        ip="100.64.0.1",
        online=True,
        last_seen=None,
        machine_id="m1",
    )
    sftp_client = SFTPClient(device)
    sftp_client._sftp_client = fake_sftp
    return sftp_client


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Reset the config singleton and isolate HOME for each test."""
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


class TestSFTPConnect:
    """SFTPClient.connect() against a fake paramiko.SSHClient."""

    def test_connect_success(self, monkeypatch, tmp_path) -> None:
        class FakeSSH:
            instances: list["FakeSSH"] = []

            def __init__(self) -> None:
                self.kwargs: dict = {}
                self.closed = False
                self.sftp = FakeSFTP()
                FakeSSH.instances.append(self)

            def set_missing_host_key_policy(self, policy) -> None:
                pass

            def connect(self, **kwargs) -> None:
                self.kwargs = kwargs

            def open_sftp(self) -> FakeSFTP:
                return self.sftp

            def close(self) -> None:
                self.closed = True

        monkeypatch.setattr(transfer_module.paramiko, "SSHClient", FakeSSH)
        monkeypatch.setattr(
            config_module.getpass, "getuser", lambda: "testuser"
        )

        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="m1",
        )
        sftp_client = SFTPClient(device)
        sftp_client.connect(password="secret")

        ssh = FakeSSH.instances[0]
        assert ssh.kwargs["hostname"] == "100.64.0.1"
        assert ssh.kwargs["port"] == 22
        assert ssh.kwargs["username"] == "testuser"
        assert ssh.kwargs["password"] == "secret"
        assert "key_filename" not in ssh.kwargs
        assert sftp_client._sftp_client is ssh.sftp
        assert sftp_client.device is device

        sftp_client.disconnect()
        assert ssh.closed
        assert ssh.sftp.closed
        assert sftp_client._sftp_client is None

    def test_connect_failure_raises_transfer_error(self, monkeypatch) -> None:
        class FakeSSH:
            def set_missing_host_key_policy(self, policy) -> None:
                pass

            def connect(self, **kwargs) -> None:
                raise paramiko.SSHException("auth failed")

            def open_sftp(self) -> FakeSFTP:
                raise AssertionError("unreachable")

            def close(self) -> None:
                pass

        monkeypatch.setattr(transfer_module.paramiko, "SSHClient", FakeSSH)
        monkeypatch.setattr(
            config_module.getpass, "getuser", lambda: "testuser"
        )

        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="m1",
        )
        sftp_client = SFTPClient(device)
        with pytest.raises(TransferError, match="SSH connection failed"):
            sftp_client.connect()

    def test_connect_not_connected_error(self, client) -> None:
        client._sftp_client = None
        with pytest.raises(TransferError, match="Not connected"):
            client.transfer_file("/nope", "/nope")
        with pytest.raises(TransferError, match="Not connected"):
            client.fetch_file("/nope", "/nope")


class TestSFTPPathHandling:
    def test_is_remote_dir(self, client, fake_sftp) -> None:
        fake_sftp.add_dir("/etc")
        fake_sftp.add_file("/etc/passwd", b"root:x")

        assert client.is_remote_dir("/etc") is True
        assert client.is_remote_dir("/etc/passwd") is False
        assert client.is_remote_dir("/missing") is None

    def test_canonicalize_uses_server_realpath(self, client, fake_sftp) -> None:
        fake_sftp.add_dir("/home/u")
        fake_sftp.cwd = "/home/u"
        assert client.canonicalize("~") == "/home/u"
        assert client.canonicalize("/home") == "/home"

    def test_canonicalize_returns_none_when_not_connected(
        self, fake_sftp
    ) -> None:
        device = Device(
            name="test-pc",
            hostname="test-pc",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="m1",
        )
        not_connected = SFTPClient(device)
        assert not_connected.canonicalize("~") is None

    def test_canonicalize_returns_none_on_error(self, client, fake_sftp) -> None:
        assert client.canonicalize("/nope") is None

    def test_is_remote_dir_tilde_maps_to_cwd(self, client, fake_sftp) -> None:
        fake_sftp.add_dir("/home/u")
        fake_sftp.cwd = "/home/u"

        assert client.is_remote_dir("~") is True
        assert "/home/u" in fake_sftp.stat_calls

    def test_is_remote_dir_unconnected(self, client) -> None:
        client._sftp_client = None
        assert client.is_remote_dir("/etc") is None

    def test_list_remote_dir_sorted_dirs_first(self, client, fake_sftp) -> None:
        fake_sftp.add_dir("/data/zebra")
        fake_sftp.add_dir("/data/alpha")
        fake_sftp.add_file("/data/file.txt", b"x")
        fake_sftp.add_file("/data/b.txt", b"y")

        entries = client.list_remote_dir("/data")

        assert entries == [
            ("alpha", True),
            ("zebra", True),
            ("b.txt", False),
            ("file.txt", False),
        ]

    def test_list_remote_dir_error(self, client) -> None:
        with pytest.raises(TransferError, match="Cannot list directory"):
            client.list_remote_dir("/missing")


class TestTransferFile:
    def test_send_file_creates_remote_file(self, client, fake_sftp, tmp_path) -> None:
        local = tmp_path / "a.txt"
        local.write_bytes(b"hello")

        progress_updates: list[float] = []
        client.transfer_file(
            str(local), "/dest/a.txt", lambda p: progress_updates.append(p.percentage)
        )

        assert fake_sftp.tree["/dest/a.txt"] == b"hello"
        assert progress_updates[-1] == 100.0

    def test_send_file_target_directory_appends_basename(
        self, client, fake_sftp, tmp_path
    ) -> None:
        fake_sftp.add_dir("/dest")
        local = tmp_path / "a.txt"
        local.write_bytes(b"data")

        client.transfer_file(str(local), "/dest")

        assert fake_sftp.tree["/dest/a.txt"] == b"data"

    def test_send_file_missing_source(self, client) -> None:
        with pytest.raises(TransferError, match="Source file not found"):
            client.transfer_file("/definitely/missing.txt", "/dest")

    def test_send_source_traversal_rejected(self, client, tmp_path) -> None:
        with pytest.raises(ValueError, match="traversal"):
            client.transfer_file(f"{tmp_path}/../etc", "/dest")


class TestFetchFile:
    def test_fetch_file_writes_local_file(self, client, fake_sftp, tmp_path) -> None:
        fake_sftp.add_file("/remote/a.txt", b"remote-data")
        target = tmp_path / "out" / "a.txt"

        client.fetch_file("/remote/a.txt", str(target))

        assert target.read_bytes() == b"remote-data"

    def test_fetch_file_target_directory_appends_basename(
        self, client, fake_sftp, tmp_path
    ) -> None:
        fake_sftp.add_file("/remote/a.txt", b"remote-data")
        target_dir = tmp_path / "out"
        target_dir.mkdir()

        client.fetch_file("/remote/a.txt", str(target_dir))

        assert (target_dir / "a.txt").read_bytes() == b"remote-data"

    def test_fetch_file_missing_remote(self, client, tmp_path) -> None:
        with pytest.raises(TransferError, match="Remote file not found"):
            client.fetch_file("/remote/missing.txt", str(tmp_path / "out"))


class TestFetchFolder:
    def test_fetch_folder_nested(self, client, fake_sftp, tmp_path) -> None:
        fake_sftp.add_file("/top/a.txt", b"A")
        fake_sftp.add_file("/top/nested/b.bin", b"BB")

        target = tmp_path / "dest"
        client.fetch_folder("/top", str(target))

        assert (target / "a.txt").read_bytes() == b"A"
        assert (target / "nested" / "b.bin").read_bytes() == b"BB"

    def test_fetch_folder_missing_remote(self, client, tmp_path) -> None:
        with pytest.raises(TransferError, match="Cannot browse remote directory"):
            client.fetch_folder("/missing", str(tmp_path / "dest"))

    def test_fetch_folder_stops_at_max_depth(
        self, client, fake_sftp, tmp_path, monkeypatch
    ) -> None:
        """Symlink-loop guard: recursion must stop at MAX_FOLDER_DEPTH."""
        monkeypatch.setattr(transfer_module, "MAX_FOLDER_DEPTH", 3)
        fake_sftp.add_file("/top/f.txt", b"F")
        # a directory that repeats itself forever (symlink to ancestor)
        fake_sftp.add_file("/top/loop/loop/deep.txt", b"D")
        fake_sftp.add_dir("/top/loop/loop/loop/loop")

        target = tmp_path / "dest"
        client.fetch_folder("/top", str(target))

        assert (target / "f.txt").read_bytes() == b"F"
        assert (target / "loop" / "loop" / "deep.txt").read_bytes() == b"D"
        # recursion stopped at the cap; nothing deeper was created
        assert not (target / "loop" / "loop" / "loop").exists()


class TestTransferFolder:
    def test_send_folder_nested(self, client, fake_sftp, tmp_path) -> None:
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_bytes(b"A")
        (src / "sub" / "b.bin").write_bytes(b"BB")

        client.transfer_folder(str(src), "/remote/dir")

        assert fake_sftp.tree["/remote/dir/a.txt"] == b"A"
        assert fake_sftp.tree["/remote/dir/sub/b.bin"] == b"BB"

    def test_send_folder_missing_source(self, client) -> None:
        with pytest.raises(TransferError, match="Source folder not found"):
            client.transfer_folder("/definitely/missing", "/remote")


class TestTransferCancellation:
    """Chunked transfers honour a cancel event mid-file/folder."""

    def test_send_file_cancelled_before_start(
        self, client, fake_sftp, tmp_path
    ) -> None:
        local = tmp_path / "a.txt"
        local.write_bytes(b"hello")
        cancel = threading.Event()
        cancel.set()

        with pytest.raises(TransferCancelled):
            client.transfer_file(str(local), "/dest/a.txt", cancel_event=cancel)

        assert "/dest/a.txt" not in fake_sftp.tree
        assert local.read_bytes() == b"hello"

    def test_send_file_cancelled_mid_transfer_removes_partial(
        self, client, fake_sftp, tmp_path
    ) -> None:
        payload = b"x" * (TRANSFER_CHUNK_SIZE * 3)
        local = tmp_path / "big.bin"
        local.write_bytes(payload)
        cancel = threading.Event()
        fake_sftp.write_hook = lambda path: cancel.set()

        with pytest.raises(TransferCancelled):
            client.transfer_file(str(local), "/dest/big.bin", cancel_event=cancel)

        # partial upload removed, local source untouched
        assert "/dest/big.bin" not in fake_sftp.tree
        assert local.read_bytes() == payload

    def test_fetch_file_cancelled_mid_transfer_removes_partial(
        self, client, fake_sftp, tmp_path
    ) -> None:
        fake_sftp.add_file("/remote/big.bin", b"y" * (TRANSFER_CHUNK_SIZE * 3))
        target = tmp_path / "out" / "big.bin"
        cancel = threading.Event()
        fake_sftp.read_hook = lambda path: cancel.set()

        with pytest.raises(TransferCancelled):
            client.fetch_file("/remote/big.bin", str(target), cancel_event=cancel)

        assert not target.exists()

    def test_send_folder_cancelled_between_files(
        self, client, fake_sftp, tmp_path
    ) -> None:
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_bytes(b"A")
        (src / "sub" / "b.bin").write_bytes(b"BB")
        cancel = threading.Event()

        def hook(path):
            if path == "/remote/dir/sub/b.bin":
                cancel.set()

        fake_sftp.write_hook = hook

        with pytest.raises(TransferCancelled):
            client.transfer_folder(str(src), "/remote/dir", cancel_event=cancel)

        assert fake_sftp.tree["/remote/dir/a.txt"] == b"A"
        assert "/remote/dir/sub/b.bin" not in fake_sftp.tree

    def test_fetch_folder_cancelled_between_files(
        self, client, fake_sftp, tmp_path
    ) -> None:
        fake_sftp.add_file("/top/a.txt", b"A" * 50)
        fake_sftp.add_file("/top/b.bin", b"B" * (TRANSFER_CHUNK_SIZE * 2))
        target = tmp_path / "dest"
        cancel = threading.Event()

        def hook(path):
            if path == "/top/b.bin":
                cancel.set()

        fake_sftp.read_hook = hook

        with pytest.raises(TransferCancelled):
            client.fetch_folder("/top", str(target), cancel_event=cancel)

        assert (target / "a.txt").read_bytes() == b"A" * 50
        assert not (target / "b.bin").exists()


class TestEnsureRemoteDir:
    def test_creates_parent_chain(self, client, fake_sftp) -> None:
        client._ensure_remote_dir("/a/b/c")

        assert fake_sftp.mkdir_calls == ["/a", "/a/b", "/a/b/c"]

    def test_existing_dir_not_recreated(self, client, fake_sftp) -> None:
        fake_sftp.add_dir("/a/b")
        client._ensure_remote_dir("/a/b")
        assert fake_sftp.mkdir_calls == []


class TestExecuteQueue:
    """TransferManager.execute_queue against a fake SFTPClient factory."""

    def _make_factory(
        self,
        monkeypatch,
        shared: FakeSFTP | None = None,
        connect_error: Exception | None = None,
    ) -> list[SFTPClient]:
        created: list[SFTPClient] = []
        fs = shared if shared is not None else FakeSFTP()

        def factory(device: Device) -> SFTPClient:
            client = SFTPClient(device)
            client._sftp_client = fs
            created.append(client)

            def fake_connect(username=None, password=None) -> None:
                if connect_error is not None:
                    raise connect_error

            client.connect = fake_connect  # type: ignore[method-assign]
            return client

        monkeypatch.setattr(transfer_module, "SFTPClient", factory)
        return created

    def _device(self, ip: str = "100.64.0.1") -> Device:
        return Device(
            name="test-pc",
            hostname="test-pc",
            ip=ip,
            online=True,
            last_seen=None,
            machine_id=f"m-{ip}",
        )

    def test_execute_send_file(self, monkeypatch, tmp_path) -> None:
        fs = FakeSFTP()
        self._make_factory(monkeypatch, shared=fs)
        local = tmp_path / "a.txt"
        local.write_bytes(b"payload")

        manager = TransferManager()
        task = manager.queue_transfer(
            str(local), "remote/a.txt", self._device(),
            direction=TransferDirection.SEND,
        )
        manager.execute_queue()

        assert task.status == "completed"
        assert fs.tree["/remote/a.txt"] == b"payload"

    def test_execute_fetch_file(self, monkeypatch, tmp_path) -> None:
        fs = FakeSFTP()
        fs.add_file("/remote/a.txt", b"remote-payload")
        self._make_factory(monkeypatch, shared=fs)

        manager = TransferManager()
        target = tmp_path / "out" / "a.txt"
        # local target's parent directory is created automatically
        task = manager.queue_transfer(
            "remote/a.txt", str(target), self._device(),
            direction=TransferDirection.FETCH,
        )
        manager.execute_queue()

        assert task.status == "completed"
        assert target.read_bytes() == b"remote-payload"

    def test_execute_fetch_folder(self, monkeypatch, tmp_path) -> None:
        fs = FakeSFTP()
        fs.add_file("/remote/dir/a.txt", b"A")
        fs.add_file("/remote/dir/nested/b.bin", b"B")
        self._make_factory(monkeypatch, shared=fs)

        manager = TransferManager()
        task = manager.queue_transfer(
            "remote/dir", str(tmp_path / "out"), self._device(),
            direction=TransferDirection.FETCH,
        )
        manager.execute_queue()

        assert task.status == "completed"
        assert (tmp_path / "out" / "a.txt").read_bytes() == b"A"
        assert (tmp_path / "out" / "nested" / "b.bin").read_bytes() == b"B"

    def test_execute_send_folder(self, monkeypatch, tmp_path) -> None:
        fs = FakeSFTP()
        self._make_factory(monkeypatch, shared=fs)
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"A")

        manager = TransferManager()
        task = manager.queue_transfer(
            str(src), "remote/dst", self._device(),
            direction=TransferDirection.SEND,
        )
        manager.execute_queue()

        assert task.status == "completed"
        assert fs.tree["/remote/dst/a.txt"] == b"A"

    def test_execute_connection_failure_fails_all_tasks(self, monkeypatch, tmp_path):
        self._make_factory(
            monkeypatch,
            connect_error=TransferError("Connection failed: boom"),
        )
        manager = TransferManager()
        t1 = manager.queue_transfer(
            str(tmp_path / "x.txt"), "r1", self._device(),
        )
        t2 = manager.queue_transfer(
            str(tmp_path / "y.txt"), "r2", self._device(),
        )
        manager.execute_queue()

        assert t1.status == "failed"
        assert "Connection failed" in (t1.error or "")
        assert t2.status == "failed"

    def test_execute_transfer_error_fails_task_only(self, monkeypatch, tmp_path):
        fs = FakeSFTP()
        self._make_factory(monkeypatch, shared=fs)
        manager = TransferManager()

        good = tmp_path / "good.txt"
        good.write_bytes(b"ok")
        t_ok = manager.queue_transfer(
            str(good), "r/good.txt", self._device(),
        )
        t_bad = manager.queue_transfer(
            str(tmp_path / "missing.txt"), "r/missing.txt", self._device(),
        )

        manager.execute_queue()

        assert t_ok.status == "completed"
        assert t_bad.status == "failed"
        assert "Source file not found" in (t_bad.error or "")

    def test_execute_picks_up_tasks_queued_during_run(self, monkeypatch, tmp_path):
        fs = FakeSFTP()
        self._make_factory(monkeypatch, shared=fs)
        first = tmp_path / "first.txt"
        first.write_bytes(b"1")

        manager = TransferManager()
        queued_during_run: list = []

        def progress_callback(task) -> None:
            if not queued_during_run:
                queued_during_run.append(
                    manager.queue_transfer(
                        str(first), "r/second.txt", self._device(),
                    )
                )

        manager.set_progress_callback(progress_callback)
        task = manager.queue_transfer(
            str(first), "r/first.txt", self._device(),
        )

        manager.execute_queue()

        assert task.status == "completed"
        assert queued_during_run[0].status == "completed"
        assert len(manager.get_pending_tasks()) == 0

    def test_disconnect_called_after_batch(self, monkeypatch, tmp_path) -> None:
        fs = FakeSFTP()
        created = self._make_factory(monkeypatch, shared=fs)
        local = tmp_path / "a.txt"
        local.write_bytes(b"x")

        manager = TransferManager()
        manager.queue_transfer(str(local), "r/a.txt", self._device())
        manager.execute_queue()

        assert created[0]._sftp_client is None  # disconnect() ran

    def test_cancelled_pending_task_is_skipped(self, monkeypatch, tmp_path) -> None:
        fs = FakeSFTP()
        self._make_factory(monkeypatch, shared=fs)
        manager = TransferManager()
        f1 = tmp_path / "one.txt"
        f1.write_bytes(b"1")
        f2 = tmp_path / "two.txt"
        f2.write_bytes(b"2")
        t1 = manager.queue_transfer(str(f1), "r/one.txt", self._device())
        t2 = manager.queue_transfer(str(f2), "r/two.txt", self._device())

        manager.cancel_task(t1)

        manager.execute_queue()

        assert t1.status == "cancelled"
        assert t2.status == "completed"
        assert "/r/one.txt" not in fs.tree
        assert fs.tree.get("/r/two.txt") == b"2"

    def test_cancel_during_transfer_marks_cancelled_and_queue_continues(
        self, monkeypatch, tmp_path
    ) -> None:
        fs = FakeSFTP()
        self._make_factory(monkeypatch, shared=fs)
        manager = TransferManager()
        f1 = tmp_path / "one.bin"
        f1.write_bytes(b"1" * (TRANSFER_CHUNK_SIZE * 2))
        f2 = tmp_path / "two.txt"
        f2.write_bytes(b"2")
        t1 = manager.queue_transfer(str(f1), "r/one.bin", self._device())
        t2 = manager.queue_transfer(str(f2), "r/two.txt", self._device())

        def progress_callback(task) -> None:
            if task is t1 and not t1.cancel_event.is_set():
                manager.cancel_task(t1)

        manager.set_progress_callback(progress_callback)

        manager.execute_queue()

        assert t1.status == "cancelled"
        assert t2.status == "completed"
        assert "/r/one.bin" not in fs.tree
        assert fs.tree.get("/r/two.txt") == b"2"
        assert t1 not in manager.get_all_tasks()

    def test_cancel_task_removes_task_from_manager(
        self, monkeypatch, tmp_path
    ) -> None:
        self._make_factory(monkeypatch)
        manager = TransferManager()
        f1 = tmp_path / "one.txt"
        f1.write_bytes(b"1")
        t1 = manager.queue_transfer(str(f1), "r/one.txt", self._device())

        manager.cancel_task(t1)

        assert t1.status == "cancelled"
        assert t1.cancel_event.is_set()
        assert t1 not in manager.get_all_tasks()

    def test_clear_completed_removes_cancelled_tasks(
        self, monkeypatch, tmp_path
    ) -> None:
        self._make_factory(monkeypatch)
        manager = TransferManager()
        f1 = tmp_path / "one.txt"
        f1.write_bytes(b"1")
        t1 = manager.queue_transfer(str(f1), "r/one.txt", self._device())

        manager.cancel_task(t1)
        manager.clear_completed()

        assert manager.get_all_tasks() == []
