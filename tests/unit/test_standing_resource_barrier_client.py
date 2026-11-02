"""Frozen Resources barrier transport oracles; synthetic packets, no sockets.

These tests precede the independent client implementation. A native context is
metadata and never a physical finalization receipt or claim-release permit.
"""

import asyncio
import copy
import gzip
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_standing_execution_contract import claim as claim_vector
from test_standing_execution_contract import initial_expected
from test_standing_resource_finalization_client import digest, hashed, vectors, wire

from packages.contracts.standing_execution import StandingExecutionClaim
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceFinalizeRequest,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable

PREFIX = "/v1/resources/standing"
OPS = {
    "bind_standing_claim": (PREFIX + "/binding", "StandingResourceBindingReceipt", 180224),
    "lookup_standing_binding": (
        PREFIX + "/binding/lookup",
        "StandingResourceBindingReceipt",
        180224,
    ),
    "seal_standing_claim": (PREFIX + "/seal", "StandingResourceFinalizeContext", 786432),
    "lookup_standing_seal": (PREFIX + "/seal/lookup", "StandingResourceFinalizeContext", 786432),
}
LOOKUPS = {"lookup_standing_binding", "lookup_standing_seal"}
MARKER = "SYNTHETIC-PRIVATE-RESOURCE-BARRIER-DO-NOT-ECHO"


def large_vectors():
    # Independently enlarge a complete old native graph, rehashing every
    # enclosing body. The old 10,200-character fixture was only 256,769 bytes
    # as a context and cannot prove support above the unrelated 262,144 cap.
    data = vectors()
    request = data["StandingResourceFinalizeRequest"]
    request["original"]["task"]["objective"] = "\0" * 10500
    record = claim_vector(request["original"])
    context = data["StandingResourceFinalizeContext"]
    initial = context["closure"]["initial"]
    initial["claim"] = record
    native = {
        **initial_expected(record),
        **{key: initial[key] for key in ("event_id", "sequence", "created_at")},
        "origin": "standing-supervisor",
    }
    initial["entry_digest"] = digest(
        "kinaiya.standing.initial.v1",
        {"claim_id": record["claim_id"], "claim_digest": record["claim_digest"], "entry": native},
    )
    binding = data["StandingResourceBindingRequest"]
    binding["claim"] = record
    binding_receipt = data["StandingResourceBindingReceipt"]
    binding_receipt["request"] = binding
    binding_receipt["request_digest"] = digest("kinaiya.standing.resource-binding.v1", binding)
    closure = context["closure"]
    closure.update(
        intent_digest=record["intent_digest"], claim_id=record["claim_id"], initial=initial
    )
    hashed(closure, "closure_digest", "kinaiya.standing.admission-closure.v1")
    observation = context["observation"]
    resource_context = observation["reservation"]["context"]
    resource_context["standing"] = initial
    hashed(resource_context, "context_digest", "kinaiya.standing.resource-context.v1")
    observation.update(
        intent_digest=record["intent_digest"],
        claim_id=record["claim_id"],
        closure_digest=closure["closure_digest"],
    )
    hashed(observation, "observation_digest", "kinaiya.standing.resource-observation.v1")
    context.update(request=request, binding=binding, closure=closure, observation=observation)
    hashed(context, "context_digest", "kinaiya.standing.resource-finalize-context.v1")
    return data


def client_type():
    try:
        return importlib.import_module("services.resources.standing_client").StandingResourceClient
    except ModuleNotFoundError as exc:
        if exc.name != "services.resources.standing_client":
            raise
        pytest.fail("Frozen StandingResourceClient is absent", pytrace=False)


def proposal(data, operation):
    if "binding" in operation or operation == "bind_standing_claim":
        return StandingExecutionClaim.model_validate(
            copy.deepcopy(data["StandingResourceBindingRequest"]["claim"])
        )
    return StandingResourceFinalizeRequest.model_validate(
        copy.deepcopy(data["StandingResourceFinalizeRequest"])
    )


async def resistant(event):
    while not event.is_set():
        try:
            await event.wait()
        except asyncio.CancelledError:
            pass


