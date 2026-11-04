"""Original-process hold/release requirements, written before candidate review.

Uses the unchanged real FD/spool/process fixture. Callback state is controlled
native-owner stand-in, not Session admission, sandbox or budget qualification.
Every actual disposable process is retained and waited by that fixture.
"""

import asyncio
import copy
import errno
import json
import os
from pathlib import Path

import pytest
from test_media_processes import _WAIT, _async_refused, _binding, _line
from test_media_processes import harness as harness

from packages.contracts import media


async def _prepared(harness, name="held", **callbacks):
    pool, marker = harness.pool(name, **callbacks)
    binding = _binding()
    launch = harness.register(pool, binding)
    upload, grant = harness.grant(launch, binding)
    prepared = await asyncio.wait_for(pool.prepare(launch, grant), _WAIT)
    return pool, marker, binding, launch, upload, grant, prepared


def _kernel_identity(process):
    raw = Path(f"/proc/{process.pid}/stat").read_bytes()
    fields = raw[raw.rfind(b")") + 2 :].split()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
    return int(fields[19]), boot


def _still_held(process, marker):
    command = Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0")
    assert any(part.endswith(b"/_media_bootstrap.py") for part in command)
    assert process.returncode is None
    assert not marker.exists()


async def test_verified_hold_exposes_exact_kernel_identity_before_exec(harness):
    pool, marker, binding, launch, _, grant, prepared = await _prepared(harness)
    assert len(harness.processes) == 1
    process = harness.processes[0]
    _still_held(process, marker)
    identity = prepared.identity
    assert type(identity) is media.MediaProcessIdentity
    start, boot = _kernel_identity(process)
    assert (identity.pid, identity.start_tick, identity.boot_id) == (process.pid, start, boot)
    assert identity.nonce.encode() in Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0")
    assert len(identity.pidfd_owner) == 32
    assert prepared.binding == binding
    assert await pool.prepare(launch, grant) is prepared
    child = await asyncio.wait_for(pool.start_prepared(prepared), _WAIT)
    assert json.loads(await _line(child.channel))["pid"] == identity.pid
    assert marker.read_bytes() == b"original executed once"
    assert await pool.start_prepared(prepared) is child
    assert len(harness.processes) == 1
    assert (await pool.settle(launch)).state == "settled"


async def test_mutating_returned_identity_and_binding_does_not_change_original(harness):
    pool, marker, binding, _, _, _, prepared = await _prepared(harness)
    first = media.media_bytes(prepared.identity)
    exposed = prepared.identity
    object.__setattr__(exposed, "pid", 1)
    exposed_binding = prepared.binding
    object.__setattr__(exposed_binding.source, "receipt_id", "f" * 32)
    assert media.media_bytes(prepared.identity) == first
    assert prepared.binding == binding
    child = await pool.start_prepared(prepared)
    assert json.loads(await _line(child.channel))["pid"] == harness.processes[0].pid
    assert marker.read_bytes() == b"original executed once"


async def test_cancelled_prepare_waiter_reuses_original_late_spawn(harness):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    harness.hold_spawn = True
    waiting = asyncio.create_task(pool.prepare(launch, grant))
    try:
        await asyncio.wait_for(harness.spawned.wait(), _WAIT)
        waiting.cancel()
        done, _ = await asyncio.wait({waiting}, timeout=_WAIT)
        assert waiting in done, "cancelled observer did not detach from the held original"
        with pytest.raises(asyncio.CancelledError):
            await waiting
        _still_held(harness.processes[0], marker)
        replay = asyncio.create_task(pool.prepare(launch, grant))
        harness.release_spawn.set()
        prepared = await asyncio.wait_for(replay, _WAIT)
        _still_held(harness.processes[0], marker)
        child = await pool.start_prepared(prepared)
        assert json.loads(await _line(child.channel))["pid"] == harness.processes[0].pid
        assert len(harness.processes) == 1
    finally:
        # A broken cancellation shield must not prevent fixture teardown from
        # releasing and actually joining the intercepted native spawn.
        harness.release_spawn.set()
        done, _ = await asyncio.wait({waiting}, timeout=_WAIT)
        assert waiting in done, "test observer remains unresolved after spawn release"
        if not waiting.cancelled():
            waiting.exception()


async def test_cancelled_release_observer_retains_one_original_grant(harness):
    observer = None
    releasing = False
    requested = asyncio.Event()

    def current(_binding):
        nonlocal releasing
        if releasing:
            releasing = False
            requested.set()
            observer.cancel()
        return True

    pool, marker, _, _, _, _, prepared = await _prepared(harness, current=current)
    assigned = asyncio.Event()

    async def observe():
        await assigned.wait()
        return await pool.start_prepared(prepared)

    releasing = True
    observer = asyncio.create_task(observe())
    assigned.set()  # Also prevents an eager factory from entering before assignment.
    await asyncio.wait_for(requested.wait(), _WAIT)
    done, _ = await asyncio.wait({observer}, timeout=_WAIT)
    assert observer in done, "cancelled release observer failed to detach"
    with pytest.raises(asyncio.CancelledError):
        await observer
    child = await asyncio.wait_for(pool.start_prepared(prepared), _WAIT)
    assert json.loads(await _line(child.channel))["pid"] == harness.processes[0].pid
    assert marker.read_bytes() == b"original executed once"
    assert len(harness.processes) == 1


