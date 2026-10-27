"""Boundary regressions for owner-controlled managed workers (GitHub #9).

Spawns only disposable synthetic Python workers plus one disposable sentinel
(explicitly authorized fixture execution) — never real user PIDs, never the
real voice/Meta/Strata workers. PID reuse and kernel races are simulated
honestly with narrow fault injection at the identity boundary; no test claims
actual PID reuse was observed.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from packages.resources import WorkerManager, WorkerSpec
from packages.resources import processes as procmod

PY = sys.executable

SLEEP_CHILD = "import time\ntime.sleep(30)\n"

SPLIT_SECRET_CHILD = """\
import os, sys, time
secret = os.environ["SYNTH_SECRET"]
first, second = secret[: len(secret) // 2], secret[len(secret) // 2 :]
sys.stdout.write(first[: len(first) // 2])
sys.stdout.flush()
sys.stderr.write(first[len(first) // 2 :])
sys.stderr.flush()
sys.stdout.write(second[: len(second) // 2])
sys.stdout.flush()
sys.stderr.write(second[len(second) // 2 :])
sys.stderr.flush()
while True:
    time.sleep(0.2)
"""

SYNTH_SECRET = "synth-boundary-secret-" + "0123456789abcdef" * 12


def write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def make_spec(wid: str, argv: list[str], cwd: Path, **kwargs) -> WorkerSpec:
    return WorkerSpec(id=wid, argv=tuple(argv), cwd=cwd, **kwargs)


async def ready_now(spec, proc):
    return True


def file_probe(path: Path):
    async def probe(spec, proc):
        return path.exists()

    return probe


def group_live(pgid: int) -> list[int]:
    """Live (non-zombie) pids currently in pgid; /proc races yield skips."""
    live = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return live
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                raw = fh.read().decode("ascii", "replace")
        except OSError:
            continue
        cut = raw.rfind(")")
        parts = raw[cut + 2 :].split()  # state(3) ppid(4) pgrp(5) ...
        if len(parts) >= 20 and parts[0] not in ("Z", "X", "x") and int(parts[2]) == pgid:
            live.append(int(entry))
    return live


async def wait_until(predicate, timeout_s: float = 5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


@pytest.fixture
async def tracked():
    managers: list[WorkerManager] = []
    yield managers
    for manager in reversed(managers):
        try:
            await asyncio.wait_for(manager.close(), timeout=8.0)
        except (TimeoutError, RuntimeError):
            pass
        except Exception:
            pass


def track(tracked, manager: WorkerManager) -> WorkerManager:
    tracked.append(manager)
    return manager


def stop_sentinel(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


async def test_identity_loss_refuses_and_spares_sentinel(tracked, tmp_path, monkeypatch):
    script = write_script(tmp_path, "sleepy.py", SLEEP_CHILD)
    manager = track(
        tracked, WorkerManager([make_spec("w", [PY, str(script)], tmp_path)], readiness=ready_now)
    )
    started = await asyncio.wait_for(manager.start("w"), timeout=8.0)
    assert started["ok"]
    pid = started["pid"]
    pgid = manager.status()["workers"]["w"]["owned_pgid"]
    managed = manager._workers["w"]
    saved_starttime = managed.leader_starttime

    sentinel = subprocess.Popen([PY, "-c", SLEEP_CHILD])
    try:
        kill_targets: list[int] = []
        real_kill, real_killpg = os.kill, os.killpg

        def spy_kill(target, sig):
            kill_targets.append(target)
            return real_kill(target, sig)

        def spy_killpg(target, sig):
            kill_targets.append(target)
            return real_killpg(target, sig)

        monkeypatch.setattr(os, "kill", spy_kill)
        monkeypatch.setattr(os, "killpg", spy_killpg)

        bound_pids: list[int] = []
        real_send = signal.pidfd_send_signal

        def spy_send(fd, sig):
            try:
                with open(f"/proc/self/fdinfo/{fd}", "rb") as fh:
                    for line in fh.read().split(b"\n"):
                        if line.startswith(b"Pid:"):
                            bound_pids.append(int(line.split()[1]))
            except (OSError, ValueError, IndexError):
                pass
            return real_send(fd, sig)

        monkeypatch.setattr(signal, "pidfd_send_signal", spy_send)

        # Honest simulation (not observed reuse): the kernel record no longer
        # matches, and a stale numeric scan implicates the unrelated sentinel.
        managed.leader_starttime = (saved_starttime or 0) + 10**9
        real_scan = procmod._scan_group

        def spoofed_scan(target):
            members, ok = real_scan(target)
            if target == pgid:
                members = dict(members)
                members[sentinel.pid] = ("S", pgid, 0)
            return members, ok

        monkeypatch.setattr(procmod, "_scan_group", spoofed_scan)

        result = await asyncio.wait_for(manager.stop("w"), timeout=8.0)
        assert not result["ok"] and result["error"] == "ownership-unverified"
        assert sentinel.poll() is None  # sentinel never signalled
        assert sentinel.pid not in kill_targets
        assert sentinel.pid not in bound_pids
        entry = manager.status()["workers"]["w"]
        assert entry["state"] == "failed"  # refused: neither success nor unsafe signals

        # The uncleaned incarnation blocks restart: no new process is spawned.
        refused = await asyncio.wait_for(manager.start("w"), timeout=8.0)
        assert not refused["ok"] and refused["error"] == "ownership-unverified"
        assert manager.status()["workers"]["w"]["owned_pid"] == pid

        # Recovery: drop the spoof, restore identity, stop and start honestly.
        monkeypatch.undo()
        managed.leader_starttime = saved_starttime
        recovered = await asyncio.wait_for(manager.stop("w"), timeout=8.0)
        assert recovered["ok"], recovered
        restarted = await asyncio.wait_for(manager.start("w"), timeout=8.0)
        assert restarted["ok"] and restarted["pid"] != pid
    finally:
        stop_sentinel(sentinel)


async def test_close_identity_loss_spares_sentinel(tracked, tmp_path, monkeypatch):
    script = write_script(tmp_path, "sleepy.py", SLEEP_CHILD)
    manager = track(
        tracked, WorkerManager([make_spec("w", [PY, str(script)], tmp_path)], readiness=ready_now)
    )
    assert (await asyncio.wait_for(manager.start("w"), timeout=8.0))["ok"]
    managed = manager._workers["w"]
    saved_starttime, pgid = managed.leader_starttime, managed.pgid

    sentinel = subprocess.Popen([PY, "-c", SLEEP_CHILD])
    try:
        managed.leader_starttime = (saved_starttime or 0) + 10**9
        real_scan = procmod._scan_group

        def spoofed_scan(target):
            members, ok = real_scan(target)
            if target == pgid:
                members = dict(members)
                members[sentinel.pid] = ("S", pgid, 0)
            return members, ok

        monkeypatch.setattr(procmod, "_scan_group", spoofed_scan)
        with pytest.raises(RuntimeError, match="close incomplete"):
            await asyncio.wait_for(manager.close(), timeout=10.0)
        assert sentinel.poll() is None
        entry = manager.status()["workers"]["w"]
        assert entry["state"] == "failed" and entry["error"] == "ownership-unverified"
    finally:
        stop_sentinel(sentinel)
        managed.leader_starttime = saved_starttime


async def test_split_secret_absent_from_status(tracked, tmp_path):
    script = write_script(tmp_path, "split.py", SPLIT_SECRET_CHILD)
    manager = track(
        tracked,
        WorkerManager(
            [make_spec("w", [PY, str(script)], tmp_path, env={"SYNTH_SECRET": SYNTH_SECRET})],
            readiness=ready_now,
        ),
    )
    assert (await asyncio.wait_for(manager.start("w"), timeout=8.0))["ok"]
    # Prove the child really printed on both streams while status hides content.
    assert await wait_until(
        lambda: (
            manager.status()["workers"]["w"]["log_stdout_bytes"] > 0
            and manager.status()["workers"]["w"]["log_stderr_bytes"] > 0
        ),
        timeout_s=5.0,
    )
    entry = manager.status()["workers"]["w"]
    assert entry["log_tail_stdout"] == "" and entry["log_tail_stderr"] == ""
    dump = json.dumps(manager.status())
    assert SYNTH_SECRET not in dump  # the actual printed synthetic secret
    assert "SYNTH_SECRET" not in dump
    assert PY not in dump
    assert "argv" not in entry and "env" not in entry and "cwd" not in entry
    assert (await asyncio.wait_for(manager.stop("w"), timeout=8.0))["ok"]


async def test_ready_after_exit_is_false(tracked, tmp_path):
    manager = track(
        tracked,
        WorkerManager([make_spec("plain", [PY, "-c", SLEEP_CHILD], tmp_path)], readiness=ready_now),
    )
    started = await asyncio.wait_for(manager.start("plain"), timeout=8.0)
    assert started["ok"] and started["ready"] is True
    os.kill(started["pid"], signal.SIGKILL)
    assert await wait_until(
        lambda: (
            manager._workers["plain"].process is not None
            and manager._workers["plain"].process.returncode is not None
        ),
        timeout_s=5.0,
    )
    entry = manager.status()["workers"]["plain"]
    assert entry["state"] == "failed" and entry["ready"] is False and entry["readiness"] == "failed"


async def test_repeated_stop_safe_without_reinspection(tracked, tmp_path, monkeypatch):
    script = write_script(tmp_path, "sleepy.py", SLEEP_CHILD)
    manager = track(
        tracked, WorkerManager([make_spec("w", [PY, str(script)], tmp_path)], readiness=ready_now)
    )
    assert (await asyncio.wait_for(manager.start("w"), timeout=8.0))["ok"]
    first = await asyncio.wait_for(manager.stop("w"), timeout=8.0)
    assert first["ok"] and first["stop_evidence"]["ownership_verified"] is True

    def boom(path, *args, **kwargs):
        raise OSError("simulated /proc loss")

    monkeypatch.setattr(os, "listdir", boom)
    again = await asyncio.wait_for(manager.stop("w"), timeout=8.0)
    assert again["ok"]  # idempotent with no inspection and no signals
    entry = manager.status()["workers"]["w"]
    assert entry["state"] == "stopped" and entry["group_rss_bytes"] is None


async def test_cancel_during_spawn_retains_cleanup_ownership(tracked, tmp_path, monkeypatch):
    manager = track(
        tracked,
        WorkerManager(
            [make_spec("w", [PY, "-c", SLEEP_CHILD], tmp_path, startup_timeout_s=10.0)],
            readiness=file_probe(tmp_path / "never"),
        ),
    )
    real_create = asyncio.create_subprocess_exec
    spawned: list[int] = []

    async def slow_spawn(*args, **kwargs):
        await asyncio.sleep(0.5)
        proc = await real_create(*args, **kwargs)
        spawned.append(proc.pid)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    task = asyncio.ensure_future(manager.start("w"))
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15.0)
    entry = manager.status()["workers"]["w"]
    assert entry["state"] == "failed" and entry["error"] == "cancelled"
    assert spawned, "spawn completed under the shield, so ownership was retained and cleaned"
    assert await wait_until(
        lambda: (
            manager._workers["w"].process is not None
            and manager._workers["w"].process.returncode is not None
        ),
        timeout_s=5.0,
    )
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)
    assert group_live(spawned[0]) == []


async def test_inaccessible_proc_fails_closed(tracked, tmp_path, monkeypatch):
    script = write_script(tmp_path, "sleepy.py", SLEEP_CHILD)
    manager = track(
        tracked, WorkerManager([make_spec("w", [PY, str(script)], tmp_path)], readiness=ready_now)
    )
    assert (await asyncio.wait_for(manager.start("w"), timeout=8.0))["ok"]

    def boom(path, *args, **kwargs):
        raise OSError("simulated /proc loss")

    monkeypatch.setattr(os, "listdir", boom)
    result = await asyncio.wait_for(manager.stop("w"), timeout=8.0)
    assert not result["ok"] and result["error"] == "ownership-unverified"
    assert manager.status()["workers"]["w"]["state"] == "failed"


def test_env_config_immutable(tmp_path):
    env = {"A": "1"}
    spec = make_spec("w", [PY, "-c", "pass"], tmp_path, env=env)
    env["PWN"] = "x"
    assert "PWN" not in spec.env
    with pytest.raises(TypeError):
        spec.env["B"] = "2"  # frozen: no caller may mutate spawn config
    snap = spec.child_env()
    snap["PWN2"] = "y"
    assert "PWN2" not in spec.child_env()
    with pytest.raises(ValueError, match="env too large"):
        make_spec(
            "big", [PY, "-c", "pass"], tmp_path, env={f"K{i:03d}": "v" * 32768 for i in range(9)}
        )


async def test_status_returns_deep_copies(tracked, tmp_path):
    script = write_script(tmp_path, "sleepy.py", SLEEP_CHILD)
    manager = track(
        tracked, WorkerManager([make_spec("w", [PY, str(script)], tmp_path)], readiness=ready_now)
    )
    assert (await asyncio.wait_for(manager.start("w"), timeout=8.0))["ok"]
    stopped = await asyncio.wait_for(manager.stop("w"), timeout=8.0)
    assert stopped["ok"]
    first = manager.status()
    first["workers"]["w"]["stop_evidence"]["residual_pids"].append(9999)
    first["workers"]["w"]["last_transition"]["event"] = "forged"
    stopped["stop_evidence"]["residual_pids"].append(9999)
    second = manager.status()
    assert second["workers"]["w"]["stop_evidence"]["residual_pids"] == []
    assert second["workers"]["w"]["last_transition"]["event"] == "stopped"


async def test_pidfd_missing_refuses_spawn(tracked, tmp_path, monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    manager = track(
        tracked,
        WorkerManager([make_spec("w", [PY, "-c", SLEEP_CHILD], tmp_path)], readiness=ready_now),
    )
    result = await asyncio.wait_for(manager.start("w"), timeout=8.0)
    assert not result["ok"] and result["error"] == "unsupported-platform"
    assert manager._workers["w"].process is None
