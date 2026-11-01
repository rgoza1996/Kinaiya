"""Codex #217: durable bootstrap provenance and native historical proof."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from test_agent_operating_mode_native import change, proposal, steering

from packages.contracts.agent_submit import AgentSubmitUnavailable
from packages.contracts.interfaces import AgentControlUnavailable, AgentModeOff
from packages.contracts.models import SessionRequest
from services.common.auth import Auth
from services.session import agent_control
from services.session.agent_control import AgentControl
from services.session.api import create_app
from services.session.store import LocalJournal, Store

AUTH = Auth({"owner": "o" * 32, "agent": "a" * 32})
HEADERS = {"Authorization": "Bearer " + "a" * 32}


class RegistrationCommitFault:
    def __init__(self, actual, phase):
        self.actual, self.phase = actual, phase
        self.fired = self.rollback_fired = False
        self.mode_at_fault = None

    def __getattr__(self, name):
        return getattr(self.actual, name)

    def commit(self):
        if not self.fired:
            assert self.actual.in_transaction
            assert (
                self.actual.execute("SELECT count(*) FROM agent_control_generations").fetchone()[0]
                == 1
            )
            self.mode_at_fault = self.actual.execute(
                "SELECT mode FROM agent_control_state"
            ).fetchone()[0]
            self.fired = True
            if self.phase == "after":
                self.actual.commit()
            raise RuntimeError("Synthetic registration commit uncertainty")
        self.actual.commit()

    def rollback(self):
        self.rollback_fired = True
        self.actual.rollback()

    def __enter__(self):
        self.actual.__enter__()
        return self

    def __exit__(self, kind, value, traceback):
        return self.actual.__exit__(kind, value, traceback)


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("observe_before_reopen", [False, True])
async def test_failed_off_registration_cannot_default_current_or_reopened_store(
    tmp_path, phase, observe_before_reopen
):
    path = tmp_path / "failed-registration.sqlite"
    store = Store(path)
    session = store.create_session(SessionRequest(channel="agent"))
    backend = store.agent_control.backend()
    fault = RegistrationCommitFault(store.db, phase)
    store.db = fault
    try:
        app = create_app(store, AUTH, agent_enabled=False)
        assert fault.fired and fault.rollback_fired and fault.mode_at_fault == "off"
        assert not store.db.in_transaction
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
            ) as client,
        ):
            state = await client.get("/v1/agent-control/state", headers=HEADERS)
            assert state.status_code == 503
            if observe_before_reopen:
                assert store.agent_control.backend() == backend
                result = await client.post(
                    "/v1/transcript/agent-submits",
                    json=proposal(session).model_dump(mode="json"),
                    headers=HEADERS,
                )
                assert result.status_code == (503 if phase == "before" else 403)
                assert (
                    store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 0
                )
    finally:
        store.close()

    reopened = Store(path)
    try:
        assert reopened.agent_control.new_store is False
        assert reopened.agent_control.backend() == backend
        if phase == "before":
            with pytest.raises(AgentSubmitUnavailable):
                reopened.admit_submit(proposal(session), "agent")
            assert (
                reopened.db.execute("SELECT agent_control_version FROM schema_meta").fetchone()[0]
                == 0
            )
        app = create_app(reopened, AUTH, agent_enabled=False)
        assert app.state.agent_control.snapshot().mode == "off"
        assert app.state.agent_control.snapshot().backend_id == backend
        with pytest.raises(AgentModeOff):
            reopened.admit_submit(proposal(session), "agent")
        assert reopened.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 0
    finally:
        reopened.close()


async def test_creator_backend_read_is_passive_then_local_admission_initializes_default(tmp_path):
    store = Store(tmp_path / "creator.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="agent"))
        journal = LocalJournal(store, "agent")
        backend = await journal.agent_control_backend()
        assert store.db.execute("SELECT agent_control_version FROM schema_meta").fetchone()[0] == 0
        with pytest.raises(AgentControlUnavailable):
            await journal.agent_control_state()
        assert (
            store.db.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name='agent_control_generations'"
            ).fetchone()[0]
            == 0
        )
        assert (await journal.admit_submit(proposal(session))).disposition == "created"
        state = store.agent_control.metadata()
        assert (
            state["backend_id"] == backend
            and state["mode"] == "on_demand"
            and state["instance_id"] is None
        )
        assert store.db.execute("SELECT count(*) FROM agent_control_generations").fetchone()[0] == 0
    finally:
        store.close()


async def test_registration_attempt_retires_default_capability_before_uuid_allocation(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "allocation-failure.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))

    def allocation_failure():
        raise RuntimeError("Synthetic native generation allocation failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(agent_control, "uuid", SimpleNamespace(uuid4=allocation_failure))
            app = create_app(store, AUTH, agent_enabled=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
            ) as client,
        ):
            state = await client.get("/v1/agent-control/state", headers=HEADERS)
            assert state.status_code == 503
            with pytest.raises(AgentSubmitUnavailable):
                store.admit_submit(proposal(session), "agent")
            assert (
                store.db.execute("SELECT agent_control_version FROM schema_meta").fetchone()[0] == 0
            )
            assert store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 0
    finally:
        store.close()


def test_auxiliary_pending_reader_cannot_consume_creator_default_capability(tmp_path):
    creator = Store(tmp_path / "pending.sqlite")
    observer = Store(creator.path)
    try:
        session = creator.create_session(SessionRequest(channel="agent"))
        backend = creator.agent_control.backend()
        assert observer.agent_control.backend() == backend
        with pytest.raises(AgentSubmitUnavailable):
            observer.admit_submit(proposal(session), "agent")
        assert (
            creator.db.execute("SELECT agent_control_version FROM schema_meta").fetchone()[0] == 0
        )
        assert creator.admit_submit(proposal(session), "agent").disposition == "created"
        assert observer.agent_control.metadata()["mode"] == "on_demand"
    finally:
        observer.close()
        creator.close()


def test_original_creator_first_commit_is_not_fenced_by_other_uncommitted_off(tmp_path):
    creator = Store(tmp_path / "two-owners.sqlite")
    other = Store(creator.path)
    try:
        session = creator.create_session(SessionRequest(channel="agent"))
        fault = RegistrationCommitFault(other.db, "before")
        other.db = fault
        failed = create_app(other, AUTH, agent_enabled=False)
        assert fault.fired and fault.rollback_fired and failed.state.agent_control.instance is None
        assert (
            creator.db.execute("SELECT agent_control_version FROM schema_meta").fetchone()[0] == 0
        )
        assert creator.admit_submit(proposal(session), "agent").disposition == "created"
        healthy = create_app(other, AUTH, agent_enabled=False)
        assert healthy.state.agent_control.snapshot().mode == "on_demand"
        assert healthy.state.agent_control.snapshot().revision == 0
    finally:
        other.close()
        creator.close()


def test_genuine_legacy_database_remains_distinct_from_pending_modern_origin(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as old:
        old.executescript(
            "CREATE TABLE schema_meta(version INTEGER NOT NULL); INSERT INTO schema_meta VALUES(1); CREATE TABLE other_component(value TEXT);"
        )
    store = Store(path)
    try:
        assert store.agent_control.new_store is False
        app = create_app(store, AUTH, agent_enabled=False)
        assert app.state.agent_control.snapshot().mode == "on_demand"
        session = store.create_session(SessionRequest(channel="agent"))
        assert store.admit_submit(proposal(session), "agent").disposition == "created"
    finally:
        store.close()


def test_two_simultaneous_constructors_assign_only_one_default_creator(tmp_path):
    path = tmp_path / "constructor-race.sqlite"
    with sqlite3.connect(path) as empty:
        empty.execute("PRAGMA journal_mode=WAL")
    gate = threading.Barrier(2)

    def open_store():
        gate.wait(timeout=3)
        return Store(path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(lambda _: open_store(), range(2)))
    try:
        assert sum(store.agent_control.new_store for store in stores) == 1
        assert len({store.agent_control.backend() for store in stores}) == 1
        assert all(
            store.db.execute("SELECT agent_control_version FROM schema_meta").fetchone()[0] == 0
            for store in stores
        )
    finally:
        for store in stores:
            store.close()


@pytest.fixture
def native_control(tmp_path):
    store = Store(tmp_path / "proof.sqlite")
    controller = AgentControl(store)
    controller.register()
    session = store.create_session(SessionRequest(channel="agent"))
    initial = store.admit_submit(proposal(session), "agent").entry
    try:
        yield store, controller, initial
    finally:
        store.close()


@pytest.mark.parametrize("timestamp", [1, 1.0, True, "1", "2026-10-31", "2026-10-31T01:02:03"])
def test_native_timestamp_is_an_aware_iso_string_without_coercion(native_control, timestamp):
    store, owner, initial = native_control
    original = steering(owner, initial)
    saved = owner.admit_steer(original)
    row = store.db.execute(
        "SELECT value FROM entries WHERE event_id=?", (saved.event_id,)
    ).fetchone()
    value = json.loads(row[0])
    value["created_at"] = timestamp
    store.db.execute(
        "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(value), saved.event_id)
    )
    store.db.commit()
    with pytest.raises(AgentControlUnavailable):
        owner.lookup_steer(original)


def test_native_timestamp_retains_equivalent_utc_iso_format(native_control):
    store, owner, initial = native_control
    original = steering(owner, initial)
    saved = owner.admit_steer(original)
    value = json.loads(
        store.db.execute(
            "SELECT value FROM entries WHERE event_id=?", (saved.event_id,)
        ).fetchone()[0]
    )
    value["created_at"] = value["created_at"].removesuffix("Z") + "+00:00"
    store.db.execute(
        "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(value), saved.event_id)
    )
    store.db.commit()
    assert owner.lookup_steer(original).status == "recorded"


@pytest.mark.parametrize("fault", ["future", "initial-mode", "historical-mode", "historical-proof"])
def test_native_steer_receipt_requires_actual_historical_control_authority(native_control, fault):
    store, owner, initial = native_control
    if fault.startswith("historical"):
        owner.change(change(owner, "on_demand", "historical"))
    original = steering(owner, initial)
    owner.admit_steer(original)
    owner.change(change(owner, "off", "off"))
    owner.change(change(owner, "on_demand", "aba"))
    assert owner.lookup_steer(original).status == "recorded"
    if fault == "historical-proof":
        request = json.loads(
            store.db.execute(
                "SELECT request FROM agent_control_changes WHERE key='historical'"
            ).fetchone()[0]
        )
        request["mode"] = "off"
        store.db.execute(
            "UPDATE agent_control_changes SET request=? WHERE key='historical'",
            (json.dumps(request),),
        )
    else:
        receipt = json.loads(
            store.db.execute("SELECT receipt FROM agent_steer_intents").fetchone()[0]
        )
        if fault == "future":
            receipt["control_revision"] = owner.snapshot().revision + 1
        else:
            receipt["admission_mode"] = "autonomous"
        store.db.execute("UPDATE agent_steer_intents SET receipt=?", (json.dumps(receipt),))
    store.db.commit()
    with pytest.raises(AgentControlUnavailable):
        owner.lookup_steer(original)
