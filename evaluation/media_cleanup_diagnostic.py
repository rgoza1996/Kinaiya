"""Opt-in timing observations; recorder state supplies no native authority."""

import asyncio
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

_lock = threading.Lock()
_events = deque(maxlen=512)
_totals = {}
_originals = {}
_pending = {}
_close_snapshots = deque(maxlen=8)
_local = threading.local()
_lost_measurements = False
_sequence = 0
_pending_dropped = 0
_snapshots_dropped = 0
_began = time.perf_counter_ns()
_allowed = {"deleted", "finish", "close", "delete", "_connection_retired"}


@dataclass(frozen=True)
class _Observation:
    sequence: int
    owner: object
    phase: str
    closing: bool
    began: int
    cpu_began: int | None
    parent_sequence: int | None
    operation: object
    remaining_before: float | None
    task_count: int | None


def _start(owner, phase, *, function=None, operation=None, tasks=None, parent=None):
    global _lost_measurements, _sequence, _pending_dropped
    try:
        began = time.perf_counter_ns()
        cpu = time.thread_time_ns() if phase in {"audit", "native_worker"} else None
        if phase in {"native", "native_worker"}:
            name = getattr(function, "__name__", "")
            phase += "_" + (name if name in _allowed else "other")
        if phase == "audit":
            parent = getattr(_local, "worker_sequence", None)
        remaining = None if operation is None else operation.deadline - operation.loop.time()
        with _lock:
            _sequence += 1
            observation = _Observation(
                _sequence,
                owner,
                phase,
                bool(owner._closing),
                began,
                cpu,
                parent,
                operation,
                remaining,
                None if tasks is None else len(tasks),
            )
            if len(_pending) < 512:
                _pending[observation.sequence] = observation
            else:
                _pending_dropped += 1
                _lost_measurements = True
        return observation
    except BaseException:
        _lost_measurements = True
        return None


def _pending_view(now, owner=None):
    """Caller holds the recorder lock. Owner identity is never serialized."""
    return [
        {
            "sequence": item.sequence,
            "parent_sequence": item.parent_sequence,
            "phase": item.phase,
            "closing": item.closing,
            "start_ns": item.began - _began,
            "elapsed_wall_ns": now - item.began,
        }
        for item in _pending.values()
        if owner is None or item.owner is owner
    ]


def _record(observation, outcome, result=None):
    global _lost_measurements
    if observation is None:
        return
    try:
        ended = time.perf_counter_ns()
        wall = ended - observation.began
        cpu = (
            None if observation.cpu_began is None else time.thread_time_ns() - observation.cpu_began
        )
        detail = None
        if observation.phase in {"close", "close_wait"}:
            detail = {"result_bool": result if type(result) is bool else None}
        if observation.operation is not None:
            remaining = observation.operation.deadline - observation.operation.loop.time()
            detail.update(
                task_count=observation.task_count,
                remaining_s_before=max(0.0, observation.remaining_before),
                remaining_s_after=max(0.0, remaining),
                deadline_remaining_s_after=remaining,
                deadline_overrun_s_after=max(0.0, -remaining),
            )
        with _lock:
            key = (observation.phase, observation.closing)
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
                    "sequence": observation.sequence,
                    "parent_sequence": observation.parent_sequence,
                    "phase": observation.phase,
                    "closing": observation.closing,
                    "start_ns": observation.began - _began,
                    "end_ns": ended - _began,
                    "wall_ns": wall,
                    "thread_cpu_ns": cpu,
                    "outcome": outcome,
                    "detail": detail,
                }
            )
    except BaseException:
        _lost_measurements = True
    finally:
        try:
            with _lock:
                _pending.pop(observation.sequence, None)
        except BaseException:
            _lost_measurements = True


