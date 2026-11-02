"""Review-derived regressions for completed binding metadata replay.

Frozen after reviewing 71cd7023; these are not blinded implementation oracles.
The actual SQLite/ASGI/Coordinator/client/Agent/Session chain is used, with the
existing measured synthetic provider and dormant disposable worker fixture.
"""

import httpx
import pytest
from test_standing_agent_runtime import ADMIT, entries, finish, submit
from test_standing_agent_runtime import stack as stack
from test_standing_execution_api import SECRET
from test_standing_execution_api import factory as factory
from test_standing_resource_barrier import barriers as barriers
from test_standing_resource_barrier import seal_request

from packages.contracts.agent_submit import AgentSubmitUnavailable
from packages.contracts.standing_work import StandingUnavailable
from services.resources.standing_client import StandingResourceClient


def client(resources):
    return StandingResourceClient(
        "http://127.0.0.1:8764",
        "a" * 32,
        transport=httpx.ASGITransport(app=resources.app),
    )


async def close_agent(value, gateway):
    await value.runtime.close()
    value.runtime_closed = True
    assert all(provider.closed for provider in value.made)
    assert not value.runtime.model_settings.catalog_leases
    assert gateway.http.is_closed and not gateway._submit_exchanges


async def assert_live_bind_refuses_without_model_or_admission(value, gateway):
    value.runtime.standing_resource_client = gateway
    assert await value.journal.standing_task_lookup(value.original) is None
    assert value.made == [] and entries(value) == [] and value.transport.paths(ADMIT) == []
    with pytest.raises(AgentSubmitUnavailable):
        await submit(value)
    assert value.made == [] and entries(value) == [] and value.transport.paths(ADMIT) == []
    assert value.runtime._tasks == {}
    with pytest.raises(StandingUnavailable):
        await gateway.bind_standing_claim(value.claim)


async def test_same_controller_completed_binding_replay_still_permits_genuine_launch(
    stack, barriers
):
    value = await stack()
    resources = await barriers(session=value.env, claim=value.claim)
    gateway = client(resources)
    value.runtime.standing_resource_client = gateway
    try:
        first = await gateway.bind_standing_claim(value.claim)
        replay = await gateway.bind_standing_claim(value.claim)
        assert replay.request == first.request
        assert replay.request.resource_instance == resources.coordinator.controller_id
        assert (await gateway.lookup_standing_binding(value.claim)).request == first.request
        accepted = await submit(value)
        assert accepted.disposition == "created" and accepted.proof.claim == value.claim
        assert (await finish(value)).state.value == "succeeded"
        assert len(value.made) == 1 and len(entries(value)) == 1
        assert len(value.transport.paths(ADMIT)) == 1
    finally:
        await close_agent(value, gateway)
        await gateway.close()


async def test_resources_only_restart_retains_history_but_refuses_live_binding_before_model(
    stack, barriers
):
    value = await stack()
    original = await barriers(session=value.env, claim=value.claim)
    previous = client(original)
    first = await previous.bind_standing_claim(value.claim)
    await previous.close()
    await original.coordinator.close()
    successor = await barriers(session=value.env, claim=value.claim, path=original.path)
    gateway = client(successor)
    value.runtime.standing_resource_client = gateway
    try:
        assert successor.coordinator.controller_id != original.coordinator.controller_id
        assert successor.coordinator.standing_handoff.backend == (
            original.coordinator.standing_handoff.backend
        )
        historical = await gateway.lookup_standing_binding(value.claim)
        assert historical.request == first.request
        assert historical.request.resource_instance != successor.coordinator.controller_id
        await assert_live_bind_refuses_without_model_or_admission(value, gateway)
        assert (await gateway.lookup_standing_binding(value.claim)).request == first.request
    finally:
        await close_agent(value, gateway)
        await gateway.close()


async def test_sealed_binding_retains_history_but_refuses_live_binding_before_model(
    stack, barriers
):
    value = await stack()
    resources = await barriers(session=value.env, claim=value.claim)
    gateway = client(resources)
    value.runtime.standing_resource_client = gateway
    try:
        first = await gateway.bind_standing_claim(value.claim)
        await value.journal.standing_close_admission(value.original, capability=SECRET)
        request = seal_request(resources, first)
        drained = await gateway.seal_standing_claim(request)
        assert resources.coordinator.standing_barrier.sealed(value.task_id)
        assert (await gateway.lookup_standing_binding(value.claim)).request == first.request
        assert await gateway.lookup_standing_seal(request) == drained
        await assert_live_bind_refuses_without_model_or_admission(value, gateway)
        assert (await gateway.lookup_standing_binding(value.claim)).request == first.request
        assert await gateway.lookup_standing_seal(request) == drained
    finally:
        await close_agent(value, gateway)
        await gateway.close()
