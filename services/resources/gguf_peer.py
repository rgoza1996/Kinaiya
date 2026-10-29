"""Observer-only native identity and nested-process release evidence.

Only WorkerManager signals processes. These separately retained pidfds prove
that the originally observed namespace init and native peer were extinguished;
an empty outer process group alone does not establish that fact.
"""

import asyncio
import fcntl
import hashlib
import os
import socket
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path

from packages.resources.processes import _adopt_member, _pidfd_pid, _proc_stat_full

MAX_OBSERVERS = 16
MAX_ANCESTORS = 8
MAX_EXECUTABLE_BYTES = 64 * 1024**2
_SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL


def _need(value, message):
    if not value:
        raise ValueError(message)


def _namespaces(pid):
    nspid = None
    # No environment, command line, credentials, serials or diagnostic tails.
    with open(f"/proc/{pid}/status", "rb") as source:
        raw = source.read(64 * 1024 + 1)
    _need(len(raw) <= 64 * 1024, "Native status exceeds its bound")
    for line in raw.splitlines():
        if line.startswith(b"NSpid:"):
            nspid = tuple(int(item) for item in line.split()[1:])
    _need(nspid and nspid[0] == pid, "Native namespace PID is unavailable")
    spaces = tuple(os.readlink(f"/proc/{pid}/ns/{kind}") for kind in ("pid", "net"))
    return nspid, spaces


