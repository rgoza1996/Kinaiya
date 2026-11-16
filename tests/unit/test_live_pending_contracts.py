"""Independent pending-discovery schema pin and original query boundaries."""

import hashlib
import json
from pathlib import Path

from packages.contracts import live_decisions_pending as contracts


def test_discovery_exports_have_separate_exact_bytes_and_closed_required_fields():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    names = sorted(["LiveDecisionsPendingQuery", "LiveDecisionsPendingPage"])
    raw = b"".join(
        (name + ".json").encode() + b"\0" + (root / (name + ".json")).read_bytes() + b"\0"
        for name in names
    )
    assert (
        hashlib.sha256(raw).hexdigest()
        == "39d6e3c27ba4e888aa8a4d08e887cf4a832c532c18bcf3fc8e69b781242727bf"
    )
    for name in names:
        schema = json.loads((root / (name + ".json")).read_bytes())
        assert schema == getattr(contracts, name).model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["properties"]["schema_version"]["const"] == "2.0"
