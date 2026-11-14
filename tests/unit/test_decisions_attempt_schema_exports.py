"""Issue #371's additive wire family has its own pin, preserving legacy bytes."""

import hashlib
from pathlib import Path


def test_exact_decisions_attempt_schema_family_is_pinned_separately_from_legacy():
    root = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "schemas"
    names = (
        "DecisionsSource",
        "DecisionsAttemptProvider",
        "DecisionsAttemptLookup",
        "DecisionsAttemptAdmit",
        "DecisionsAttemptFinish",
        "DecisionsAttempt",
        "DecisionsAttemptState",
    )
    paths = sorted(root / f"{name}.json" for name in names)
    raw = b"".join(path.name.encode() + b"\0" + path.read_bytes() + b"\0" for path in paths)
    assert len(paths) == 7
    assert (
        hashlib.sha256(raw).hexdigest()
        == "40447a37a3263ebb365a801e146b8bef2e2549cfed2e39a69fb4160d220f0086"
    )
