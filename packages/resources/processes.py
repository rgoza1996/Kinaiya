"""Owner-controlled local worker lifecycle (GitHub #9, Roadmap 14.2-14.4).

Only trusted owner configuration fixed at construction (argv/cwd/env) may define
a worker. No API accepts an arbitrary PID, argv, or cwd at runtime; no shell is used.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["MAX_WORKERS", "TAIL_MAX_BYTES", "WorkerSpec", "WorkerManager"]

LOG = logging.getLogger(__name__)

MAX_WORKERS = 16
TAIL_MAX_BYTES = 65536
STATUS_TAIL_CHARS = 4096
MAX_ARGV = 64
MAX_ARG_LEN = 32768
MAX_ARGV_TOTAL = 262144
MAX_ENV_VARS = 128
MAX_ENV_VALUE_LEN = 65536
MIN_STARTUP_TIMEOUT_S = 0.1
MAX_STARTUP_TIMEOUT_S = 300.0
MIN_STOP_TIMEOUT_S = 0.1
MAX_STOP_TIMEOUT_S = 60.0
KILL_GRACE_S = 2.0
PROBE_INTERVAL_S = 0.05
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
DEFAULT_LANG = "C.UTF-8"

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,255}")


def _utcnow() -> str:  # single timestamp source for transitions/evidence
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class WorkerSpec:
    """Immutable owner configuration for one managed worker process.

    ``argv``/``cwd``/``env`` come ONLY from trusted owner configuration.
    The child gets minimal ``PATH``/``LANG`` plus ``env``; the parent
    environment is never inherited. Owners may opt cache paths into ``env``.
    """

    id: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] | None = None
    startup_timeout_s: float = 30.0
    stop_timeout_s: float = 3.0

    def __post_init__(self) -> None:
        wid = self.id
        if not isinstance(wid, str) or not _ID_RE.fullmatch(wid) or "\x00" in wid:
            raise ValueError(f"invalid worker id: {wid!r}")

        def bad(msg: str) -> ValueError:
            return ValueError(f"{msg} for worker {wid!r}")

        argv = self.argv
        if isinstance(argv, list):
            argv = tuple(argv)
            object.__setattr__(self, "argv", argv)
        if not isinstance(argv, tuple) or not 1 <= len(argv) <= MAX_ARGV:
            raise bad("invalid argv")
        if any(not isinstance(a, str) or not a or len(a) > MAX_ARG_LEN or "\x00" in a for a in argv):
            raise bad("invalid argv entry")
        if sum(map(len, argv)) > MAX_ARGV_TOTAL:
            raise bad("argv too large")
        if not (os.path.isabs(argv[0]) and os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
            raise bad("argv[0] must be an absolute existing executable")
        cwd = self.cwd if isinstance(self.cwd, Path) else Path(self.cwd)
        object.__setattr__(self, "cwd", cwd)
        if not (cwd.is_absolute() and cwd.is_dir()):
            raise bad("cwd must be an absolute existing directory")
        if self.env is not None:
            if not isinstance(self.env, dict) or len(self.env) > MAX_ENV_VARS:
                raise bad("invalid env mapping")
            for key, value in self.env.items():
                if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key) or "\x00" in key:
                    raise bad("invalid env key")
                if not isinstance(value, str) or len(value) > MAX_ENV_VALUE_LEN or "\x00" in value:
                    raise bad("invalid env value")
            object.__setattr__(self, "env", dict(self.env))
        for name, value, lo, hi in (
            ("startup_timeout_s", self.startup_timeout_s, MIN_STARTUP_TIMEOUT_S, MAX_STARTUP_TIMEOUT_S),
            ("stop_timeout_s", self.stop_timeout_s, MIN_STOP_TIMEOUT_S, MAX_STOP_TIMEOUT_S),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= float(value) <= hi:
                raise bad(f"{name} out of bounds")

    def child_env(self) -> dict[str, str]:
        """Minimal explicit child environment plus owner-provided entries."""
        env = {"PATH": DEFAULT_PATH, "LANG": DEFAULT_LANG}
        if self.env:
            env.update(self.env)
        return env


def _proc_stat(pid: int) -> tuple[str, int, int] | None:
    """Return (state, pgrp, starttime) from /proc/<pid>/stat, else None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("ascii", "replace")
    except OSError:
        return None
    cut = raw.rfind(")")
    if cut < 0:
        return None
    fields = raw[cut + 2 :].split()  # state(3) ppid(4) pgrp(5) ... starttime(22)
    if len(fields) < 20:
        return None
    try:
        return fields[0], int(fields[2]), int(fields[19])
    except ValueError:
        return None


