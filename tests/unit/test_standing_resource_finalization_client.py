"""Independent frozen-v1 JournalClient transport oracles; no sockets or inference.

Only old codecs/fixtures and the frozen public interface informed these tests.
New client and Session finalization implementations were not consulted.
"""

import asyncio
import copy
import gzip
import hashlib
import importlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_standing_admission_contract import proof as old_proof
from test_standing_execution_contract import GOLDEN_INTENT, claim, initial_expected
from test_standing_resources_contract import vectors as old_vectors

from packages.contracts.client import JournalClient, _SubmitExchange
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_execution import StandingExecutionIntent
from packages.contracts.standing_resources import StandingResourceObservation, TaskResourceQuery
from packages.contracts.standing_work import StandingConflict, StandingUnavailable

MARKER = "SYNTHETIC-PRIVATE-FINALIZATION-TRANSPORT-DO-NOT-ECHO"
CAP_HEADER = "X-Kinaiya-Standing-Capability"
PREFIX = "/v1/standing/resources"
OPS = {
    "standing_resource_bind": (
        PREFIX + "/binding",
        "StandingResourceBindingRequest",
        "StandingResourceBindingReceipt",
        180224,
    ),
    "standing_resource_binding_lookup": (
        PREFIX + "/binding/lookup",
        "StandingResourceBindingRequest",
        "StandingResourceBindingReceipt",
        180224,
    ),
    "standing_resource_finalize_context": (
        PREFIX + "/finalization/context",
        "StandingResourceFinalizeRequest",
        "StandingResourceFinalizeContext",
        786432,
    ),
    "standing_resource_report": (
        PREFIX + "/finalization/report",
        "StandingResourceFinalization",
        "StandingResourceFinalizationReceipt",
        819200,
    ),
    "standing_resource_finalization_lookup": (
        PREFIX + "/finalization/lookup",
        "StandingResourceFinalizeRequest",
        "StandingResourceFinalizationReceipt",
        819200,
    ),
}
NULLABLE = {"standing_resource_binding_lookup", "standing_resource_finalization_lookup"}


