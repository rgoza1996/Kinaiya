"""Preserve native audit boundaries and private lookup-return isolation.

Authored from requirement-first expectations and baseline04553bd source/fixtures.
The prospective single-audit memo body was not read. These cases should pass
both the original and optimized source; they assert no cache shape or counters.
The existing fixture owns real temporary Auth, ASGI, SQLite and spool custody.
Deliberate corruption preserves the fixture's existing unknown-shutdown rules;
successful SQL rollback does not establish physical source cleanup.
"""

import pytest
from test_session_media_owner import _json, _stack

from packages.contracts import media
from services.session.media_clients import MediaSessionError

_TABLES = (
    "entries",
    "media_meta",
    "media_owner_auth",
    "media_operations",
    "media_receipts",
    "media_catalog",
    "media_clients",
    "media_reservations",
    "media_sources",
    "media_dependencies",
    "media_uploads",
    "media_objective_anchors",
    "media_equivalences",
)


def _rows(env):
    with env.store.lock:
        return {
            table: tuple(
                tuple(row) for row in env.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            )
            for table in _TABLES
        }


def _offsets(env):
    return {source: upload.next_offset for source, upload in env.uploads.items()}


async def test_post_audit_rejects_yield_corruption_and_rolls_back_without_allocation(tmp_path):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        source, _ = await env.admit()
        before = _rows(env)
        reservations = tuple(env.reserve_calls)
        reserved_bytes = env.spool.reserved_bytes
        offsets = _offsets(env)
        yielded = False

        with pytest.raises(MediaSessionError) as rejected:
            with env.owner._boundary():
                # Entering the real boundary has completed its fresh pre-audit.
                yielded = True
                row = env.store.db.execute(
                    "SELECT canonical_receipt FROM media_receipts WHERE receipt_id=?",
                    (source["id"],),
                ).fetchone()
                assert row is not None
                corrupted = bytes(row[0]) + b" "
                changed = env.store.db.execute(
                    "UPDATE media_receipts SET canonical_receipt=? WHERE receipt_id=?",
                    (corrupted, source["id"]),
                )
                assert changed.rowcount == 1
                assert (
                    bytes(
                        env.store.db.execute(
                            "SELECT canonical_receipt FROM media_receipts WHERE receipt_id=?",
                            (source["id"],),
                        ).fetchone()[0]
                    )
                    == corrupted
                )
                # No manual audit, commit or rollback: the original boundary
                # must reject this noncanonical persisted content on its exit.

        assert yielded is True
        assert rejected.value.code == "unavailable"
        assert env.owner._poisoned is True
        assert env.store.db.in_transaction is False
        assert _rows(env) == before
        assert tuple(env.reserve_calls) == reservations
        assert env.spool.reserved_bytes == reserved_bytes
        assert _offsets(env) == offsets


async def test_native_lookup_isolates_mutable_children_and_rechecks_current_row_and_ref(tmp_path):
    async with _stack(tmp_path) as env:
        source, _ = await env.admit()
        status = await env.current_status(source)
        assert status["body"]["children"]
        expected = _json(status)
        reference = media.media_json(_json(env.ref(status)), media.MediaNativeRef)
        before = _rows(env)
        reservations = tuple(env.reserve_calls)
        reserved_bytes = env.spool.reserved_bytes
        offsets = _offsets(env)

        with env.owner._boundary() as token:
            first = env.owner._lookup_ref(token, reference, "status")
            assert media.media_bytes(first) == expected
            assert first.body.children
            first.body.children.clear()
            assert first.body.children == []

            second = env.owner._lookup_ref(token, reference, "status")
            assert media.media_bytes(second) == expected

            bad_backend = media.media_json(
                _json(
                    {
                        **env.ref(status),
                        "backend_id": "0" * 32 if reference.backend_id != "0" * 32 else "1" * 32,
                    }
                ),
                media.MediaNativeRef,
            )
            bad_hash = media.media_json(
                _json(
                    {
                        **env.ref(status),
                        "sha256": "0" * 64 if status["sha256"] != "0" * 64 else "1" * 64,
                    }
                ),
                media.MediaNativeRef,
            )
            for candidate, kind in (
                (bad_backend, "status"),
                (bad_hash, "status"),
                (reference, "source"),
            ):
                with pytest.raises(MediaSessionError) as rejected:
                    env.owner._lookup_ref(token, candidate, kind)
                assert rejected.value.code == "unknown_ref"

            malformed = media.media_json(_json(env.ref(status)), media.MediaNativeRef)
            malformed.__dict__["receipt_id"] = True
            with pytest.raises(media.MediaContractError) as rejected:
                env.owner._lookup_ref(token, malformed, "status")
            assert rejected.value.code == "bad_packet"

            # The same native ref must require its actual current SQL row even
            # after successful resolution. Restore it before the post-audit.
            env.store.db.execute("SAVEPOINT audit_scope_row")
            try:
                removed = env.store.db.execute(
                    "DELETE FROM media_receipts WHERE receipt_id=?", (status["id"],)
                )
                assert removed.rowcount == 1
                with pytest.raises(MediaSessionError) as rejected:
                    env.owner._lookup_ref(token, reference, "status")
                assert rejected.value.code == "unknown_ref"
            finally:
                env.store.db.execute("ROLLBACK TO audit_scope_row")
                env.store.db.execute("RELEASE audit_scope_row")

            restored = env.owner._lookup_ref(token, reference, "status")
            assert media.media_bytes(restored) == expected

        assert env.owner._poisoned is False
        assert env.store.db.in_transaction is False
        assert _rows(env) == before
        assert tuple(env.reserve_calls) == reservations
        assert env.spool.reserved_bytes == reserved_bytes
        assert _offsets(env) == offsets
