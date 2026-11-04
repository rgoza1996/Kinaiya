"""Requirements-first refusals; ordinary files are never a native cgroup.

Authored without candidate source exposure. No Segment budget, Session or
qualified-runtime claim follows from this module. Root executes it separately.
"""

import asyncio
import fcntl
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from test_media_processes import _PROGRAM, _binding, _limits
from test_media_processes import harness as harness

from packages.resources.media_cgroup import MediaCgroupError, MediaCgroupPool
from packages.resources.media_processes import MediaWorkerPool
from packages.resources.processes import WorkerSpec

_CODES = {"bad_input", "unavailable", "conflict", "capacity", "expired", "unknown"}
_MESSAGE = "Invalid native media cgroup operation"


def _refuses(call):
    with pytest.raises(MediaCgroupError) as failure:
        call()
    assert isinstance(failure.value, ValueError)
    assert failure.value.code in _CODES
    assert str(failure.value) == _MESSAGE


def _fake(root):
    root.mkdir()
    (root / "supervisor").mkdir()
    for name, content in {
        "cgroup.procs": b"",
        "cgroup.controllers": b"cpu memory pids\n",
        "cgroup.subtree_control": b"cpu\n",
        "cpu.stat": b"usage_usec 0\nuser_usec 0\nsystem_usec 0\n",
        "cgroup.events": b"populated 0\nfrozen 0\n",
        "cgroup.freeze": b"0\n",
        "cgroup.kill": b"",
    }.items():
        (root / name).write_bytes(content)
    return root


def _tree(root):
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("capacity", [1, 2, 0, -1, 3, True, 1.0, None, "2"])
def test_ordinary_filesystem_with_cgroup_names_never_becomes_custody(tmp_path, capacity):
    root = _fake(tmp_path / "untrusted-native-detail")
    before = _tree(root)
    _refuses(lambda: MediaCgroupPool(root, capacity=capacity))
    assert _tree(root) == before


@pytest.mark.parametrize("component", ["root", "ancestor"])
def test_symlinked_root_components_refuse_without_mutating_target(tmp_path, component):
    actual = _fake(tmp_path / "actual")
    if component == "root":
        selected = tmp_path / "selected"
        selected.symlink_to(actual, target_is_directory=True)
    else:
        link = tmp_path / "ancestor"
        link.symlink_to(tmp_path, target_is_directory=True)
        selected = link / "actual"
    before = _tree(actual)
    _refuses(lambda: MediaCgroupPool(selected))
    assert _tree(actual) == before


def test_real_mount_root_is_not_a_dedicated_supervisor_service():
    _refuses(lambda: MediaCgroupPool(Path("/sys/fs/cgroup")))


async def test_default_none_preparation_does_not_acquire_new_scoped_startup_timer(harness):
    marker = harness.root / "unscoped-marker"
    spec = WorkerSpec(
        id="unscoped",
        argv=(sys.executable, "-B", "-c", _PROGRAM, str(marker)),
        cwd=harness.root,
        startup_timeout_s=0.1,
        stop_timeout_s=0.1,
    )
    worker = MediaWorkerPool(
        spec, _limits(), current=harness.current, retire_dependencies=harness.retire
    )
    harness.pools.append(worker)
    binding = _binding()
    launch = harness.register(worker, binding)
    _, grant = harness.grant(launch, binding)
    harness.hold_spawn = True
    observer = asyncio.create_task(worker.prepare(launch, grant))
    try:
        await asyncio.wait_for(harness.spawned.wait(), 10)
        elapsed = asyncio.Event()
        loop = asyncio.get_running_loop()
        timer = loop.call_at(loop.time() + 0.2, elapsed.set)
        try:
            await asyncio.wait_for(elapsed.wait(), 10)
            assert not observer.done(), "old None path must keep observing its held original"
        finally:
            timer.cancel()
    finally:
        harness.release_spawn.set()
        await asyncio.wait_for(asyncio.gather(observer, return_exceptions=True), 10)
    prepared = await observer
    assert await worker.prepare(launch, grant, cgroup_scope=None) is prepared
    assert len(harness.processes) == 1 and not marker.exists()
    assert (await worker.settle(launch)).state == "settled"
    assert await asyncio.wait_for(harness.processes[0].wait(), 10) is not None


@pytest.mark.parametrize("kind", ["ordinary", "pipe", "duplicate", "wrong_tag", "extra_arg"])
def test_bootstrap_rejects_untrusted_enrollment_before_waiting_for_grant(tmp_path, kind):
    _bootstrap_refuses(tmp_path, kind)


def _bootstrap_refuses(tmp_path, kind, *, actual_control=None):
    # This helper starts the real bootstrap directly, keeping the grant writer
    # OPEN. Actual exit therefore proves refusal preceded grant/target execution.
    # Its extra FD is not an actual cgroup, even when named cgroup.procs.
    script = Path(__file__).resolve().parents[2] / "packages/resources/_media_bootstrap.py"
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
