"""Issue #165 native publication, owner API, authority and recovery boundaries.

All inference is explicitly scripted. These checks use the real journal,
artifact SQLite store and supervisor, with no providers or external services.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    SessionRequest,
    SpeechWindow,
    SteerRequest,
    TaskRequest,
    TaskState,
)
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.agent.artifacts import ArtifactStore
from services.agent.plugin_controls import PluginControl, apply_plugin_control
from services.agent.plugin_service import PluginService
from services.common.auth import Auth
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store


def action(tool, **arguments):
    return json.dumps({"tool": tool, "arguments": arguments})


def publication(**updates):
    return {
        "title": "Synthetic comparison",
        "summary": "Explicitly simulated values for a local UI fixture.",
        "components": [
            {
                "id": "choices",
                "type": "select",
                "data": {
                    "label": "Scenario",
                    "options": ["A", "B"],
                    "value": "A",
                },
            },
            {
                "id": "facts",
                "type": "table",
                "data": {
                    "columns": ["Item", "Value"],
                    "rows": [["Fixture", 3], ["Synthetic", 5]],
                },
            },
            {
                "id": "followup",
                "type": "ask",
                "data": {
                    "label": "Ask more",
                    "message": "Explain the synthetic comparison",
                },
            },
        ],
        **updates,
    }


def finish(revision=1):
    return action(
        "finish", summary="Published the synthetic comparison", objective_revision=revision
    )


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = tmp_path / "operator"
    data.mkdir(mode=0o700)
    journal_store = Store(data / "journal.sqlite")
    session = journal_store.create_session(SessionRequest(channel="converse"))
    provider = ScriptedProvider([])
    artifacts = ArtifactStore(data / "artifacts.sqlite3")
    runtime = AgentRuntime(
        LocalJournal(journal_store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace, allow_write=True, yolo=True),
        artifact_store=artifacts,
    )
    tokens = {
        role: role + "-synthetic-artifact-token-0123456789"
        for role in (
            "owner",
            "agent",
            "converse",
            "decisions",
            "resources",
        )
    }
    result = SimpleNamespace(
        workspace=workspace,
        data=data,
        journal=journal_store,
        session=session,
        provider=provider,
        artifacts=artifacts,
        runtime=runtime,
        tokens=tokens,
        auth=Auth(tokens),
    )
    yield result
    await result.runtime.close()
    journal_store.close()


def request(env, key="synthetic-ui", **updates):
    return TaskRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        objective="Publish a synthetic comparison UI",
        idempotency_key=key,
        **updates,
    )


def scope(env, **updates):
    return {
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
        **updates,
    }


def headers(env, role="owner"):
    return {"Authorization": "Bearer " + env.tokens[role]}


def client(env):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env.runtime, env.auth)),
        base_url="http://synthetic",
        headers=headers(env),
    )


async def publish(env, key="synthetic-ui"):
    env.provider.responses.extend([action("publish_ui", **publication()), finish()])
    task = await env.runtime.submit(request(env, key))
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.SUCCEEDED, final.error
    return final, env.artifacts.page(**scope(env))["items"][-1]


async def wait_state(runtime, task_id, expected):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = await runtime.status(task_id)
        if task.state == expected:
            return task
        assert task.state not in {TaskState.FAILED, TaskState.CANCELLED, TaskState.SUCCEEDED}, task
        await asyncio.sleep(0.005)
    pytest.fail(f"Task did not reach {expected}")


async def test_publication_auth_scope_receipt_and_incremental_pages(env):
    task, envelope = await publish(env)
    evidence = task.evidence[-1]
    assert evidence.kind == "artifact"
    assert evidence.path == "artifact:" + envelope["artifact_id"]
    assert evidence.sha256 == envelope["content_sha256"]
    assert env.artifacts.verify(evidence, task)
    events = env.journal.read(session_id=task.session_id, kinds=["artifact.updated"])
    assert len(events) == 1
    assert "components" not in events[0].payload and "view_state" not in events[0].payload
    submit_sequence = env.runtime._get(task.task_id).submit_sequence
    assert submit_sequence > 0
    assert events[0].payload["task_submit_sequence"] == submit_sequence
    assert "publication only" in evidence.summary
    async with client(env) as api:
        assert "publish_ui" in (await api.get("/v1/agents/capabilities")).json()["tools"]
        page = await api.get("/v1/artifacts", params=scope(env))
        assert page.status_code == 200, page.text
        assert page.json()["items"][0]["task_submit_sequence"] == submit_sequence
        cursor = page.json()["next_after"]
        assert cursor > 0
        empty = await api.get("/v1/artifacts", params=scope(env, after=cursor))
        assert empty.json() == {"items": [], "next_after": cursor}
        path = "/v1/artifacts/" + envelope["artifact_id"]
        assert (await api.get(path, params=scope(env))).json()["content_sha256"] == evidence.sha256
        assert (await api.get(path, params=scope(env, session_id="other"))).status_code == 404
        assert (await api.get(path, params=scope(env, thread_id="other"))).status_code == 404
        assert (await api.get(path, params=scope(env, epoch=2))).status_code == 404
        assert (await api.get("/v1/artifacts", params=scope(env, limit=21))).status_code == 422
        assert (await api.get("/v1/artifacts", params=scope(env, after=-1))).status_code == 422
        assert (await api.get("/v1/artifacts")).status_code == 422
        assert (
            await api.get("/v1/artifacts", params=scope(env), headers={"Authorization": ""})
        ).status_code == 401
        for role in ("agent", "decisions", "converse", "resources"):
            assert (
                await api.get("/v1/artifacts", params=scope(env), headers=headers(env, role))
            ).status_code == 403
            assert (
                await api.get(path, params=scope(env), headers=headers(env, role))
            ).status_code == 403
            denied = await api.post(
                path + "/actions",
                json={
                    **scope(env),
                    "expected_revision": 1,
                    "component_id": "choices",
                    "value": "B",
                    "idempotency_key": "denied",
                },
                headers=headers(env, role),
            )
            assert denied.status_code == 403


async def test_actions_only_change_presentation_and_survive_restart(env):
    task, envelope = await publish(env)
    path = "/v1/artifacts/" + envelope["artifact_id"]
    body = {
        **scope(env),
        "expected_revision": 1,
        "component_id": "choices",
        "value": "B",
        "idempotency_key": "select-B",
    }
    inference_calls = len(env.provider.calls)
    async with client(env) as api:
        response = await api.post(path + "/actions", json=body)
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["view_state"] == {"choices": "B"}
        assert updated["view_revision"] == 1 and updated["revision"] == 1
        assert updated["content_sha256"] == envelope["content_sha256"]
        assert (await api.post(path + "/actions", json=body)).json() == updated
        assert (await api.post(path + "/actions", json={**body, "value": "A"})).status_code == 409
        assert (
            await api.post(path + "/actions", json={**body, "expected_revision": 2})
        ).status_code == 409
        assert (
            await api.post(path + "/actions", json={**body, "session_id": "other"})
        ).status_code == 404
        assert (await api.post(path + "/actions", json={**body, "extra": "no"})).status_code == 422
        assert (
            await api.post(path + "/actions", json={**body, "expected_revision": True})
        ).status_code == 422
        assert (
            await api.post(
                path + "/actions", json={**body, "value": "unknown", "idempotency_key": "unknown"}
            )
        ).status_code == 400
        assert (
            await api.post(
                path + "/actions",
                json={**body, "component_id": "followup", "idempotency_key": "ask"},
            )
        ).status_code == 400
    assert len(env.provider.calls) == inference_calls
    assert env.artifacts.verify(task.evidence[-1], task)
    await env.runtime.close()
    env.artifacts = ArtifactStore(env.data / "artifacts.sqlite3")
    env.provider = ScriptedProvider([])
    env.runtime = AgentRuntime(
        LocalJournal(env.journal, "agent"),
        env.provider,
        env.workspace,
        ToolPolicy(env.workspace),
        artifact_store=env.artifacts,
    )
    await env.runtime.recover()
    async with client(env) as api:
        got = await api.get(path, params=scope(env))
        assert got.status_code == 200
        assert got.json()["view_state"] == {"choices": "B"}
        assert got.json()["task_submit_sequence"] > 0
        assert (await api.post(path + "/actions", json=body)).json() == updated
    assert not env.provider.calls


async def test_unconfigured_store_exposes_no_tool_and_does_not_create_storage(env):
    await env.runtime.close()
    env.runtime = AgentRuntime(
        LocalJournal(env.journal, "agent"),
        env.provider,
        env.workspace,
        ToolPolicy(env.workspace),
    )
    async with client(env) as api:
        assert "publish_ui" not in (await api.get("/v1/agents/capabilities")).json()["tools"]
        response = await api.get("/v1/artifacts", params=scope(env))
        assert response.status_code == 503
        assert response.json() == {"detail": "UI artifacts are not configured"}
    env.provider.responses.extend([action("publish_ui", **publication()), finish()])
    task = await env.runtime.submit(request(env))
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.FAILED
    assert final.error == "finish with no current-revision evidence"
    assert not final.evidence
    assert "UI artifacts: not configured" in env.provider.calls[0][1]["content"]
    assert "publish_ui" not in env.provider.calls[0][0]["content"]
    assert not list(env.workspace.iterdir())


async def test_uncommitted_speech_publication_spends_exact_owner_approval_even_yolo(env):
    window = SpeechWindow(
        **scope(env),
        utterance_id="synthetic-ui-window",
        window_index=0,
        revision=1,
        text="Publish a synthetic comparison UI",
        start_ms=0,
        end_ms=1000,
        user_speaking=False,
        capture_ended=False,
        content_scope="public_synthetic",
    )
    source = env.journal.append(
        AppendRequest(
            **scope(env),
            kind="conversation.window",
            payload=window.model_dump(mode="json"),
            idempotency_key="synthetic-ui-window",
        ),
        "owner",
    )
    env.provider.responses.extend([action("publish_ui", **publication()), finish()])
    task = await env.runtime.submit(
        request(env, source_event_ids=[source.event_id]), actor="decisions"
    )
    await wait_state(env.runtime, task.task_id, TaskState.AWAITING_APPROVAL)
    assert env.artifacts.page(**scope(env))["items"] == []
    pending = env.runtime._get(task.task_id).pending
    assert pending["tool"] == "publish_ui" and pending["reason"] == "uncommitted_speech"
    await env.runtime.resolve_approval(
        pending["approval_id"],
        ApprovalResolution(
            approved=True,
            action_digest=pending["digest"],
            objective_revision=1,
        ),
    )
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert env.runtime.policy.peek_approval(pending["approval_id"]).used is True


class HeldFinish(ScriptedProvider):
    def __init__(self):
        super().__init__([action("publish_ui", **publication()), finish()])
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages):
        if len(self.calls) == 1:
            self.waiting.set()
            await self.release.wait()
        return await super().complete(messages)


@pytest.mark.parametrize(
    "corruption", ["wrong_task", "wrong_session", "tamper_hash", "tamper_storage"]
)
async def test_finish_rejects_foreign_or_tampered_publication_evidence(env, corruption):
    provider = HeldFinish()
    env.provider = provider
    env.runtime.provider = provider
    task = await env.runtime.submit(request(env))
    await asyncio.wait_for(provider.waiting.wait(), timeout=3)
    rec = env.runtime._get(task.task_id)
    evidence = rec.task.evidence[-1]
    if corruption in {"wrong_task", "wrong_session"}:
        updates = (
            {"task_id": "other-task"}
            if corruption == "wrong_task"
            else {"session_id": "other-session"}
        )
        other = env.artifacts.publish(rec.task.model_copy(update=updates), publication())
        evidence.path = "artifact:" + other["artifact_id"]
        evidence.sha256 = other["content_sha256"]
    elif corruption == "tamper_hash":
        evidence.sha256 = "0" * 64
    else:
        env.artifacts._conn.execute("UPDATE artifacts SET summary=?", ("tampered native storage",))
    provider.release.set()
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.FAILED
    assert "UI publication no longer matches" in final.error


async def test_finish_checks_latest_receipt_and_terminal_task_cannot_update(env):
    class UpdatingProvider(ScriptedProvider):
        async def complete(self, messages):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return action("publish_ui", **publication())
            if len(self.calls) == 2:
                envelope = env.artifacts.page(**scope(env))["items"][0]
                return action(
                    "publish_ui",
                    artifact_id=envelope["artifact_id"],
                    expected_revision=1,
                    updates=[
                        {
                            "id": "choices",
                            "data": {"label": "Scenario", "options": ["A", "B"], "value": "B"},
                        }
                    ],
                )
            return finish()

    env.provider = UpdatingProvider([])
    env.runtime.provider = env.provider
    task = await env.runtime.submit(request(env))
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert len(final.evidence) == 2
    assert not env.artifacts.verify(final.evidence[0], final)
    assert env.artifacts.verify(final.evidence[1], final)
    envelope = env.artifacts.page(**scope(env))["items"][0]
    await env.runtime._execute(
        env.runtime._get(task.task_id),
        "publish_ui",
        {
            "artifact_id": envelope["artifact_id"],
            "expected_revision": 2,
            "updates": [
                {"id": "choices", "data": {"label": "Late", "options": ["A"], "value": "A"}}
            ],
        },
        "",
    )
    assert env.artifacts.get(envelope["artifact_id"], **scope(env))["revision"] == 2


@pytest.mark.parametrize("effect_order", ["publish_read", "write_publish", "publish_write"])
async def test_unrelated_success_cannot_hide_failed_publication_or_work_effect(env, effect_order):
    (env.workspace / "known.txt").write_text("Synthetic known content")
    bad_publish = action(
        "publish_ui", **publication(components=[{"id": "bad", "type": "html", "data": {}}])
    )
    good_publish = action("publish_ui", **publication())
    good_write = action("write_file", path="created.txt", content="Synthetic new content")
    bad_write = action("write_file", path="../escaped.txt", content="Must not be written")
    scripts = {
        "publish_read": [bad_publish, action("read_file", path="known.txt")],
        "write_publish": [bad_write, good_publish],
        "publish_write": [bad_publish, good_write],
    }
    env.provider.responses.extend([*scripts[effect_order], finish()])
    task = await env.runtime.submit(request(env))
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.FAILED
    assert "required effect failed" in final.error
    assert env.runtime._get(task.task_id).failed_effect is True


@pytest.mark.parametrize("kind", ["artifact.updated", "agent.tool", "agent.outcome"])
@pytest.mark.parametrize("commit_then_error", [False, True])
async def test_journal_partial_publication_is_truthful_and_never_retried(
    env, kind, commit_then_error
):
    delegate = env.runtime.journal

    class FailingJournal:
        def __getattr__(self, name):
            return getattr(delegate, name)

        async def append(self, body):
            if body.kind == kind:
                if commit_then_error:
                    await delegate.append(body)
                raise RuntimeError("synthetic-secret-value-must-not-escape")
            return await delegate.append(body)

    env.runtime.journal = FailingJournal()
    env.provider.responses.extend(
        [action("publish_ui", **publication()), finish(), action("publish_ui", **publication())]
    )
    task = await env.runtime.submit(request(env))
    final = await env.runtime.wait(task.task_id, timeout=3)
    if kind == "agent.outcome":
        # The same completion protocol now positively reconciles the exact
        # committed outcome; absent confirmation remains paused, never a
        # conflicting local/durable failure or an automatic retry.
        assert final.state == (TaskState.SUCCEEDED if commit_then_error else TaskState.PAUSED)
        if not commit_then_error:
            assert "unconfirmed" in final.error and "without retry" in final.error
            assert "secret" not in final.error
    else:
        assert final.state == TaskState.FAILED
        assert "was stored" in final.error and "without retry" in final.error
        assert "secret" not in final.error
    assert len(env.artifacts.page(**scope(env))["items"]) == 1
    assert len(env.provider.calls) == (2 if kind == "agent.outcome" else 1)
    if kind == "agent.outcome" and commit_then_error:
        # Confirmed local status and recovery use the same native receipt.
        outcomes = env.journal.read(session_id=env.session.session_id, kinds=["agent.outcome"])
        assert len(outcomes) == 1 and outcomes[0].payload["state"] == "succeeded"
        recovered = AgentRuntime(
            delegate,
            ScriptedProvider([]),
            env.workspace,
            ToolPolicy(env.workspace),
            artifact_store=ArtifactStore(env.data / "artifacts.sqlite3"),
        )
        try:
            await recovered.recover()
            assert (await recovered.status(task.task_id)).state == TaskState.SUCCEEDED
        finally:
            await recovered.close()


async def test_unconfirmed_storage_outcome_is_not_blindly_repeated(env, monkeypatch):
    original = env.artifacts.publish

    def uncertain(*args):
        original(*args)
        raise RuntimeError("synthetic-storage-secret")

    monkeypatch.setattr(env.artifacts, "publish", uncertain)
    env.provider.responses.extend(
        [action("publish_ui", **publication()), action("publish_ui", **publication()), finish()]
    )
    task = await env.runtime.submit(request(env))
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.FAILED
    assert "storage outcome is unconfirmed" in final.error
    assert "secret" not in final.error
    assert len(env.provider.calls) == 1
    assert len(env.artifacts.page(**scope(env))["items"]) == 1


async def test_api_storage_errors_are_bounded_and_do_not_reflect_exception_data(env, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic-storage-secret SQL and private path")

    monkeypatch.setattr(env.artifacts, "get", unavailable)
    async with client(env) as api:
        response = await api.get("/v1/artifacts/synthetic", params=scope(env))
        assert response.status_code == 503
        assert response.json() == {"detail": "UI artifact storage is unavailable"}


async def test_native_submit_order_survives_late_older_publication(env):
    gate = asyncio.Event()
    entered = asyncio.Event()

    class LateProvider(ScriptedProvider):
        async def complete(self, messages):
            self.calls.append(messages)
            if "older synthetic UI" in messages[1]["content"] and not gate.is_set():
                entered.set()
                await gate.wait()
            if len(messages) == 2:
                return action("publish_ui", **publication())
            return finish()

    env.runtime.provider = LateProvider([])
    older = request(env, "older").model_copy(update={"objective": "older synthetic UI"})
    old_task = await env.runtime.submit(older)
    await asyncio.wait_for(entered.wait(), timeout=3)
    new_task = await env.runtime.submit(request(env, "newer"))
    assert (await env.runtime.wait(new_task.task_id, timeout=3)).state == TaskState.SUCCEEDED
    gate.set()
    assert (await env.runtime.wait(old_task.task_id, timeout=3)).state == TaskState.SUCCEEDED
    async with client(env) as api:
        items = (await api.get("/v1/artifacts", params=scope(env))).json()["items"]
    assert [item["task_id"] for item in items] == [new_task.task_id, old_task.task_id]
    assert items[0]["task_submit_sequence"] > items[1]["task_submit_sequence"] > 0


async def test_epoch_reset_during_inference_cannot_publish(env):
    gate = asyncio.Event()
    entered = asyncio.Event()

    class ResetProvider(ScriptedProvider):
        async def complete(self, messages):
            entered.set()
            await gate.wait()
            return action("publish_ui", **publication())

    env.runtime.provider = ResetProvider([])
    task = await env.runtime.submit(request(env))
    await asyncio.wait_for(entered.wait(), timeout=3)
    env.journal.reset(env.session.session_id)
    gate.set()
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.FAILED
    assert env.artifacts.page(**scope(env))["items"] == []


def publication_authority_boundary(env, monkeypatch, boundary, invalidation):
    """Change native state after a final authority read captured its response."""
    window = SpeechWindow(
        **scope(env),
        utterance_id="publication-authority",
        window_index=0,
        revision=1,
        text="Publish a synthetic comparison UI",
        start_ms=0,
        end_ms=1000,
        user_speaking=False,
        capture_ended=True,
        content_scope="public_synthetic",
    )
    source = env.journal.append(
        AppendRequest(
            **scope(env),
            kind="conversation.window",
            payload=window.model_dump(mode="json"),
            idempotency_key="publication-authority",
        ),
        "owner",
    )
    delegate = env.runtime.journal
    state = SimpleNamespace(phase="", speech_reads=0, invalidated=False)
    original_speech = env.runtime._uncommitted_speech_sources
    original_epoch = env.runtime._epoch_error

    async def invalidate():
        state.invalidated = True
        if invalidation == "reset":
            env.journal.reset(env.session.session_id)
        elif invalidation == "revision":
            revised = window.model_copy(update={"revision": 2, "text": "Corrected synthetic UI"})
            env.journal.append(
                AppendRequest(
                    **scope(env),
                    kind="conversation.window",
                    payload=revised.model_dump(mode="json"),
                    idempotency_key="publication-authority-correction",
                ),
                "owner",
            )
        else:
            task_id = next(iter(env.runtime._tasks))
            await env.runtime.steer(
                task_id,
                SteerRequest(
                    instruction="Replace the earlier UI with a revised synthetic comparison",
                    expected_objective_revision=1,
                    idempotency_key="publication-authority-steer",
                ),
            )

    class BoundaryJournal:
        def __getattr__(self, name):
            return getattr(delegate, name)

        async def resolve(self, event_id):
            captured = await delegate.resolve(event_id)
            if state.phase == boundary and boundary != "session" and not state.invalidated:
                await invalidate()
            return captured

        async def session(self, session_id):
            captured = await delegate.session(session_id)
            if boundary == "session" and state.phase == "session" and not state.invalidated:
                await invalidate()
            return captured

    async def speech_authority(rec):
        state.speech_reads += 1
        state.phase = "speech" if state.speech_reads == 2 else ""
        try:
            return await original_speech(rec)
        finally:
            state.phase = ""

    async def revision_authority(rec):
        final_read = state.speech_reads == 2 and not state.invalidated
        if final_read:
            state.phase = "revision"
        try:
            return await original_epoch(rec)
        finally:
            state.phase = "session" if final_read else ""

    env.runtime.journal = BoundaryJournal()
    monkeypatch.setattr(env.runtime, "_uncommitted_speech_sources", speech_authority)
    monkeypatch.setattr(env.runtime, "_epoch_error", revision_authority)
    return source, state


@pytest.mark.parametrize(
    ("boundary", "invalidation"),
    [("speech", "reset"), ("speech", "revision"), ("revision", "reset")],
)
async def test_final_source_authority_await_cannot_persist_stale_publication(
    env, monkeypatch, boundary, invalidation
):
    source, state = publication_authority_boundary(env, monkeypatch, boundary, invalidation)
    env.provider.responses.extend([action("publish_ui", **publication()), finish()])
    task = await env.runtime.submit(
        request(env, source_event_ids=[source.event_id]), actor="decisions"
    )
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert state.invalidated, "Test did not reach the final authority await"
    assert final.state == TaskState.FAILED, final
    assert env.artifacts.page(**scope(env))["items"] == []
    assert env.journal.read(task_id=task.task_id, kinds=["artifact.updated"]) == []
    assert not any(evidence.kind == "artifact" for evidence in final.evidence)


@pytest.mark.parametrize("boundary", ["speech", "session"])
async def test_steer_during_final_authority_await_discards_old_publication(
    env, monkeypatch, boundary
):
    source, state = publication_authority_boundary(env, monkeypatch, boundary, "steer")
    env.provider.responses.extend(
        [
            action("publish_ui", **publication(title="Obsolete synthetic result")),
            action("publish_ui", **publication(title="Replanned synthetic result")),
            finish(2),
        ]
    )
    task = await env.runtime.submit(
        request(env, source_event_ids=[source.event_id]), actor="decisions"
    )
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert state.invalidated, "Test did not reach the final authority await"
    assert final.state == TaskState.SUCCEEDED, final.error
    assert final.objective_revision == 2
    artifacts = env.artifacts.page(**scope(env))["items"]
    assert len(artifacts) == 1
    assert artifacts[0]["title"] == "Replanned synthetic result"
    assert artifacts[0]["objective_revision"] == 2
    events = env.journal.read(task_id=task.task_id, kinds=["artifact.updated"])
    assert len(events) == 1 and events[0].payload["objective_revision"] == 2


async def test_steered_revision_cannot_reuse_earlier_artifact_receipt(env):
    provider = HeldFinish()
    env.runtime.provider = provider
    task = await env.runtime.submit(request(env))
    await asyncio.wait_for(provider.waiting.wait(), timeout=3)
    await env.runtime.steer(
        task.task_id,
        SteerRequest(
            instruction="Use a revised synthetic comparison",
            expected_objective_revision=1,
            idempotency_key="revise-ui",
        ),
    )
    provider.responses.append(finish(2))
    provider.release.set()
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.FAILED
    assert final.objective_revision == 2
    assert final.error == "finish with no current-revision evidence"


@pytest.mark.parametrize("workspace_symlink", [False, True])
async def test_actual_launcher_configures_protected_storage_or_refuses_workspace_target(
    env, monkeypatch, workspace_symlink
):
    import uvicorn

    import packages.contracts.client
    import services.agent
    from packages.commands import Registry
    from services import launcher

    await env.runtime.close()
    if workspace_symlink:
        (env.data / "artifacts.sqlite3").unlink()
        (env.data / "artifacts.sqlite3").symlink_to(env.workspace / "mutable.sqlite3")
    constructed = []
    registries = []

    def runtime(*args, **kwargs):
        value = AgentRuntime(*args, **kwargs)
        constructed.append(value)
        return value

    def registry(*args, **kwargs):
        value = Registry(*args, **kwargs)
        registries.append(value)
        return value

    monkeypatch.setattr(
        launcher,
        "load_config",
        lambda _: {
            "session_url": "http://synthetic",
            "providers": {
                "agent": {
                    "kind": "compatible",
                    "model": "synthetic-model",
                    "base_url": "http://127.0.0.1:9999/v1",
                }
            },
        },
    )
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: env.tokens)
    monkeypatch.setattr(launcher, "provider", lambda *args, **kwargs: ScriptedProvider([]))
    monkeypatch.setattr(
        packages.contracts.client, "JournalClient", lambda *args: LocalJournal(env.journal, "agent")
    )
    monkeypatch.setattr(services.agent, "AgentRuntime", runtime)
    monkeypatch.setattr("packages.commands.Registry", registry)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    args = SimpleNamespace(
        service="agent", data_dir=env.data, config=None, workspace=env.workspace, port=None
    )
    try:
        if workspace_symlink:
            with pytest.raises(SystemExit, match="outside the worker workspace"):
                launcher.serve(args)
            assert not constructed and not (env.workspace / "mutable.sqlite3").exists()
        else:
            launcher.serve(args)
            assert len(constructed) == 1
            assert constructed[0].artifact_store._path == env.data / "artifacts.sqlite3"
            assert not constructed[0].artifact_store._path.resolve().is_relative_to(env.workspace)
    finally:
        for value in constructed:
            await value.close()
        for value in registries:
            value.close()


async def test_compute_plugin_publication_outcome_announces_only_native_ui_effect(env):
    suite = env.data / "synthetic-adder-suite.json"
    suite.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "plugin_id": "adder",
                "cases": [{"input": {"a": 40, "b": 2}, "expected": {"sum": 42}}],
            }
        )
    )
    service = PluginService(env.data / "plugins", env.workspace, {"adder": suite})
    env.runtime.plugin_service = service
    manifest = {
        "plugin_id": "adder",
        "name": "Synthetic addition",
        "description": "Local fixture sum",
        "author": "synthetic-fixture",
        "license": "MIT",
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"sum": {"type": "integer"}},
            "required": ["sum"],
            "additionalProperties": False,
        },
    }
    version = service.registry.register_bytes(
        b"def run(arguments):\n    return {'sum': arguments['a'] + arguments['b']}\n", manifest
    )
    report = await service.evaluate(version)
    assert report["passed"]
    await apply_plugin_control(
        env.runtime,
        PluginControl(
            operation="promote",
            version_id=version,
            report_id=report["report_id"],
            reviewed=True,
            idempotency_key="promote-synthetic-adder",
        ),
    )
    env.provider.responses.extend(
        [
            action("plugin_call", plugin_id="adder", arguments={"a": 40, "b": 2}),
            action(
                "publish_ui",
                **publication(
                    components=[
                        {
                            "id": "sum",
                            "type": "text",
                            "data": {"text": "Synthetic local sum: 42"},
                        }
                    ]
                ),
            ),
            action(
                "finish",
                summary="Sent an external payment and modified a remote account",
                objective_revision=1,
            ),
        ]
    )
    submitted = await env.runtime.submit(request(env))
    task = await env.runtime.wait(submitted.task_id, timeout=5)
    assert task.state == TaskState.SUCCEEDED, task.error
    assert [e.kind for e in task.evidence] == ["extension", "artifact"]
    assert env.artifacts.verify(task.evidence[-1], task)
    outcome = env.journal.read(task_id=task.task_id, kinds=["agent.outcome"])[-1]
    assert outcome.origin == "agent-supervisor"
    provider = ScriptedProvider([])
    converse = ConverseRuntime(LocalJournal(env.journal, "converse"), provider)
    try:
        notice = await converse.outcome_notice(outcome)
        assert notice is not None
        assert notice.payload["text"] == (
            "The Agent published a verified UI result. Its receipt confirms UI publication only."
        )
        assert "external payment" not in notice.payload["text"]
        assert "remote account" not in notice.payload["text"]
        assert notice.payload["outcome_event_id"] == outcome.event_id
        assert notice.task_refs == [task.task_id]
        assert len(env.provider.calls) == 3 and not provider.calls
    finally:
        await converse.close()


@pytest.mark.parametrize("invalid", ["path", "digest", "revision", "origin", "inspection"])
async def test_converse_refuses_malformed_or_unauthenticated_artifact_receipts(env, invalid):
    task, _ = await publish(env)
    outcome = env.journal.read(task_id=task.task_id, kinds=["agent.outcome"])[-1]
    payload = json.loads(json.dumps(outcome.payload))
    artifact = payload["task"]["evidence"][-1]
    if invalid == "path":
        artifact["path"] = "untrusted-receipt"
    elif invalid == "digest":
        artifact["sha256"] = "not-a-native-digest"
    elif invalid == "revision":
        artifact["objective_revision"] = task.objective_revision + 1
    elif invalid == "inspection":
        # Even an authenticated succeeded envelope cannot turn metadata
        # inspection into evidence of the requested UI/work effects.
        payload["task"]["evidence"] = [
            {
                **artifact,
                "kind": "inspection",
                "path": None,
                "output": '{"inspection_only":true,"metadata_omitted":true}',
            }
        ]
    modified = outcome.model_copy(
        update={
            "payload": payload,
            "origin": "authenticated-user" if invalid == "origin" else outcome.origin,
        }
    )
    converse = ConverseRuntime(LocalJournal(env.journal, "converse"), ScriptedProvider([]))
    try:
        with pytest.raises(ValueError, match="evidenced|independent"):
            await converse.outcome_notice(modified)
        assert not env.journal.read(kinds=["conversation.converse"])
    finally:
        await converse.close()