def _snapshot_close(owner, observation, outcome, result):
    global _lost_measurements, _snapshots_dropped
    try:
        now = time.perf_counter_ns()
        operation = owner._close_operation
        remaining = None if operation is None else operation.deadline - operation.loop.time()
        with _lock:
            pending = _pending_view(now, owner)
            if observation is not None:
                pending = [x for x in pending if x["sequence"] != observation.sequence]
            if len(_close_snapshots) == _close_snapshots.maxlen:
                _snapshots_dropped += 1
                _lost_measurements = True
            _close_snapshots.append(
                {
                    "observed_ns": now - _began,
                    "close_sequence": None if observation is None else observation.sequence,
                    "outcome": outcome,
                    "result_bool": result if type(result) is bool else None,
                    "deadline_remaining_s": remaining,
                    "deadline_overrun_s": None if remaining is None else max(0.0, -remaining),
                    "pending_same_owner": pending,
                }
            )
    except BaseException:
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

        def worker(*worker_args, **worker_kwargs):
            global _lost_measurements
            span = _start(
                self,
                "native_worker",
                function=function,
                parent=None if observation is None else observation.sequence,
            )
            previous, saved = None, False
            outcome = "raised"
            try:
                try:
                    previous = getattr(_local, "worker_sequence", None)
                    _local.worker_sequence = None if span is None else span.sequence
                    saved = True
                except BaseException:
                    _lost_measurements = True
                value = function(*worker_args, **worker_kwargs)
                outcome = "returned"
                return value
            finally:
                try:
                    if saved:
                        _local.worker_sequence = previous
                except BaseException:
                    _lost_measurements = True
                _record(span, outcome)

        outcome = "raised"
        try:
            value = await _originals["_run_native_original"](self, worker, *args, **kwargs)
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
            _snapshot_close(self, observation, outcome, result)
            _record(observation, outcome, result)

    MediaSessionOwner._audit = audit
    MediaSessionOwner._run_native_original = native
    MediaSessionOwner._close_wait = wait
    MediaSessionOwner.close = close


def pytest_sessionfinish(session, exitstatus):
    global _lost_measurements
    try:
        now = time.perf_counter_ns()
        with _lock:
            report = {
                "diagnostic_only": True,
                "pytest_exitstatus": int(exitstatus),
                "lost_measurements": _lost_measurements,
                "pending_dropped": _pending_dropped,
                "close_snapshots_dropped": _snapshots_dropped,
                "limits": [
                    "Opt-in wrappers perturb timing; this is not acceptance qualification.",
                    "The worker wrapper calls the exact original function once with unchanged arguments; callable wrapping and retained recorder references can perturb timing and object lifetime.",
                    "Native observer wall includes queue/worker/join overhead; worker and audit CPU is same-thread only; async CPU is unmeasured.",
                    "Nested/concurrent totals are not additive. Only512 completed spans,512 pending spans and8 close snapshots are retained; recorder loss invalidates completeness claims.",
                    "Recorder-local sequence numbers associate spans, never native identity or ownership. Close pending lists privately filter the same owner object.",
                    "A close snapshot preserves then-pending observations even if later fixture joins finish them. Missing spans, snapshots, counters and coroutine completion cannot prove native settlement or timeout cause.",
                    "Categories use a fixed allowlist or other. No arguments, native identities, bodies, paths, SQL, credential values or nonboolean results are serialized here.",
                    "Closing is sampled at entry. Ordinary pytest logs/JUnit have their normal synthetic fixture output, outside this categorical-report scope.",
                ],
                "totals": [
                    {"phase": k[0], "closing": k[1], **v} for k, v in sorted(_totals.items())
                ],
                "last_completed_observations": list(_events),
                "pending_observations": _pending_view(now),
                "close_snapshots": list(_close_snapshots),
            }
        Path(os.environ["KINAIYA_MEDIA_PHASE_OUTPUT"]).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
    except BaseException:
        # Missing/unwritable diagnostic output is invalid evidence, not a new
        # outcome for the original pytest computation. Root checks the artifact.
        _lost_measurements = True


def pytest_unconfigure(config):
    if _originals:
        from services.session.media_owner import MediaSessionOwner

        for name, value in _originals.items():
            setattr(MediaSessionOwner, name, value)
