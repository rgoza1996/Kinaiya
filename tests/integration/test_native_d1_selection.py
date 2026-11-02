"""Independent native-d1 catalog, native capture and real HTTP ownership oracles.

All model replies are synthetic. ASGI/HTTPX transports never open a socket.
"""

import asyncio
import importlib
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from packages.contracts.client import JournalClient
from packages.contracts.decisions import DecisionsCallStart
from packages.contracts.decisions_models import DecisionsModelChange, DecisionsModelPin
from packages.contracts.models import (
    AppendRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    Task,
)
from packages.providers.decision_capture import DecisionCapture, decision_attempt
from packages.providers.text import (
    ProviderCleanupError,
    ProviderError,
    ProviderRefusal,
    ScriptedProvider,
)
from services.common.auth import Auth
from services.decisions.model_api import router as model_router
from services.decisions.model_settings import DecisionsModelSettings, DecisionsModelUnavailable
from services.decisions.runtime import ModelClassifier
from services.decisions.window_routing import WindowClassifier
from services.decisions.window_worker import WindowDecisionsWorker
from services.launcher import provider as configure_provider
from services.session.api import create_app
from services.session.store import Store
from services.settings.provider_api import router as catalog_router
from services.settings.provider_catalog import ProviderCatalog, ProviderChange
from services.settings.provider_factory import declaration_info, inspect_recipe

MODEL = "native/d1-Omni_2B.Q8"
AUTH = Auth({role: role * 32 for role in ("owner", "agent", "converse", "decisions", "resources")})
REAL_ASYNC_CLIENT = httpx.AsyncClient


def native():
    assert importlib.util.find_spec("packages.providers.native_d1") is not None, (
        "UNIMPLEMENTED native_d1 adapter module, not a behavioral failure"
    )
    return importlib.import_module("packages.providers.native_d1").NativeD1RoutingProvider


def compact(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def config(**changes):
    return {
        "kind": "native_d1",
        "base_url": "http://127.0.0.1:8870",
        "model": MODEL,
        "allow_cloud": False,
        "credential": None,
        "timeout_s": 5,
        "min_confidence": 0.6,
        **changes,
    }


def upsert(catalog, cfg=None, *, key="profile-one"):
    return catalog.change(
        ProviderChange.model_validate(
            {
                "schema_version": "1.0",
                "expected_revision": catalog.snapshot()["revision"],
                "idempotency_key": key,
                "operation": "profile_upsert",
                "profile_id": "native-d1",
                "label": "Public native classifier",
                "config": config() if cfg is None else cfg,
            }
        )
    )


def selection(receipt):
    return {
        "source": "catalog",
        "catalog_binding": {
            k: receipt["result"][k] for k in ("id", "generation", "configuration_sha256")
        },
    }


def change(settings, selected, *, key="select-native"):
    state = settings.snapshot()
    return DecisionsModelChange.model_validate(
        {
            "schema_version": "1.0",
            "expected_backend": state["backend_id"],
            "expected_instance": state["instance_id"],
            "expected_revision": state["revision"],
            "expected_state": state["state_token"],
            "idempotency_key": key,
            "selection": selected,
        }
    )


def source(env, mode="window", *, index=0, text="Public native café instruction Ω"):
    payload = (
        {"text": text, "revision": 1, "final": True, "content_scope": "public_synthetic"}
        if mode == "finalized"
        else SpeechWindow(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            utterance_id="native-public",
            window_index=index,
            text=text,
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        ).model_dump(mode="json")
    )
    return env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.user" if mode == "finalized" else "conversation.window",
            payload=payload,
            idempotency_key=f"public-{mode}-{index}",
        ),
        "owner",
    )


class Wire:
    def __init__(self, route="correction"):
        self.route, self.requests, self.raw = route, [], None

    def __call__(self, request):
        self.requests.append(request)
        body = json.loads(request.content)
        choices = {
            "conversation": "ready",
            "agent_route": self.route,
            "route": self.route,
            "target": "t0"
            if "t0" in body["questions"].get("target", {}).get("criteria", {})
            else "NONE",
        }
        response = {
            "model": body["model"],
            "answers": {
                name: {
                    "type": "choice",
                    "choice": choices[name],
                    "probabilities": {
                        label: float(label == choices[name]) for label in q["criteria"]
                    },
                    "confidence": 1.0,
                }
                for name, q in body["questions"].items()
            },
            "usage": {"input_tokens": 23, "output_tokens": 0},
        }
        self.raw = compact(response)
        return httpx.Response(200, stream=httpx.ByteStream(self.raw))


