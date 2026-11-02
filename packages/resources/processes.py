"""Owner-controlled local worker lifecycle (GitHub #9, Roadmap 14.2-14.4).

Only trusted owner configuration fixed at construction (argv/cwd/env) may define
a worker. No API accepts an arbitrary PID, argv, or cwd at runtime; no shell is used.

Configured code cannot execute until parent ownership is established: each
spawn launches trusted first-party ``_bootstrap.py`` (absolute script path via
``sys.executable``) which blocks on a private grant pipe. Argv/env travel in a
bounded sealed memfd, never on the bootstrap command line (only fd numbers and
a fresh random nonce). The parent verifies pidfd bind-back, ppid/session/group/
starttime and the bootstrap nonce around ``pidfd_open`` before writing the
one-byte grant; only then does the bootstrap validate, close control fds and
``os.execve`` the configured executable. Verification failure closes the grant
so the bootstrap exits by itself; the child is reaped without signals and start
reports ``ownership-unverified``. Asyncio child watchers may reap exited
children automatically, so numeric PIDs are never trusted after reap and a lost
leader handle is never reopened numerically.

Cleanup signalling uses Linux pidfds exclusively. Group members are adopted
only while the verified granted leader is alive, with kernel
start-time/session/group rechecks around ``pidfd_open`` so PID reuse cannot
redirect a signal. Numeric ``/proc`` scans are measurement-only diagnostics and
are never signalled. Anything unattributable fails closed as
``stop-incomplete``/``ownership-unverified`` instead of success.
"""

from __future__ import annotations

import asyncio
import copy
import errno
import inspect
import json
import logging
import os
import re
import secrets
import signal
import sys
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

__all__ = ["MAX_WORKERS", "TAIL_MAX_BYTES", "WorkerIdentity", "WorkerManager", "WorkerSpec"]

LOG = logging.getLogger(__name__)

MAX_WORKERS = 16
TAIL_MAX_BYTES = 65536
MAX_ARGV = 64
MAX_ARG_LEN = 32768
MAX_ARGV_TOTAL = 262144
MAX_ENV_VARS = 128
MAX_ENV_VALUE_LEN = 65536
MAX_ENV_TOTAL_BYTES = 262144
MIN_STARTUP_TIMEOUT_S = 0.1
MAX_STARTUP_TIMEOUT_S = 300.0
MIN_STOP_TIMEOUT_S = 0.1
MAX_STOP_TIMEOUT_S = 60.0
KILL_GRACE_S = 2.0
PROBE_INTERVAL_S = 0.05
SPAWN_SETTLE_S = 5.0
REAP_GRACE_S = 5.0
GRANT_BYTE = b"\x01"
MAX_CONFIG_BYTES = 1048576
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
DEFAULT_LANG = "C.UTF-8"

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,255}")
_ZOMBIE_STATES = ("Z", "X", "x")


def _utcnow() -> str:  # single timestamp source for transitions/evidence
    return datetime.now(UTC).isoformat()


def _pidfd_supported() -> bool:
    """True when stable pidfd signalling is available (required before spawn)."""
    return hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")


@dataclass(frozen=True)
class WorkerSpec:
    """Immutable owner configuration for one managed worker process.

    ``argv``/``cwd``/``env`` come ONLY from trusted owner configuration.
    The child gets minimal ``PATH``/``LANG`` plus ``env``; the parent
    environment is never inherited. Owners may opt cache paths into ``env``.
    ``env`` is copied and frozen to an immutable mapping at construction, so
    no caller can modify spawn config through the public spec reference.
    """

    id: str
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] | None = None
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
        if any(
            not isinstance(a, str) or not a or len(a) > MAX_ARG_LEN or "\x00" in a for a in argv
        ):
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
            if not isinstance(self.env, Mapping) or len(self.env) > MAX_ENV_VARS:
                raise bad("invalid env mapping")
            total = 0
            for key, value in self.env.items():
                if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key) or "\x00" in key:
                    raise bad("invalid env key")
                if not isinstance(value, str) or len(value) > MAX_ENV_VALUE_LEN or "\x00" in value:
                    raise bad("invalid env value")
                total += len(key.encode("utf-8")) + len(value.encode("utf-8"))
            if total > MAX_ENV_TOTAL_BYTES:
                raise bad("env too large")
            object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        for name, value, lo, hi in (
            (
                "startup_timeout_s",
                self.startup_timeout_s,
                MIN_STARTUP_TIMEOUT_S,
                MAX_STARTUP_TIMEOUT_S,
            ),
            ("stop_timeout_s", self.stop_timeout_s, MIN_STOP_TIMEOUT_S, MAX_STOP_TIMEOUT_S),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not lo <= float(value) <= hi
            ):
                raise bad(f"{name} out of bounds")

    def child_env(self) -> dict[str, str]:
        """Minimal explicit child environment plus owner-provided entries."""
        env = {"PATH": DEFAULT_PATH, "LANG": DEFAULT_LANG}
        if self.env:
            env.update(self.env)
        return env


