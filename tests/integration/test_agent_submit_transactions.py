"""Codex-authored #199 transaction checks using synthetic data and real SQLite.

Promoted from the preserved private transaction probes for repeatable CI. There
are no HTTP calls, model/provider operations, or live configuration accesses.
"""

import asyncio
import sqlite3

import pytest
from test_agent_submit_admission import (
    env as env,
)
from test_agent_submit_admission import lookup, proposal
from test_agent_submit_native_revision import checked_legacy, native_rows, slots, table_rows

from packages.contracts.agent_submit import AgentSubmitLookupRequest, AgentSubmitUnavailable
from packages.contracts.models import AppendRequest, SessionRequest
from services.session.store import Store


class NativeSqlFault:
    """Raise after real INSERTs, at commit, or during rollback, without fake rows."""

    def __init__(self, connection, failure, *, target=None, commit=False, rollback=False):
        self.connection = connection
        self.failure = failure
        self.target = target
        self.fail_commit = commit
        self.fail_rollback = rollback
        self.fired = False
        self.rollback_attempted = False
        self.closed = False
        self.insert_rowcount = None
        self.transaction_at_fault = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        result = self.connection.execute(sql, *args)
        compact = " ".join(sql.lower().split())
        if self.target and compact.startswith("insert into " + self.target + "("):
            self.fired = True
            self.insert_rowcount = result.rowcount
            self.transaction_at_fault = self.connection.in_transaction
            raise self.failure()
        return result

    def commit(self):
        if self.fail_commit:
            self.fired = True
            self.transaction_at_fault = self.connection.in_transaction
            assert len(native_rows(self.connection)) == len(slots(self.connection)) == 1
            raise self.failure()
        return self.connection.commit()

    def rollback(self):
        self.rollback_attempted = True
        if self.fail_rollback:
            raise RuntimeError("Public synthetic rollback failure")
        return self.connection.rollback()

    def close(self):
        self.closed = True
        return self.connection.close()


def query(session, *, key="logical-submit"):
    return AgentSubmitLookupRequest.model_validate(lookup(session, key=key))


def ordinary_converse(store):
    session = store.create_session(SessionRequest(channel="converse"))
    event = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.converse",
            payload={"text": "Public synthetic independent operation.", "final": True},
            idempotency_key="ordinary-converse-control",
        ),
        "converse",
    )
    assert store.entry(event.event_id) == event


