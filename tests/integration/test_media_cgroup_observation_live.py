"""R2 draft OS-boundary oracles; candidate production bodies remain unread.

Root must supply the sealed original live helper on the test import path and
an actual dedicated KINAIYA_TEST_CGROUP_ROOT. These tests inherit root's R2
finding. They establish no strong-reachability, Session or budget proof.
"""

import asyncio
import errno
import os
import re
import stat

import pytest
from test_media_cgroup_custody_live import (
    _WAIT,
    _async_refuses,
    _positive,
    _refuses,
)
from test_media_cgroup_custody_live import native as native
from test_media_processes import _binding

from packages.resources.media_cgroup import MediaCgroupPool
from packages.resources.media_processes import MediaProcessError

pytestmark = pytest.mark.live
_FAULTS = ("post_open_fstat", "ambiguous_close")
_SENTINEL = b"R2 replacement belongs to the test, never the observation owner"


def _observation_kind(path):
    if path == "/proc/sys/kernel/random/boot_id":
        return "boot"
    match = re.fullmatch(r"/proc/(self|[0-9]+)/(mountinfo|cgroup)", path)
    if match is None:
        return None
    own = match[1] in {"self", str(os.getpid())}
    if match[2] == "mountinfo":
        return "mount" if own else None
    return "self_proc" if own else "membership"


class _ObservationIO:
    """Actual OS fault seam, with original inode/FD and explicit HIT guards."""

    def __init__(self, root, selector=None, fault=None, sentinel=None):
        self.root = str(root)
        self.selector, self.fault, self.sentinel = selector, fault, sentinel
        self.open_os, self.fstat_os, self.close_os = os.open, os.fstat, os.close
        self.read_os, self.pread_os, self.write_os = os.read, os.pread, os.write
        self.readlink_os, self.dup2_os = os.readlink, os.dup2
        self.opens, self.active, self.directories = [], {}, []
        self.positive_closes, self.target = [], None
        self.hits, self.after_fault_opens, self.sentinel_touches = [], [], []
        self.injected = False
        self.sentinel_stamp = None
        self.max_live = 0

    def _path(self, fd):
        try:
            return self.readlink_os(f"/proc/self/fd/{fd}")
        except OSError:
            return ""

    @staticmethod
    def _stamp(value):
        return value.st_dev, value.st_ino

    def _touch(self, operation, fd):
        if (
            self.injected
            and self.fault == "ambiguous_close"
            and self.target is not None
            and fd == self.target["fd"]
        ):
            self.sentinel_touches.append(operation)

    def opened(self, path, flags, mode=0o777, *, dir_fd=None):
        fd = self.open_os(path, flags, mode, dir_fd=dir_fd)
        actual = self._path(fd)
        observed = self.fstat_os(fd)  # Independent kernel identity, not the fault.
        kind = _observation_kind(actual)
        if kind is not None:
            item = {"fd": fd, "stamp": self._stamp(observed), "path": actual, "kind": kind}
            self.opens.append(item)
            self.active[fd] = item
            self.max_live = max(self.max_live, len(self.active))
            if self.injected:
                self.after_fault_opens.append(item)
            elif self.target is None and self.selector == kind:
                self.target = item
        elif stat.S_ISDIR(observed.st_mode) and (
            actual == self.root or actual.startswith(self.root + "/")
        ):
            self.directories.append({"fd": fd, "stamp": self._stamp(observed)})
        return fd

    def fstat(self, fd):
        self._touch("fstat", fd)
        if (
            not self.injected
            and self.fault == "post_open_fstat"
            and self.target is not None
            and fd == self.target["fd"]
            and self.active.get(fd) is self.target
        ):
            self.injected = True
            self.hits.append(("fstat", fd))
            raise OSError(errno.EIO, "synthetic post-open fstat uncertainty")
        return self.fstat_os(fd)

    def close(self, fd):
        self._touch("close", fd)
        if (
            not self.injected
            and self.fault == "ambiguous_close"
            and self.target is not None
            and fd == self.target["fd"]
            and self.active.get(fd) is self.target
        ):
            self.close_os(fd)  # The original is really consumed.
            self.active.pop(fd)
            fresh = self.open_os(self.sentinel, os.O_RDONLY | os.O_CLOEXEC)
            if fresh != fd:
                self.dup2_os(fresh, fd, inheritable=False)
                self.close_os(fresh)
            self.sentinel_stamp = self._stamp(self.fstat_os(fd))
            self.injected = True
            self.hits.append(("close", fd))
            raise OSError(errno.EIO, "synthetic close consumed before error")
        result = self.close_os(fd)
        item = self.active.pop(fd, None)
        if item is not None:
            self.positive_closes.append(item)
        return result

    def read(self, fd, size):
        self._touch("read", fd)
        return self.read_os(fd, size)

    def pread(self, fd, size, offset):
        self._touch("pread", fd)
        return self.pread_os(fd, size, offset)

    def write(self, fd, data):
        self._touch("write", fd)
        return self.write_os(fd, data)

    def install(self, patch):
        for name in ("open", "fstat", "close", "read", "pread", "write"):
            patch.setattr(os, name, self.opened if name == "open" else getattr(self, name))

    def assert_fault_and_original(self):
        assert self.hits, (
            f"selected {self.selector}/{self.fault} OS seam did not HIT; "
            "an unexercised fixture is not a demonstrated production result"
        )
        assert len(self.hits) == 1
        assert self.target is not None
        fd = self.target["fd"]
        actual = self._stamp(self.fstat_os(fd))
        if self.fault == "post_open_fstat":
            assert actual == self.target["stamp"], "returned original FD was lost"
            assert self.active.get(fd) is self.target
            assert not any(item is self.target for item in self.positive_closes)
        else:
            assert actual == self.sentinel_stamp
            assert self.pread_os(fd, len(_SENTINEL) + 1, 0) == _SENTINEL
            assert not self.sentinel_touches, (
                "an ambiguous consumed integer was retried as the original"
            )
        assert not self.after_fault_opens, "UNKNOWN dispatched another observation"

    def cleanup_injected_fd(self):
        """Test-only FD cleanup; never a candidate retirement/refund assertion."""
        if not self.injected or self.target is None:
            return
        fd = self.target["fd"]
        expected = self.sentinel_stamp if self.fault == "ambiguous_close" else self.target["stamp"]
        try:
            actual = self._stamp(self.fstat_os(fd))
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            if actual == expected:
                self.close_os(fd)


