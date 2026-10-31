"""Agent-only resource admissions compose real count and export boundaries."""

import asyncio
import json

import httpx
import pytest

from packages.contracts.agent_tools import (
    agent_export_scope,
    agent_tool_scope,
    native_tool_snapshot,
)
from packages.providers.completion_budget import PreparedComplete
from packages.providers.prompt_budget import PromptCountUnavailable
from packages.providers.text import (
    CompatibleTextProvider,
    MetaResponsesProvider,
    ProviderRefusal,
    ScriptedProvider,
)
from services.resources.providers import ResourceBoundProvider
from services.resources.state import ResourceState

MESSAGES = [{"role": "user", "content": "Public synthetic resource task"}]


def meta(handler=None):
    calls = []

    async def send(request):
        calls.append(request.url.path)
        if handler is not None:
            await handler(request)
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 12})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Synthetic reply"}],
                    }
                ],
            },
        )

    p = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-key",
        allow_cloud=True,
        max_tokens=8192,
        context_tokens=1007997,
        transport=httpx.MockTransport(send),
    )
    return p, calls


@pytest.fixture
def policy(tmp_path):
    state = ResourceState(tmp_path / "resources.json", profile="configured_hybrid")
    state.save(phase="ready")
    yield state
    state.close()


async def test_agent_resource_prepare_count_and_generation(policy):
    p, calls = meta()
    wrapped = ResourceBoundProvider(p, policy.path, "agent")
    try:
        with agent_tool_scope(native_tool_snapshot()):
            prepared = await wrapped.prepare_complete(MESSAGES)
            assert not prepared.fixture and prepared.measurement.reserved_output_tokens == 8192
            assert await prepared.complete() == "Synthetic reply"
            with pytest.raises(PromptCountUnavailable):
                await prepared.complete()
        assert calls == ["/v1/responses/input_tokens", "/v1/responses"]
    finally:
        await p.close()


@pytest.mark.parametrize("role", ["converse", "decisions", "invalid"])
async def test_complete_preparation_is_not_a_new_other_role_path(policy, role):
    p, calls = meta()
    try:
        with pytest.raises(PromptCountUnavailable):
            await ResourceBoundProvider(p, policy.path, role).prepare_complete(MESSAGES)
        assert not calls
    finally:
        await p.close()


async def test_local_only_resource_policy_refuses_before_count(policy):
    p, calls = meta()
    policy.save(profile="local_only")
    try:
        with pytest.raises(PromptCountUnavailable):
            await ResourceBoundProvider(p, policy.path, "agent").prepare_complete(MESSAGES)
        assert not calls
    finally:
        await p.close()


@pytest.mark.parametrize(
    "change", ["policy", "same_revision_policy", "role", "provider", "state_path"]
)
async def test_changed_resource_admission_consumes_without_generation(policy, change):
    p, calls = meta()
    wrapped = ResourceBoundProvider(p, policy.path, "agent")
    try:
        prepared = await wrapped.prepare_complete(MESSAGES)
        if change == "policy":
            policy.save(reason="Owner changed policy")
        elif change == "same_revision_policy":
            policy.path.write_text(
                json.dumps({**policy.record.model_dump(), "profile": "conversation_priority"})
            )
        elif change == "role":
            wrapped.role = "converse"
        elif change == "provider":
            wrapped.provider = ScriptedProvider(["must not execute"])
        else:
            wrapped.state_path = policy.path.parent / "missing-policy.json"
        with pytest.raises(PromptCountUnavailable):
            await prepared.complete()
        with pytest.raises(PromptCountUnavailable):
            await prepared.complete()
        assert calls == ["/v1/responses/input_tokens"]
    finally:
        await p.close()


async def test_resource_guard_is_applied_before_adapter_count_export(policy):
    p, calls = meta()

    class ChangesBeforeCount:
        info = p.info

        async def prepare_complete(self, messages):
            policy.save(reason="Changed after wrapper admission")
            return await p.prepare_complete(messages)

    try:
        with pytest.raises(PromptCountUnavailable):
            await ResourceBoundProvider(
                ChangesBeforeCount(), policy.path, "agent"
            ).prepare_complete(MESSAGES)
        assert not calls
    finally:
        await p.close()


async def test_resource_change_while_count_is_returning_prevents_admission(policy):
    async def change(_request):
        policy.save(reason="Changed during synthetic count")

    p, calls = meta(change)
    try:
        with pytest.raises(PromptCountUnavailable):
            await ResourceBoundProvider(p, policy.path, "agent").prepare_complete(MESSAGES)
        assert calls == ["/v1/responses/input_tokens"]
    finally:
        await p.close()


