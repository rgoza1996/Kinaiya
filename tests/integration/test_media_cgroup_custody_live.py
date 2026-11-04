"""Optional real-kernel custody oracles, sealed before candidate exposure.

Requires a NEW dedicated delegated service; pytest itself must be in its
immediate supervisor subgroup. Every mutation below is confined to newly
recorded test-owned leaves. This is no Session/budget/runtime qualification.
"""

import asyncio
import copy
import errno
import json
import os
import re
import select
import signal
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from media_process_helpers import _binding, _bootstrap_refuses, _Harness, _limits, _line

from packages.resources.media_cgroup import MediaCgroupError, MediaCgroupPool
from packages.resources.media_processes import MediaProcessError, MediaWorkerPool
from packages.resources.processes import WorkerSpec

pytestmark = pytest.mark.live
_WAIT = 10
_LEAF = re.compile(r"kinaiya-media-[0-9a-f]{32}\Z")
_CODES = {"bad_input", "unavailable", "conflict", "capacity", "expired", "unknown"}

_PROGRAM = r"""
import ctypes, fcntl, json, os, signal, socket, sys
marker = sys.argv[1]
with open(marker, 'xb') as stream:
    stream.write(b'one original exec')
channel = socket.socket(fileno=3)
def send(value):
    channel.sendall(json.dumps(value, sort_keys=True).encode() + b'\n')
def receive():
    raw = b''
    while not raw.endswith(b'\n'):
        part = channel.recv(1)
        if not part:
            raise SystemExit(0)
        raw += part
    return raw[:-1]
fds = {}
for name in os.listdir('/proc/self/fd'):
    if name.isdecimal() and int(name) > 2:
        try:
            fds[name] = os.readlink('/proc/self/fd/' + name)
        except FileNotFoundError:
            pass
send({'pid': os.getpid(), 'cgroup': open('/proc/self/cgroup').read(), 'fds': fds,
      'readonly': fcntl.fcntl(4, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY})
while True:
    command = receive()
    if command == b'EXIT':
        send({'bye': True})
        os.close(4)
        channel.close()
        raise SystemExit(0)
    if command == b'WORK':
        child = os.fork()
        if child == 0:
            os.setsid()
            value = 1
            for number in range(1500000):
                value = (value * 33 + number) & 0xffffffff
            os._exit(0 if value >= 0 else 1)
        waited, status = os.waitpid(child, 0)
        send({'worked': waited, 'status': status})
    elif command == b'DESCENDANT':
        read, write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read)
            parent = os.getppid()
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
                os._exit(3)
            os.setsid()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.write(write, b'R')
            os.close(write)
            while True:
                signal.pause()
        os.close(write)
        assert os.read(read, 1) == b'R'
        os.close(read)
        send({'descendant': child})
    else:
        send({'pong': command.decode('ascii')})
"""

_GATED_BOOTSTRAP = r"""
import os, sys
ready, gate = map(int, sys.argv[1:3])
command = sys.argv[3:]
os.write(ready, b'R')
os.close(ready)
if os.read(gate, 1) != b'G':
    os._exit(7)
os.close(gate)
os.execv(command[0], command)
"""


def _refuses(call):
    with pytest.raises(MediaCgroupError) as failure:
        call()
    assert failure.value.code in _CODES
    assert str(failure.value) == "Invalid native media cgroup operation"


async def _async_refuses(awaitable):
    with pytest.raises((MediaProcessError, MediaCgroupError)):
        await asyncio.wait_for(awaitable, _WAIT)


def _events(path):
    return {
        key: int(value)
        for key, value in (
            line.split() for line in (path / "cgroup.events").read_text().splitlines()
        )
    }


def _cpu(path):
    return {
        key: int(value)
        for key, value in (line.split() for line in (path / "cpu.stat").read_text().splitlines())
    }["usage_usec"]