def wire(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(domain, value):
    return hashlib.sha256(domain.encode() + b"\0" + wire(value)).hexdigest()


def hashed(value, field, domain):
    value[field] = digest(domain, {key: child for key, child in value.items() if key != field})
    return value


def vectors(*, large=False):
    original = copy.deepcopy(GOLDEN_INTENT)
    if large:
        original["task"]["objective"] = "\0" * 10200
    record, initial = claim(original), old_proof()
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
    reservation = copy.deepcopy(old_vectors()["StandingResourceReservation"])
    resource = reservation["context"]
    resource["standing"] = initial
    hashed(resource, "context_digest", "kinaiya.standing.resource-context.v1")
    closure = hashed(
        {
            "schema_version": "1.0",
            "intent_digest": record["intent_digest"],
            "claim_id": record["claim_id"],
            "initial": initial,
        },
        "closure_digest",
        "kinaiya.standing.admission-closure.v1",
    )
    observation = hashed(
        {
            "schema_version": "1.0",
            "intent_digest": record["intent_digest"],
            "claim_id": record["claim_id"],
            "closure_digest": closure["closure_digest"],
            "coverage_sequence": 16,
            "accepted_sequence": 17,
            "coverage": "accepted-after-bootstrap",
            "reservation": reservation,
        },
        "observation_digest",
        "kinaiya.standing.resource-observation.v1",
    )
    binding = {
        "schema_version": "1.0",
        "claim": record,
        "resource_backend": reservation["resource_backend"],
        "resource_instance": reservation["resource_instance"],
    }
    request = {
        "schema_version": "1.0",
        "idempotency_key": "client-finalization:one",
        "original": original,
        "expected_resource_backend": binding["resource_backend"],
        "expected_resource_instance": binding["resource_instance"],
    }
    context = hashed(
        {
            "schema_version": "1.0",
            "request": request,
            "binding": binding,
            "closure": closure,
            "observation": observation,
        },
        "context_digest",
        "kinaiya.standing.resource-finalize-context.v1",
    )
    report = hashed(
        {
            "schema_version": "1.0",
            "context": context,
            "resource_disposition": "matching-lease-finalized",
            "handoff_action_key": "synthetic café 🧭",
            "handoff_action_digest": reservation["action_digest"],
            "reserved_lease_id": reservation["lease_id"],
            "policy_revision": 5,
            "owner_intent_revision": 8,
            "restoration": "restored",
        },
        "finalization_digest",
        "kinaiya.standing.resource-finalization.v1",
    )
    return {
        "StandingResourceBindingRequest": binding,
        "StandingResourceBindingReceipt": {
            "schema_version": "1.0",
            "disposition": "created",
            "request": binding,
            "request_digest": digest("kinaiya.standing.resource-binding.v1", binding),
        },
        "StandingResourceFinalizeRequest": request,
        "StandingResourceFinalizeContext": context,
        "StandingResourceFinalization": report,
        "StandingResourceFinalizationReceipt": {
            "schema_version": "1.0",
            "disposition": "created",
            "finalization": report,
        },
    }


def typed(name, raw):
    return getattr(
        importlib.import_module("packages.contracts.standing_resource_finalization"), name
    ).model_validate(copy.deepcopy(raw))


def method(client, operation):
    invoke = getattr(client, operation, None)
    assert callable(invoke), "Frozen Resources finalization JournalClient wrapper is absent"
    return invoke


def safe(error):
    assert MARKER not in str(error) and len(str(error)) <= 256 and error.__cause__ is None


async def resistant(release):
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass


class Body(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw, self.close_calls = raw, 0
        self.hold_read = self.hold_close = self.read_error = self.close_error = False
        (
            self.read_entered,
            self.read_release,
            self.close_entered,
            self.close_release,
            self.closed,
        ) = (asyncio.Event() for _ in range(5))

    async def __aiter__(self):
        self.read_entered.set()
        if self.hold_read:
            await resistant(self.read_release)
        yield self.raw[:31]
        yield self.raw[31:]
        if self.read_error:
            raise OSError(MARKER)

    async def aclose(self):
        self.close_calls += 1
        self.close_entered.set()
        if self.hold_close:
            await resistant(self.close_release)
        if self.close_error:
            raise OSError(MARKER)
        self.closed.set()


async def joined(client):
    while client._submit_exchanges:
        tasks = {
            task
            for exchange in client._submit_exchanges
            for task in (exchange.task, exchange.close_task)
            if task is not None
        }
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


@asynccontextmanager
async def scenario(
    operation="standing_resource_report", *, raw=None, status=200, headers=None, large=False
):
    data = vectors(large=large)
    raw = wire(data[OPS[operation][2]]) if raw is None else raw
    body = Body(raw)
    env = SimpleNamespace(
        body=body,
        bodies=[],
        responses=[],
        requests=[],
        tasks=[],
        hold_send=False,
        send_entered=asyncio.Event(),
        send_release=asyncio.Event(),
        data=data,
    )

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            stream = body if not env.requests else Body(raw)
            env.bodies.append(stream)
            env.requests.append(request)
            env.send_entered.set()
            if env.hold_send:
                await resistant(env.send_release)
            response = httpx.Response(
                status,
                headers={"Content-Type": "application/json", **(headers or {})},
                stream=stream,
            )
            env.responses.append(response)
            return response

    env.client = JournalClient(
        "http://synthetic.invalid", "synthetic-resources-credential", transport=Transport()
    )
    try:
        yield env
    finally:
        env.send_release.set()
        for stream in env.bodies or [body]:
            stream.read_release.set()
            stream.close_release.set()
        for task in env.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*env.tasks, return_exceptions=True)
        await env.client.close()
        await asyncio.wait_for(joined(env.client), 3)
        assert not env.client._submit_exchanges and env.client.http.is_closed
        assert all(
            stream.close_calls == 1 for stream in env.bodies if stream.close_entered.is_set()
        )
        assert all(response.is_closed for response in env.responses)


async def call(env, operation, request=None):
    invoke = method(env.client, operation)
    request = typed(OPS[operation][1], env.data[OPS[operation][1]]) if request is None else request
    return await invoke(request)


def test_independent_large_vectors_still_validate_through_unchanged_old_children():
    data = vectors(large=True)
    context = data["StandingResourceFinalizeContext"]
    original = StandingExecutionIntent.model_validate(context["request"]["original"])
    assert StandingInitialProof.model_validate(context["closure"]["initial"]).validate_request(
        original
    )
    assert StandingResourceObservation.model_validate(context["observation"]).validate_request(
        original
    )
    assert 131072 < len(wire(data["StandingResourceFinalization"])) <= 802816


async def test_unchanged_old_resource_client_uses_actual_owned_stream_and_single_close():
    raw = old_vectors()["TaskResourceContext"]
    body = Body(wire(raw))
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"Content-Type": "application/json"}, stream=body)
    )
    client = JournalClient(
        "http://synthetic.invalid", "synthetic-resources-credential", transport=transport
    )
    try:
        value = await client.resource_task_context(TaskResourceQuery.model_validate(raw["query"]))
        assert value.model_dump(mode="json") == raw
        assert body.close_calls == 1 and body.closed.is_set()
    finally:
        await client.close()
        await joined(client)


