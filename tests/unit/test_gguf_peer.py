"""Synthetic kernel observations; no native executables or device handles are opened."""

import fcntl
import hashlib
import os
import stat
import struct
from types import SimpleNamespace

import pytest

from packages.resources.processes import WorkerIdentity
from services.resources import gguf_peer as peer

_HASH_EXECUTABLE = peer._sealed_executable


class OSView:
    """Patch this module's OS view without changing pytest's real OS module."""

    def __init__(self, **overrides):
        self.overrides = overrides

    def __getattr__(self, name):
        return self.overrides.get(name, getattr(os, name))


class SocketPath:
    def __init__(self):
        self.row = SimpleNamespace(
            st_mode=stat.S_IFSOCK | 0o600, st_uid=os.geteuid(), st_dev=7, st_ino=8
        )
        self.removed = False

    def lstat(self):
        if self.removed:
            raise FileNotFoundError
        return self.row

    def unlink(self):
        self.removed = True


@pytest.fixture
def kernel(monkeypatch):
    """Host PIDs 100/101/102 represent outer/init/native in one owned group."""
    rows = {
        100: ("S", 1, 100, 100, 10),
        101: ("S", 100, 100, 100, 11),
        102: ("S", 101, 100, 100, 12),
    }
    spaces = {
        100: ((100,), ("pid:host", "net:host")),
        101: ((101, 1), ("pid:child", "net:child")),
        102: ((102, 2), ("pid:child", "net:child")),
    }
    handles, closed = {}, []
    executable = [SimpleNamespace(st_dev=20, st_ino=21, st_mode=stat.S_IFREG | 0o500, st_size=12)]
    path = SocketPath()
    lease = peer.NativeLease(100, 10, {"bytes": 12, "sha256": "a" * 64}, path)

    def adopt(outer, pid):
        assert outer == 100
        fd = pid + 1000
        handles[fd] = pid
        return fd

    monkeypatch.setattr(peer, "_proc_stat_full", rows.get)
    monkeypatch.setattr(peer, "_adopt_member", adopt)
    monkeypatch.setattr(peer, "_pidfd_pid", handles.get)
    monkeypatch.setattr(peer, "_namespaces", spaces.__getitem__)
    monkeypatch.setattr(peer, "_sealed_executable", lambda pid, pin: (20, 21))
    monkeypatch.setattr(
        peer,
        "os",
        OSView(
            readlink=lambda path: "pid:host" if path.endswith("/pid") else "net:host",
            stat=lambda path: executable[0],
            close=closed.append,
        ),
    )
    native = SimpleNamespace(getsockopt=lambda *args: struct.pack("3i", 102, os.geteuid(), 0))
    return SimpleNamespace(
        lease=lease,
        rows=rows,
        spaces=spaces,
        handles=handles,
        closed=closed,
        executable=executable,
        path=path,
        native=native,
    )


def test_peer_verification_binds_observed_chain_and_exact_issued_identity(kernel):
    lease = kernel.lease
    lease.verify_peer(kernel.native)
    assert (lease.native_pid, lease.init_pid, lease.socket_inode) == (102, 101, (7, 8))
    assert set(lease.handles) == {100, 101, 102}
    issued = WorkerIdentity("gguf", 100, 10)
    assert lease.bind(issued) is lease
    lease.current(issued)
    with pytest.raises(ValueError, match="not current"):
        lease.current(WorkerIdentity("gguf", 100, 10))
    with pytest.raises(ValueError, match="ready worker identity"):
        lease.bind(WorkerIdentity("gguf", 100, 10))