async def _positive(predicate):
    # Polling yields only while waiting for a POSITIVE observed kernel predicate;
    # time passing or quiet I/O never supplies that predicate.
    deadline = asyncio.get_running_loop().time() + _WAIT
    while True:
        value = predicate()
        if value:
            return value
        assert asyncio.get_running_loop().time() < deadline, "positive kernel observation missing"
        await asyncio.sleep(0)


def _fd_path(fd):
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return ""


class _Native:
    def __init__(self, root, harness):
        self.root, self.harness = root, harness
        self.pools, self.records, self.extra_fds = [], [], []

    def owner(self, capacity=2):
        pool = MediaCgroupPool(self.root, capacity=capacity)
        self.pools.append(pool)
        return pool

    def worker(self, index=1, *, startup=2):
        marker = self.harness.root / f"custody-{index}.executed"
        spec = WorkerSpec(
            id=f"custody-{index}",
            argv=(sys.executable, "-B", "-c", _PROGRAM, str(marker)),
            cwd=self.harness.root,
            startup_timeout_s=startup,
            stop_timeout_s=0.1,
        )
        worker = MediaWorkerPool(
            spec, _limits(), current=self.harness.current, retire_dependencies=self.harness.retire
        )
        self.harness.pools.append(worker)
        binding = _binding(index)
        launch = self.harness.register(worker, binding)
        upload, grant = self.harness.grant(launch, binding)
        return SimpleNamespace(
            worker=worker, launch=launch, upload=upload, grant=grant, marker=marker
        )

    def track_new(self, before):
        fresh = [
            path
            for path in self.root.iterdir()
            if _LEAF.fullmatch(path.name) and path not in before
        ]
        for path in fresh:
            stamp = path.stat()
            kill_fd = os.open(path / "cgroup.kill", os.O_WRONLY | os.O_CLOEXEC)
            record = SimpleNamespace(path=path, stamp=(stamp.st_dev, stamp.st_ino), kill_fd=kill_fd)
            self.records.append(record)
        return fresh

    def scope(self, owner, original):
        before = set(self.root.iterdir())
        try:
            scope = owner.create(original.launch)
        finally:
            fresh = self.track_new(before)
        assert len(fresh) == 1, "one actual original must create exactly one new leaf"
        record = next(record for record in self.records if record.path == fresh[0])
        return scope, record

    async def close(self):
        self.harness.release_spawn.set()
        self.harness.release_dependencies.set()
        # Independent emergency teardown uses ONLY a retained descriptor of a
        # newly observed test-owned leaf, never parent/root/app scopes. It does
        # not make a candidate unknown observation count as positive settlement.
        for record in self.records:
            try:
                os.write(record.kill_fd, b"1")
            except OSError:
                pass
        try:
            await self.harness.close()
        finally:
            try:
                for owner in self.pools:
                    owner.close()
            finally:
                try:
                    for record in self.records:
                        os.close(record.kill_fd)
                        if record.path is None:
                            # This fixture already observed successful external
                            # rmdir of that exact empty original, not native
                            # owner settlement or permission to remove its reuse.
                            assert record.external_rmdir == record.stamp
                            continue
                        if record.path.exists():
                            actual = record.path.stat()
                            assert (actual.st_dev, actual.st_ino) == record.stamp
                            await _positive(
                                lambda record=record: _events(record.path)["populated"] == 0
                            )
                            record.path.rmdir()
                finally:
                    for fd in self.extra_fds:
                        os.close(fd)


@pytest.fixture
async def native(tmp_path, monkeypatch):
    configured = os.environ.get("KINAIYA_TEST_CGROUP_ROOT")
    if configured is None:
        pytest.skip("optional native custody needs an explicitly delegated test service")
    root = Path(configured)
    assert root.is_absolute(), "configured root must be absolute; invalid configuration cannot skip"
    selected = root.stat()
    assert stat.S_ISDIR(selected.st_mode)
    relative = Path("/proc/self/cgroup").read_text().strip().split("0::", 1)[1]
    actual = Path("/sys/fs/cgroup") / relative.lstrip("/")
    assert actual.samefile(root / "supervisor"), "pytest must be in the immediate supervisor"
    assert root.name not in {"app.slice", "user@1000.service"}
    assert "chromium" not in root.name.lower()
    harness = _Harness(tmp_path, monkeypatch)
    fixture = _Native(root, harness)
    native_write = os.write

    def no_parent_migration(fd, data):
        path = _fd_path(fd)
        if path.endswith("/cgroup.procs") and path.startswith(str(root) + "/"):
            raise AssertionError("parent must never write a numeric PID/SELF to cgroup.procs")
        return native_write(fd, data)

    monkeypatch.setattr(os, "write", no_parent_migration)
    try:
        yield fixture
    finally:
        await fixture.close()


