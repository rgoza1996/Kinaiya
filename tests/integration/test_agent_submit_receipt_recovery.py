"""#192: native submit receipts survive restart without new inference admission.

SQLite, LocalJournal and HTTP authentication are real. Providers are explicit
synthetic fixtures; no model, external API or controller is contacted.
"""

import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.commands import Registry
from packages.contracts.models import (
    AppendRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    TaskRequest,
    TaskState,
)
from packages.providers.text import ScriptedProvider
from services.agent.app import create_app
from services.agent.model_settings import AgentModelSettings, BoundModelSettingsChange
from services.agent.runtime import AgentRuntime, IdemConflict, StaleRevision
from services.agent.skill_service import SkillService
from services.common.auth import Auth
from services.session.store import LocalJournal, Store

OBJECTIVE = "Read the synthetic fixture.txt file."
AUTH = Auth(
    {
        role: letter * 32
        for role, letter in (("owner", "o"), ("decisions", "d"), ("agent", "a"), ("resources", "r"))
    }
)


class ProbeProvider(ScriptedProvider):
    def __init__(self, *, local, label):
        super().__init__(
            [
                json.dumps({"tool": "read_file", "arguments": {"path": "fixture.txt"}}),
                json.dumps(
                    {
                        "tool": "finish",
                        "arguments": {"summary": "Read synthetic fixture", "objective_revision": 1},
                    }
                ),
            ]
        )
        self._info = ProviderInfo(provider="synthetic-receipt-fixture", model=label, local=local)
        self.armed = False
        self.metadata_reads = 0

    @property
    def info(self):
        if self.armed:
            self.metadata_reads += 1
            raise AssertionError("Receipt recovery must not select a current provider identity")
        return self._info


def append_source(
    store, session, *, kind="user", scope="local_only", revision=1, index=0, text=OBJECTIVE
):
    payload = {"text": text, "final": True, "revision": revision, "content_scope": scope}
    if kind == "window":
        payload = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="synthetic-utterance",
            window_index=index,
            revision=revision,
            text=text,
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope=scope,
        ).model_dump(mode="json")
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation." + kind,
            payload=payload,
            idempotency_key=f"source-{kind}-{index}-{revision}",
        ),
        "owner",
    )


def request(session, source=None, *, scope="local_only", key="accepted-submit"):
    return TaskRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective=OBJECTIVE,
        content_scope=scope,
        idempotency_key=key,
        source_event_ids=[source.event_id] if source is not None else [],
    )


