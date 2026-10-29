"""Owner catalog over the actual Session store and ASGI routes; synthetic data only."""

import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from packages.contracts.memory import MemoryBinding
from packages.contracts.memory_catalog import MemoryCatalogResult
from packages.contracts.models import AppendRequest, SessionRequest, new_id
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import Store

PATH = "/v1/converse-memory/catalog"
TOKENS = {
    role: letter * 32
    for role, letter in zip(
        ("owner", "converse", "agent", "decisions", "resources"), "ocadr", strict=True
    )
}
OWNER = {"Authorization": "Bearer " + TOKENS["owner"]}


def binding(session):
    return {key: getattr(session, key) for key in ("session_id", "thread_id", "epoch")}


@pytest.fixture
def host(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    app = create_app(store, Auth(TOKENS, ("https://ui.invalid",)), converse_memory=True)
    with TestClient(app, base_url="https://session.invalid") as client:
        yield SimpleNamespace(
            store=store,
            session=session,
            app=app,
            memory=app.state.converse_memory,
            client=client,
        )
    store.close()


def test_catalog_native_empty_namespace_does_not_create_rows(host):
    result = host.memory.catalog(MemoryBinding(**binding(host.session)))
    assert result.entries == [] and result.state.node_count == 0
    assert result.state.enabled is False and result.state.revision == 0
    assert host.store.db.execute("SELECT count(*) FROM memory_namespaces").fetchone()[0] == 0


def test_catalog_owner_route_is_complete_and_no_store(host):
    response = host.client.post(PATH, json=binding(host.session), headers=OWNER)
    assert response.status_code == 200
    assert response.json()["entries"] == []
    assert response.json()["state"]["node_count"] == 0
    assert response.headers["cache-control"] == "no-store"


def configure(host, enabled):
    return host.memory.configure(
        {
            **binding(host.session),
            "expected_revision": host.memory.state(binding(host.session)).revision,
            "enabled": enabled,
        }
    )


def source(host):
    return host.store.append(
        AppendRequest(
            **binding(host.session),
            kind="conversation.user",
            payload={"text": "SOURCE-CONTENT-MUST-NOT-BE-LISTED", "final": True},
            idempotency_key=new_id(),
        ),
        "owner",
    )


def capture(host, *, body="A synthetic retained note.", event=None, parents=(), kind="assertion"):
    if not host.memory.state(binding(host.session)).enabled:
        configure(host, True)
    event = event or source(host)
    result = host.memory.capture(
        {
            **binding(host.session),
            "expected_revision": host.memory.state(binding(host.session)).revision,
            "idempotency_key": new_id(),
            "kind": kind,
            "source_event_id": event.event_id,
            "source_revision": 1,
            "body": body,
            "fact": {"key": "SECRET-FACT-KEY", "value": "SECRET-FACT-VALUE"}
            if kind != "inference"
            else None,
            "prerequisite_ids": list(parents),
        }
    )
    return result.node, event


def catalog(host):
    return host.client.post(PATH, json=binding(host.session), headers=OWNER)


@contextmanager
def stored_graph_reads_only(store):
    denied, queries = [], []

    def authorize(action, first, second, database, trigger):
        if action in {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_TRANSACTION,
        } or (action == sqlite3.SQLITE_READ and first == "entries"):
            denied.append((action, first, second))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store.db.set_authorizer(authorize)
    store.db.set_trace_callback(queries.append)
    try:
        yield denied, queries
    finally:
        store.db.set_authorizer(None)
        store.db.set_trace_callback(None)


def test_disabled_catalog_validates_stored_graph_without_source_reads_or_writes(host):
    first, event = capture(host)
    child, _ = capture(host, parents=[first.node_id], kind="inference")
    off = configure(host, False)
    host.store.append(
        AppendRequest(
            **binding(host.session),
            kind="conversation.revision",
            payload={"target_event_id": event.event_id, "revision": 2, "text": "Corrected"},
            idempotency_key=new_id(),
        ),
        "owner",
    )
    changes = host.store.db.total_changes
    with stored_graph_reads_only(host.store) as (denied, queries):
        response = catalog(host)
    assert response.status_code == 200 and denied == []
    result = MemoryCatalogResult.model_validate(response.json())
    assert result.state == off
    assert [row.node_id for row in result.entries] == [first.node_id, child.node_id]
    assert [row.state for row in result.entries] == ["active", "active"]
    assert host.store.db.total_changes == changes
    node_reads = [sql for sql in queries if "FROM memory_nodes WHERE session_id=" in sql]
    assert any("ORDER BY ordinal LIMIT 257" in sql for sql in node_reads)
    assert any("LIMIT 2049" in sql for sql in queries)


def test_enabled_catalog_commits_correction_and_dependent_observation_taint(host):
    first, event = capture(host)
    observed, _ = capture(host, parents=[first.node_id], kind="observation")
    last, _ = capture(host, parents=[observed.node_id], kind="inference")
    old_revision = host.memory.state(binding(host.session)).revision
    host.store.append(
        AppendRequest(
            **binding(host.session),
            kind="conversation.revision",
            payload={"target_event_id": event.event_id, "revision": 2, "text": "Corrected"},
            idempotency_key=new_id(),
        ),
        "owner",
    )
    result = MemoryCatalogResult.model_validate(catalog(host).json())
    assert result.state.revision == old_revision + 1
    assert [row.node_id for row in result.entries] == [
        first.node_id,
        observed.node_id,
        last.node_id,
    ]
    assert [row.state for row in result.entries] == ["invalidated", "tainted", "invalidated"]
    with sqlite3.connect(host.store.path) as other:
        assert [
            row[0] for row in other.execute("SELECT state FROM memory_nodes ORDER BY ordinal")
        ] == [
            "invalidated",
            "tainted",
            "invalidated",
        ]


@pytest.mark.parametrize(
    "body,expected,truncated",
    [
        ("a" * 128, "a" * 128, False),
        ("a" * 129, "a" * 128, True),
        ("😀" * 32, "😀" * 32, False),
        ("a" * 127 + "😀", "a" * 127, True),
        ("é" * 63 + "😀", "é" * 63, True),
        ("\n\t" * 64 + "note", "\n\t" * 64, True),
        ('"\\' * 64 + "end", '"\\' * 64, True),
    ],
)
def test_preview_is_longest_complete_utf8_prefix(host, body, expected, truncated):
    capture(host, body=body)
    result = MemoryCatalogResult.model_validate(catalog(host).json())
    row = result.entries[0]
    assert row.preview == expected and row.preview_truncated is truncated
    assert len(row.preview.encode("utf-8")) <= 128
    if truncated:
        next_char = body[len(expected)]
        assert len((expected + next_char).encode("utf-8")) > 128


def test_summaries_redact_forgotten_closure_and_omit_facts_and_source_contents(host):
    first, event = capture(host, body="BODY-MARKER-ERASED")
    last, _ = capture(host, parents=[first.node_id], kind="inference", body="CHILD-BODY-ERASED")
    visible, _ = capture(host, body="Visible note")
    host.memory.forget(
        {
            **binding(host.session),
            "expected_revision": host.memory.state(binding(host.session)).revision,
            "idempotency_key": new_id(),
            "node_id": first.node_id,
        }
    )
    response = catalog(host)
    result = MemoryCatalogResult.model_validate(response.json())
    assert [row.node_id for row in result.entries] == [first.node_id, last.node_id, visible.node_id]
    assert [(row.state, row.preview, row.preview_truncated) for row in result.entries[:2]] == [
        ("forgotten", None, False),
        ("forgotten", None, False),
    ]
    for sentinel in (
        "BODY-MARKER-ERASED",
        "CHILD-BODY-ERASED",
        "SECRET-FACT-KEY",
        "SECRET-FACT-VALUE",
        "SOURCE-CONTENT-MUST-NOT-BE-LISTED",
        event.event_id,
    ):
        assert sentinel not in response.text
    assert set(response.json()["entries"][2]) == {
        "schema_version",
        "node_id",
        "ordinal",
        "kind",
        "state",
        "preview",
        "preview_truncated",
    }


def test_full_256_catalog_remains_complete_ordered_and_bounded_when_on_and_off(host):
    event = source(host)
    nodes = [capture(host, event=event, body='"\\' * 1024)[0] for _ in range(256)]
    for enabled in (True, False):
        if not enabled:
            configure(host, False)
        response = catalog(host)
        assert response.status_code == 200 and len(response.content) <= 256 * 1024
        result = MemoryCatalogResult.model_validate(response.json())
        assert result.state.node_count == 256 and result.state.enabled is enabled
        assert [row.node_id for row in result.entries] == [node.node_id for node in nodes]
        assert [row.ordinal for row in result.entries] == list(range(1, 257))
        assert all(
            row.preview_truncated and len(row.preview.encode()) == 128 for row in result.entries
        )


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("corruption", ["overflow", "ordinal", "body", "edge", "quarantined"])
def test_corrupt_or_overflowing_graph_refuses_wholly_and_off_never_writes(
    host, enabled, corruption
):
    first, _ = capture(host)
    if not enabled:
        configure(host, False)
    with host.store.lock, host.store.db:
        if corruption == "overflow":
            host.store.db.executemany(
                """INSERT INTO memory_nodes SELECT ?,session_id,thread_id,epoch,?,kind,
                source_event_id,source_revision,source_sequence,body,fact_key,value_json,state
                FROM memory_nodes WHERE node_id=?""",
                [(f"overflow-{i}", i, first.node_id) for i in range(2, 258)],
            )
        elif corruption == "ordinal":
            host.store.db.execute(
                "UPDATE memory_nodes SET ordinal=2 WHERE node_id=?", (first.node_id,)
            )
        elif corruption == "body":
            host.store.db.execute(
                "UPDATE memory_nodes SET body=? WHERE node_id=?", ("x" * 2049, first.node_id)
            )
        elif corruption == "edge":
            host.store.db.execute(
                "INSERT INTO memory_edges VALUES(?,?)", (first.node_id, first.node_id)
            )
        else:
            host.store.db.execute("UPDATE memory_namespaces SET status='quarantined'")
    changes = host.store.db.total_changes
    if enabled:
        response = catalog(host)
        assert host.memory.state(binding(host.session)).status == "quarantined"
    else:
        with stored_graph_reads_only(host.store) as (denied, queries):
            response = catalog(host)
        assert denied == [] and host.store.db.total_changes == changes
        assert not any("count(*) FROM memory_nodes" in sql for sql in queries)
    assert response.status_code == 409 and "entries" not in response.json()
    assert response.json()["detail"] in {
        "Memory catalog unavailable",
        "Memory namespace quarantined",
    }
    assert "A synthetic retained note" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "identity",
    [
        "missing",
        "bad",
        "converse",
        "agent",
        "decisions",
        "resources",
        "duplicate-auth",
        "bad-origin",
        "duplicate-origin",
    ],
)
async def test_catalog_unauthorized_requests_never_receive_a_body(host, identity):
    headers = [(b"authorization", OWNER["Authorization"].encode())]
    status = 403
    if identity == "missing":
        headers, status = [], 401
    elif identity == "bad":
        headers, status = [(b"authorization", b"Bearer wrong-token")], 401
    elif identity in TOKENS:
        headers = [(b"authorization", ("Bearer " + TOKENS[identity]).encode())]
    elif identity == "duplicate-auth":
        headers *= 2
        status = 401
    elif identity == "bad-origin":
        headers.append((b"origin", b"https://unpermitted.invalid"))
    else:
        headers.extend([(b"origin", b"https://ui.invalid"), (b"origin", b"https://other.invalid")])
    messages = []

    async def receive():
        raise AssertionError("Denied catalog request consumed its body")

    async def send(message):
        messages.append(message)

    await host.app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "POST",
            "scheme": "https",
            "path": PATH,
            "raw_path": PATH.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("session.invalid", 443),
            "http_version": "1.1",
        },
        receive,
        send,
    )
    assert (
        next(message["status"] for message in messages if message["type"] == "http.response.start")
        == status
    )


