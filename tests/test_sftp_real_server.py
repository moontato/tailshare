"""Integration tests: the real SFTPClient against a real paramiko SFTP server.

These drive the full SFTP protocol over a live socket (no fakes) so that
every wrapper call is proven to exist and behave against an actual
paramiko client/server pair - the class of bug a fake cannot catch
(e.g. calling a paramiko method that does not exist).
"""

import errno
import os
import socket
import threading
import time

import paramiko
import pytest

import tailshare.config as config_module
from tailshare.devices import Device
from tailshare.transfer import SFTPClient
from tailshare.tui import RemoteDestinationBrowser, remote_parent


class _AuthServer(paramiko.ServerInterface):
    """Accepts any password; opens only session channels."""

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _HomeSFTP(paramiko.SFTPServerInterface):
    """Serves a temp home directory; optionally as a chroot-style jail.

    In jail mode the session's '/' is the home root (like a
    ChrootDirectory SFTP account), and REALPATH never reveals a path
    above it.
    """

    def __init__(self, server, home: str, jailed: bool = False) -> None:
        super().__init__(server)
        self._home = home
        self._jailed = jailed

    def _resolve(self, path: str) -> str:
        if not path.startswith("/"):
            path = os.path.join(self._home, path)
        return os.path.normpath(path)

    def canonicalize(self, path: str) -> str:
        resolved = self._resolve(path)
        if not self._jailed:
            return resolved
        rel = os.path.relpath(resolved, self._home)
        if rel in ("", ".") or rel == ".." or rel.startswith(".." + os.sep):
            return "/"
        return "/" + rel.replace(os.sep, "/")

    def stat(self, path: str):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._resolve(path)))
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EPERM):
                return paramiko.SFTP_PERMISSION_DENIED
            return paramiko.SFTP_NO_SUCH_FILE

    def list_folder(self, path: str):
        resolved = self._resolve(path)
        try:
            names = sorted(os.listdir(resolved))
        except OSError:
            return paramiko.SFTP_NO_SUCH_FILE
        data = []
        for name in names:
            try:
                st = os.lstat(os.path.join(resolved, name))
            except OSError:
                continue
            attr = paramiko.SFTPAttributes.from_stat(st)
            attr.filename = name
            data.append(attr)
        return data


def _host_key():
    """One RSA host key for the whole test session (slow to generate)."""
    key = getattr(_host_key, "key", None)
    if key is None:
        key = paramiko.RSAKey.generate(2048)
        _host_key.key = key
    return key


def _start_sftp_server(home: str, jailed: bool = False) -> tuple[int, threading.Thread]:
    """Run a real paramiko SFTP server on localhost; return (port, thread)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    ready = threading.Event()

    def serve() -> None:
        ready.set()  # listener is bound and about to accept
        conn, _ = listener.accept()
        try:
            transport = paramiko.Transport(conn)
            transport.add_server_key(_host_key())
            transport.set_subsystem_handler(
                "sftp",
                paramiko.SFTPServer,
                _HomeSFTP,
                home=home,
                jailed=jailed,
            )
            transport.start_server(server=_AuthServer())
            ready.set()
            while transport.is_active():
                time.sleep(0.05)
            transport.close()
        except Exception:
            ready.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(10), "SFTP server did not start"
    return port, thread


def _connect(port: int) -> paramiko.SFTPClient:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    transport = paramiko.Transport(sock)
    transport.start_client()
    transport.auth_password("u", "p")
    return paramiko.SFTPClient.from_transport(transport)


def make_device() -> Device:
    return Device(
        name="real-pc",
        hostname="real-pc.tailnet.ts.net",
        ip="100.64.0.40",
        online=True,
        last_seen="1m ago",
        machine_id="m-real",
    )


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "_config", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


def _build_home(tmp_path) -> str:
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    (home / "report.txt").write_text("hi")
    return str(home)


def test_canonicalize_against_real_server(tmp_path) -> None:
    """'~' resolves to the real home over a live SFTP connection."""
    home = _build_home(tmp_path)
    port, _ = _start_sftp_server(home, jailed=False)
    sftp = _connect(port)
    try:
        client = SFTPClient(make_device())
        client._sftp_client = sftp

        assert client.canonicalize("~") == home

        # The browser's home listing works end to end, and the parent of
        # home resolves (so '..' from home can climb up).
        path, entries, note = RemoteDestinationBrowser._find_listing(client, "~")
        assert path == "~"
        assert ("docs", True) in entries
        assert ("report.txt", False) in entries
        assert note == ""
        assert remote_parent(sftp.normalize(".")) == os.path.dirname(home)
    finally:
        sftp.close()


def test_jailed_real_server_home_is_root(tmp_path) -> None:
    """A jailed SFTP session reports '/' as home (no parent above)."""
    home = _build_home(tmp_path)
    port, _ = _start_sftp_server(home, jailed=True)
    sftp = _connect(port)
    try:
        client = SFTPClient(make_device())
        client._sftp_client = sftp

        assert client.canonicalize("~") == "/"
        assert client.canonicalize("docs") == "/docs"

        # Home still lists fine; there is just nothing above it.
        path, entries, note = RemoteDestinationBrowser._find_listing(client, "~")
        assert path == "~"
        assert ("docs", True) in entries
        assert note == ""
        assert remote_parent(client.canonicalize("~")) is None
    finally:
        sftp.close()


def test_real_server_missing_path_probe(tmp_path) -> None:
    """probe_remote distinguishes missing from present over the wire."""
    home = _build_home(tmp_path)
    port, _ = _start_sftp_server(home, jailed=False)
    sftp = _connect(port)
    try:
        client = SFTPClient(make_device())
        client._sftp_client = sftp
        assert client.probe_remote("docs") == "dir"
        assert client.probe_remote("report.txt") == "file"
        assert client.probe_remote("nope") == "missing"
    finally:
        sftp.close()