def _proc_stat_full(pid: int) -> tuple[str, int, int, int, int] | None:
    """(state, ppid, pgrp, session, starttime) from /proc/<pid>/stat, else None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("ascii", "replace")
    except OSError:
        return None
    cut = raw.rfind(")")
    if cut < 0:
        return None
    fields = raw[cut + 2 :].split()  # state(3) ppid(4) pgrp(5) session(6) ... starttime(22)
    if len(fields) < 20:
        return None
    try:
        return fields[0], int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19])
    except ValueError:
        return None


def _pidfd_pid(fd: int) -> int | None:
    """PID number the kernel currently binds to pidfd ``fd``, else None."""
    try:
        with open(f"/proc/self/fdinfo/{fd}", "rb") as fh:
            for line in fh.read().split(b"\n"):
                if line.startswith(b"Pid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _pidfd_send(fd: int, sig: int) -> bool:
    """Send ``sig`` through a stable pidfd; True only if delivered.

    A dead target reports ESRCH (False); the signal can never be redirected
    to a recycled PID the way numeric kill can.
    """
    try:
        signal.pidfd_send_signal(fd, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _pidfd_alive(fd: int) -> bool:
    """True when the process bound to pidfd ``fd`` still exists."""
    try:
        signal.pidfd_send_signal(fd, 0)
    except PermissionError:
        return True  # exists; EPERM cannot happen for our own children
    except OSError:
        return False
    return True


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


def _scan_group(pgid: int) -> tuple[dict[int, tuple[str, int, int]], bool]:
    """Map pid -> (state, session, starttime) for numeric pgid members.

    Measurement only: callers must never signal these PIDs. ``ok`` is False
    when ``/proc`` enumeration itself failed, which must not be treated as an
    empty-group proof.
    """
    members: dict[int, tuple[str, int, int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members, False
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        stat = _proc_stat_full(pid)
        if stat is not None and stat[2] == pgid:
            members[pid] = (stat[0], stat[3], stat[4])
    return members, True


def _adopt_member(pgid: int, pid: int) -> int | None:
    """Open a pidfd for numeric ``pid`` after verifying group membership.

    Requires the candidate to sit in ``pgid``/session ``pgid`` with a stable
    starttime across ``pidfd_open`` (checked before and after), and requires
    the new pidfd to bind back to ``pid``. Returns the pidfd, else None. Any
    rejection is fail-closed: the candidate is left alone, never signalled.
    """
    if pid <= 0:
        return None
    before = _proc_stat_full(pid)
    if before is None:
        return None
    if before[2] != pgid or before[3] != pgid or before[0] in _ZOMBIE_STATES:
        return None
    try:
        fd = os.pidfd_open(pid, 0)
    except OSError:
        return None
    try:
        after = _proc_stat_full(pid)
        bound = _pidfd_pid(fd)
    except Exception:
        after, bound = None, None
    if (
        after is None
        or bound != pid
        or after[2] != pgid
        or after[3] != pgid
        or after[4] != before[4]
    ):
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def _bootstrap_path() -> str:
    return str(Path(__file__).with_name("_bootstrap.py").resolve())


def _make_config_fd(raw: bytes) -> tuple[int, Any | None]:
    """Sealed memfd holding bounded config; inability to seal refuses spawn."""
    fd = os.memfd_create("worker-config", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        import fcntl

        off = 0
        while off < len(raw):
            off += os.write(fd, raw[off:])
        fcntl.fcntl(
            fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL,
        )
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd, None


class _CommandCapture:
    __slots__ = ("active",)

    def __init__(self):
        self.active = True


_COMMAND_CAPTURE = ContextVar("worker_bootstrap_command_observation", default=None)
_COMMAND_RESULTS = frozenset(
    {
        "read_failed",
        "empty_read",
        "decode_failed",
        "missing_nonce",
        "missing_bootstrap",
        "missing_both",
        "matched",
    }
)
_BOOTSTRAP_EXIT_RESULTS = frozenset(
    {"not_reported", "exited_zero", "exited_nonzero", "terminated", "unavailable"}
)


def _record_command_observation(result):
    capture = None
    try:
        state = _COMMAND_CAPTURE.get()
        if type(state) is not tuple or len(state) != 2:
            return
        capture, outcomes = state
        if type(capture) is not _CommandCapture or not capture.active:
            return
        if outcomes is None:
            return
        if (
            type(outcomes) is not tuple
            or outcomes
            or type(result) is not str
            or result not in _COMMAND_RESULTS
        ):
            _COMMAND_CAPTURE.set((capture, None))
            return
        # Immutable context-local data cannot be changed by a copied context.
        _COMMAND_CAPTURE.set((capture, (result,)))
    except BaseException:
        if type(capture) is _CommandCapture:
            capture.active = False


def _observed_cmdline_has(pid, nonce, bootstrap):
    """One fresh, bounded slot around each existing verifier helper call."""
    capture = token = None
    try:
        capture = _CommandCapture()
        token = _COMMAND_CAPTURE.set((capture, ()))
    except BaseException:
        # If storage fails during reentrancy, omit the outer observation too;
        # its slot must not accidentally capture this invocation's outcome.
        try:
            previous = _COMMAND_CAPTURE.get()
            if previous is not None:
                previous[0].active = False
        except BaseException:
            pass
        capture = None
    try:
        result = _cmdline_has(pid, nonce, bootstrap)
        detail = None
        try:
            state = _COMMAND_CAPTURE.get()
            if (
                capture is not None
                and capture.active
                and state[0] is capture
                and len(state[1]) == 1
            ):
                detail = state[1][0]
        except BaseException:
            pass
        return result, detail
    finally:
        if capture is not None:
            capture.active = False
        if token is not None:
            try:
                _COMMAND_CAPTURE.reset(token)
            except BaseException:
                pass


def _cmdline_has(pid: int, nonce: str, bootstrap: str) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read(65536)
            parts = raw.split(b"\x00")
    except OSError:
        _record_command_observation("read_failed")
        return False
    try:
        texts = [p.decode("utf-8", "replace") for p in parts]
    except Exception:
        _record_command_observation("decode_failed")
        return False
    result = nonce in texts and bootstrap in texts
    try:
        # Classify only after the original short-circuit predicate. These
        # checks use the same decoded tokens and never decide authority.
        if type(raw) is bytes and type(nonce) is str and type(bootstrap) is str:
            detail = "matched"
            if not raw:
                detail = "empty_read"
            elif not result:
                has_nonce, has_bootstrap = nonce in texts, bootstrap in texts
                detail = (
                    "missing_both"
                    if not has_nonce and not has_bootstrap
                    else "missing_nonce"
                    if not has_nonce
                    else "missing_bootstrap"
                )
            _record_command_observation(detail)
    except BaseException:
        pass
    return result


_BOOTSTRAP_REASONS = frozenset(
    {
        "pre_stat_unavailable",
        "pre_not_live",
        "pre_parent_mismatch",
        "pre_group_mismatch",
        "pre_session_mismatch",
        "pre_command_mismatch",
        "pidfd_open_failed",
        "post_observation_error",
        "post_stat_unavailable",
        "pidfd_binding_unavailable",
        "pidfd_binding_mismatch",
        "post_command_mismatch",
        "post_not_live",
        "post_parent_mismatch",
        "post_group_mismatch",
        "post_session_mismatch",
        "post_starttime_mismatch",
    }
)
_BOOTSTRAP_ERRNOS = {
    errno.EACCES: "access_denied",
    errno.EPERM: "operation_denied",
    errno.ESRCH: "process_missing",
    errno.EMFILE: "process_fd_limit",
    errno.ENFILE: "system_fd_limit",
    errno.ENOSYS: "not_supported",
    errno.EINVAL: "invalid_argument",
    errno.ENOMEM: "memory_unavailable",
}


def _bootstrap_refused(reason: str, error: OSError | None = None) -> tuple[None, None]:
    """Best-effort constant observation; diagnostics cannot grant or block refusal."""
    try:
        label = reason if type(reason) is str and reason in _BOOTSTRAP_REASONS else "unknown"
        code = getattr(error, "errno", None) if label == "pidfd_open_failed" else None
        category = _BOOTSTRAP_ERRNOS.get(code, "other") if type(code) is int else "not_reported"
        LOG.warning("worker bootstrap refused: reason=%s errno=%s", label, category)
    except Exception:
        # An unavailable/broken logging sink must not change ownership authority.
        pass
    return None, None


def _bootstrap_command_refused(reason, detail):
    result = _bootstrap_refused(reason)
    try:
        if type(detail) is str and detail in _COMMAND_RESULTS and detail != "matched":
            LOG.warning("worker bootstrap command observation: result=%s", detail)
    except BaseException:
        pass
    return result


def _cached_bootstrap_exit(proc):
    try:
        code = proc.returncode
        if code is None:
            return "not_reported"
        if type(code) is not int:
            return "unavailable"
        return "terminated" if code < 0 else "exited_zero" if code == 0 else "exited_nonzero"
    except BaseException:
        return "unavailable"


def _bootstrap_refusal_exit(before, proc):
    try:
        after = _cached_bootstrap_exit(proc)
        before = (
            before if type(before) is str and before in _BOOTSTRAP_EXIT_RESULTS else "unavailable"
        )
        after = after if type(after) is str and after in _BOOTSTRAP_EXIT_RESULTS else "unavailable"
        LOG.warning(
            "worker bootstrap refusal exit: before_grant_close=%s after_reap_attempt=%s",
            before,
            after,
        )
    except BaseException:
        pass


def _verify_bootstrap(pid: int, nonce: str, bootstrap: str) -> tuple[int | None, int | None]:
    """Verify pre-grant bootstrap identity; return (pidfd, starttime) or (None, None).

    Checks ppid/session/group/starttime plus bootstrap nonce around pidfd_open
    with an fdinfo bind-back. A foreign handle is closed without signalling.
    """
    before = _proc_stat_full(pid)
    if before is None:
        return _bootstrap_refused("pre_stat_unavailable")
    if before[0] in _ZOMBIE_STATES:
        return _bootstrap_refused("pre_not_live")
    _, ppid, pgrp, sess, start = before
    if ppid != os.getpid():
        return _bootstrap_refused("pre_parent_mismatch")
    if pgrp != pid:
        return _bootstrap_refused("pre_group_mismatch")
    if sess != pid:
        return _bootstrap_refused("pre_session_mismatch")
    cmd_ok, command_detail = _observed_cmdline_has(pid, nonce, bootstrap)
    if not cmd_ok:
        return _bootstrap_command_refused("pre_command_mismatch", command_detail)
    try:
        fd = os.pidfd_open(pid, 0)
    except OSError as exc:
        return _bootstrap_refused("pidfd_open_failed", exc)
    observation_error = False
    try:
        after = _proc_stat_full(pid)
        bound = _pidfd_pid(fd)
        cmd_ok, command_detail = _observed_cmdline_has(pid, nonce, bootstrap)
    except Exception:
        after, bound, cmd_ok = None, None, False
        observation_error = True
    if after is None or bound != pid or not cmd_ok:
        try:
            os.close(fd)
        except OSError:
            pass
        if observation_error:
            reason = "post_observation_error"
        elif after is None:
            reason = "post_stat_unavailable"
        elif bound is None:
            reason = "pidfd_binding_unavailable"
        elif bound != pid:
            reason = "pidfd_binding_mismatch"
        else:
            return _bootstrap_command_refused("post_command_mismatch", command_detail)
        return _bootstrap_refused(reason)
    a_state, a_ppid, a_pgrp, a_sess, a_start = after
    if a_state in _ZOMBIE_STATES:
        reason = "post_not_live"
    elif a_ppid != os.getpid():
        reason = "post_parent_mismatch"
    elif a_pgrp != pid:
        reason = "post_group_mismatch"
    elif a_sess != pid:
        reason = "post_session_mismatch"
    elif a_start != start:
        reason = "post_starttime_mismatch"
    else:
        return fd, start
    try:
        os.close(fd)
    except OSError:
        pass
    return _bootstrap_refused(reason)


def _mem_available_bytes() -> int | None:
    try:
        with open("/proc/meminfo", "rb") as fh:
            for line in fh.read().split(b"\n"):
                if line.startswith(b"MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


@dataclass(frozen=True, eq=False)
class WorkerIdentity:
    """Opaque, in-process identity observation; a PID is never cleanup authority.

    Only the exact object retained by its manager can select an incarnation.
    No process handle or mutable lifecycle state is exposed to the caller.
    """

    worker_id: str
    pid: int
    start_time: int


@dataclass
class _Managed:
    spec: WorkerSpec
    process: asyncio.subprocess.Process | None = None
    pgid: int | None = None
    leader_starttime: int | None = None
    leader_fd: int | None = None
    member_fds: dict[int, int] = field(default_factory=dict)
    state: str = "stopped"
    ready: bool = False
    readiness: str = "not-started"
    error: str | None = None
    stdout_tail: bytearray = field(default_factory=bytearray)
    stderr_tail: bytearray = field(default_factory=bytearray)
    drains: list[asyncio.Task[None]] = field(default_factory=list)
    last_transition: dict[str, Any] | None = None
    stop_evidence: dict[str, Any] | None = None
    granted: bool = False
    grant_w: int | None = None
    pending_spawn: asyncio.Task[Any] | None = None
    pending_cleanup: asyncio.Task[Any] | None = None
    pending_fds: list[int] = field(default_factory=list)
    pending_obj: Any | None = None
    identity: WorkerIdentity | None = None
    identity_process: asyncio.subprocess.Process | None = None


ReadinessProbe = Callable[[WorkerSpec, asyncio.subprocess.Process], bool | Awaitable[bool]]


class WorkerManager:
    """Owns up to 16 worker processes defined solely by owner configuration.

    One lock serializes :meth:`start`/``stop``/``restart``/``close``;
    :meth:`status` is synchronous and never awaits a probe or hung worker.
    Each spawn runs trusted ``_bootstrap.py`` via ``sys.executable`` with
    ``start_new_session=True``; the bootstrap blocks for a one-byte parent
    grant, then execs the configured executable with the minimal explicit env
    and cwd. Stdout/stderr drain in the background with 64 KiB tails per stream
    so flood output neither blocks the child nor exhausts memory; status reports
    bounded byte counts, never raw content.

    Signals travel only through pidfds adopted while the verified granted leader
    is alive. A reused numeric PID/group can therefore never receive cleanup
    signals. Anything unattributable fails closed and blocks restart.
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
        self._identities: weakref.WeakSet[WorkerIdentity] = weakref.WeakSet()

    def status(self) -> dict[str, Any]:
        workers = {}
        for wid, managed in self._workers.items():
            proc = managed.process
            pid = proc.pid if proc is not None else None
            state, error, ready, readiness = (
                managed.state,
                managed.error,
                managed.ready,
                managed.readiness,
            )
            if (
                proc is not None
                and proc.returncode is not None
                and state in ("running", "starting")
            ):
                # Reconcile: the real process exited, so cached ready is a lie.
                state, error, ready, readiness = "failed", error or "early-exit", False, "failed"
            workers[wid] = {
                "id": wid,
                "state": state,
                "ready": ready,
                "readiness": readiness,
                "owned_pid": pid,
                "owned_pgid": managed.pgid,
                "error": error,
                "rss_bytes": _rss_bytes(pid)
                if pid is not None and self._identity_ok(managed)
                else None,
                "group_rss_bytes": self._group_rss(managed),
                "mem_available_bytes": _mem_available_bytes(),
                # Raw child output is never exposed: known-secret scrubbing cannot
                # prove safety (splits across streams, tail truncation), so the
                # tails stay empty and status carries bounded diagnostic counts.
                "log_tail_stdout": "",
                "log_tail_stderr": "",
                "log_stdout_bytes": len(managed.stdout_tail),
                "log_stderr_bytes": len(managed.stderr_tail),
                "last_transition": copy.deepcopy(managed.last_transition),
                "stop_evidence": copy.deepcopy(managed.stop_evidence),
            }
        return {
            "workers": workers,
            "worker_count": len(workers),
            "closed": self._closed,
            "gpu": "unknown/unverified",
        }

    @staticmethod
    def _group_rss(managed: _Managed) -> int | None:
        if managed.pgid is None or not WorkerManager._identity_ok(managed):
            return None
        members, _ = _scan_group(managed.pgid)
        values = [v for pid in members if (v := _rss_bytes(pid)) is not None]
        return sum(values) if values else None

    @staticmethod
    def _identity_ok(managed: _Managed) -> bool:
        """True only when the granted leader still matches its kernel identity.

        Asyncio child watchers may reap exited children automatically, so a
        numeric PID alone proves nothing; this requires the verified pidfd plus
        ppid/session/group/starttime. Gates member adoption and measurement.
        """
        if (
            managed.process is None
            or managed.pgid is None
            or managed.leader_starttime is None
            or managed.leader_fd is None
            or not managed.granted
        ):
            return False
        stat = _proc_stat_full(managed.process.pid)
        return (
            stat is not None
            and _pidfd_pid(managed.leader_fd) == managed.process.pid
            and stat[0] not in _ZOMBIE_STATES
            and stat[1] == os.getpid()
            and stat[2] == managed.pgid
            and stat[3] == managed.pgid
            and stat[4] == managed.leader_starttime
        )

    @staticmethod
    def _has_pending(managed: _Managed) -> bool:
        """True while an unsettled spawn still owns this slot's future."""
        spawn_active = managed.pending_spawn is not None and not managed.pending_spawn.done()
        cleanup_active = managed.pending_cleanup is not None and not managed.pending_cleanup.done()
        if spawn_active or cleanup_active:
            return True
        if (managed.pending_spawn is not None and managed.pending_spawn.done()) and (
            managed.pending_cleanup is None or managed.pending_cleanup.done()
        ):
            # Settled in the background without observation; drop bookkeeping.
            # Fds were closed by the reaper; keep process/pgid as-is.
            managed.pending_spawn = None
            managed.pending_cleanup = None
            managed.pending_fds = []
            managed.pending_obj = None
            return False
        if managed.pending_spawn is not None or managed.pending_cleanup is not None:
            # One side done, the other still reaping: still pending.
            return True
        return False

    async def start(self, worker_id: str) -> dict[str, Any]:
        async with self._lock:
            return await self._start_locked(worker_id)

    async def stop(self, worker_id: str) -> dict[str, Any]:
        async with self._lock:
            return await self._stop_locked(worker_id)

    def owned_identity(self, worker_id: str) -> WorkerIdentity | None:
        """Observe only a running, ready, kernel-verified owned incarnation."""
        managed = self._workers.get(worker_id)
        if (
            self._closed
            or managed is None
            or managed.state != "running"
            or not managed.ready
            or managed.identity_process is not managed.process
            or not self._identity_ok(managed)
        ):
            return None
        return managed.identity

    def is_current(self, identity: WorkerIdentity) -> bool:
        return (
            isinstance(identity, WorkerIdentity)
            and self.owned_identity(identity.worker_id) is identity
        )

    async def stop_if_current(self, identity: WorkerIdentity) -> dict[str, Any]:
        """Stop this exact binding, including an exited leader's retained group.

        Superseded requests send no signal and invent no stop evidence. A new
        binding is possible only after the existing start/quiescence gate has
        confirmed release of the previous process/group. Unlike is_current,
        this comparison deliberately does not require a live/ready leader.
        """
        if not isinstance(identity, WorkerIdentity):
            raise TypeError("Cleanup requires an owned WorkerIdentity")
        async with self._lock:
            if identity not in self._identities:
                return {"ok": False, "worker_id": identity.worker_id, "error": "unowned-identity"}
            managed = self._workers.get(identity.worker_id)
            if (
                managed is None
                or managed.identity is not identity
                or managed.identity_process is not managed.process
                or managed.leader_starttime != identity.start_time
            ):
                if self._closed:
                    return {"ok": False, "worker_id": identity.worker_id, "error": "closed"}
                return {"ok": True, "worker_id": identity.worker_id, "superseded": True}
            result = await self._stop_locked(identity.worker_id)
            evidence = managed.stop_evidence or {}
            if (
                result.get("ok")
                and result.get("already")
                and managed.state == "stopped"
                and managed.pgid is None
                and evidence.get("reaped")
                and evidence.get("group_empty")
            ):
                # Retained verified release, not a fresh signal/RSS/stop event.
                result = {**result, "release_confirmed": True}
            return result

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
            return {
                "ok": start_result["ok"],
                "worker_id": worker_id,
                "restarted": start_result["ok"],
                "old_pid": old_pid,
                "pid": start_result.get("pid"),
                "ready": start_result.get("ready", False),
                "readiness": start_result.get("readiness"),
                "error": start_result.get("error"),
                "stop": stop_result,
            }

    async def close(self) -> None:
        """Stop every worker, retaining cleanup authority for any incomplete stop."""
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
                raise RuntimeError(
                    f"close incomplete; attempted all workers: {'; '.join(failures)}"
                )

    def _note(self, managed: _Managed, event: str, duration_s: float) -> None:
        managed.last_transition = {"event": event, "at": _utcnow(), "duration_s": duration_s}

    async def _start_locked(self, worker_id: str) -> dict[str, Any]:
        managed = self._workers.get(worker_id)
        if managed is None:
            return {"ok": False, "worker_id": worker_id, "error": "unknown-worker"}
        if self._closed:
            return {"ok": False, "worker_id": worker_id, "error": "closed"}
        if self._has_pending(managed):
            return {"ok": False, "worker_id": worker_id, "error": "ownership-unverified"}
        proc = managed.process
        if proc is not None and proc.returncode is None and managed.state == "running":
            return {
                "ok": True,
                "worker_id": worker_id,
                "pid": proc.pid,
                "reused": True,
                "ready": managed.ready,
                "readiness": managed.readiness,
            }
        if (
            proc is not None
            or managed.pgid is not None
            or managed.leader_fd is not None
            or managed.member_fds
            or managed.granted
            or managed.grant_w is not None
        ):
            clean, reason = self._quiesced(managed)
            if not clean:
                return {"ok": False, "worker_id": worker_id, "error": reason}
            self._release_handles(managed)
            managed.pgid = None
            managed.granted = False
        if not _pidfd_supported():
            managed.state, managed.ready, managed.readiness = "failed", False, "failed"
            managed.error = "unsupported-platform"
            self._note(managed, "failed", 0.0)
            LOG.warning("worker failed: id=%s category=%s", managed.spec.id, "unsupported-platform")
            return {"ok": False, "worker_id": worker_id, "error": "unsupported-platform"}
        t0 = time.monotonic()
        spec = managed.spec
        managed.state, managed.ready, managed.readiness, managed.error = (
            "starting",
            False,
            "starting",
            None,
        )
        managed.stdout_tail.clear()
        managed.stderr_tail.clear()
        managed.granted = False
        managed.leader_fd = None
        managed.leader_starttime = None
        nonce = secrets.token_hex(16)
        bootstrap = _bootstrap_path()
        try:
            raw = json.dumps(
                {
                    "nonce": nonce,
                    "parent_pid": os.getpid(),
                    "argv": list(spec.argv),
                    "cwd": str(spec.cwd),
                    "env": spec.child_env(),
                },
                separators=(",", ":"),
            ).encode("utf-8")
        except Exception:
            return await self._fail_start(managed, t0, "spawn-failed")
        if len(raw) > MAX_CONFIG_BYTES:
            return await self._fail_start(managed, t0, "spawn-failed")
        try:
            grant_r, grant_w = os.pipe()
        except OSError:
            return await self._fail_start(managed, t0, "spawn-failed")
        try:
            config_fd, config_obj = _make_config_fd(raw)
        except Exception:
            for fd in (grant_r, grant_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            return await self._fail_start(managed, t0, "spawn-failed")
        spawn_task = asyncio.ensure_future(
            asyncio.create_subprocess_exec(
                sys.executable,
                bootstrap,
                str(grant_r),
                str(config_fd),
                nonce,
                cwd=str(spec.cwd),
                env={"PATH": DEFAULT_PATH, "LANG": DEFAULT_LANG},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=TAIL_MAX_BYTES,
                pass_fds=(grant_r, config_fd),
            )
        )
        try:
            proc = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            await self._settle_cancelled_spawn(
                managed, spawn_task, t0, grant_w, grant_r, config_fd, config_obj
            )
            raise
        except Exception:
            for fd in (grant_r, grant_w, config_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                if config_obj is not None:
                    config_obj.close()
            except Exception:
                pass
            managed.process = None
            managed.pgid = None
            return await self._fail_start(managed, t0, "spawn-failed")
        for fd in (grant_r, config_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if config_obj is not None:
                config_obj.close()
        except Exception:
            pass
        managed.process = proc
        managed.pgid = proc.pid
        managed.grant_w = grant_w
        fd, start = _verify_bootstrap(proc.pid, nonce, bootstrap)
        if fd is None:
            before_grant_close = _cached_bootstrap_exit(proc)
            try:
                os.close(grant_w)
            except OSError:
                pass
            managed.grant_w = None
            managed.leader_fd = None
            managed.leader_starttime = None
            managed.granted = False
            try:
                return await self._fail_unverified(managed, t0)
            finally:
                _bootstrap_refusal_exit(before_grant_close, proc)
        managed.leader_fd = fd
        managed.leader_starttime = start
        managed.identity = WorkerIdentity(worker_id, proc.pid, start)
        managed.identity_process = proc
        self._identities.add(managed.identity)
        try:
            os.write(grant_w, GRANT_BYTE)
        except OSError:
            try:
                os.close(grant_w)
            except OSError:
                pass
            managed.grant_w = None
            return await self._fail_start(managed, t0, "ownership-unverified")
        try:
            os.close(grant_w)
        except OSError:
            pass
        managed.grant_w = None
        managed.granted = True
        managed.drains = [
            asyncio.ensure_future(self._drain(proc.stdout, managed.stdout_tail)),
            asyncio.ensure_future(self._drain(proc.stderr, managed.stderr_tail)),
        ]
        self._capture_members(managed)
        try:
            return await self._await_ready(managed, t0)
        except asyncio.CancelledError:
            await self._emergency_cleanup(managed)
            managed.state = "failed"
            managed.error = "cancelled"
            managed.ready = False
            managed.readiness = "failed"
            self._note(managed, "failed", time.monotonic() - t0)
            raise

    async def _fail_unverified(self, managed: _Managed, t0: float) -> dict[str, Any]:
        """Reap an unverified bootstrap without signals; configured code never ran."""
        proc = managed.process
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=REAP_GRACE_S)
            except BaseException:
                pass
            try:
                await self._finalize_pipes(managed)
            except BaseException:
                pass
        managed.state, managed.ready, managed.readiness = "failed", False, "failed"
        managed.error = "ownership-unverified"
        self._note(managed, "failed", time.monotonic() - t0)
        LOG.warning("worker failed: id=%s category=%s", managed.spec.id, "ownership-unverified")
        result: dict[str, Any] = {
            "ok": False,
            "worker_id": managed.spec.id,
            "error": "ownership-unverified",
        }
        if proc is not None and proc.returncode is not None:
            result["returncode"] = proc.returncode
        return result

    async def _settle_cancelled_spawn(
        self,
        managed: _Managed,
        spawn_task: asyncio.Task[Any],
        t0: float,
        grant_w: int,
        grant_r: int,
        config_fd: int,
        config_obj: Any | None,
    ) -> None:
        """Settle a cancelled spawn without ever granting execution.

        Closes the grant writer immediately so the bootstrap exits by itself;
        retains read/config fds until the spawn settles to prevent fd-number
        reuse. A still-pending spawn is preserved with a reaping callback and
        never overwritten; it can never exec (grant closed).
        """
        try:
            os.close(grant_w)
        except OSError:
            pass
        managed.grant_w = None
        managed.granted = False
        retain = [fd for fd in (grant_r, config_fd) if fd is not None]
        managed.pending_fds = list(retain)
        managed.pending_obj = config_obj
        managed.pending_spawn = spawn_task
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.wait_for(asyncio.shield(spawn_task), timeout=SPAWN_SETTLE_S)
        except BaseException:
            if spawn_task.done() and not spawn_task.cancelled():
                try:
                    maybe = spawn_task.result()
                except BaseException:
                    maybe = None
                if isinstance(maybe, asyncio.subprocess.Process):
                    proc = maybe
            else:
                if not spawn_task.done():

                    async def _reap() -> None:
                        try:
                            pending_proc = await spawn_task
                        except BaseException:
                            pending_proc = None
                        try:
                            if isinstance(pending_proc, asyncio.subprocess.Process):
                                try:
                                    await asyncio.wait_for(
                                        pending_proc.wait(), timeout=REAP_GRACE_S
                                    )
                                except BaseException:
                                    pass
                        finally:
                            for fd in retain:
                                try:
                                    os.close(fd)
                                except OSError:
                                    pass
                            try:
                                if config_obj is not None:
                                    config_obj.close()
                            except Exception:
                                pass
                            managed.pending_spawn = None
                            managed.pending_cleanup = None
                            managed.pending_fds = []
                            managed.pending_obj = None

                    managed.pending_cleanup = asyncio.ensure_future(_reap())
                    managed.process = None
                    managed.pgid = None
                    managed.leader_fd = None
                    managed.leader_starttime = None
                    managed.state, managed.ready, managed.readiness = "failed", False, "failed"
                    managed.error = "cancelled"
                    self._note(managed, "failed", time.monotonic() - t0)
                    LOG.warning("worker cancelled during spawn: id=%s", managed.spec.id)
                    return
                proc = None
        managed.pending_spawn = None
        managed.pending_cleanup = None
        managed.pending_fds = []
        managed.pending_obj = None
        for fd in retain:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if config_obj is not None:
                config_obj.close()
        except Exception:
            pass
        if proc is None:
            managed.process = None
            managed.pgid = None
            managed.leader_fd = None
            managed.leader_starttime = None
            managed.state, managed.ready, managed.readiness = "failed", False, "failed"
            managed.error = "cancelled"
            self._note(managed, "failed", time.monotonic() - t0)
            LOG.warning("worker cancelled during spawn: id=%s", managed.spec.id)
            return
        managed.process = proc
        managed.pgid = proc.pid
        managed.leader_fd = None
        managed.leader_starttime = None
        try:
            await asyncio.wait_for(proc.wait(), timeout=REAP_GRACE_S)
        except BaseException:
            pass
        try:
            await self._finalize_pipes(managed)
        except BaseException:
            pass
        managed.state, managed.ready, managed.readiness = "failed", False, "failed"
        managed.error = "cancelled"
        self._note(managed, "failed", time.monotonic() - t0)
        LOG.warning("worker cancelled during spawn: id=%s", managed.spec.id)

    @staticmethod
    async def _wait_reap(proc: asyncio.subprocess.Process | None) -> None:
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
        except (TimeoutError, Exception):
            pass

    def _quiesced(self, managed: _Managed) -> tuple[bool, str]:
        """True when no owned process/group remains. Inspects only; never signals."""
        if self._has_pending(managed):
            return False, "ownership-unverified"
        proc = managed.process
        if proc is not None and proc.returncode is None:
            return False, "cleanup-incomplete"  # old leader still alive: stop first
        if managed.leader_fd is not None and _pidfd_alive(managed.leader_fd):
            return False, "cleanup-incomplete"
        if any(_pidfd_alive(fd) for fd in managed.member_fds.values()):
            return False, "cleanup-incomplete"
        if managed.pgid is not None:
            members, ok = _scan_group(managed.pgid)
            if not ok:
                return False, "ownership-unverified"  # no enumeration: not an empty proof
            if any(state not in _ZOMBIE_STATES for state, _, _ in members.values()):
                return False, "ownership-unverified"  # occupants we cannot attribute
        return True, ""

    @staticmethod
    def _release_handles(managed: _Managed) -> None:
        """Close pidfds only after verified cleanup."""
        if managed.leader_fd is not None:
            try:
                os.close(managed.leader_fd)
            except OSError:
                pass
            managed.leader_fd = None
        for fd in managed.member_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        managed.member_fds.clear()
        if managed.grant_w is not None:
            try:
                os.close(managed.grant_w)
            except OSError:
                pass
            managed.grant_w = None
        managed.granted = False

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
                if proc.returncode is not None:
                    # Probe passed for a process that already exited: stale.
                    return await self._fail_start(managed, t0, "early-exit")
                managed.state = "running"
                managed.ready = self._readiness is not None
                managed.readiness = "verified" if self._readiness is not None else "unverified"
                self._note(managed, "started", time.monotonic() - t0)
                return {
                    "ok": True,
                    "worker_id": spec.id,
                    "pid": proc.pid,
                    "reused": False,
                    "ready": managed.ready,
                    "readiness": managed.readiness,
                    "duration_s": time.monotonic() - t0,
                }
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
        # Cancelling readers alone leaves pipe transports bound to an event
        # loop that may then close. Close the owned read ends explicitly; this
        # never sends a signal to a numeric PID or invokes Process.kill().
        transport = getattr(managed.process, "_transport", None)
        if transport is not None:
            for descriptor in (1, 2):
                pipe = transport.get_pipe_transport(descriptor)
                if pipe is not None:
                    pipe.close()
            await asyncio.sleep(0)

    def _capture_members(self, managed: _Managed) -> None:
        """Adopt pidfds for current group members. Identity-gated; never signals.

        Runs after the grant and immediately before stop, while the granted
        leader is verifiably alive, so every adopted handle is provably ours.
        A lost leader handle is never reopened numerically. After the leader
        exits only already-adopted handles may be signalled.
        """
        if managed.pgid is None:
            return
        if not self._identity_ok(managed):
            return
        self._tracked_alive(managed)  # prune dead handles (bookkeeping, not ownership release)
        members, ok = _scan_group(managed.pgid)
        if not ok:
            return
        leader_pid = managed.process.pid if managed.process is not None else -1
        for pid in members:
            if pid == leader_pid or pid in managed.member_fds:
                continue
            fd = _adopt_member(managed.pgid, pid)
            if fd is not None:
                managed.member_fds[pid] = fd

    def _signal_tracked(self, managed: _Managed, sig: int) -> bool:
        """Send ``sig`` through stable handles only. Never touches a numeric PID."""
        sent = False
        proc = managed.process
        if proc is not None and proc.returncode is None and managed.leader_fd is not None:
            if _pidfd_send(managed.leader_fd, sig):
                sent = True
        for fd in list(managed.member_fds.values()):
            if _pidfd_send(fd, sig):
                sent = True
        return sent

    def _tracked_alive(self, managed: _Managed) -> list[int]:
        """Live tracked member PIDs, pruning dead handles. Never signals numerically."""
        live = []
        for pid, fd in list(managed.member_fds.items()):
            if _pidfd_alive(fd):
                live.append(pid)
            else:
                try:
                    os.close(fd)
                except OSError:
                    pass
                del managed.member_fds[pid]
        return live

    def _diagnostic_scan(self, managed: _Managed) -> tuple[list[int], bool]:
        """Live numeric group members for evidence only. Never signalled."""
        if managed.pgid is None:
            return [], True
        members, ok = _scan_group(managed.pgid)
        if not ok:
            return [], False
        return [pid for pid, (state, _, _) in members.items() if state not in _ZOMBIE_STATES], True

    async def _fail_start(self, managed: _Managed, t0: float, error: str) -> dict[str, Any]:
        """Clean up an owned group after a failed start and report honestly."""
        proc = managed.process
        if proc is not None:
            self._capture_members(managed)
            self._signal_tracked(managed, signal.SIGTERM)
            await asyncio.sleep(PROBE_INTERVAL_S)
            live = self._tracked_alive(managed)
            if proc.returncode is None or live:
                self._signal_tracked(managed, signal.SIGKILL)
            await self._wait_reap(proc)
            await self._finalize_pipes(managed)
            clean, _ = self._quiesced(managed)
            if clean:
                self._release_handles(managed)
                managed.pgid = None
            else:
                error = "ownership-unverified"  # could not prove cleanup: say so
        managed.state = "failed"
        managed.error = error
        managed.ready = False
        managed.readiness = "failed"
        self._note(managed, "failed", time.monotonic() - t0)
        LOG.warning("worker failed: id=%s category=%s", managed.spec.id, error)
        result: dict[str, Any] = {"ok": False, "worker_id": managed.spec.id, "error": error}
        if proc is not None and proc.returncode is not None:
            result["returncode"] = proc.returncode
        return result

    async def _stop_locked(self, worker_id: str) -> dict[str, Any]:
        managed = self._workers.get(worker_id)
        if managed is None:
            return {"ok": False, "worker_id": worker_id, "error": "unknown-worker"}
        if self._has_pending(managed):
            return {"ok": False, "worker_id": worker_id, "error": "ownership-unverified"}
        proc = managed.process
        t0 = time.monotonic()
        if proc is None:
            if managed.state != "stopped":
                managed.state = "stopped"
                self._note(managed, "stopped", 0.0)
            return {"ok": True, "worker_id": worker_id, "already": True}
        if managed.state == "stopped" and managed.pgid is None:
            # Earlier stop verified cleanup and released every handle: there is
            # nothing owned left to inspect or signal.
            return {"ok": True, "worker_id": worker_id, "already": True}
        pid, pgid = proc.pid, managed.pgid
        rss_before = _rss_bytes(pid) if self._identity_ok(managed) else None
        group_rss_before = self._group_rss(managed)
        evidence: dict[str, Any] = {
            "pid": pid,
            "pgid": pgid,
            "rss_before_bytes": rss_before,
            "group_rss_before_bytes": group_rss_before,
            "mem_available_bytes": _mem_available_bytes(),
            "term_sent": False,
            "kill_sent": False,
            "reaped": False,
            "group_empty": False,
            "residual_pids": [],
            "ownership_verified": False,
        }
        try:
            # Adopt current members while the leader is verifiably alive; after
            # this point only already-verified handles may be signalled.
            self._capture_members(managed)
            evidence["term_sent"] = self._signal_tracked(managed, signal.SIGTERM)
            deadline = time.monotonic() + float(managed.spec.stop_timeout_s)
            escalated = False
            verdict = ""
            residual: list[int] = []
            while True:
                if proc.returncode is not None:
                    try:
                        await proc.wait()
                    except Exception:
                        pass
                live_tracked = self._tracked_alive(managed)
                leader_done = proc.returncode is not None
                if leader_done and not live_tracked:
                    scan_live, scan_ok = self._diagnostic_scan(managed)
                    if not scan_ok:
                        residual = []
                        verdict = "ownership-unverified"  # no enumeration: not an empty proof
                        break
                    if scan_live:
                        # Occupants we cannot attribute (untracked descendant or a
                        # foreign group reusing the number): refuse, never signal.
                        residual = scan_live[:MAX_WORKERS]
                        verdict = "ownership-unverified"
                        break
                    verdict = "stopped"
                    break
                if time.monotonic() >= deadline:
                    if not escalated and (not leader_done or live_tracked):
                        if self._signal_tracked(managed, signal.SIGKILL):
                            evidence["kill_sent"] = True
                        escalated = True
                        deadline = time.monotonic() + KILL_GRACE_S
                        continue
                    if live_tracked or not leader_done:
                        residual = ([pid] if not leader_done else []) + live_tracked
                        verdict = "stop-incomplete"  # known-owned survivors: honest failure
                    else:
                        scan_live, scan_ok = self._diagnostic_scan(managed)
                        if not scan_ok or scan_live:
                            residual = scan_live[:MAX_WORKERS]
                            verdict = "ownership-unverified"
                        else:
                            verdict = "stopped"
                    break
                await asyncio.sleep(PROBE_INTERVAL_S)
            await self._wait_reap(proc)
            await self._finalize_pipes(managed)
            evidence["reaped"] = proc.returncode is not None
            evidence["residual_pids"] = residual[:MAX_WORKERS]
            evidence["duration_s"] = time.monotonic() - t0
            evidence["at"] = _utcnow()
            managed.stop_evidence = evidence
            if verdict == "stopped":
                evidence["group_empty"] = True
                evidence["ownership_verified"] = True
                self._release_handles(managed)  # verified clean: handles may go
                managed.pgid = None
                managed.state = "stopped"
                managed.ready = False
                managed.readiness = "not-started" if managed.error else managed.readiness
                managed.error = None
                self._note(managed, "stopped", evidence["duration_s"])
                return {
                    "ok": True,
                    "worker_id": worker_id,
                    "pid": pid,
                    "stop_evidence": copy.deepcopy(evidence),
                }
            evidence["group_empty"] = not residual
            managed.state = "failed"
            managed.error = verdict
            managed.ready = False
            self._note(managed, "failed", evidence["duration_s"])
            LOG.warning("worker stop incomplete: id=%s category=%s", worker_id, verdict)
            return {
                "ok": False,
                "worker_id": worker_id,
                "error": verdict,
                "stop_evidence": copy.deepcopy(evidence),
            }
        except asyncio.CancelledError:
            await self._emergency_cleanup(managed)
            managed.state = "failed"
            managed.error = "cancelled"
            managed.ready = False
            managed.stop_evidence = evidence
            raise
        except Exception as exc:
            evidence["duration_s"] = time.monotonic() - t0
            managed.stop_evidence = evidence
            managed.state = "failed"
            managed.error = "stop-error"
            LOG.warning("worker stop error: id=%s category=%s", worker_id, type(exc).__name__)
            raise

    async def _emergency_cleanup(self, managed: _Managed) -> None:
        """Best-effort pidfd-only cleanup for cancellation paths. Claims nothing."""
        try:
            self._capture_members(managed)
        except Exception:
            pass
        try:
            self._signal_tracked(managed, signal.SIGTERM)
        except Exception:
            pass
        proc = managed.process
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=PROBE_INTERVAL_S)
            except (TimeoutError, Exception):
                pass
        try:
            for pid in self._tracked_alive(managed):
                fd = managed.member_fds.get(pid)
                if fd is not None:
                    _pidfd_send(fd, signal.SIGKILL)
            if proc is not None and proc.returncode is None and managed.leader_fd is not None:
                _pidfd_send(managed.leader_fd, signal.SIGKILL)
        except Exception:
            pass
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
            except (TimeoutError, Exception):
                pass
        try:
            await self._finalize_pipes(managed)
        except Exception:
            pass
