"""Frozen additive consumption bytes and malformed untrusted metadata boundaries."""

import hashlib
import json
from pathlib import Path

import pytest

from packages.contracts import live_effects

NAMES = (
    "LiveEffectQuery",
    "LiveEffectAdmit",
    "LiveEffectReceipt",
    "LiveEffectEvidence",
    "LiveEffectStart",
    "LiveEffectStarted",
    "LiveEffectFinish",
    "LiveEffectRecord",
    "LiveEffectState",
)


def test_live_effect_exports_have_separate_closed_required_bytes():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    raw = b"".join(
        (name + ".json").encode() + b"\0" + (root / (name + ".json")).read_bytes() + b"\0"
        for name in sorted(NAMES)
    )
    assert hashlib.sha256(raw).hexdigest() == (
        "1044614df72ec0f52a38947ce3063745030401ec9baa6d2144cfdbdc13e2daf9"
    )
    for name in NAMES:
        schema = json.loads((root / (name + ".json")).read_bytes())
        assert schema == getattr(live_effects, name).model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize(
    "raw",
    [
        b'{"n":1,"n":2}',
        b'{"n":{"n":1,"n":2}}',
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b'{"n":1.0}',
        b'{"n":"\xff"}',
        b'{"n":"\\ud800"}',
        b"[]",
        b'{"n":' + b"[" * 20 + b"0" + b"]" * 20 + b"}",
        b'{"n":[' + b"0," * 1100 + b"0]}",
        b" " * (live_effects.MAX_REQUEST_BYTES + 1),
    ],
)
def test_live_consumer_parser_refuses_ambiguous_or_unbounded_metadata(raw):
    with pytest.raises(ValueError):
        live_effects.effect_json(raw)


@pytest.mark.parametrize("sequence", [True, False, 1.0, "1", 0, 1 << 53])
def test_effect_evidence_requires_a_native_positive_sequence(sequence):
    with pytest.raises(ValueError):
        live_effects.LiveEffectEvidence(event_id="public-synthetic-event", sequence=sequence)


def test_live_consumer_parser_preserves_literal_valid_metadata():
    value = {"public": ["caf\u00e9 \u03a9", True, None, 1]}
    assert live_effects.effect_json(json.dumps(value).encode()) == value
