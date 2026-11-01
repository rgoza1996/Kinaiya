"""A failed close must retain actual cleanup authority for the same manager."""

import asyncio
import json
import os
import select
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from packages.resources import WorkerManager, WorkerSpec

REAL_SEND = signal.pidfd_send_signal


def _stat(pid):
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields = raw[raw.rfind(")") + 2 :].split()
    return fields[0], int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])


def _bound_pid(fd):
    if fd is None:
        return None
    try:
        lines = Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
    except OSError:
        return None
    return next(int(line.split()[1]) for line in lines if line.startswith("Pid:"))


def _group_live(pgid):
    members = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            state, _, group, _, _ = _stat(int(path.name))
        except (OSError, ValueError, IndexError):
            continue
        if group == pgid and state not in {"Z", "X", "x"}:
            members.append(int(path.name))
    return members


@dataclass
class OwnedChild:
    worker_id: str
    process: asyncio.subprocess.Process
    pidfd: int
    start_time: int
    native_fd: int
    native_fd_inode: int
    pipes: tuple


def _retain_child(manager, worker_id):
    identity = manager.owned_identity(worker_id)
    assert identity is not None
    managed = manager._workers[worker_id]
    process = managed.process
    assert process is not None and process.returncode is None
    before = _stat(process.pid)
    assert before[0] not in {"Z", "X", "x"}
    assert before[1:] == (os.getpid(), process.pid, process.pid, identity.start_time)
    assert managed.leader_fd is not None
    native_fd = managed.leader_fd
    assert _bound_pid(native_fd) == process.pid
    # Duplication preserves the manager's already-verified kernel identity; it
    # never opens a new handle from a numeric PID or signals a discovered group.
    pidfd = os.dup(native_fd)
    try:
        assert _bound_pid(pidfd) == process.pid
        after = _stat(process.pid)
        assert after[0] not in {"Z", "X", "x"}
        assert after[1:] == before[1:]
        pipes = tuple(process._transport.get_pipe_transport(fd) for fd in (1, 2))
        assert all(pipe is not None for pipe in pipes)
        return OwnedChild(
            worker_id,
            process,
            pidfd,
            identity.start_time,
            native_fd,
            os.fstat(native_fd).st_ino,
            pipes,
        )
    except BaseException:
        os.close(pidfd)
        raise


async def _assert_reaped(manager, child):
    await asyncio.wait_for(child.process.wait(), timeout=3)
    assert child.process.returncode is not None
    assert select.select([child.pidfd], [], [], 0)[0] == [child.pidfd]
    assert _group_live(child.process.pid) == []
    with pytest.raises(ChildProcessError):
        os.waitpid(child.process.pid, os.WNOHANG)
    managed = manager._workers[child.worker_id]
    evidence = managed.stop_evidence
    assert evidence is not None
    assert evidence["pid"] == child.process.pid
    assert evidence["reaped"] is True
    assert evidence["group_empty"] is True
    assert evidence["ownership_verified"] is True
    assert evidence["residual_pids"] == []
    assert managed.state == "stopped" and managed.ready is False
    assert managed.pgid is None
    assert managed.leader_fd is None and managed.member_fds == {}
    assert managed.pending_spawn is None and managed.pending_cleanup is None
    assert managed.drains == []
    assert all(pipe.is_closing() for pipe in child.pipes)


@pytest.fixture
async def two_owned_workers(tmp_path, record_property):
    specs = [
        WorkerSpec(
            id=worker_id,
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; import sys,time; "
                "Path(sys.argv[1]).write_text('ready'); time.sleep(60)",
                str(tmp_path / f"{worker_id}.ready"),
            ),
            cwd=tmp_path,
            stop_timeout_s=0.2,
        )
        for worker_id in ("a", "b")
    ]

    async def ready(spec, process):
        return (tmp_path / f"{spec.id}.ready").is_file()

    manager = WorkerManager(specs, readiness=ready)
    children = []
    try:
        for spec in specs:
            started = await asyncio.wait_for(manager.start(spec.id), timeout=8)
            assert started["ok"] is True
            children.append(_retain_child(manager, spec.id))
        record_property(
            "owned_children",
            json.dumps(
                [
                    {"worker": child.worker_id, "pid": child.process.pid, "start": child.start_time}
                    for child in children
                ]
            ),
        )
        yield manager, children
    finally:
        # The oracle owns these verified duplicates independently of manager
        # bookkeeping. A red close must not leak its disposable child.
        try:
            for child in children:
                if child.process.returncode is None:
                    try:
                        REAL_SEND(child.pidfd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await asyncio.wait_for(child.process.wait(), timeout=5)
                assert child.process.returncode is not None
                assert _group_live(child.process.pid) == []
            await asyncio.wait_for(manager.close(), timeout=10)
        finally:
            for child in children:
                for pipe in child.pipes:
                    pipe.close()
                os.close(child.pidfd)
            await asyncio.sleep(0)
        record_property("fixture_cleanup", "all actual children reaped; independent pidfds closed")


async def test_failed_close_retains_native_ownership_and_same_manager_can_retry(
    two_owned_workers, monkeypatch, record_property
):
    manager, children = two_owned_workers
    first, second = children
    injected = []

    def fail_first_signal(fd, sig):
        if sig and not injected and _bound_pid(fd) == first.process.pid:
            injected.append({"pid": _bound_pid(fd), "signal": int(sig)})
            raise RuntimeError("synthetic first owned pidfd signal failure")
        return REAL_SEND(fd, sig)

    with monkeypatch.context() as patch:
        patch.setattr(signal, "pidfd_send_signal", fail_first_signal)
        with pytest.raises(RuntimeError, match="close incomplete"):
            await asyncio.wait_for(manager.close(), timeout=10)
        assert len(injected) == 1
        assert first.process.returncode is None
        assert select.select([first.pidfd], [], [], 0)[0] == []
        await _assert_reaped(manager, second)
        managed = manager._workers[first.worker_id]
        retained = {
            "leader_fd": managed.leader_fd,
            "bound_pid": _bound_pid(managed.leader_fd),
            "start_time": managed.leader_starttime,
            "granted": managed.granted,
            "pgid": managed.pgid,
            "same_native_handle": managed.leader_fd is not None
            and os.fstat(managed.leader_fd).st_ino == first.native_fd_inode,
        }

    # Always exercise the second close before asserting retention, so the red
    # baseline records both the lost authority and the resulting failed retry.
    retry_error = None
    try:
        await asyncio.wait_for(manager.close(), timeout=10)
    except RuntimeError as exc:
        retry_error = str(exc)
    record_property("injected_failure", json.dumps(injected))
    record_property("first_close_retained_owner", json.dumps(retained, sort_keys=True))
    record_property("same_manager_retry_error", retry_error or "none")
    assert retained["bound_pid"] == first.process.pid
    assert retained["same_native_handle"] is True
    assert retained["start_time"] == first.start_time
    assert retained["granted"] is True
    assert retained["pgid"] == first.process.pid
    assert retry_error is None
    for child in children:
        await _assert_reaped(manager, child)


async def test_all_stopped_close_is_repeatable_and_releases_actual_handles(two_owned_workers):
    manager, children = two_owned_workers
    await asyncio.wait_for(manager.close(), timeout=10)
    for child in children:
        await _assert_reaped(manager, child)
    await asyncio.wait_for(manager.close(), timeout=10)
    for child in children:
        await _assert_reaped(manager, child)
