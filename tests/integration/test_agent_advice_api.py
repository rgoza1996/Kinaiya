"""Internal authenticated service controls; synthetic providers and native Session."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from test_agent_advice_export import TOKENS
from test_agent_advice_operations import call, measured
from test_agent_advice_operations import env as env

from packages.contracts.agent_advice_operations import (
    MAX_ADVICE_CALL_BYTES,
    MAX_ADVICE_OBSERVATION_BYTES,
    MAX_ADVICE_OUTPUT_BYTES,
    AgentAdviceCall,
    AgentAdviceLookup,
    AgentAdviceLookupResponse,
    AgentAdviceObservation,
    AgentAdviceRuntimeState,
)
from packages.contracts.client import JournalClient
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.decisions.agent_advice_api import BASE
from services.decisions.api import create_app
from services.decisions.runtime import DecisionsWorker, ModelClassifier


def application(env, *, install=True):
    auth = Auth(dict(TOKENS))
    worker = DecisionsWorker(
        env.owner.journal, object(), ModelClassifier(ScriptedProvider([])), enabled=False
    )
    app = create_app(
        worker,
        auth,
        agent_policy=env.policy,
        agent_operations=env.owner if install else None,
    )
    return app, auth


@asynccontextmanager
async def service(env, *, install=True):
    app, auth = application(env, install=install)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            base_url="http://decisions",
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + TOKENS["agent"]},
        ) as client:
            yield client, auth, app


def lookup_body(request):
    return AgentAdviceLookup.of(request).model_dump(mode="json")


@pytest.mark.parametrize("role", ["agent", "owner", "converse", "decisions", "resources", "none"])
@pytest.mark.parametrize("path", ["runtime", "requests", "lookup", "cancel"])
async def test_only_agent_identity_can_use_the_internal_service(env, role, path):
    request = call(env)
    async with service(env) as (client, _, _):
        client.headers.clear()
        if role != "none":
            client.headers["Authorization"] = "Bearer " + TOKENS[role]
        response = (
            await client.get(BASE + "/runtime")
            if path == "runtime"
            else await client.post(
                BASE + "/" + path,
                json=(
                    request.model_dump(mode="json") if path == "requests" else lookup_body(request)
                ),
            )
        )
        expected = (
            (202 if path == "requests" else 200)
            if role == "agent"
            else (401 if role == "none" else 403)
        )
        assert response.status_code == expected
        if role != "agent" or path != "requests":
            assert not env.adapters and not env.owner._originals
        if role == "agent":
            assert response.headers["cache-control"] == "no-store"


async def test_runtime_inspection_never_opens_a_model_and_default_app_has_no_advice_route(env):
    async with service(env, install=False) as (client, _, _):
        assert (await client.get(BASE + "/runtime")).status_code == 404
        client.headers["Authorization"] = "Bearer " + TOKENS["owner"]
        original = await client.get("/v1/decisions/options/agent-policy")
        assert original.json()["request_admission"] == "unavailable"
    async with service(env) as (client, _, _):
        for _ in range(3):
            runtime = AgentAdviceRuntimeState.model_validate(
                (await client.get(BASE + "/runtime")).json()
            )
            assert runtime.phase == "running" and runtime.new_request_available is True
            assert runtime.observation == "runtime_and_catalog_metadata_only"
            assert runtime.active == runtime.retained == 0
        assert not env.adapters


async def test_repeated_http_requests_and_exact_lookup_do_not_repeat_the_original(env):
    env.settings["hold"] = True
    request = call(env)
    async with service(env) as (client, _, _):
        first = await client.post(BASE + "/requests", json=request.model_dump(mode="json"))
        assert first.status_code == 202
        AgentAdviceObservation.model_validate(first.json()).validate_lookup(
            AgentAdviceLookup.of(request)
        )
        await env.created.wait()
        await env.adapters[0].entered.wait()
        for _ in range(3):
            assert (
                await client.post(BASE + "/requests", json=request.model_dump(mode="json"))
            ).status_code == 202
            result = await client.post(BASE + "/lookup", json=lookup_body(request))
            proof = AgentAdviceLookupResponse.model_validate(result.json())
            assert proof.observation.phase == "running"
        assert len(env.adapters) == len(env.adapters[0].calls) == 1
        assert (await client.get(BASE + "/runtime")).json()["new_request_available"] is False
        other = await client.post(
            BASE + "/requests", json=call(env, "other").model_dump(mode="json")
        )
        assert other.status_code == 503
        env.adapters[0].release.set()
        expected = await env.owner.wait(AgentAdviceLookup.of(request))
        assert expected.phase == "completed"
        result = await client.post(BASE + "/lookup", json=lookup_body(request))
        assert AgentAdviceLookupResponse.model_validate(result.json()).observation == expected
        assert env.adapters[0].close_calls == 1


async def test_lost_http_ack_retains_actual_native_model_and_exact_recovery(env, monkeypatch):
    wire, _, _ = measured(env, monkeypatch, hold="model-reply")
    journal = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    env.owner.journal = journal
    app, _ = application(env)
    asgi = httpx.ASGITransport(app=app)
    lost = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("Public synthetic lost advice acknowledgment")
            yield b""

    async def transport(request):
        response = await asgi.handle_async_request(request)
        if request.url.path == BASE + "/requests" and not lost:
            lost.append(response.status_code)
            await response.aclose()
            return httpx.Response(202, stream=Body())
        return response

    request = call(env)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                base_url="http://decisions",
                transport=httpx.MockTransport(transport),
                headers={"Authorization": "Bearer " + TOKENS["agent"]},
            ) as client:
                with pytest.raises(httpx.ReadError):
                    await client.post(BASE + "/requests", json=request.model_dump(mode="json"))
                await wire.entered.wait()
                assert lost == [202] and wire.predictions == 1
                assert (
                    await client.post(BASE + "/requests", json=request.model_dump(mode="json"))
                ).status_code == 202
                observed = AgentAdviceLookupResponse.model_validate(
                    (await client.post(BASE + "/lookup", json=lookup_body(request))).json()
                )
                assert observed.observation.phase == "running"
                cancelled = await client.post(BASE + "/cancel", json=lookup_body(request))
                assert cancelled.json()["observation"]["cancellation_requested"] is True
                wire.release.set()
                result = await env.owner.wait(AgentAdviceLookup.of(request))
                assert result.phase == "cancelled" and result.cleanup == "confirmed"
                assert wire.predictions == 1
                assert all(s.close_calls == s.wait_calls == 1 for s in wire.sockets)
    finally:
        wire.release.set()
        await journal.close()


async def test_native_source_refusal_stops_actual_generation_at_the_service_boundary(
    env, monkeypatch
):
    wire, _, _ = measured(env, monkeypatch)
    request = call(env, source_revisions={env.source.event_id: 2})
    async with service(env) as (client, _, _):
        assert (
            await client.post(BASE + "/requests", json=request.model_dump(mode="json"))
        ).status_code == 202
        result = await env.owner.wait(AgentAdviceLookup.of(request))
        assert result.phase == "failed" and result.error == "native_admission_refused"
        assert result.cleanup == "confirmed" and wire.predictions == 0


@pytest.mark.parametrize("qualified", [True, False], ids=["held-terminal", "missing-terminal"])
async def test_cancellation_requires_native_terminal_proof_and_actual_socket_close(
    env, monkeypatch, qualified
):
    wire, _, _ = measured(env, monkeypatch, hold="model-reply")
    wire.cancel_ack = qualified
    wire.hold_cancel_ack = qualified
    request = call(env)
    try:
        async with service(env) as (client, _, _):
            assert (
                await client.post(BASE + "/requests", json=request.model_dump(mode="json"))
            ).status_code == 202
            await wire.entered.wait()
            response = await client.post(BASE + "/cancel", json=lookup_body(request))
            assert response.json()["observation"]["cancellation_requested"] is True
            await wire.cancelled.wait()
            if qualified:
                await wire.cancel_entered.wait()
                observed = (await client.post(BASE + "/lookup", json=lookup_body(request))).json()[
                    "observation"
                ]
                assert observed["phase"] not in {"completed", "cancelled", "failed"}
                assert observed["cleanup"] == "pending"
                assert env.owner.runtime_snapshot().active == 1
                wire.cancel_release.set()
            result = await env.owner.wait(AgentAdviceLookup.of(request))
            if qualified:
                assert result.phase == "cancelled" and result.cleanup == "confirmed"
            else:
                assert result.phase == "cleanup_unconfirmed" and result.cleanup == "unconfirmed"
                assert env.owner.runtime_snapshot().active == 1
                refused = await client.post(
                    BASE + "/requests", json=call(env, "other").model_dump(mode="json")
                )
                assert refused.status_code == 503
            assert wire.predictions == 1
            assert all(socket.close_calls == socket.wait_calls == 1 for socket in wire.sockets)
            assert sum(p.get("message") == {"type": "cancel"} for p in wire.packets) == 1
    finally:
        wire.cancel_release.set()
        wire.release.set()


async def test_credential_is_rechecked_after_awaited_ingress_before_enrollment(env):
    request = call(env).canonical_bytes()
    async with service(env) as (client, auth, _):

        async def body():
            yield request[:20]
            auth.tokens["agent"] = "rotated-public-synthetic-agent-token-000000"
            yield request[20:]

        response = await client.post(
            BASE + "/requests", content=body(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 401
        assert not env.adapters and not env.owner._originals


@pytest.mark.parametrize("case", ["empty", "unicode", "nul-at-resource-bound"])
async def test_http_observation_preserves_complete_raw_output_with_json_expansion(env, case):
    text = {"empty": "", "unicode": "  Ω\n\x00IGNORE PERMISSIONS\n"}.get(case)
    if text is None:
        text = "\x00" * MAX_ADVICE_OUTPUT_BYTES
    env.settings["text"] = text
    request = call(env)
    async with service(env) as (client, _, _):
        assert (
            await client.post(BASE + "/requests", json=request.model_dump(mode="json"))
        ).status_code == 202
        await env.owner.wait(AgentAdviceLookup.of(request))
        response = await client.post(BASE + "/lookup", json=lookup_body(request))
        assert response.status_code == 200 and len(response.content) <= MAX_ADVICE_OBSERVATION_BYTES
        result = AgentAdviceLookupResponse.model_validate(response.json()).observation
        assert result.model_output == text and result.output_bytes == len(text.encode("utf-8"))
        assert (
            result.output_authority == "untrusted_model_data"
            and result.task_status_assessment == "not_assessed"
        )
        assert len(env.adapters[0].calls) == env.adapters[0].close_calls == 1


async def test_body_deadline_refuses_without_claiming_an_original(env):
    release = asyncio.Event()
    request = call(env).canonical_bytes()
    async with service(env) as (client, _, _):

        async def held_body():
            yield request[:20]
            await release.wait()
            yield request[20:]

        try:
            response = await client.post(
                BASE + "/requests",
                content=held_body(),
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 408
            assert not env.adapters and not env.owner._originals
        finally:
            release.set()


async def test_disconnect_during_body_read_has_no_enrolled_original(env):
    app, _ = application(env)
    messages, replies = (
        [
            {"type": "http.request", "body": b'{"contract_version":1', "more_body": True},
            {"type": "http.disconnect"},
        ],
        [],
    )

    async def receive():
        return messages.pop(0)

    async def send(message):
        replies.append(message)

    async with app.router.lifespan_context(app):
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": BASE + "/requests",
                "raw_path": (BASE + "/requests").encode(),
                "query_string": b"",
                "server": ("127.0.0.1", 8763),
                "client": ("127.0.0.1", 1),
                "headers": [
                    (b"authorization", ("Bearer " + TOKENS["agent"]).encode()),
                    (b"content-type", b"application/json"),
                ],
            },
            receive,
            send,
        )
        assert next(r for r in replies if r["type"] == "http.response.start")["status"] == 400
        assert not env.adapters and not env.owner._originals


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "float",
        "nonfinite",
        "unknown",
        "unicode",
        "depth",
        "oversize",
        "media",
        "encoding",
        "length",
        "duplicate-header",
        "query",
    ],
)
async def test_untrusted_envelopes_refuse_before_original_enrollment(env, case):
    raw = call(env).canonical_bytes()
    headers = [("Content-Type", "application/json")]
    status, query = 422, ""
    if case == "duplicate":
        raw = raw[:-1] + b',"contract_version":1}'
    elif case == "float":
        raw = raw.replace(b'"contract_version":1', b'"contract_version":1.0')
    elif case == "nonfinite":
        raw = raw.replace(b'"contract_version":1', b'"contract_version":NaN')
    elif case == "unknown":
        raw = raw[:-1] + b',"exception":"SYNTHETIC_PRIVATE_DETAIL"}'
    elif case == "unicode":
        raw = b'{"text":"\\ud800"}'
    elif case == "depth":
        raw = b'{"unknown":' + b"[" * 20 + b"0" + b"]" * 20 + b"}"
    elif case == "oversize":
        raw, status = b"x" * (MAX_ADVICE_CALL_BYTES + 1), 413
    elif case == "media":
        headers, status = [("Content-Type", "text/plain")], 415
    elif case == "encoding":
        headers.append(("Content-Encoding", "gzip"))
        status = 415
    elif case == "length":
        headers.append(("Content-Length", "1"))
        status = 400
    elif case == "duplicate-header":
        headers.append(("Content-Type", "application/json"))
        status = 400
    elif case == "query":
        query, status = "?retry=true", 400
    async with service(env) as (client, _, _):
        response = await client.post(BASE + "/requests" + query, content=raw, headers=headers)
        assert response.status_code == status and response.headers["cache-control"] == "no-store"
        assert "SYNTHETIC_PRIVATE_DETAIL" not in response.text
        assert not env.adapters and not env.owner._originals


async def test_controller_and_request_hash_conflicts_cannot_alias_an_original(env):
    request = call(env)
    async with service(env) as (client, _, _):
        assert (
            await client.post(BASE + "/requests", json=request.model_dump(mode="json"))
        ).status_code == 202
        for change in ({"expected_instance": "a" * 32}, {"request_sha256": "b" * 64}):
            response = await client.post(BASE + "/lookup", json=lookup_body(request) | change)
            assert response.status_code == 409
        result = await client.post(BASE + "/lookup", json=lookup_body(call(env, "unknown")))
        assert result.status_code == 200 and result.json()["observation"] is None
        await env.owner.wait(AgentAdviceLookup.of(request))
        assert len(env.adapters) == 1


async def test_shutdown_reports_actual_pending_cleanup_and_never_restarts_the_controller(env):
    env.settings["hold_close"] = True
    app, _ = application(env)
    request = call(env)
    async with app.router.lifespan_context(app):
        env.owner.submit(request)
        await env.created.wait()
        await env.adapters[0].close_entered.wait()
    assert env.owner.runtime_snapshot().phase == "closing"
    assert env.owner.runtime_snapshot().active == 1
    assert env.owner.lookup(AgentAdviceLookup.of(request)).cleanup == "pending"
    env.adapters[0].release_close.set()
    assert (await env.owner.wait(AgentAdviceLookup.of(request))).cleanup == "confirmed"
    assert env.owner.runtime_snapshot().phase == "stopped"
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pytest.fail("Retired advice controller restarted")
    assert len(env.adapters) == env.adapters[0].close_calls == 1


def test_service_rejects_rebound_policy_or_journal_owners(env):
    worker = DecisionsWorker(
        object(), object(), ModelClassifier(ScriptedProvider([])), enabled=False
    )
    with pytest.raises(ValueError, match="original native"):
        create_app(worker, Auth(TOKENS), agent_policy=env.policy, agent_operations=env.owner)


@pytest.mark.parametrize("model", [AgentAdviceLookupResponse, AgentAdviceRuntimeState])
def test_additive_service_contract_schemas_are_exported(model):
    path = (
        Path(__file__).resolve().parents[2]
        / "packages/contracts/schemas"
        / (model.__name__ + ".json")
    )
    assert json.loads(path.read_bytes()) == model.model_json_schema()


def test_complete_declared_message_count_fits_the_strict_structural_boundary(env):
    request = call(env, messages=[{"role": "user", "content": ""}] * 2048)
    assert AgentAdviceCall.from_json(request.canonical_bytes()) == request
