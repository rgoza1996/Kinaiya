"""Supervisor-side helpers: the runtime authors every Evidence record.

Model output is an untrusted observation. Evidence is built here from
observed tool results (file bytes actually written, subprocess exit codes),
so a model can never supply its own evidence, state, or success claim.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from packages.contracts.models import Evidence

OUTPUT_LIMIT = 4000


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def file_evidence(
    path: Path, content: bytes, objective_revision: int, summary: str
) -> Evidence:
    return Evidence(
        kind="file",
        summary=summary,
        path=str(path),
        sha256=sha256_bytes(content),
        objective_revision=objective_revision,
    )


def read_evidence(
    path: Path, content: bytes, objective_revision: int, summary: str
) -> Evidence:
    return Evidence(
        kind="read",
        summary=summary,
        path=str(path),
        sha256=sha256_bytes(content),
        objective_revision=objective_revision,
    )


def search_evidence(
    scope: Path, output: str, objective_revision: int, summary: str
) -> Evidence:
    return Evidence(
        kind="search",
        summary=summary,
        path=str(scope),
        output=truncate(output),
        objective_revision=objective_revision,
    )


def command_evidence(
    command: str,
    exit_code: int,
    output: str,
    objective_revision: int,
    summary: str,
    network_isolated: bool = True,
) -> Evidence:
    suffix = "" if network_isolated else " [network namespace unavailable in environment]"
    return Evidence(
        kind="command",
        summary=summary + suffix,
        exit_code=exit_code,
        output=truncate(output),
        objective_revision=objective_revision,
    )