@asynccontextmanager
async def restarted(
    tmp_path,
    *,
    initial_local=True,
    current_local=False,
    actor="owner",
    source_kind="user",
    pinned=True,
    terminal=False,
    explicit_skill=None,
    retired_profile=False,
    legacy=False,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("Public synthetic receipt fixture.\n")
    path = tmp_path / "journal.sqlite"
    models_path = tmp_path / "models.sqlite"
    store = Store(path)
    session = store.create_session(
        SessionRequest(channel="converse" if source_kind == "window" else "agent")
    )
    scope = "local_only" if initial_local else "public_synthetic"
    source = append_source(store, session, kind=source_kind, scope=scope) if source_kind else None
    body = request(session, source, scope=scope)
    initial = ProbeProvider(
        local=initial_local, label="accepted-local" if initial_local else "accepted-cloud"
    )
    runtime = AgentRuntime(LocalJournal(store, "agent"), initial, workspace, ToolPolicy(workspace))
    try:
        if explicit_skill is not None:
            runtime.skill_service = SkillService(tmp_path / "skills", workspace)
        if pinned:
            runtime.model_settings = AgentModelSettings(
                initial,
                {"retired-profile": lambda effort: initial} if retired_profile else {},
                path=models_path,
            )
            if retired_profile:
                authority = runtime.model_settings.snapshot()["write_authority"]
                runtime.model_settings.change(
                    BoundModelSettingsChange(
                        schema_version="1.1",
                        expected_backend=authority["backend_id"],
                        expected_instance=authority["instance_id"],
                        expected_revision=0,
                        idempotency_key="select-original",
                        profile_id="retired-profile",
                    )
                )
        accepted = await runtime.submit(body, actor=actor, explicit_skill=explicit_skill)
        pin = accepted.model_pin.model_copy(deep=True) if accepted.model_pin else None
        if terminal:
            settled = await runtime.wait(accepted.task_id, timeout=3)
            assert settled.state == TaskState.SUCCEEDED
            assert len(initial.calls) == 2
        await runtime.close()
        if not terminal:
            assert initial.calls == [], "acceptance is closed before its worker can run"
        if legacy:
            # A historical pre-scope submit hash omitted only content_scope.
            entry = store.read(task_id=accepted.task_id, kinds=["agent.command"])[0]
            old = body.model_dump(exclude={"schema_version", "idempotency_key", "content_scope"})
            value = entry.model_dump(mode="json")
            original = json.loads(
                store.db.execute(
                    "SELECT request FROM entries WHERE event_id=?", (entry.event_id,)
                ).fetchone()[0]
            )
            legacy_digest = hashlib.sha256(
                json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            # Both native representations must describe the same historical submit.
            for payload in (value["payload"], original["payload"]):
                payload.pop("requested_content_scope")
                payload.pop("model_pin", None)
                payload["payload_digest"] = legacy_digest
            with store.db:
                store.db.execute(
                    "UPDATE entries SET value=?, request=? WHERE event_id=?",
                    (json.dumps(value), json.dumps(original), entry.event_id),
                )
            pin = None
        store.close()
        store = Store(path)
        current = ProbeProvider(
            local=current_local, label="current-local" if current_local else "current-cloud"
        )
        journal = LocalJournal(store, "agent")
        runtime = AgentRuntime(journal, current, workspace, ToolPolicy(workspace))
        # The persisted profile may now be absent; reopening the owner does not
        # select it. In-memory old pins stay native output, never fabricated.
        runtime.model_settings = AgentModelSettings(current, {}, path=models_path)
        await runtime.recover()
        retained = await runtime.status(accepted.task_id)
        assert retained.model_pin == pin
        assert retained.state == (TaskState.SUCCEEDED if terminal else TaskState.PAUSED)
        assert current.calls == []
        yield SimpleNamespace(
            store=store,
            session=session,
            source=source,
            body=body,
            runtime=runtime,
            journal=journal,
            provider=current,
            retained=retained,
            pin=pin,
            workspace=workspace,
        )
    finally:
        await runtime.close()
        store.close()


def snapshot(env):
    settings = env.runtime.model_settings.db
    return (
        tuple(
            tuple(row) for row in env.store.db.execute("SELECT * FROM sessions ORDER BY session_id")
        ),
        tuple(
            tuple(row) for row in env.store.db.execute("SELECT * FROM entries ORDER BY sequence")
        ),
        env.store.db.total_changes,
        tuple(settings.execute("SELECT * FROM settings")),
        tuple(settings.execute("SELECT * FROM operations ORDER BY key")),
        settings.total_changes,
        {key: value.task.model_dump(mode="json") for key, value in env.runtime._tasks.items()},
        dict(env.runtime._submit_idem),
    )


def guard_receipt_only(env, monkeypatch, *, provider_identity=True):
    env.provider.armed = provider_identity
    sync = [
        (env.runtime, "session_harness"),
        (env.runtime, "_run"),
        (env.runtime.model_settings, "selection"),
        (env.runtime.model_settings, "resolve"),
        (env.runtime.model_settings, "change"),
    ]
    asynchronous = [
        (env.provider, "prepare_complete"),
        (env.provider, "complete"),
        (env.runtime, "_complete"),
        (env.journal, "append"),
        (env.journal, "agent_export_admission"),
    ]
    traps = []
    for owner, name in sync + asynchronous:
        cls = AsyncMock if (owner, name) in asynchronous else Mock
        trap = cls(side_effect=AssertionError(f"Receipt recovery attempted {name}"))
        monkeypatch.setattr(owner, name, trap)
        traps.append(trap)
    return traps


def assert_no_effects(env, before, traps):
    assert snapshot(env) == before
    assert env.provider.calls == []
    assert env.provider.metadata_reads == 0
    assert all(rec.worker is None for rec in env.runtime._tasks.values())
    for trap in traps:
        trap.assert_not_called()


@pytest.mark.parametrize("source_kind", [None, "user"])
async def test_native_local_receipt_recovery_survives_a_cloud_default_without_traps(
    tmp_path, source_kind
):
    async with restarted(tmp_path, source_kind=source_kind) as env:
        before = snapshot(env)
        assert await env.runtime.submit(env.body) == env.retained
        assert snapshot(env) == before
        assert env.provider.calls == []


@pytest.mark.parametrize("initial_local,current_local", [(True, False), (False, True)])
@pytest.mark.parametrize("actor", ["owner", "decisions"])
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
async def test_restart_exact_submit_is_a_receipt_not_current_provider_admission(
    tmp_path, monkeypatch, initial_local, current_local, actor, pinned, terminal
):
    async with restarted(
        tmp_path,
        initial_local=initial_local,
        current_local=current_local,
        actor=actor,
        pinned=pinned,
        terminal=terminal,
    ) as env:
        traps = guard_receipt_only(env, monkeypatch)
        before = snapshot(env)
        for _ in range(3):
            recovered = await env.runtime.submit(env.body, actor=actor)
            assert recovered == env.retained
            assert recovered.model_pin == env.pin
        assert_no_effects(env, before, traps)


@pytest.mark.parametrize("source_kind", [None, "window"])
async def test_exact_receipt_needs_no_current_profile_or_provider_factory(
    tmp_path, monkeypatch, source_kind
):
    async with restarted(tmp_path, source_kind=source_kind, retired_profile=True) as env:
        assert env.pin.profile_id == "retired-profile"
        assert "retired-profile" not in env.runtime.model_settings.base
        factory = Mock(
            side_effect=AssertionError("Receipt must not construct a replacement provider")
        )
        env.runtime.model_settings.host_factory = factory
        traps = guard_receipt_only(env, monkeypatch)
        before = snapshot(env)
        assert await env.runtime.submit(env.body) == env.retained
        assert_no_effects(env, before, traps)
        factory.assert_not_called()


async def test_pre_scope_legacy_hash_and_unknown_pin_recover_without_export(tmp_path, monkeypatch):
    async with restarted(tmp_path, source_kind=None, pinned=False, legacy=True) as env:
        assert env.retained.model_pin is None
        traps = guard_receipt_only(env, monkeypatch)
        before = snapshot(env)
        assert await env.runtime.submit(env.body) == env.retained
        with pytest.raises(IdemConflict):
            await env.runtime.submit(
                env.body.model_copy(update={"content_scope": "public_synthetic"})
            )
        assert_no_effects(env, before, traps)


@pytest.mark.parametrize(
    "change",
    [
        {"objective": "Different literal objective"},
        {"content_scope": "public_synthetic"},
        {"source_revision": 2},
    ],
)
async def test_exact_key_still_rejects_changed_submit_body(tmp_path, monkeypatch, change):
    async with restarted(tmp_path, current_local=True, source_kind=None) as env:
        traps = guard_receipt_only(env, monkeypatch, provider_identity=False)
        before = snapshot(env)
        with pytest.raises(IdemConflict):
            await env.runtime.submit(env.body.model_copy(update=change))
        assert_no_effects(env, before, traps)


@pytest.mark.parametrize("change", ["owner-objective", "valid-source-id"])
async def test_owner_source_resolution_cannot_normalize_a_conflicting_submit(
    tmp_path, monkeypatch, change
):
    async with restarted(tmp_path, current_local=True) as env:
        if change == "owner-objective":
            body = env.body.model_copy(update={"objective": "A different owner objective"})
        else:
            other = append_source(env.store, env.session, index=1)
            assert other.event_id != env.source.event_id
            body = env.body.model_copy(update={"source_event_ids": [other.event_id]})
        traps = guard_receipt_only(env, monkeypatch, provider_identity=False)
        before = snapshot(env)
        with pytest.raises(IdemConflict):
            await env.runtime.submit(body, actor="owner")
        assert_no_effects(env, before, traps)


@pytest.mark.parametrize("changed_skill", [None, "different-skill"])
async def test_original_explicit_skill_identity_cannot_be_omitted_or_changed(
    tmp_path, monkeypatch, changed_skill
):
    async with restarted(tmp_path, current_local=True, explicit_skill="original-skill") as env:
        assert env.runtime.skill_service is None
        traps = guard_receipt_only(env, monkeypatch, provider_identity=False)
        before = snapshot(env)
        with pytest.raises(IdemConflict):
            await env.runtime.submit(env.body, explicit_skill=changed_skill)
        assert_no_effects(env, before, traps)


async def test_exact_explicit_skill_receipt_survives_service_removal(tmp_path, monkeypatch):
    async with restarted(tmp_path, explicit_skill="original-skill") as env:
        assert env.runtime.skill_service is None
        traps = guard_receipt_only(env, monkeypatch)
        before = snapshot(env)
        assert await env.runtime.submit(env.body, explicit_skill="original-skill") == env.retained
        with pytest.raises(PolicyDenied):
            await env.runtime.submit(env.body, actor="decisions", explicit_skill="original-skill")
        assert_no_effects(env, before, traps)


@pytest.mark.parametrize("skill", ["", "x" * 257, 1, True])
async def test_recovery_does_not_relax_explicit_skill_label_validation(
    tmp_path, monkeypatch, skill
):
    async with restarted(tmp_path, current_local=True, explicit_skill="original-skill") as env:
        traps = guard_receipt_only(env, monkeypatch, provider_identity=False)
        before = snapshot(env)
        with pytest.raises((ValueError, PolicyDenied)):
            await env.runtime.submit(env.body, explicit_skill=skill)
        assert_no_effects(env, before, traps)


@pytest.mark.parametrize(
    "defect",
    [
        "revised",
        "reset",
        "wrong-thread",
        "cross-session",
        "missing-source",
        "not-final",
        "bad-text",
        "unauthenticated",
    ],
)
async def test_receipt_rechecks_current_native_session_and_source_authority(
    tmp_path, monkeypatch, defect
):
    async with restarted(tmp_path, current_local=True, actor="decisions") as env:
        body = env.body
        expected = PolicyDenied
        if defect == "revised":
            env.store.append(
                AppendRequest(
                    session_id=env.session.session_id,
                    thread_id=env.session.thread_id,
                    epoch=env.session.epoch,
                    kind="conversation.revision",
                    payload={
                        "target_event_id": env.source.event_id,
                        "text": OBJECTIVE,
                        "revision": 2,
                        "content_scope": "local_only",
                    },
                    idempotency_key="source-correction",
                ),
                "owner",
            )
        elif defect == "reset":
            env.store.reset(env.session.session_id)
            expected = StaleRevision
        elif defect == "wrong-thread":
            body = body.model_copy(update={"thread_id": "different-thread"})
            expected = StaleRevision
        elif defect == "cross-session":
            other = env.store.create_session(SessionRequest(channel="agent"))
            source = append_source(env.store, other)
            body = body.model_copy(update={"source_event_ids": [source.event_id]})
        elif defect == "missing-source":
            body = body.model_copy(update={"source_event_ids": ["missing-native-source"]})
        else:
            # Fault injection into persisted synthetic data tests read-side
            # authentication/shape checks; no API can assert this provenance.
            event = env.store.entry(env.source.event_id).model_dump(mode="json")
            if defect == "not-final":
                event["payload"]["final"] = False
            elif defect == "bad-text":
                event["payload"]["text"] = ["invalid native text"]
            else:
                event["origin"] = "untrusted-fixture"
            with env.store.db:
                env.store.db.execute(
                    "UPDATE entries SET value=? WHERE event_id=?",
                    (json.dumps(event), env.source.event_id),
                )
        traps = guard_receipt_only(env, monkeypatch, provider_identity=False)
        before = snapshot(env)
        with pytest.raises(expected):
            await env.runtime.submit(body, actor="decisions")
        assert_no_effects(env, before, traps)


async def test_window_receipt_rejects_a_later_authenticated_instruction(tmp_path, monkeypatch):
    async with restarted(
        tmp_path, current_local=True, actor="decisions", source_kind="window"
    ) as env:
        append_source(
            env.store, env.session, kind="window", index=1, text="A later synthetic instruction"
        )
        traps = guard_receipt_only(env, monkeypatch, provider_identity=False)
        before = snapshot(env)
        with pytest.raises(PolicyDenied, match="superseded"):
            await env.runtime.submit(env.body, actor="decisions")
        assert_no_effects(env, before, traps)


async def test_decisions_receipt_keeps_literal_objective_equality(tmp_path, monkeypatch):
    async with restarted(tmp_path, current_local=True, actor="decisions") as env:
        traps = guard_receipt_only(env, monkeypatch, provider_identity=False)
        before = snapshot(env)
        with pytest.raises(PolicyDenied, match="does not match"):
            await env.runtime.submit(
                env.body.model_copy(update={"objective": "Invented replacement instruction"}),
                actor="decisions",
            )
        assert_no_effects(env, before, traps)


async def test_new_key_keeps_new_task_disclosure_admission(tmp_path):
    async with restarted(tmp_path, source_kind=None) as env:
        before = snapshot(env)
        with pytest.raises(PolicyDenied, match="disclosure"):
            await env.runtime.submit(
                env.body.model_copy(update={"idempotency_key": "genuinely-new-task"})
            )
        assert snapshot(env) == before
        assert env.provider.calls == []


async def test_http_receipt_rechecks_authentication_roles_and_current_epoch(tmp_path, monkeypatch):
    async with restarted(tmp_path) as env:
        registry = Registry(env.runtime)
        app = create_app(env.runtime, AUTH, commands=registry)
        traps = guard_receipt_only(env, monkeypatch)
        before = snapshot(env)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for token, status in (
                    (None, 401),
                    ("invalid-synthetic-token", 401),
                    ("a" * 32, 403),
                    ("r" * 32, 403),
                ):
                    headers = {} if token is None else {"Authorization": "Bearer " + token}
                    result = await client.post(
                        "/v1/agent/tasks", json=env.body.model_dump(mode="json"), headers=headers
                    )
                    assert result.status_code == status
                for token in ("o" * 32, "d" * 32):
                    result = await client.post(
                        "/v1/agent/tasks",
                        json=env.body.model_dump(mode="json"),
                        headers={"Authorization": "Bearer " + token},
                    )
                    assert result.status_code == 201, result.text
                    assert result.json() == env.retained.model_dump(mode="json")
                assert_no_effects(env, before, traps)
                env.store.reset(env.session.session_id)
                before = snapshot(env)
                result = await client.post(
                    "/v1/agent/tasks",
                    json=env.body.model_dump(mode="json"),
                    headers={"Authorization": "Bearer " + "o" * 32},
                )
                assert result.status_code == 409
                assert_no_effects(env, before, traps)
        finally:
            registry.close()
