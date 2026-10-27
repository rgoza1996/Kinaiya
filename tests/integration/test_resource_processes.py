"""Integration tests for owner-controlled managed workers (GitHub #9).

These tests spawn real synthetic Python worker subprocesses (explicitly
authorized fixture execution) — never the real voice/Meta/Strata workers —
and verify lifecycle, measurement, cleanup, and secrecy behavior end to end.
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
from packages.resources.processes import MAX_WORKERS

PY = sys.executable
MiB = 1024 * 1024

READY_MEM = """\
import sys, time
from pathlib import Path
ready_path, mib = Path(sys.argv[1]), int(sys.argv[2])
buf = bytearray(mib * 1024 * 1024)
for off in range(0, len(buf), 4096):
    buf[off] = 1
ready_path.write_text("ready")
while True:
    time.sleep(0.2)
"""

FORK_STUBBORN = """\
import os, signal, subprocess, sys, time
from pathlib import Path
if sys.argv[1] == "child":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(sys.argv[2]).write_text(str(os.getpid()))
    while True:
        time.sleep(0.2)
else:
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    Path(sys.argv[1]).write_text("parent-ready")
    subprocess.Popen([sys.executable, __file__, "child", sys.argv[2]])
    while True:
        time.sleep(0.2)
"""

FLOOD = """\
import sys
i = 0
while True:
    sys.stdout.write("flood-line %d %s\\n" % (i, "x" * 200))
    i += 1
