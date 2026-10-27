"""Public SDK for authoring context_selector.v1 extensions.

Candidate authors (including the model) use these helpers to build and
self-check selectors *outside* the sandbox. Execution, scoring, and
promotion stay on the trusted side (``core.extensions``).
"""

from __future__ import annotations


def make_file(file_id: str, path: str, text: str) -> dict:
    """Build one ``files`` entry of a selector payload."""
    return {"id": file_id, "path": path, "text": text}


def make_payload(query: str, files: list[dict], max_chars: int) -> dict:
    """Build a ``select_context`` payload dict."""
    return {"query": query, "files": files, "max_chars": max_chars}


def selected_chars(ids: list[str], files: list[dict]) -> int:
    """Char budget used by ``ids``: sum of ``len(text)`` (chars, not tokens)."""
    by_id = {f["id"]: f["text"] for f in files}
    return sum(len(by_id[i]) for i in ids)


def fits_budget(ids: list[str], files: list[dict], max_chars: int) -> bool:
    """True when the selection fits the declared char budget."""
    return selected_chars(ids, files) <= max_chars


def validate_output(ids: object, payload: dict) -> list[str]:
    """Author-side mirror of the host check; raises ValueError on violation."""
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("output must be a list of file-ID strings")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate file IDs")
    known = {f["id"] for f in payload["files"]}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"unknown file IDs: {unknown}")
    used = selected_chars(ids, payload["files"])
    if used > payload["max_chars"]:
        raise ValueError(f"over budget: {used} chars > {payload['max_chars']}")
    return ids


__all__ = [
    "fits_budget",
    "make_file",
    "make_payload",
    "selected_chars",
    "validate_output",
]