def test_companion_grant_cannot_read_catalog(host):
    issued = host.client.post(
        "/v1/companion/pairings",
        json={**binding(host.session), "label": "Synthetic phone"},
        headers=OWNER,
    )
    assert issued.status_code == 200
    redeemed = host.client.post(
        "/v1/companion/pairings/redeem",
        json={"code": issued.json()["pairing_code"], "device_name": "Synthetic device"},
    )
    assert redeemed.status_code == 200
    response = host.client.post(
        PATH,
        json=binding(host.session),
        headers={
            "Authorization": "Bearer " + redeemed.json()["device_token"],
        },
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "raw,status",
    [
        (b'{"epoch":1,"epoch":2}', 422),
        (b'{"epoch":NaN}', 422),
        (b'{"epoch":Infinity}', 422),
        (b'{"epoch":1e999}', 422),
        (b"\xff", 422),
        (b"[]", 422),
        (b"[" * 1100, 422),
        (b'{"unknown":"SENSITIVE-INPUT"}', 422),
        (b"{}", 422),
    ],
)
def test_catalog_strict_bounded_json_errors_never_echo(host, raw, status):
    response = host.client.post(
        PATH, content=raw, headers={**OWNER, "Content-Type": "application/json"}
    )
    assert response.status_code == status
    assert response.json() == {
        "detail": "Invalid memory JSON"
        if raw
        in (b"\xff", b"[" * 1100, b'{"epoch":1,"epoch":2}', b'{"epoch":NaN}', b'{"epoch":Infinity}')
        else "Invalid memory request"
    }
    assert "SENSITIVE-INPUT" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("extra,status", [(0, 200), (1, 413)])