class Body(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw, self.close_calls = raw, 0
        self.read_entered, self.read_release = asyncio.Event(), asyncio.Event()
        self.close_entered, self.close_release = asyncio.Event(), asyncio.Event()
        self.hold_read = self.hold_close = self.cooperative = False
        self.closed = False

    async def __aiter__(self):
        self.read_entered.set()
        if self.hold_read:
            await resistant(self.read_release)
        yield self.raw[:17]
        yield self.raw[17:]

    async def aclose(self):
        self.close_calls += 1
        self.close_entered.set()
        if self.hold_close:
            if self.cooperative:
                await self.close_release.wait()
            else:
                await resistant(self.close_release)
        self.closed = True


@asynccontextmanager
async def scenario(operation="bind_standing_claim", *, raw=None, status=200, headers=None):
    data = vectors()
    env = SimpleNamespace(
        data=data,
        requests=[],
        bodies=[],
        responses=[],
        tasks=[],
        send_entered=asyncio.Event(),
        send_release=asyncio.Event(),
        hold_send=False,
        hold_read=False,
        hold_close=False,
        cooperative=False,
    )
    payload = wire(data[OPS[operation][1]]) if raw is None else raw

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            env.requests.append(request)
            env.send_entered.set()
            if env.hold_send:
                await resistant(env.send_release)
            body = Body(payload)
            body.hold_read, body.hold_close = env.hold_read, env.hold_close
            body.cooperative = env.cooperative
            response = httpx.Response(
                status,
                headers={"Content-Type": "application/json", **(headers or {})},
                stream=body,
            )
            env.bodies.append(body)
            env.responses.append(response)
            return response

    client = env.client = client_type()("http://127.0.0.1:8764", "a" * 32, transport=Transport())
    try:
        yield env
    finally:
        env.send_release.set()
        for body in env.bodies:
            body.read_release.set()
            body.close_release.set()
        for task in env.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*env.tasks, return_exceptions=True)
        await client.close()
        # Observe the actual original close handles after any bounded facade.
        for _ in range(100):
            if all(response.is_closed for response in env.responses):
                break
            await asyncio.sleep(0)
        assert all(response.is_closed for response in env.responses)
        assert all(body.close_calls == 1 for body in env.bodies)
        assert client.http.is_closed


def start(env, operation, value=None):
    method = getattr(env.client, operation, None)
    assert callable(method), "Frozen Resources barrier client method is absent"
    task = asyncio.create_task(method(proposal(env.data, operation) if value is None else value))
    env.tasks.append(task)
    return task


@pytest.mark.parametrize("operation", OPS)
async def test_fixed_paths_complete_packet_identity_and_agent_credential(operation):
    async with scenario(operation) as env:
        result = await start(env, operation)
        (request,) = env.requests
        assert request.method == "POST" and request.url.path == OPS[operation][0]
        assert not request.url.query
        assert request.headers["authorization"] == "Bearer " + "a" * 32
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["content-type"] == "application/json"
        assert "x-kinaiya-standing-capability" not in request.headers
        assert request.content == proposal(env.data, operation).canonical_bytes()
        assert result.canonical_bytes() == wire(env.data[OPS[operation][1]])
        assert all(body.closed and body.close_calls == 1 for body in env.bodies)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8764",
        "http://example.org",
        "http://127.0.0.1:8764/path",
        "http://user@localhost:8764",
        "http://localhost:8764?x=1",
        "http://localhost:8764#x",
    ],
)
def test_only_plain_loopback_origins(url):
    with pytest.raises(ValueError):
        client_type()(url, "a" * 32)


@pytest.mark.parametrize("operation", OPS)
async def test_null_is_absence_only_for_lookup(operation):
    async with scenario(operation, raw=b"null") as env:
        if operation in LOOKUPS:
            assert await start(env, operation) is None
        else:
            with pytest.raises(StandingUnavailable):
                await start(env, operation)
        assert len(env.requests) == 1


