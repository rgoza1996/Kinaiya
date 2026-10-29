"""Actual Session append authority and selected-memory HTTP bridge."""

import builtins
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from packages.contracts.memory_context import MemoryContextPin
from packages.contracts.models import AppendRequest, SessionRequest, new_id
from services.common.auth import Auth
from services.session.api import create_app
from services.session.memory import MemoryError
from services.session.store import JournalError, Store

PREFIX = "/v1/converse-memory"
TOKENS = {
    role: letter * 32
    for role, letter in zip(
        ("owner", "converse", "agent", "decisions", "resources"), "ocadr", strict=True
    )
}
OWNER = {"Authorization": "Bearer " + TOKENS["owner"]}
CONVERSE = {"Authorization": "Bearer " + TOKENS["converse"]}


def binding(session):
    return {key: getattr(session, key) for key in ("session_id", "thread_id", "epoch")}


@pytest.fixture
def host(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    app = create_app(store, Auth(TOKENS, ("https://ui.invalid",)), converse_memory=True)
    memory = app.state.converse_memory
    memory.configure({**binding(session), "expected_revision": 0, "enabled": True})
    event = store.append(
        AppendRequest(
            **binding(session),
            kind="conversation.user",
            idempotency_key="public-source",
            payload={"text": "Public source.", "final": True, "revision": 1},
        ),
        "owner",
    )
    saved = memory.capture(
        {
            **binding(session),
            "expected_revision": 1,
            "idempotency_key": "note",
            "kind": "assertion",
            "source_event_id": event.event_id,
            "source_revision": 1,
            "body": "Owner-curated public note.",
            "fact": {"key": "public.note", "value": "green"},
        }
    )
    recalled = memory.recall({**binding(session), "seed_ids": [saved.node.node_id]})
    pin = MemoryContextPin(
        schema_version="1.0",
        implementation_id="korina-dag-v1",
        use_generation=1,
        namespace_revision=recalled.state.revision,
        closure_sha256=recalled.closure_sha256,
        selector_ids=[saved.node.node_id],
    )
    with TestClient(app, base_url="https://session.invalid") as client:
        yield SimpleNamespace(
            store=store,
            session=session,
            app=app,
            memory=memory,
            source=event,
            node=saved.node,
            pin=pin,
            client=client,
        )
    store.close()


def publication(host, **changes):
    return AppendRequest(
        **{
            **binding(host.session),
            "kind": "conversation.converse",
            "idempotency_key": new_id(),
            "causes": [host.source.event_id],
            "payload": {
                "text": "Public memory-derived reply.",
                "final": True,
                "runtime_id": "native-runtime",
                "content_scope": "local_only",
                "memory_context": host.pin.model_dump(),
            },
            **changes,
        }
    )


def test_reserved_memory_marker_cannot_enter_as_authenticated_user_content(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    try:
        request = AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            idempotency_key="forged-reserved-marker",
            payload={"text": "Public synthetic input.", "final": True, "memory_context": None},
        )
        with pytest.raises(JournalError):
            store.append(request, "owner")
        assert store.read(session_id=session.session_id) == []
    finally:
        store.close()


def test_bridge_roundtrip_owner_and_converse_and_unchanged_admin_authority(host):
    recall = {**binding(host.session), "seed_ids": [host.node.node_id]}
    for headers in (OWNER, CONVERSE):
        response = host.client.post(PREFIX + "/recall", json=recall, headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert len(response.content) <= 17 * 1024
        assert response.json()["closure_sha256"] == host.pin.closure_sha256
        checked = host.client.post(
            PREFIX + "/validate",
            json={**binding(host.session), "pin": host.pin.model_dump()},
            headers=headers,
        )
        assert checked.status_code == 200 and checked.json()["valid"] is True
        assert checked.headers["cache-control"] == "no-store" and len(checked.content) <= 4096
    for route in ("state", "configure", "capture", "inspect", "query", "forget"):
        assert (
            host.client.post(
                PREFIX + "/" + route, json=binding(host.session), headers=CONVERSE
            ).status_code
            == 403
        )


@pytest.mark.parametrize("route", ["recall", "validate"])
@pytest.mark.parametrize(
    "identity",
    [
        "absent",
        "agent",
        "decisions",
        "resources",
        "wrong-origin",
        "duplicate-auth",
        "duplicate-origin",
    ],
)
async def test_bridge_denial_never_receives_a_body(host, route, identity):
    headers = [(b"authorization", CONVERSE["Authorization"].encode())]
    expected = 403
    if identity == "absent":
        headers, expected = [], 401
    elif identity in TOKENS:
        headers = [(b"authorization", b"Bearer " + TOKENS[identity].encode())]
    elif identity == "wrong-origin":
        headers.append((b"origin", b"https://foreign.invalid"))
    elif identity == "duplicate-auth":
        headers *= 2
        expected = 401
    else:
        headers.extend(
            [(b"origin", b"https://ui.invalid"), (b"origin", b"https://foreign.invalid")]
        )
    messages = []

    async def receive():
        raise AssertionError("Rejected authorization reached body receive")

    async def send(message):
        messages.append(message)

    path = PREFIX + "/" + route
    await host.app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "server": ("session.invalid", 443),
            "client": ("127.0.0.1", 12345),
        },
        receive,
        send,
    )
    assert messages[0]["status"] == expected
    assert (b"cache-control", b"no-store") in messages[0]["headers"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"session_id":"secret","session_id":"other"}',
        b'{"epoch":NaN}',
        b'{"epoch":Infinity}',
        b"\xff",
        b'"\\ud800"',
        b"[" * 1500,
        b'{"seed_ids":[true]}',
        b'{"private":"untrusted-sentinel"}',
    ],
)
def test_bridge_native_json_errors_are_fixed_and_no_store(host, raw):
    response = host.client.post(
        PREFIX + "/recall", content=raw, headers={**CONVERSE, "Content-Type": "application/json"}
    )
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert (
        len(response.content) < 150
        and "secret" not in response.text
        and "sentinel" not in response.text
    )


