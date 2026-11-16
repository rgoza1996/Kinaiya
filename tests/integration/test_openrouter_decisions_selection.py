"""Integration tests for OpenRouter Decisions catalog, selection, and native capture."""

import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from packages.contracts.client import JournalClient
from packages.contracts.decisions_models import DecisionsModelChange
from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow, Task
from packages.providers.decision_capture import CaptureNativeUnavailable, decision_attempt
from packages.providers.openrouter_decisions import (
    OPENROUTER_DECISIONS_MODELS,
    OpenRouterDecisionsProvider,
)
from packages.providers.text import ProviderRefusal, ScriptedProvider
from services.common.auth import Auth
from services.decisions.model_api import router as model_router
from services.decisions.model_settings import DecisionsModelSettings
from services.decisions.runtime import ModelClassifier
from services.decisions.window_routing import WindowClassifier
from services.launcher import provider as configure_provider
from services.launcher import validate_setup_provider
from services.session.api import create_app
from services.session.store import Store
from services.settings.provider_api import router as catalog_router
from services.settings.provider_catalog import ProviderCatalog, ProviderChange
from services.settings.provider_factory import inspect_recipe

MODEL = "typesafe/jev-1.14"
AUTH = Auth({role: role * 32 for role in ("owner", "agent", "converse", "decisions", "resources")})
REAL_ASYNC_CLIENT = httpx.AsyncClient
MAX_SELECTION_OBSERVATIONS = 3


class UnconfirmedSelectionCapture(AssertionError):
    """The positive selection oracle cannot continue on unknown inspection."""


