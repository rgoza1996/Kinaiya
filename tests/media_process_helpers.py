"""Shared original-process fixtures for unit and integration media oracles.

This module supplies test-owned process/FD custody only; it grants no native
Session authority, Segment budget, qualified runtime or public cleanup proof.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from packages.contracts import media
from packages.resources.media_processes import MediaLaunchBinding, MediaWorkerPool
from packages.resources.processes import WorkerSpec
from services.session.media_spool import MediaSpool

_SOURCE = b"synthetic media launch source\0" + bytes(range(32))

_WAIT = 10

_PROGRAM = r"""
import errno, fcntl, json, os, socket, sys
marker = sys.argv[1]
with open(marker, 'ab') as stream:
    stream.write(b'original executed once')
channel = socket.socket(fileno=3)
position = os.lseek(4, 0, os.SEEK_CUR)
flags = fcntl.fcntl(4, fcntl.F_GETFL)
write_errno = None
try:
    os.write(4, b'forbidden')
except OSError as error:
    write_errno = error.errno
report = {'pid': os.getpid(), 'position': position,
          'readonly': flags & os.O_ACCMODE == os.O_RDONLY,
          'write_errno': write_errno, 'source': os.read(4, 1024).hex()}
channel.sendall(json.dumps(report, sort_keys=True).encode() + b'\n')
pending = b''
while True:
    part = channel.recv(65536)
    if not part:
        break
    pending += part
    while b'\n' in pending:
        line, pending = pending.split(b'\n', 1)
        if line == b'EXIT':
            channel.sendall(b'BYE\n')
            os.close(4)
            channel.close()
            raise SystemExit(0)
        channel.sendall(b'PONG\n' if line == b'PING' else line + b'\n')