def test_bridge_query_header_and_content_type_fail_closed(host):
    body = {**binding(host.session), "seed_ids": [host.node.node_id]}
    assert (
        host.client.post(PREFIX + "/recall?secret=x", json=body, headers=CONVERSE).status_code
        == 422
    )
    for name, value in [
        ("Content-Type", "application/json"),
        ("Content-Encoding", "identity"),
        ("Content-Length", "10"),
    ]:
        headers = [*CONVERSE.items(), (name, value), (name, value)]
        assert (
            host.client.post(
                PREFIX + "/recall", content=json.dumps(body), headers=headers
            ).status_code
            == 422
        )
    assert (
        host.client.post(PREFIX + "/recall", content=json.dumps(body), headers=CONVERSE).status_code
        == 415
    )
    assert (
        host.client.post(
            PREFIX + "/recall", json=body, headers={**CONVERSE, "Content-Encoding": "gzip"}
        ).status_code
        == 415
    )


@pytest.mark.parametrize("extra", [0, 1])
def test_bridge_request_16kib_boundary_is_inclusive(host, extra):
    raw = json.dumps({**binding(host.session), "seed_ids": [host.node.node_id]}).encode()
    raw += b" " * (16 * 1024 + extra - len(raw))
    response = host.client.post(
        PREFIX + "/recall", content=raw, headers={**CONVERSE, "Content-Type": "application/json"}
    )
    assert response.status_code == (413 if extra else 200)


async def test_bridge_chunked_body_limit(host):
    async def chunks():
        for _ in range(17):
            yield b"x" * 1024

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(host.app), base_url="https://session.invalid"
    ) as client:
        response = await client.post(
            PREFIX + "/recall",
            content=chunks(),
            headers={**CONVERSE, "Content-Type": "application/json"},
        )
    assert response.status_code == 413 and len(response.content) < 150


