"""Bounded task_orchestrator.v1 computation with native mediated operations.

Generated code runs only through the existing pure-compute OS sandbox. Its
returned operation is untrusted input to the host broker, never authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
from collections.abc import Awaitable, Callable

from core.plugins.errors import PluginCandidateFailure
from core.plugins.sandbox import PluginLimits, run_plugin
from core.plugins.sourcecheck import decode_source

CONTRACT = "task_orchestrator.v1"
MAX_SOURCE_BYTES = 65_536
MAX_PAYLOAD_BYTES = 131_072
MAX_RESULT_BYTES = 65_536
MAX_PHASES = 16


class TaskHarnessFailure(ValueError):
    """The generated program or an operation did not establish a usable result."""


def json_bytes(value, maximum=MAX_PAYLOAD_BYTES):
    """Validate finite, structurally bounded JSON without coercing scalar kinds."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 16_384:
            raise TaskHarnessFailure("Harness JSON exceeds its structural bound")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise TaskHarnessFailure("Harness JSON requires string object keys")
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is int:
            if not -9_007_199_254_740_991 <= item <= 9_007_199_254_740_991:
                raise TaskHarnessFailure("Harness integer exceeds the JSON safe bound")
        elif type(item) is float:
            if not math.isfinite(item):
                raise TaskHarnessFailure("Harness JSON requires finite numbers")
        elif item is not None and type(item) not in {str, bool}:
            raise TaskHarnessFailure("Harness value is not JSON")
    try:
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TaskHarnessFailure("Harness JSON is not strict UTF-8") from exc
    if len(raw) > maximum:
        raise TaskHarnessFailure("Harness JSON exceeds its encoded byte bound")
    return raw


async def compute_phase(source: bytes, payload: dict):
    """Cancellation retires the actual sandbox worker before the caller exits."""
    cancelled = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(
            run_plugin,
            source,
            payload,
            limits=PluginLimits(output_bytes=MAX_RESULT_BYTES),
            cancel_event=cancelled,
        )
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        # Further cancellation cannot detach the owned thread/process group.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except PluginCandidateFailure as exc:
                if "cancel" not in str(exc):
                    raise
                break
        if worker.done() and not worker.cancelled():
            failure = worker.exception()
            if failure is not None and not (
                isinstance(failure, PluginCandidateFailure) and "cancel" in str(failure)
            ):
                raise failure from None
        raise


async def orchestrate(
    source: bytes,
    *,
    binding: dict,
    inputs,
    consult: Callable[[dict], Awaitable[dict]],
    current: Callable[[], None],
    compute=compute_phase,
):
    """Execute generated phases and actual broker calls; do not mark Task success.

    The native caller owns the broker, task identity and immutable source bytes.
    There is no candidate-selected generic tool, network client or callback.
    """
    if type(source) is not bytes or not 1 <= len(source) <= MAX_SOURCE_BYTES:
        raise TaskHarnessFailure("Harness source must be bounded immutable bytes")
    decode_source(source)
    source_sha256 = hashlib.sha256(source).hexdigest()
    json_bytes(binding, 4096)
    json_bytes(inputs, MAX_RESULT_BYTES)
    state, observation, receipts = None, None, []
    for phase in range(MAX_PHASES):
        current()
        payload = {
            "contract": CONTRACT,
            "binding": binding,
            "source_sha256": source_sha256,
            "input": inputs,
            "state": state,
            "observation": observation,
        }
        json_bytes(payload)
        action = await compute(source, payload)
        current()
        json_bytes(action, MAX_RESULT_BYTES)
        if type(action) is not dict:
            raise TaskHarnessFailure("Harness phase must return an action object")
        if action.get("op") == "finish":
            if set(action) != {"op", "result"} or not receipts:
                raise TaskHarnessFailure("Harness finish requires actual media consultation")
            result = {
                "contract": CONTRACT,
                "binding": binding,
                "source_sha256": source_sha256,
                "phases": phase + 1,
                "consultations": receipts,
                "result": action["result"],
                "task_completion": "not_established",
            }
            json_bytes(result, MAX_RESULT_BYTES)
            return result
        if (
            action.get("op") != "consult_media"
            or set(action) != {"op", "request", "state"}
            or type(action["request"]) is not dict
            or phase == MAX_PHASES - 1
        ):
            raise TaskHarnessFailure("Harness emitted an unsupported or exhausted action")
        state = action["state"]
        json_bytes(state, MAX_RESULT_BYTES)
        # Each iteration dispatches once. A broker refusal is never retried.
        current()
        observation = await consult(action["request"])
        current()
        raw = json_bytes(observation, MAX_RESULT_BYTES)
        receipts.append(
            {
                **{
                    key: observation[key]
                    for key in (
                        "operation",
                        "binding",
                        "source_path",
                        "source_sha256",
                        "source_bytes",
                        "content_scope",
                        "model",
                        "request_sha256",
                        "provider_request_sha256",
                        "provider_response_sha256",
                        "result_sha256",
                        "status",
                    )
                    if key in observation
                },
                "observed_result_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    raise TaskHarnessFailure("Harness exhausted its phase bound")