async def test_resource_guard_survives_generation_capture_await(policy, monkeypatch):
    from packages.providers import decision_capture

    p, calls = meta()
    wrapped = ResourceBoundProvider(p, policy.path, "agent")

    async def capture(*_args):
        policy.save(reason="Changed during native capture")

    monkeypatch.setattr(decision_capture, "capture_request", capture)
    try:
        prepared = await wrapped.prepare_complete(MESSAGES)
        with pytest.raises(ProviderRefusal):
            await prepared.complete()
        assert calls == ["/v1/responses/input_tokens"]
    finally:
        await p.close()


@pytest.mark.parametrize("where", ["count", "generation"])
async def test_resource_revocation_in_awaited_request_hook_blocks_transport(policy, where):
    p, calls = meta()
    wrapped = ResourceBoundProvider(p, policy.path, "agent")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(request):
        if request.url.path.endswith("input_tokens") == (where == "count"):
            entered.set()
            await release.wait()

    async def revoke():
        await entered.wait()
        policy.save(reason="Changed while synthetic request hook awaited")
        release.set()

    p.http.event_hooks["request"].append(hook)
    revocation = asyncio.create_task(revoke())
    try:
        if where == "count":
            with pytest.raises(PromptCountUnavailable):
                await wrapped.prepare_complete(MESSAGES)
        else:
            prepared = await wrapped.prepare_complete(MESSAGES)
            with pytest.raises(ProviderRefusal):
                await prepared.complete()
            with pytest.raises(PromptCountUnavailable):
                await prepared.complete()
        await revocation
        assert calls == ([] if where == "count" else ["/v1/responses/input_tokens"])
    finally:
        release.set()
        revocation.cancel()
        await asyncio.gather(revocation, return_exceptions=True)
        await p.close()


@pytest.mark.parametrize("where", ["prepare", "generation"])
async def test_wrapper_cannot_replace_native_export_guard(policy, where):
    p, calls = meta()
    wrapped = ResourceBoundProvider(p, policy.path, "agent")
    try:
        if where == "prepare":
            with agent_export_scope(lambda: False), pytest.raises(PromptCountUnavailable):
                await wrapped.prepare_complete(MESSAGES)
            assert not calls
        else:
            prepared = await wrapped.prepare_complete(MESSAGES)
            with agent_export_scope(lambda: False), pytest.raises(ProviderRefusal):
                await prepared.complete()
            assert calls == ["/v1/responses/input_tokens"]
    finally:
        await p.close()


async def test_resource_handle_rejects_inherited_child_task(policy):
    p, calls = meta()
    try:
        prepared = await ResourceBoundProvider(p, policy.path, "agent").prepare_complete(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await asyncio.create_task(prepared.complete())
        with pytest.raises(PromptCountUnavailable):
            await prepared.complete()
        assert len(calls) == 1
    finally:
        await p.close()


async def test_scripted_fixture_is_explicit_and_never_counted(policy):
    p = ScriptedProvider(["Synthetic fixture"])
    wrapped = ResourceBoundProvider(p, policy.path, "agent")
    prepared = await wrapped.prepare_complete(MESSAGES)
    assert prepared.fixture and prepared.measurement is None and not p.calls
    assert await prepared.complete() == "Synthetic fixture"


async def test_foreign_provider_cannot_relabel_an_uncounted_generation_as_fixture(policy):
    executed = False

    class Foreign:
        info = ScriptedProvider.info

        async def prepare_complete(self, _messages):
            async def generate():
                nonlocal executed
                executed = True
                return "foreign"

            return PreparedComplete(None, generate, fixture=True)

    with pytest.raises(PromptCountUnavailable):
        await ResourceBoundProvider(Foreign(), policy.path, "agent").prepare_complete(MESSAGES)
    assert not executed


async def test_unsupported_real_provider_never_falls_back_to_complete(policy):
    p = CompatibleTextProvider(
        "http://127.0.0.1:1/v1",
        "synthetic",
        transport=httpx.MockTransport(lambda _: pytest.fail("unmeasured inference")),
    )
    try:
        with pytest.raises(PromptCountUnavailable):
            await ResourceBoundProvider(p, policy.path, "agent").prepare_complete(MESSAGES)
    finally:
        await p.close()