@pytest.mark.parametrize("capacity", [0, 3, True, 1.0, None])
def test_real_delegated_root_still_rejects_noncanonical_capacity(native, capacity):
    _refuses(lambda: MediaCgroupPool(native.root, capacity=capacity))


@pytest.mark.parametrize("attribute", ["cgroup.freeze", "cgroup.kill"])
def test_bootstrap_rejects_other_actual_cgroup_control_descriptors(native, tmp_path, attribute):
    owner = native.owner()
    original = native.worker()
    _, record = native.scope(owner, original)
    _bootstrap_refuses(tmp_path, "ordinary", actual_control=record.path / attribute)
    assert _events(record.path)["populated"] == 0


async def test_self_enrolled_original_is_held_before_grant_and_leaks_only_media_fds(native):
    original = native.worker()
    owner = native.owner()
    scope, record = native.scope(owner, original)
    assert owner.create(original.launch) is scope
    snapshot = scope.snapshot()
    assert re.fullmatch("[0-9a-f]{32}", snapshot.instance_id)
    assert re.fullmatch("[0-9a-f]{32}", snapshot.boot_id)
    assert (snapshot.device, snapshot.inode) == record.stamp
    with pytest.raises((AttributeError, TypeError, ValueError)):
        snapshot.usage_usec = 0
    prepared = await original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
    process = native.harness.processes[-1]
    membership = Path(f"/proc/{process.pid}/cgroup").read_text().strip().split("0::", 1)[1]
    assert (Path("/sys/fs/cgroup") / membership.lstrip("/")).samefile(record.path)
    assert not original.marker.exists()
    assert scope.snapshot().populated is True
    assert (
        await original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
        is prepared
    )
    child = await original.worker.start_prepared(prepared)
    report = json.loads(await _line(child.channel))
    assert report["pid"] == process.pid and report["readonly"] is True
    assert set(report["fds"]) == {"3", "4"}
    assert report["cgroup"].strip().split("0::", 1)[1] == membership
    assert original.marker.read_bytes() == b"one original exec"
    assert await original.worker.start_prepared(prepared) is child
    assert len(native.harness.processes) == 1
    await child.channel.send_chunk(b"EXIT\n")
    assert json.loads(await _line(child.channel)) == {"bye": True}
    assert await asyncio.wait_for(process.wait(), _WAIT) == 0
    assert (await original.worker.settle(original.launch)).state == "settled"
    final = scope.snapshot()
    assert final.populated is False
    retired = scope.close_after_settle(original.launch)
    assert retired.instance_id == final.instance_id
    assert retired.usage_usec >= final.usage_usec
    assert scope.close_after_settle(original.launch) is retired
    with pytest.raises((AttributeError, TypeError, ValueError)):
        retired.usage_usec = 0
    assert original.upload.delete() is True
    assert owner.close() is True


async def test_cpu_is_actual_cumulative_kernel_accounting_after_setsid_child_reaped(native):
    original = native.worker()
    owner = native.owner()
    scope, record = native.scope(owner, original)
    prepared = await original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
    child = await original.worker.start_prepared(prepared)
    await _line(child.channel)
    before = scope.snapshot()
    await child.channel.send_chunk(b"WORK\n")
    completed = json.loads(await _line(child.channel))
    assert completed["worked"] > 0 and completed["status"] == 0
    scope.request_freeze()
    await _positive(lambda: _events(record.path)["frozen"] == 1)
    first, second = scope.snapshot(), scope.snapshot()
    assert first.frozen is second.frozen is True
    assert first.usage_usec == second.usage_usec == _cpu(record.path)
    assert first.usage_usec > before.usage_usec
    assert second.observed_boottime_ns >= first.observed_boottime_ns >= before.observed_boottime_ns
    scope.request_thaw()
    await _positive(lambda: _events(record.path)["frozen"] == 0)
    await child.channel.send_chunk(b"PING\n")
    assert json.loads(await _line(child.channel)) == {"pong": "PING"}


