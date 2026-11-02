"""Prepared claim -> trusted Resources binding -> native Agent admission.

Uses the unchanged actual Session/runtime fixture and native binding endpoint;
only model completion is a measured synthetic fixture. No real inference.
"""

import asyncio
import copy

import httpx
import pytest
from test_standing_agent_runtime import (
    ADMIT,
    REFUSALS,
    entries,
    finish,
    submit,
)
from test_standing_agent_runtime import stack as stack
from test_standing_execution_api import SECRET
from test_standing_execution_api import factory as factory
from test_standing_resource_barrier import barriers as barriers

from packages.contracts.agent_submit import AgentSubmitUnavailable
from packages.contracts.client import JournalClient
from packages.contracts.standing_execution import StandingExecutionClaim, claim_digest
from packages.contracts.standing_resource_finalization import StandingResourceBindingRequest
from packages.contracts.standing_work import StandingUnavailable

CLAIM_LOOKUP = "/v1/standing/executions/lookup"


class NativeBinder:
    """The configured trusted gateway, backed by real resources-role Session IO."""

    def __init__(self, value, events):
        self.value, self.events = value, events
        self.client = JournalClient(
            "http://127.0.0.1:8760",
            "r" * 32,
            transport=httpx.ASGITransport(app=value.env.app),
        )
        self.calls, self.closed = [], False
        self.mode = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def bind_standing_claim(self, claim):
        self.events.append("bind")
        assert type(claim) is StandingExecutionClaim
        assert claim.canonical_bytes() == self.value.claim.canonical_bytes()
        self.calls.append(claim)
        self.entered.set()
        if self.mode == "hold":
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    pass
        if self.mode == "fail":
            raise StandingUnavailable
        if self.mode == "null":
            return None
        if self.mode == "foreign-prepared":
            raw = claim.model_dump(mode="json")
            raw["prepared_utc"] = "2026-11-01T12:00:01Z"
            raw["claim_digest"] = claim_digest(claim.request, raw["prepared_utc"])
            claim = StandingExecutionClaim.model_validate(raw)
        request = StandingResourceBindingRequest(
            schema_version="1.0",
            claim=claim,
            resource_backend="b" * 32,
            resource_instance="00000000-0000-4000-8000-000000000991",
        )
        if self.mode == "foreign-prepared":
            # A separately valid whole receipt, not malformed bytes, is still
            # not the exact native prepared claim the launch just obtained.
            from packages.contracts.standing_resource_finalization import (
                StandingResourceBindingReceipt,
                resource_binding_digest,
            )

            return StandingResourceBindingReceipt(
                schema_version="1.0",
                disposition="retained",
                request=request,
                request_digest=resource_binding_digest(request),
            )
        return await self.client.standing_resource_bind(request)

    async def close(self):
        self.release.set()
        await self.client.close()
        self.closed = True


@pytest.fixture
async def bound_agent(stack, monkeypatch):
    opened = []

    async def make(mode=None):
        value = await stack()
        events = []
        binder = NativeBinder(value, events)
        binder.mode = mode
        value.runtime.standing_resource_client = binder
        value.binder, value.order = binder, events
        opened.append(value)
        actual_lookup = value.journal.standing_claim_lookup
        actual_selection = value.runtime.model_settings.task_selection
        actual_admit = value.journal.standing_admit

        async def lookup(original):
            events.append("prepared")
            return await actual_lookup(original)

        def selection(*args, **kwargs):
            events.append("model")
            return actual_selection(*args, **kwargs)

        async def admit(original, *, capability):
            events.append("admit")
            assert capability == SECRET
            return await actual_admit(original, capability=capability)

        monkeypatch.setattr(value.journal, "standing_claim_lookup", lookup)
        monkeypatch.setattr(value.runtime.model_settings, "task_selection", selection)
        monkeypatch.setattr(value.journal, "standing_admit", admit)
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            value.binder.release.set()
            await value.binder.close()


async def test_actual_binding_precedes_owned_model_capture_and_native_admission(bound_agent):
    value = await bound_agent()
    assert (await submit(value)).disposition == "created"
    assert (
        value.order.index("prepared")
        < value.order.index("bind")
        < value.order.index("model")
        < value.order.index("admit")
    )
    assert len(value.binder.calls) == 1
    assert value.transport.paths(CLAIM_LOOKUP)
    assert (await finish(value)).state.value == "succeeded"
    assert len(entries(value)) == 1
    assert await value.journal.standing_unresolved() == value.claim


@pytest.mark.parametrize("mode", ["missing", "fail", "null", "foreign-prepared"])
async def test_missing_failed_or_mismatched_binding_never_allocates_or_admits(bound_agent, mode):
    value = await bound_agent(None if mode == "missing" else mode)
    if mode == "missing":
        value.runtime.standing_resource_client = None
    with pytest.raises(AgentSubmitUnavailable):
        await submit(value)
    assert "model" not in value.order and "admit" not in value.order
    assert value.made == [] and value.transport.paths(ADMIT) == [] and entries(value) == []
    assert value.runtime._tasks == {}
    assert await value.journal.standing_unresolved() == value.claim


