"""Real owner ASGI boundary and journal compatibility, using synthetic data only."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from packages.contracts.models import AppendRequest, SessionRequest, new_id
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import Store

PREFIX = "/v1/converse-memory"
OWNER = {"Authorization": "Bearer " + "o" * 32}


def binding(session):
    return {k: getattr(session, k) for k in ("session_id", "thread_id", "epoch")}


def source(store, session, text="A synthetic preference."):
    return store.append(
        AppendRequest(
            **binding(session),
            kind="conversation.user",
            idempotency_key=new_id(),
            payload={"text": text, "final": True, "content_scope": "public_synthetic"},
        ),
        "owner",
    )


@pytest.fixture
def host(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    auth = Auth(
        {
            role: letter * 32
            for role, letter in zip(
                ("owner", "agent", "converse", "decisions", "resources"), "oacdr", strict=True
            )
        },
        ("https://ui.invalid",),
    )
    app = create_app(store, auth, converse_memory=True)
    with TestClient(app, base_url="https://host.invalid") as client:
        yield SimpleNamespace(store=store, session=session, auth=auth, app=app, client=client)
    store.close()


def post(host, route, **fields):
    return host.client.post(PREFIX + route, json={**binding(host.session), **fields}, headers=OWNER)


def capture(host, event, revision, *, key="fixture", value="tea", parents=(), kind="assertion"):
    return post(
        host,
        "/capture",
        expected_revision=revision,
        idempotency_key=key,
        kind=kind,
        source_event_id=event.event_id,
        source_revision=1,
        body="Synthetic owner-curated context.",
        fact={"key": "person:fixture/preference", "value": value} if kind != "inference" else None,
        prerequisite_ids=list(parents),
    )


def test_default_app_has_no_memory_routes_or_dependency(tmp_path):
    store = Store(tmp_path / "ordinary.sqlite")
    auth = Auth({"owner": "o" * 32})
    app = create_app(store, auth)
    assert not hasattr(app.state, "converse_memory")
    with TestClient(app) as client:
        assert client.post(PREFIX + "/state", json={}, headers=OWNER).status_code == 404
        assert client.get("/health").json()["status"] == "ready"
        created = client.post("/v1/sessions", json={}, headers=OWNER)
        assert created.status_code == 200
    assert store.db.execute("SELECT version FROM schema_meta").fetchone()[0] == 1
    store.close()


def test_selected_unavailable_memory_does_not_disable_ordinary_journal(tmp_path, monkeypatch):
    from services.session import memory_api
    from services.session.memory import MemoryError

    def unavailable(_):
        raise MemoryError(503, "PRIVATE-STORAGE-FIXTURE")

    monkeypatch.setattr(memory_api, "MemoryStore", unavailable)
    store = Store(tmp_path / "fallback.sqlite")
    auth = Auth({"owner": "o" * 32})
    app = create_app(store, auth, converse_memory=True)
    assert app.state.converse_memory is None
    with TestClient(app) as client:
        session = client.post("/v1/sessions", json={}, headers=OWNER)
        assert session.status_code == 200
        response = client.post(
            PREFIX + "/state",
            json={k: session.json()[k] for k in ("session_id", "thread_id", "epoch")},
            headers=OWNER,
        )
        assert response.status_code == 503 and "PRIVATE-STORAGE-FIXTURE" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/health").json()["status"] == "ready"
        assert (
            client.get("/v1/sessions", headers=OWNER).json()[0]["session_id"]
            == session.json()["session_id"]
        )
    store.close()


@pytest.mark.parametrize(
    "headers, status",
    [
        ([], 401),
        ([(b"authorization", b"Bearer " + b"a" * 32)], 403),
        (
            [
                (b"authorization", b"Bearer " + b"o" * 32),
                (b"origin", b"https://unpermitted.invalid"),
            ],
            403,
        ),
    ],
)
async def test_rejected_identity_never_waits_for_or_receives_body(host, headers, status):
    response = []

    async def receive():
        raise AssertionError("An unauthorized request must not reach body receive")

    async def send(message):
        response.append(message)

    path = PREFIX + "/capture"
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
            "server": ("host.invalid", 443),
            "client": ("127.0.0.1", 12345),
        },
        receive,
        send,
    )
    assert response[0]["type"] == "http.response.start" and response[0]["status"] == status
    assert (b"cache-control", b"no-store") in response[0]["headers"]


def test_owner_flow_off_state_correction_and_journal_independence(host):
    state = post(host, "/state")
    assert state.status_code == 200, state.text
    assert state.headers["cache-control"] == "no-store"
    assert state.json()["enabled"] is False and state.json()["revision"] == 0
    event = source(host.store, host.session)
    assert capture(host, event, 0).status_code == 409
    enabled = post(host, "/configure", expected_revision=0, enabled=True)
    assert enabled.status_code == 200, enabled.text
    first = capture(host, event, enabled.json()["revision"])
    assert first.status_code == 200, first.text
    node = first.json()["node"]
    assert node["content_scope"] == "local_only"
    assert node["provenance"] == "owner_curated"
    revision = first.json()["state"]["revision"]
    query = post(host, "/query", seed_ids=[node["node_id"]])
    assert query.status_code == 200 and query.json()["nodes"] == [node]
    assert post(host, "/configure", expected_revision=revision, enabled=False).status_code == 200
    inspected = post(host, "/inspect", node_id=node["node_id"])
    assert inspected.status_code == 200 and inspected.json()["node"]["body"]
    assert post(host, "/query", seed_ids=[node["node_id"]]).status_code == 409
    correction = host.store.append(
        AppendRequest(
            **binding(host.session),
            kind="conversation.revision",
            idempotency_key=new_id(),
            payload={
                "target_event_id": event.event_id,
                "text": "A corrected fixture.",
                "revision": 2,
            },
        ),
        "owner",
    )
    # Disabled owner inspection is administrative; it does not process the correction.
    retained = post(host, "/inspect", node_id=node["node_id"]).json()
    assert retained["node"]["state"] == "active"
    reenabled = post(
        host, "/configure", expected_revision=retained["state"]["revision"], enabled=True
    )
    assert reenabled.status_code == 409
    current = post(host, "/state").json()
    assert current["revision"] > retained["state"]["revision"]
    assert current["enabled"] is False
    reenabled = post(host, "/configure", expected_revision=current["revision"], enabled=True)
    assert reenabled.status_code == 200, reenabled.text
    assert post(host, "/query", seed_ids=[node["node_id"]]).status_code == 409
    invalid = post(host, "/inspect", node_id=node["node_id"]).json()
    assert invalid["node"]["state"] == "invalidated"
    forgotten = post(
        host,
        "/forget",
        expected_revision=invalid["state"]["revision"],
        idempotency_key="forget-fixture",
        node_id=node["node_id"],
    )
    assert forgotten.status_code == 200, forgotten.text
    assert forgotten.json()["forgotten_ids"] == [node["node_id"]]
    redacted = post(host, "/inspect", node_id=node["node_id"]).json()["node"]
    assert (
        redacted["state"] == "forgotten" and redacted["body"] is None and redacted["fact"] is None
    )
    # Memory enable/disable/forget never rewrites or deletes the authoritative history.
    assert host.store.entry(event.event_id).payload["text"] == "A synthetic preference."
    assert host.store.entry(correction.event_id).payload["text"] == "A corrected fixture."
    assert host.store.resolve(event.event_id).payload["revision"] == 2
    assert len(host.store.read(session_id=host.session.session_id)) == 2


@pytest.mark.parametrize("route", ("state", "configure", "capture", "inspect", "query", "forget"))
def test_every_route_requires_owner_and_origin(host, route):
    url = PREFIX + "/" + route
    body = binding(host.session)
    assert host.client.post(url, json=body).status_code == 401
    for role in ("agent", "converse", "decisions", "resources"):
        response = host.client.post(
            url, json=body, headers={"Authorization": "Bearer " + host.auth.tokens[role]}
        )
        assert response.status_code == 403, (route, role, response.text)
        assert response.headers["cache-control"] == "no-store"
    assert (
        host.client.post(
            url, json=body, headers={**OWNER, "Origin": "https://unpermitted.invalid"}
        ).status_code
        == 403
    )


def test_scoped_companion_bearer_cannot_read_or_change_memory(host):
    pairing = host.client.post(
        "/v1/companion/pairings",
        json={**binding(host.session), "label": "Synthetic device"},
        headers=OWNER,
    )
    assert pairing.status_code == 200
    issued = host.client.post(
        "/v1/companion/pairings/redeem",
        json={"code": pairing.json()["pairing_code"], "device_name": "Public fixture"},
    )
    assert issued.status_code == 200
    for route in ("state", "configure", "capture", "inspect", "query", "forget"):
        response = host.client.post(
            PREFIX + "/" + route,
            json=binding(host.session),
            headers={"Authorization": "Bearer " + issued.json()["device_token"]},
        )
        assert response.status_code == 401


@pytest.mark.parametrize(
    "raw,status",
    [
        (b'{"session_id":"secret-one","session_id":"secret-two"}', 422),
        (b'{"epoch":NaN}', 422),
        (b'{"epoch":Infinity}', 422),
        (b"\xff", 422),
        (b"[" * 1100, 422),
        (b'"' + b"x" * 16384 + b'"', 413),
        (b'{"untrusted_body":"SENSITIVE-FIXTURE"}', 422),
    ],
)
def test_wire_errors_are_bounded_and_never_echo_input(host, raw, status):
    response = host.client.post(
        PREFIX + "/capture", content=raw, headers={**OWNER, "Content-Type": "application/json"}
    )
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) < 150
    assert "secret-" not in response.text and "SENSITIVE-FIXTURE" not in response.text


def test_headers_parameters_and_content_types_fail_closed(host):
    raw = json.dumps(binding(host.session))
    assert host.client.post(PREFIX + "/state", content=raw, headers=OWNER).status_code == 415
    for suffix in ("?unknown=x", "?epoch=1&epoch=1", "?bearer=synthetic"):
        response = host.client.post(
            PREFIX + "/state" + suffix, json=binding(host.session), headers=OWNER
        )
        assert response.status_code == 422
    duplicate = [("authorization", OWNER["Authorization"])] * 2
    assert (
        host.client.post(
            PREFIX + "/state", json=binding(host.session), headers=duplicate
        ).status_code
        == 401
    )


async def test_chunked_request_limit_is_enforced_before_json_decoding(host):
    async def chunks():
        for _ in range(20):
            yield b"x" * 1024

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app), base_url="https://host.invalid"
    ) as client:
        response = await client.post(
            PREFIX + "/capture",
            content=chunks(),
            headers={**OWNER, "Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert post(host, "/state").json()["node_count"] == 0


def test_current_namespace_reset_and_delete_cascade(host):
    event = source(host.store, host.session)
    enabled = post(host, "/configure", expected_revision=0, enabled=True).json()
    saved = capture(host, event, enabled["revision"]).json()
    assert saved["node"]["body"]
    old = binding(host.session)
    host.session = host.store.reset(host.session.session_id)
    assert host.client.post(PREFIX + "/state", json=old, headers=OWNER).status_code == 409
    current = post(host, "/state").json()
    assert current["enabled"] is False and current["node_count"] == 0
    assert post(host, "/inspect", node_id=saved["node"]["node_id"]).status_code in (404, 409)
    host.store.delete(host.session.session_id)
    assert post(host, "/state").status_code == 404
    assert not host.store.db.execute("PRAGMA foreign_key_check").fetchall()


def test_launcher_opt_in_only_reaches_session_child(tmp_path, monkeypatch):
    from services import launcher

    monkeypatch.setattr(launcher, "load_config", lambda _: {"origins": []})
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: {"owner": "o" * 32})
    captured = []
    monkeypatch.setattr(launcher, "supervise", lambda commands: captured.append(commands))
    for enabled in (False, True):
        launcher.serve(
            SimpleNamespace(
                service="all",
                data_dir=tmp_path,
                config=tmp_path / "config.json",
                workspace=tmp_path,
                converse_memory=enabled,
            )
        )
    for commands, enabled in zip(captured, (False, True), strict=True):
        assert ("--converse-memory" in commands["session"]) is enabled
        assert all(
            "--converse-memory" not in command
            for name, command in commands.items()
            if name != "session"
        )