async def test_group_kill_joins_setsid_originals_before_spool_and_scope_retirement(native):
    original = native.worker()
    owner = native.owner(capacity=1)
    scope, record = native.scope(owner, original)
    prepared = await original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
    child = await original.worker.start_prepared(prepared)
    await _line(child.channel)
    await child.channel.send_chunk(b"DESCENDANT\n")
    descendant = json.loads(await _line(child.channel))["descendant"]
    descendant_fd = os.pidfd_open(descendant)
    native.extra_fds.append(descendant_fd)
    membership = Path(f"/proc/{descendant}/cgroup").read_text().strip().split("0::", 1)[1]
    assert (Path("/sys/fs/cgroup") / membership.lstrip("/")).samefile(record.path)
    assert os.getsid(descendant) == descendant
    native.harness.hold_dependencies = True
    scope.request_kill()
    _refuses(scope.request_thaw)
    waiter = asyncio.create_task(original.worker.settle(original.launch))
    await asyncio.wait_for(native.harness.dependency_entered.wait(), _WAIT)
    try:
        assert original.upload.delete() is False
        assert owner.close() is False
        probe = native.worker(2)
        _refuses(lambda: owner.create(probe.launch))
    finally:
        native.harness.release_dependencies.set()
    assert (await asyncio.wait_for(waiter, _WAIT)).state == "settled"
    assert await asyncio.wait_for(native.harness.processes[-1].wait(), _WAIT) is not None
    poller = select.poll()
    poller.register(descendant_fd, select.POLLIN)
    assert poller.poll(0), "actual descendant pidfd must report exit"
    assert _events(record.path)["populated"] == 0
    final = scope.snapshot()
    kernel_final = _cpu(record.path)
    assert final.usage_usec == kernel_final
    retired = scope.close_after_settle(original.launch)
    assert retired.usage_usec >= kernel_final
    assert original.upload.delete() is True
    assert owner.close() is True


async def test_foreign_copy_subclass_and_changed_scope_replay_do_not_select_original(native):
    first, second = native.worker(1), native.worker(2)
    owner = native.owner()
    scope, _ = native.scope(owner, first)
    other, _ = native.scope(owner, second)
    try:
        copied = copy.copy(scope)
    except (TypeError, ValueError):
        copied = None
    if copied is not None and copied is not scope:
        _refuses(copied.snapshot)
        await _async_refuses(first.worker.prepare(first.launch, first.grant, cgroup_scope=copied))
    try:

        class Forged(type(scope)):
            pass
    except TypeError:
        pass
    else:
        forged = object.__new__(Forged)
        await _async_refuses(first.worker.prepare(first.launch, first.grant, cgroup_scope=forged))
    await _async_refuses(first.worker.prepare(first.launch, first.grant, cgroup_scope=other))
    try:
        copied_launch = copy.copy(first.launch)
    except (TypeError, ValueError):
        copied_launch = None
    if copied_launch is not None and copied_launch is not first.launch:
        _refuses(lambda: owner.create(copied_launch))
    try:

        class ForgedLaunch(type(first.launch)):
            pass
    except TypeError:
        pass
    else:
        _refuses(lambda: owner.create(object.__new__(ForgedLaunch)))
    prepared = await first.worker.prepare(first.launch, first.grant, cgroup_scope=scope)
    await _async_refuses(first.worker.prepare(first.launch, first.grant, cgroup_scope=other))
    await _async_refuses(first.worker.prepare(first.launch, first.grant))
    await _async_refuses(first.worker.start(first.launch, first.grant))
    scope.fence()
    _refuses(scope.request_thaw)
    await _async_refuses(first.worker.start_prepared(prepared))
    assert not first.marker.exists()
    assert len(native.harness.processes) == 1


