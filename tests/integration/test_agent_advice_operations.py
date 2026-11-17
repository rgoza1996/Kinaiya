"""Retained originals use synthetic adapters; no live or paid model calls."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from advice_native_wire import NativeWire
from test_agent_advice_export import TOKENS
from test_agent_advice_export import env as source_environment

from packages.contracts.agent_advice_operations import (
    AgentAdviceCall,
    AgentAdviceLookup,
    AgentAdviceModelPin,
    AgentAdviceObservation,
)
from packages.contracts.agent_tools import (
    advisory_text_scope,
    agent_export_permitted,
    agent_tool_scope,
    capture_agent_export_admission,
    current_agent_tools,
    native_tool_snapshot,
)
from packages.contracts.client import JournalClient
from packages.contracts.models import AppendRequest
from packages.contracts.operation_ownership import ActualOperation, actual_owner
from packages.providers import lmstudio
from packages.providers.text import ScriptedProvider
from services.decisions import agent_operations
from services.decisions.agent_operations import (
    AgentAdviceConflict,
    AgentAdviceOperations,
    AgentAdviceUnavailable,
)
from services.decisions.agent_policy import AgentPolicyOwner
from services.session.store import LocalJournal
from services.settings import provider_factory
from services.settings.provider_catalog import ProviderCatalog, ProviderChange


class ControlledText(ScriptedProvider):
    def __init__(self, info, settings):
        super().__init__([])
        self.info, self.settings = info, settings
        self.entered, self.close_entered = asyncio.Event(), asyncio.Event()
        self.release, self.release_close = asyncio.Event(), asyncio.Event()
        if not settings.get("hold"):
            self.release.set()
        if not settings.get("hold_close"):
            self.release_close.set()
        self.close_calls = 0
        self.tools = []

    async def complete(self, messages):
        self.calls.append(copy.deepcopy(messages))
        self.tools.append(current_agent_tools())
        self.entered.set()
        await self.release.wait()
        return self.settings.get("text", "  Raw synthetic advice Ω\n")

    async def close(self):
        self.close_calls += 1
        self.close_entered.set()
        await self.release_close.wait()
        if self.settings.get("fail_close"):
            raise RuntimeError("SYNTHETIC_PRIVATE_CLOSE_DETAIL")


def save(catalog, model="synthetic-alias"):
    result = catalog.change(
        ProviderChange.model_validate(
            {
                "schema_version": "1.0",
                "expected_revision": catalog.snapshot()["revision"],
                "idempotency_key": f"profile-{catalog.snapshot()['revision']}",
                "operation": "profile_upsert",
                "profile_id": "advisor",
                "label": "Synthetic advisor",
                "config": {
                    "kind": "lmstudio",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "model": model,
                },
            }
        )
    )["result"]
    return {key: result[key] for key in ("id", "generation", "configuration_sha256")}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    original = source_environment.__wrapped__(tmp_path)
    native = next(original)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    catalog = ProviderCatalog(tmp_path / "providers.sqlite", workspace)
    binding = save(catalog)
    instructions = "Independent synthetic Agent preference.\n"
    policy = AgentPolicyOwner(
        {
            "decisions_agent_policy": {
                "catalog_binding": binding,
                "instructions": instructions,
                "content_scope": "public_synthetic",
            }
        },
        catalog,
    )
    journal = LocalJournal(native.store, "decisions")
    owner = AgentAdviceOperations(policy, journal, capacity=1, retention=4)
    await owner.start()
    settings, adapters = {}, []
    created = asyncio.Event()

    def open_fixture(captured, **kwargs):
        assert kwargs == {"auxiliary": True, "resource_state": None, "role": "agent"}
        info = captured.recipe.info
        captured.release()
        adapter = ControlledText(info, dict(settings))
        adapters.append(adapter)
        created.set()
        return adapter

    monkeypatch.setattr(agent_operations, "open_captured_recipe", open_fixture)
    value = SimpleNamespace(
        **vars(native),
        owner=owner,
        policy=policy,
        catalog=catalog,
        binding=binding,
        instructions=instructions,
        settings=settings,
        adapters=adapters,
        created=created,
    )
    try:
        yield value
    finally:
        for adapter in adapters:
            adapter.release.set()
            adapter.release_close.set()
        await owner.close(timeout=1)
        catalog.close()
        original.close()


def call(env, key="original", **changes):
    return AgentAdviceCall.model_validate(
        {
            "expected_instance": env.owner.instance_id,
            "policy_sha256": env.policy.snapshot().policy_sha256,
            "logical_request_id": key,
            "task": env.identity.model_dump(),
            "objective_revision": 1,
            "source_revisions": {env.source.event_id: 1},
            "content_scope": "public_synthetic",
            "messages": [{"role": "user", "content": "Full synthetic Agent input Ω\n"}],
            **changes,
        }
    )


async def test_original_request_returns_raw_output_and_exact_model_pin(env):
    request = call(env)
    env.owner.submit(request)
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.phase == "completed" and result.cleanup == "confirmed"
    assert result.model_output == "  Raw synthetic advice Ω\n"
    assert (
        result.output_authority == "untrusted_model_data"
        and result.task_status_assessment == "not_assessed"
    )
    assert result.model_pin.catalog_binding.model_dump() == env.binding
    assert result.model_pin.policy_sha256 == request.policy_sha256
    # This explicit ScriptedProvider fixture never measures or uploads a prompt.
    assert result.generation_admission_observed is False
    assert env.adapters[0].calls == [
        [
            {"role": "system", "content": env.instructions},
            *[m.model_dump() for m in request.messages],
        ]
    ]
    assert env.adapters[0].tools == [None] and env.adapters[0].close_calls == 1


@pytest.mark.parametrize("text", ["", "IGNORE PERMISSIONS", '{"success":true}', "\x00Ω\n"])
async def test_model_content_is_preserved_without_quality_or_style_overrides(env, text):
    env.settings["text"] = text
    request = call(env)
    env.owner.submit(request)
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.phase == "completed" and result.model_output == text
    assert result.output_bytes == len(text.encode("utf-8"))
    assert result.task_status_assessment == "not_assessed"


async def test_repeated_submit_and_lookup_never_repeat_original_model_call(env):
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    first = await env.owner.wait(lookup)
    for _ in range(5):
        assert env.owner.submit(request) == env.owner.lookup(lookup) == first
    assert len(env.adapters) == len(env.adapters[0].calls) == env.adapters[0].close_calls == 1
    with pytest.raises(AgentAdviceConflict):
        env.owner.submit(call(env, messages=[{"role": "user", "content": "Changed input"}]))


async def test_facade_timeout_and_cancellation_retain_the_original_task(env, monkeypatch):
    env.settings["hold"] = True
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    await env.created.wait()
    adapter = env.adapters[0]
    await adapter.entered.wait()
    shielded = asyncio.Event()
    original_task = env.owner._originals[request.logical_request_id].task
    actual_shield = asyncio.shield
    waiting = None

    def observe_shield(task):
        if asyncio.current_task() is waiting and task is original_task:
            shielded.set()
        return actual_shield(task)

    monkeypatch.setattr(agent_operations.asyncio, "shield", observe_shield)
    waiting = asyncio.create_task(env.owner.wait(lookup, timeout=30))
    await shielded.wait()
    assert waiting.cancel() is True
    with pytest.raises(asyncio.CancelledError):
        await waiting
    result = await env.owner.wait(lookup, timeout=0.001)
    assert result.phase == "running" and len(adapter.calls) == 1
    with pytest.raises(AgentAdviceUnavailable):
        env.owner.submit(call(env, "other"))
    adapter.release.set()
    assert (await env.owner.wait(lookup)).phase == "completed"


async def test_cancel_before_task_entry_returns_a_confirmed_native_observation(env):
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    response = env.owner.cancel(lookup)
    assert response.cancellation_requested is True
    result = await env.owner.wait(lookup)
    assert result.phase == "cancelled" and result.cleanup == "confirmed"
    assert not env.adapters


async def test_original_cancellation_does_not_consume_an_observers_earlier_cancellation(env):
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    env.owner.cancel(lookup)
    observer = asyncio.current_task()
    previous = observer.cancelling()
    observer.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        count = observer.cancelling()
        result = await env.owner.wait(lookup)
        assert result.phase == "cancelled" and result.cleanup == "confirmed"
        assert observer.cancelling() == count == previous + 1
    finally:
        while observer.cancelling() > previous:
            observer.uncancel()


async def test_capacity_stays_owned_until_the_actual_adapter_close(env):
    env.settings["hold_close"] = True
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    await env.created.wait()
    adapter = env.adapters[0]
    await adapter.close_entered.wait()
    result = await env.owner.wait(lookup, timeout=0.001)
    assert result.phase == "closing" and result.cleanup == "pending"
    assert result.model_output == "  Raw synthetic advice Ω\n"
    with pytest.raises(AgentAdviceUnavailable):
        env.owner.submit(call(env, "other"))
    env.owner.cancel(lookup)
    assert (await env.owner.wait(lookup, timeout=0.001)).cleanup == "pending"
    adapter.release_close.set()
    finished = await env.owner.wait(lookup)
    assert finished.phase == "completed" and finished.cleanup == "confirmed"
    assert adapter.close_calls == 1


async def test_unconfirmed_close_retains_capacity_and_original_output(env):
    env.settings["fail_close"] = True
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    result = await env.owner.wait(lookup)
    assert result.phase == "cleanup_unconfirmed" and result.cleanup == "unconfirmed"
    assert result.model_output == "  Raw synthetic advice Ω\n"
    assert result.error == "actual_cleanup_unconfirmed"
    assert "SYNTHETIC_PRIVATE_CLOSE_DETAIL" not in result.model_dump_json()
    with pytest.raises(AgentAdviceUnavailable):
        env.owner.submit(call(env, "other"))
    assert await env.owner.close() is False
    assert env.owner.lookup(lookup) == result.model_copy(update={"cancellation_requested": True})
    assert env.adapters[0].close_calls == 1


async def test_catalog_rotation_never_retargets_an_admitted_operation(env):
    env.settings["hold"] = True
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    await env.created.wait()
    adapter = env.adapters[0]
    await adapter.entered.wait()
    replacement = save(env.catalog, model="replacement")
    assert replacement["generation"] == 2
    adapter.release.set()
    first = await env.owner.wait(lookup)
    assert first.model_pin.catalog_binding.generation == 1
    assert env.owner.submit(request) == first
    second = call(env, "after-rotation")
    env.owner.submit(second)
    assert (await env.owner.wait(AgentAdviceLookup.of(second))).error == "policy_unavailable"
    assert len(env.adapters) == 1


async def test_terminal_history_and_controller_identity_do_not_fund_replay(env):
    original = None
    for n in range(4):
        request = call(env, str(n))
        env.owner.submit(request)
        result = await env.owner.wait(AgentAdviceLookup.of(request))
        assert result.phase == "completed"
        if n == 0:
            original = request
    with pytest.raises(AgentAdviceUnavailable):
        env.owner.submit(call(env, "overflow"))
    assert (
        env.owner.lookup(AgentAdviceLookup.of(original)).model_output
        == "  Raw synthetic advice Ω\n"
    )
    with pytest.raises(AgentAdviceConflict):
        env.owner.lookup(
            AgentAdviceLookup.of(original).model_copy(update={"expected_instance": "a" * 32})
        )
    assert len(env.adapters) == 4


async def test_close_wait_is_bounded_and_does_not_claim_retirement(env):
    env.settings["hold_close"] = True
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    await env.created.wait()
    adapter = env.adapters[0]
    await adapter.close_entered.wait()
    assert await env.owner.close(timeout=0.001) is False
    assert env.owner.lookup(lookup).cleanup == "pending"
    with pytest.raises(AgentAdviceUnavailable):
        env.owner.submit(call(env, "new"))
    adapter.release_close.set()
    assert (await env.owner.wait(lookup)).cleanup == "confirmed"
    assert await env.owner.close() is True


async def test_caller_mutation_cannot_change_the_enrolled_input(env):
    env.settings["hold"] = True
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    request.messages.clear()
    request.source_revisions.clear()
    await env.created.wait()
    adapter = env.adapters[0]
    await adapter.entered.wait()
    adapter.release.set()
    result = await env.owner.wait(lookup)
    assert result.request_sha256 == lookup.request_sha256
    assert adapter.calls[0][-1]["content"] == "Full synthetic Agent input Ω\n"


@pytest.mark.parametrize(
    "model", [AgentAdviceCall, AgentAdviceLookup, AgentAdviceModelPin, AgentAdviceObservation]
)
def test_operation_contract_exports_are_closed_and_independent(model):

    path = (
        Path(__file__).resolve().parents[2]
        / "packages/contracts/schemas"
        / f"{model.__name__}.json"
    )
    assert json.loads(path.read_bytes()) == model.model_json_schema()
    assert model.model_json_schema()["additionalProperties"] is False


def measured(env, monkeypatch, *, hold=None):
    wire = NativeWire(hold=hold)
    actual = lmstudio.LMStudioProvider
    providers = []

    def construct(*args, **kwargs):
        value = actual(*args, transport_factory=wire.connect, **kwargs)
        providers.append(value)
        return value

    monkeypatch.setattr(lmstudio, "LMStudioProvider", construct)
    monkeypatch.setattr(
        agent_operations, "open_captured_recipe", provider_factory.open_captured_recipe
    )
    calls = []
    original = env.owner.journal.agent_advice_export_admission

    async def admitted(request):
        calls.append(request)
        return await original(request)

    monkeypatch.setattr(env.owner.journal, "agent_advice_export_admission", admitted)
    return wire, calls, providers


async def test_actual_measured_adapter_qualifies_each_exact_upload_without_execution_tools(
    env, monkeypatch
):
    wire, admissions, providers = measured(env, monkeypatch)
    request = call(env)
    with agent_tool_scope(native_tool_snapshot(catalog_available=True)):
        env.owner.submit(request)
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.phase == "completed" and result.model_output == "Public result"
    assert result.generation_admission_observed is True
    assert wire.predictions == 1 and len(providers) == 1
    generation = [a for a in admissions if a.purpose == "complete_generation"]
    counts = [a for a in admissions if a.purpose == "complete_count"]
    assert len(generation) == 1 and counts
    uploaded = [p for p in wire.packets if p.get("endpoint") == "applyPromptTemplate"]
    expected = [
        {"role": "system", "content": [{"type": "text", "text": env.instructions}]},
        {"role": "user", "content": [{"type": "text", "text": "Full synthetic Agent input Ω\n"}]},
    ]
    assert uploaded and all(p["parameter"]["history"]["messages"] == expected for p in uploaded)
    assert all(
        a.task == env.identity and a.policy_sha256 == request.policy_sha256 for a in admissions
    )
    before = len(wire.packets), len(admissions)
    for _ in range(4):
        assert env.owner.lookup(AgentAdviceLookup.of(request)) == result
    assert before == (len(wire.packets), len(admissions))
    assert all(s.close_calls == s.wait_calls == 1 for s in wire.sockets)


async def test_actual_native_source_change_blocks_generation_without_reroll(env, monkeypatch):
    wire, _, _ = measured(env, monkeypatch, hold=("applyPromptTemplate", 1))
    request = call(env)
    env.owner.submit(request)
    await wire.entered.wait()
    env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.revision",
            payload={
                "target_event_id": env.source.event_id,
                "revision": 2,
                "text": "Synthetic corrected input",
                "content_scope": "local_only",
            },
            idempotency_key="corrected",
        ),
        "owner",
    )
    wire.release.set()
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.phase == "failed" and result.cleanup == "confirmed"
    assert result.error == "native_admission_refused" and wire.predictions == 0
    assert result.generation_admission_observed is False
    assert env.owner.submit(request) == result


async def test_returned_model_text_survives_later_epoch_change_as_untrusted_data(env, monkeypatch):
    wire, _, _ = measured(env, monkeypatch, hold="model-reply")
    request = call(env)
    env.owner.submit(request)
    await wire.entered.wait()
    assert wire.predictions == 1
    env.store.reset(env.session.session_id)
    wire.release.set()
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.phase == "completed" and result.model_output == "Public result"
    assert result.task.epoch == request.task.epoch
    assert result.task_status_assessment == "not_assessed"
    assert env.owner.lookup(AgentAdviceLookup.of(request)) == result
    assert wire.predictions == 1


async def test_agent_resource_group_does_not_depend_on_converse_wake_state(
    env, monkeypatch, tmp_path
):
    from services.resources.state import ResourceRecord

    state = tmp_path / "resource-state.json"
    state.write_text(
        ResourceRecord(
            profile="local_work_priority", phase="local_work", desired_converse=False
        ).model_dump_json()
    )
    state.chmod(0o600)
    env.owner.resource_state = state
    wire, _, _ = measured(env, monkeypatch)
    request = call(env)
    env.owner.submit(request)
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.phase == "completed" and wire.predictions == 1


@pytest.mark.parametrize("kind", ["raise-before", "raise-after-enqueue", "foreign-task"])
async def test_uncertain_task_factory_keeps_original_key_and_capacity(env, kind):
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    observed = []

    def uncertain_factory(loop, coroutine, context=None):
        if coroutine.cr_code is AgentAdviceOperations._run.__code__:
            if kind == "raise-before":
                raise RuntimeError("SYNTHETIC_PRIVATE_FACTORY_DETAIL")
            if kind == "raise-after-enqueue":
                task = asyncio.Task(coroutine, loop=loop, context=context)
                observed.append(task)
                raise RuntimeError("SYNTHETIC_PRIVATE_FACTORY_DETAIL")
            task = asyncio.Task(asyncio.sleep(0), loop=loop, context=context)
            observed.append(task)
            return task
        return asyncio.Task(coroutine, loop=loop, context=context)

    loop.set_task_factory(uncertain_factory)
    try:
        with pytest.raises(AgentAdviceUnavailable):
            env.owner.submit(request)
    finally:
        loop.set_task_factory(previous)
    await asyncio.sleep(0)
    original = env.owner._originals[request.logical_request_id]
    result = env.owner.lookup(lookup)
    assert result.phase == "setup_unconfirmed" and result.cleanup == "unconfirmed"
    assert not env.adapters
    with pytest.raises(AgentAdviceUnavailable):
        env.owner.submit(call(env, "other"))
    assert env.owner.submit(request) == result
    assert await env.owner.close(timeout=0.001) is False
    # The controlled fixture knows these actual handles. Retiring them here is
    # test cleanup; the owner must not infer their absence from a missing return.
    for task in observed:
        task.cancel()
    if observed:
        await asyncio.gather(*observed, return_exceptions=True)
    else:
        original.coroutine.close()
    if kind == "foreign-task":
        original.coroutine.close()
    assert env.owner.lookup(lookup).phase == "setup_unconfirmed"
    assert await env.owner.close(timeout=0.001) is False


@pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"),
    reason="Native eager task factories require Python 3.12 or later",
)
async def test_eager_native_factory_still_enrolls_before_any_model_effect(env):
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        request = call(env)
        first = env.owner.submit(request)
        assert first.phase == "pending" and not env.adapters
    finally:
        loop.set_task_factory(previous)
    assert (await env.owner.wait(AgentAdviceLookup.of(request))).phase == "completed"


@pytest.mark.parametrize(
    "value,state,error",
    [
        ({"not": "raw text"}, "invalid_text", "invalid_output"),
        ("\ud800", "invalid_text", "invalid_output"),
        ("x" * (4 * 1024 * 1024 + 1), "resource_refused", "output_exceeds_bound"),
    ],
    ids=["wrong-protocol-type", "invalid-unicode", "declared-output-bound"],
)
async def test_protocol_and_declared_resource_failures_are_explicit_without_truncation(
    env, value, state, error
):
    env.settings["text"] = value
    request = call(env)
    env.owner.submit(request)
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.phase == "failed" and result.cleanup == "confirmed"
    assert result.model_returned is True and result.model_output is None
    assert result.output_state == state and result.error == error
    assert len(env.adapters[0].calls) == 1


def test_operation_json_fields_do_not_include_private_exception_details():
    assert "exception" not in AgentAdviceObservation.model_fields
    assert "stack" not in AgentAdviceObservation.model_fields


async def test_constructor_uncertainty_never_releases_or_replays_the_original(env, monkeypatch):
    constructions = []

    def unavailable(captured, **kwargs):
        constructions.append(captured)
        captured.release()
        raise RuntimeError("SYNTHETIC_PRIVATE_CONSTRUCTOR_DETAIL")

    monkeypatch.setattr(agent_operations, "open_captured_recipe", unavailable)
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    env.owner.submit(request)
    result = await env.owner.wait(lookup)
    assert result.phase == "setup_unconfirmed" and result.cleanup == "unconfirmed"
    assert result.error == "original_setup_unconfirmed"
    assert "SYNTHETIC_PRIVATE_CONSTRUCTOR_DETAIL" not in result.model_dump_json()
    assert env.owner.submit(request) == result and len(constructions) == 1
    assert constructions[0].consumed is True and constructions[0].key is None
    with pytest.raises(AgentAdviceUnavailable):
        env.owner.submit(call(env, "other"))
    assert await env.owner.close() is False


def test_advisory_scope_preserves_a_native_denial_and_restores_original_context():
    tools = native_tool_snapshot(catalog_available=True)

    async def original_admission(*args):
        pass

    async def advisory_admission(*args):
        pass

    with agent_tool_scope(tools, export_guard=lambda: False, export_admission=original_admission):
        assert current_agent_tools() is tools
        with pytest.raises(RuntimeError):
            with advisory_text_scope(lambda: True, advisory_admission):
                assert current_agent_tools() is None
                assert capture_agent_export_admission() is advisory_admission
                assert agent_export_permitted() is False
                raise RuntimeError("Synthetic operation exit")
        assert current_agent_tools() is tools
        assert capture_agent_export_admission() is original_admission
        assert agent_export_permitted() is False


@pytest.mark.parametrize(
    "update",
    [
        {"contract_version": True},
        {"objective_revision": True},
        {"messages": [{"role": "tool", "content": "unsupported protocol"}]},
        {"messages": [{"role": "user", "content": "\ud800"}]},
        {"messages": [{"role": "user", "content": "x" * (4 * 1024 * 1024)}]},
        {"context_event_ids": ["duplicate", "duplicate"]},
    ],
    ids=["version", "counter", "role", "unicode", "declared-request-bound", "duplicate-context"],
)
def test_request_protocol_and_resource_bounds_are_explicit(env, update):
    with pytest.raises(ValueError):
        call(env, **update)


async def test_resource_owner_keeps_the_original_until_adapter_cleanup_finishes(env):
    retired = []
    ownership = ActualOperation(retired.append)
    caller = object()
    ownership.hold(caller)
    env.settings["hold_close"] = True
    request = call(env)
    with actual_owner(ownership):
        env.owner.submit(request)
    ownership.release(caller)
    assert ownership.finished is False
    await env.created.wait()
    adapter = env.adapters[0]
    await adapter.close_entered.wait()
    assert ownership.finished is False and not retired
    adapter.release_close.set()
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.cleanup == "confirmed"
    assert ownership.finished is True and ownership.failed is False
    assert retired == [ownership] and not ownership.handles


async def test_unconfirmed_adapter_close_keeps_the_native_resource_owner_failed_and_held(env):
    ownership = ActualOperation(lambda owner: None)
    caller = object()
    ownership.hold(caller)
    env.settings["fail_close"] = True
    request = call(env)
    with actual_owner(ownership):
        env.owner.submit(request)
    ownership.release(caller)
    result = await env.owner.wait(AgentAdviceLookup.of(request))
    assert result.cleanup == "unconfirmed" and ownership.failed is True
    assert ownership.finished is False and ownership.handles
    assert env.owner._originals[request.logical_request_id] in ownership.handles


@pytest.mark.parametrize("fail_close", [False, True], ids=["confirmed-close", "failed-close"])
async def test_cancelled_operation_waits_for_its_actual_native_proof_close(
    env, monkeypatch, fail_close
):
    wire, _, _ = measured(env, monkeypatch)
    native = httpx.ASGITransport(app=env.app)
    entered, release = asyncio.Event(), asyncio.Event()
    closes = []

    class Body(httpx.AsyncByteStream):
        def __init__(self, raw):
            self.raw = raw

        async def __aiter__(self):
            yield self.raw

        async def aclose(self):
            closes.append(self)
            entered.set()
            await release.wait()
            if fail_close:
                raise RuntimeError("SYNTHETIC_PRIVATE_METADATA_CLOSE_DETAIL")

    async def handle(request):
        response = await native.handle_async_request(request)
        raw = await response.aread()
        return httpx.Response(response.status_code, headers=response.headers, stream=Body(raw))

    journal = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.MockTransport(handle)
    )
    env.owner.journal = journal
    ownership = ActualOperation(lambda owner: None)
    caller = object()
    ownership.hold(caller)
    request = call(env)
    lookup = AgentAdviceLookup.of(request)
    try:
        with actual_owner(ownership):
            env.owner.submit(request)
        ownership.release(caller)
        await entered.wait()
        original = env.owner._originals[request.logical_request_id]
        exchange = next(iter(journal._submit_exchanges))
        env.owner.cancel(lookup)
        await asyncio.wait_for(asyncio.shield(original.task), 1)
        result = await env.owner.wait(lookup, timeout=0.001)
        assert result.phase == "closing" and result.cleanup == "pending"
        assert original.active is True and ownership.finished is False
        assert await env.owner.close(timeout=0.001) is False
        with pytest.raises(AgentAdviceUnavailable):
            env.owner.submit(call(env, "other"))
        assert env.owner.submit(request) == result
        assert wire.predictions == 0 and len(closes) == 1
        release.set()
        await asyncio.gather(exchange.task, return_exceptions=True)
        result = await env.owner.wait(lookup)
        if fail_close:
            assert result.phase == "cleanup_unconfirmed" and result.cleanup == "unconfirmed"
            assert ownership.failed is True and ownership.finished is False
            assert original.active is True and original in ownership.handles
        else:
            assert result.phase == "cancelled" and result.cleanup == "confirmed"
            assert ownership.failed is False and ownership.finished is True
            assert original.active is False
        assert len(closes) == 1
    finally:
        release.set()
        await journal.close()
        await native.aclose()


async def test_actual_http_advice_retires_independently_of_other_parent_work(env, monkeypatch):
    wire, _, _ = measured(env, monkeypatch)
    journal = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    env.owner.journal = journal
    ownership = ActualOperation(lambda owner: None)
    unrelated = object()
    ownership.hold(unrelated)
    request = call(env)
    try:
        with actual_owner(ownership):
            env.owner.submit(request)
        result = await env.owner.wait(AgentAdviceLookup.of(request))
        assert result.phase == "completed" and result.cleanup == "confirmed"
        assert result.generation_admission_observed is True and wire.predictions == 1
        assert result.model_output == "Public result"
        assert not journal._submit_exchanges
        assert ownership.finished is False and ownership.failed is False
        assert ownership.handles == {unrelated}
        ownership.release(unrelated)
        assert ownership.finished is True and ownership.failed is False
    finally:
        await journal.close()