def test_bridge_rejects_oversized_backend_response_without_echo(host, monkeypatch):
    result = host.memory.recall({**binding(host.session), "seed_ids": [host.node.node_id]})
    oversized = result.model_copy(
        update={"nodes": [result.nodes[0].model_copy(update={"body": "UNTRUSTED-BACKEND" * 2000})]}
    )
    monkeypatch.setattr(host.memory, "recall", lambda _: oversized)
    response = host.client.post(
        PREFIX + "/recall",
        json={**binding(host.session), "seed_ids": [host.node.node_id]},
        headers=CONVERSE,
    )
    assert response.status_code == 503 and "UNTRUSTED-BACKEND" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_bridge_validate_response_has_independent_four_kib_bound(host, monkeypatch):
    result = host.memory.validate_context({**binding(host.session), "pin": host.pin.model_dump()})
    invalid_state = result.state.model_copy(update={"session_id": "UNTRUSTED-STATE" * 500})
    monkeypatch.setattr(
        host.memory,
        "validate_context",
        lambda _: result.model_copy(update={"state": invalid_state}),
    )
    response = host.client.post(
        PREFIX + "/validate",
        json={**binding(host.session), "pin": host.pin.model_dump()},
        headers=CONVERSE,
    )
    assert response.status_code == 503 and "UNTRUSTED-STATE" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_default_and_selected_unavailable_backend_preserve_ordinary_journal(tmp_path, monkeypatch):
    from services.session import memory_api

    store = Store(tmp_path / "ordinary.sqlite")
    auth = Auth(TOKENS)
    absent = create_app(store, auth)
    with TestClient(absent) as client:
        assert client.post(PREFIX + "/recall", json={}, headers=CONVERSE).status_code == 404

    def unavailable(_):
        raise MemoryError(503, "PRIVATE-BACKEND-SENTINEL")

    monkeypatch.setattr(memory_api, "MemoryStore", unavailable)
    app = create_app(store, auth, converse_memory=True)
    try:
        with TestClient(app) as client:
            created = client.post("/v1/sessions", json={"channel": "converse"}, headers=OWNER)
            assert created.status_code == 200 and app.state.converse_memory is None
            scope = {key: created.json()[key] for key in ("session_id", "thread_id", "epoch")}
            for route in ("recall", "validate"):
                body = (
                    {**scope, "seed_ids": ["unknown"]}
                    if route == "recall"
                    else {
                        **scope,
                        "pin": {
                            "schema_version": "1.0",
                            "implementation_id": "korina-dag-v1",
                            "use_generation": 1,
                            "namespace_revision": 0,
                            "closure_sha256": "a" * 64,
                            "selector_ids": ["unknown"],
                        },
                    }
                )
                response = client.post(PREFIX + "/" + route, json=body, headers=CONVERSE)
                assert response.status_code == 503 and "PRIVATE-BACKEND" not in response.text
            saved = client.post(
                "/v1/transcript/entries",
                json={
                    **scope,
                    "kind": "conversation.user",
                    "payload": {"text": "Ordinary public text.", "final": True},
                    "idempotency_key": "ordinary",
                },
                headers=OWNER,
            )
            assert saved.status_code == 200
    finally:
        store.close()


def test_actual_tagged_publication_and_idempotent_receipt_after_disable(host, monkeypatch):
    request = publication(host)
    saved = host.store.append(request, "converse")
    assert saved.origin == "converse" and saved.payload["memory_context"] == host.pin.model_dump()
    host.memory.configure(
        {
            **binding(host.session),
            "expected_revision": host.pin.namespace_revision,
            "enabled": False,
        }
    )

    def forbidden(*args):
        raise AssertionError("Exact receipt replay must not revalidate or read graph")

    monkeypatch.setattr(host.memory, "validate_context", forbidden)
    replay = host.store.append(request, "converse")
    assert replay.event_id == saved.event_id and replay.sequence == saved.sequence
    with pytest.raises(JournalError):
        host.store.append(request.model_copy(update={"idempotency_key": "new-use"}), "converse")
    assert len(host.store.read(session_id=host.session.session_id)) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "2.0"},
        {"schema_version": None},
        {"use_generation": True},
        {"namespace_revision": 1.0},
        {"selector_ids": []},
        {"selector_ids": ["x", "x"]},
        {"closure_sha256": "\ud800"},
        {"extra": "x" * 2048},
    ],
)
def test_reserved_tag_shape_is_strict_before_callback(host, change, monkeypatch):
    def forbidden(*args):
        raise AssertionError("Malformed tag reached validator")

    monkeypatch.setattr(host.memory, "validate_context", forbidden)
    payload = publication(host).payload
    payload["memory_context"] = {**host.pin.model_dump(), **change}
    with pytest.raises(JournalError):
        host.store.append(publication(host, payload=payload), "converse")
    assert len(host.store.read(session_id=host.session.session_id)) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"memory_context": None},
        {"runtime_id": ""},
        {"runtime_id": True},
        {"runtime_id": "bad id"},
        {"content_scope": "public_synthetic"},
        {"content_scope": "private_cloud_authorized"},
        {"final": False},
        {"text": 1},
    ],
)
def test_tagged_publication_requires_native_identity_final_local_scope(host, change):
    payload = {**publication(host).payload, **change}
    with pytest.raises(JournalError):
        host.store.append(publication(host, payload=payload), "converse")