def test_pre_listener_observation_does_not_grant_socket_or_worker_readiness(kernel):
    lease = kernel.lease
    kernel.path.removed = True
    lease.observe_native(102)
    assert (lease.native_pid, lease.init_pid, lease.executable_inode) == (102, 101, (20, 21))
    assert set(lease.handles) == {100, 101, 102}
    assert lease.socket_inode is None and lease.identity is None
    issued = WorkerIdentity("gguf", 100, 10)
    with pytest.raises(ValueError, match="ready worker identity"):
        lease.bind(issued)
    assert lease.identity is None
    # Listener appearance alone still grants no readiness; SO_PEERCRED must pass.
    kernel.path.removed = False
    with pytest.raises(ValueError, match="ready worker identity"):
        lease.bind(issued)
    kernel.native.getsockopt = lambda *args: struct.pack("3i", 102, os.geteuid() + 1, 0)
    with pytest.raises(ValueError, match="UID"):
        lease.verify_peer(kernel.native)
    assert lease.socket_inode is None and lease.identity is None
    kernel.native.getsockopt = lambda *args: struct.pack("3i", 102, os.geteuid(), 0)
    lease.verify_peer(kernel.native)
    assert lease.bind(issued) is lease


@pytest.mark.parametrize("pid", [100, 101])
def test_pre_listener_observation_refuses_outer_or_namespace_init(kernel, monkeypatch, pid):
    monkeypatch.setattr(
        peer, "_sealed_executable", lambda *args: pytest.fail("must not hash outer/init")
    )
    with pytest.raises(ValueError):
        kernel.lease.observe_native(pid)
    assert kernel.lease.native_pid is None and kernel.lease.executable_inode is None


@pytest.mark.parametrize("change", ["size", "type", "zero_bound", "bool_bound", "excessive_bound"])
def test_pre_listener_observation_cheap_metadata_rejects_before_hash(kernel, monkeypatch, change):
    monkeypatch.setattr(
        peer, "_sealed_executable", lambda *args: pytest.fail("must reject before hashing")
    )
    if change == "size":
        kernel.executable[0].st_size += 1
    elif change == "type":
        kernel.executable[0].st_mode = stat.S_IFDIR | 0o500
    else:
        kernel.lease.executable_pin["bytes"] = {
            "zero_bound": 0,
            "bool_bound": True,
            "excessive_bound": peer.MAX_EXECUTABLE_BYTES + 1,
        }[change]
    with pytest.raises(ValueError, match="size/type"):
        kernel.lease.observe_native(102)
    assert kernel.lease.native_pid is None and kernel.lease.handles


