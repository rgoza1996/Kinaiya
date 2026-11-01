"""Exact positive journal confirmation; absence is never a retry permission."""

import asyncio

from packages.contracts.models import AppendRequest, JournalEntry
from services.agent.auxiliary_routes import encode


def _matches(entry, request, after):
    return (
        type(entry) is JournalEntry
        and entry.origin == "agent-supervisor"
        and type(entry.sequence) is int
        and entry.sequence > after
        and encode(entry.model_dump(mode="json", include=set(AppendRequest.model_fields)))
        == encode(request.model_dump(mode="json"))
    )


async def append_attempt(journal, request, *, after):
    """Dispatch exactly one frozen append; read back its exact kind and body."""
    if (
        type(request) is not AppendRequest
        or request.kind != "agent.model_attempt"
        or len(request.task_refs) != 1
        or type(after) is not int
        or after < 0
    ):
        return None
    try:
        entry = await journal.append(request)
        if _matches(entry, request, after):
            return entry
    except Exception:
        pass
    try:
        async with asyncio.timeout(5):
            cursor = after
            for _ in range(8):
                page = await journal.read(
                    after=cursor,
                    limit=200,
                    session_id=request.session_id,
                    thread_id=request.thread_id,
                    kinds=[request.kind],
                    task_id=request.task_refs[0],
                )
                if type(page) is not list or not 1 <= len(page) <= 200:
                    return None
                previous = cursor
                for entry in page:
                    if (
                        type(entry) is not JournalEntry
                        or type(entry.sequence) is not int
                        or entry.sequence <= previous
                    ):
                        return None
                    previous = entry.sequence
                for entry in page:
                    if _matches(entry, request, after):
                        return entry
                cursor = previous
    except Exception:
        pass
    return None
