"""Synthetic model routing, measured auxiliary admission and owner fences."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from core.policy import ToolPolicy
from packages.contracts.models import ProviderInfo, SessionRequest, Task, TaskRequest, TaskState
from packages.providers.text import ScriptedProvider
from services.agent.auxiliary import consult_model
from services.agent.model_settings import (
    AgentModelSettings,
    BoundModelSettingsChange,
    ModelSettingsChange,
    ModelSettingsConflict,
)
from services.agent.options_api import router
from services.agent.runtime import AgentRuntime, _Record
from services.agent.tool_catalog import CatalogGuard
from services.agent.tools import ToolFailure
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


def fixture(model, effort=None, *, local=True, replies=()):
    result = ScriptedProvider(replies)
    result.info = ProviderInfo(provider="deterministic-fixture", model=model, local=local)
    result.reasoning_effort = effort
    return result


def change(
    settings, revision, key, *, profile="fast", harness=None, purpose="analysis", reasoning=None
):
    authority = settings.snapshot()["write_authority"]
    return BoundModelSettingsChange(
        schema_version="1.1",
        expected_backend=authority["backend_id"],
        expected_instance=authority["instance_id"],
        expected_revision=revision,
        idempotency_key=key,
        harness_version=harness,
        profile_id=profile,
        reasoning=reasoning,
        auxiliary_task=purpose,
    )


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent", harness_version="seed-v1"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    primary = fixture("primary")
    aux = fixture("fast", replies=["Synthetic auxiliary result"])
    settings = AgentModelSettings(
        primary,
        {"fast": lambda effort: fixture("fast", effort)},
        path=tmp_path / "models.sqlite",
        auxiliary_factories={"fast": lambda effort: aux},
        reasoning_levels={"fast": ["low", "high"]},
    )
    settings.change(change(settings, 0, "analysis-default"))
    runtime = AgentRuntime(
        LocalJournal(store, "agent"), primary, workspace, ToolPolicy(workspace, allow_write=True)
    )
    runtime.model_settings = settings
    providers, pins = settings.auxiliary_selection("seed-v1")
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Synthetic public objective",
        idempotency_key="direct",
        harness_version="seed-v1",
        state=TaskState.RUNNING,
        model_pin=settings.selection("seed-v1")[1],
        auxiliary_models=pins,
    )
    rec = _Record(task=task, provider=primary, auxiliary_providers=providers)
    runtime._tasks[task.task_id] = rec
    yield SimpleNamespace(
        store=store,
        session=session,
        runtime=runtime,
        settings=settings,
        primary=primary,
        aux=aux,
        rec=rec,
        workspace=workspace,
    )
    await runtime.close()
    store.close()


async def test_auxiliary_binding_inheritance_override_and_persistence(env):
    settings = env.settings
    assert settings.selection("seed-v1", "analysis")[1].model == "fast"
    assert settings.selection("seed-v1", "compression")[1].model == "primary"
    settings.change(change(settings, 1, "harness-main", harness="seed-v1", purpose=None))
    assert settings.selection("seed-v1", "compression")[1].model == "fast"
    receipt = settings.change(
        change(settings, 2, "harness-analysis", harness="seed-v1", profile="host-default")
    )
    assert receipt["auxiliary_task"] == "analysis"
    assert settings.selection("seed-v1", "analysis")[1].model == "primary"
    assert (
        settings.change(
            change(settings, 2, "harness-analysis", harness="seed-v1", profile="host-default")
        )
        == receipt
    )
    settings.change(change(settings, 3, "inherit-analysis", harness="seed-v1", profile=None))
    assert settings.selection("seed-v1", "analysis")[1].model == "fast"
    with pytest.raises(ModelSettingsConflict):
        settings.change(change(settings, 1, "stale"))
    with pytest.raises(ValidationError):
        change(settings, 4, "bad-purpose", purpose="make_permissions")
    assert env.primary.calls == env.aux.calls == []
    snapshot = settings.snapshot()
    assert snapshot["auxiliary"] == {"analysis": {"profile_id": "fast", "reasoning": None}}
    assert snapshot["harness_auxiliary"] == {}


async def test_legacy_model_receipt_digest_is_unchanged(env):
    body = {
        "schema_version": "1.0",
        "expected_revision": 0,
        "idempotency_key": "legacy",
        "harness_version": None,
        "profile_id": "host-default",
        "reasoning": None,
    }
    receipt = {
        "schema_version": "1.0",
        "idempotency_key": "legacy",
        "revision": 1,
        "harness_version": None,
        "binding": {"profile_id": "host-default", "reasoning": None},
        "change_boundary": "new_tasks",
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with env.settings.db:
        env.settings.db.execute(
            "INSERT INTO operations VALUES (?,?,?)", ("legacy", digest, json.dumps(receipt))
        )
    assert env.settings.change(ModelSettingsChange(**body)) == receipt


async def test_auxiliary_text_is_pinned_untrusted_and_not_effect_evidence(env):
    # Later settings cannot replace the captured provider object or identity.
    env.settings.change(change(env.settings, 1, "later", profile="host-default"))
    output, evidence = await consult_model(
        env.runtime, env.rec, {"purpose": "analysis", "prompt": "Public question"}
    )
    result = json.loads(output)
    assert result["model_pin"]["model"] == "fast"
    assert result["untrusted_model_output"] == "Synthetic auxiliary result"
    assert evidence.kind == "external" and evidence.objective_revision == 1
    assert env.primary.calls == [] and len(env.aux.calls) == 1
    assert env.aux.calls[0][-1]["content"] == "Public question"


@pytest.mark.parametrize(
    "arguments",
    [
        {"purpose": "invalid", "prompt": "public"},
        {"purpose": "analysis", "prompt": ""},
        {"purpose": "analysis", "prompt": "public", "model": "caller-choice"},
        {"purpose": "analysis", "prompt": "x" * 8001},
        {"purpose": "analysis", "prompt": "😀" * 5000},
    ],
)
async def test_invalid_auxiliary_inputs_never_infer(env, arguments):
    with pytest.raises(ToolFailure):
        await consult_model(env.runtime, env.rec, arguments)
    assert env.aux.calls == []


async def test_cloud_auxiliary_disclosure_refuses_before_inference(env):
    env.aux.info = env.aux.info.model_copy(update={"local": False})
    env.rec.task.auxiliary_models["analysis"] = env.rec.task.auxiliary_models[
        "analysis"
    ].model_copy(update={"local": False})
    with pytest.raises(ToolFailure):
        await consult_model(
            env.runtime, env.rec, {"purpose": "analysis", "prompt": "Private objective"}
        )
    assert env.aux.calls == []


@pytest.mark.parametrize("mutation", ["cancel", "epoch", "provider", "revision", "steer"])
async def test_auxiliary_late_results_cannot_cross_owner_fences(env, mutation):
    started, release = asyncio.Event(), asyncio.Event()

    async def complete(messages):
        env.aux.calls.append(messages)
        started.set()
        await release.wait()
        return "Late synthetic result"

    env.aux.complete = complete
    pending = asyncio.create_task(
        consult_model(env.runtime, env.rec, {"purpose": "analysis", "prompt": "Public question"})
    )
    await started.wait()
    if mutation == "cancel":
        env.rec.cancel_requested = True
    elif mutation == "epoch":
        env.store.reset(env.session.session_id)
    elif mutation == "revision":
        env.rec.task.objective_revision += 1
    elif mutation == "steer":
        env.rec.pending_steer = object()
    else:
        env.aux.info = env.aux.info.model_copy(update={"model": "replacement"})
    release.set()
    with pytest.raises(ToolFailure):
        await pending
    assert env.rec.task.evidence == []


async def test_native_catalog_does_not_block_public_cloud_auxiliary(env):
    env.rec.task.content_scope = "public_synthetic"
    env.aux.info = env.aux.info.model_copy(update={"local": False})
    pin = env.rec.task.auxiliary_models["analysis"]
    env.rec.task.auxiliary_models["analysis"] = pin.model_copy(update={"local": False})
    env.rec.catalog_guards["native-only"] = CatalogGuard(env.runtime, env.rec)
    await consult_model(env.runtime, env.rec, {"purpose": "analysis", "prompt": "Public"})
    assert len(env.aux.calls) == 1


async def test_retained_foreign_context_is_not_redisclosed_to_cloud(env):
    env.rec.task.content_scope = "public_synthetic"
    env.rec.foreign_context = True
    env.aux.info = env.aux.info.model_copy(update={"local": False})
    pin = env.rec.task.auxiliary_models["analysis"]
    env.rec.task.auxiliary_models["analysis"] = pin.model_copy(update={"local": False})
    with pytest.raises(ToolFailure, match="local auxiliary"):
        await consult_model(env.runtime, env.rec, {"purpose": "analysis", "prompt": "Public"})
    assert env.aux.calls == []
    env.aux.info = env.aux.info.model_copy(update={"local": True})
    env.rec.task.auxiliary_models["analysis"] = pin
    await consult_model(env.runtime, env.rec, {"purpose": "analysis", "prompt": "Public"})
    assert len(env.aux.calls) == 1


@pytest.mark.parametrize("response", ["", "x" * 8001, "😀" * 5000])
async def test_invalid_auxiliary_output_is_not_evidence(env, response):
    env.aux.responses.clear()
    env.aux.responses.append(response)
    with pytest.raises(ToolFailure):
        await consult_model(env.runtime, env.rec, {"purpose": "analysis", "prompt": "Public"})
    assert env.rec.task.evidence == []


async def test_missing_task_pin_never_falls_back(env):
    env.rec.task.auxiliary_models.pop("analysis")
    with pytest.raises(ToolFailure, match="no available model pin"):
        await consult_model(env.runtime, env.rec, {"purpose": "analysis", "prompt": "Public"})
    assert env.aux.calls == env.primary.calls == []


async def test_unmeasured_adapter_constructs_once_and_is_closed():
    constructed, closed = [], []

    def unmeasured(effort):
        candidate = fixture("fast")
        candidate.prepare_complete = None

        async def close():
            closed.append(candidate)

        candidate.close = close
        constructed.append(candidate)
        return candidate

    settings = AgentModelSettings(
        fixture("primary"),
        {"fast": lambda effort: fixture("fast")},
        auxiliary_factories={"fast": unmeasured},
    )
    for _ in range(2):
        declaration = next(p for p in settings.snapshot()["profiles"] if p["id"] == "fast")
        assert declaration["auxiliary_selectable"]
        assert declaration["qualification"] == "not_checked"
        assert declaration["readiness"] == "unknown"
        assert constructed == closed == []
    with pytest.raises(ValueError, match="measured auxiliary"):
        settings.resolve_auxiliary({"profile_id": "fast", "reasoning": None})
    assert not next(p for p in settings.snapshot()["profiles"] if p["id"] == "fast")[
        "auxiliary_selectable"
    ]
    await settings.close()
    assert len(constructed) == 1 and closed == constructed


async def test_auxiliary_owner_api_auth_revision_eligibility_and_exact_retry(env):
    app = FastAPI()
    app.include_router(router(env.runtime, Auth({"owner": "o" * 32, "agent": "a" * 32})))
    body = change(
        env.settings, 1, "owner-save", purpose="compression", harness="seed-v1"
    ).model_dump()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
    ) as client:
        assert (await client.post("/v1/agent/options/models", json=body)).status_code == 401
        assert (
            await client.post(
                "/v1/agent/options/models",
                json=body,
                headers={"Authorization": "Bearer " + "a" * 32},
            )
        ).status_code == 403
        client.headers["Authorization"] = "Bearer " + "o" * 32
        unavailable = {**body, "harness_version": "v-" + "f" * 24}
        assert (await client.post("/v1/agent/options/models", json=unavailable)).status_code == 400
        saved = await client.post("/v1/agent/options/models", json=body)
        assert saved.status_code == 200
        receipt = saved.json()
        assert receipt["revision"] == 2 and receipt["auxiliary_task"] == "compression"
        assert (await client.post("/v1/agent/options/models", json=body)).json() == receipt
        assert (await client.get("/v1/agent/options/model-changes/owner-save")).json() == receipt
        stale = {**body, "idempotency_key": "stale"}
        assert (await client.post("/v1/agent/options/models", json=stale)).status_code == 409
        assert env.primary.calls == env.aux.calls == []


async def test_normal_agent_auxiliary_execution_and_future_pin_boundary(env):
    env.runtime._tasks.pop(env.rec.task.task_id)
    env.primary.responses.extend(
        [
            json.dumps(
                {
                    "tool": "consult_model",
                    "arguments": {"purpose": "analysis", "prompt": "Public question"},
                }
            ),
            json.dumps(
                {
                    "tool": "write_file",
                    "arguments": {"path": "answer.md", "content": "Synthetic auxiliary result"},
                }
            ),
            json.dumps(
                {
                    "tool": "finish",
                    "arguments": {"summary": "Saved answer", "objective_revision": 1},
                }
            ),
        ]
    )
    task = await env.runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Save a synthetic analysis result",
            idempotency_key="normal",
        )
    )
    assert task.auxiliary_models["analysis"].model == "fast"
    env.settings.change(change(env.settings, 1, "future-only", profile="host-default"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        task = await env.runtime.status(task.task_id)
        if task.state in {TaskState.SUCCEEDED, TaskState.FAILED}:
            break
    assert task.state == TaskState.SUCCEEDED, task.error
    assert len(env.aux.calls) == 1
    assert task.auxiliary_models["analysis"].settings_revision == 1
    assert env.settings.selection("seed-v1", "analysis")[1].model == "primary"
    assert (env.workspace / "answer.md").read_text() == "Synthetic auxiliary result"
    assert [e.kind for e in task.evidence] == ["external", "file"]
    assert any("Synthetic auxiliary result" in turn["content"] for turn in env.primary.calls[1])


async def test_auxiliary_route_persists_and_restores_inheritance(tmp_path):
    path = tmp_path / "models.sqlite"
    primary = fixture("primary")

    def settings():
        return AgentModelSettings(
            primary,
            {"fast": lambda effort: fixture("fast", effort)},
            path=path,
            reasoning_levels={"fast": ["low", "high"]},
        )

    before = settings()
    original = change(before, 0, "save", reasoning="high", harness="seed-v1", purpose="compression")
    receipt = before.change(original)
    await before.close()
    after = settings()
    assert after.change(original) == receipt
    assert after.selection("seed-v1", "compression")[1].reasoning == "high"
    after.change(
        change(after, 1, "inherit", harness="seed-v1", profile=None, purpose="compression")
    )
    assert after.selection("seed-v1", "compression")[1].model == "primary"
    await after.close()