async def test_c1_fence_joins_already_dispatched_original_before_releasing_custody(native):
    original = native.worker()
    owner = native.owner(capacity=1)
    scope, record = native.scope(owner, original)
    ready_r, ready_w = os.pipe2(os.O_CLOEXEC)
    gate_r, gate_w = os.pipe2(os.O_CLOEXEC)
    native.extra_fds.extend([ready_r, ready_w, gate_r, gate_w])
    original_spawn = native.harness.native_spawn

    async def gated(*args, **kwargs):
        passed = tuple(kwargs.get("pass_fds", ()))
        kwargs["pass_fds"] = passed + (ready_w, gate_r)
        return await original_spawn(
            sys.executable,
            "-I",
            "-B",
            "-c",
            _GATED_BOOTSTRAP,
            str(ready_w),
            str(gate_r),
            *args,
            **kwargs,
        )

    native.harness.native_spawn = gated
    native.harness.hold_spawn = True
    observer = asyncio.create_task(
        original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
    )
    try:
        await asyncio.wait_for(native.harness.spawned.wait(), _WAIT)
        assert await asyncio.wait_for(asyncio.to_thread(os.read, ready_r, 1), _WAIT) == b"R"
        assert not original.marker.exists()
        assert _events(record.path)["populated"] == 0
        scope.fence()
        assert original.upload.delete() is False
        _refuses(lambda: scope.close_after_settle(original.launch))
        assert owner.close() is False
    finally:
        os.write(gate_w, b"G")
        native.harness.release_spawn.set()
        await asyncio.wait_for(asyncio.gather(observer, return_exceptions=True), _WAIT)
    await _async_refuses(observer)
    outcome = await original.worker.settle(original.launch)
    assert len(native.harness.processes) == 1
    assert await asyncio.wait_for(native.harness.processes[0].wait(), _WAIT) is not None
    assert not original.marker.exists()
    assert _events(record.path)["populated"] == 0
    assert _cpu(record.path) > 0, "the inherited SELF enrollment must remain historically valid"
    if outcome.state == "settled":
        scope.close_after_settle(original.launch)
        assert original.upload.delete() is True
    else:
        assert outcome.state == "unknown"
        _refuses(lambda: scope.close_after_settle(original.launch))
        assert original.upload.delete() is False and owner.close() is False


async def test_fence_after_real_enrollment_borrow_prevents_new_dispatch(native, monkeypatch):
    original = native.worker()
    owner = native.owner(capacity=1)
    scope, record = native.scope(owner, original)
    real_borrow, hits = type(scope)._borrow_enrollment_fd, []

    def fenced_borrow(selected, launch):
        descriptor = real_borrow(selected, launch)
        assert selected is scope and launch is original.launch
        hits.append(descriptor)
        # Source-informed interposition: the real borrow completed, then a
        # direct negative fence wins before the original dispatch boundary.
        scope.fence()
        return descriptor

    with monkeypatch.context() as patch:
        patch.setattr(type(scope), "_borrow_enrollment_fd", fenced_borrow)
        await _async_refuses(
            original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
        )
    assert len(hits) == 1
    assert not native.harness.processes and not original.marker.exists()
    assert _events(record.path)["populated"] == 0
    outcome = await original.worker.settle(original.launch)
    if outcome.state == "settled":
        scope.close_after_settle(original.launch)
        assert original.upload.delete() is True
    else:
        assert outcome.state == "unknown"
        assert original.upload.delete() is False and owner.close() is False