@pytest.mark.parametrize("operation", OPS)
async def test_all_five_wrappers_route_full_frozen_request_and_wait_for_owned_close(operation):
    async with scenario(operation) as env:
        result = await call(env, operation)
        assert result.model_dump(mode="json") == env.data[OPS[operation][2]]
        assert env.body.closed.is_set() and env.body.close_calls == 1
        assert len(env.requests) == 1 and not env.client._submit_exchanges
        request = env.requests[0]
        assert request.method == "POST" and request.url.path == OPS[operation][0]
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Content-Type"].split(";", 1)[0] == "application/json"
        assert CAP_HEADER not in request.headers
        assert request.content == wire(env.data[OPS[operation][1]])


@pytest.mark.parametrize("operation", OPS)
async def test_only_declared_lookups_can_return_literal_null(operation):
    async with scenario(operation, raw=b" \nnull\t") as env:
        if operation in NULLABLE:
            assert await call(env, operation) is None
        else:
            with pytest.raises(StandingUnavailable) as caught:
                await call(env, operation)
            safe(caught.value)
        assert env.body.close_calls == 1


@pytest.mark.parametrize("operation", OPS)
async def test_full_original_response_joins_and_report_body_cannot_be_borrowed(operation):
    raw = copy.deepcopy(vectors()[OPS[operation][2]])
    if operation in {"standing_resource_bind", "standing_resource_binding_lookup"}:
        raw["request"]["resource_backend"] = "c" * 32
        raw["request_digest"] = digest("kinaiya.standing.resource-binding.v1", raw["request"])
    else:
        context = (
            raw
            if operation == "standing_resource_finalize_context"
            else raw["finalization"]["context"]
        )
        if operation == "standing_resource_report":
            # This remains valid for exactly the SAME finalize request.
            raw["finalization"]["policy_revision"] += 1
        else:
            context["request"]["idempotency_key"] = "client-finalization:other"
            hashed(context, "context_digest", "kinaiya.standing.resource-finalize-context.v1")
        if "finalization" in raw:
            hashed(
                raw["finalization"],
                "finalization_digest",
                "kinaiya.standing.resource-finalization.v1",
            )
    async with scenario(operation, raw=wire(raw)) as env:
        with pytest.raises(StandingUnavailable) as caught:
            await call(env, operation)
        safe(caught.value)
        assert env.body.close_calls == 1


@pytest.mark.parametrize("operation", OPS)
async def test_actual_nested_request_forgeries_are_rejected_before_io(operation):
    async with scenario(operation) as env:
        method(env.client, operation)
        value = typed(OPS[operation][1], env.data[OPS[operation][1]])
        target = (
            value.context.request.original.task.model_pin
            if operation == "standing_resource_report"
            else value.claim.request.task.model_pin
            if operation in {"standing_resource_bind", "standing_resource_binding_lookup"}
            else value.original.task.model_pin
        )
        target.__dict__[MARKER] = True
        with pytest.raises(StandingUnavailable) as caught:
            await call(env, operation, value)
        safe(caught.value)
        assert env.requests == [] and not env.client._submit_exchanges