@asynccontextmanager
async def environment(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    catalog = ProviderCatalog(tmp_path / "catalog.sqlite", workspace)
    host = ScriptedProvider([])
    settings = DecisionsModelSettings(
        host, catalog, path=tmp_path / "decisions.sqlite", workspace=workspace
    )
    store = Store(tmp_path / "session.sqlite")
    session = store.create_session(SessionRequest())
    journal = JournalClient(
        "http://session",
        AUTH.tokens["decisions"],
        transport=httpx.ASGITransport(app=create_app(store, AUTH)),
    )
    app = FastAPI()
    app.include_router(catalog_router(catalog, AUTH))
    app.include_router(model_router(settings, AUTH))
    api = REAL_ASYNC_CLIENT(base_url="http://owner", transport=httpx.ASGITransport(app=app))
    env = SimpleNamespace(
        catalog=catalog,
        host=host,
        settings=settings,
        store=store,
        session=session,
        journal=journal,
        api=api,
    )
    try:
        yield env
    finally:
        await api.aclose()
        await settings.close()
        if settings._host_cleanup is not None:
            await asyncio.wait_for(asyncio.shield(settings._host_cleanup), 5)
        assert not settings.leases and settings._db_closed and settings._owner_fd is None
        await journal.close()
        catalog.close()
        store.close()


def rows(env):
    return [
        json.loads(row[0])
        for row in env.store.db.execute("SELECT value FROM decisions_calls ORDER BY call_sequence")
    ]


def inject_transport(monkeypatch, wire):
    def constructor(*args, **kwargs):
        assert kwargs.get("transport") is None
        kwargs["transport"] = httpx.MockTransport(wire)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", constructor)


async def test_catalog_and_selection_owner_apis_are_declaration_only_truthful_and_role_closed(
    tmp_path, monkeypatch
):
    async with environment(tmp_path) as env:
        adapter_type = native()
        calls = []

        def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("metadata must not construct an adapter")

        monkeypatch.setattr(
            importlib.import_module("packages.providers.native_d1"),
            "NativeD1RoutingProvider",
            forbidden,
        )
        headers = {"Authorization": "Bearer " + AUTH.tokens["owner"]}
        request = {
            "schema_version": "1.0",
            "expected_revision": 0,
            "idempotency_key": "api-save",
            "operation": "profile_upsert",
            "profile_id": "native-d1",
            "label": "D1 native",
            "config": config(),
        }
        for role in ("agent", "converse", "decisions", "resources"):
            denied = await env.api.post(
                "/v1/providers/changes",
                json=request,
                headers={"Authorization": "Bearer " + AUTH.tokens[role]},
            )
            assert denied.status_code == 403
            denied = await env.api.get(
                "/v1/decisions/options/model",
                headers={"Authorization": "Bearer " + AUTH.tokens[role]},
            )
            assert denied.status_code == 403
        saved = await env.api.post("/v1/providers/changes", json=request, headers=headers)
        assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
        metadata = await env.api.get("/v1/providers", headers=headers)
        assert metadata.status_code == 200 and metadata.headers["cache-control"] == "no-store"
        profile = metadata.json()["profiles"][0]
        assert profile["config"] == config()
        assert profile["capability"] == {
            "declared_roles": ["decisions"],
            "supported_inputs": ["text"],
            "measured_complete_interface": False,
            "measured_stream_interface": False,
            "qualification": "not_checked",
            "readiness": "unknown",
        }
        model = await env.api.get("/v1/decisions/options/model", headers=headers)
        candidate = model.json()["catalog_profiles"][0]
        assert candidate["selectable"] is True and candidate["protocol"] == "native_d1_systemone"
        assert candidate["qualification"] == "not_checked" and candidate["readiness"] == "unknown"
        selected = change(env.settings, selection(saved.json()))
        result = await env.api.post(
            "/v1/decisions/options/model", json=selected.model_dump(mode="json"), headers=headers
        )
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        lookup = await env.api.post(
            "/v1/decisions/options/model-changes/lookup",
            json=selected.model_dump(mode="json"),
            headers=headers,
        )
        assert lookup.json()["status"] == "recorded" and lookup.json()["receipt"] == result.json()
        assert env.settings.snapshot()["selected_info"] == {
            "provider": "native_d1",
            "model": MODEL,
            "local": True,
            "streaming": False,
        }
        assert calls == []
        monkeypatch.setattr(
            importlib.import_module("packages.providers.native_d1"),
            "NativeD1RoutingProvider",
            adapter_type,
        )
        binding = selection(saved.json())["catalog_binding"]
        for role in ("agent", "converse"):
            with pytest.raises(ValueError):
                inspect_recipe(env.catalog, binding, role=role)


@pytest.mark.parametrize(
    "patch",
    [
        {"min_confidence": None},
        {"min_confidence": True},
        {"min_confidence": -0.1},
        {"min_confidence": 1.1},
        {"allow_cloud": True},
        {"allow_cloud": 0},
        {"base_url": "http://remote.invalid:8870"},
        {"base_url": "http://localhost"},
        {"model": "alias space"},
        {"files": []},
        {"max_len": 4096},
        {"timeout_s": 0},
    ],
)
def test_native_catalog_profile_is_closed_and_policy_strict(patch):
    with pytest.raises(ValidationError):
        ProviderChange.model_validate(
            {
                "schema_version": "1.0",
                "expected_revision": 0,
                "idempotency_key": "strict",
                "operation": "profile_upsert",
                "profile_id": "native-d1",
                "label": "D1",
                "config": config(**patch),
            }
        )


def test_native_catalog_confidence_has_no_default():
    cfg = config()
    cfg.pop("min_confidence")
    with pytest.raises(ValidationError):
        ProviderChange.model_validate(
            {
                "schema_version": "1.0",
                "expected_revision": 0,
                "idempotency_key": "strict",
                "operation": "profile_upsert",
                "profile_id": "native-d1",
                "label": "D1",
                "config": cfg,
            }
        )


async def test_catalog_generation_rotation_never_retargets_old_selection_and_new_remains_usable(
    tmp_path, monkeypatch
):
    async with environment(tmp_path) as env:
        first = upsert(env.catalog)
        env.settings.change(change(env.settings, selection(first)))
        second = upsert(env.catalog, config(model="native/d1-new"), key="profile-two")
        assert first["result"]["generation"] != second["result"]["generation"]
        state = env.settings.snapshot()
        assert state["selected_info"] is None and state["selected_error"] is not None
        with pytest.raises(DecisionsModelUnavailable):
            async with env.settings.operation():
                pytest.fail("old generation cannot acquire a replacement")
        env.settings.change(change(env.settings, selection(second), key="new-generation"))
        wire = Wire()
        inject_transport(monkeypatch, wire)
        async with env.settings.operation() as lease:
            assert (
                lease.pin.protocol == "native_d1_systemone" and lease.pin.model == "native/d1-new"
            )
            await WindowClassifier(lease.provider).classify(source(env), [], [])
        assert json.loads(wire.requests[0].content)["model"] == "native/d1-new"
        assert not env.settings.busy()


@pytest.mark.parametrize("mode", ["window", "finalized"])
async def test_selected_native_adapter_records_original_capture_and_routes_real_native_source(
    tmp_path, monkeypatch, mode
):
    async with environment(tmp_path) as env:
        saved = upsert(env.catalog)
        env.settings.change(change(env.settings, selection(saved)))
        wire = Wire()
        inject_transport(monkeypatch, wire)
        entry = source(env, mode)
        task = Task(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="SECRET-OBJECTIVE-EXCLUDED",
            objective_revision=4,
            state="running",
            idempotency_key="native-task",
        )
        async with env.settings.operation() as lease:
            assert lease.pin.protocol == "native_d1_systemone" and lease.pin.provider == "native_d1"
            async with decision_attempt(
                env.journal, entry, lease.provider, model_pin=lease.pin, current=lease.current
            ) as capture:
                result = await (
                    WindowClassifier(lease.provider)
                    if mode == "window"
                    else ModelClassifier(lease.provider)
                ).classify(entry, [], [task])
                capture.validated_output = result
            pin = lease.pin.model_dump(mode="json")
        assert (
            result["target_task_id"] == task.task_id and result["expected_objective_revision"] == 4
        )
        assert result["agent_route" if mode == "window" else "route"] == (
            "STEER_CURRENT_TASK" if mode == "window" else "steer"
        )
        records = rows(env)
        assert len(records) == 1
        record = records[0]
        assert record["source_event_id"] == entry.event_id and record["source_revision"] == 1
        assert record["model_pin"] == pin and record["request_format"] == "native_d1_systemone"
        assert compact(record["request"]) == wire.requests[0].content
        assert record["raw_response"].encode() == wire.raw and record["artifact_sha256"] is None
        assert record["validated_output"] == result and record["status"] == "completed"
        assert b"SECRET-OBJECTIVE" not in wire.requests[0].content


async def test_window_reuse_retains_original_native_model_pin_without_another_request(
    tmp_path, monkeypatch
):
    async with environment(tmp_path) as env:
        env.settings.change(change(env.settings, selection(upsert(env.catalog))))
        wire = Wire(route="conversation")
        inject_transport(monkeypatch, wire)

        class Agent:
            async def status(self):
                return []

        worker = WindowDecisionsWorker(
            env.journal, Agent(), WindowClassifier(env.host), model_settings=env.settings
        )
        first = source(env, index=0)
        await worker.classify(first)
        second = source(env, index=1)
        await worker.classify(second)
        assert len(wire.requests) == 1
        records = rows(env)
        assert len(records) == 2
        original, reused = records
        assert (
            original["request_format"] == "native_d1_systemone"
            and original["model_pin"]["provider"] == "native_d1"
        )
        assert (
            reused["request_format"] == "reused"
            and reused["reused_from_call_id"] == original["call_id"]
        )
        assert reused["model_pin"] == original["model_pin"] and reused["request"] is None
        assert reused["source_event_id"] == second.event_id and reused["status"] == "reused"
        assert (await env.journal.window_decision(second.event_id)).classification_reused is True


@pytest.mark.parametrize(
    "mutation", ["body", "model", "info", "headers", "origin", "client", "source-revision", "reset"]
)
async def test_capture_await_cannot_authorize_mutated_transport_or_native_source(
    tmp_path, monkeypatch, mutation
):
    async with environment(tmp_path) as env:
        adapter = native()(
            "http://127.0.0.1:8870",
            MODEL,
            min_confidence=0.5,
            transport=httpx.MockTransport(wire := Wire()),
        )
        entry = source(env, "finalized")
        original_client = adapter.http
        replacement_clients = []
        original = DecisionCapture.start

        async def start(capture, provider, body, request_format):
            await original(capture, provider, body, request_format)
            if request_format != "native_d1_systemone":
                return
            if mutation == "body":
                body["state"] += " mutation"
            elif mutation == "model":
                adapter.model = "foreign-model"
            elif mutation == "info":
                adapter.info = ProviderInfo(
                    provider="native_d1", model=MODEL, local=False, streaming=False
                )
            elif mutation == "headers":
                adapter.http.headers["authorization"] = "Bearer replacement"
            elif mutation == "origin":
                adapter.http.base_url = "http://localhost:8871"
            elif mutation == "client":
                adapter.http = REAL_ASYNC_CLIENT(
                    base_url="http://127.0.0.1:8870", transport=httpx.MockTransport(wire)
                )
                replacement_clients.append(adapter.http)
            elif mutation == "source-revision":
                env.store.append(
                    AppendRequest(
                        session_id=entry.session_id,
                        thread_id=entry.thread_id,
                        epoch=entry.epoch,
                        kind="conversation.revision",
                        payload={
                            "target_event_id": entry.event_id,
                            "revision": 2,
                            "text": "Public revised request",
                            "content_scope": "public_synthetic",
                        },
                        idempotency_key="revision",
                    ),
                    "owner",
                )
            else:
                env.store.reset(entry.session_id)

        monkeypatch.setattr(DecisionCapture, "start", start)
        try:
            with pytest.raises((ProviderRefusal, DecisionsModelUnavailable, ProviderError)):
                async with decision_attempt(env.journal, entry, adapter):
                    await ModelClassifier(adapter).classify(entry, [], [])
            assert wire.requests == []
        finally:
            await adapter.close()
            try:
                assert original_client.is_closed
            finally:
                await original_client.aclose()
                for replacement in replacement_clients:
                    await replacement.aclose()


def pin(provider="native_d1", protocol="native_d1_systemone"):
    return {
        "role": "decisions",
        "backend_id": "a" * 32,
        "instance_id": "b" * 32,
        "settings_revision": 0,
        "selection": {
            "source": "catalog",
            "catalog_binding": {
                "id": "native-d1",
                "generation": 1,
                "configuration_sha256": "c" * 64,
            },
        },
        "catalog_revision": 1,
        "provider": provider,
        "model": MODEL,
        "local": True,
        "protocol": protocol,
    }


def call(model_pin, request_format="native_d1_systemone"):
    return {
        "call_id": "native-call",
        "attempt_id": "native-attempt",
        "attempt": 1,
        "source_event_id": "native-source",
        "source_sequence": 1,
        "source_revision": 1,
        "session_id": "native-session",
        "thread_id": "native-thread",
        "epoch": 1,
        "provider": model_pin["provider"],
        "model": model_pin["model"],
        "model_pin": model_pin,
        "request_format": request_format,
        "request": {"model": MODEL, "state": "public", "questions": {}},
    }


def test_native_pin_and_submitted_capture_protocol_are_exact_pairs():
    value = pin()
    assert DecisionsModelPin.model_validate(value).protocol == "native_d1_systemone"
    assert DecisionsCallStart.model_validate(call(value)).request_format == "native_d1_systemone"
    for provider, protocol in [
        ("laya", "native_d1_systemone"),
        ("native_d1", "laya_systemone"),
        ("native_d1", "host_configured"),
    ]:
        with pytest.raises(ValidationError):
            DecisionsModelPin.model_validate(pin(provider, protocol))
    for protocol in ("laya_systemone", "chat_completions", "responses"):
        with pytest.raises(ValidationError):
            DecisionsCallStart.model_validate(call(value, protocol))
    legacy = pin("laya", "laya_systemone")
    assert (
        DecisionsCallStart.model_validate(call(legacy, "laya_systemone")).model_pin.provider
        == "laya"
    )
    with pytest.raises(ValidationError):
        DecisionsCallStart.model_validate(call(legacy))


async def test_native_loader_is_explicit_decisions_only_with_protected_key_and_no_probe(
    monkeypatch,
):
    monkeypatch.setenv("PUBLIC_NATIVE_D1_TEST_KEY", "synthetic-key-only")
    cfg = {
        "providers": {
            role: {
                "kind": "native_d1",
                "base_url": "http://127.0.0.1:8870",
                "model": MODEL,
                "min_confidence": 0.73,
                "timeout_s": 4,
                "api_key_env": "PUBLIC_NATIVE_D1_TEST_KEY",
            }
            for role in ("agent", "converse", "decisions")
        }
    }
    for role in ("agent", "converse"):
        with pytest.raises(SystemExit):
            configure_provider(cfg, role)
    adapter = configure_provider(cfg, "decisions")
    try:
        assert adapter.info == declaration_info(config())
        assert (
            not adapter.info.streaming
            and adapter.http.headers["authorization"] == "Bearer synthetic-key-only"
        )
        assert adapter.http.timeout.read == 4
        health = await adapter.health()
        assert health["ready"] is False and health["qualification"] == "not_checked"
    finally:
        await adapter.close()


class GateStream(httpx.AsyncByteStream):
    def __init__(self, transport):
        self.owner = transport

    async def __aiter__(self):
        self.owner.read_task = asyncio.current_task()
        if self.owner.phase == "read":
            await self.owner.hold()
        if self.owner.phase == "timeout":
            raise httpx.ReadTimeout("synthetic bounded read timeout")
        yield self.owner.body

    async def aclose(self):
        self.owner.close_count += 1
        self.owner.close_task = asyncio.current_task()
        if self.owner.phase in {"close", "failure", "timeout"}:
            await self.owner.hold()
        if self.owner.phase == "failure":
            raise RuntimeError("synthetic original stream close failed")


class GateTransport(httpx.AsyncBaseTransport):
    def __init__(self, phase):
        self.phase, self.entered, self.release = phase, asyncio.Event(), asyncio.Event()
        self.send_task = self.read_task = self.close_task = None
        self.close_count = self.transport_closes = 0
        self.body = compact(
            {
                "model": MODEL,
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "conversation",
                        "probabilities": {
                            "conversation": 1.0,
                            "new_task": 0.0,
                            "correction": 0.0,
                            "cancellation": 0.0,
                        },
                        "confidence": 1.0,
                    }
                },
                "usage": {"input_tokens": 17, "output_tokens": 0},
            }
        )

    async def hold(self):
        self.entered.set()
        while not self.release.is_set():
            try:
                await asyncio.shield(self.release.wait())
            except asyncio.CancelledError:
                pass

    async def handle_async_request(self, request):
        self.send_task = asyncio.current_task()
        if self.phase == "send":
            await self.hold()
        return httpx.Response(200, stream=GateStream(self))

    async def aclose(self):
        self.transport_closes += 1