def _sentinel(tmp_path):
    path = tmp_path / "r2-observation-replacement"
    path.write_bytes(_SENTINEL)
    return path


def _unknown(owner, another, trace, scope=None):
    before = len(trace.opens)
    for _ in range(2):
        _refuses(lambda: owner.create(another.launch))
        assert owner.close() is False
        if scope is not None:
            _refuses(scope.snapshot)
            _refuses(scope.request_thaw)
    assert len(trace.opens) == before, "unknown observation owner was reused"
    trace.assert_fault_and_original()


@pytest.mark.parametrize("selector", ("boot", "mount", "self_proc"))
@pytest.mark.parametrize("fault", _FAULTS)
def test_constructor_observation_fault_retains_exact_unknown_owner(
    native, monkeypatch, tmp_path, selector, fault
):
    original = native.worker()
    trace = _ObservationIO(native.root, selector, fault, _sentinel(tmp_path))
    partial = []
    init = MediaCgroupPool.__init__

    def capture(instance, *args, **kwargs):
        partial.append(instance)
        init(instance, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            trace.install(patch)
            patch.setattr(MediaCgroupPool, "__init__", capture)
            _refuses(lambda: MediaCgroupPool(native.root, capacity=2))
            assert len(partial) == 1
            owner = partial[0]
            assert type(owner) is MediaCgroupPool
            native.pools.append(owner)
            _unknown(owner, original, trace)
            # The actual directory-close pattern is not prescribed. Even if
            # all directories closed positively, close() above must stay False.
            # This external partial reference does NOT prove production keeps
            # a strong reference after a constructor exception.
    finally:
        trace.cleanup_injected_fd()


@pytest.mark.parametrize("operation", ("snapshot", "request_freeze", "request_thaw"))
@pytest.mark.parametrize("fault", _FAULTS)
async def test_later_identity_observation_fault_never_renews_or_false_closes(
    native, monkeypatch, tmp_path, operation, fault
):
    original, another = native.worker(1), native.worker(2)
    owner = native.owner(capacity=2)
    scope, _ = native.scope(owner, original)
    assert scope.snapshot().populated is False
    if operation == "request_thaw":
        scope.request_freeze()
        await _positive(lambda: scope.snapshot().frozen is True)
    trace = _ObservationIO(native.root, "boot", fault, _sentinel(tmp_path))
    try:
        with monkeypatch.context() as patch:
            trace.install(patch)
            _refuses(getattr(scope, operation))
            _unknown(owner, another, trace, scope)
    finally:
        trace.cleanup_injected_fd()


@pytest.mark.parametrize("fault", _FAULTS)
async def test_actual_held_membership_fault_keeps_original_process_and_grant(
    native, monkeypatch, tmp_path, fault
):
    original, another = native.worker(1), native.worker(2)
    owner = native.owner(capacity=2)
    scope, _ = native.scope(owner, original)
    trace = _ObservationIO(native.root, "membership", fault, _sentinel(tmp_path))
    try:
        with monkeypatch.context() as patch:
            trace.install(patch)
            await _async_refuses(
                original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
            )
            trace.assert_fault_and_original()
            assert len(native.harness.processes) == 1
            process = native.harness.processes[0]
            assert not original.marker.exists()
            assert original.upload.delete() is False
            _unknown(owner, another, trace, scope)
            await _async_refuses(
                original.worker.prepare(original.launch, original.grant, cgroup_scope=scope)
            )
            assert len(native.harness.processes) == 1
            trace.assert_fault_and_original()
            # Join the actual original without treating its exit as observation
            # cleanup. The fixture retains its own separate emergency teardown.
            outcome = await asyncio.wait_for(original.worker.settle(original.launch), _WAIT)
            assert outcome.state == "unknown"
            assert await asyncio.wait_for(process.wait(), _WAIT) is not None
            assert not original.marker.exists()
            assert original.upload.delete() is False
            _refuses(lambda: scope.close_after_settle(original.launch))
            # The default fixture's original is _binding(1). A new child for
            # that same operation cannot reuse the still-charged process slot.
            with pytest.raises(MediaProcessError) as failure:
                original.worker.register(_binding(3, operation_id=_binding(1).operation_id))
            assert failure.value.code == "capacity"
            assert owner.close() is False
            trace.assert_fault_and_original()
    finally:
        trace.cleanup_injected_fd()


async def test_positive_prior_close_allows_one_bounded_reusable_observation(native, monkeypatch):
    first, second = native.worker(1), native.worker(2)
    trace = _ObservationIO(native.root)
    with monkeypatch.context() as patch:
        trace.install(patch)
        owner = native.owner(capacity=2)
        scope, _ = native.scope(owner, first)
        assert scope.snapshot().populated is False
        scope.request_freeze()
        await _positive(lambda: scope.snapshot().frozen is True)
        scope.request_thaw()
        await _positive(lambda: scope.snapshot().frozen is False)
        other, _ = native.scope(owner, second)
        assert other.snapshot().populated is False
        assert owner.create(first.launch) is scope
        assert owner.create(second.launch) is other
        assert trace.max_live == 1
        assert len(trace.opens) >= 3
        assert {item["kind"] for item in trace.opens} >= {
            "boot",
            "mount",
            "self_proc",
        }
        assert len(trace.active) <= 1
        assert len(trace.positive_closes) + len(trace.active) == len(trace.opens)