async def test_c2_expired_original_scoped_deadline_is_not_renewed_on_late_spawn_replay(native):
    original = native.worker(startup=0.1)
    owner = native.owner()
    scope, _ = native.scope(owner, original)
    native.harness.hold_spawn = True
    observers = [
        asyncio.create_task(
            original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
        )
    ]
    try:
        await asyncio.wait_for(native.harness.spawned.wait(), _WAIT)
        expired = asyncio.Event()
        loop = asyncio.get_running_loop()
        timer = loop.call_at(loop.time() + 0.2, expired.set)
        try:
            await asyncio.wait_for(expired.wait(), _WAIT)
            observers.append(
                asyncio.create_task(
                    original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
                )
            )
        finally:
            timer.cancel()
    finally:
        native.harness.release_spawn.set()
        await asyncio.wait_for(asyncio.gather(*observers, return_exceptions=True), _WAIT)
    for observer in observers:
        await _async_refuses(observer)
    await _async_refuses(
        original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
    )
    outcome = await original.worker.settle(original.launch)
    assert outcome.state in {"settled", "unknown"}
    assert len(native.harness.processes) == 1
    assert await asyncio.wait_for(native.harness.processes[0].wait(), _WAIT) is not None
    assert not original.marker.exists()
    if outcome.state == "unknown":
        assert original.upload.delete() is False and owner.close() is False


async def test_existing_matching_leaf_is_unresolved_not_adopted_or_refunded(native):
    leaf = native.root / ("kinaiya-media-" + "e" * 32)
    assert not leaf.exists(), "fixture name must be fresh"
    before = set(native.root.iterdir())
    leaf.mkdir()
    native.track_new(before)
    stamp = leaf.stat()
    try:
        owner = native.owner(capacity=1)
    except MediaCgroupError as error:
        assert error.code in _CODES
    else:
        original = native.worker()
        _refuses(lambda: owner.create(original.launch))
        assert owner.close() is False
    assert leaf.exists() and leaf.stat().st_ino == stamp.st_ino
    assert not native.harness.processes


async def test_uncertain_mkdir_is_retained_for_same_original_without_replacement(
    native, monkeypatch
):
    original, another, third = native.worker(1), native.worker(2), native.worker(3)
    owner = native.owner(capacity=2)
    native_mkdir, hits = os.mkdir, []

    def uncertain(path, *args, **kwargs):
        result = native_mkdir(path, *args, **kwargs)
        if _LEAF.fullmatch(Path(os.fsdecode(path)).name):
            hits.append(path)
            raise OSError(errno.EIO, "synthetic post-acquisition ambiguity")
        return result

    before = set(native.root.iterdir())
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "mkdir", uncertain)
            _refuses(lambda: owner.create(original.launch))
    finally:
        fresh = native.track_new(before)
    assert len(hits) == len(fresh) == 1
    original_leaf = {path.name: (path.stat().st_dev, path.stat().st_ino) for path in fresh}
    replay = None
    try:
        replay = owner.create(original.launch)
    except MediaCgroupError as error:
        assert error.code in _CODES
        assert str(error) == "Invalid native media cgroup operation"
    else:
        assert owner.create(original.launch) is replay
        _refuses(replay.snapshot)
        _refuses(replay.request_thaw)
        await _async_refuses(
            original.worker.prepare(original.launch, original.grant, cgroup_scope=replay)
        )
    assert {
        path.name: (path.stat().st_dev, path.stat().st_ino)
        for path in native.root.iterdir()
        if _LEAF.fullmatch(path.name)
    } == original_leaf
    assert len(hits) == 1 and not native.harness.processes
    # Per-acquisition uncertainty need not poison an unrelated free slot. If
    # that second slot is usable, the uncertain first still consumes its own.
    before_second = set(native.root.iterdir())
    try:
        second = owner.create(another.launch)
    except MediaCgroupError as error:
        assert error.code in _CODES
    else:
        assert second is not replay
        _refuses(lambda: owner.create(third.launch))
        assert len([path for path in native.root.iterdir() if _LEAF.fullmatch(path.name)]) == 2
    finally:
        # Track only newly created leaves even when acquisition itself raises.
        native.track_new(before_second)
    assert owner.close() is False
    assert original.upload.delete() is False
    assert not original.marker.exists() and not native.harness.processes


