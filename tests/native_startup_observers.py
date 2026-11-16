"""Bounded test-only observations of existing native startup failures.

Never retry, probe a process, change an owner or interpret a failure as success.
Only static code locations, exception types and byte counts leave the wrappers.
"""

import logging
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from packages.resources.media_processes import MediaLaunch

_LOG = logging.getLogger(__name__)


def _exception_chain(error):
    chain, seen = [], set()
    while error is not None and len(chain) < 8 and id(error) not in seen:
        seen.add(id(error))
        frames, trace = [], error.__traceback__
        while trace is not None and len(frames) < 20:
            code = trace.tb_frame.f_code
            frames.append(
                {
                    "file": Path(code.co_filename).name,
                    "function": code.co_name,
                    "line": trace.tb_lineno,
                }
            )
            trace = trace.tb_next
        kind = type(error)
        chain.append({"type": f"{kind.__module__}.{kind.__qualname__}", "frames": frames})
        # Inspect hidden context without formatting errors, arguments or locals.
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    return chain


@contextmanager
def observe_standing_read(operations):
    """Tee one fixture read's original run result/exception; restore on all exits."""
    failures = []
    original = operations.run
    previous = vars(operations).get("run")
    had_instance_method = "run" in vars(operations)

    async def observed(*args, **kwargs):
        started = time.monotonic()
        try:
            return await original(*args, **kwargs)
        except Exception as error:
            if not failures:
                failures.append(
                    {
                        "owner_closed": operations._closed,
                        "active_operations": operations.active_operations,
                        "elapsed_seconds": round(time.monotonic() - started, 6),
                        "chain": _exception_chain(error),
                    }
                )
            raise

    operations.run = observed
    try:
        yield failures
    finally:
        if had_instance_method:
            operations.run = previous
        else:
            del operations.run


def observe_media_commands(monkeypatch):
    """Tee the existing bounded read; never acquire another /proc observation."""
    failures, pending = deque(maxlen=64), {}
    read_original = MediaLaunch._read_observation
    command_original = MediaLaunch._command

    def read(owner, path, limit):
        raw = read_original(owner, path, limit)
        key = id(owner)
        if key in pending and path.startswith("/proc/") and path.endswith("/cmdline"):
            kind = (
                "unavailable"
                if raw is None
                else "empty"
                if type(raw) is bytes and not raw
                else "present"
                if type(raw) is bytes
                else "invalid_type"
            )
            pending[key] = {"kind": kind, "size_bytes": len(raw) if type(raw) is bytes else None}
        return raw

    def command(owner, pid, nonce, bootstrap):
        key = id(owner)
        pending[key] = None
        try:
            result = command_original(owner, pid, nonce, bootstrap)
            if result is False:
                observation = pending[key]
                if observation is None:
                    observation = {"kind": "not_observed", "size_bytes": None}
                failures.append(observation)
                _LOG.warning("Native media command refused: %s", observation)
            return result
        finally:
            del pending[key]

    monkeypatch.setattr(MediaLaunch, "_read_observation", read)
    monkeypatch.setattr(MediaLaunch, "_command", command)
    return failures
