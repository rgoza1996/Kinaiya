"""Independent selected private spool-port oracles, authored before its source.

Only synthetic temporary files and real registered pool handles are used. The
callbacks below establish test-local dependency observations, not production
permission, decoder readiness, inner-worker proof or a native cleanup receipt.
"""

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from packages.contracts import media
from packages.resources.media_processes import (
    MediaLaunchBinding,
    MediaProcessError,
    MediaWorkerPool,
)
from packages.resources.processes import WorkerSpec
from services.session.media_spool import MediaSpool, MediaSpoolError

_CODES = {"bad_packet", "bound", "capacity", "conflict", "expired", "unavailable"}
_SOURCE = b"synthetic original grant source\0" + bytes(range(64))
_WAIT = 10
_PROGRAM = r"""
import fcntl, json, os, socket
channel = socket.socket(fileno=3)
position = os.lseek(4, 0, os.SEEK_CUR)
flags = fcntl.fcntl(4, fcntl.F_GETFL)
failure = None
try:
    os.write(4, b'forbidden')
except OSError as error:
    failure = error.errno
report = {'pid': os.getpid(), 'position': position,
          'readonly': flags & os.O_ACCMODE == os.O_RDONLY,
          'write_errno': failure, 'body': os.read(4, 1024).hex()}
channel.sendall(json.dumps(report, sort_keys=True).encode() + b'\n')
while channel.recv(65536):
    pass
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


def _binding(index=1, *, source_id=None):
    return MediaLaunchBinding(
        operation_id=f"{index + 1000:032x}",
        child_instance=f"{index:032x}",
        source=media.MediaNativeRef(
            backend_id="a" * 32, receipt_id=f"{index + 100:032x}", sha256="b" * 64
        ),
        source_id=source_id or f"{index + 100:032x}",
        consumer=media.MediaConsumerRef(consumer_id="c" * 32, generation=1),
        fence=media.MediaFence(
            session=media.MediaSession(session_id="session", thread_id="thread", epoch=1),
            source_revision=1,
            generation=1,
            lease_generation=1,
        ),
        binding_sha256=hashlib.sha256(f"native-grant-binding-{index}".encode()).hexdigest(),
    )


def _refused(operation, code=None):
    with pytest.raises(MediaSpoolError) as failure:
        operation()
    assert isinstance(failure.value, ValueError) and failure.value.code in _CODES
    if code is not None:
        assert failure.value.code == code


def _gate_denied(operation):
    # Root selected either fixed typed refusal or exact False for a false phase
    # gate. Neither may release original membership or charged capacity.
    try:
        value = operation()
    except MediaSpoolError as failure:
        assert failure.code in _CODES
    else:
        assert value is False


async def _report(channel):
    data = bytearray()
    while not data.endswith(b"\n"):
        part = await asyncio.wait_for(channel.recv_chunk(65536), _WAIT)
        assert part
        data.extend(part)
        assert len(data) <= 65536
    return json.loads(data)


class _Fixture:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.pools = []
        self.launches = []
        self.spools = []
        self.readers = []
        self.processes = []
        self.dependency = True
        self.current = True
        self.source_fd = None
        self.native_spawn = asyncio.create_subprocess_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", self.spawn)

    async def spawn(self, *args, **kwargs):
        originals = []
        for fd in kwargs.get("pass_fds", ()):
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if info.st_size == len(_SOURCE):
                    originals.append(fd)
        assert len(originals) == 1, "actual spawn did not pass one original synthetic source"
        self.source_fd = originals[0]
        assert fcntl.fcntl(self.source_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        process = await self.native_spawn(*args, **kwargs)
        self.processes.append(process)
        return process

    async def retire(self, binding, child):
        return self.dependency

    def pool(self):
        pool = MediaWorkerPool(
            WorkerSpec(
                id=f"grant-{len(self.pools)}",
                argv=(sys.executable, "-B", "-c", _PROGRAM),
                cwd=self.root,
                startup_timeout_s=2,
                stop_timeout_s=0.1,
            ),
            _limits(),
            current=lambda binding: self.current,
            retire_dependencies=self.retire,
        )
        self.pools.append(pool)
        return pool

    def register(self, pool, binding):
        launch = pool.register(binding)
        self.launches.append((pool, launch))
        return launch

    def upload(self, binding, *, sealed=True):
        root = self.root / f"spool-{len(self.spools)}"
        root.mkdir(mode=0o700)
        spool = MediaSpool(root, _limits())
        self.spools.append(spool)
        upload = spool.reserve(binding.source_id, len(_SOURCE))
        digest = hashlib.sha256(_SOURCE).hexdigest()
        upload.append(0, 0, _SOURCE, digest)
        if sealed:
            upload.seal(len(_SOURCE), digest)
        return spool, upload, root

    def reader(self, upload):
        reader = upload.open_reader()
        self.readers.append(reader)
        return reader

    async def close(self):
        self.current = True
        self.dependency = True
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
            for reader in self.readers:
                try:
                    reader.close()
                except MediaSpoolError as failure:
                    assert failure.code in _CODES
            for spool in self.spools:
                try:
                    spool.close()
                except MediaSpoolError as failure:
                    assert failure.code in _CODES


@pytest.fixture
async def fixture(tmp_path, monkeypatch):
    owner = _Fixture(tmp_path, monkeypatch)
    try:
        yield owner
    finally:
        await owner.close()


async def test_only_real_registered_source_matched_launch_can_bind_without_poison(fixture):
    pool = fixture.pool()
    binding = _binding()
    wrong = fixture.register(pool, _binding(2))
    launch = fixture.register(pool, binding)
    spool, upload, _ = fixture.upload(binding)
    impostor = SimpleNamespace(
        _bind_spool_grant=lambda *args: None,
        _spool_grant_can_spawn=lambda *args: True,
        _spool_grant_can_close_parent=lambda *args: True,
        _spool_grant_can_retire=lambda *args: True,
    )
    _refused(lambda: upload._open_child_grant(impostor))
    _refused(lambda: upload._open_child_grant(wrong))
    assert fixture.processes == [] and spool.reserved_bytes == len(_SOURCE)
    grant = upload._open_child_grant(launch)
    readers = [fixture.reader(upload) for _ in range(15)]
    _refused(upload.open_reader, "capacity")
    assert all(reader.read(1) == _SOURCE[:1] for reader in readers)
    _refused(lambda: upload._open_child_grant(launch))
    child = await pool.start(launch, grant)
    assert (await _report(child.channel))["body"] == _SOURCE.hex()
    assert len(fixture.processes) == 1


async def test_already_bound_original_does_not_poison_other_healthy_upload(fixture):
    pool = fixture.pool()
    binding = _binding()
    launch = fixture.register(pool, binding)
    _, first, _ = fixture.upload(binding)
    grant = first._open_child_grant(launch)
    second_spool, second, _ = fixture.upload(binding)
    _refused(lambda: second._open_child_grant(launch))
    readers = [fixture.reader(second) for _ in range(16)]
    assert all(reader.read(1) == _SOURCE[:1] for reader in readers)
    assert second_spool.reserved_bytes == len(_SOURCE)
    _refused(second.open_reader, "capacity")
    pool.fence(launch)
    assert (await pool.settle(launch)).state == "settled"
    assert grant._retire_child_use(launch) is True


async def test_unsealed_refusal_has_no_descriptor_or_reader_membership_effect(fixture):
    pool = fixture.pool()
    binding = _binding()
    launch = fixture.register(pool, binding)
    _, upload, _ = fixture.upload(binding, sealed=False)
    _refused(lambda: upload._open_child_grant(launch))
    upload.seal(len(_SOURCE), hashlib.sha256(_SOURCE).hexdigest())
    readers = [fixture.reader(upload) for _ in range(16)]
    assert all(reader.read(1) == _SOURCE[:1] for reader in readers)
    _refused(upload.open_reader, "capacity")
    assert fixture.processes == []


async def test_grant_is_registered_in_shared_sixteen_before_os_open(fixture, monkeypatch):
    pool = fixture.pool()
    binding = _binding()
    launch = fixture.register(pool, binding)
    spool, upload, _ = fixture.upload(binding)
    readers = [fixture.reader(upload) for _ in range(15)]
    native_open = os.open
    observed = []

    def opening(path, flags, *args, **kwargs):
        if os.fsdecode(path).endswith(binding.source_id + ".data"):
            observed.append(path)
            _refused(upload.open_reader, "capacity")
        return native_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opening)
        grant = upload._open_child_grant(launch)
    assert observed, "the independent source acquisition was not observed"
    _refused(upload.open_reader, "capacity")
    _refused(lambda: grant._fd_for_spawn(launch))
    _gate_denied(lambda: grant._close_parent_copy(launch))
    _gate_denied(lambda: grant._retire_child_use(launch))
    assert upload.delete() is False and spool.reserved_bytes == len(_SOURCE)
    for reader in readers:
        reader.close()
    assert not upload.deleted and spool.reserved_bytes == len(_SOURCE)
    pool.fence(launch)
    assert (await pool.settle(launch)).state == "settled"
    assert upload.delete() is True and spool.reserved_bytes == 0


async def test_parent_close_keeps_live_child_reader_until_native_dependencies_settle(fixture):
    pool = fixture.pool()
    binding = _binding()
    launch = fixture.register(pool, binding)
    spool, upload, _ = fixture.upload(binding)
    public_reader = fixture.reader(upload)
    assert public_reader.read(9) == _SOURCE[:9]
    grant = upload._open_child_grant(launch)
    child = await pool.start(launch, grant)
    report = await _report(child.channel)
    assert report == {
        "pid": fixture.processes[0].pid,
        "position": 0,
        "readonly": True,
        "write_errno": errno.EBADF,
        "body": _SOURCE.hex(),
    }
    assert fixture.processes[0].returncode is None
    assert grant._close_parent_copy(launch) is True
    _gate_denied(lambda: grant._retire_child_use(launch))
    assert upload.delete() is False and upload.delete_pending
    public_reader.close()
    assert not upload.deleted and spool.reserved_bytes == len(_SOURCE)
    fixture.dependency = False
    pool.fence(launch)
    assert (await pool.settle(launch)).state == "unknown"
    assert await fixture.processes[0].wait() is not None
    _gate_denied(lambda: grant._retire_child_use(launch))
    assert not upload.deleted and spool.reserved_bytes == len(_SOURCE)
    fixture.current = False  # Expired input permission cannot prevent original cleanup.
    fixture.dependency = True
    assert (await pool.settle(launch)).state == "settled"
    assert grant._retire_child_use(launch) is True
    assert upload.delete() is True and upload.deleted and spool.reserved_bytes == 0


async def test_foreign_launch_cannot_take_or_retire_original_source_descriptor(fixture):
    pool = fixture.pool()
    foreign_pool = fixture.pool()
    binding = _binding()
    launch = fixture.register(pool, binding)
    foreign = fixture.register(foreign_pool, binding)
    spool, upload, _ = fixture.upload(binding)
    grant = upload._open_child_grant(launch)
    _refused(lambda: grant._fd_for_spawn(foreign))
    _gate_denied(lambda: grant._close_parent_copy(foreign))
    _gate_denied(lambda: grant._retire_child_use(foreign))
    assert spool.reserved_bytes == len(_SOURCE)
    child = await pool.start(launch, grant)
    assert (await _report(child.channel))["body"] == _SOURCE.hex()
    pool.fence(launch)
    assert (await pool.settle(launch)).state == "settled"
    assert upload.delete() is True


async def test_sealed_inode_replacement_refuses_child_grant_and_keeps_charge(fixture):
    pool = fixture.pool()
    binding = _binding()
    launch = fixture.register(pool, binding)
    spool, upload, root = fixture.upload(binding)
    source = root / (binding.source_id + ".data")
    replacement = root / "replacement"
    replacement.write_bytes(_SOURCE)
    replacement.chmod(0o600)
    replacement.replace(source)
    _refused(lambda: upload._open_child_grant(launch))
    assert fixture.processes == []
    assert not upload.deleted and spool.reserved_bytes == len(_SOURCE)


async def test_post_open_source_stamp_race_never_returns_a_spawnable_grant(fixture, monkeypatch):
    pool = fixture.pool()
    binding = _binding()
    launch = fixture.register(pool, binding)
    spool, upload, root = fixture.upload(binding)
    native_open = os.open
    changes = []

    def opening(path, flags, *args, **kwargs):
        fd = native_open(path, flags, *args, **kwargs)
        if os.fsdecode(path).endswith(binding.source_id + ".data"):
            changes.append(fd)
            source = root / (binding.source_id + ".data")
            with source.open("r+b") as stream:
                stream.write(b"changed after independent open")
                stream.flush()
                os.fsync(stream.fileno())
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opening)
        _refused(lambda: upload._open_child_grant(launch))
    assert changes and fixture.processes == []
    assert not upload.deleted and spool.reserved_bytes == len(_SOURCE)


async def test_parent_close_eio_discards_integer_and_never_closes_reused_sentinel(
    fixture, monkeypatch, tmp_path
):
    pool = fixture.pool()
    binding = _binding()
    launch = fixture.register(pool, binding)
    spool, upload, _ = fixture.upload(binding)
    grant = upload._open_child_grant(launch)
    sentinel_path = tmp_path / "sentinel"
    sentinel_path.write_bytes(b"sentinel survives ambiguous close")
    sentinel_inode = sentinel_path.stat().st_ino
    native_close = os.close
    closes = []
    sentinels = []

    def close(fd):
        if fd != fixture.source_fd:
            return native_close(fd)
        closes.append(fd)
        if len(closes) > 1:
            return native_close(fd)
        native_close(fd)
        temporary = os.open(sentinel_path, os.O_RDONLY | os.O_CLOEXEC)
        if temporary == fd:
            reused = temporary
        else:
            reused = fcntl.fcntl(temporary, fcntl.F_DUPFD_CLOEXEC, fd)
            native_close(temporary)
        sentinels.append(reused)
        assert reused == fd
        raise OSError(errno.EIO, "untrusted-child-detail")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "close", close)
            try:
                child = await asyncio.wait_for(pool.start(launch, grant), _WAIT)
            except MediaProcessError as failure:
                assert failure.code in _CODES
            else:
                assert (await _report(child.channel))["body"] == _SOURCE.hex()
            pool.fence(launch)
            assert (await pool.settle(launch)).state == "unknown"
            assert closes == [fixture.source_fd] and sentinels == [fixture.source_fd]
            assert await fixture.processes[0].wait() is not None
            for _ in range(2):
                _gate_denied(lambda: grant._close_parent_copy(launch))
                _gate_denied(lambda: grant._retire_child_use(launch))
                assert (await pool.settle(launch)).state == "unknown"
            assert closes == [fixture.source_fd]
            assert os.fstat(sentinels[0]).st_ino == sentinel_inode
            assert os.read(sentinels[0], 128) == b"sentinel survives ambiguous close"
            assert not upload.deleted and spool.reserved_bytes == len(_SOURCE)
    finally:
        for fd in sentinels:
            try:
                if os.fstat(fd).st_ino == sentinel_inode:
                    native_close(fd)
            except OSError as failure:
                if failure.errno != errno.EBADF:
                    raise