def _sealed_executable(pid, pin):
    """Open the observed process's executable, not a mutable owner pathname."""
    _need(
        type(pin["bytes"]) is int and 0 < pin["bytes"] <= MAX_EXECUTABLE_BYTES,
        "Native executable exceeds its verification bound",
    )
    # /proc/PID/exe is intentionally a kernel magic link. Caller brackets this
    # open/hash with a retained pidfd and stable kernel start/ancestry checks.
    fd = os.open(f"/proc/{pid}/exe", os.O_RDONLY | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        _need(
            stat.S_ISREG(before.st_mode) and before.st_size == pin["bytes"],
            "Native executable size/type changed",
        )
        _need(
            fcntl.fcntl(fd, fcntl.F_GET_SEALS) & _SEALS == _SEALS,
            "Native executable is not an immutable sealed input",
        )
        digest, count = hashlib.sha256(), 0
        while chunk := os.read(fd, 1024 * 1024):
            count += len(chunk)
            _need(count <= pin["bytes"], "Native executable changed while hashing")
            digest.update(chunk)
        _need(
            count == pin["bytes"] and digest.hexdigest() == pin["sha256"],
            "Native executable digest changed",
        )
        after = os.fstat(fd)
        _need(
            (before.st_dev, before.st_ino, before.st_size)
            == (after.st_dev, after.st_ino, after.st_size),
            "Native executable changed",
        )
        return before.st_dev, before.st_ino
    finally:
        os.close(fd)


@dataclass(eq=False)
class NativeLease:
    outer_pid: int
    start_time: int
    executable_pin: dict
    socket_path: Path
    handles: dict = field(default_factory=dict)
    observations: dict = field(default_factory=dict)
    native_pid: int | None = None
    init_pid: int | None = None
    executable_inode: tuple | None = None
    socket_inode: tuple | None = None
    identity: object = None
    released: bool = False

    def leader_current(self):
        before = _proc_stat_full(self.outer_pid)
        return bool(
            before is not None
            and before[0] not in {"Z", "X", "x"}
            and before[2] == before[3] == self.outer_pid
            and before[4] == self.start_time
            and (
                self.outer_pid not in self.handles
                or _pidfd_pid(self.handles[self.outer_pid]) == self.outer_pid
            )
        )

    def observe(self, pid):
        _need(not self.released and self.leader_current(), "Native outer owner changed")
        chain = []
        for _ in range(MAX_ANCESTORS):
            before = _proc_stat_full(pid)
            _need(
                before is not None
                and before[0] not in {"Z", "X", "x"}
                and before[2] == before[3] == self.outer_pid,
                "Native ancestry is outside its owned group/session",
            )
            if pid not in self.handles:
                _need(len(self.handles) < MAX_OBSERVERS, "Native observer bound exceeded")
                fd = _adopt_member(self.outer_pid, pid)
                _need(fd is not None, "Native observer identity is unavailable")
                # Retain even if a later check fails. Unknown is not extinction.
                self.handles[pid] = fd
            _need(_pidfd_pid(self.handles[pid]) == pid, "Native observer binding changed")
            nspid, spaces = _namespaces(pid)
            after = _proc_stat_full(pid)
            _need(
                after is not None
                and before[1:] == after[1:]
                and after[0] not in {"Z", "X", "x"}
                and _pidfd_pid(self.handles[pid]) == pid,
                "Native ancestry changed during observation",
            )
            previous = self.observations.get(pid)
            observation = (after[4], nspid, spaces)
            _need(
                previous is None
                or previous == observation
                or (
                    pid == self.outer_pid
                    and self.native_pid is None
                    and previous[0] == observation[0]
                ),
                "Native namespace identity changed",
            )
            self.observations[pid] = observation
            chain.append(pid)
            if len(nspid) > 1 and nspid[-1] == 1:
                _need(self.init_pid in {None, pid}, "Native namespace init changed")
                self.init_pid = pid
            if pid == self.outer_pid:
                _need(self.leader_current(), "Native outer owner changed during observation")
                return chain
            pid = before[1]
        raise ValueError("Native ancestry exceeds its depth bound")

    def check_socket(self):
        row = self.socket_path.lstat()
        _need(
            stat.S_ISSOCK(row.st_mode) and row.st_uid == os.geteuid() and not row.st_mode & 0o077,
            "Native socket is not private",
        )
        inode = row.st_dev, row.st_ino
        _need(self.socket_inode in {None, inode}, "Native socket incarnation changed")
        return inode

    def _native_executable(self, pid, *, socket_inode=None):
        """Check a process without treating it as an HTTP peer or ready worker."""
        _need(self.native_pid in {None, pid}, "Native peer incarnation changed")
        _need(pid != self.outer_pid, "Native process is the outer owner")
        chain = self.observe(pid)
        _need(
            self.init_pid is not None and self.init_pid != pid and self.init_pid in chain,
            "Native peer has no observed namespace init",
        )
        peer_spaces = self.observations[pid][2]
        _need(peer_spaces == self.observations[self.init_pid][2], "Native/init namespaces differ")
        host_spaces = tuple(os.readlink(f"/proc/self/ns/{kind}") for kind in ("pid", "net"))
        _need(
            all(native != host for native, host in zip(peer_spaces, host_spaces, strict=True)),
            "Native peer is not isolated from host PID/network namespaces",
        )
        row = os.stat(f"/proc/{pid}/exe")
        _need(
            type(self.executable_pin["bytes"]) is int
            and 0 < self.executable_pin["bytes"] <= MAX_EXECUTABLE_BYTES
            and stat.S_ISREG(row.st_mode)
            and row.st_size == self.executable_pin["bytes"],
            "Native executable size/type changed",
        )
        if self.executable_inode is None:
            inode = _sealed_executable(pid, self.executable_pin)
        else:
            inode = row.st_dev, row.st_ino
            _need(inode == self.executable_inode, "Native executable incarnation changed")
        # Recheck exact process and ancestry after hashing before binding it.
        _need(
            self.observe(pid) == chain
            and (socket_inode is None or self.check_socket() == socket_inode),
            "Native process changed during verification",
        )
        current_exe = os.stat(f"/proc/{pid}/exe")
        _need(
            (current_exe.st_dev, current_exe.st_ino) == inode,
            "Native executable changed during verification",
        )
        return inode

    def observe_native(self, pid):
        """Retain a sealed native/init identity even before its listener exists.

        This observer grants no HTTP readiness or socket-unlink authority.
        """
        inode = self._native_executable(pid)
        self.native_pid, self.executable_inode = pid, inode

    def verify_peer(self, peer):
        before_inode = self.check_socket()
        pid, uid, _ = struct.unpack(
            "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        _need(uid == os.geteuid(), "Native peer UID changed")
        inode = self._native_executable(pid, socket_inode=before_inode)
        self.native_pid, self.executable_inode, self.socket_inode = pid, inode, before_inode

    def bind(self, identity):
        _need(
            identity.worker_id == "gguf"
            and identity.pid == self.outer_pid
            and identity.start_time == self.start_time
            and self.native_pid is not None
            and self.init_pid is not None
            and self.socket_inode is not None
            and self.identity in {None, identity},
            "Native ready worker identity differs",
        )
        self.identity = identity
        self.current(identity)
        return self

    def current(self, identity):
        _need(
            not self.released and self.identity is identity and self.leader_current(),
            "Native lease is not current",
        )
        _need(self.socket_inode == self.check_socket(), "Native ready socket changed")
        for pid in (self.outer_pid, self.native_pid, self.init_pid):
            _need(
                pid in self.handles and _pidfd_pid(self.handles[pid]) == pid,
                "Native ready process identity changed",
            )

    async def extinct(self, *, timeout_s=5):
        _need(
            self.native_pid is not None and self.init_pid is not None,
            "Native/init identities were never established",
        )
        end = asyncio.get_running_loop().time() + timeout_s
        while True:
            if self.handles and all(_pidfd_pid(fd) == -1 for fd in self.handles.values()):
                return True
            if asyncio.get_running_loop().time() >= end:
                return False
            await asyncio.sleep(0.01)

    def forget(self):
        _need(
            self.handles and all(_pidfd_pid(fd) == -1 for fd in self.handles.values()),
            "Cannot forget an unconfirmed native process",
        )
        try:
            inode = self.check_socket()
        except FileNotFoundError:
            pass
        else:
            _need(
                self.socket_inode is not None and inode == self.socket_inode,
                "Cannot remove an unbound native socket",
            )
            self.socket_path.unlink()
        for fd in self.handles.values():
            os.close(fd)
        self.handles.clear()
        self.released = True
