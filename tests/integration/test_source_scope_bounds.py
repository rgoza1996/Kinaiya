"""Bounded source-floor parity with existing native history revision limits."""

import pytest

from packages.contracts.models import AppendRequest, SessionRequest
from packages.providers.disclosure import source_scope
from services.converse.history import MAX_INTEGER, fold_users
from services.session.store import LocalJournal, Store


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["oversize-base", "oversize-next"])
async def test_unprocessable_native_revision_cannot_authorize_source_export(tmp_path, variant):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    owner = LocalJournal(store, "owner")
    journal = LocalJournal(store, "converse")

    async def write(kind, payload, key):
        return await owner.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind=kind,
                payload=payload,
                idempotency_key=key,
            )
        )

    try:
        original = await write(
            "conversation.user",
            {
                "text": "Public synthetic source.",
                "revision": MAX_INTEGER + 1 if variant == "oversize-base" else MAX_INTEGER,
                "final": True,
                "content_scope": "public_synthetic",
            },
            "base",
        )
        if variant == "oversize-next":
            await write(
                "conversation.revision",
                {
                    "target_event_id": original.event_id,
                    "text": "Public synthetic correction.",
                    "revision": MAX_INTEGER + 1,
                    "content_scope": "public_synthetic",
                },
                "revision",
            )
        actual = await source_scope(journal, original.event_id)
        folded = fold_users(store.read(limit=100), lambda _entry: True)[original.event_id]
        assert not folded.permitted
        assert actual == "local_only", (
            "Shared source_scope authorizes a chain the native history validator declares unprocessable"
        )
    finally:
        store.close()
