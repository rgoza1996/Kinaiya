"""Codex independent native-slot discovery regressions for #228/#239."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from packages.contracts.models import AppendRequest, SessionRequest
from packages.contracts.standing_resources import TaskResourceQuery
from packages.contracts.standing_work import StandingUnavailable
from services.session.agent_control import AgentControl
from services.session.standing_admission import StandingTaskOwner
from services.session.standing_dispatch import StandingDispatchOwner
from services.session.standing_execution import StandingExecutionOwner
from services.session.standing_resources import StandingResourceOwner
from services.session.standing_work import StandingOwner
from services.session.store import Store


@pytest.fixture
def native_resources(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    control = AgentControl(store, new_store=store.agent_control.new_store)
    control.register()
    owner = StandingOwner(store, control)
    owner.register()
    dispatch = StandingDispatchOwner(store, control)
    dispatch.register()
    claims = StandingExecutionOwner(store, control, owner, dispatch)
    claims.register()
    tasks = StandingTaskOwner(store, control, owner, dispatch, claims)
    tasks.register()
    resources = StandingResourceOwner(store, control, tasks)
    resources.register()
    try:
        yield SimpleNamespace(store=store, resources=resources)
    finally:
        resources.close()
        tasks.close()
        claims.close()
        dispatch.close()
        owner.close()
        control.close()
        store.close()


def _admit(store, key):
    session = store.create_session(SessionRequest(channel="agent"))
    logical = {
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "epoch": session.epoch,
        "objective": "Inspect public synthetic task input",
        "source_event_ids": [],
        "source_revision": 1,
        "content_scope": "public_synthetic",
    }
    payload = {
        "task_id": "same-task",
        "state": "accepted",
        "op": "submit",
        "actor": "owner",
        **{k: v for k, v in logical.items() if k != "content_scope"},
        "objective_revision": 1,
        "requested_content_scope": "public_synthetic",
        "content_scope": "public_synthetic",
        "idempotency_key": key,
        "payload_digest": hashlib.sha256(
            json.dumps(logical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "harness_version": "seed-v1",
        "model_pin": {
            "schema_version": "1.0",
            "profile_id": "public-synthetic",
            "provider": "synthetic",
            "model": "synthetic",
            "local": False,
            "reasoning": None,
            "settings_revision": 0,
            "catalog_binding": None,
            "catalog_revision": None,
        },
        "explicit_skill": None,
        "plugin_versions": {},
    }
    request = AppendRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        kind="agent.command",
        payload=payload,
        causes=[],
        task_refs=["same-task"],
        idempotency_key="agent:same-task:agent.command:1",
    )
    receipt = store.admit_submit(request, "agent").validate_request(request)
    assert receipt.disposition == "created"
    return receipt.entry


def _slots(store):
    return tuple(
        tuple(row)
        for row in store.db.execute(
            "SELECT session_id,logical_key,event_id FROM agent_submit_slots ORDER BY session_id"
        )
    )


def test_single_native_ordinary_slot_returns_exact_context_without_mutation(native_resources):
    store, resources = native_resources.store, native_resources.resources
    entry = _admit(store, "public:single")
    before = tuple(store.db.iterdump())
    result = resources.context(TaskResourceQuery(schema_version="1.0", task_id="same-task"))
    assert result.family == "ordinary" and result.standing is None
    assert result.ordinary.submit_event_id == entry.event_id
    assert result.ordinary.submit_sequence == entry.sequence
    assert result.ordinary.session_id == entry.session_id
    assert result.ordinary.submit_idempotency_key == "public:single"
    assert tuple(store.db.iterdump()) == before


def test_corrupt_slot_backed_initial_cannot_disappear_from_task_discrimination(native_resources):
    store, resources = native_resources.store, native_resources.resources
    first, second = _admit(store, "public:first"), _admit(store, "public:second")
    assert first.session_id != second.session_id
    assert first.payload["task_id"] == second.payload["task_id"] == "same-task"
    slots = _slots(store)
    assert len(slots) == 2 and {row[2] for row in slots} == {first.event_id, second.event_id}
    query = TaskResourceQuery(schema_version="1.0", task_id="same-task")
    before_ambiguous = tuple(store.db.iterdump())
    with pytest.raises(StandingUnavailable):
        resources.context(query)
    assert tuple(store.db.iterdump()) == before_ambiguous

    row = store.db.execute(
        "SELECT request,value FROM entries WHERE event_id=?", (first.event_id,)
    ).fetchone()
    request, value = json.loads(row["request"]), json.loads(row["value"])
    assert request["payload"]["op"] == value["payload"]["op"] == "submit"
    request["payload"]["op"] = value["payload"]["op"] = "finish"
    with store.db:
        store.db.execute(
            "UPDATE entries SET request=?,value=? WHERE event_id=?",
            (json.dumps(request), json.dumps(value), first.event_id),
        )
    assert _slots(store) == slots
    assert (
        store.db.execute("SELECT kind FROM entries WHERE event_id=?", (first.event_id,)).fetchone()[
            0
        ]
        == "agent.command"
    )
    before_lookup = tuple(store.db.iterdump())
    try:
        with pytest.raises(StandingUnavailable):
            resources.context(query)
    finally:
        assert tuple(store.db.iterdump()) == before_lookup
        assert _slots(store) == slots
