"""Positive native confirmation for one frozen final outcome append.

Read-back never proves absence: a timed-out POST can still commit later.
The caller owns task state and keeps its finalization fence on uncertainty.
"""

import asyncio
import json

from packages.contracts.interfaces import Journal
from packages.contracts.models import AppendRequest, JournalEntry

READBACK_TIMEOUT_S = 5.0
READBACK_MAX_PAGES = 8
READBACK_PAGE_LIMIT = 200


def _matches(entry: JournalEntry, request: AppendRequest, after: int) -> bool:
    return (
        isinstance(entry, JournalEntry)
        and entry.origin == "agent-supervisor"
        and type(entry.sequence) is int
        and entry.sequence > after
        and json.dumps(
            entry.model_dump(mode="json", include=set(AppendRequest.model_fields)),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        == json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    )


async def _read_back(journal: Journal, request: AppendRequest, after: int) -> bool:
    cursor = after
    for _ in range(READBACK_MAX_PAGES):
        page = await journal.read(
            after=cursor,
            limit=READBACK_PAGE_LIMIT,
            session_id=request.session_id,
            thread_id=request.thread_id,
            kinds=["agent.outcome"],
            task_id=request.task_refs[0],
        )
        if not isinstance(page, list) or not 1 <= len(page) <= READBACK_PAGE_LIMIT:
            return False
        # Validate the entire page before accepting a match; malformed trailing
        # rows or a non-advancing cursor never turn a broken response into proof.
        previous = cursor
        for entry in page:
            if (
                not isinstance(entry, JournalEntry)
                or type(entry.sequence) is not int
                or entry.sequence <= previous
            ):
                return False
            previous = entry.sequence
        if any(_matches(entry, request, after) for entry in page):
            return True
        cursor = previous
    return False


async def append_completion(journal: Journal, request: AppendRequest, *, after: int) -> bool:
    """Append once, then accept only its exact acknowledged/read-back receipt.

    False means unconfirmed, never permission to append another outcome. Only
    ordinary exceptions are sanitized; cancellation propagates to the caller's
    conservative shutdown path. All remote operations use the Journal adapter's
    existing bounded transport; read-back has an additional total deadline.
    """
    if (
        not isinstance(request, AppendRequest)
        or request.kind != "agent.outcome"
        or len(request.task_refs) != 1
        or type(after) is not int
        or after < 0
    ):
        return False
    try:
        entry = await journal.append(request)
        if _matches(entry, request, after):
            return True
    except Exception:
        pass
    try:
        async with asyncio.timeout(READBACK_TIMEOUT_S):
            return await _read_back(journal, request, after)
    except Exception:
        return False
