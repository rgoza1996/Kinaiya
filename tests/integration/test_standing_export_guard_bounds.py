"""Independent cursor-boundary oracles for standing ordinary-export guards."""

import sqlite3
from contextlib import closing, contextmanager

import pytest
from test_agent_submit_admission import proposal as ordinary_proposal
from test_standing_execution_owner import database_state, uid
from test_standing_export_admission import accept, old_export, safe
from test_standing_export_admission import accepted as accepted
from test_standing_export_admission import build_native as build_native
from test_standing_export_admission import native as native
from test_standing_export_admission import prepared as prepared
from test_standing_export_guard_corruption import missing_initial

from packages.contracts import models
from packages.contracts.agent_export import AgentExportAdmission
from packages.contracts.models import SessionRequest
from services.session.store import JournalError

MIB = 1024 * 1024
MAX_BOUNDED_CELL = 163840  # Largest permitted retained initial-proof cell.


class ObservedCursor:
    def __init__(self, cursor, witness):
        self.cursor, self.witness = cursor, witness

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def execute(self, *args, **kwargs):
        self.cursor.execute(*args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        self.cursor.executemany(*args, **kwargs)
        return self

    def fetchone(self):
        return self.witness.observe(self.cursor.fetchone())

    def fetchmany(self, *args, **kwargs):
        return [self.witness.observe(row) for row in self.cursor.fetchmany(*args, **kwargs)]

    def fetchall(self):
        return [self.witness.observe(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return self

    def __next__(self):
        return self.witness.observe(next(self.cursor))


class FetchWitness:
    """Forward actual DBAPI results unchanged and record bytes before return.

    No statement text or expected SQL projection is inspected. Recording is
    separate from facade errors, so a caught exception cannot hide a fetch.
    """

    def __init__(self, connection):
        self.connection = connection
        self.rows = 0
        self.oversized = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def observe(self, row):
        if row is not None:
            self.rows += 1
            for value in row:
                size = (
                    len(value.encode("utf-8"))
                    if type(value) is str
                    else (len(value) if type(value) is bytes else 0)
                )
                if size > MAX_BOUNDED_CELL:
                    self.oversized.append((type(value).__name__, size))
        return row

    def cursor(self, *args, **kwargs):
        return ObservedCursor(self.connection.cursor(*args, **kwargs), self)

    def execute(self, *args, **kwargs):
        return ObservedCursor(self.connection.execute(*args, **kwargs), self)

    def executemany(self, *args, **kwargs):
        return ObservedCursor(self.connection.executemany(*args, **kwargs), self)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)


@contextmanager
def observed_fetches(store):
    connection = store.db
    assert isinstance(connection, sqlite3.Connection)
    witness = FetchWitness(connection)
    store.db = witness
    try:
        yield witness
    finally:
        store.db = connection


@pytest.mark.parametrize("column", ("claim_id", "event_id", "sequence"))
@pytest.mark.parametrize("storage_class", ("text", "blob"))
def test_corrupt_link_metadata_is_refused_without_materializing_unbounded_cells(
    accepted, column, storage_class
):
    store = accepted.native.store
    connection = store.db
    missing_initial(accepted)
    corrupt = "x" * MIB if storage_class == "text" else b"x" * MIB
    with connection:
        connection.execute(f"UPDATE standing_task_admissions SET {column}=?", (corrupt,))
    assert (
        connection.execute(f"SELECT typeof({column}) FROM standing_task_admissions").fetchone()[0]
        == storage_class
    )
    ordinary = old_export(accepted.proof, content_scope="public_synthetic")
    before, changes = database_state(connection), connection.total_changes
    with observed_fetches(store) as witness:
        with pytest.raises(JournalError) as refused:
            store.agent_export_admission(ordinary)
        safe(refused.value)
        assert witness.rows > 0
        assert witness.oversized == []
    assert database_state(connection) == before
    assert connection.total_changes == changes
    assert not connection.in_transaction


def test_unrelated_ordinary_task_exports_beside_genuine_max_utf8_native_event(
    prepared, monkeypatch
):
    event_id = "\U0001f9ea" * 128
    assert len(event_id) == 128 and len(event_id.encode("utf-8")) == 512
    # Only the trusted native metadata allocator is controlled. The real
    # TaskOwner still appends, validates and commits the actual initial/proof.
    with monkeypatch.context() as allocator:
        allocator.setattr(models, "uuid4", lambda: event_id)
        value = accept(prepared)
    store = value.native.store
    assert value.proof.event_id == event_id
    assert value.owner.lookup(value.prepared.request) == value.proof
    assert store.entry(event_id) == value.proof.journal_entry()
    session = store.create_session(SessionRequest(channel="agent"))
    task_id = uid(9300)
    ordinary_task = store.admit_submit(ordinary_proposal(session, task_id=task_id), "agent")
    assert ordinary_task.entry.payload["task_id"] == task_id
    ordinary = old_export(
        value.proof,
        task_id=task_id,
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
    )
    connection = store.db
    before, changes = database_state(connection), connection.total_changes
    with observed_fetches(store) as witness:
        result = store.agent_export_admission(ordinary)
        assert type(result) is AgentExportAdmission
        assert result.validate_request(ordinary) == result
        assert witness.rows > 0 and witness.oversized == []
    assert database_state(connection) == before
    assert connection.total_changes == changes
    assert not connection.in_transaction


def test_fetch_witness_detects_actual_oversized_dbapi_result_before_return():
    with closing(sqlite3.connect(":memory:")) as connection:
        witness = FetchWitness(connection)
        result = witness.execute("SELECT ?", (b"x" * MIB,)).fetchone()
        assert len(result[0]) == MIB
        assert witness.rows == 1
        assert witness.oversized == [("bytes", MIB)]
