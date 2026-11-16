"""Independent v2 inference wire pin; original finalized schemas stay frozen."""

import hashlib
import json
from pathlib import Path

import pytest

from packages.contracts import live_decisions_attempts as contracts

NAMES = {
    "LiveDecisionsSource",
    "LiveDecisionsAttemptLookup",
    "LiveDecisionsAttemptAdmit",
    "LiveDecisionsAttemptFinish",
    "LiveDecisionsAttempt",
    "LiveDecisionsAttemptState",
    "LiveDecisionsPublication",
    "LiveDecisionsOutcome",
    "LiveDecisionsPublicationState",
}


def test_exact_additive_schema_bytes_match_runtime_and_require_explicit_version():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    body = b"".join(
        (name + ".json").encode() + b"\0" + (root / (name + ".json")).read_bytes() + b"\0"
        for name in sorted(NAMES)
    )
    assert len(NAMES) == 9
    assert (
        hashlib.sha256(body).hexdigest()
        == "66c476073f97a5bdf9ebd4131f7dbad81106918601ef7c8d30cdde206114f9d7"
    )
    for name in NAMES:
        schema = json.loads((root / (name + ".json")).read_bytes())
        assert schema == getattr(contracts, name).model_json_schema()
        assert schema["additionalProperties"] is False
        field = (
            "contract_version" if "contract_version" in schema["properties"] else "schema_version"
        )
        if field in schema["properties"]:
            assert field in schema["required"]
            assert schema["properties"][field]["const"] == (
                2 if field == "contract_version" else "2.0"
            )


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e9999}',
        b'{"x":' + b"[" * 17 + b"1" + b"]" * 17 + b"}",
        b'{"x":[' + b"0," * 4096 + b"0]}",
    ],
)
def test_untrusted_metadata_rejects_duplicate_nonfinite_and_unbounded_structures(raw):
    with pytest.raises(ValueError):
        contracts.live_attempt_json(raw)


def test_finite_classifier_timing_and_exact_byte_limit_are_preserved():
    raw = b'{"classifier_ms":1.25}'
    assert contracts.live_attempt_json(raw, len(raw)) == {"classifier_ms": 1.25}
    with pytest.raises(ValueError):
        contracts.live_attempt_json(raw, len(raw) - 1)
