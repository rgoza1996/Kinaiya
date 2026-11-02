"""Independent frozen Resources finalization RPC client gate; no real sockets.

The native producer and older client fixtures are baseline code. The new RPC
implementation was not consulted. Original HTTP work/sole-close tasks remain
real; synthetic gates and wire faults never stand in for cleanup evidence.
"""

import asyncio
import copy
import gzip
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_standing_execution_contract import GOLDEN_INTENT
from test_standing_resource_finalization_client import (
    MARKER,
    Body,
    hashed,
    joined,
    resistant,
    safe,
    typed,
    vectors,
    wire,
)
from test_standing_resource_finalization_contracts import vectors as complete_vectors

from packages.contracts.client import _SubmitExchange
from packages.contracts.operation_ownership import ActualOperation, actual_owner
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.resources.standing_client import StandingResourceClient

FINALIZE = "/v1/resources/standing/finalization"
LOOKUP = FINALIZE + "/lookup"
OPS = (("finalize_standing_claim", FINALIZE), ("lookup_standing_finalization", LOOKUP))
CAP = "X-Kinaiya-Standing-Capability"
REQUEST = "StandingResourceFinalizeRequest"
RECEIPT = "StandingResourceFinalizationReceipt"


def method(client, name):
    value = getattr(client, name, None)
    assert callable(value), "Frozen Resources finalization transport capability is absent"
    return value


def packets(*, large=False):
    if not large:
        return vectors()
    # The independently frozen raw builder uses the same valid 10500-NUL
    # objective as the native large-graph fixture, within the old Task cap.
    original = copy.deepcopy(GOLDEN_INTENT)
    original["task"]["objective"] = "\0" * 10500
    return complete_vectors(original=original)


@asynccontextmanager
async def scenario(*, raw=None, headers=None, status=200, large=False):
    data = packets(large=large)
    raw = wire(data[RECEIPT]) if raw is None else raw
    body = Body(raw)
    value = SimpleNamespace(
        data=data,
        raw=raw,
        body=body,
        requests=[],
        bodies=[],
        responses=[],
        tasks=[],
        hold_send=False,
        send_entered=asyncio.Event(),
        send_release=asyncio.Event(),
        transport_closes=0,
    )

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            stream = body if not value.requests else Body(raw)
            value.requests.append(request)
            value.bodies.append(stream)
            value.send_entered.set()
            if value.hold_send:
                await resistant(value.send_release)
            response = httpx.Response(
                status,
                headers={"Content-Type": "application/json", **(headers or {})},
                stream=stream,
            )
            value.responses.append(response)
            return response

        async def aclose(self):
            value.transport_closes += 1

    value.client = StandingResourceClient("http://127.0.0.1:8764", "a" * 32, transport=Transport())
    try:
        yield value
    finally:
        value.send_release.set()
        for stream in value.bodies or [body]:
            stream.read_release.set()
            stream.close_release.set()
        for task in value.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*value.tasks, return_exceptions=True)
        try:
            await value.client.close()
        except StandingUnavailable:
            pass
        await asyncio.wait_for(asyncio.shield(value.client._close_task), 8)
        await asyncio.wait_for(joined(value.client), 8)
        assert value.client.http.is_closed and not value.client._submit_exchanges
        assert value.transport_closes == 1
        assert all(stream.close_calls == 1 for stream in value.bodies)
        assert all(response.is_closed for response in value.responses)


async def invoke(value, name="finalize_standing_claim", request=None):
    request = typed(REQUEST, value.data[REQUEST]) if request is None else request
    return await method(value.client, name)(request)


def test_frozen_resources_finalization_transport_capability_is_present():
    # The one bounded cold baseline check: absence is an unimplemented feature,
    # not a behavioral regression or native cleanup result.
    for name, _ in OPS:
        assert callable(getattr(StandingResourceClient, name, None)), (
            "Frozen Resources finalization transport capability is absent",
            name,
        )


@pytest.mark.parametrize("name,path", OPS)
async def test_exact_agent_post_routes_full_request_receipt_and_original_close(name, path):
    async with scenario() as value:
        result = await invoke(value, name)
        assert result.model_dump(mode="json") == value.data[RECEIPT]
        assert len(value.requests) == 1
        request = value.requests[0]
        assert request.method == "POST" and request.url.path == path
        assert request.headers["Authorization"] == "Bearer " + "a" * 32
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Content-Type"] == "application/json" and CAP not in request.headers
        assert request.content == wire(value.data[REQUEST])
        assert value.body.closed.is_set() and value.body.close_calls == 1
        assert not value.client._submit_exchanges


