"""Selected recall uses the real persistent graph, without inference or sockets."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from packages.contracts.memory_context import MemoryContextPin, MemoryRecallResult
from packages.contracts.models import AppendRequest, SessionRequest, new_id
from services.session.memory import MemoryError, MemoryStore
from services.session.store import Store


def binding(session):
    return {key: getattr(session, key) for key in ("session_id", "thread_id", "epoch")}


def source(store, session, text="Public synthetic source."):
    return store.append(
        AppendRequest(
            **binding(session),
            kind="conversation.user",
            idempotency_key=new_id(),
            payload={"text": text, "final": True, "revision": 1},
        ),
        "owner",
    )


def capture(host, event, *, parents=(), kind="assertion", body="Owner-curated synthetic note."):
    return host.memory.capture(
        {
            **binding(host.session),
            "expected_revision": host.memory.state(binding(host.session)).revision,
            "idempotency_key": new_id(),
            "kind": kind,
            "source_event_id": event.event_id,
            "source_revision": 1,
            "body": body,
            "prerequisite_ids": list(parents),
            "fact": {"key": "public.preference", "value": "green"} if kind != "inference" else None,
        }
    )


@pytest.fixture
def host(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    memory = MemoryStore(store)
    memory.configure({**binding(session), "expected_revision": 0, "enabled": True})
    value = SimpleNamespace(store=store, memory=memory, session=session)
    value.source = source(store, session)
    value.parent = capture(value, value.source).node
    value.child = capture(
        value, value.source, kind="inference", parents=[value.parent.node_id]
    ).node
    yield value
    store.close()


def pin_for(host, recall=None, **changes):
    recall = recall or host.memory.recall(
        {**binding(host.session), "seed_ids": [host.child.node_id]}
    )
    return MemoryContextPin.model_validate(
        {
            "schema_version": "1.0",
            "implementation_id": "korina-dag-v1",
            "use_generation": 7,
            "namespace_revision": recall.state.revision,
            "closure_sha256": recall.closure_sha256,
            "selector_ids": [host.child.node_id],
            **changes,
        }
    )


def test_recall_returns_exact_complete_dependency_snapshot(host):
    request = {**binding(host.session), "seed_ids": [host.child.node_id]}
    result = host.memory.recall(request)
    assert type(result) is MemoryRecallResult
    assert [node.node_id for node in result.nodes] == [host.parent.node_id, host.child.node_id]
    original = host.memory.query(request).model_dump(mode="json")
    canonical = json.dumps(
        original, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    assert result.closure_sha256 == hashlib.sha256(canonical).hexdigest()
    assert result.implementation_id == "korina-dag-v1"
    assert all(node.content_scope == "local_only" for node in result.nodes)


def test_validate_context_and_publication_use_the_same_complete_snapshot(host):
    pin = pin_for(host)
    result = host.memory.validate_context({**binding(host.session), "pin": pin.model_dump()})
    assert result.valid is True and result.state.enabled
    assert host.memory.validate_publication(**binding(host.session), pin=pin) is True
    assert [e.event_id for e in host.store.read(session_id=host.session.session_id)] == [
        host.source.event_id
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace_revision", 0),
        ("closure_sha256", "0" * 64),
        ("implementation_id", "replacement-v1"),
    ],
)
def test_stale_or_other_implementation_pin_is_not_admitted(host, field, value):
    pin = pin_for(host, **{field: value})
    result = host.memory.validate_context({**binding(host.session), "pin": pin.model_dump()})
    assert result.valid is False


@pytest.mark.parametrize("cold", [False, True])
def test_disabled_validation_reads_no_graph_or_retained_contents(host, monkeypatch, cold):
    pin = pin_for(host)
    if cold:
        session = host.store.create_session(SessionRequest(channel="converse"))
    else:
        session = host.session
        host.memory.configure(
            {
                **binding(session),
                "expected_revision": host.memory.state(binding(session)).revision,
                "enabled": False,
            }
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled validation must not load/reconcile/query any graph")

    for name in ("_load", "_reconcile", "_available", "query", "recall"):
        monkeypatch.setattr(host.memory, name, forbidden)
    result = host.memory.validate_context({**binding(session), "pin": pin.model_dump()})
    assert result.valid is False and result.state.enabled is False


@pytest.mark.parametrize(
    "changes",
    [
        {"seed_ids": []},
        {"seed_ids": [True]},
        {"seed_ids": ["same", "same"]},
        {"seed_ids": ["x"] * 9},
        {"seed_ids": ["\ud800"]},
        {"seed_ids": ["ok"], "fact_keys": ["no"]},
        {"seed_ids": ["ok"], "epoch": True},
    ],
)
def test_recall_rejects_untrusted_query_shape_before_graph_read(host, changes, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid query must not read the graph")

    monkeypatch.setattr(host.memory, "query", forbidden)
    with pytest.raises(MemoryError) as caught:
        host.memory.recall({**binding(host.session), **changes})
    assert caught.value.status == 422


def test_disabled_recall_refuses_instead_of_returning_retained_notes(host):
    host.memory.configure(
        {
            **binding(host.session),
            "expected_revision": host.memory.state(binding(host.session)).revision,
            "enabled": False,
        }
    )
    with pytest.raises(MemoryError) as caught:
        host.memory.recall({**binding(host.session), "seed_ids": [host.child.node_id]})
    assert caught.value.status == 409


def test_unrelated_capture_changes_the_namespace_pin_without_mutating_old_snapshot(host):
    old = host.memory.recall({**binding(host.session), "seed_ids": [host.child.node_id]})
    pin = pin_for(host, old)
    capture(
        host,
        host.source,
        kind="inference",
        parents=[host.parent.node_id],
        body="Another public inference.",
    )
    result = host.memory.validate_context({**binding(host.session), "pin": pin.model_dump()})
    assert result.valid is False and result.state.revision > old.state.revision
    assert old.nodes[-1].body == host.child.body


def test_forget_source_correction_and_reset_refuse_old_pin(host):
    pin = pin_for(host)
    host.memory.forget(
        {
            **binding(host.session),
            "expected_revision": pin.namespace_revision,
            "node_id": host.parent.node_id,
            "idempotency_key": "erase-parent",
        }
    )
    with pytest.raises(MemoryError):
        host.memory.validate_context({**binding(host.session), "pin": pin.model_dump()})
    host.session = host.store.reset(host.session.session_id)
    with pytest.raises(MemoryError):
        host.memory.validate_context(
            {
                "session_id": host.session.session_id,
                "thread_id": host.parent.thread_id,
                "epoch": host.parent.epoch,
                "pin": pin.model_dump(),
            }
        )


def test_recall_refuses_corrupt_dependency_cycle_and_persists_quarantine(host):
    with host.store.db:
        host.store.db.execute(
            "INSERT INTO memory_edges VALUES(?,?)", (host.child.node_id, host.parent.node_id)
        )
    with pytest.raises(MemoryError):
        host.memory.recall({**binding(host.session), "seed_ids": [host.child.node_id]})
    assert host.memory.state(binding(host.session)).status == "quarantined"


def test_recall_complete_byte_limit_never_returns_a_truncated_closure(host):
    last = host.child.node_id
    for index in range(9):
        last = capture(
            host,
            host.source,
            kind="inference",
            parents=[last],
            body=f"Public node{index}: " + "x" * 1900,
        ).node.node_id
    with pytest.raises(MemoryError) as caught:
        host.memory.recall({**binding(host.session), "seed_ids": [last]})
    assert caught.value.status == 409 and "bound" in str(caught.value)


def test_recall_respects_core_depth_limit(host):
    last = host.child.node_id
    for _ in range(14):
        last = capture(host, host.source, kind="inference", parents=[last]).node.node_id
    result = host.memory.recall({**binding(host.session), "seed_ids": [last]})
    assert len(result.nodes) == 16
    with pytest.raises(MemoryError):
        capture(host, host.source, kind="inference", parents=[last])
