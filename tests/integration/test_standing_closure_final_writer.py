"""Independent first-close writer recheck at the actual final SQLite read.

Only the known pre-fix native query boundary is gated. The real control.close
retires RAM before waiting on the same actual Store RLock; no fake authority,
COMMIT, closure proof, clock or cancellation callback supplies that event.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_standing_admission_closure_native import (
    assert_fence,
    feature,
    rows,
)
from test_standing_task_admission import (
    SECRET,
    assert_created,
    database_state,
    links,
    standing_rows,
)
from test_standing_task_admission import build_native as build_native
from test_standing_task_admission import native as native
from test_standing_task_admission import prepared as prepared

from packages.contracts.standing_work import StandingConflict, StandingUnavailable

ROWS = "standing_admission_closures"


class ActualLock:
    """Delegate to the actual RLock and observe the real retirement contender."""

    def __init__(self, lock, control):
        self.lock, self.control = lock, control
        self.retirement_thread = None
        self.retirement_entered = threading.Event()
        self.retired_before_lock = False

    def __getattr__(self, name):
        return getattr(self.lock, name)

    def __enter__(self):
        if threading.get_ident() == self.retirement_thread:
            self.retired_before_lock = self.control.instance is None
            self.retirement_entered.set()
        return self.lock.__enter__()

    def __exit__(self, *arguments):
        return self.lock.__exit__(*arguments)


class FinalClosureRead:
    """Hold the final real receipt SELECT after DML, before its fetch returns.

    The qualified native core validates the inserted receipt once, reads it
    again inside its complete final observation, and then performs its final
    receipt observation. The third post-INSERT SELECT is the fixed race seam;
    its actual row and active SQLite transaction must both be observed.
    """

    def __init__(self, connection):
        self.connection = connection
        self.inserted = self.fired = False
        self.post_insert_reads = 0
        self.entered, self.release = threading.Event(), threading.Event()
        self.at_gate = None

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        cursor = self.connection.execute(statement, parameters)
        normalized = " ".join(statement.upper().split())
        if normalized.startswith(f"INSERT INTO {ROWS.upper()}"):
            self.inserted = True
        if self.inserted and normalized == f"SELECT CLAIM_ID,CLOSURE FROM {ROWS.upper()}":
            self.post_insert_reads += 1
            if self.post_insert_reads == 3:
                self.fired = True
                self.at_gate = database_state(self.connection)
                assert self.connection.in_transaction
                assert len(self.at_gate[1][ROWS]) == 1
                self.entered.set()
                assert self.release.wait(8), "Final native SQL gate was not released"
        return cursor


@pytest.mark.parametrize(
    ("accepted", "retire"),
    [(False, True), (True, True), (False, False)],
    ids=["empty-retired-writer", "accepted-retired-writer", "timely-current-writer"],
)
def test_first_closure_rechecks_actual_ram_writer_after_final_native_sql(
    prepared, accepted, retire
):
    value = prepared.native
    owner = feature(prepared.task_owner())
    initial = (
        assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
        if accepted
        else None
    )
    connection, lock = value.store.db, value.store.lock
    before = database_state(connection)
    before_entries, before_links = standing_rows(connection), links(connection)
    proxy = FinalClosureRead(connection)
    observed = ActualLock(lock, value.control)
    value.store.db, value.store.lock = proxy, observed
    retirement = None

    def close_admission():
        try:
            return owner.close_admission(prepared.request, capability=SECRET)
        except (StandingConflict, StandingUnavailable) as error:
            return error

    def retire_control():
        observed.retirement_thread = threading.get_ident()
        return value.control.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            closing = pool.submit(close_admission)
            try:
                assert proxy.entered.wait(8) and proxy.fired
                if retire:
                    retirement = pool.submit(retire_control)
                    assert observed.retirement_entered.wait(3)
                    assert observed.retired_before_lock and value.control.instance is None
                    assert not retirement.done()
            finally:
                proxy.release.set()
            result = closing.result(timeout=10)
            if retirement is not None:
                retirement.result(timeout=10)
        assert proxy.post_insert_reads == 3
    finally:
        proxy.release.set()
        value.store.db, value.store.lock = connection, lock

    assert proxy.inserted and proxy.fired and proxy.at_gate is not None
    assert not connection.in_transaction
    assert database_state(connection)[0] == before[0]
    assert owner.lookup(prepared.request) == initial
    assert prepared.claims.unresolved() == prepared.claim
    assert standing_rows(connection) == before_entries
    assert links(connection) == before_links
    assert len(standing_rows(connection)) == len(links(connection)) == int(accepted)
    if retire:
        assert value.control.instance is None
        stored = connection.execute(
            "SELECT retired FROM agent_control_generations WHERE instance_id=?",
            (prepared.request.expected_instance,),
        ).fetchone()
        assert tuple(stored) == (1,)
        assert isinstance(result, (StandingConflict, StandingUnavailable)), (
            f"Retired writer returned {type(result).__name__}; "
            f"native closure count={len(rows(connection))}"
        )
        assert rows(connection) == []
        assert owner.lookup_closure(prepared.request) is None
        changed_by_retirement = {"agent_control_state", "agent_control_generations"}
        assert {
            name: contents
            for name, contents in database_state(connection)[1].items()
            if name not in changed_by_retirement
        } == {
            name: contents
            for name, contents in before[1].items()
            if name not in changed_by_retirement
        }
    else:
        assert value.control.instance == prepared.request.expected_instance
        assert_fence(prepared, owner, result, initial)
        assert result.initial is None
