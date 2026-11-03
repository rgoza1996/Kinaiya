"""Diagnostic-only observation; no timing observation supplies native authority."""

import asyncio
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

_lock = threading.Lock()
_events = deque(maxlen=512)
_totals = {}
_originals = {}
_lost_measurements = False
_began = time.perf_counter_ns()
_allowed = {"deleted", "finish", "close", "delete", "_connection_retired"}


def _start(owner, phase, *, function=None, operation=None, tasks=None):
    global _lost_measurements
    try:
        began = time.perf_counter_ns()
        cpu = time.thread_time_ns() if phase == "audit" else None
        closing = bool(owner._closing)
        if phase == "native":
            name = getattr(function, "__name__", "")
            phase = "native_" + (name if name in _allowed else "other")
        before = None if operation is None else max(0.0, operation.deadline - operation.loop.time())
        count = None if tasks is None else len(tasks)
        return phase, closing, began, cpu, operation, before, count
    except BaseException:
        _lost_measurements = True
        return None


def _record(observation, outcome, result=None):
    global _lost_measurements
    try:
        if observation is None:
            return
        phase, closing, began, cpu_began, operation, before, count = observation
        ended = time.perf_counter_ns()
        wall = ended - began
        cpu = None if cpu_began is None else time.thread_time_ns() - cpu_began
        detail = None
        if phase in {"close", "close_wait"}:
            detail = {"result_bool": result if type(result) is bool else None}
        if operation is not None:
            detail.update(
                task_count=count,
                remaining_s_before=before,
                remaining_s_after=max(0.0, operation.deadline - operation.loop.time()),
            )
        with _lock:
            key = (phase, closing)
            total = _totals.setdefault(
                key,
                {
                    "calls": 0,
                    "wall_ns": 0,
                    "cpu_ns": 0,
                    "cpu_measured": cpu is not None,
                    "max_wall_ns": 0,
                },
            )
            total["calls"] += 1
            total["wall_ns"] += wall
            total["cpu_ns"] += 0 if cpu is None else cpu
            total["max_wall_ns"] = max(total["max_wall_ns"], wall)
            _events.append(
                {
                    "phase": phase,
                    "closing": closing,
                    "start_ns": began - _began,
                    "end_ns": ended - _began,
                    "wall_ns": wall,
                    "thread_cpu_ns": cpu,
                    "outcome": outcome,
                    "detail": detail,
                }
            )
    except BaseException:
        # A failed clock/metadata/recorder observation cannot replace the original.
        _lost_measurements = True


def pytest_configure(config):
    from services.session.media_owner import MediaSessionOwner

    for name in ("_audit", "_run_native_original", "_close_wait", "close"):
        _originals[name] = getattr(MediaSessionOwner, name)

    def audit(self, token):
        observation = _start(self, "audit")
        outcome = "raised"
        try:
            value = _originals["_audit"](self, token)
            outcome = "returned"
            return value
        finally:
            _record(observation, outcome)

    async def native(self, function, *args, **kwargs):
        observation = _start(self, "native", function=function)
        outcome = "raised"
        try:
            value = await _originals["_run_native_original"](self, function, *args, **kwargs)
            outcome = "returned"
            return value
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            _record(observation, outcome)

    async def wait(self, operation, tasks):
        observation = _start(self, "close_wait", operation=operation, tasks=tasks)
        outcome, result = "raised", None
        try:
            result = await _originals["_close_wait"](self, operation, tasks)
            outcome = "returned"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            _record(observation, outcome, result)

    async def close(self):
        observation = _start(self, "close")
        outcome, result = "raised", None
        try:
            result = await _originals["close"](self)
            outcome = "returned"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            _record(observation, outcome, result)

    MediaSessionOwner._audit = audit
    MediaSessionOwner._run_native_original = native
    MediaSessionOwner._close_wait = wait
    MediaSessionOwner.close = close


def pytest_sessionfinish(session, exitstatus):
    with _lock:
        report = {
            "diagnostic_only": True,
            "pytest_exitstatus": int(exitstatus),
            "lost_measurements": _lost_measurements,
            "limits": [
                "Temporary method wrappers perturb timing; not acceptance qualification.",
                "Native wall includes queue/worker/observer overhead; audit CPU is current-thread CPU, async CPU is unmeasured.",
                "Nested/concurrent totals are not additive; only the last 512 completed observations are retained.",
                "Only completed spans are emitted; a missing span proves nothing. Native cancellation is observed after worker join.",
                "Only this phase JSON has categorical-only scope; pytest logs/JUnit have normal synthetic fixture output.",
                "No args, results beyond bool, identities, bodies, paths or SQL parameters are serialized here.",
                "Closing is sampled at entry; categories mix owners and instances, not original identity proof.",
                "Delegates originals unchanged in the reviewed Python environment; any recorder loss invalidates complete timing claims.",
            ],
            "totals": [{"phase": k[0], "closing": k[1], **v} for k, v in sorted(_totals.items())],
            "last_completed_observations": list(_events),
        }
    Path(os.environ["KINAIYA_MEDIA_PHASE_OUTPUT"]).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )


def pytest_unconfigure(config):
    if _originals:
        from services.session.media_owner import MediaSessionOwner

        for name, value in _originals.items():
            setattr(MediaSessionOwner, name, value)