@pytest.mark.parametrize("operation", ("standing_resource_bind", "standing_resource_report"))
async def test_ambient_private_capability_is_never_disclosed_to_resources_routes(operation):
    async with scenario(operation) as env:
        env.client.http.headers[CAP_HEADER] = "a" * 64
        try:
            await call(env, operation)
        except StandingUnavailable as error:
            safe(error)
            assert env.requests == []
        assert all(CAP_HEADER not in request.headers for request in env.requests)


@pytest.mark.parametrize("status", (302, 401, 403, 409, 422, 503))
async def test_status_failures_are_typed_input_free_never_redirected_and_owned_closed(status):
    async with scenario(
        status=status,
        raw=wire({"detail": MARKER}),
        headers={"Location": "http://never-follow.invalid"},
    ) as env:
        with pytest.raises(StandingConflict if status == 409 else StandingUnavailable) as caught:
            await call(env, "standing_resource_report")
        safe(caught.value)
        assert len(env.requests) == 1 and env.body.close_calls == 1


@pytest.mark.parametrize(
    "bad", ("compressed", "media", "duplicate", "utf8", "length", "transfer-length")
)
async def test_malformed_headers_and_raw_bodies_fail_without_losing_close_ownership(bad):
    raw, headers = wire(vectors()["StandingResourceFinalizationReceipt"]), {}
    if bad == "compressed":
        raw, headers = gzip.compress(raw, mtime=0), {"Content-Encoding": "gzip"}
    elif bad == "media":
        headers = {"Content-Type": "text/plain"}
    elif bad == "duplicate":
        raw = b'{"schema_version":"1.0",' + raw[1:]
    elif bad == "utf8":
        raw = b"\xff"
    elif bad == "length":
        headers = {"Content-Length": "1"}
    else:
        headers = {"Content-Length": str(len(raw)), "Transfer-Encoding": "chunked"}
    async with scenario(raw=raw, headers=headers) as env:
        with pytest.raises(StandingUnavailable) as caught:
            await call(env, "standing_resource_report")
        safe(caught.value)
        assert env.body.close_calls == 1


@pytest.mark.parametrize(
    "operation",
    ("standing_resource_bind", "standing_resource_finalize_context", "standing_resource_report"),
)
async def test_exact_per_class_response_limits_and_isolation_from_old_262144_clamp(operation):
    maximum = OPS[operation][3]
    raw = wire(vectors()[OPS[operation][2]])
    padded = b" " * (maximum - len(raw)) + raw
    async with scenario(operation, raw=padded) as env:
        assert (await call(env, operation)).model_dump(mode="json") == env.data[OPS[operation][2]]
    async with scenario(operation, raw=b" " + padded) as env:
        with pytest.raises(StandingUnavailable):
            await call(env, operation)
        assert env.body.close_calls == 1


async def test_valid_large_report_request_is_not_clamped_to_old_standing_request_limit():
    async with scenario(large=True) as env:
        result = await call(env, "standing_resource_report")
        assert (
            result.finalization.model_dump(mode="json") == env.data["StandingResourceFinalization"]
        )
        assert len(env.requests[0].content) > 131072


@pytest.mark.parametrize("phase", ("send", "read", "close"))
async def test_caller_cancellation_retains_actual_exchange_until_send_read_close_join(phase):
    async with scenario() as env:
        method(env.client, "standing_resource_report")
        env.hold_send = phase == "send"
        env.body.hold_read = phase == "read"
        env.body.hold_close = phase == "close"
        entered = env.send_entered if phase == "send" else getattr(env.body, phase + "_entered")
        task = asyncio.create_task(call(env, "standing_resource_report"))
        env.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 2)
        (exchange,) = env.client._submit_exchanges
        assert isinstance(exchange, _SubmitExchange)
        close_task = exchange.close_task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
        assert task.cancelling() and exchange in env.client._submit_exchanges
        env.send_release.set()
        env.body.read_release.set()
        env.body.close_release.set()
        await asyncio.wait_for(joined(env.client), 3)
        assert env.body.close_calls == 1 and env.body.closed.is_set()
        if phase == "close":
            assert exchange.close_task is close_task and close_task.done()


