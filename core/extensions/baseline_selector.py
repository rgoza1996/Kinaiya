"""Pinned baseline context selector (executable, seeded into every Registry).

Keyword-overlap ranking with greedy budget fill. Deterministic: ties break
by file ID. Stdlib only (``re``). This is a deliberately simple reference
point, not a claim of quality; candidates must beat or match it under the
predeclared policy to be promotable.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def select_context(payload: dict) -> list[str]:
    """Return file IDs with the most query-word overlap within max_chars."""
    query_words = _tokens(str(payload.get("query", "")))
    files = payload.get("files", [])
    budget = int(payload.get("max_chars", 0))
    scored: list[tuple[int, str, int]] = []
    for entry in files:
        fid = str(entry.get("id", ""))
        text = str(entry.get("text", ""))
        overlap = len(query_words & _tokens(text)) if query_words else 0
        scored.append((-overlap, fid, len(text)))
    scored.sort()
    chosen: list[str] = []
    used = 0
    for _neg, fid, size in scored:
        if not fid or fid in chosen:
            continue
        if used + size <= budget:
            chosen.append(fid)
            used += size
    return chosen