@pytest.mark.parametrize(
    "role,kind",
    [
        ("owner", "conversation.user"),
        ("converse", "conversation.user"),
        ("agent", "agent.state"),
        ("decisions", "routing.decided"),
    ],
)
def test_other_roles_and_kinds_cannot_record_reserved_tag(host, role, kind):
    with pytest.raises((JournalError, PermissionError)):
        host.store.append(publication(host, kind=kind), role)


def test_tagged_agent_channel_wrong_scope_and_unknown_validator_refuse(host):
    other = host.store.create_session(SessionRequest(channel="agent"))
    with pytest.raises(JournalError):
        host.store.append(publication(host, **binding(other), causes=[]), "converse")
    unknown = host.pin.model_copy(update={"implementation_id": "uninstalled-v1"})
    payload = {**publication(host).payload, "memory_context": unknown.model_dump()}
    with pytest.raises(JournalError):
        host.store.append(publication(host, payload=payload), "converse")
    host.store.reset(host.session.session_id)
    with pytest.raises(JournalError):
        host.store.append(publication(host), "converse")


def test_registry_is_bounded_once_per_name_and_requires_sync_callback(host):
    with pytest.raises(ValueError):
        host.store.register_converse_context_validator("korina-dag-v1", lambda *args: True)
    for number in range(3):
        host.store.register_converse_context_validator(f"replacement{number}", lambda *args: True)
    with pytest.raises(ValueError):
        host.store.register_converse_context_validator("fifth", lambda *args: True)


@pytest.mark.parametrize(
    "name,callback",
    [
        (True, lambda *a: True),
        ("bad name", lambda *a: True),
        ("x" * 129, lambda *a: True),
        ("ok", None),
    ],
)
def test_invalid_registry_input_refuses(host, name, callback):
    with pytest.raises(ValueError):
        host.store.register_converse_context_validator(name, callback)


async def test_async_context_validator_is_not_registered(host):
    async def callback(*args):
        return True

    with pytest.raises(ValueError):
        host.store.register_converse_context_validator("async-v1", callback)


@pytest.mark.parametrize("result", [False, None, 1, {}, "true"])
def test_callback_only_literal_true_admits_new_publication(host, result):
    host.store.register_converse_context_validator("replacement-v1", lambda *args: result)
    payload = publication(host).payload
    payload["memory_context"]["implementation_id"] = "replacement-v1"
    with pytest.raises(JournalError):
        host.store.append(publication(host, payload=payload), "converse")


def test_callback_gets_exact_native_scope_and_exception_is_fixed(host):
    seen = []

    def callback(*args):
        seen.append(args)
        raise RuntimeError("PRIVATE-CALLBACK-ERROR")

    host.store.register_converse_context_validator("replacement-v1", callback)
    payload = publication(host).payload
    payload["memory_context"]["implementation_id"] = "replacement-v1"
    with pytest.raises(JournalError) as caught:
        host.store.append(publication(host, payload=payload), "converse")
    assert "PRIVATE-CALLBACK" not in str(caught.value)
    assert seen[0][:3] == tuple(binding(host.session).values())
    assert type(seen[0][3]) is MemoryContextPin
    assert len(host.store.read(session_id=host.session.session_id)) == 1


def test_new_callback_is_after_causal_checks_before_any_append_dml(host):
    seen = []

    def callback(*args):
        seen.append(
            (len(host.store.read(session_id=host.session.session_id)), host.store.db.in_transaction)
        )
        return True

    host.store.register_converse_context_validator("replacement-v1", callback)
    payload = publication(host).payload
    payload["memory_context"]["implementation_id"] = "replacement-v1"
    request = publication(host, payload=payload)
    # Unknown cause refuses before a validator can reconcile or commit anything.
    with pytest.raises(KeyError):
        host.store.append(request.model_copy(update={"causes": ["missing-source"]}), "converse")
    assert seen == []
    saved = host.store.append(request, "converse")
    assert seen == [(1, False)] and saved.sequence > host.source.sequence
    assert len(host.store.read(session_id=host.session.session_id)) == 2


