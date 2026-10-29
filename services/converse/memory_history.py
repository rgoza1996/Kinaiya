"""Pure selected-recall gate for tagged assistant history.

No backend, network, model, tokenizer, or journal imports. Strict
MemoryContextPin validation only; fail-closed on any malformed, unknown,
mismatched, or unavailable input.
"""

import json
import re

from packages.contracts.memory_context import MemoryContextPin

TAG_JSON_LIMIT_BYTES = 2048

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _is_identifier(value):
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def _canonical_bytes(value):
    """Canonical JSON bytes, or None when the value is not JSON-serializable."""
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return encoded


def validate_pin(value):
    """Strictly validate a pin value, or return None when it is not usable."""
    try:
        if isinstance(value, MemoryContextPin):
            pin = value
        elif isinstance(value, dict):
            if "schema_version" not in value:
                return None
            raw = _canonical_bytes(value)
            if raw is None or len(raw) > TAG_JSON_LIMIT_BYTES:
                return None
            pin = MemoryContextPin.model_validate(value)
        else:
            return None
        dumped = pin.model_dump(mode="json")
        encoded = _canonical_bytes(dumped)
        if encoded is None or len(encoded) > TAG_JSON_LIMIT_BYTES:
            return None
        return pin
    except Exception:
        return None


def resolve_current_pin(memory_context):
    """Validate the current pin once per projection; None means absent or bad."""
    if memory_context is None:
        return None
    return validate_pin(memory_context)


def tagged_assistant_eligible(entry, source, info, current_pin, runtime_id):
    """Fail-closed eligibility for one tagged assistant entry.

    `current_pin` must already be the validated result of
    `resolve_current_pin`; None (absent or bad) excludes every tagged entry.
    Presence of the `memory_context` key marks an entry as tagged, including
    an explicit null tag.
    """
    try:
        if entry is None or source is None or info is None:
            return False
        if getattr(entry, "kind", None) != "conversation.converse":
            return False
        if getattr(entry, "origin", None) != "converse":
            return False
        payload = getattr(entry, "payload", None)
        if not isinstance(payload, dict):
            return False
        if "memory_context" not in payload:
            return False
        tag_raw = payload["memory_context"]
        if tag_raw is None:
            return False
        tag_pin = validate_pin(tag_raw)
        if tag_pin is None:
            return False
        if current_pin is None:
            return False
        if tag_pin.model_dump(mode="json") != current_pin.model_dump(mode="json"):
            return False
        if getattr(info, "local", None) is not True:
            return False
        if "content_scope" not in payload or payload["content_scope"] != "local_only":
            return False
        try:
            if (entry.session_id, entry.thread_id, entry.epoch) != (
                source.session_id,
                source.thread_id,
                source.epoch,
            ):
                return False
        except Exception:
            return False
        if not _is_identifier(runtime_id):
            return False
        if payload.get("runtime_id") != runtime_id:
            return False
        return True
    except Exception:
        return False
