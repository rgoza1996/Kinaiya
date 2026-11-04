"""Integration tests for OpenRouter Decisions catalog, selection, and native capture."""

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
from packages.providers.decision_capture import decision_attempt
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


def compact(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


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
        records = [
            json.loads(row[0])
            for row in env.store.db.execute(
                "SELECT value FROM decisions_calls ORDER BY call_sequence"
            )
        ]
        assert len(records) == 1
        record = records[0]
        assert record["model_pin"] == pin and record["request_format"] == "laya_systemone"
        assert compact(record["request"]) == wire.requests[0].content
        assert record["raw_response"].encode() == wire.raw
        assert record["validated_output"] == result and record["status"] == "completed"


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
