"""Independent closed metadata vectors and additive schema pin."""

import hashlib
import json
from pathlib import Path

import pytest

from packages.contracts.journal_wake import (
    JournalWakeCursor,
    JournalWakeObservation,
    JournalWakeWait,
    wake_json,
)

CURSOR = {
    "schema_version": "1.0",
    "protocol": "journal_wake_v1",
    "owner_instance": "a" * 32,
    "revision": 0,
}
WAIT = {"schema_version": "1.0", "protocol": "journal_wake_v1", "cursor": CURSOR, "wait_ms": 1000}


def test_frozen_family_and_unrelated_fields_are_absent():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    pin = hashlib.sha256()
    for cls in (JournalWakeCursor, JournalWakeObservation, JournalWakeWait):
        p = root / (cls.__name__ + ".json")
        raw = p.read_bytes()
        assert json.loads(raw) == cls.model_json_schema()
        pin.update(p.name.encode() + b"\0" + raw)
    assert pin.hexdigest() == "09f80a17004329a1b6250a7b5222d4ef01856845c20f94dc921e72e0f4b5098b"


@pytest.mark.parametrize(
    "change",
    [
        {"revision": True},
        {"revision": 1.0},
        {"revision": -1},
        {"revision": 1 << 53},
        {"owner_instance": "b" * 31},
        {"owner_instance": "B" * 32},
        {"schema_version": "2.0"},
        {"process_released": True},
    ],
)
def test_unknown_or_coerced_cursor_is_redacted(change):
    with pytest.raises(ValueError):
        JournalWakeCursor.model_validate(CURSOR | change)


@pytest.mark.parametrize(
    "change",
    [
        {"changed": False},
        {"cursor": CURSOR | {"owner_instance": "b" * 32, "revision": 1}},
        {"original": WAIT | {"cursor": CURSOR | {"revision": 2}}},
        {"publication_committed": True},
        {"changed": 1},
    ],
)
def test_changed_hint_binds_original_instance_and_monotonic_revision(change):
    value = {
        "schema_version": "1.0",
        "protocol": "journal_wake_v1",
        "original": WAIT,
        "cursor": CURSOR | {"revision": 1},
        "changed": True,
    }
    with pytest.raises(ValueError):
        JournalWakeObservation.model_validate(value | change)


@pytest.mark.parametrize("raw", [b'{"x":0,"x":1}', b'{"x":NaN}', b"[" * 4097, b"\xff"])
def test_raw_metadata_refuses_ambiguous_nonfinite_oversize_or_non_utf8(raw):
    with pytest.raises((ValueError, RecursionError)):
        wake_json(raw)


def test_explicit_zero_wait_and_exact_maximum_cursor_are_valid():
    cursor = JournalWakeCursor.model_validate(CURSOR | {"revision": (1 << 53) - 1})
    request = JournalWakeWait.model_validate(WAIT | {"cursor": cursor, "wait_ms": 0})
    proof = JournalWakeObservation(
        schema_version="1.0",
        protocol="journal_wake_v1",
        original=request,
        cursor=cursor,
        changed=False,
    )
    assert proof.changed is False
