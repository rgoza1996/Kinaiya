"""Interface-first process/grant oracles; no new launch implementation was read.

Actual disposable children are captured at asyncio's subprocess seam and waited
in teardown. Test callbacks stand for explicit native dependency gates only;
these observations do not qualify a decoder, sandbox, model or public cleanup.
"""

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import sys

import pytest

from packages.contracts import media
from packages.resources.media_processes import (
    MediaLaunchBinding,
    MediaProcessError,
    MediaWorkerPool,
)
from packages.resources.processes import WorkerManager, WorkerSpec
from services.session.media_spool import MediaSpool

_CODES = {"bad_packet", "bound", "capacity", "conflict", "expired", "unavailable"}
_SOURCE = b"synthetic media launch source\0" + bytes(range(32))
_WAIT = 10
_PROGRAM = r"""
import errno, fcntl, json, os, socket, sys
marker = sys.argv[1]
with open(marker, 'xb') as stream:
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


def _refused(call, code=None):
    with pytest.raises(MediaProcessError) as failure:
        call()
    assert isinstance(failure.value, ValueError) and failure.value.code in _CODES
    if code is not None:
        assert failure.value.code == code
    assert "untrusted-child-detail" not in str(failure.value)


async def _async_refused(awaitable, code=None):
    with pytest.raises(MediaProcessError) as failure:
        await asyncio.wait_for(awaitable, _WAIT)
    assert isinstance(failure.value, ValueError) and failure.value.code in _CODES
    if code is not None:
        assert failure.value.code == code


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


def test_binding_is_exact_frozen_and_snapshots_nested_native_inputs():
    source = media.MediaNativeRef(backend_id="a" * 32, receipt_id=f"{101:032x}", sha256="b" * 64)
    original = _binding(source=source)
    source.receipt_id = "e" * 32
    assert original.source.receipt_id == f"{101:032x}"
    with pytest.raises((AttributeError, TypeError)):
        original.source_id = "f" * 32
    fields = {
        name: getattr(original, name)
        for name in (
            "operation_id",
            "child_instance",
            "source",
            "source_id",
            "consumer",
            "fence",
            "binding_sha256",
        )
    }
    with pytest.raises(TypeError):
        MediaLaunchBinding(**fields, extra="untrusted-child-detail")


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "untrusted-child-detail"),
        ("child_instance", "A" * 32),
        ("source_id", True),
        ("binding_sha256", "a" * 63),
        ("source", object()),
        ("consumer", object()),
        ("fence", object()),
    ],
)
def test_binding_refuses_malformed_native_identity(field, value):
    _refused(lambda: _binding(**{field: value}))


async def test_registration_captures_inputs_and_exact_replay_before_any_spawn(harness):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    object.__setattr__(binding.source, "receipt_id", "d" * 32)
    object.__setattr__(binding.fence.session, "epoch", 9)
    assert pool.register(_binding()) is launch
    assert harness.processes == [] and not marker.exists()
    _refused(lambda: pool.register(_binding(binding_sha256="d" * 64)), "conflict")
    _refused(lambda: pool.register(_binding(source_id="d" * 32)), "conflict")
    pool.fence(launch)
    assert (await pool.settle(launch)).state == "settled"
    assert harness.dependencies and harness.dependencies[-1][1] is None


@pytest.mark.parametrize("callback", ["current", "retire_dependencies"])
async def test_native_callbacks_are_required_owner_configuration(harness, callback):
    with pytest.raises(MediaProcessError) as failure:
        harness.pool(**{callback: None})
    assert failure.value.code in _CODES and harness.processes == []


async def test_reserved_slots_are_conjunctive_and_positive_history_is_retained(harness):
    pool, _ = harness.pool()
    first = harness.register(pool, _binding(1))
    second = harness.register(pool, _binding(2))
    _refused(lambda: pool.register(_binding(3)), "capacity")
    _refused(lambda: pool.register(_binding(4, operation_id=_binding(1).operation_id)), "capacity")
    pool.fence(first)
    assert (await pool.settle(first)).state == "settled"
    third = harness.register(pool, _binding(3))
    assert pool.register(_binding(1)) is first
    assert third is not first and second is not first
    _refused(lambda: pool.register(_binding(1, binding_sha256="e" * 64)), "conflict")


async def test_original_child_has_duplex_fd3_and_independent_readonly_fd4(harness):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    upload, grant = harness.grant(launch, binding)
    reader = upload.open_reader()
    try:
        assert reader.read(7) == _SOURCE[:7]
        child = await asyncio.wait_for(pool.start(launch, grant), _WAIT)
        report = json.loads(await _line(child.channel))
        assert marker.read_bytes() == b"original executed once"
        assert report == {
            "pid": harness.processes[0].pid,
            "position": 0,
            "readonly": True,
            "write_errno": errno.EBADF,
            "source": _SOURCE.hex(),
        }
        assert await pool.start(launch, grant) is child
        assert len(harness.processes) == 1
        await child.channel.send_chunk(b"PING\n")
        assert await _line(child.channel) == b"PONG"
        assert reader.read(7) == _SOURCE[7:14]
        pool.fence(launch)
        await _async_refused(child.channel.send_chunk(b"untrusted-child-detail"))
        await _async_refused(child.channel.recv_chunk(1))
        assert (await pool.settle(launch)).state == "settled"
        assert await harness.processes[0].wait() is not None
    finally:
        reader.close()


async def test_foreign_handle_and_different_grant_never_create_second_effect(harness):
    pool, _ = harness.pool("first")
    foreign_pool, _ = harness.pool("second")
    binding = _binding()
    launch = harness.register(pool, binding)
    foreign = harness.register(foreign_pool, binding)
    _, grant = harness.grant(launch, binding)
    _, other_grant = harness.grant(foreign, binding)
    await _async_refused(pool.start(foreign, other_grant), "conflict")
    child = await pool.start(launch, grant)
    await _line(child.channel)
    await _async_refused(pool.start(launch, other_grant), "conflict")
    assert len(harness.processes) == 1


@pytest.mark.parametrize("replacement", [False, None, 1, RuntimeError("untrusted-child-detail")])
async def test_currentness_rechecked_after_actual_spawn_before_grant(harness, replacement):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    harness.after_spawn = lambda args, kwargs: setattr(harness, "current_result", replacement)
    await _async_refused(pool.start(launch, grant))
    assert (await pool.settle(launch)).state == "settled"
    assert len(harness.processes) == 1
    assert await harness.processes[0].wait() is not None
    assert not marker.exists(), "configured code ran after the pre-grant owner gate refused"


@pytest.mark.parametrize("fence_before_release", [False, True])
async def test_cancelled_waiter_retains_actual_late_spawn_and_exact_replay(
    harness, fence_before_release
):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    harness.hold_spawn = True
    waiter = asyncio.create_task(pool.start(launch, grant))
    await asyncio.wait_for(harness.spawned.wait(), _WAIT)
    assert not marker.exists()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    _refused(lambda: pool.register(_binding(9, operation_id=binding.operation_id)), "capacity")
    if fence_before_release:
        pool.fence(launch)
    harness.release_spawn.set()
    if fence_before_release:
        await _async_refused(pool.start(launch, grant))
    else:
        child = await asyncio.wait_for(pool.start(launch, grant), _WAIT)
        assert json.loads(await _line(child.channel))["source"] == _SOURCE.hex()
        pool.fence(launch)
    assert (await pool.settle(launch)).state == "settled"
    assert len(harness.processes) == 1 and await harness.processes[0].wait() is not None
    assert marker.exists() is not fence_before_release


@pytest.mark.parametrize("dependency", [False, None, 1, RuntimeError("untrusted-child-detail")])
async def test_outer_reap_does_not_retire_unknown_native_dependencies(harness, dependency):
    pool, _ = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    upload, grant = harness.grant(launch, binding)
    child = await pool.start(launch, grant)
    await _line(child.channel)
    harness.dependency_result = dependency
    pool.fence(launch)
    observed = await pool.settle(launch)
    assert observed.state == "unknown"
    with pytest.raises((AttributeError, TypeError, ValueError)):
        observed.state = "settled"
    assert observed.state == "unknown"
    assert await harness.processes[0].wait() is not None
    assert upload.delete() is False and not upload.deleted
    _refused(lambda: pool.register(_binding(9, operation_id=binding.operation_id)), "capacity")
    harness.dependency_result = True
    assert (await pool.settle(launch)).state == "settled"
    assert upload.delete() is True and upload.deleted
    assert len(harness.processes) == 1


async def test_pool_close_fences_all_and_unknown_dependency_prevents_aggregate_success(harness):
    pool, _ = harness.pool()
    bindings = [_binding(1), _binding(2)]
    children = []
    for binding in bindings:
        launch = harness.register(pool, binding)
        _, grant = harness.grant(launch, binding)
        child = await pool.start(launch, grant)
        await _line(child.channel)
        children.append(child)
    harness.dependency_result = False
    assert (await pool.close()).state == "unknown"
    for child in children:
        await _async_refused(child.channel.send_chunk(b"PING\n"))
    assert all(process.returncode is not None for process in harness.processes)
    _refused(lambda: pool.register(_binding(3)))
    harness.dependency_result = True
    assert (await pool.close()).state == "settled"


@pytest.mark.parametrize("method", ["settle", "close"])
async def test_cancelled_settlement_observer_keeps_original_dependency_join(harness, method):
    pool, _ = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    upload, grant = harness.grant(launch, binding)
    child = await pool.start(launch, grant)
    await _line(child.channel)
    harness.hold_dependencies = True
    operation = pool.settle(launch) if method == "settle" else pool.close()
    observer = asyncio.create_task(operation)
    await asyncio.wait_for(harness.dependency_entered.wait(), _WAIT)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    _refused(lambda: pool.register(_binding(9, operation_id=binding.operation_id)))
    assert upload.delete() is False and not upload.deleted
    harness.release_dependencies.set()
    assert (await pool.settle(launch)).state == "settled"
    assert len(harness.dependencies) == 1, "caller cancellation replaced the original native join"
    assert len(harness.processes) == 1 and await harness.processes[0].wait() is not None
    assert upload.delete() is True


@pytest.mark.parametrize("primitive", ["pidfd_open", "memfd_create"])
async def test_unavailable_native_ownership_primitive_never_executes_configured_code(
    harness, monkeypatch, primitive
):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)

    def unavailable(*args, **kwargs):
        raise OSError(errno.ENOSYS, "untrusted-child-detail")

    with monkeypatch.context() as patch:
        patch.setattr(os, primitive, unavailable)
        await _async_refused(pool.start(launch, grant))
    assert (await pool.settle(launch)).state == "settled"
    for process in harness.processes:
        assert await process.wait() is not None
    assert not marker.exists()


@pytest.mark.parametrize("value", [b"", b"x" * 65537, bytearray(b"x"), "x", None])
async def test_channel_send_refuses_nonbytes_and_out_of_bound_chunks(harness, value):
    pool, _ = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    child = await pool.start(launch, grant)
    await _line(child.channel)
    await _async_refused(child.channel.send_chunk(value))
    await child.channel.send_chunk(b"PING\n")
    assert await _line(child.channel) == b"PONG"


@pytest.mark.parametrize("value", [0, 65537, True, 1.0, None])
async def test_channel_receive_has_strict_bounded_native_integer(harness, value):
    pool, _ = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    child = await pool.start(launch, grant)
    await _line(child.channel)
    await _async_refused(child.channel.recv_chunk(value))
    await child.channel.send_chunk(b"PING\n")
    assert await _line(child.channel) == b"PONG"


async def test_one_pending_receiver_fence_joins_original_before_descriptor_reuse(
    harness, monkeypatch, tmp_path
):
    pool, _ = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    child = await pool.start(launch, grant)
    await _line(child.channel)
    loop = asyncio.get_running_loop()
    native_add_reader = loop._add_reader
    queued = []
    registered = asyncio.Event()

    def observe_reader(fd, callback, *args):
        queued.append((fd, callback, args))
        result = native_add_reader(fd, callback, *args)
        registered.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(loop, "_add_reader", observe_reader)
        original_read = asyncio.create_task(child.channel.recv_chunk(1))
        await asyncio.wait_for(registered.wait(), _WAIT)
    assert queued, "the native event-loop read registration seam was not exercised"
    await _async_refused(child.channel.recv_chunk(1))
    pool.fence(launch)
    assert (await pool.settle(launch)).state == "settled"
    assert original_read.done(), "positive settlement left original control I/O live"
    await asyncio.gather(original_read, return_exceptions=True)
    assert await harness.processes[0].wait() is not None
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"untouched sentinel")
    sentinels = []
    try:
        for fd, callback, args in queued:
            temporary = os.open(sentinel, os.O_RDONLY | os.O_CLOEXEC)
            if temporary == fd:
                reused = temporary
            else:
                reused = fcntl.fcntl(temporary, fcntl.F_DUPFD_CLOEXEC, fd)
                os.close(temporary)
            sentinels.append(reused)
            assert reused == fd, "old control descriptor did not become available for reuse"
            callback(*args)
            assert os.read(reused, 64) == b"untouched sentinel"
    finally:
        for fd in sentinels:
            os.close(fd)


async def test_more_than_sixteen_positive_originals_reuse_slots_without_rearming(harness):
    pool, marker = harness.pool()
    history = []
    for index in range(1, 19):
        binding = _binding(index)
        launch = harness.register(pool, binding)
        _, grant = harness.grant(launch, binding)
        # Each real execution must create its marker exactly once.
        if marker.exists():
            marker.unlink()
        child = await pool.start(launch, grant)
        await _line(child.channel)
        assert marker.read_bytes() == b"original executed once"
        pool.fence(launch)
        assert (await pool.settle(launch)).state == "settled"
        assert await harness.processes[-1].wait() is not None
        history.append((binding, launch, grant, child))
    assert len(harness.processes) == 18
    for binding, launch, grant, child in history:
        assert pool.register(binding) is launch
        assert await pool.start(launch, grant) is child
        await _async_refused(child.channel.send_chunk(b"PING\n"))
    assert len(harness.processes) == 18


async def test_channel_accepts_exact_65536_byte_send_and_receive_bound(harness):
    pool, _ = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    child = await pool.start(launch, grant)
    await _line(child.channel)
    body = b"x" * 65535
    await asyncio.wait_for(child.channel.send_chunk(body + b"\n"), _WAIT)
    assert await _line(child.channel) == body


async def test_baseline_generic_manager_still_runs_and_reaps_ordinary_fixed_worker(
    tmp_path, monkeypatch
):
    original_spawn = asyncio.create_subprocess_exec
    processes = []

    async def capture(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    spec = WorkerSpec(
        id="ordinary",
        argv=(sys.executable, "-B", "-c", "import time; time.sleep(60)"),
        cwd=tmp_path,
        stop_timeout_s=0.1,
    )
    manager = WorkerManager([spec])
    try:
        await manager.start("ordinary")
        identity = manager.owned_identity("ordinary")
        assert identity is not None and manager.is_current(identity)
        await manager.stop("ordinary")
        assert manager.owned_identity("ordinary") is None
        assert len(processes) == 1 and processes[0].returncode is not None
    finally:
        try:
            await manager.close()
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), _WAIT)
                assert process.returncode is not None
