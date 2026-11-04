"""Interface-first process/grant oracles; no new launch implementation was read.

Actual disposable children are captured at asyncio's subprocess seam and waited
in teardown. Test callbacks stand for explicit native dependency gates only;
these observations do not qualify a decoder, sandbox, model or public cleanup.
"""

import asyncio
import errno
import fcntl
import json
import os
import sys

import pytest
from media_process_helpers import (
    _PROGRAM as _PROGRAM,
)
from media_process_helpers import (
    _SOURCE as _SOURCE,
)
from media_process_helpers import (
    _WAIT as _WAIT,
)
from media_process_helpers import (
    _binding as _binding,
)
from media_process_helpers import (
    _Harness as _Harness,
)
from media_process_helpers import (
    _limits as _limits,
)
from media_process_helpers import (
    _line as _line,
)
from media_process_helpers import (
    harness as harness,
)

from packages.contracts import media
from packages.resources.media_processes import (
    MediaLaunchBinding,
    MediaProcessError,
)
from packages.resources.processes import WorkerManager, WorkerSpec

_CODES = {"bad_packet", "bound", "capacity", "conflict", "expired", "unavailable"}


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


def test_binding_is_exact_frozen_and_snapshots_nested_native_inputs():
    source = media.MediaNativeRef(backend_id="a" * 32, receipt_id=f"{101:032x}", sha256="b" * 64)
    original = _binding(source=source)
    object.__setattr__(source, "receipt_id", "e" * 32)
    assert source.receipt_id == "e" * 32
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
    pool, marker = harness.pool()
    bindings = [_binding(1), _binding(2)]
    children = []
    for binding in bindings:
        launch = harness.register(pool, binding)
        _, grant = harness.grant(launch, binding)
        child = await pool.start(launch, grant)
        await _line(child.channel)
        children.append(child)
    assert marker.read_bytes() == b"original executed once" * 2
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

    native_primitive = getattr(os, primitive)
    unavailable_calls = []

    def unavailable(*args, **kwargs):
        if primitive == "pidfd_open":
            pid = args[0] if args else kwargs.get("pid")
            if not any(process.pid == pid for process in harness.processes):
                # CPython's child watcher opens its pidfd before the spawn
                # seam returns the actual Process to this fixture.
                return native_primitive(*args, **kwargs)
        unavailable_calls.append((args, kwargs))
        raise OSError(errno.ENOSYS, "untrusted-child-detail")

    with monkeypatch.context() as patch:
        patch.setattr(os, primitive, unavailable)
        await _async_refused(pool.start(launch, grant))
    assert unavailable_calls, "the media ownership refusal seam was not exercised"
    if primitive == "pidfd_open":
        assert len(harness.processes) == 1
        assert all(args[0] == harness.processes[0].pid for args, _ in unavailable_calls)
    assert (await pool.settle(launch)).state == "settled"
    for process in harness.processes:
        assert await process.wait() is not None
    assert not marker.exists()


async def test_lost_spawn_return_keeps_original_unknown_and_charged_after_actual_reap(harness):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    upload, grant = harness.grant(launch, binding)
    lost_returns = []

    def lose_return(_args, _kwargs):
        assert len(harness.processes) == 1
        lost_returns.append(harness.processes[0])
        raise OSError(errno.EIO, "synthetic lost spawn return facade")

    harness.after_spawn = lose_return
    await _async_refused(pool.start(launch, grant))
    assert lost_returns, "the actual captured subprocess return was not lost"
    assert len(harness.processes) == 1 and lost_returns == harness.processes
    original = lost_returns[0]
    assert not marker.exists()
    assert await asyncio.wait_for(original.wait(), _WAIT) is not None
    assert not marker.exists(), "configured code executed without a returned ownership handle"
    pool.fence(launch)
    assert (await pool.settle(launch)).state == "unknown"
    assert upload.delete() is False and not upload.deleted
    _refused(lambda: pool.register(_binding(9, operation_id=binding.operation_id)), "capacity")
    assert pool.register(binding) is launch
    await _async_refused(pool.start(launch, grant))
    assert len(harness.processes) == 1 and lost_returns == harness.processes
    assert upload.delete() is False and not upload.deleted


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
    marker = tmp_path / "ordinary.executed"
    spec = WorkerSpec(
        id="ordinary",
        argv=(
            sys.executable,
            "-B",
            "-c",
            "import pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_bytes(b'ordinary ready'); time.sleep(60)",
            str(marker),
        ),
        cwd=tmp_path,
        stop_timeout_s=0.1,
    )
    readiness_calls = []

    async def ready(probed_spec, process):
        readiness_calls.append((probed_spec, process))
        return (
            probed_spec is spec
            and process in processes
            and process.returncode is None
            and marker.exists()
            and marker.read_bytes() == b"ordinary ready"
        )

    manager = WorkerManager([spec], readiness=ready)
    try:
        started = await manager.start("ordinary")
        assert started["ok"] is True and started["ready"] is True
        assert started["readiness"] == "verified"
        assert readiness_calls and marker.read_bytes() == b"ordinary ready"
        identity = manager.owned_identity("ordinary")
        assert identity is not None and manager.is_current(identity)
        stopped = await manager.stop("ordinary")
        assert stopped["ok"] is True and stopped["stop_evidence"]["reaped"] is True
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