@pytest.mark.parametrize(
    "fault",
    [
        "negative",
        "duplicate",
        "missing",
        "regression",
        "unbounded",
        "boot",
        "event_duplicate",
        "event_missing",
        "event_nonboolean",
    ],
)
async def test_invalid_or_regressed_original_measurement_keeps_unknown_capacity(
    native, monkeypatch, fault
):
    original = native.worker()
    owner = native.owner(capacity=1)
    scope, record = native.scope(owner, original)
    prepared = await original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
    child = await original.worker.start_prepared(prepared)
    await _line(child.channel)
    await child.channel.send_chunk(b"WORK\n")
    assert json.loads(await _line(child.channel))["status"] == 0
    previous = scope.snapshot()
    assert previous.usage_usec > 0
    native_read, native_pread, hits = os.read, os.pread, []

    def corrupted(fd, raw):
        path = _fd_path(fd)
        if fault == "boot" and path == "/proc/sys/kernel/random/boot_id" and raw:
            hits.append(path)
            return (b"0" if raw[:1] != b"0" else b"1") + raw[1:]
        if fault.startswith("event_") and path == str(record.path / "cgroup.events") and raw:
            hits.append(path)
            return {
                "event_duplicate": b"populated 1\npopulated 1\nfrozen 0\n",
                "event_missing": b"populated 1\n",
                "event_nonboolean": b"populated 2\nfrozen 0\n",
            }[fault]
        if (
            fault not in {"boot", "event_duplicate", "event_missing", "event_nonboolean"}
            and path == str(record.path / "cpu.stat")
            and raw
        ):
            hits.append(path)
            return {
                "negative": b"usage_usec -1\n",
                "duplicate": b"usage_usec 1\nusage_usec 1\n",
                "missing": b"user_usec 0\nsystem_usec 0\n",
                "regression": b"usage_usec 0\n",
                "unbounded": b"usage_usec " + b"9" * 100 + b"\n",
            }[fault]
        return raw

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", lambda fd, size: corrupted(fd, native_read(fd, size)))
        patch.setattr(
            os, "pread", lambda fd, size, offset: corrupted(fd, native_pread(fd, size, offset))
        )
        _refuses(scope.snapshot)
    assert hits, "the selected OS read fault must actually reach the original measurement"
    _refuses(scope.request_thaw)
    assert owner.close() is False
    assert original.upload.delete() is False
    another = native.worker(2)
    _refuses(lambda: owner.create(another.launch))


async def test_root_fd_number_reuse_never_selects_or_closes_replacement(native, tmp_path):
    before = set()
    for number in os.listdir("/proc/self/fd"):
        try:
            os.fstat(int(number))
        except OSError:
            continue
        before.add(number)
    owner = native.owner(capacity=1)
    original = native.worker()
    scope, _ = native.scope(owner, original)
    root_stamp = native.root.stat()
    candidates = []
    for number in set(os.listdir("/proc/self/fd")) - before:
        if number.isdecimal():
            try:
                stamp = os.fstat(int(number))
            except OSError:
                continue
            if stat.S_ISDIR(stamp.st_mode) and (stamp.st_dev, stamp.st_ino) == (
                root_stamp.st_dev,
                root_stamp.st_ino,
            ):
                candidates.append(int(number))
    assert candidates, "the native original root directory FD must be retained"
    sentinel = tmp_path / "replacement-fd"
    sentinel.write_bytes(b"retained replacement")
    replacements = []
    try:
        for fd in candidates:
            os.close(fd)
            fresh = os.open(sentinel, os.O_RDONLY | os.O_CLOEXEC)
            if fresh != fd:
                os.dup2(fresh, fd, inheritable=False)
                os.close(fresh)
            replacements.append(fd)
        _refuses(scope.snapshot)
        assert owner.close() is False
        for fd in replacements:
            assert os.pread(fd, 64, 0) == b"retained replacement"
    finally:
        for fd in replacements:
            os.close(fd)