os.close(4)
channel.close()
"""


def _limits():
    return media.MediaLimits(
        v=1,
        source=media.MediaSourceLimits(
            file_bytes=8388608,
            file_duration_ms=21600000,
            upload_chunk_bytes=4194304,
            owner_spool_bytes=16777216,
            global_spool_bytes=33554432,
            unsealed_uploads_per_owner=2,
            operations_global=2,
            live_spool_bytes=536870912,
            live_buffer_target_ms=120000,
            sealed_unstarted_ms=1800000,
        ),
        decode=media.MediaDecodeLimits(
            segment_ms=60000,
            cpu_ms=120000,
            wall_ms=300000,
            address_space_bytes=805306368,
            cpu_affinity_count=1,
            children_per_operation=1,
            children_global=2,
            track_limit=8,
            frames_per_track_segment=3600,
            pixels_per_frame=16777216,
            max_edge=4096,
            frame_rate=media.MediaQ(num=60, den=1),
            probe_metadata_bytes=1048576,
            stderr_bytes=65536,
        ),
        wire=media.MediaWireLimits(
            control_bytes=65536,
            body_bytes=16777216,
            audio_packet_ms=1000,
            image_bytes=8388608,
            clip_bytes=16777216,
            clip_ms=10000,
            consumer_packets=8,
            consumer_bytes=16777216,
            total_pending_bytes=67108864,
            live_backlog_ms=2000,
        ),
    )


def _binding(index=1, **changes):
    source_id = f"{index + 100:032x}"
    values = {
        "operation_id": f"{index + 1000:032x}",
        "child_instance": f"{index:032x}",
        "source": media.MediaNativeRef(backend_id="a" * 32, receipt_id=source_id, sha256="b" * 64),
        "source_id": source_id,
        "consumer": media.MediaConsumerRef(consumer_id="c" * 32, generation=1),
        "fence": media.MediaFence(
            session=media.MediaSession(session_id="session", thread_id="thread", epoch=1),
            source_revision=1,
            generation=1,
            lease_generation=1,
        ),
        "binding_sha256": hashlib.sha256(f"native-binding-{index}".encode()).hexdigest(),
    }
    values.update(changes)
    return MediaLaunchBinding(**values)


async def _line(channel):
    chunks = bytearray()
    while not chunks.endswith(b"\n"):
        part = await asyncio.wait_for(channel.recv_chunk(65536), _WAIT)
        assert part, "synthetic child ended before the complete expected response"
        chunks.extend(part)
        assert len(chunks) <= 65536
    return bytes(chunks[:-1])


class _Harness:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.processes = []
        self.pools = []
        self.launches = []
        self.spools = []
        self.spawned = asyncio.Event()
        self.release_spawn = asyncio.Event()
        self.hold_spawn = False
        self.after_spawn = None
        self.current_result = True
        self.dependency_result = True
        self.hold_dependencies = False
        self.dependency_entered = asyncio.Event()
        self.release_dependencies = asyncio.Event()
        self.dependencies = []
        self.native_spawn = asyncio.create_subprocess_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", self.spawn)

    async def spawn(self, *args, **kwargs):
        process = await self.native_spawn(*args, **kwargs)
        self.processes.append(process)
        self.spawned.set()
        if self.after_spawn is not None:
            self.after_spawn(args, kwargs)
        while self.hold_spawn and not self.release_spawn.is_set():
            try:
                await self.release_spawn.wait()
            except asyncio.CancelledError:
                # A real kernel child exists even if a caller stops awaiting it.
                continue
        return process

    def current(self, binding):
        if isinstance(self.current_result, Exception):
            raise self.current_result
        return self.current_result

    async def retire(self, binding, child):
        self.dependencies.append((binding, child))
        if self.hold_dependencies:
            self.dependency_entered.set()
            await self.release_dependencies.wait()
        if isinstance(self.dependency_result, Exception):
            raise self.dependency_result
        return self.dependency_result

    def pool(self, name="target", **callbacks):
        marker = self.root / f"{name}.executed"
        spec = WorkerSpec(
            id=name,
            argv=(sys.executable, "-B", "-c", _PROGRAM, str(marker)),
            cwd=self.root,
            startup_timeout_s=2,
            stop_timeout_s=0.1,
        )
        pool = MediaWorkerPool(
            spec,
            _limits(),
            current=callbacks.get("current", self.current),
            retire_dependencies=callbacks.get("retire_dependencies", self.retire),
        )
        self.pools.append(pool)
        return pool, marker

    def register(self, pool, binding):
        launch = pool.register(binding)
        self.launches.append((pool, launch))
        return launch

    def grant(self, launch, binding):
        root = self.root / f"spool-{len(self.spools)}"
        root.mkdir(mode=0o700)
        spool = MediaSpool(root, _limits())
        self.spools.append(spool)
        upload = spool.reserve(binding.source_id, len(_SOURCE))
        digest = hashlib.sha256(_SOURCE).hexdigest()
        upload.append(0, 0, _SOURCE, digest)
        upload.seal(len(_SOURCE), digest)
        return upload, upload._open_child_grant(launch)

    async def close(self):
        self.release_spawn.set()
        self.release_dependencies.set()
        self.current_result = True
        self.dependency_result = True
        for pool, launch in self.launches:
            pool.fence(launch)
        try:
            for pool in self.pools:
                await pool.close()
        finally:
            for process in self.processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), _WAIT)
                assert process.returncode is not None
            for spool in self.spools:
                spool.close()


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    fixture = _Harness(tmp_path, monkeypatch)
    try:
        yield fixture
    finally:
        await fixture.close()


def _bootstrap_refuses(tmp_path, kind, *, actual_control=None):
    # This helper starts the real bootstrap directly, keeping the grant writer
    # OPEN. Actual exit therefore proves refusal preceded grant/target execution.
    # Its extra FD is not an actual cgroup, even when named cgroup.procs.
    script = Path(__file__).resolve().parents[1] / "packages/resources/_media_bootstrap.py"
    source = tmp_path / "source"
    source.write_bytes(b"synthetic source")
    marker = tmp_path / "target"
    fds = []
    sockets = []
    process = None
    try:
        grant_r, grant_w = os.pipe2(os.O_CLOEXEC)
        fds.extend([grant_r, grant_w])
        control, child_control = socket.socketpair()
        sockets.extend([control, child_control])
        source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
        config_fd = os.memfd_create("cgroup-oracle-config", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        fds.extend([source_fd, config_fd])
        raw = json.dumps(
            {
                "nonce": "a" * 32,
                "parent_pid": os.getpid(),
                "argv": [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'exec')",
                    str(marker),
                ],
                "cwd": str(tmp_path),
                "env": {},
            },
            separators=(",", ":"),
        ).encode()
        assert os.write(config_fd, raw) == len(raw)
        fcntl.fcntl(
            config_fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE,
        )
        ordinary = tmp_path / "cgroup.procs"
        ordinary.write_bytes(b"do not overwrite")
        enrollment = os.open(
            actual_control if actual_control is not None else ordinary, os.O_WRONLY | os.O_CLOEXEC
        )
        fds.append(enrollment)
        if kind == "pipe":
            read, enrollment = os.pipe2(os.O_CLOEXEC)
            fds.extend([read, enrollment])
        if kind == "duplicate":
            enrollment = source_fd
        tag = "--untrusted-detail" if kind == "wrong_tag" else "--kinaiya-cgroup-v1"
        args = [
            str(grant_r),
            str(config_fd),
            "a" * 32,
            str(child_control.fileno()),
            str(source_fd),
            tag,
            str(enrollment),
        ]
        if kind == "extra_arg":
            args.append("extra")
        process = subprocess.Popen(
            [sys.executable, "-B", str(script), *args],
            pass_fds=tuple(
                set([grant_r, config_fd, child_control.fileno(), source_fd, enrollment])
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.wait(timeout=5) == 2
        stdout, stderr = process.communicate(timeout=5)
        assert stdout == stderr == b""
        assert not marker.exists()
        assert ordinary.read_bytes() == b"do not overwrite"
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        for endpoint in sockets:
            endpoint.close()
        for fd in set(fds):
            os.close(fd)