def test_ordinary_append_has_no_backend_import_or_callback(host, monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("services.session.memory"):
            raise AssertionError("Ordinary append imported the optional backend")
        return original(name, *args, **kwargs)

    def forbidden(*args):
        raise AssertionError("Ordinary append called memory validation")

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(host.memory, "validate_context", forbidden)
    payload = {
        key: value for key, value in publication(host).payload.items() if key != "memory_context"
    }
    saved = host.store.append(publication(host, payload=payload), "converse")
    assert saved.payload["text"] == payload["text"]


@pytest.mark.parametrize("reason", ["correction", "forget", "disable", "quarantine"])
def test_new_publication_refusal_preserves_native_invalidation_and_ordinary_journal(host, reason):
    if reason == "correction":
        host.store.append(
            AppendRequest(
                **binding(host.session),
                kind="conversation.revision",
                idempotency_key="correction",
                payload={
                    "target_event_id": host.source.event_id,
                    "text": "Corrected public source.",
                    "revision": 2,
                },
            ),
            "owner",
        )
    elif reason == "forget":
        host.memory.forget(
            {
                **binding(host.session),
                "expected_revision": host.pin.namespace_revision,
                "idempotency_key": "forget",
                "node_id": host.node.node_id,
            }
        )
    elif reason == "disable":
        host.memory.configure(
            {
                **binding(host.session),
                "expected_revision": host.pin.namespace_revision,
                "enabled": False,
            }
        )
    else:
        with host.store.db:
            host.store.db.execute(
                "UPDATE memory_nodes SET body=? WHERE node_id=?", ("x" * 2049, host.node.node_id)
            )
    entries_before = len(host.store.read(session_id=host.session.session_id))
    with pytest.raises(JournalError):
        host.store.append(publication(host), "converse")
    assert len(host.store.read(session_id=host.session.session_id)) == entries_before
    # A separate connection proves that the outer failed append did not roll
    # back reconciliation/quarantine committed inside the validator.
    with sqlite3.connect(host.store.path) as reader:
        state = reader.execute(
            "SELECT state FROM memory_nodes WHERE node_id=?", (host.node.node_id,)
        ).fetchone()[0]
        status, revision = reader.execute(
            "SELECT status,revision FROM memory_namespaces WHERE session_id=?",
            (host.session.session_id,),
        ).fetchone()
    if reason == "correction":
        assert state == "invalidated" and revision > host.pin.namespace_revision
    elif reason == "forget":
        assert state == "forgotten"
    elif reason == "quarantine":
        assert status == "quarantined" and revision > host.pin.namespace_revision
    payload = {"text": "Ordinary fallback remains usable.", "final": True}
    assert host.store.append(publication(host, payload=payload), "converse").payload == payload


def test_false_validation_also_commits_unrelated_source_invalidation(host, monkeypatch):
    other = host.store.append(
        AppendRequest(
            **binding(host.session),
            kind="conversation.user",
            idempotency_key="other-source",
            payload={"text": "Other public source.", "final": True, "revision": 1},
        ),
        "owner",
    )
    saved = host.memory.capture(
        {
            **binding(host.session),
            "expected_revision": host.pin.namespace_revision,
            "idempotency_key": "other-note",
            "kind": "assertion",
            "source_event_id": other.event_id,
            "source_revision": 1,
            "body": "Another public note.",
            "fact": {"key": "other.note", "value": "blue"},
        }
    )
    selected = host.memory.recall({**binding(host.session), "seed_ids": [host.node.node_id]})
    host.pin = host.pin.model_copy(
        update={
            "namespace_revision": selected.state.revision,
            "closure_sha256": selected.closure_sha256,
        }
    )
    host.store.append(
        AppendRequest(
            **binding(host.session),
            kind="conversation.revision",
            idempotency_key="other-correction",
            payload={
                "target_event_id": other.event_id,
                "text": "Other corrected public source.",
                "revision": 2,
            },
        ),
        "owner",
    )
    original, returned = host.memory.validate_context, []

    def record(request):
        result = original(request)
        returned.append(result.valid)
        return result

    monkeypatch.setattr(host.memory, "validate_context", record)
    before = len(host.store.read(session_id=host.session.session_id))
    with pytest.raises(JournalError):
        host.store.append(publication(host), "converse")
    assert returned == [False]  # The selected node survives; its namespace pin changed.
    assert len(host.store.read(session_id=host.session.session_id)) == before
    with sqlite3.connect(host.store.path) as reader:
        assert (
            reader.execute(
                "SELECT state FROM memory_nodes WHERE node_id=?", (saved.node.node_id,)
            ).fetchone()[0]
            == "invalidated"
        )
        assert (
            reader.execute(
                "SELECT revision FROM memory_namespaces WHERE session_id=?",
                (host.session.session_id,),
            ).fetchone()[0]
            > host.pin.namespace_revision
        )
