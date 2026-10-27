"""Seed harness: bounded JSON tool-action loop for one model and tool budget.

The harness decides prompt shape and action parsing only. It never touches
the filesystem, the network, permissions, or task state. The runtime
(``services.agent``) enforces every boundary outside model control.
"""

from __future__ import annotations

import json

from packages.contracts.models import ToolAction

SYSTEM_PROMPT = """You are a bounded work agent in a sandbox. You MUST reply with exactly one JSON object per turn, no other text.
Schema: {"tool": "read_file"|"write_file"|"search"|"shell"|"finish", "arguments": {...}}.
Arguments:
- read_file: {"path": "<workspace-relative path>", "max_bytes": <optional int>}
- write_file: {"path": "<workspace-relative path>", "content": "<full text>"}
- search: {"pattern": "<regex>", "path": "<optional workspace-relative dir>"}
- shell: {"command": "<bash -c string>", "timeout_s": <optional int, max 120>}
- finish: {"summary": "<what was done>", "objective_revision": <int you were given>}.
Rules: paths stay inside the workspace; absolute paths are rejected. The finish revision MUST equal your current objective revision or it is rejected as stale. Calling finish is a proposal: it succeeds only if the supervisor recorded evidence for the current revision and your last tool in this revision succeeded. A failed tool can never be reported as success; recover or report failure honestly.
""".strip()

HISTORY_LIMIT = 8
OBSERVATION_LIMIT = 2000


def _short(text: str, limit: int = OBSERVATION_LIMIT) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[truncated {len(text) - limit} chars]"


def build_messages(
    objective: str,
    objective_revision: int,
    source_event_ids: list[str],
    history: list[dict[str, str]],
    grants: str,
) -> list[dict[str, str]]:
    """Build one bounded provider request. Source refs only, no transcript pull."""
    refs = ", ".join(source_event_ids) if source_event_ids else "none"
    user = (
        f"Objective revision {objective_revision}: {objective}\n"
        f"Source refs: {refs}\n"
        f"Grants: {grants}\n"
        "Reply with exactly one JSON ToolAction now."
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    for turn in history[-HISTORY_LIMIT:]:
        messages.append(
            {
                "role": turn["role"],
                "content": _short(turn["content"]),
            }
        )
    return messages


def parse_action(text: str) -> ToolAction:
    """Parse one JSON ToolAction; raises ValueError on any deviation."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("response must be one JSON object")
    return ToolAction.model_validate(data)


def observation_text(ok: bool, output: str, note: str = "") -> str:
    status = "ok" if ok else "error"
    body = _short(output or "")
    suffix = f" note={_short(note, 500)}" if note else ""
    return f"[{status}]{suffix} {body}"
