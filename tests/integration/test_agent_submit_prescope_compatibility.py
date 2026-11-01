"""Authentic pre-scope native rows from runtime 99e8c438, using synthetic data.

The historical runtime emitted neither scope field nor a model pin. These
fixtures preserve its complete 16-field payload and both native JSON cells;
they never send an old submit through the prohibited generic append endpoint.
"""

import copy
import hashlib
import json
import sqlite3

import pytest

from core.policy import ToolPolicy
from packages.commands import Registry
from packages.contracts.agent_submit import (
    AgentSubmitLookupRequest,
    AgentSubmitLookupResult,
    AgentSubmitReceipt,
    AgentSubmitUnavailable,
    submit_entry,
    submit_request,
)
from packages.contracts.models import (
    AppendRequest,
    CommandRequest,
    JournalEntry,
    Session,
    TaskRequest,
)
from services.agent import AgentRuntime
from services.session.store import LocalJournal, Store


def digest(logical):
    return hashlib.sha256(
        json.dumps(logical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def historical_submit(
    session, *, task_id="historical", key="historical-key", objective="Read public.txt"
):
    """Exact _submit plus _append payload shape at 99e8c43878d89eeb5ea47e02e9ce47df9e7e87a6."""
    logical = {
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "epoch": session.epoch,
        "objective": objective,
        "source_event_ids": [],
        "source_revision": 1,
    }
    return AppendRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        kind="agent.command",
        task_refs=[task_id],
        idempotency_key=f"agent:{task_id}:agent.command:1",
        payload={
            "task_id": task_id,
            "state": "accepted",
            "op": "submit",
            "actor": "owner",
            **logical,
            "objective_revision": 1,
            "idempotency_key": key,
            "payload_digest": digest(logical),
            "harness_version": "seed-v1",
            "explicit_skill": None,
            "plugin_versions": {},
        },
    )


def insert_legacy_submit(db, body, *, writer="agent", original=None):
    """Test-only historical insertion, with original request and SQL identity."""
    sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM entries").fetchone()[0]
    entry = JournalEntry(**body.model_dump(), sequence=sequence, origin="agent-supervisor")
    with db:
        db.execute(
            "INSERT INTO entries(sequence,event_id,session_id,thread_id,epoch,kind,writer,"
            "idem,request,value) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                entry.sequence,
                entry.event_id,
                entry.session_id,
                entry.thread_id,
                entry.epoch,
                entry.kind,
                writer,
                entry.idempotency_key,
                body.model_dump_json() if original is None else json.dumps(original),
                entry.model_dump_json(),
            ),
        )
    return entry


