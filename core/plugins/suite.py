"""Owner evaluation suites for compute plugins.

Suite file format (JSON)::

    {"schema_version": "1.0",
     "plugin_id": "<id>",
     "cases": [{"input": {...}, "expected": <JSON>}, ...]}

Rules: file <=256KiB; ``cases`` non-empty with at most 64 entries; each
case has an object ``input`` and any JSON ``expected``. The suite is an
OWNER configuration mapping plugin_id->Path outside the task workspace;
the manifest and the candidate cannot pick criteria or paths. The suite
is pinned by sha256 of its canonical JSON; promotion refuses if the suite
file changed after evaluation. Case inputs/outputs reuse the 64KiB JSON
value bounds.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.plugins.storage import read_bytes

MAX_SUITE_BYTES = 256 * 1024
MAX_SUITE_CASES = 64


def canonical_bytes(doc: Any) -> bytes:
    """Canonical JSON bytes used for pinning hashes."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def suite_hash_of(suite_path: Path | str) -> str:
    """sha256 of the canonical suite bytes (missing file -> integrity error)."""
    from core.plugins.errors import PluginIntegrityError

    try:
        doc = json.loads(read_bytes(Path(suite_path), MAX_SUITE_BYTES))
        return hashlib.sha256(canonical_bytes(doc)).hexdigest()
    except (OSError, ValueError, RecursionError) as exc:
        raise PluginIntegrityError(f"suite unreadable: {exc}") from exc


def load_suite(suite_path: Path | str, *, expected_plugin_id: str) -> tuple[list[dict], str]:
    """Validate the suite file; return (cases, suite_hash)."""
    from core.plugins.errors import PluginIntegrityError
    from core.plugins.schemas import check_json_value

    try:
        raw = read_bytes(Path(suite_path), MAX_SUITE_BYTES)
    except OSError as exc:
        raise PluginIntegrityError(f"suite unreadable: {exc}") from exc
    if len(raw) > MAX_SUITE_BYTES:
        raise PluginIntegrityError(f"suite exceeds {MAX_SUITE_BYTES} bytes ({len(raw)})")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PluginIntegrityError(f"suite is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise PluginIntegrityError("suite must be an object")
    if doc.get("schema_version") != "1.0":
        raise PluginIntegrityError("suite schema_version must be '1.0'")
    if doc.get("plugin_id") != expected_plugin_id:
        raise PluginIntegrityError("suite plugin_id does not match candidate plugin_id")
    cases_raw = doc.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise PluginIntegrityError("suite must contain a non-empty cases list")
    if len(cases_raw) > MAX_SUITE_CASES:
        raise PluginIntegrityError(f"suite exceeds max cases ({MAX_SUITE_CASES}): {len(cases_raw)}")
    cases: list[dict[str, Any]] = []
    for index, entry in enumerate(cases_raw):
        if not isinstance(entry, dict):
            raise PluginIntegrityError(f"suite case {index} must be an object")
        if "input" not in entry or "expected" not in entry:
            raise PluginIntegrityError(f"suite case {index} needs 'input' and 'expected'")
        suite_input = entry["input"]
        expected = entry["expected"]
        if not isinstance(suite_input, dict):
            raise PluginIntegrityError(f"suite case {index}: input must be an object")
        try:
            check_json_value(suite_input, what=f"suite case {index} input")
            check_json_value(expected, what=f"suite case {index} expected")
        except Exception as exc:
            # check_json_value raises PluginCandidateFailure; suite bounds
            # violations are owner-config integrity failures instead.
            raise PluginIntegrityError(f"suite case {index}: {exc}") from exc
        cases.append({"input": suite_input, "expected": expected})
    return cases, hashlib.sha256(canonical_bytes(doc)).hexdigest()