@pytest.mark.parametrize("failure", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("target", ["entries", "agent_submit_slots"])
def test_base_exception_after_actual_insert_rolls_back_and_propagates(env, failure, target):
    assert env.store.lookup_submit(query(env.session), "agent").status == "unobserved"
    connection = env.store.db
    before = table_rows(connection)
    proxy = NativeSqlFault(connection, failure, target=target)
    env.store.db = proxy
    try:
        with pytest.raises(failure):
            env.store.admit_submit(proposal(env.session), "agent")
        assert proxy.fired and proxy.insert_rowcount == 1 and proxy.transaction_at_fault
        assert proxy.rollback_attempted and not connection.in_transaction
        assert table_rows(connection) == before
    finally:
        env.store.db = connection
    assert env.store.lookup_submit(query(env.session), "agent").status == "unobserved"
    ordinary_converse(env.store)
    assert native_rows(connection) == slots(connection) == ()


def test_commit_failure_rolls_back_actual_entry_and_slot(env):
    assert env.store.lookup_submit(query(env.session), "agent").status == "unobserved"
    connection = env.store.db
    before = table_rows(connection)
    proxy = NativeSqlFault(connection, RuntimeError, commit=True)
    env.store.db = proxy
    try:
        with pytest.raises(AgentSubmitUnavailable):
            env.store.admit_submit(proposal(env.session), "agent")
        assert proxy.fired and proxy.transaction_at_fault and proxy.rollback_attempted
        assert not connection.in_transaction and table_rows(connection) == before
    finally:
        env.store.db = connection
    assert env.store.lookup_submit(query(env.session), "agent").status == "unobserved"
    ordinary_converse(env.store)
    assert native_rows(connection) == slots(connection) == ()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_rollback_failure_closes_unsafe_connection_and_poisoned_native_refuses(env, failure):
    assert env.store.lookup_submit(query(env.session), "agent").status == "unobserved"
    connection = env.store.db
    proxy = NativeSqlFault(connection, failure, target="entries", rollback=True)
    env.store.db = proxy
    try:
        expected = AgentSubmitUnavailable if failure is RuntimeError else failure
        with pytest.raises(expected):
            env.store.admit_submit(proposal(env.session), "agent")
        assert proxy.fired and proxy.insert_rowcount == 1 and proxy.transaction_at_fault
        assert proxy.rollback_attempted and proxy.closed
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with pytest.raises(AgentSubmitUnavailable):
            env.store.lookup_submit(query(env.session), "agent")
        with sqlite3.connect(env.path) as independent:
            assert native_rows(independent) == slots(independent) == ()
    finally:
        env.store.db = connection


@pytest.mark.parametrize("failure", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_interrupted_upgrade_rolls_back_prefix_and_stays_unavailable(env, failure):
    path = env.tmp_path / "interrupted-upgrade.sqlite"
    checked_legacy(path, env.session, [proposal(env.session)])
    store = Store(path)
    connection = store.db
    before = native_rows(connection)
    proxy = NativeSqlFault(connection, failure, target="agent_submit_slots")
    store.db = proxy
    try:
        with pytest.raises(failure):
            store.lookup_submit(query(env.session), "agent")
        assert proxy.fired and proxy.insert_rowcount == 1 and proxy.transaction_at_fault
        assert proxy.rollback_attempted and not connection.in_transaction
        assert native_rows(connection) == before and slots(connection) == ()
        store.db = connection
        with pytest.raises(AgentSubmitUnavailable):
            store.lookup_submit(query(env.session), "agent")
        ordinary_converse(store)
        with sqlite3.connect(path) as independent:
            assert native_rows(independent) == before and slots(independent) == ()
    finally:
        store.db = connection
        store.close()


@pytest.mark.parametrize("duplicate", [False, True], ids=["201_valid", "202_ambiguous"])
def test_upgrade_preserves_paged_history_and_rejects_late_ambiguity(env, duplicate):
    bodies = [
        proposal(
            env.session, task_id="public-synthetic-task-" + str(index), key="key-" + str(index)
        )
        for index in range(201)
    ]
    if duplicate:
        bodies.append(proposal(env.session, task_id="duplicate-task", key="key-0"))
    path = env.tmp_path / "paged-legacy.sqlite"
    checked_legacy(path, env.session, bodies)
    store = Store(path)
    try:
        before = native_rows(store.db)
        if duplicate:
            with pytest.raises(AgentSubmitUnavailable):
                store.lookup_submit(query(env.session, key="key-200"), "agent")
            assert slots(store.db) == ()
            with pytest.raises(AgentSubmitUnavailable):
                store.lookup_submit(query(env.session, key="never-recorded"), "agent")
        else:
            result = store.lookup_submit(query(env.session, key="key-200"), "agent")
            assert result.status == "recorded"
            assert result.entry.payload["task_id"] == bodies[-1].payload["task_id"]
            assert len(slots(store.db)) == len(bodies)
        assert not store.db.in_transaction and native_rows(store.db) == before
        ordinary_converse(store)
        assert native_rows(store.db) == before
    finally:
        store.close()


def test_valid_ordinary_non_submit_command_does_not_disable_admission(env):
    request = AppendRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        kind="agent.command",
        payload={"op": "steer", "text": "Public synthetic ordinary command."},
        idempotency_key="ordinary-command",
    )
    ordinary = env.store.append(request, "agent")
    assert env.store.lookup_submit(query(env.session), "agent").status == "unobserved"
    assert env.store.admit_submit(proposal(env.session), "agent").disposition == "created"
    assert env.store.entry(ordinary.event_id) == ordinary