@pytest.mark.parametrize("name,path", OPS)
async def test_only_finalization_lookup_allows_literal_null(name, path):
    async with scenario(raw=b" \nnull\t") as value:
        if path == LOOKUP:
            assert await invoke(value, name) is None
        else:
            with pytest.raises(StandingUnavailable) as caught:
                await invoke(value, name)
            safe(caught.value)
        assert value.body.close_calls == 1


@pytest.mark.parametrize("name,path", OPS)
async def test_request_is_deeply_frozen_before_existing_first_readiness_await(
    name, path, monkeypatch
):
    async with scenario() as value:
        entered, release = asyncio.Event(), asyncio.Event()
        actual_ready = value.client._settle_ready

        async def ready(deadline):
            # Keep the actual original readiness operation; hold only return
            # from this existing await seam, without any fake registry owner.
            await actual_ready(deadline)
            entered.set()
            await resistant(release)

        monkeypatch.setattr(value.client, "_settle_ready", ready)
        request = typed(REQUEST, value.data[REQUEST])
        task = asyncio.create_task(invoke(value, name, request))
        value.tasks.append(task)
        try:
            await asyncio.wait_for(entered.wait(), 8)
            request.original.task.model_pin.__dict__["model"] = MARKER
            release.set()
            result = await asyncio.wait_for(asyncio.shield(task), 8)
            assert result.model_dump(mode="json") == value.data[RECEIPT]
            assert value.requests[0].url.path == path
            assert value.requests[0].content == wire(value.data[REQUEST])
        finally:
            release.set()


async def test_actual_forged_request_refuses_before_readiness_or_http(monkeypatch):
    async with scenario() as value:
        crossed = []
        actual_ready = value.client._settle_ready

        async def ready(deadline):
            crossed.append(True)
            return await actual_ready(deadline)

        monkeypatch.setattr(value.client, "_settle_ready", ready)
        request = typed(REQUEST, value.data[REQUEST])
        request.original.task.model_pin.__dict__[MARKER] = True
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value, request=request)
        safe(caught.value)
        assert crossed == [] and value.requests == [] and not value.client._submit_exchanges


@pytest.mark.parametrize("foreign", ("key", "backend", "controller"))
async def test_complete_valid_foreign_response_cannot_borrow_original_request(foreign):
    raw = copy.deepcopy(vectors()[RECEIPT])
    report, context = raw["finalization"], raw["finalization"]["context"]
    if foreign == "key":
        context["request"]["idempotency_key"] = "different-original-key"
    else:
        field = "resource_backend" if foreign == "backend" else "resource_instance"
        replacement = "c" * 32 if foreign == "backend" else "00000000-0000-4000-8000-000000000999"
        context["binding"][field] = replacement
        context["request"]["expected_" + field] = replacement
        context["observation"]["reservation"][field] = replacement
        hashed(
            context["observation"],
            "observation_digest",
            "kinaiya.standing.resource-observation.v1",
        )
    hashed(context, "context_digest", "kinaiya.standing.resource-finalize-context.v1")
    hashed(report, "finalization_digest", "kinaiya.standing.resource-finalization.v1")
    assert typed(RECEIPT, raw).model_dump(mode="json") == raw
    async with scenario(raw=wire(raw)) as value:
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value)
        safe(caught.value)
        assert value.body.close_calls == 1


@pytest.mark.parametrize("fault", ("compressed", "duplicate", "length", "oversize"))
async def test_strict_bounded_response_failure_keeps_single_original_close(fault):
    raw, headers = wire(vectors()[RECEIPT]), {}
    if fault == "compressed":
        raw, headers = gzip.compress(raw, mtime=0), {"Content-Encoding": "gzip"}
    elif fault == "duplicate":
        raw = b'{"schema_version":"1.0",' + raw[1:]
    elif fault == "length":
        headers = {"Content-Length": "1"}
    else:
        raw = b" " * (819201 - len(raw)) + raw
    async with scenario(raw=raw, headers=headers) as value:
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value)
        safe(caught.value)
        assert value.body.close_calls == 1


@pytest.mark.parametrize("status", (302, 409, 503))
async def test_fixed_failure_mapping_never_redirects_or_discloses_body(status):
    async with scenario(
        status=status, raw=wire({"detail": MARKER}), headers={"Location": "http://never.invalid"}
    ) as value:
        with pytest.raises(StandingConflict if status == 409 else StandingUnavailable) as caught:
            await invoke(value)
        safe(caught.value)
        assert len(value.requests) == 1 and value.body.close_calls == 1