@pytest.mark.parametrize("operation", OPS)
@pytest.mark.parametrize("fault", ["redirect", "encoding", "media", "duplicate", "oversize"])
async def test_no_retry_fallback_compression_or_ambiguous_packets(operation, fault):
    data, headers, status = vectors(), {}, 200
    raw = wire(data[OPS[operation][1]])
    if fault == "redirect":
        status, headers = 307, {"Location": "http://127.0.0.1:8760/forbidden"}
    elif fault == "encoding":
        raw, headers = gzip.compress(raw), {"Content-Encoding": "gzip"}
    elif fault == "media":
        headers = {"Content-Type": "text/plain"}
    elif fault == "duplicate":
        raw = b'{"schema_version":"1.0",' + raw[1:]
    else:
        raw = b" " * (OPS[operation][2] + 1)
    async with scenario(operation, raw=raw, status=status, headers=headers) as env:
        with pytest.raises(StandingUnavailable) as error:
            await start(env, operation)
        assert MARKER not in str(error.value) and error.value.__cause__ is None
        assert len(env.requests) == 1


@pytest.mark.parametrize("fault", ["raw", "child_dict", "extra", "wrong_prepared"])
async def test_binding_requires_whole_actual_claim_not_only_intent_id(fault):
    async with scenario() as env:
        claim = proposal(env.data, "bind_standing_claim")
        if fault == "raw":
            value = claim.model_dump(mode="json")
        elif fault == "child_dict":
            value = claim.model_copy(update={"request": claim.request.model_dump(mode="json")})
        elif fault == "extra":
            value = claim.model_copy(update={"untrusted_extra": MARKER})
        else:
            # The response is valid for another prepare instant, same original.
            value = claim.model_copy(update={"prepared_utc": "2026-11-01T12:00:01Z"})
        with pytest.raises((StandingUnavailable, ValueError)):
            await start(env, "bind_standing_claim", value)
        assert env.requests == []


@pytest.mark.parametrize("operation", ["bind_standing_claim", "seal_standing_claim"])
async def test_valid_foreign_response_never_matches_a_different_complete_original(operation):
    data = vectors()
    if operation == "bind_standing_claim":
        raw = data["StandingResourceBindingReceipt"]
        raw["request"]["claim"]["prepared_utc"] = "2026-11-01T12:00:01Z"
        from packages.contracts.standing_execution import claim_digest
        from packages.contracts.standing_resource_finalization import resource_binding_digest

        claim = raw["request"]["claim"]
        claim["claim_digest"] = claim_digest(
            proposal(vectors(), operation).request, claim["prepared_utc"]
        )
        from packages.contracts.standing_resource_finalization import StandingResourceBindingRequest

        raw["request_digest"] = resource_binding_digest(
            StandingResourceBindingRequest.model_validate(raw["request"])
        )
        StandingResourceBindingReceipt.model_validate(raw)
    else:
        # Same native context, another finalize idempotency key, valid own hash.
        raw = data["StandingResourceFinalizeContext"]
        raw["request"]["idempotency_key"] += ":foreign"
        from test_standing_resource_finalization_client import hashed

        hashed(raw, "context_digest", "kinaiya.standing.resource-finalize-context.v1")
    async with scenario(operation, raw=wire(raw)) as env:
        with pytest.raises(StandingUnavailable):
            await start(env, operation)
        assert len(env.requests) == 1


@pytest.mark.parametrize("phase", ["send", "read", "close"])
async def test_cancelled_facade_holds_shared64_until_actual_original_owners_settle(phase):
    async with scenario() as env:
        env.hold_send = phase == "send"
        env.hold_read = phase == "read"
        env.hold_close = phase == "close"
        tasks = [start(env, "bind_standing_claim") for _ in range(64)]
        for _ in range(1000):
            if len(env.requests) == 64 and (
                phase == "send"
                or len(env.bodies) == 64
                and all(
                    (body.read_entered if phase == "read" else body.close_entered).is_set()
                    for body in env.bodies
                )
            ):
                break
            await asyncio.sleep(0)
        assert len(env.requests) == 64
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        for operation in OPS:
            with pytest.raises(StandingUnavailable):
                await start(env, operation)
        assert len(env.requests) == 64
        env.send_release.set()
        for body in env.bodies:
            body.read_release.set()
            body.close_release.set()
        for _ in range(1000):
            if len(env.bodies) == 64 and all(body.closed for body in env.bodies):
                break
            await asyncio.sleep(0)
        assert len(env.bodies) == 64 and all(body.closed for body in env.bodies)
        # A resistant successful sole close is a positive completion control.
        env.hold_send = env.hold_read = env.hold_close = False
        assert await start(env, "bind_standing_claim") is not None


