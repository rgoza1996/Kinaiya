"""Detached positive outcome knowledge from one original standing worker.

This capture is neither retirement evidence nor permission. Native settlement
must independently join the complete stored request and entry to its claim.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from packages.contracts.models import AppendRequest, JournalEntry, Task
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_agent_settlement import StandingAgentOutcomeReference
from packages.contracts.standing_execution import StandingExecutionIntent, occurrence_id
from packages.contracts.standing_resource_finalization import (
    checked_finalization,
    finalization_bytes,
)

_ERROR = "Invalid standing outcome capture"
_MAX_INTEGER = 9_007_199_254_740_991
_REQUEST_BYTES = 65536
_ENTRY_BYTES = 69632
_MAX_NODES = 33792
_STATES = {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class RetainedOutcome:
    request: bytes
    entry: bytes
    reference: StandingAgentOutcomeReference


def _tree(value):
    pending = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 20 or nodes > _MAX_NODES:
            raise ValueError(_ERROR)
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError(_ERROR)
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            item.encode("utf-8")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError(_ERROR)
        elif item is not None and type(item) not in (bool, int):
            raise ValueError(_ERROR)


def _encode(value, maximum):
    _tree(value)
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(raw) > maximum:
        raise ValueError(_ERROR)
    return raw


def _actual(value, kind):
    if type(value) is not kind:
        raise ValueError(_ERROR)
    fields = kind.model_fields
    values = getattr(value, "__dict__", None)
    supplied = getattr(value, "__pydantic_fields_set__", None)
    if (
        type(values) is not dict
        or values.keys() != fields.keys()
        or type(supplied) is not set
        or any(type(name) is not str for name in supplied)
        or not supplied <= fields.keys()
        or getattr(value, "__pydantic_extra__", None) is not None
    ):
        raise ValueError(_ERROR)
    return dict(values)


def capture_outcome(original, initial, request, entry):
    """Capture one exact positive ACK/readback, never a reconstructed outcome."""
    try:
        original = checked_finalization(original, StandingExecutionIntent)
        initial = checked_finalization(initial, StandingInitialProof).validate_request(original)
        body = _actual(request, AppendRequest)
        envelope = _actual(entry, JournalEntry)
        created = envelope["created_at"]
        if type(created) is not datetime or created.utcoffset() != timedelta(0):
            raise ValueError(_ERROR)
        envelope["created_at"] = created.isoformat().replace("+00:00", "Z")
        raw_request = _encode(body, _REQUEST_BYTES)
        raw_entry = _encode(envelope, _ENTRY_BYTES)
        # Validate the complete old envelopes from detached JSON, including
        # exact scalar types. Defaults cannot hide missing actual fields above.
        AppendRequest.model_validate_json(raw_request, strict=True)
        JournalEntry.model_validate_json(raw_entry, strict=True)
        task = original.task
        session = task.task_session
        if (
            envelope["origin"] != "agent-supervisor"
            or type(envelope["sequence"]) is not int
            or not initial.sequence < envelope["sequence"] <= _MAX_INTEGER
            or _encode(
                {name: envelope[name] for name in AppendRequest.model_fields}, _REQUEST_BYTES
            )
            != raw_request
            or body["kind"] != "agent.outcome"
            or (body["session_id"], body["thread_id"], body["epoch"], body["task_refs"])
            != (session.session_id, session.thread_id, session.epoch, [task.task_id])
        ):
            raise ValueError(_ERROR)
        payload = body["payload"]
        snapshot = payload.get("task")
        if type(snapshot) is not dict or snapshot.keys() != Task.model_fields.keys():
            raise ValueError(_ERROR)
        Task.model_validate_json(_encode(snapshot, _REQUEST_BYTES), strict=True)
        if (
            type(payload.get("state")) is not str
            or payload["state"] not in _STATES
            or snapshot["state"] != payload["state"]
            or type(snapshot["objective_revision"]) is not int
            or not 1 <= snapshot["objective_revision"] <= _MAX_INTEGER
            or (
                snapshot["task_id"],
                snapshot["session_id"],
                snapshot["thread_id"],
                snapshot["epoch"],
                snapshot["harness_version"],
                snapshot["idempotency_key"],
            )
            != (
                task.task_id,
                session.session_id,
                session.thread_id,
                session.epoch,
                task.harness_version,
                "standing:" + occurrence_id(original),
            )
            or _encode(snapshot["workspace_pin"], _REQUEST_BYTES)
            != finalization_bytes(task.workspace_pin)
            or _encode(snapshot["model_pin"], _REQUEST_BYTES) != finalization_bytes(task.model_pin)
            or any(
                name not in payload
                or _encode(payload[name], _REQUEST_BYTES) != _encode(snapshot[name], _REQUEST_BYTES)
                for name in (
                    "task_id",
                    "session_id",
                    "thread_id",
                    "epoch",
                    "objective",
                    "objective_revision",
                    "idempotency_key",
                    "source_event_ids",
                    "content_scope",
                    "evidence",
                )
            )
            or any(
                name in payload
                and _encode(payload[name], _REQUEST_BYTES)
                != _encode(snapshot[name], _REQUEST_BYTES)
                for name in ("summary", "error")
            )
        ):
            raise ValueError(_ERROR)
        reference = StandingAgentOutcomeReference(
            schema_version="1.0",
            task_id=task.task_id,
            event_id=envelope["event_id"],
            sequence=envelope["sequence"],
            objective_revision=snapshot["objective_revision"],
            state=payload["state"],
            request_sha256=hashlib.sha256(raw_request).hexdigest(),
            entry_sha256=hashlib.sha256(raw_entry).hexdigest(),
        )
        return RetainedOutcome(raw_request, raw_entry, reference)
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        raise ValueError(_ERROR) from None
