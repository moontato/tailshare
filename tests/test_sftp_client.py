"""SFTPClient and TransferManager tests against an in-memory SFTP filesystem."""

import posixpath
import types

import paramiko
import pytest

import tailshare.config as config_module
import tailshare.transfer as transfer_module
from tailshare.devices import Device
from tailshare.transfer import (
    SFTPClient,
    TransferDirection,
    TransferError,
    TransferManager,
)

DIR_MODE = 0o40755
FILE_MODE = 0o100644


class FakeSFTP:
    """In-memory stand-in for paramiko.SFTPClient."""

    def __init__(self) -> None:
        # path -> bytes for files, None for directories
        self.tree: dict[str, bytes | None] = {"/": None}
        self.cwd = "/"
        self.stat_calls: list[str] = []
        self.mkdir_calls: list[str] = []
        self.closed = False

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

    def put(self, localpath: str, remotepath: str, callback=None) -> None:
        with open(localpath, "rb") as f:
            data = f.read()
        self.tree[self._norm(remotepath)] = data
        if callback:
            callback(len(data), len(data))

    def get(self, remotepath: str, localpath: str, callback=None) -> None:
        data = self._require(remotepath)
        assert data is not None
        with open(localpath, "wb") as f:
            f.write(data)
        if callback:
            callback(len(data), len(data))

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