def routing_messages():
    return [
        {"role": "system", "content": "Native typed routing"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "latest": {"kind": "conversation.user", "text": "Public gate request"},
                    "context": [],
                    "tasks": [],
                }
            ),
        },
    ]


@pytest.mark.parametrize("phase", ["send", "read", "close", "timeout", "failure"])
async def test_actual_http_work_and_sole_close_keep_decisions_lease_and_store_owned(
    tmp_path, phase
):
    wire = GateTransport(phase)
    adapter = native()(
        "http://127.0.0.1:8870",
        MODEL,
        min_confidence=0.5,
        timeout=5,
        transport=wire,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = DecisionsModelSettings(
        adapter, None, path=tmp_path / "decisions.sqlite", workspace=workspace
    )
    leases = []

    async def run():
        async with settings.operation() as lease:
            leases.append(lease)
            return await lease.provider.complete(routing_messages())

    runner = asyncio.create_task(run())
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
        original = {
            task for task in (wire.send_task, wire.read_task, wire.close_task) if task is not None
        }
        lease = leases[0]
        assert lease in settings.leases and settings.busy()
        assert (
            settings._owner_fd is not None
            and not settings._db_closed
            and not adapter.http.is_closed
        )
        if phase not in {"failure", "timeout"}:
            runner.cancel()
        await asyncio.sleep(0)
        assert not runner.done() and lease in settings.leases
        await settings.close()
        assert (
            settings._owner_fd is not None
            and not settings._db_closed
            and not adapter.http.is_closed
        )
        assert wire.transport_closes == 0
        wire.release.set()
        if phase == "failure":
            with pytest.raises(ProviderCleanupError):
                await asyncio.wait_for(asyncio.shield(runner), 5)
            assert lease.failed and lease in settings.leases and settings.busy()
            await settings.close()
            assert settings._owner_fd is not None and not settings._db_closed
        else:
            if phase == "timeout":
                with pytest.raises(ProviderError) as error:
                    await asyncio.wait_for(asyncio.shield(runner), 5)
                assert not isinstance(error.value, ProviderRefusal)
            else:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(runner), 5)
            await settings.close()
            assert not settings.leases and settings._db_closed and settings._owner_fd is None
            assert adapter.http.is_closed and wire.transport_closes == 1
        assert wire.close_count == 1 and wire.close_task is not None and wire.close_task.done()
        assert all(task.done() for task in original)
    finally:
        wire.release.set()
        await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 5)
        if leases and leases[0].cleanup is not None:
            await asyncio.wait_for(asyncio.gather(leases[0].cleanup, return_exceptions=True), 5)
        if phase == "failure":
            # Fault cleanup is fixture-owned after the original failed owner is joined.
            # This releases temporary test resources; it never clears the sticky lease.
            await adapter.http.aclose()
            assert leases[0].failed and leases[0] in settings.leases
            settings.db.close()
            os.close(settings._owner_fd)
        else:
            await settings.close()