@pytest.mark.parametrize("phase", ("send", "read", "close"))
async def test_25_second_deadline_is_not_caller_cancellation_or_actual_cleanup(phase, monkeypatch):
    async with scenario() as env:
        method(env.client, "standing_resource_report")
        env.hold_send = phase == "send"
        env.body.hold_read = phase == "read"
        env.body.hold_close = phase == "close"
        entered = env.send_entered if phase == "send" else getattr(env.body, phase + "_entered")
        loop, offset = asyncio.get_running_loop(), [0.0]
        actual_time = loop.time
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: actual_time() + offset[0])
            task = asyncio.create_task(call(env, "standing_resource_report"))
            env.tasks.append(task)
            await asyncio.wait_for(entered.wait(), 2)
            (exchange,) = env.client._submit_exchanges
            offset[0] = 24
            await asyncio.sleep(0)
            assert not task.done()
            offset[0] = 26
            with pytest.raises(StandingUnavailable) as caught:
                await asyncio.wait_for(asyncio.shield(task), 2)
            safe(caught.value)
            assert task.cancelling() == 0 and exchange in env.client._submit_exchanges
            assert not env.body.closed.is_set()


async def test_success_requires_actual_close_and_close_failure_is_sticky():
    async with scenario() as env:
        method(env.client, "standing_resource_report")
        env.body.hold_close = True
        task = asyncio.create_task(call(env, "standing_resource_report"))
        env.tasks.append(task)
        await asyncio.wait_for(env.body.close_entered.wait(), 2)
        assert not task.done() and len(env.client._submit_exchanges) == 1
        env.body.close_error = True
        env.body.close_release.set()
        with pytest.raises(StandingUnavailable) as caught:
            await task
        safe(caught.value)
        await joined(env.client)
        assert env.client._submit_cleanup_failed and env.body.close_calls == 1
        with pytest.raises(StandingUnavailable):
            await call(env, "standing_resource_report")
        assert len(env.requests) == 1


async def test_client_close_retains_resistant_close_and_refuses_new_work_until_actual_join():
    async with scenario() as env:
        method(env.client, "standing_resource_report")
        env.body.hold_close = True
        task = asyncio.create_task(call(env, "standing_resource_report"))
        env.tasks.append(task)
        await asyncio.wait_for(env.body.close_entered.wait(), 2)
        (exchange,) = env.client._submit_exchanges
        close_task = exchange.close_task
        closing = asyncio.create_task(env.client.close())
        env.tasks.append(closing)
        await asyncio.wait_for(asyncio.shield(closing), 2)
        assert env.client.http.is_closed and exchange in env.client._submit_exchanges
        assert not close_task.done() and env.body.close_calls == 1
        with pytest.raises(StandingUnavailable):
            await call(env, "standing_resource_report")
        assert len(env.requests) == 1
        env.body.close_release.set()
        await asyncio.wait_for(joined(env.client), 3)
        with pytest.raises(StandingUnavailable):
            await task
        assert task.cancelling() == 0
        assert exchange.close_task is close_task and env.body.closed.is_set()


async def test_resistant_actual_owners_keep_shared_64_capacity_until_join():
    operation = "standing_resource_finalization_lookup"
    async with scenario(operation, raw=b"null") as env:
        method(env.client, operation)
        env.hold_send = True
        tasks = [asyncio.create_task(call(env, operation)) for _ in range(64)]
        env.tasks.extend(tasks)
        async with asyncio.timeout(3):
            while len(env.client._submit_exchanges) != 64 or len(env.requests) != 64:
                await asyncio.sleep(0)
        assert all(isinstance(value, _SubmitExchange) for value in env.client._submit_exchanges)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert len(env.client._submit_exchanges) == 64
        with pytest.raises(StandingUnavailable):
            await call(env, operation)
        assert len(env.requests) == 64
        env.send_release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(result is None for result in results[1:])
        await joined(env.client)
        assert all(body.closed.is_set() and body.close_calls == 1 for body in env.bodies)