def _rss_bytes(pid: int) -> int | None:
    """Resident bytes for pid, or None when unreadable. Zombies read 0."""
    try:
        with open(f"/proc/{pid}/statm", "rb") as fh:
            parts = fh.read().split()
        pages = int(parts[1])
    except (OSError, ValueError, IndexError):
        return None
    try:
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return None


def _group_members(pgid: int) -> list[tuple[int, str]]:
    """(pid, state) for processes currently in pgid; races yield skips."""
    members: list[tuple[int, str]] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        stat = _proc_stat(pid)
        if stat is not None and stat[1] == pgid:
            members.append((pid, stat[0]))
    return members


def _live_group_members(pgid: int) -> list[int]:
    return [pid for pid, state in _group_members(pgid) if state not in ("Z", "X", "x")]


def _mem_available_bytes() -> int | None:
    try:
        with open("/proc/meminfo", "rb") as fh:
            for line in fh.read().split(b"\n"):
                if line.startswith(b"MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


@dataclass
class _Managed:
    spec: WorkerSpec
    process: asyncio.subprocess.Process | None = None
    pgid: int | None = None
    leader_starttime: int | None = None
    state: str = "stopped"
    ready: bool = False
    readiness: str = "not-started"
    error: str | None = None
    stdout_tail: bytearray = field(default_factory=bytearray)
    stderr_tail: bytearray = field(default_factory=bytearray)
    drains: list[asyncio.Task[None]] = field(default_factory=list)
    last_transition: dict[str, Any] | None = None
    stop_evidence: dict[str, Any] | None = None


ReadinessProbe = Callable[[WorkerSpec, asyncio.subprocess.Process], bool | Awaitable[bool]]


class WorkerManager:
    """Owns up to 16 worker processes defined solely by owner configuration.

    One lock serializes mutations; :meth:`status` never awaits a probe or hung
    worker. Only spawned children are signalled, guarded by /proc start-time.
    """

    def __init__(self, specs: list[WorkerSpec], *, readiness: ReadinessProbe | None = None) -> None:
        if not isinstance(specs, list) or not specs or len(specs) > MAX_WORKERS:
            raise ValueError(f"specs must be a non-empty list of at most {MAX_WORKERS}")
        seen: set[str] = set()
        for spec in specs:
            if not isinstance(spec, WorkerSpec):
                raise TypeError("specs must contain WorkerSpec instances")
            if spec.id in seen:
                raise ValueError(f"duplicate worker id: {spec.id!r}")
            seen.add(spec.id)
        if readiness is not None and not callable(readiness):
            raise TypeError("readiness must be an async callable or None")
        self._workers: dict[str, _Managed] = {spec.id: _Managed(spec=spec) for spec in specs}
        self._readiness = readiness
        self._lock = asyncio.Lock()
        self._closed = False

    def status(self) -> dict[str, Any]:
        workers = {}
        for wid, managed in self._workers.items():
            proc = managed.process
            pid = proc.pid if proc is not None else None
            state, error = managed.state, managed.error
            if proc is not None and proc.returncode is not None and state == "running":
                state, error = "failed", error or "early-exit"  # reconcile; never a mere flag
            workers[wid] = {
                "id": wid, "state": state, "ready": managed.ready, "readiness": managed.readiness,
                "owned_pid": pid, "owned_pgid": managed.pgid, "error": error,
                "rss_bytes": _rss_bytes(pid) if pid is not None else None,
                "group_rss_bytes": self._group_rss(managed), "mem_available_bytes": _mem_available_bytes(),
                "log_tail_stdout": self._tail_text(managed.stdout_tail),
                "log_tail_stderr": self._tail_text(managed.stderr_tail),
                "last_transition": managed.last_transition, "stop_evidence": managed.stop_evidence,
            }
        return {"workers": workers, "worker_count": len(workers), "closed": self._closed, "gpu": "unknown/unverified"}

    @staticmethod
    def _group_rss(managed: _Managed) -> int | None:
        if managed.pgid is None:
            return None
        values = [v for pid, _ in _group_members(managed.pgid) if (v := _rss_bytes(pid)) is not None]
        return sum(values) if values else None

    @staticmethod
    def _tail_text(tail: bytearray) -> str:
        text = bytes(tail).decode("utf-8", "replace")
        if len(text) > STATUS_TAIL_CHARS:
            text = text[-STATUS_TAIL_CHARS:]
        return text

    async def start(self, worker_id: str) -> dict[str, Any]:
        async with self._lock:
            return await self._start_locked(worker_id)

    async def stop(self, worker_id: str) -> dict[str, Any]:
        async with self._lock:
            return await self._stop_locked(worker_id)

    async def restart(self, worker_id: str) -> dict[str, Any]:
        async with self._lock:
            managed = self._workers.get(worker_id)
            if managed is None:
                return {"ok": False, "worker_id": worker_id, "error": "unknown-worker"}
            if self._closed:
                return {"ok": False, "worker_id": worker_id, "error": "closed"}
            old_pid = managed.process.pid if managed.process is not None else None
            stop_result = await self._stop_locked(worker_id)
            start_result = await self._start_locked(worker_id)
            return {"ok": start_result["ok"], "worker_id": worker_id, "restarted": start_result["ok"],
                    "old_pid": old_pid, "pid": start_result.get("pid"), "ready": start_result.get("ready", False),
                    "readiness": start_result.get("readiness"), "error": start_result.get("error"), "stop": stop_result}

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            failures: list[str] = []
            for wid in self._workers:
                try:
                    result = await self._stop_locked(wid)
                    if not result.get("ok"):
                        failures.append(f"{wid}:{result.get('error')}")
                except Exception as exc:  # keep stopping every other worker
                    failures.append(f"{wid}:{type(exc).__name__}")
            if failures:
                raise RuntimeError(f"close incomplete; attempted all workers: {'; '.join(failures)}")

    def _note(self, managed: _Managed, event: str, duration_s: float) -> None:
        managed.last_transition = {"event": event, "at": _utcnow(), "duration_s": duration_s}

    async def _start_locked(self, worker_id: str) -> dict[str, Any]:
        managed = self._workers.get(worker_id)
        if managed is None:
            return {"ok": False, "worker_id": worker_id, "error": "unknown-worker"}
        if self._closed:
            return {"ok": False, "worker_id": worker_id, "error": "closed"}
        proc = managed.process
        if proc is not None and proc.returncode is None and managed.state == "running":
            return {"ok": True, "worker_id": worker_id, "pid": proc.pid, "reused": True,
                    "ready": managed.ready, "readiness": managed.readiness}
        t0 = time.monotonic()
        spec = managed.spec
        managed.state, managed.ready, managed.readiness, managed.error = "starting", False, "starting", None
        managed.stdout_tail.clear()
        managed.stderr_tail.clear()
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv, cwd=str(spec.cwd), env=spec.child_env(), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=TAIL_MAX_BYTES)
        except Exception:
            managed.process = None
            managed.pgid = None  # never signal a previous incarnation's group
            return await self._fail_start(managed, t0, "spawn-failed")
        managed.process = proc
        managed.pgid = proc.pid
        stat = _proc_stat(proc.pid)
        managed.leader_starttime = stat[2] if stat else None
        managed.drains = [
            asyncio.ensure_future(self._drain(proc.stdout, managed.stdout_tail)),
            asyncio.ensure_future(self._drain(proc.stderr, managed.stderr_tail)),
        ]
        try:
            return await self._await_ready(managed, t0)
        except asyncio.CancelledError:
            await self._emergency_cleanup(managed)
            managed.state = "failed"
            managed.error = "cancelled"
            managed.readiness = "failed"
            self._note(managed, "failed", time.monotonic() - t0)
            raise

    @staticmethod
    async def _wait_reap(proc: asyncio.subprocess.Process | None) -> None:
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
        except (TimeoutError, Exception):
            pass

    async def _fail_start(self, managed: _Managed, t0: float, error: str) -> dict[str, Any]:
        """Cleanup an owned group after a failed start and report honestly."""
        proc = managed.process
        await self._kill_group(managed, escalate=True)
        await self._wait_reap(proc)
        await self._finalize_pipes(managed)
        managed.state = "failed"
        managed.error = error
        managed.readiness = "failed"
        self._note(managed, "failed", time.monotonic() - t0)
        LOG.warning("worker failed: id=%s category=%s", managed.spec.id, error)
        result: dict[str, Any] = {"ok": False, "worker_id": managed.spec.id, "error": error}
        if proc is not None and proc.returncode is not None:
            result["returncode"] = proc.returncode
        return result

    async def _await_ready(self, managed: _Managed, t0: float) -> dict[str, Any]:
        spec = managed.spec
        proc = managed.process
        assert proc is not None
        deadline = t0 + float(spec.startup_timeout_s)
        loop = asyncio.get_running_loop()
        while True:
            if proc.returncode is not None:
                return await self._fail_start(managed, t0, "early-exit")
            ready = False
            if self._readiness is None:
                ready = True
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    ready = False
                else:
                    try:
                        outcome = self._readiness(spec, proc)
                        if inspect.isawaitable(outcome):
                            outcome = await asyncio.wait_for(outcome, timeout=remaining)
                        ready = bool(outcome)
                    except TimeoutError:
                        ready = False
                    except Exception:
                        return await self._fail_start(managed, t0, "readiness-failed")
            if ready:
                managed.state = "running"
                managed.ready = self._readiness is not None
                managed.readiness = "verified" if self._readiness is not None else "unverified"
                self._note(managed, "started", time.monotonic() - t0)
                return {"ok": True, "worker_id": spec.id, "pid": proc.pid, "reused": False,
                        "ready": managed.ready, "readiness": managed.readiness, "duration_s": time.monotonic() - t0}
            if loop.time() >= deadline:
                return await self._fail_start(managed, t0, "startup-timeout")
            await asyncio.sleep(PROBE_INTERVAL_S)

    @staticmethod
    async def _drain(stream: asyncio.StreamReader | None, tail: bytearray) -> None:
        if stream is None:
            return
        try:
            while chunk := await stream.read(8192):
                tail += chunk
                if len(tail) > TAIL_MAX_BYTES:
                    del tail[: len(tail) - TAIL_MAX_BYTES]
        except (asyncio.CancelledError, Exception):
            return

    async def _finalize_pipes(self, managed: _Managed) -> None:
        for task in managed.drains:
            if not task.done():
                task.cancel()
        if managed.drains:
            await asyncio.gather(*managed.drains, return_exceptions=True)
            managed.drains = []

    def _identity_ok(self, managed: _Managed) -> bool:
        if managed.process is None or managed.pgid is None:
            return False
        stat = _proc_stat(managed.process.pid)
        return stat is not None and stat[2] == managed.leader_starttime

    async def _kill_group(self, managed: _Managed, *, escalate: bool) -> dict[str, Any]:
        """TERM the owned group; KILL stragglers when escalating. Never touches foreign PIDs."""
        info = {"term_sent": False, "kill_sent": False}
        if managed.pgid is None:
            return info
        if not self._identity_ok(managed) and managed.state != "starting":
            return info
        try:
            os.killpg(managed.pgid, signal.SIGTERM)
            info["term_sent"] = True
        except (ProcessLookupError, PermissionError):
            return info
        if escalate:
            await asyncio.sleep(0.2)
            live = _live_group_members(managed.pgid)
            if live:
                try:
                    os.killpg(managed.pgid, signal.SIGKILL)
                    info["kill_sent"] = True
                except (ProcessLookupError, PermissionError):
                    pass
                for pid in _live_group_members(managed.pgid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                        info["kill_sent"] = True
                    except (ProcessLookupError, PermissionError):
                        pass
        return info

    async def _stop_locked(self, worker_id: str) -> dict[str, Any]:
        managed = self._workers.get(worker_id)
        if managed is None:
            return {"ok": False, "worker_id": worker_id, "error": "unknown-worker"}
        proc = managed.process
        t0 = time.monotonic()
        if proc is None:
            if managed.state != "stopped":
                managed.state = "stopped"
                self._note(managed, "stopped", 0.0)
            return {"ok": True, "worker_id": worker_id, "already": True}
        pid, pgid = proc.pid, managed.pgid
        rss_before = _rss_bytes(pid)
        group_rss_before = self._group_rss(managed)
        evidence: dict[str, Any] = {
            "pid": pid, "pgid": pgid, "rss_before_bytes": rss_before,
            "group_rss_before_bytes": group_rss_before, "mem_available_bytes": _mem_available_bytes(),
            "term_sent": False, "kill_sent": False, "reaped": False, "group_empty": False, "residual_pids": [],
        }
        try:
            if proc.returncode is None and self._identity_ok(managed):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    evidence["term_sent"] = True
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.monotonic() + float(managed.spec.stop_timeout_s)
            escalated = False
            while True:
                if proc.returncode is not None:
                    try:
                        await proc.wait()
                    except Exception:
                        pass
                live = _live_group_members(pgid) if pgid is not None else []
                if proc.returncode is not None and not live:
                    break
                if time.monotonic() >= deadline:
                    if not escalated and live:
                        for target in [pgid, *live]:
                            try:
                                if target == pgid:
                                    os.killpg(pgid, signal.SIGKILL)
                                else:
                                    os.kill(target, signal.SIGKILL)
                                evidence["kill_sent"] = True
                            except (ProcessLookupError, PermissionError):
                                pass
                        escalated = True
                        deadline = time.monotonic() + KILL_GRACE_S
                        continue
                    break
                await asyncio.sleep(PROBE_INTERVAL_S)
            await self._wait_reap(proc)
            await self._finalize_pipes(managed)
            live = _live_group_members(pgid) if pgid is not None else []
            evidence["reaped"] = proc.returncode is not None
            evidence["group_empty"] = not live
            evidence["residual_pids"] = live[:MAX_WORKERS]
            evidence["duration_s"] = time.monotonic() - t0
            evidence["at"] = _utcnow()
            managed.stop_evidence = evidence
            if evidence["reaped"] and evidence["group_empty"]:
                managed.state = "stopped"
                managed.ready = False
                managed.readiness = "not-started" if managed.error else managed.readiness
                managed.error = None
                self._note(managed, "stopped", evidence["duration_s"])
                return {"ok": True, "worker_id": worker_id, "pid": pid, "stop_evidence": evidence}
            managed.state = "failed"
            managed.error = "stop-incomplete"
            self._note(managed, "failed", evidence["duration_s"])
            LOG.warning("worker stop incomplete: id=%s residual=%s", worker_id, evidence["residual_pids"])
            return {"ok": False, "worker_id": worker_id, "error": "stop-incomplete", "stop_evidence": evidence}
        except asyncio.CancelledError:
            await self._emergency_cleanup(managed)
            managed.state = "failed"
            managed.error = "cancelled"
            managed.stop_evidence = evidence
            raise
        except Exception as exc:
            evidence["duration_s"] = time.monotonic() - t0
            managed.stop_evidence = evidence
            managed.state = "failed"
            managed.error = "stop-error"
            LOG.warning("worker stop error: id=%s exc=%s", worker_id, type(exc).__name__)
            raise

    async def _emergency_cleanup(self, managed: _Managed) -> None:
        try:
            await self._kill_group(managed, escalate=True)
        except Exception:
            pass
        proc = managed.process
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
            except (TimeoutError, Exception):
                pass
        await self._finalize_pipes(managed)