async def test_two_concurrent_release_observers_share_original_child(harness):
    pool, marker, _, _, _, _, prepared = await _prepared(harness)
    first, second = await asyncio.gather(
        pool.start_prepared(prepared), pool.start_prepared(prepared)
    )
    assert first is second
    assert json.loads(await _line(first.channel))["pid"] == harness.processes[0].pid
    assert marker.read_bytes() == b"original executed once"
    assert len(harness.processes) == 1


@pytest.mark.parametrize("action", ["fence", "settle", "close", "stale", "dead"])
async def test_retired_or_stale_held_original_cannot_execute(harness, action):
    pool, marker, _, launch, _, _, prepared = await _prepared(harness)
    process = harness.processes[0]
    _still_held(process, marker)
    if action == "fence":
        pool.fence(launch)
    elif action == "settle":
        assert (await pool.settle(launch)).state == "settled"
    elif action == "close":
        assert (await pool.close()).state == "settled"
    elif action == "stale":
        harness.current_result = False
    else:
        process.kill()
        await asyncio.wait_for(process.wait(), _WAIT)
    await _async_refused(pool.start_prepared(prepared))
    assert not marker.exists()
    harness.current_result = True
    assert (await pool.settle(launch)).state == "settled"
    await asyncio.wait_for(process.wait(), _WAIT)
    assert process.returncode is not None
    assert len(harness.processes) == 1


async def test_copied_or_foreign_capsule_never_selects_original(harness):
    pool, marker, _, _, _, _, prepared = await _prepared(harness)
    other, other_marker = harness.pool("other")
    await _async_refused(other.start_prepared(prepared))
    await _async_refused(pool.start_prepared(object()))
    try:
        copied = copy.copy(prepared)
    except (TypeError, ValueError):
        pass  # A capsule that cannot be copied also refuses this forgery.
    else:
        if copied is not prepared:
            await _async_refused(pool.start_prepared(copied))
        # Identity-preserving copying is ordinary exact replay, not a forgery.
    _still_held(harness.processes[0], marker)
    assert not other_marker.exists()
    child = await pool.start_prepared(prepared)
    await _line(child.channel)
    assert marker.read_bytes() == b"original executed once"


async def test_legacy_start_cannot_release_prepared_launch(harness):
    pool, marker, _, launch, _, grant, prepared = await _prepared(harness)
    await _async_refused(pool.start(launch, grant), "conflict")
    _still_held(harness.processes[0], marker)
    child = await pool.start_prepared(prepared)
    await _line(child.channel)
    assert marker.read_bytes() == b"original executed once"


async def test_prepare_cannot_reinterpret_legacy_running_original(harness):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    original = await pool.start(launch, grant)
    await _line(original.channel)
    await _async_refused(pool.prepare(launch, grant), "conflict")
    assert await pool.start(launch, grant) is original
    assert len(harness.processes) == 1
    assert marker.read_bytes() == b"original executed once"


async def test_wrong_grant_refuses_before_spawning_and_does_not_replace(harness):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    await _async_refused(pool.prepare(launch, object()), "conflict")
    assert harness.processes == [] and not marker.exists()
    prepared = await pool.prepare(launch, grant)
    assert len(harness.processes) == 1
    _still_held(harness.processes[0], marker)
    assert prepared.binding == binding


async def test_unavailable_kernel_boot_identity_never_grants(harness, monkeypatch):
    native_open = os.open
    hits = []

    def unavailable(path, *args, **kwargs):
        if os.fspath(path) == "/proc/sys/kernel/random/boot_id":
            hits.append(path)
            raise OSError(errno.ENOENT, "synthetic kernel observation unavailable")
        return native_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", unavailable)
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    await _async_refused(pool.prepare(launch, grant))
    assert hits, "source-reviewed kernel observation fault was not reached"
    assert not marker.exists()
    assert (await pool.settle(launch)).state == "settled"
    assert len(harness.processes) <= 1
    for process in harness.processes:
        await asyncio.wait_for(process.wait(), _WAIT)
        assert process.returncode is not None


async def test_successful_release_replay_after_exit_observes_historical_original(harness):
    pool, marker, _, launch, _, _, prepared = await _prepared(harness)
    child = await pool.start_prepared(prepared)
    await _line(child.channel)
    await child.channel.send_chunk(b"EXIT\n")
    assert await _line(child.channel) == b"BYE"
    process = harness.processes[0]
    assert await asyncio.wait_for(process.wait(), _WAIT) == 0
    assert await pool.start_prepared(prepared) is child
    assert len(harness.processes) == 1
    assert marker.read_bytes() == b"original executed once"
    assert (await pool.settle(launch)).state == "settled"