"""


def write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def make_spec(wid: str, argv: list[str], cwd: Path, **kwargs) -> WorkerSpec:
    return WorkerSpec(id=wid, argv=tuple(argv), cwd=cwd, **kwargs)


def file_probe(path: Path):
    async def probe(spec, proc):
        return path.exists()

    return probe


async def ready_now(spec, proc):
    return True


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


# -- validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "wid",
    ["", "has space", "a" * 65, "../x", "x\x00", "-lead", ".dot", "semi;colon"],
)
def test_bad_worker_ids_rejected(tmp_path, wid):
    with pytest.raises(ValueError):
        make_spec(wid, [PY, "-c", "pass"], tmp_path)


@pytest.mark.parametrize(
    "argv",
    [[], ["relative/exe"], ["/nonexistent/exe-xyz"], [""], ["\x00"], ["x"] * 65],
)
def test_bad_argv_rejected(tmp_path, argv):
    with pytest.raises(ValueError):
        make_spec("w", argv, tmp_path)


def test_bad_cwd_env_timeouts_rejected(tmp_path):
    afile = tmp_path / "f.txt"
    afile.write_text("x")
    with pytest.raises(ValueError):
        make_spec("w", [PY, "-c", "pass"], Path("relative"))
    with pytest.raises(ValueError):
        make_spec("w", [PY, "-c", "pass"], tmp_path / "missing")
    with pytest.raises(ValueError):
        make_spec("w", [PY, "-c", "pass"], afile)
    for env in ({"9bad": "v"}, {"has space": "v"}, {"": "v"}, {"ok": "v\x00"}, {"o" * 300: "v"}):
        with pytest.raises(ValueError):
            make_spec("w", [PY, "-c", "pass"], tmp_path, env=env)
    with pytest.raises(ValueError):
        make_spec("w", [PY, "-c", "pass"], tmp_path, env={"ok": "v" * 70000})
    for kwargs in (
        {"startup_timeout_s": 0.0},
        {"startup_timeout_s": 400.0},
        {"stop_timeout_s": 100.0},
        {"startup_timeout_s": True},
        {"stop_timeout_s": float("nan")},
    ):
        with pytest.raises(ValueError):
            make_spec("w", [PY, "-c", "pass"], tmp_path, **kwargs)


def test_manager_limits(tmp_path):
    good = [make_spec(f"w{i}", [PY, "-c", "pass"], tmp_path) for i in range(MAX_WORKERS)]
    WorkerManager(good)
    with pytest.raises(ValueError):
        WorkerManager(good + [make_spec("extra", [PY, "-c", "pass"], tmp_path)])
    with pytest.raises(ValueError):
        WorkerManager([good[0], make_spec(good[0].id, [PY, "-c", "pass"], tmp_path)])
    with pytest.raises(TypeError):
        WorkerManager(["not-a-spec"])
    with pytest.raises(TypeError):
        WorkerManager(good, readiness="yes")


def test_lazy_interface():
    import packages.resources as package

    assert set(package.__dir__()) == {
        "MAX_WORKERS",
        "TAIL_MAX_BYTES",
        "WorkerManager",
        "WorkerSpec",
    }
    with pytest.raises(AttributeError):
        package.__getattr__("Nope")


# -- lifecycle with real workers -------------------------------------------


async def test_start_stop_restart_memory(tracked, tmp_path):
    script = write_script(tmp_path, "mem.py", READY_MEM)
    ready = tmp_path / "ready"
    manager = track(
        tracked,
        WorkerManager(
            [make_spec("mem", [PY, str(script), str(ready), "40"], tmp_path)],
            readiness=file_probe(ready),
        ),
    )
    started = await asyncio.wait_for(manager.start("mem"), timeout=8.0)
    assert started["ok"] and started["pid"] > 0
    assert started["readiness"] == "verified" and started["ready"]
    entry = manager.status()["workers"]["mem"]
    assert entry["state"] == "running" and entry["owned_pid"] == started["pid"]
    assert entry["rss_bytes"] is not None and entry["rss_bytes"] >= 32 * MiB
    assert entry["group_rss_bytes"] is not None and entry["group_rss_bytes"] >= 32 * MiB
    assert entry["last_transition"]["event"] == "started"
    again = await asyncio.wait_for(manager.start("mem"), timeout=8.0)
    assert again["ok"] and again.get("reused") and again["pid"] == started["pid"]
    ready.unlink()  # force the post-restart probe to wait for fresh allocation
    restarted = await asyncio.wait_for(manager.restart("mem"), timeout=8.0)
    assert restarted["ok"] and restarted["pid"] != started["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(started["pid"], 0)
    stopped = await asyncio.wait_for(manager.stop("mem"), timeout=8.0)
    assert stopped["ok"]
    evidence = stopped["stop_evidence"]
    assert evidence["reaped"] and evidence["group_empty"] and evidence["residual_pids"] == []
    assert evidence["rss_before_bytes"] is not None and evidence["rss_before_bytes"] >= 32 * MiB
    assert evidence["term_sent"] and evidence["duration_s"] >= 0
    entry = manager.status()["workers"]["mem"]
    assert (
        entry["state"] == "stopped"
        and entry["rss_bytes"] is None
        and entry["group_rss_bytes"] is None
    )
    repeat = await asyncio.wait_for(manager.stop("mem"), timeout=8.0)
    assert repeat["ok"]  # idempotent: still ok after the worker is gone
    assert manager.status()["workers"]["mem"]["group_rss_bytes"] is None


async def test_unverified_readiness_without_probe(tracked, tmp_path):
    manager = track(
        tracked,
        WorkerManager([make_spec("plain", [PY, "-c", "import time; time.sleep(30)"], tmp_path)]),
    )
    started = await asyncio.wait_for(manager.start("plain"), timeout=8.0)
    assert started["ok"] and started["readiness"] == "unverified" and not started["ready"]
    entry = manager.status()["workers"]["plain"]
    assert entry["state"] == "running" and entry["readiness"] == "unverified" and not entry["ready"]
    assert (await asyncio.wait_for(manager.stop("plain"), timeout=8.0))["ok"]


async def test_stop_never_started_is_idempotent(tracked, tmp_path):
    manager = track(
        tracked, WorkerManager([make_spec("w", [PY, "-c", "pass"], tmp_path)], readiness=ready_now)
    )
    assert await asyncio.wait_for(manager.stop("w"), timeout=8.0) == {
        "ok": True,
        "worker_id": "w",
        "already": True,
    }


async def test_unknown_worker_ids(tracked, tmp_path):
    manager = track(
        tracked, WorkerManager([make_spec("w", [PY, "-c", "pass"], tmp_path)], readiness=ready_now)
    )
    for call in (manager.start, manager.stop, manager.restart):
        result = await call("nope")
        assert result == {"ok": False, "worker_id": "nope", "error": "unknown-worker"}


async def test_early_exit_reports_failed_and_cleans_group(tracked, tmp_path):
    def probe_false(spec, proc):
        return False

    manager = track(
        tracked,
        WorkerManager(
            [
                make_spec(
                    "doomed", [PY, "-c", "import sys; sys.exit(3)"], tmp_path, startup_timeout_s=5.0
                )
            ],
            readiness=probe_false,
        ),
    )
    result = await asyncio.wait_for(manager.start("doomed"), timeout=8.0)
    assert result == {"ok": False, "worker_id": "doomed", "error": "early-exit", "returncode": 3}
    entry = manager.status()["workers"]["doomed"]
    assert entry["state"] == "failed" and entry["error"] == "early-exit"
    assert entry["group_rss_bytes"] is None
    assert group_live(result.get("pid", -1) or -1) == []


async def test_probe_failure_reports_readiness_failed(tracked, tmp_path):
    async def probe_boom(spec, proc):
        raise RuntimeError("synthetic probe failure")

    manager = track(
        tracked,
        WorkerManager(
            [
                make_spec(
                    "w", [PY, "-c", "import time; time.sleep(30)"], tmp_path, startup_timeout_s=5.0
                )
            ],
            readiness=probe_boom,
        ),
    )
    result = await asyncio.wait_for(manager.start("w"), timeout=8.0)
    assert result["ok"] is False and result["error"] == "readiness-failed"
    assert manager.status()["workers"]["w"]["state"] == "failed"


async def test_startup_timeout_cleans_group(tracked, tmp_path):
    manager = track(
        tracked,
        WorkerManager(
            [
                make_spec(
                    "slow",
                    [PY, "-c", "import time; time.sleep(30)"],
                    tmp_path,
                    startup_timeout_s=1.0,
                )
            ],
            readiness=file_probe(tmp_path / "never"),
        ),
    )
    result = await asyncio.wait_for(manager.start("slow"), timeout=8.0)
    assert result["ok"] is False and result["error"] == "startup-timeout"
    entry = manager.status()["workers"]["slow"]
    assert entry["state"] == "failed" and entry["group_rss_bytes"] is None


async def test_cancelled_start_cleans_group(tracked, tmp_path):
    manager = track(
        tracked,
        WorkerManager(
            [
                make_spec(
                    "slow",
                    [PY, "-c", "import time; time.sleep(30)"],
                    tmp_path,
                    startup_timeout_s=10.0,
                )
            ],
            readiness=file_probe(tmp_path / "never"),
        ),
    )
    task = asyncio.ensure_future(manager.start("slow"))
    await asyncio.sleep(0.4)
    pgid = manager.status()["workers"]["slow"]["owned_pgid"]
    assert pgid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=8.0)
    assert await wait_until(lambda: group_live(pgid) == [], timeout_s=5.0)
    entry = manager.status()["workers"]["slow"]
    assert entry["state"] == "failed" and entry["error"] == "cancelled"


async def test_status_never_blocks_on_probe(tracked, tmp_path):
    manager = track(
        tracked,
        WorkerManager(
            [
                make_spec(
                    "slow",
                    [PY, "-c", "import time; time.sleep(30)"],
                    tmp_path,
                    startup_timeout_s=6.0,
                )
            ],
            readiness=file_probe(tmp_path / "never"),
        ),
    )
    task = asyncio.ensure_future(manager.start("slow"))
    try:
        assert await wait_until(lambda: manager.status()["workers"]["slow"]["state"] == "starting")
        begin = time.monotonic()
        entry = manager.status()["workers"]["slow"]
        assert time.monotonic() - begin < 1.0
        assert entry["state"] == "starting" and entry["owned_pid"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8.0)


async def test_concurrent_start_stop_serialized(tracked, tmp_path):
    manager = track(
        tracked,
        WorkerManager(
            [make_spec("w", [PY, "-c", "import time; time.sleep(30)"], tmp_path)],
            readiness=ready_now,
        ),
    )
    results = await asyncio.wait_for(
        asyncio.gather(
            *[manager.start("w") for _ in range(3)], *[manager.stop("w") for _ in range(3)]
        ),
        timeout=10.0,
    )
    assert len(results) == 6 and all(r["worker_id"] == "w" for r in results)
    assert all("error" not in r or r["error"] != "stop-incomplete" for r in results)
    final = manager.status()["workers"]["w"]
    assert final["state"] in ("running", "stopped")
    if final["state"] == "running":
        os.kill(final["owned_pid"], 0)
        assert group_live(final["owned_pgid"]) != []
    else:
        assert final["group_rss_bytes"] is None


async def test_concurrent_distinct_starts(tracked, tmp_path):
    specs = [
        make_spec(f"w{i}", [PY, "-c", "import time; time.sleep(30)"], tmp_path) for i in range(4)
    ]
    manager = track(tracked, WorkerManager(specs, readiness=ready_now))
    results = await asyncio.wait_for(
        asyncio.gather(*[manager.start(s.id) for s in specs]), timeout=10.0
    )
    assert all(r["ok"] for r in results)
    assert len({r["pid"] for r in results}) == 4


async def test_flood_logs_bounded(tracked, tmp_path):
    script = write_script(tmp_path, "flood.py", FLOOD)
    manager = track(
        tracked,
        WorkerManager(
            [make_spec("loud", [PY, str(script)], tmp_path, stop_timeout_s=2.0)],
            readiness=ready_now,
        ),
    )
    started = await asyncio.wait_for(manager.start("loud"), timeout=8.0)
    assert started["ok"]
    await asyncio.sleep(1.2)
    mid = manager.status()["workers"]["loud"]
    assert mid["state"] == "running"  # pipe pressure never wedged the child
    stopped = await asyncio.wait_for(manager.stop("loud"), timeout=8.0)
    assert stopped["ok"]
    entry = manager.status()["workers"]["loud"]
    assert len(entry["log_tail_stdout"]) <= 4096 and len(entry["log_tail_stderr"]) <= 4096


async def test_status_hides_secrets_and_commands(tracked, tmp_path):
    secret = "s3cr3t-worker-token-xyz"
    manager = track(
        tracked,
        WorkerManager(
            [
                make_spec(
                    "w",
                    [PY, "-c", "import time; time.sleep(30)"],
                    tmp_path,
                    env={"WORKER_TOKEN": secret},
                )
            ],
            readiness=ready_now,
        ),
    )
    assert (await asyncio.wait_for(manager.start("w"), timeout=8.0))["ok"]
    dump = json.dumps(manager.status())
    assert secret not in dump and "WORKER_TOKEN" not in dump
    assert PY not in dump
    entry = manager.status()["workers"]["w"]
    assert (
        "argv" not in entry and "env" not in entry and "cwd" not in entry and "command" not in entry
    )


async def test_stop_stubborn_descendant_kills_group(tracked, tmp_path):
    script = write_script(tmp_path, "fork.py", FORK_STUBBORN)
    parent_ready, child_ready = tmp_path / "parent", tmp_path / "child"
    manager = track(
        tracked,
        WorkerManager(
            [
                make_spec(
                    "fork",
                    [PY, str(script), str(parent_ready), str(child_ready)],
                    tmp_path,
                    stop_timeout_s=1.0,
                )
            ],
            readiness=file_probe(parent_ready),
        ),
    )
    started = await asyncio.wait_for(manager.start("fork"), timeout=8.0)
    assert started["ok"]
    assert await wait_until(child_ready.exists)
    child_pid = int(child_ready.read_text())
    assert child_pid != started["pid"]
    stopped = await asyncio.wait_for(manager.stop("fork"), timeout=8.0)
    assert stopped["ok"], stopped
    assert stopped["stop_evidence"]["kill_sent"]
    assert await wait_until(lambda: group_live(started["pid"]) == [], timeout_s=5.0)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


async def test_close_kills_descendant_and_spares_sentinel(tracked, tmp_path):
    script = write_script(tmp_path, "fork.py", FORK_STUBBORN)
    parent_ready, child_ready = tmp_path / "parent", tmp_path / "child"
    manager = track(
        tracked,
        WorkerManager(
            [
                make_spec(
                    "fork",
                    [PY, str(script), str(parent_ready), str(child_ready)],
                    tmp_path,
                    stop_timeout_s=1.0,
                )
            ],
            readiness=file_probe(parent_ready),
        ),
    )
    started = await asyncio.wait_for(manager.start("fork"), timeout=8.0)
    assert started["ok"]
    assert await wait_until(child_ready.exists)
    child_pid = int(child_ready.read_text())
    sentinel = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        await asyncio.wait_for(manager.close(), timeout=10.0)
        assert await wait_until(lambda: group_live(started["pid"]) == [], timeout_s=5.0)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert sentinel.poll() is None
        os.kill(sentinel.pid, 0)
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


async def test_partial_close_attempts_all_workers(tracked, tmp_path, monkeypatch):
    specs = [
        make_spec("a", [PY, "-c", "import time; time.sleep(30)"], tmp_path),
        make_spec("b", [PY, "-c", "import time; time.sleep(30)"], tmp_path),
    ]
    manager = track(tracked, WorkerManager(specs, readiness=ready_now))
    assert all(
        r["ok"]
        for r in await asyncio.wait_for(
            asyncio.gather(*[manager.start(s.id) for s in specs]), timeout=8.0
        )
    )
    # Cleanup signals travel only through pidfds now (numeric killpg is gone),
    # so the synthetic failure is injected at the pidfd layer. The first
    # signal belongs to worker "a" (close iterates in spec order); the rest
    # delegate to the real sender so "b" still stops.
    real_send = signal.pidfd_send_signal
    calls: list[tuple[int, int]] = []

    def flaky(fd, sig):
        if not calls:
            calls.append((fd, sig))
            raise RuntimeError("synthetic cleanup failure")
        return real_send(fd, sig)

    monkeypatch.setattr(signal, "pidfd_send_signal", flaky)
    with pytest.raises(RuntimeError, match="close incomplete"):
        await asyncio.wait_for(manager.close(), timeout=10.0)
    assert manager.status()["workers"]["b"]["state"] == "stopped"