@pytest.mark.parametrize("valid_digest", [False, True])
def test_pre_listener_observation_requires_actual_sealed_memfd_hash(
    kernel, monkeypatch, valid_digest
):
    raw = b"public synthetic native loader identity"
    fd = os.memfd_create("synthetic-pre-listener", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(fd, raw)
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, peer._SEALS)
        kernel.executable[0] = os.fstat(fd)
        kernel.lease.executable_pin = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest() if valid_digest else "0" * 64,
        }
        monkeypatch.setattr(peer, "_sealed_executable", _HASH_EXECUTABLE)
        monkeypatch.setattr(
            peer,
            "os",
            OSView(
                open=lambda path, flags: os.dup(fd),
                stat=lambda path: kernel.executable[0],
                readlink=lambda path: "pid:host" if path.endswith("/pid") else "net:host",
            ),
        )
        if valid_digest:
            kernel.lease.observe_native(102)
            assert kernel.lease.executable_inode == (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            assert kernel.lease.native_pid == 102 and kernel.lease.socket_inode is None
        else:
            with pytest.raises(ValueError, match="digest"):
                kernel.lease.observe_native(102)
            assert kernel.lease.native_pid is None and kernel.lease.executable_inode is None
            assert kernel.lease.handles and not kernel.closed
    finally:
        os.close(fd)


def test_pre_listener_post_hash_exec_race_cannot_bind_process(kernel, monkeypatch):
    def hash_original(pid, pin):
        kernel.executable[0].st_ino = 99
        return 20, 21

    monkeypatch.setattr(peer, "_sealed_executable", hash_original)
    with pytest.raises(ValueError, match="executable changed during verification"):
        kernel.lease.observe_native(102)
    assert kernel.lease.native_pid is None and kernel.lease.executable_inode is None


@pytest.mark.parametrize("socket_absent", [True, False])
async def test_failed_loader_extinction_releases_only_absent_or_bound_socket(kernel, socket_absent):
    lease = kernel.lease
    lease.observe_native(102)
    kernel.path.removed = socket_absent
    for fd in kernel.handles:
        kernel.handles[fd] = -1
    assert await lease.extinct(timeout_s=0)
    if socket_absent:
        lease.forget()
        assert lease.released and not lease.handles and set(kernel.closed) == {1100, 1101, 1102}
    else:
        with pytest.raises(ValueError, match="unbound native socket"):
            lease.forget()
        assert not lease.released and lease.handles and not kernel.closed
        assert not kernel.path.removed


async def test_unverified_loading_process_stays_unknown_even_after_observed_handles_die(kernel):
    kernel.lease.observe(102)
    for fd in kernel.handles:
        kernel.handles[fd] = -1
    with pytest.raises(ValueError, match="never established"):
        await kernel.lease.extinct(timeout_s=0)
    assert kernel.lease.native_pid is None and kernel.lease.handles


@pytest.mark.parametrize(
    "attack",
    [
        "uid",
        "socket_owner",
        "socket_mode",
        "socket_regular",
        "host_pid",
        "host_net",
        "init_namespace",
        "no_init",
        "pgid",
        "sid",
    ],
)
def test_foreign_peer_socket_or_namespace_never_becomes_ready(kernel, attack):
    if attack == "uid":
        kernel.native.getsockopt = lambda *args: struct.pack("3i", 102, os.geteuid() + 1, 0)
    elif attack == "socket_owner":
        kernel.path.row.st_uid += 1
    elif attack == "socket_mode":
        kernel.path.row.st_mode |= 0o020
    elif attack == "socket_regular":
        kernel.path.row.st_mode = stat.S_IFREG | 0o600
    elif attack in {"host_pid", "host_net"}:
        value = ("pid:host", "net:child") if attack == "host_pid" else ("pid:child", "net:host")
        kernel.spaces[101] = ((101, 1), value)
        kernel.spaces[102] = ((102, 2), value)
    elif attack == "init_namespace":
        kernel.spaces[101] = ((101, 1), ("pid:other", "net:other"))
    elif attack == "no_init":
        kernel.spaces[101] = ((101, 9), ("pid:child", "net:child"))
    else:
        row = list(kernel.rows[102])
        row[2 if attack == "pgid" else 3] = 999
        kernel.rows[102] = tuple(row)
    with pytest.raises(ValueError):
        kernel.lease.verify_peer(kernel.native)
    assert kernel.lease.native_pid is None


def test_first_peer_exec_change_during_hash_is_rejected(kernel, monkeypatch):
    def hash_original(pid, pin):
        kernel.executable[0].st_ino = 99
        return 20, 21

    monkeypatch.setattr(peer, "_sealed_executable", hash_original)
    with pytest.raises(ValueError, match="executable changed during verification"):
        kernel.lease.verify_peer(kernel.native)
    assert kernel.lease.native_pid is None
    assert kernel.lease.handles  # Retain the original observers on refusal.


def test_socket_replaced_during_first_peer_hash_is_rejected(kernel, monkeypatch):
    def hash_original(pid, pin):
        kernel.path.row.st_ino += 1
        return 20, 21

    monkeypatch.setattr(peer, "_sealed_executable", hash_original)
    with pytest.raises(ValueError, match="changed during verification"):
        kernel.lease.verify_peer(kernel.native)
    assert kernel.lease.native_pid is None and kernel.lease.handles


@pytest.mark.parametrize("change", ["peer_pid", "namespace", "executable"])
def test_later_connection_cannot_rebind_native_incarnation(kernel, change):
    kernel.lease.verify_peer(kernel.native)
    if change == "peer_pid":
        kernel.native.getsockopt = lambda *args: struct.pack("3i", 103, os.geteuid(), 0)
    elif change == "namespace":
        kernel.spaces[102] = ((102, 2), ("pid:replacement", "net:replacement"))
    else:
        kernel.executable[0].st_ino = 99
    with pytest.raises(ValueError):
        kernel.lease.verify_peer(kernel.native)
    assert (kernel.lease.native_pid, kernel.lease.executable_inode) == (102, (20, 21))


def test_pid_start_change_during_observation_is_rejected_and_retained(kernel, monkeypatch):
    def changed(pid):
        if pid == 102:
            kernel.rows[pid] = ("S", 101, 100, 100, 999)
        return kernel.spaces[pid]

    monkeypatch.setattr(peer, "_namespaces", changed)
    with pytest.raises(ValueError, match="ancestry changed"):
        kernel.lease.verify_peer(kernel.native)
    assert kernel.lease.handles == {102: 1102}
    assert not kernel.closed


def test_scheduler_state_change_is_not_pid_reuse(kernel, monkeypatch):
    def scheduling(pid):
        row = kernel.rows[pid]
        kernel.rows[pid] = ("R" if row[0] == "S" else "S", *row[1:])
        return kernel.spaces[pid]

    monkeypatch.setattr(peer, "_namespaces", scheduling)
    kernel.lease.verify_peer(kernel.native)
    assert kernel.lease.native_pid == 102


@pytest.mark.parametrize("change", ["socket", "native_pidfd", "init_pidfd", "outer_start"])
def test_current_rechecks_every_retained_identity(kernel, change):
    lease = kernel.lease
    lease.verify_peer(kernel.native)
    issued = WorkerIdentity("gguf", 100, 10)
    lease.bind(issued)
    if change == "socket":
        kernel.path.row.st_ino += 1
    elif change == "outer_start":
        kernel.rows[100] = ("S", 1, 100, 100, 99)
    else:
        kernel.handles[1102 if change == "native_pidfd" else 1101] = -1
    with pytest.raises(ValueError):
        lease.current(issued)


@pytest.mark.parametrize("held", [100, 101, 102, None])
async def test_extinction_and_forget_require_every_original_pidfd_minus_one(kernel, held):
    lease = kernel.lease
    lease.verify_peer(kernel.native)
    for fd in kernel.handles:
        kernel.handles[fd] = -1
    if held is not None:
        kernel.handles[held + 1000] = None if held == 100 else held
        assert not await lease.extinct(timeout_s=0)
        with pytest.raises(ValueError, match="unconfirmed"):
            lease.forget()
        assert not kernel.path.removed and not kernel.closed and lease.handles
    else:
        assert await lease.extinct(timeout_s=0)
        lease.forget()
        assert lease.released and not lease.handles and kernel.path.removed
        assert set(kernel.closed) == {1100, 1101, 1102}


def test_extinct_original_processes_do_not_authorize_unlinking_replacement_socket(kernel):
    lease = kernel.lease
    lease.verify_peer(kernel.native)
    for fd in kernel.handles:
        kernel.handles[fd] = -1
    kernel.path.row.st_ino += 1
    with pytest.raises(ValueError, match="socket incarnation"):
        lease.forget()
    assert not kernel.path.removed and not kernel.closed and lease.handles


@pytest.mark.parametrize(
    "omitted", [0, fcntl.F_SEAL_WRITE, fcntl.F_SEAL_GROW, fcntl.F_SEAL_SHRINK, fcntl.F_SEAL_SEAL]
)
def test_executable_hash_requires_real_complete_memfd_seals(monkeypatch, omitted):
    data = b"synthetic public executable identity\x00" * 7
    fd = os.memfd_create("synthetic-gguf-peer-test", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, peer._SEALS & ~omitted)
        monkeypatch.setattr(peer, "os", OSView(open=lambda path, flags: os.dup(fd)))
        pin = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if omitted:
            with pytest.raises(ValueError, match="immutable sealed"):
                peer._sealed_executable(102, pin)
        else:
            row = os.fstat(fd)
            assert peer._sealed_executable(102, pin) == (row.st_dev, row.st_ino)
            os.lseek(fd, 0, os.SEEK_SET)
            with pytest.raises(ValueError, match="digest"):
                peer._sealed_executable(102, {**pin, "sha256": "0" * 64})
    finally:
        os.close(fd)