async def test_cooperative_cancelled_sole_close_is_sticky_after_handle_settlement():
    async with scenario() as env:
        env.hold_close = env.cooperative = True
        task = start(env, "bind_standing_claim")
        for _ in range(1000):
            if env.bodies and env.bodies[0].close_entered.is_set():
                break
            await asyncio.sleep(0)
        assert env.bodies and env.bodies[0].close_entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        env.bodies[0].close_release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        env.hold_close = False
        with pytest.raises(StandingUnavailable):
            await start(env, "lookup_standing_binding")
        assert len(env.requests) == 1


@pytest.mark.parametrize("operation", ["bind_standing_claim", "seal_standing_claim"])
async def test_full_large_complete_children_use_own_response_cap(operation):
    data = large_vectors()
    raw = wire(data[OPS[operation][1]])
    assert len(raw) <= OPS[operation][2]
    if operation == "seal_standing_claim":
        assert len(raw) > 262144
    async with scenario(operation, raw=raw) as env:
        result = await start(env, operation, proposal(data, operation))
        assert result.canonical_bytes() == raw


@pytest.mark.parametrize("operation", OPS)
async def test_closed_client_has_no_fallback_or_new_exchange(operation):
    async with scenario(operation) as env:
        await env.client.close()
        with pytest.raises(StandingUnavailable):
            await start(env, operation)
        assert env.requests == []


@pytest.mark.parametrize("operation", OPS)
async def test_private_executor_header_is_refused_before_any_send(operation):
    async with scenario(operation) as env:
        env.client.http.headers["X-Kinaiya-Standing-Capability"] = "00" * 32
        with pytest.raises(StandingUnavailable):
            await start(env, operation)
        assert env.requests == []


@pytest.mark.parametrize("phase", ["send", "close"])
async def test_client_shutdown_fences_new_calls_before_retiring_actual_http_owner(phase):
    async with scenario() as env:
        env.hold_send = phase == "send"
        env.hold_close = phase == "close"
        operation = start(env, "bind_standing_claim")
        for _ in range(1000):
            if (
                phase == "send"
                and env.send_entered.is_set()
                or (phase == "close" and env.bodies and env.bodies[0].close_entered.is_set())
            ):
                break
            await asyncio.sleep(0)
        assert env.send_entered.is_set()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        closing = asyncio.create_task(env.client.close())
        env.tasks.append(closing)
        await asyncio.sleep(0.02)
        assert not env.client.http.is_closed
        with pytest.raises(StandingUnavailable):
            await start(env, "lookup_standing_binding")
        assert len(env.requests) == 1
        env.send_release.set()
        for body in env.bodies:
            body.close_release.set()
        await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 8)
        for _ in range(1000):
            if env.client.http.is_closed:
                break
            # A late response body created after acquisition also gets its
            # original sole-close release; no second closer is introduced.
            for body in env.bodies:
                body.close_release.set()
            await asyncio.sleep(0)
        assert env.client.http.is_closed
        assert all(body.closed and body.close_calls == 1 for body in env.bodies)


def test_independent_large_complete_graph_control_is_valid_above_legacy_clamp():
    from packages.contracts.standing_resource_finalization import StandingResourceFinalizeContext

    data = large_vectors()
    raw = wire(data["StandingResourceFinalizeContext"])
    assert 262144 < len(raw) <= 786432
    context = StandingResourceFinalizeContext.from_json(raw)
    assert context.validate_request(proposal(data, "seal_standing_claim")) == context
    assert len(proposal(data, "bind_standing_claim").canonical_bytes()) <= 147456
    assert StandingResourceBindingReceipt.from_json(
        wire(data["StandingResourceBindingReceipt"])
    ).request.claim == proposal(data, "bind_standing_claim")


@pytest.mark.parametrize("operation", OPS)
async def test_valid_conflict_response_keeps_fixed_conflict_type_and_never_retries(operation):
    async with scenario(operation, status=409, raw=b'{"detail":"private synthetic detail"}') as env:
        with pytest.raises(StandingConflict) as error:
            await start(env, operation)
        assert "private synthetic detail" not in str(error.value)
        assert error.value.__cause__ is None and len(env.requests) == 1
