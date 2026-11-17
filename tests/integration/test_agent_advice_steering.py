"""Agent advice export authority around an actual native steer."""

import asyncio

import httpx
import pytest
import test_agent_advice_operations
from test_agent_advice_export import PATH, TOKENS, append, request
from test_agent_advice_export import env as env
from test_agent_advice_operations import call, measured

from packages.contracts.agent_advice_operations import AgentAdviceLookup
from packages.contracts.agent_control import (
    AgentModeChange,
    AgentSteerAdmissionRequest,
    AgentSteerChange,
)
from packages.contracts.client import JournalClient

operation_env = test_agent_advice_operations.env


def commit_steer(env):
    owner = env.store.agent_control
    intent = AgentSteerAdmissionRequest(
        schema_version="1.0",
        expected_backend=owner.backend(),
        task=env.identity,
        actor="owner",
        request=AgentSteerChange(
            schema_version="1.0",
            idempotency_key="native-advice-revision",
            instruction="Synthetic revised objective",
            expected_objective_revision=1,
            content_scope="local_only",
            source_event_ids=(),
        ),
        effective_content_scope="local_only",
        source_revisions=(),
    )
    return intent, owner.admit_steer(intent)


def resolve_steer(env, intent, receipt, outcome, **changes):
    return append(
        env,
        "agent.command",
        {
            "op": outcome,
            "task_id": env.identity.task_id,
            "actor": "owner",
            "state": "running",
            "content_scope": "local_only",
            "steer_event_id": receipt.event_id,
            "intent_digest": intent.request_digest(),
            "objective_revision": 2 if outcome == "steer-applied" else 1,
            **({"reason": "source_changed_before_boundary"} if outcome == "steer-rejected" else {}),
        }
        | changes,
    )


@pytest.mark.parametrize("purpose", ["complete_count", "complete_generation"])
async def test_committed_steer_fences_old_advice_until_native_application(env, purpose):
    original = request(
        env, purpose=purpose, provider={"provider": "lmstudio", "model": "synthetic", "local": True}
    )
    client = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    try:
        assert (await client.agent_advice_export_admission(original)).task_state == "running"
        intent, receipt = commit_steer(env)
        assert receipt.disposition == "created"
        before = env.store.read()
        with pytest.raises(httpx.HTTPStatusError) as refused:
            await client.agent_advice_export_admission(original)
        assert refused.value.response.status_code == 409
        assert refused.value.response.request.url.path == PATH
        assert env.store.read() == before

        resolve_steer(env, intent, receipt, "steer-applied")
        with pytest.raises(httpx.HTTPStatusError) as stale:
            await client.agent_advice_export_admission(original)
        assert stale.value.response.status_code == 409
        current = request(
            env,
            purpose=purpose,
            objective_revision=2,
            provider={"provider": "lmstudio", "model": "synthetic", "local": True},
        )
        admitted = await client.agent_advice_export_admission(current)
        assert admitted.request == current and admitted.task_content_scope == "local_only"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "changes",
    [{}, {"intent_digest": "a" * 64}, {"steer_event_id": "different-intent"}],
    ids=["exact", "wrong-digest", "wrong-event"],
)
async def test_only_an_exact_rejection_restores_the_existing_revision(env, changes):
    intent, receipt = commit_steer(env)
    resolve_steer(env, intent, receipt, "steer-rejected", **changes)
    original = request(env, provider={"provider": "lmstudio", "model": "synthetic", "local": True})
    client = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    try:
        if changes:
            with pytest.raises(httpx.HTTPStatusError) as refused:
                await client.agent_advice_export_admission(original)
            assert refused.value.response.status_code == 409
        else:
            response = await client.agent_advice_export_admission(original)
            assert response.request == original and response.task_content_scope == "local_only"
    finally:
        await client.close()


async def test_agent_off_preserves_advice_authority_for_already_accepted_work(env):
    owner = env.store.agent_control
    owner.register()
    state = owner.snapshot()
    owner.change(
        AgentModeChange(
            schema_version="1.0",
            idempotency_key="stop-new-task-admission",
            expected_backend=state.backend_id,
            expected_instance=state.instance_id,
            expected_revision=state.revision,
            mode="off",
            active_work_policy="finish_already_accepted",
        )
    )
    native = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    try:
        original = request(env)
        assert (await native.agent_advice_export_admission(original)).request == original
    finally:
        await native.close()


async def test_steer_after_the_first_count_stops_actual_generation(operation_env, monkeypatch):
    env = operation_env
    native = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    env.owner.journal = native
    wire, attempted, _ = measured(env, monkeypatch, hold=("applyPromptTemplate", 1))
    original = call(env)
    lookup = AgentAdviceLookup.of(original)
    try:
        env.owner.submit(original)
        await asyncio.wait_for(wire.entered.wait(), 3)
        _, receipt = commit_steer(env)
        assert receipt.disposition == "created"
        wire.release.set()
        observed = await env.owner.wait(lookup)
        assert observed.phase == "failed" and observed.error == "native_admission_refused"
        assert observed.cleanup == "confirmed" and not observed.generation_admission_observed
        assert wire.predictions == 0
        assert attempted and attempted[0].purpose == "complete_count"
        assert (
            len(
                [
                    packet
                    for packet in wire.packets
                    if packet.get("endpoint") == "applyPromptTemplate"
                ]
            )
            == 1
        )
        assert env.owner.submit(original) == observed
    finally:
        wire.release.set()
        await native.close()