def test_catalog_request_limit_is_inclusive(host, extra, status):
    raw = json.dumps(binding(host.session)).encode()
    raw += b" " * (16 * 1024 + extra - len(raw))
    response = host.client.post(
        PATH, content=raw, headers={**OWNER, "Content-Type": "application/json"}
    )
    assert response.status_code == status and response.headers["cache-control"] == "no-store"


def test_catalog_transport_and_binding_mismatches_refuse(host):
    assert (
        host.client.post(
            PATH, json=binding(host.session), headers={**OWNER, "Content-Encoding": "gzip"}
        ).status_code
        == 415
    )
    assert (
        host.client.post(
            PATH, content="{}", headers=OWNER | {"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        host.client.post(PATH + "?extra=1", json=binding(host.session), headers=OWNER).status_code
        == 422
    )
    agent = host.store.create_session(SessionRequest(channel="agent"))
    assert host.client.post(PATH, json=binding(agent), headers=OWNER).status_code == 409
    reset = host.store.reset(host.session.session_id)
    assert catalog(host).status_code == 409
    response = host.client.post(PATH, json=binding(reset), headers=OWNER)
    assert response.status_code == 200 and response.json()["entries"] == []


@pytest.mark.parametrize("extra,status", [(0, 200), (1, 503)])
def test_catalog_response_limit_is_inclusive_and_fixed_on_overflow(
    host, monkeypatch, extra, status
):
    result = host.memory.catalog(binding(host.session))
    raw = result.model_dump_json().encode()
    raw += b" " * (256 * 1024 + extra - len(raw))
    # Return a complete JSON body with padding to exercise the real ASGI
    # transport guard independently of the stricter native catalog DTO bound.
    monkeypatch.setattr(
        host.memory, "catalog", lambda _: Response(raw, media_type="application/json")
    )
    response = catalog(host)
    assert response.status_code == status
    if extra:
        assert response.json() == {"detail": "Memory response exceeds its bound"}
    else:
        assert len(response.content) == 256 * 1024
        assert response.json() == result.model_dump(mode="json")
    assert response.headers["cache-control"] == "no-store"


def test_catalog_absent_without_opt_in_preserves_ordinary_journal(tmp_path):
    store = Store(tmp_path / "ordinary.sqlite")
    try:
        app = create_app(store, Auth(TOKENS))
        with TestClient(app) as client:
            assert client.post(PATH, json={}, headers=OWNER).status_code == 404
            assert client.get("/health").json()["status"] == "ready"
            assert client.post("/v1/sessions", json={}, headers=OWNER).status_code == 200
        assert not hasattr(app.state, "converse_memory")
        assert (
            store.db.execute("SELECT name FROM sqlite_master WHERE name='memory_nodes'").fetchone()
            is None
        )
    finally:
        store.close()


def test_catalog_unavailable_backend_is_fixed_and_ordinary_journal_survives(tmp_path, monkeypatch):
    from services.session import memory_api
    from services.session.memory import MemoryError

    def unavailable(_):
        raise MemoryError(503, "PRIVATE-STORAGE-MARKER")

    monkeypatch.setattr(memory_api, "MemoryStore", unavailable)
    store = Store(tmp_path / "unavailable.sqlite")
    try:
        app = create_app(store, Auth(TOKENS), converse_memory=True)
        with TestClient(app) as client:
            created = client.post("/v1/sessions", json={}, headers=OWNER)
            assert created.status_code == 200
            response = client.post(
                PATH,
                json={key: created.json()[key] for key in ("session_id", "thread_id", "epoch")},
                headers=OWNER,
            )
            assert response.status_code == 503 and response.json() == {
                "detail": "Converse memory is unavailable"
            }
            assert client.get("/health").json()["status"] == "ready"
        assert app.state.converse_memory is None
    finally:
        store.close()