@pytest.mark.parametrize("component", ["root", "ancestor"])
async def test_symlink_to_otherwise_valid_current_service_is_not_an_original_root(
    native, tmp_path, component
):
    selected = tmp_path / "selected"
    target = native.root if component == "root" else native.root.parent
    selected.symlink_to(target, target_is_directory=True)
    path = selected if component == "root" else selected / native.root.name
    _refuses(lambda: MediaCgroupPool(path))
    assert not native.harness.processes


async def test_recreated_named_leaf_does_not_select_original_fd_or_mutate_replacement(native):
    original = native.worker()
    owner = native.owner(capacity=1)
    scope, record = native.scope(owner, original)
    old = record.path
    empty = scope.snapshot()
    assert empty.populated is False
    assert (empty.device, empty.inode) == record.stamp
    old.rmdir()
    # Retain the old kill-FD owner, but record only this positively observed
    # external removal. The replacement gets its own independent record below.
    record.external_rmdir = record.stamp
    record.path = None
    before = set(native.root.iterdir())
    try:
        old.mkdir()
    finally:
        fresh = native.track_new(before)
    assert fresh == [old]
    assert old.stat().st_ino != record.stamp[1]
    _refuses(scope.snapshot)
    _refuses(scope.request_freeze)
    assert _events(old)["frozen"] == 0
    assert owner.close() is False
    assert not original.marker.exists() and not native.harness.processes


async def test_forked_process_cannot_become_custodian_of_parent_scope(native):
    owner = native.owner()
    original = native.worker()
    scope, _ = native.scope(owner, original)
    child = os.fork()
    if child == 0:
        try:
            scope.snapshot()
        except MediaCgroupError as error:
            os._exit(0 if error.code in _CODES else 2)
        except BaseException:
            os._exit(3)
        os._exit(1)
    child_fd, reaped = None, None

    def observe_original_wait():
        nonlocal reaped
        if reaped is None:
            result = os.waitpid(child, os.WNOHANG)
            if result[0] == child:
                # Publish the actual reap synchronously before any yield. No
                # thread or background waiter can reap ahead of this record.
                reaped = result
        return reaped

    try:
        child_fd = os.pidfd_open(child)
        waited, status = await _positive(observe_original_wait)
        assert waited == child and os.waitstatus_to_exitcode(status) == 0
    finally:
        try:
            if reaped is None:
                try:
                    if child_fd is not None:
                        signal.pidfd_send_signal(child_fd, signal.SIGKILL)
                    else:
                        # pidfd acquisition failed before a waiter started.
                        # This direct child is still unreaped and owns its PID.
                        os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await _positive(observe_original_wait)
        finally:
            if child_fd is not None:
                os.close(child_fd)


async def test_ambiguous_original_close_retains_charge_without_reclosing_reused_fd(
    native, monkeypatch, tmp_path
):
    original, another = native.worker(1), native.worker(2)
    owner = native.owner(capacity=1)
    scope, record = native.scope(owner, original)
    assert (await original.worker.settle(original.launch)).state == "settled"
    sentinel = tmp_path / "close-replacement"
    sentinel.write_bytes(b"never reclose")
    native_close, hits, replacement = os.close, [], []

    def ambiguous(fd):
        if not hits and _fd_path(fd).startswith(str(record.path)) and fd != record.kill_fd:
            hits.append(fd)
            native_close(fd)
            fresh = os.open(sentinel, os.O_RDONLY | os.O_CLOEXEC)
            if fresh != fd:
                os.dup2(fresh, fd, inheritable=False)
                native_close(fresh)
            replacement.append(fd)
            raise OSError(errno.EIO, "synthetic ambiguous original close")
        return native_close(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "close", ambiguous)
            _refuses(lambda: scope.close_after_settle(original.launch))
        assert hits, "one actual owned cgroup FD close must be ambiguous"
        _refuses(lambda: scope.close_after_settle(original.launch))
        _refuses(lambda: owner.create(another.launch))
        assert owner.close() is False
        assert os.pread(replacement[0], 64, 0) == b"never reclose"
    finally:
        for fd in replacement:
            native_close(fd)
