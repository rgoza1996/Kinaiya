"""Payload validation and budget helpers for context_selector.v1.

Budget unit (stated honestly): **characters** — ``len(text)`` of each
selected file's text, summed; the sum must be ``<= max_chars``. Character
count is an explicit proxy budget, not a token count or token upper bound.
"""

from __future__ import annotations

from typing import Any

MAX_FILES = 128
MAX_QUERY_CHARS = 20_000
MAX_TEXT_CHARS_PER_FILE = 200_000
MAX_TOTAL_INPUT_CHARS = 2_000_000
MAX_MAX_CHARS = 200_000


def validate_payload(payload: Any) -> dict:
    """Validate an untrusted payload dict; return it unchanged if valid."""
    from core.extensions.errors import CandidateFailure

    def fail(reason: str) -> CandidateFailure:
        return CandidateFailure(f"invalid payload: {reason}")

    if not isinstance(payload, dict):
        raise fail("payload must be a dict")
    query = payload.get("query")
    files = payload.get("files")
    max_chars = payload.get("max_chars")
    if not isinstance(query, str) or not query or len(query) > MAX_QUERY_CHARS:
        raise fail("query must be a non-empty string")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise fail(f"files must be a non-empty list of at most {MAX_FILES}")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        raise fail("max_chars must be an int")
    if not 1 <= max_chars <= MAX_MAX_CHARS:
        raise fail("max_chars out of bounds")
    seen: set[str] = set()
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise fail("each file must be a dict")
        fid, path, text = entry.get("id"), entry.get("path"), entry.get("text")
        if not isinstance(fid, str) or not fid or len(fid) > 256:
            raise fail("each file needs a non-empty string id")
        if fid in seen:
            raise fail(f"duplicate file id: {fid!r}")
        seen.add(fid)
        if not isinstance(path, str) or not path or len(path) > 1024:
            raise fail("each file needs a non-empty string path")
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS_PER_FILE:
            raise fail("each file needs string text within per-file bound")
        total += len(text)
    if total > MAX_TOTAL_INPUT_CHARS:
        raise fail("total input text exceeds bound")
    return payload


def selected_chars(ids: list[str], files: list[dict]) -> int:
    """Sum of ``len(text)`` over the selected file IDs (chars, not tokens)."""
    by_id = {f["id"]: f["text"] for f in files}
    return sum(len(by_id[i]) for i in ids)


def check_output_ids(ids: Any, payload: dict) -> list[str]:
    """Authoritative host-side output check; rejects forged/over-budget IDs."""
    from core.extensions.errors import CandidateFailure

    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise CandidateFailure("candidate must return a list of file-ID strings")
    if len(set(ids)) != len(ids):
        raise CandidateFailure("candidate returned duplicate file IDs")
    known = {f["id"] for f in payload["files"]}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise CandidateFailure(f"candidate returned unknown file IDs: {unknown}")
    used = selected_chars(ids, payload["files"])
    if used > payload["max_chars"]:
        raise CandidateFailure(f"candidate exceeded char budget: {used} > {payload['max_chars']}")
    return ids
