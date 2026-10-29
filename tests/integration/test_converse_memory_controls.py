"""Converse owner controls remain bounded and independent of a memory backend."""

import httpx
import pytest

from packages.contracts.models import SessionRequest
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store

TOKENS = {
    role: char * 32
    for role, char in zip(
        ("owner", "converse", "agent", "decisions", "resources"), "ocadr", strict=True
    )
}


@pytest.fixture
async def control(tmp_path):
    store = Store(tmp_path / "session.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"), ScriptedProvider(["Synthetic reply."])
    )
    app = create_app(runtime, Auth(TOKENS, ("https://ui.invalid",)))
    binding = {key: getattr(session, key) for key in ("session_id", "thread_id", "epoch")}
    try:
        yield app, runtime, binding
    finally:
        await runtime.close()
        store.close()


async def test_owner_roundtrip_and_cas_without_dag_backend(control):
    app, runtime, binding = control
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKENS["owner"]},
    ) as client:
        initial = await client.post("/v1/memory/state", json=binding)
        assert initial.status_code == 200 and initial.json()["enabled"] is False
        assert (
            initial.json()["use_generation"] == 0 and initial.headers["cache-control"] == "no-store"
        )
        enabled = await client.post(
            "/v1/memory/use",
            json={
                **binding,
                "expected_runtime_id": initial.json()["runtime_id"],
                "expected_generation": 0,
                "enabled": True,
                "implementation_id": "unavailable-v1",
                "selector_ids": ["node"],
            },
        )
        assert enabled.status_code == 200 and enabled.json()["notice"] == "unavailable"
        assert enabled.json()["enabled"] and not enabled.json()["pending"]
        stale = await client.post(
            "/v1/memory/use",
            json={
                **binding,
                "expected_runtime_id": initial.json()["runtime_id"],
                "expected_generation": 0,
                "enabled": False,
            },
        )
        assert stale.status_code == 409
        disabled = await client.post(
            "/v1/memory/use",
            json={
                **binding,
                "expected_runtime_id": initial.json()["runtime_id"],
                "expected_generation": enabled.json()["use_generation"],
                "enabled": False,
            },
        )
        assert disabled.status_code == 200 and not disabled.json()["enabled"]
        assert runtime.enabled and runtime.memory.adapters == {}


@pytest.mark.parametrize(
    "headers,status",
    [
        ([], 401),
        ([(b"authorization", ("Bearer " + TOKENS["agent"]).encode())], 403),
        ([(b"authorization", ("Bearer " + TOKENS["resources"]).encode())], 403),
        ([(b"authorization", ("Bearer " + TOKENS["decisions"]).encode())], 403),
        ([(b"authorization", ("Bearer " + TOKENS["owner"]).encode())] * 2, 401),
        (
            [
                (b"authorization", ("Bearer " + TOKENS["owner"]).encode()),
                (b"origin", b"https://ui.invalid"),
                (b"origin", b"https://foreign.invalid"),
            ],
            401,
        ),
    ],
)
async def test_denial_does_not_receive_any_body(control, headers, status):
    app, _, _ = control
    output = []

    async def receive():
        pytest.fail("Denied memory control received its body")

    async def send(message):
        output.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/memory/use",
            "raw_path": b"/v1/memory/use",
            "query_string": b"",
            "headers": headers,
            "root_path": "",
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 1),
        },
        receive,
        send,
    )
    assert output[0]["status"] == status


@pytest.mark.parametrize(
    "path,headers,body,status",
    [
        ("/v1/memory/state?unexpected=1", {}, b"{}", 422),
        ("/v1/memory/state", {"Content-Encoding": "gzip"}, b"{}", 415),
        ("/v1/memory/state", {}, b'{"secret":"synthetic-do-not-echo"}', 422),
    ],
)
async def test_fixed_errors_do_not_echo_payload(control, path, headers, body, status):
    app, _, _ = control
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            path,
            content=body,
            headers={
                "Authorization": "Bearer " + TOKENS["owner"],
                "Content-Type": "application/json",
                **headers,
            },
        )
    assert response.status_code == status and len(response.content) < 200
    assert b"synthetic-do-not-echo" not in response.content
    assert response.headers["cache-control"] == "no-store"