@pytest.mark.parametrize("phase", ("send", "read", "close"))
async def test_cancelled_facade_retains_original_exchange_scope_and_dependency_close(phase):
    async with scenario() as value:
        method(value.client, "finalize_standing_claim")
        value.hold_send = phase == "send"
        value.body.hold_read = phase == "read"
        value.body.hold_close = phase == "close"
        entered = value.send_entered if phase == "send" else getattr(value.body, phase + "_entered")
        scope = ActualOperation(lambda _: None)
        root = object()
        scope.hold(root)
        with actual_owner(scope):
            task = asyncio.create_task(invoke(value))
            scope.retain_task(task)
        scope.release(root)
        value.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 8)
        (exchange,) = value.client._submit_exchanges
        assert isinstance(exchange, _SubmitExchange) and exchange.actual_owner is scope
        assert exchange in scope.handles
        original_close = exchange.close_task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 8)
        assert exchange in value.client._submit_exchanges and exchange in scope.handles
        closer = asyncio.create_task(value.client.close())
        value.tasks.append(closer)
        await asyncio.sleep(0)
        retained_close = value.client._close_task
        assert retained_close is not None and not retained_close.done()
        assert value.transport_closes == 0 and not value.client.http.is_closed
        value.send_release.set()
        value.body.read_release.set()
        value.body.close_release.set()
        await asyncio.gather(closer, return_exceptions=True)
        await asyncio.wait_for(asyncio.shield(retained_close), 8)
        await asyncio.wait_for(scope.join(), 8)
        assert value.client._close_task is retained_close and value.transport_closes == 1
        assert not value.client._submit_exchanges and not scope.failed
        assert value.body.close_calls == 1 and value.body.closed.is_set()
        assert exchange.task.done() and exchange.close_task.done()
        if phase == "close":
            assert exchange.close_task is original_close


async def test_actual_sole_close_failure_stays_unavailable_after_registry_retirement():
    async with scenario() as value:
        value.body.hold_close = True
        task = asyncio.create_task(invoke(value))
        value.tasks.append(task)
        await asyncio.wait_for(value.body.close_entered.wait(), 8)
        (exchange,) = value.client._submit_exchanges
        original_close = exchange.close_task
        assert not task.done() and not original_close.done()
        value.body.close_error = True
        value.body.close_release.set()
        with pytest.raises(StandingUnavailable) as caught:
            await task
        safe(caught.value)
        await asyncio.wait_for(joined(value.client), 8)
        assert original_close.done() and original_close.exception() is not None
        assert value.client._submit_cleanup_failed and value.body.close_calls == 1
        with pytest.raises(StandingUnavailable):
            await invoke(value, "lookup_standing_finalization")
        assert len(value.requests) == 1


async def test_complete_large_receipt_and_exact819200_bound_do_not_use_old_clamp():
    data = packets(large=True)
    raw = wire(data[RECEIPT])
    assert 262144 < len(raw) <= 819200
    padded = raw + b" " * (819200 - len(raw))
    async with scenario(raw=padded, large=True) as value:
        result = await invoke(value)
        assert result.model_dump(mode="json") == data[RECEIPT]
        assert len(value.raw) == 819200 and value.body.close_calls == 1


async def test_ambient_private_capability_is_not_sent_to_resources():
    async with scenario() as value:
        value.client.http.headers[CAP] = "a" * 64
        try:
            result = await invoke(value)
            assert result.model_dump(mode="json") == value.data[RECEIPT]
        except StandingUnavailable as caught:
            safe(caught)
            assert value.requests == []
        assert all(CAP not in request.headers for request in value.requests)


async def test_deadline_refusal_retains_original_close_without_caller_cancellation(monkeypatch):
    async with scenario() as value:
        value.body.hold_close = True
        loop, offset = asyncio.get_running_loop(), [0.0]
        actual_time = loop.time
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: actual_time() + offset[0])
            task = asyncio.create_task(invoke(value))
            value.tasks.append(task)
            await asyncio.wait_for(value.body.close_entered.wait(), 8)
            (exchange,) = value.client._submit_exchanges
            original_close = exchange.close_task
            offset[0] = 26
            with pytest.raises(StandingUnavailable) as caught:
                await asyncio.wait_for(asyncio.shield(task), 8)
            safe(caught.value)
            assert task.cancelling() == 0 and exchange in value.client._submit_exchanges
            assert exchange.close_task is original_close and not original_close.done()
            assert not value.body.closed.is_set()
