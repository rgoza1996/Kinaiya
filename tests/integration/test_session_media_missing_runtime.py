"""Source-informed recovery checks for a missing optional media runtime.

An incomplete native metadata namespace cannot prove that original custody is
settled. These deliberately partial SQLite fixtures must only cause refusal;
they do not stand in for a registered media owner or a cleanup receipt.
"""

import pytest

from packages.contracts.models import AppendRequest, SessionRequest
from services.session.store import JournalError, Store


def _append(store, session, key):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={"text": "Synthetic recovery fixture", "final": True, "revision": 1},
            idempotency_key=key,
        ),
        "owner",
    )


@pytest.mark.parametrize("operation", ["reset", "delete"])
@pytest.mark.parametrize(
    "table",
    [
        "media_operations",
        "media_reservations",
        "media_dependencies",
        "native_media_v2_attempts",
        "native_media_v2_records",
    ],
)
@pytest.mark.parametrize("retained", [False, True], ids=["empty-partial-schema", "unknown-owner"])
def test_partial_media_namespace_requires_original_owner(tmp_path, operation, table, retained):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest())
        original = _append(store, session, "before-refusal")
        # All identifiers come from this fixed fixture parameter set. No table
        # shape or absent row is allowed to fabricate positive native cleanup.
        with store.db:
            store.db.execute(f"CREATE TABLE {table} (fixture_state TEXT NOT NULL)")
            if retained:
                store.db.execute(f"INSERT INTO {table} VALUES (?)", ("unknown",))
        before = store.session(session.session_id).model_dump(mode="json")

        with pytest.raises(JournalError, match="original native media owner is unavailable"):
            getattr(store, operation)(session.session_id)

        assert store.session(session.session_id).model_dump(mode="json") == before
        assert store.entry(original.event_id) == original
        assert [row[0] for row in store.db.execute(f"SELECT fixture_state FROM {table}")] == (
            ["unknown"] if retained else []
        )
        # Only epoch reset/deletion is unavailable. Ordinary authenticated
        # transcript work still uses the same original live Store connection.
        subsequent = _append(store, session, "after-refusal")
        assert subsequent.sequence > original.sequence
        assert store.entry(subsequent.event_id) == subsequent
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_adjacent_nonmedia_table_keeps_ordinary_session_behavior(tmp_path, operation):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest())
        original = _append(store, session, "ordinary")
        with store.db:
            store.db.execute("CREATE TABLE mediaXreservations (fixture_state TEXT NOT NULL)")
            store.db.execute("INSERT INTO mediaXreservations VALUES ('ordinary')")

        if operation == "reset":
            reset = store.reset(session.session_id)
            assert reset.epoch == session.epoch + 1
            assert store.entry(original.event_id) == original
        else:
            store.delete(session.session_id)
            with pytest.raises(KeyError):
                store.session(session.session_id)
            with pytest.raises(KeyError):
                store.entry(original.event_id)
        assert store.db.execute("SELECT fixture_state FROM mediaXreservations").fetchone()[0] == (
            "ordinary"
        )
    finally:
        store.close()
