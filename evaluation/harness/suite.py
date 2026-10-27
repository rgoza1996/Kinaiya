"""Heldout suite format, loader, and pinning hash.

Suite file format (JSON)::

    {"suite_version": "1",
     "cases": [{"case_id": "c1",
                "payload": {"query": "...",
                            "files": [{"id": "f1", "path": "a.py",
                                       "text": "..."}],
                            "max_chars": 500},
                "expected_ids": ["f1"]}]}

Rules: non-empty ``cases``; unique ``case_id``; payload validated by
``core.extensions.payloads.validate_payload``; every ``expected_ids``
entry names a file in that case's payload. The suite is pinned by the
sha256 of its canonical JSON; promotion refuses if the suite file changed
after evaluation. Public development fixtures live in
``dev_fixtures.json`` and are labeled NOT-heldout; the lead generates a
fresh protected heldout before any real candidate experiment.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.extensions.payloads import validate_payload


def canonical_bytes(doc: Any) -> bytes:
    """Canonical JSON bytes used for pinning hashes."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


MAX_SUITE_CASES = 1024


def suite_hash_of(suite_path: Path | str) -> str:
    """sha256 of the canonical suite bytes (missing file -> IntegrityError)."""
    from core.extensions.errors import IntegrityError

    try:
        doc = json.loads(Path(suite_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"suite unreadable: {exc}") from exc
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def load_suite(suite_path: Path | str) -> tuple[list[dict[str, Any]], str]:
    """Validate the suite; return (cases, suite_hash)."""
    from core.extensions.errors import IntegrityError

    try:
        doc = json.loads(Path(suite_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntegrityError(f"suite unreadable: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise IntegrityError("suite must be an object with a cases list")
    if not doc["cases"]:
        raise IntegrityError("suite must contain at least one case")
    if len(doc["cases"]) > MAX_SUITE_CASES:
        raise IntegrityError(f"suite exceeds max cases ({MAX_SUITE_CASES}): {len(doc['cases'])}")
    seen: set[str] = set()
    cases: list[dict[str, Any]] = []
    for entry in doc["cases"]:
        if not isinstance(entry, dict):
            raise IntegrityError("each case must be an object")
        cid = entry.get("case_id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise IntegrityError("each case needs a unique non-empty case_id")
        seen.add(cid)
        try:
            payload = validate_payload(entry.get("payload"))
        except Exception as exc:
            raise IntegrityError(f"case {cid!r}: {exc}") from exc
        expected = entry.get("expected_ids")
        if not isinstance(expected, list) or not all(isinstance(i, str) for i in expected):
            raise IntegrityError(f"case {cid!r}: expected_ids must be strings")
        known = {f["id"] for f in payload["files"]}
        if any(i not in known for i in expected):
            raise IntegrityError(f"case {cid!r}: expected_ids outside payload")
        if len(set(expected)) != len(expected):
            raise IntegrityError(f"case {cid!r}: duplicate expected_ids")
        cases.append({"case_id": cid, "payload": payload, "expected_ids": list(expected)})
    return cases, hashlib.sha256(canonical_bytes(doc)).hexdigest()


__all__ = ["canonical_bytes", "load_suite", "suite_hash_of"]