def legacy_database(path, session, body, **kwargs):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE schema_meta(version INTEGER NOT NULL);
            INSERT INTO schema_meta VALUES(1);
            CREATE TABLE sessions(session_id TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE entries(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                thread_id TEXT NOT NULL,epoch INTEGER NOT NULL,kind TEXT NOT NULL,
                writer TEXT NOT NULL,idem TEXT NOT NULL,request TEXT NOT NULL,value TEXT NOT NULL,
                UNIQUE(session_id,writer,idem));
        """)
        db.execute(
            "INSERT INTO sessions VALUES(?,?)", (session.session_id, session.model_dump_json())
        )
        return insert_legacy_submit(db, body, **kwargs)


def current_submit(session, *, task_id="new-proposal", key="historical-key"):
    old = historical_submit(session, task_id=task_id, key=key)
    payload = copy.deepcopy(old.payload)
    logical = {
        name: payload[name]
        for name in (
            "session_id",
            "thread_id",
            "epoch",
            "objective",
            "source_event_ids",
            "source_revision",
        )
    }
    logical["content_scope"] = "local_only"
    payload.update(
        requested_content_scope="local_only",
        content_scope="local_only",
        model_pin=None,
        payload_digest=digest(logical),
    )
    return old.model_copy(update={"payload": payload}, deep=True)


def query(session):
    return AgentSubmitLookupRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        idempotency_key="historical-key",
    )


def rows(store):
    return tuple(tuple(row) for row in store.db.execute("SELECT * FROM entries ORDER BY sequence"))


@pytest.fixture
def legacy(tmp_path):
    session = Session(
        session_id="legacy-session", thread_id="legacy-thread", channel="agent", epoch=1
    )
    body = historical_submit(session)
    path = tmp_path / "legacy.sqlite"
    entry = legacy_database(path, session, body)
    return session, body, entry, path


def test_authentic_pre_scope_proof_preserves_both_absent_scopes_and_pin(legacy):
    session, body, entry, _ = legacy
    assert len(body.payload) == 16
    assert {"content_scope", "requested_content_scope", "model_pin"}.isdisjoint(body.payload)
    raw = entry.model_dump(mode="json")
    assert submit_entry(raw).model_dump(mode="json") == raw
    lookup = AgentSubmitLookupResult(status="recorded", entry=raw).validate_request(query(session))
    assert lookup.entry.model_dump(mode="json") == raw
    retained = AgentSubmitReceipt(disposition="retained", entry=raw).validate_request(
        current_submit(session)
    )
    assert retained.entry.model_dump(mode="json") == raw
    assert {"content_scope", "requested_content_scope", "model_pin"}.isdisjoint(
        retained.entry.payload
    )


def test_native_migration_retains_authentic_pre_scope_entry_for_current_local_proposal(legacy):
    session, _, entry, path = legacy
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='agent_submit_slots'").fetchone()
            is None
        )
    store = Store(path)
    try:
        before = rows(store)
        observed = store.lookup_submit(query(session), "agent")
        assert observed.status == "recorded"
        assert observed.entry.model_dump(mode="json") == entry.model_dump(mode="json")
        proposal = current_submit(session)
        retained = store.admit_submit(proposal, "agent").validate_request(proposal)
        assert retained.disposition == "retained" and retained.entry.event_id == entry.event_id
        assert retained.entry.payload["task_id"] == "historical"
        assert retained.entry.model_dump(mode="json") == entry.model_dump(mode="json")
        assert rows(store) == before
        assert tuple(
            store.db.execute("SELECT logical_key,event_id FROM agent_submit_slots").fetchone()
        ) == ("historical-key", entry.event_id)
    finally:
        store.close()


class NoProviderAccess:
    def __init__(self):
        self.metadata_reads = 0
        self.calls = []

    @property
    def info(self):
        self.metadata_reads += 1
        raise AssertionError("Historical receipts and controls must not consult a current provider")

    async def prepare_complete(self, messages):
        self.calls.append(messages)
        raise AssertionError("Historical work must never perform inference")

    async def complete(self, messages):
        self.calls.append(messages)
        raise AssertionError("Historical work must never perform inference")


async def test_runtime_recovery_receipt_and_controls_keep_authentic_old_work_local(
    legacy, tmp_path
):
    session, _, entry, path = legacy
    store = Store(path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = NoProviderAccess()
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace))
    registry = Registry(runtime)
    before = rows(store)
    try:
        recovered = await runtime.recover()
        assert len(recovered) == 1
        task = recovered[0]
        assert task.task_id == "historical" and task.state == "paused"
        assert task.content_scope == "local_only" and task.model_pin is None
        replay = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Read public.txt",
                idempotency_key="historical-key",
            )
        )
        assert replay.task_id == task.task_id and replay.state == "paused"
        assert replay.content_scope == "local_only" and replay.model_pin is None
        assert rows(store) == before
        result = await registry.execute(
            CommandRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                input="/status",
                idempotency_key="historical-status",
            )
        )
        assert result["result"][0]["content_scope"] == "local_only"
        await registry.execute(
            CommandRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                input="/stop historical",
                idempotency_key="historical-stop",
            )
        )
        assert (await runtime.status(task.task_id)).state == "cancelled"
        assert provider.calls == [] and provider.metadata_reads == 0
        assert store.entry(entry.event_id).model_dump(mode="json") == entry.model_dump(mode="json")
        after = tuple(
            store.db.execute("SELECT * FROM entries WHERE event_id=?", (entry.event_id,)).fetchone()
        )
        assert after == before[0]
    finally:
        registry.close()
        await runtime.close()
        store.close()


@pytest.mark.parametrize("missing", ["content_scope", "requested_content_scope", "both"])
def test_new_admission_cannot_omit_current_scope_fields(legacy, missing):
    session, _, _, path = legacy
    proposal = current_submit(session).model_dump(mode="json")
    for name in ("content_scope", "requested_content_scope"):
        if missing in {name, "both"}:
            proposal["payload"].pop(name)
    with pytest.raises(ValueError):
        submit_request(proposal)
    store = Store(path)
    try:
        before = rows(store)
        with pytest.raises(AgentSubmitUnavailable):
            store.admit_submit(AppendRequest.model_validate(proposal), "agent")
        assert rows(store) == before
        assert not store.db.execute("SELECT * FROM agent_submit_slots").fetchall()
    finally:
        store.close()


def test_scope_present_historical_entry_cannot_omit_effective_scope(legacy):
    session, _, old, _ = legacy
    raw = old.model_dump(mode="json")
    current = current_submit(session)
    raw["payload"]["requested_content_scope"] = "local_only"
    raw["payload"]["payload_digest"] = current.payload["payload_digest"]
    with pytest.raises(ValueError):
        submit_entry(raw)


@pytest.mark.parametrize("scope", ["public_synthetic", "private_cloud_authorized"])
def test_authentic_pre_scope_receipt_cannot_grant_new_cloud_scope(legacy, scope):
    session, _, entry, _ = legacy
    proposal = current_submit(session).model_dump(mode="json")
    proposal["payload"]["content_scope"] = scope
    proposal["payload"]["requested_content_scope"] = scope
    logical = {
        name: proposal["payload"][name]
        for name in (
            "session_id",
            "thread_id",
            "epoch",
            "objective",
            "source_event_ids",
            "source_revision",
            "content_scope",
        )
    }
    proposal["payload"]["payload_digest"] = digest(logical)
    assert submit_request(proposal).payload["content_scope"] == scope
    with pytest.raises(ValueError):
        AgentSubmitReceipt(disposition="retained", entry=entry).validate_request(proposal)


@pytest.mark.parametrize("fault", ["original_key", "digest", "writer"])
def test_corrupt_pre_scope_provenance_never_backfills_a_winner(tmp_path, fault):
    session = Session(
        session_id="legacy-session", thread_id="legacy-thread", channel="agent", epoch=1
    )
    body = historical_submit(session)
    original = body.model_dump(mode="json")
    writer = "agent"
    if fault == "original_key":
        original["payload"]["idempotency_key"] = "different-original-key"
    elif fault == "digest":
        original["payload"]["payload_digest"] = "0" * 64
        body.payload["payload_digest"] = "0" * 64
    else:
        writer = "owner"
    path = tmp_path / "corrupt-legacy.sqlite"
    legacy_database(path, session, body, writer=writer, original=original)
    store = Store(path)
    try:
        before = rows(store)
        with pytest.raises(AgentSubmitUnavailable):
            store.lookup_submit(query(session), "agent")
        with pytest.raises(AgentSubmitUnavailable):
            store.admit_submit(current_submit(session), "agent")
        assert rows(store) == before
        assert not store.db.execute("SELECT * FROM agent_submit_slots").fetchall()
        assert len(store.read(kinds=["agent.command"])) == 1
    finally:
        store.close()