async def test_retained_task_proof_is_read_only_without_binding_or_model_capture(bound_agent):
    value = await bound_agent()
    accepted = await value.journal.standing_admit(value.original, capability=SECRET)
    value.order.clear()
    value.runtime.standing_resource_client = None
    result = await submit(value)
    assert result.disposition == "retained" and result.proof == accepted.proof
    assert not {"bind", "model", "admit"}.intersection(value.order)
    assert value.binder.calls == [] and value.made == []
    assert value.runtime._tasks == {} and len(entries(value)) == 1


@pytest.mark.parametrize(
    "changed", ["binder", "journal", "workspace", "session", "shutdown", "cancel"]
)
async def test_actual_gateway_and_native_authority_are_rechecked_after_binding_await(
    bound_agent, changed
):
    value = await bound_agent("hold")
    operation = asyncio.create_task(submit(value))
    try:
        await asyncio.wait_for(value.binder.entered.wait(), 5)
        assert value.made == [] and entries(value) == []
        if changed == "binder":
            value.runtime.standing_resource_client = copy.copy(value.binder)
        elif changed == "journal":
            value.runtime.journal = copy.copy(value.journal)
        elif changed == "workspace":
            value.workspace.rename(value.workspace.with_name(value.workspace.name + "-old"))
            value.workspace.mkdir(mode=0o700)
        elif changed == "session":
            value.env.store.reset(value.env.session.session_id)
        elif changed == "shutdown":
            closing = asyncio.create_task(value.runtime.close())
            await asyncio.sleep(0)
        else:
            operation.cancel()
        value.binder.release.set()
        with pytest.raises((asyncio.CancelledError, *REFUSALS)):
            await operation
        assert value.made == [] and value.transport.paths(ADMIT) == []
        if changed == "shutdown":
            await closing
            value.runtime_closed = True
    finally:
        value.binder.release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        value.runtime.journal = value.journal
        value.runtime.standing_resource_client = value.binder


async def test_configured_client_is_closed_by_runtime_without_replacement(bound_agent):
    value = await bound_agent()
    await value.runtime.close()
    value.runtime_closed = True
    assert value.binder.closed and value.binder.client.http.is_closed


async def test_actual_coordinator_client_session_chain_launches_only_after_genuine_binding(
    stack, barriers
):
    import importlib

    value = await stack()
    resources = await barriers(session=value.env, claim=value.claim)
    kind = importlib.import_module("services.resources.standing_client").StandingResourceClient
    client = kind(
        "http://127.0.0.1:8764", "a" * 32, transport=httpx.ASGITransport(app=resources.app)
    )
    value.runtime.standing_resource_client = client
    try:
        accepted = await submit(value)
        assert accepted.disposition == "created" and accepted.proof.claim == value.claim
        binding = await resources.coordinator.lookup_standing_binding(value.claim)
        assert binding is not None and binding.request.claim == value.claim
        assert binding.request.resource_instance == resources.coordinator.controller_id
        assert (await finish(value)).state.value == "succeeded"
        assert len(entries(value)) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("phase", ["acquire", "close"])
async def test_agent_workspace_and_shutdown_keep_actual_configured_client_owner_after_cancel(
    stack, barriers, phase
):
    import importlib
    import os

    value = await stack()
    resources = await barriers(session=value.env, claim=value.claim)
    kind = importlib.import_module("services.resources.standing_client").StandingResourceClient
    delegate = httpx.ASGITransport(app=resources.app)
    entered, release = asyncio.Event(), asyncio.Event()
    bodies = []

    async def resistant():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    class Body(httpx.AsyncByteStream):
        def __init__(self, inner):
            self.inner, self.closes, self.closed = inner, 0, False

        async def __aiter__(self):
            async for chunk in self.inner:
                yield chunk

        async def aclose(self):
            self.closes += 1
            if phase == "close":
                entered.set()
                await resistant()
            await self.inner.aclose()
            self.closed = True

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await delegate.handle_async_request(request)
            body = Body(response.stream)
            bodies.append(body)
            response.stream = body
            if phase == "acquire":
                entered.set()
                await resistant()
            return response

        async def aclose(self):
            await delegate.aclose()

    client = kind("http://127.0.0.1:8764", "a" * 32, transport=Transport())
    value.runtime.standing_resource_client = client
    context = value.workspace_owner.active_context()
    operation = asyncio.create_task(submit(value))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert value.made == [] and entries(value) == []
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        state = value.runtime.workspace_operation_state()
        assert any(count for key, count in state.items() if key != "closed")
        closing = asyncio.create_task(value.runtime.close())
        await asyncio.sleep(0.02)
        assert not client.http.is_closed
        assert value.workspace_owner._lock_fd is not None
        assert os.fstat(context.root_fd).st_ino > 0
        release.set()
        await asyncio.wait_for(asyncio.shield(closing), 8)
        value.runtime_closed = True
        for _ in range(1000):
            value.workspace_owner.release_if_settled()
            if client.http.is_closed and value.workspace_owner._lock_fd is None:
                break
            await asyncio.sleep(0)
        assert client.http.is_closed and value.workspace_owner._lock_fd is None
        assert all(body.closed and body.closes == 1 for body in bodies)
        assert entries(value) == [] and value.made == []
    finally:
        release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await client.close()
