"""Independent no-cascade invariant for the protected standing claim ledger."""

from datetime import UTC, datetime

from test_standing_execution_owner import (
    build_native as build_native,
)
from test_standing_execution_owner import (
    claim_rows,
    database_state,
)
from test_standing_execution_owner import (
    native as native,
)

from packages.contracts.standing_execution import StandingOccurrence
from packages.contracts.standing_work import StandingUnavailable


def _grant_with_session_identity(native):
    """Make the real authorization identity a real Session FK target."""
    native.wall.value = datetime(2026, 11, 2, 11, 59, 50, tzinfo=UTC)
    record = native.save(
        responsibility=native.session.session_id,
        label="Synthetic responsibility with a genuine Session UUID",
    )
    native.wall.value = datetime(2026, 11, 2, 12, 0, 10, tzinfo=UTC)
    due = StandingOccurrence(
        schema_version="1.0",
        local_date="2026-11-02",
        due_utc="2026-11-02T12:00:00Z",
        utc_offset_seconds=0,
    )
    request = native.request(key="claim-schema:session-fk", record=record, due=due)
    assert request.responsibility_id == native.session.session_id
    assert request.authorization_revision == 1
    assert record.activation == "armed"
    return record, request


def _unique_columns(connection):
    result = set()
    for index in connection.execute("PRAGMA index_list(standing_execution_claims)"):
        if index[2]:
            name = '"' + index[1].replace('"', '""') + '"'
            result.add(tuple(row[2] for row in connection.execute(f"PRAGMA index_info({name})")))
    return result


def _install_only_cascading_fk(native):
    """Preserve actual rows, column declarations and unique index shapes."""
    connection = native.store.db
    before = database_state(connection)
    columns = [
        tuple(row) for row in connection.execute("PRAGMA table_info(standing_execution_claims)")
    ]
    unique = _unique_columns(connection)
    assert len(claim_rows(connection)) == 1
    assert list(connection.execute("PRAGMA foreign_key_list(standing_execution_claims)")) == []
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with connection:
        connection.execute(
            "ALTER TABLE standing_execution_claims RENAME TO original_non_cascading_claims"
        )
        connection.execute(
            "CREATE TABLE standing_execution_claims("
            "key TEXT PRIMARY KEY,claim_id TEXT NOT NULL UNIQUE,"
            "occurrence_id TEXT NOT NULL UNIQUE,responsibility_id TEXT NOT NULL,"
            "authorization_revision INTEGER NOT NULL,claim TEXT NOT NULL,"
            "FOREIGN KEY(responsibility_id) REFERENCES sessions(session_id) ON DELETE CASCADE)"
        )
        connection.execute(
            "INSERT INTO standing_execution_claims SELECT * FROM original_non_cascading_claims"
        )
        connection.execute("DROP TABLE original_non_cascading_claims")
    assert [
        tuple(row) for row in connection.execute("PRAGMA table_info(standing_execution_claims)")
    ] == columns
    assert _unique_columns(connection) == unique
    after = database_state(connection)
    assert after[1] == before[1], "Schema fixture must retain every actual original row"
    foreign = list(connection.execute("PRAGMA foreign_key_list(standing_execution_claims)"))
    assert len(foreign) == 1
    assert tuple(foreign[0])[2:5] == ("sessions", "responsibility_id", "session_id")
    assert foreign[0][6] == "CASCADE"
    assert list(connection.execute("PRAGMA foreign_key_check")) == []
    assert not connection.in_transaction


def _all_unavailable_without_mutation(native, operations):
    before = database_state(native.store.db)
    observed = {}
    for name, invoke in operations:
        try:
            result = invoke()
        except Exception as error:
            observed[name] = {"error": type(error).__name__}
            assert database_state(native.store.db) == before
            assert not native.store.db.in_transaction
            observed[name]["unavailable"] = isinstance(error, StandingUnavailable)
        else:
            observed[name] = {"result": type(result).__name__, "unavailable": False}
            assert database_state(native.store.db) == before
            assert not native.store.db.in_transaction
    assert all(result["unavailable"] for result in observed.values()), observed


def test_unsupported_fk_refuses_every_ledger_read_replay_and_fresh_registration(native):
    _, request = _grant_with_session_identity(native)
    replay = native.ledger()
    proof = replay.prepare(request).validate_request(request).claim
    lookup, unresolved = native.ledger(), native.ledger()
    fresh = native.ledger(register=False)
    assert lookup.lookup(request) == unresolved.unresolved() == proof
    _install_only_cascading_fk(native)
    native.wall.forbidden = native.mono.forbidden = True
    _all_unavailable_without_mutation(
        native,
        (
            ("lookup", lambda: lookup.lookup(request)),
            ("unresolved", unresolved.unresolved),
            ("retained_prepare", lambda: replay.prepare(request)),
            ("fresh_register", fresh.register),
        ),
    )


def test_actual_session_cascade_cannot_turn_an_unresolved_claim_into_absence(native):
    record, request = _grant_with_session_identity(native)
    prepared = native.ledger()
    proof = prepared.prepare(request).validate_request(request).claim
    lookup, unresolved = native.ledger(), native.ledger()
    _install_only_cascading_fk(native)
    assert len(claim_rows(native.store.db)) == 1
    grant_before = [
        tuple(row)
        for row in native.store.db.execute(
            "SELECT * FROM standing_authorizations ORDER BY responsibility_id,revision"
        )
    ]
    native.store.delete(native.session.session_id)
    assert (
        native.store.db.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (native.session.session_id,)
        ).fetchone()
        is None
    )
    assert claim_rows(native.store.db) == [], "The unsupported FK actually erased the retained row"
    assert [
        tuple(row)
        for row in native.store.db.execute(
            "SELECT * FROM standing_authorizations ORDER BY responsibility_id,revision"
        )
    ] == grant_before
    assert native.owner.snapshot(request.responsibility_id).record == record
    assert proof.request.responsibility_id == native.session.session_id
    native.wall.forbidden = native.mono.forbidden = True
    _all_unavailable_without_mutation(
        native,
        (
            ("lookup_after_actual_cascade", lambda: lookup.lookup(request)),
            ("unresolved_after_actual_cascade", unresolved.unresolved),
        ),
    )