def compact(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def capture_records(env):
    return [
        json.loads(row[0])
        for row in env.store.db.execute("SELECT value FROM decisions_calls ORDER BY call_sequence")
    ]


async def observe_selected_classification(env, entry, lease, wire, classifier, task):
    """Require success, while preserving strictly proved pre-dispatch refusals."""
    refused = []
    for observation in range(1, MAX_SELECTION_OBSERVATIONS + 1):
        try:
            async with decision_attempt(
                env.journal,
                entry,
                lease.provider,
                attempt=observation,
                model_pin=lease.pin,
                current=lease.current,
            ) as capture:
                result = await classifier.classify(entry, [], [task])
                capture.validated_output = result
        except CaptureNativeUnavailable as exc:
            # This test observes catalog/transport/capture bindings. A native
            # currentness refusal is permitted; it is not a positive binding
            # result and must not be converted into Ignore or discarded.
            if str(exc) != "Decisions currentness check exceeded its bound" or not isinstance(
                exc.__cause__, TimeoutError
            ):
                raise
            assert wire.requests == [], "A dispatched provider opportunity must not be retried"
            records = capture_records(env)
            assert len(records) == observation
            record = records[-1]
            assert record["call_id"] == capture.call_id
            assert record["source_event_id"] == entry.event_id
            assert record["model_pin"] == lease.pin.model_dump(mode="json")
            assert record["attempt"] == observation
            assert record["raw_response"] is None and record["validated_output"] is None
            assert (
                env.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 0
            )
            assert env.store.read(kinds=["routing.decided", "routing.window_decided"]) == []
            if record["status"] == "started":
                assert record["error_type"] is None
                raise UnconfirmedSelectionCapture(
                    "Selected-provider capture finish is unconfirmed"
                ) from exc
            assert (
                record["status"] == "error" and record["error_type"] == "CaptureNativeUnavailable"
            )
            refused.append(capture.call_id)
            if observation == MAX_SELECTION_OBSERVATIONS:
                raise
        else:
            return result, refused
    raise AssertionError("Selected-provider observation did not complete")


def hold_latest_response(monkeypatch, count):
    """Hold genuine Session responses, not the currentness implementation."""
    original_environment, original_wire = environment, Wire
    report = {"held": 0, "cancelled_reads": 0, "wires": [], "records": []}

    @asynccontextmanager
    async def controlled_environment(tmp_path):
        async with original_environment(tmp_path) as env:
            app = env.journal.http._transport.app

            @app.middleware("http")
            async def hold(request, call_next):
                response = await call_next(request)
                if request.url.path == "/v1/transcript/windows/latest" and report["held"] < count:
                    assert response.status_code == 200
                    report["held"] += 1
                    try:
                        await asyncio.Event().wait()
                    finally:
                        report["cancelled_reads"] += 1
                return response

            try:
                yield env
            finally:
                report["records"] = capture_records(env)
                report["native_reservations"] = env.store.db.execute(
                    "SELECT count(*) FROM decisions_attempts"
                ).fetchone()[0]
                report["dispatch_events"] = env.store.read(
                    kinds=["routing.decided", "routing.window_decided"]
                )

    def controlled_wire(*args, **kwargs):
        wire = original_wire(*args, **kwargs)
        report["wires"].append(wire)
        return wire

    monkeypatch.setitem(globals(), "environment", controlled_environment)
    monkeypatch.setitem(globals(), "Wire", controlled_wire)
    return report


async def test_selected_window_oracle_retains_held_currentness_refusal_then_requires_success(
    tmp_path, monkeypatch
):
    report = hold_latest_response(monkeypatch, 1)
    try:
        await test_selected_openrouter_decisions_routes_and_captures_inspection(
            tmp_path, monkeypatch, "window"
        )
    finally:
        print(
            {
                "held_native_responses": report["held"],
                "cancelled_reads": report["cancelled_reads"],
                "provider_http_calls": sum(len(wire.requests) for wire in report["wires"]),
                "capture_statuses": [row["status"] for row in report["records"]],
                "native_reservations": report.get("native_reservations"),
            }
        )
    assert report["held"] == report["cancelled_reads"] == 1
    assert [row["status"] for row in report["records"]] == ["error", "completed"]
    assert len(report["wires"][0].requests) == 1
    assert report["native_reservations"] == 0 and report["dispatch_events"] == []


async def test_selected_window_oracle_exhaustion_is_a_failure_not_success(tmp_path, monkeypatch):
    report = hold_latest_response(monkeypatch, MAX_SELECTION_OBSERVATIONS)
    with pytest.raises((CaptureNativeUnavailable, UnconfirmedSelectionCapture)) as raised:
        await test_selected_openrouter_decisions_routes_and_captures_inspection(
            tmp_path, monkeypatch, "window"
        )
    statuses = [row["status"] for row in report["records"]]
    if isinstance(raised.value, UnconfirmedSelectionCapture):
        assert str(raised.value) == "Selected-provider capture finish is unconfirmed"
        assert isinstance(raised.value.__cause__, CaptureNativeUnavailable)
        assert 1 <= len(statuses) <= MAX_SELECTION_OBSERVATIONS
        assert statuses == ["error"] * (len(statuses) - 1) + ["started"]
        assert report["records"][-1]["error_type"] is None
    else:
        assert str(raised.value) == "Decisions currentness check exceeded its bound"
        assert isinstance(raised.value.__cause__, TimeoutError)
        assert statuses == ["error"] * MAX_SELECTION_OBSERVATIONS
    assert report["held"] == report["cancelled_reads"] == len(statuses)
    assert all(
        row["raw_response"] is None and row["validated_output"] is None for row in report["records"]
    )
    assert report["wires"][0].requests == []
    assert report["native_reservations"] == 0 and report["dispatch_events"] == []


async def test_selected_oracle_never_reobserves_a_dispatched_provider_opportunity(
    tmp_path, monkeypatch
):
    created = []

    class DispatchedWire(Wire):
        def __init__(self):
            super().__init__()
            created.append(self)

        def __call__(self, request):
            self.requests.append(request)
            # Even an exception with the same text/cause cannot justify a new
            # observation once transport entry has actually occurred.
            raise CaptureNativeUnavailable(
                "Decisions currentness check exceeded its bound"
            ) from TimeoutError()

    monkeypatch.setitem(globals(), "Wire", DispatchedWire)
    with pytest.raises(AssertionError, match="dispatched provider opportunity"):
        await test_selected_openrouter_decisions_routes_and_captures_inspection(
            tmp_path, monkeypatch, "window"
        )
    assert len(created) == 1 and len(created[0].requests) == 1


class Wire:
    def __init__(self, route="correction"):
        self.route, self.requests, self.raw = route, [], None

    def __call__(self, request):
        self.requests.append(request)
        body = json.loads(request.content)
        spec = OPENROUTER_DECISIONS_MODELS[body["model"]]
        choices = {
            "conversation": "ready",
            "agent_route": self.route,
            "route": self.route,
            "target": "t0"
            if "t0" in body["questions"].get("target", {}).get("criteria", {})
            else "NONE",
        }
        response = {
            "id": "gen-openrouter-decisions-test",
            "model": spec["allowed_response_identities"][-1],
            "provider": spec["provider_name"],
            "answers": {
                name: {
                    "type": "choice",
                    "choice": choices[name],
                    "probabilities": {
                        label: float(label == choices[name]) for label in q["criteria"]
                    },
                    "confidence": 0.96,
                }
                for name, q in body["questions"].items()
            },
            "usage": {"input_tokens": 31, "output_tokens": 14, "cost": 0.00001302},
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
            await settings._host_cleanup
        await journal.close()
        catalog.close()
        store.close()


def import_credential(catalog, *, key="cred-import"):
    return catalog.change(
        ProviderChange.model_validate(
            {
                "schema_version": "1.0",
                "expected_revision": catalog.snapshot()["revision"],
                "idempotency_key": key,
                "operation": "credential_import",
                "credential_id": "openrouter-key",
                "key": "sk-or-v1-synthetic-credential-0123456789",
            }
        )
    )["result"]


def config(cred, **changes):
    return {
        "kind": "openrouter_decisions",
        "base_url": "https://openrouter.ai/api/alpha",
        "model": MODEL,
        "allow_cloud": True,
        "credential": {"id": cred["id"], "generation": cred["generation"]},
        "timeout_s": 15,
        "min_confidence_bps": 6000,
        **changes,
    }


def upsert(catalog, cred, cfg=None, *, key="profile-one"):
    return catalog.change(
        ProviderChange.model_validate(
            {
                "schema_version": "1.0",
                "expected_revision": catalog.snapshot()["revision"],
                "idempotency_key": key,
                "operation": "profile_upsert",
                "profile_id": "openrouter-decisions",
                "label": "OpenRouter Jev 1.14",
                "config": config(cred) if cfg is None else cfg,
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


def change(settings, selected, *, key="select-openrouter"):
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


def source(
    env,
    mode="window",
    *,
    index=0,
    text="Public synthetic instruction Ω",
    content_scope="public_synthetic",
):
    payload = (
        {"text": text, "revision": 1, "final": True, "content_scope": content_scope}
        if mode == "finalized"
        else SpeechWindow(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            utterance_id="openrouter-public",
            window_index=index,
            text=text,
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope=content_scope,
        ).model_dump(mode="json")
    )
    return env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.user" if mode == "finalized" else "conversation.window",
            payload=payload,
            idempotency_key=f"public-{mode}-{index}-{content_scope}",
        ),
        "owner",
    )


def inject_transport(monkeypatch, wire):
    def constructor(*args, **kwargs):
        assert kwargs.get("transport") is None
        kwargs["transport"] = httpx.MockTransport(wire)
        return OpenRouterDecisionsProvider(*args, **kwargs)

    monkeypatch.setattr(
        importlib.import_module("packages.providers.openrouter_decisions"),
        "OpenRouterDecisionsProvider",
        constructor,
    )


async def test_catalog_and_decisions_selection_accept_openrouter_decisions_and_forbid_other_roles(
    tmp_path,
):
    async with environment(tmp_path) as env:
        cred = import_credential(env.catalog)
        saved = upsert(env.catalog, cred)
        snapshot = env.catalog.snapshot()
        profile = snapshot["profiles"][0]
        assert profile["config"] == config(cred)
        assert profile["capability"] == {
            "declared_roles": ["decisions"],
            "supported_inputs": ["text"],
            "measured_complete_interface": False,
            "measured_stream_interface": False,
            "qualification": "not_checked",
            "readiness": "unknown",
        }
        options = env.settings.snapshot()
        candidate = options["catalog_profiles"][0]
        assert candidate["selectable"] is True
        assert candidate["protocol"] == "laya_systemone"
        assert candidate["provider"] == {
            "schema_version": "1.0",
            "provider": "openrouter_decisions",
            "model": MODEL,
            "local": False,
            "streaming": False,
        }
        env.settings.change(change(env.settings, selection(saved)))
        assert env.settings.snapshot()["selected_info"] == candidate["provider"]
        binding = selection(saved)["catalog_binding"]
        for role in ("agent", "converse"):
            with pytest.raises(ValueError):
                inspect_recipe(env.catalog, binding, role=role)


@pytest.mark.parametrize("mode", ["window", "finalized"])
async def test_selected_openrouter_decisions_routes_and_captures_inspection(
    tmp_path, monkeypatch, mode
):
    async with environment(tmp_path) as env:
        cred = import_credential(env.catalog)
        saved = upsert(env.catalog, cred)
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
            idempotency_key="openrouter-task",
        )
        async with env.settings.operation() as lease:
            assert (
                lease.pin.protocol == "laya_systemone"
                and lease.pin.provider == "openrouter_decisions"
                and lease.pin.local is False
            )
            classifier = (
                WindowClassifier(lease.provider)
                if mode == "window"
                else ModelClassifier(lease.provider)
            )
            result, refused = await observe_selected_classification(
                env, entry, lease, wire, classifier, task
            )
            pin = lease.pin.model_dump(mode="json")
        assert (
            result["target_task_id"] == task.task_id and result["expected_objective_revision"] == 4
        )
        assert result["agent_route" if mode == "window" else "route"] == (
            "STEER_CURRENT_TASK" if mode == "window" else "steer"
        )
        records = capture_records(env)
        assert len(records) == len(refused) + 1
        assert [record["call_id"] for record in records[:-1]] == refused
        assert len(wire.requests) == 1
        record = records[-1]
        assert record["model_pin"] == pin and record["request_format"] == "laya_systemone"
        assert compact(record["request"]) == wire.requests[0].content
        assert record["raw_response"].encode() == wire.raw
        assert record["validated_output"] == result and record["status"] == "completed"
        assert all(compact(row["request"]) == wire.requests[0].content for row in records)


async def test_local_only_content_scope_is_refused_before_any_cloud_request(tmp_path, monkeypatch):
    async with environment(tmp_path) as env:
        cred = import_credential(env.catalog)
        saved = upsert(env.catalog, cred)
        env.settings.change(change(env.settings, selection(saved)))
        wire = Wire()
        inject_transport(monkeypatch, wire)
        local_entry = source(env, "finalized", content_scope="local_only")
        async with env.settings.operation() as lease:
            with pytest.raises(ProviderRefusal, match="Source disclosure does not permit"):
                await ModelClassifier(lease.provider).classify(local_entry, [], [])
        assert wire.requests == []


def test_launcher_and_setup_validation_support_openrouter_decisions_for_decisions_only():
    profile = {
        "kind": "openrouter_decisions",
        "base_url": "https://openrouter.ai/api/alpha",
        "model": "liquid/d1",
        "allow_cloud": True,
        "api_key": "sk-or-v1-synthetic",
        "min_confidence": 0.5,
    }
    adapter = validate_setup_provider("decisions", profile)
    assert adapter.info.provider == "openrouter_decisions"
    assert adapter.info.model == "liquid/d1"
    for role in ("agent", "converse"):
        with pytest.raises(SystemExit):
            configure_provider({"providers": {role: dict(profile)}}, role)
