"""Independent Agent-settlement transport gate, frozen before client source.

Old literal Resource graphs and the public new codec supply transport fixtures;
this is not independent codec, native registration, retirement or settlement
authority proof. All HTTP streams/exchanges/sole closes below remain actual.
"""

import asyncio
import copy
import gzip
import hashlib
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
    wire,
)
from test_standing_resource_finalization_contracts import vectors as resource_vectors

from packages.contracts import standing_agent_settlement as codec
from packages.contracts.client import JournalClient, _SubmitExchange
from packages.contracts.operation_ownership import ActualOperation, actual_owner
from packages.contracts.standing_resource_finalization import StandingResourceBindingRequest
from packages.contracts.standing_work import StandingConflict, StandingUnavailable

SECRET = bytes(range(32))
CAP = "X-Kinaiya-Standing-Settlement-Capability"
CLOSURE_CAP = "X-Kinaiya-Standing-Capability"
PREFIX = "/v1/standing/tasks"
REGISTRATION = "StandingAgentRetirementRegistrationRequest"
REGISTRATION_RECORD = "StandingAgentRetirementRegistrationRecord"
REGISTRATION_RECEIPT = "StandingAgentRetirementRegistrationReceipt"
SETTLEMENT = "StandingAgentSettlementRequest"
SETTLEMENT_RECORD = "StandingAgentSettlementRecord"
SETTLEMENT_RECEIPT = "StandingAgentSettlementReceipt"
QUERY = "StandingAgentSettlementQuery"
OPS = {
    "standing_retirement_register": (
        PREFIX + "/retirement/register",
        REGISTRATION,
        REGISTRATION_RECEIPT,
        149632,
    ),
    "standing_retirement_registration_lookup": (
        PREFIX + "/retirement/registration/lookup",
        REGISTRATION,
        REGISTRATION_RECEIPT,
        149632,
    ),
    "standing_settle": (PREFIX + "/settle", SETTLEMENT, SETTLEMENT_RECEIPT, 973952),
    "standing_settlement_lookup": (
        PREFIX + "/settlement/lookup",
        QUERY,
        SETTLEMENT_RECEIPT,
        973952,
    ),
}
LOOKUPS = {"standing_retirement_registration_lookup", "standing_settlement_lookup"}


def packets(*, large=False):
    original = copy.deepcopy(GOLDEN_INTENT)
    if large:
        original["task"]["objective"] = "\0" * 10500
    resources = resource_vectors(original=original)
    claim = resources["StandingResourceBindingRequest"]["claim"]
    owner = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    key = "agent-standing-register:transport:one"
    context = {
        "backend_id": original["expected_backend"],
        "claim_id": claim["claim_id"],
        "intent_digest": claim["intent_digest"],
        "executor_id": original["executor_id"],
        "owner_id": owner,
        "registration_key": key,
    }
    request = hashed(
        {
            "schema_version": "1.0",
            "claim": claim,
            "idempotency_key": key,
            "owner_id": owner,
            "capability_sha256": hashlib.sha256(
                b"kinaiya.standing.agent-retirement-capability.v1\0"
                + wire(context)
                + b"\0"
                + SECRET
            ).hexdigest(),
        },
        "registration_digest",
        "kinaiya.standing.agent-retirement-registration.v1",
    )
    registration = hashed(
        {
            "schema_version": "1.0",
            "request": request,
            "backend_id": original["expected_backend"],
            "registered_instance": original["expected_instance"],
            "registered_utc": "2026-11-02T01:02:03Z",
            "claim_origin": "fresh-v2-prepared",
        },
        "record_digest",
        "kinaiya.standing.agent-retirement-registration-record.v1",
    )
    settlement = hashed(
        {
            "schema_version": "1.0",
            "idempotency_key": "agent-standing-settle:transport:one",
            "registration": registration,
            "resources": resources["StandingResourceFinalizationReceipt"],
            "work": {
                "schema_version": "1.0",
                "kind": "launched-outcome-unconfirmed",
                "outcome": None,
            },
        },
        "settlement_digest",
        "kinaiya.standing.agent-settlement.v1",
    )
    record = hashed(
        {
            "schema_version": "1.0",
            "request": settlement,
            "backend_id": original["expected_backend"],
            "settled_instance": original["expected_instance"],
            "settled_utc": "2026-11-02T01:02:04Z",
        },
        "record_digest",
        "kinaiya.standing.agent-settlement-record.v1",
    )
    return {
        **resources,
        REGISTRATION: request,
        REGISTRATION_RECORD: registration,
        REGISTRATION_RECEIPT: {
            "schema_version": "1.0",
            "disposition": "created",
            "registration": registration,
        },
        SETTLEMENT: settlement,
        SETTLEMENT_RECORD: record,
        SETTLEMENT_RECEIPT: {
            "schema_version": "1.0",
            "disposition": "created",
            "settlement": record,
        },
        QUERY: {
            "schema_version": "1.0",
            "registration": registration,
            "settlement_key": settlement["idempotency_key"],
        },
    }


def typed(name, raw):
    return getattr(codec, name).model_validate(copy.deepcopy(raw))


def method(client, operation):
    value = getattr(client, operation, None)
    assert callable(value), "Frozen Agent settlement JournalClient capability is absent"
    return value


class Stream(Body):
    def __init__(self, raw):
        super().__init__(raw)
        self.cooperative_close = False

    async def aclose(self):
        self.close_calls += 1
        self.close_entered.set()
        if self.hold_close:
            if self.cooperative_close:
                await self.close_release.wait()
            else:
                await resistant(self.close_release)
        if self.close_error:
            raise OSError(MARKER)
        self.closed.set()


@asynccontextmanager
async def scenario(operation="standing_settle", *, raw=None, status=200, headers=None, large=False):
    data = packets(large=large)
    raw = wire(data[OPS[operation][2]]) if raw is None else raw
    body = Stream(raw)
    value = SimpleNamespace(
        data=data,
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
            stream = body if not value.requests else Stream(raw)
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

    value.client = JournalClient(
        "http://synthetic.invalid", "synthetic-agent-credential", transport=Transport()
    )
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
        await asyncio.wait_for(asyncio.gather(*value.tasks, return_exceptions=True), 8)
        await asyncio.wait_for(joined(value.client), 8)
        await value.client.close()
        assert not value.client._submit_exchanges and value.client.http.is_closed
        assert value.transport_closes == 1
        assert all(stream.close_calls == 1 for stream in value.bodies)
        assert all(response.is_closed for response in value.responses)


async def invoke(value, operation="standing_settle", request=None, *, secret=SECRET):
    request = (
        typed(OPS[operation][1], value.data[OPS[operation][1]]) if request is None else request
    )
    call = method(value.client, operation)
    return (
        await call(request, secret32=secret)
        if operation == "standing_settle"
        else await call(request)
    )


def test_frozen_agent_settlement_client_capability_is_present():
    # Root's bounded cold gate; absence is a missing feature, not a regression.
    for operation in OPS:
        assert callable(getattr(JournalClient, operation, None)), operation


@pytest.mark.parametrize("operation", OPS)
async def test_four_exact_routes_full_typed_bindings_and_one_original_close(operation):
    async with scenario(operation) as value:
        result = await invoke(value, operation)
        assert type(result) is getattr(codec, OPS[operation][2])
        assert result.model_dump(mode="json") == value.data[OPS[operation][2]]
        assert len(value.requests) == 1 and not value.client._submit_exchanges
        request = value.requests[0]
        assert request.method == "POST" and request.url.path == OPS[operation][0]
        assert request.headers["Authorization"] == "Bearer synthetic-agent-credential"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Content-Type"].split(";", 1)[0] == "application/json"
        assert request.content == wire(value.data[OPS[operation][1]])
        assert CLOSURE_CAP not in request.headers
        assert SECRET not in request.content and SECRET.hex().encode() not in request.content
        assert request.headers.get(CAP) == (
            SECRET.hex() if operation == "standing_settle" else None
        )
        assert len(request.headers.get_list(CAP)) == (1 if operation == "standing_settle" else 0)
        assert value.body.close_calls == 1 and value.body.closed.is_set()


@pytest.mark.parametrize("operation", OPS)
async def test_literal_null_is_exclusive_to_the_two_lookup_methods(operation):
    async with scenario(operation, raw=b" \nnull\t") as value:
        if operation in LOOKUPS:
            assert await invoke(value, operation) is None
        else:
            with pytest.raises(StandingUnavailable) as caught:
                await invoke(value, operation)
            safe(caught.value)
        assert value.body.close_calls == 1 and value.body.closed.is_set()


@pytest.mark.parametrize("operation", OPS)
async def test_valid_complete_foreign_response_is_not_borrowed_for_same_family(operation):
    data = packets()
    raw = copy.deepcopy(data[OPS[operation][2]])
    if OPS[operation][2] == REGISTRATION_RECEIPT:
        record = raw["registration"]
        record["request"]["owner_id"] = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        hashed(
            record["request"],
            "registration_digest",
            "kinaiya.standing.agent-retirement-registration.v1",
        )
        hashed(record, "record_digest", "kinaiya.standing.agent-retirement-registration-record.v1")
    else:
        record = raw["settlement"]
        if operation == "standing_settle":
            # Valid for the same registration and key, but not the full request.
            record["request"]["work"]["kind"] = "created-unlaunched"
        else:
            record["request"]["idempotency_key"] = "agent-standing-settle:foreign"
        hashed(record["request"], "settlement_digest", "kinaiya.standing.agent-settlement.v1")
        hashed(record, "record_digest", "kinaiya.standing.agent-settlement-record.v1")
    typed(OPS[operation][2], raw)
    async with scenario(operation, raw=wire(raw)) as value:
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value, operation)
        safe(caught.value)
        assert value.body.closed.is_set() and value.body.close_calls == 1


async def test_settlement_result_binds_the_entire_nested_resource_report():
    raw = copy.deepcopy(packets()[SETTLEMENT_RECEIPT])
    record = raw["settlement"]
    report = record["request"]["resources"]["finalization"]
    report["policy_revision"] += 1
    hashed(report, "finalization_digest", "kinaiya.standing.resource-finalization.v1")
    hashed(record["request"], "settlement_digest", "kinaiya.standing.agent-settlement.v1")
    hashed(record, "record_digest", "kinaiya.standing.agent-settlement-record.v1")
    typed(SETTLEMENT_RECEIPT, raw)
    async with scenario(raw=wire(raw)) as value:
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value)
        safe(caught.value)
        assert value.body.closed.is_set() and value.body.close_calls == 1


async def test_settlement_lookup_binds_complete_registration_not_only_claim_and_key():
    raw = copy.deepcopy(packets()[SETTLEMENT_RECEIPT])
    record = raw["settlement"]
    registration = record["request"]["registration"]
    registration["request"]["owner_id"] = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    hashed(
        registration["request"],
        "registration_digest",
        "kinaiya.standing.agent-retirement-registration.v1",
    )
    hashed(
        registration,
        "record_digest",
        "kinaiya.standing.agent-retirement-registration-record.v1",
    )
    hashed(record["request"], "settlement_digest", "kinaiya.standing.agent-settlement.v1")
    hashed(record, "record_digest", "kinaiya.standing.agent-settlement-record.v1")
    typed(SETTLEMENT_RECEIPT, raw)
    async with scenario("standing_settlement_lookup", raw=wire(raw)) as value:
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value, "standing_settlement_lookup")
        safe(caught.value)
        assert value.body.closed.is_set() and value.body.close_calls == 1


@pytest.mark.parametrize("operation", OPS)
async def test_actual_nested_metadata_forgery_refuses_before_any_http(operation):
    async with scenario(operation) as value:
        original = typed(OPS[operation][1], value.data[OPS[operation][1]])
        claim = (
            original.claim
            if OPS[operation][1] == REGISTRATION
            else original.registration.request.claim
        )
        claim.request.task.model_pin.__dict__[MARKER] = True
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value, operation, original)
        safe(caught.value)
        assert not value.requests and not value.client._submit_exchanges


@pytest.mark.parametrize("operation", ("standing_retirement_register", "standing_settle"))
async def test_complete_detached_request_is_frozen_before_first_public_await(operation):
    async with scenario(operation) as value:
        original = typed(OPS[operation][1], value.data[OPS[operation][1]])
        target = (
            original.claim.request.task
            if operation == "standing_retirement_register"
            else original.registration.request.claim.request.task
        )
        mutated = asyncio.Event()

        def mutate():
            target.__dict__["objective"] = MARKER
            mutated.set()

        async def run():
            # Runs on the public operation's first yield, independent of the
            # candidate's private freeze/exchange helper names or call counts.
            asyncio.get_running_loop().call_soon(mutate)
            return await invoke(value, operation, original)

        task = asyncio.create_task(run())
        value.tasks.append(task)
        result = await task
        assert mutated.is_set() and target.objective == MARKER
        assert result.model_dump(mode="json") == value.data[OPS[operation][2]]
        assert value.requests[0].content == wire(value.data[OPS[operation][1]])
        assert value.body.closed.is_set() and value.body.close_calls == 1


@pytest.mark.parametrize("secret", (None, bytearray(32), b"x" * 31, b"x" * 33))
async def test_settlement_secret_is_exact_bytes32_and_refuses_before_io(secret):
    async with scenario() as value:
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value, secret=secret)
        safe(caught.value)
        assert not value.requests and not value.client._submit_exchanges


@pytest.mark.parametrize("header", (CAP, CLOSURE_CAP, "X-Kinaiya-Standing-Unrecognized"))
async def test_ambient_private_header_cannot_leak_or_override_original_secret(header):
    # Root explicitly froze refusal even when an ambient settlement header
    # could otherwise be overwritten by the method's original explicit secret.
    for operation in OPS:
        async with scenario(operation) as value:
            value.client.http.headers[header] = "f" * 64
            with pytest.raises(StandingUnavailable) as caught:
                await invoke(value, operation)
            safe(caught.value)
            assert not value.requests and not value.client._submit_exchanges


@pytest.mark.parametrize("status", (302, 403, 409, 503))
async def test_status_mapping_is_input_free_and_never_follows_redirects(status):
    async with scenario(
        status=status, raw=wire({"detail": MARKER}), headers={"Location": "http://never.invalid"}
    ) as value:
        with pytest.raises(StandingConflict if status == 409 else StandingUnavailable) as caught:
            await invoke(value)
        safe(caught.value)
        assert len(value.requests) == 1 and value.body.close_calls == 1


@pytest.mark.parametrize(
    "fault", ("gzip", "media", "duplicate", "float", "overflow", "length", "declared", "read")
)
async def test_malformed_identity_json_or_stream_never_loses_the_original_close(fault):
    data = packets()
    raw, headers = wire(data[SETTLEMENT_RECEIPT]), {}
    if fault == "gzip":
        raw, headers = gzip.compress(raw, mtime=0), {"Content-Encoding": "gzip"}
    elif fault == "media":
        headers = {"Content-Type": "text/plain"}
    elif fault == "duplicate":
        raw = b'{"schema_version":"1.0",' + raw[1:]
    elif fault in {"float", "overflow"}:
        data[SETTLEMENT_RECEIPT]["settlement"]["request"]["resources"]["finalization"][
            "policy_revision"
        ] = 1.0 if fault == "float" else 9007199254740992
        raw = wire(data[SETTLEMENT_RECEIPT])
    elif fault == "length":
        headers = {"Content-Length": "1"}
    elif fault == "declared":
        headers = {"Content-Length": str(OPS["standing_settle"][3] + 1)}
    async with scenario(raw=raw, headers=headers) as value:
        value.body.read_error = fault == "read"
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value)
        safe(caught.value)
        assert value.body.close_calls == 1 and value.body.closed.is_set()


@pytest.mark.parametrize("operation", ("standing_retirement_register", "standing_settle"))
async def test_exact_response_cap_and_complete_large_graph_are_not_old262144_clamped(operation):
    data = packets(large=operation == "standing_settle")
    raw = wire(data[OPS[operation][2]])
    if operation == "standing_settle":
        assert 262144 < len(raw) <= OPS[operation][3]
        typed(SETTLEMENT_RECEIPT, data[SETTLEMENT_RECEIPT])
    padded = b" " * (OPS[operation][3] - len(raw)) + raw
    async with scenario(operation, raw=padded, large=operation == "standing_settle") as value:
        assert (await invoke(value, operation)).model_dump(mode="json") == data[OPS[operation][2]]
        assert value.requests[0].content == wire(data[OPS[operation][1]])
    async with scenario(
        operation, raw=b" " + padded, large=operation == "standing_settle"
    ) as value:
        with pytest.raises(StandingUnavailable) as caught:
            await invoke(value, operation)
        safe(caught.value)
        assert value.body.close_calls == 1


@pytest.mark.parametrize("phase", ("send", "read", "close"))
async def test_caller_cancel_retains_actual_send_read_or_sole_close_and_exact_scope(phase):
    async with scenario() as value:
        value.hold_send = phase == "send"
        value.body.hold_read = phase == "read"
        value.body.hold_close = phase == "close"
        entered = value.send_entered if phase == "send" else getattr(value.body, phase + "_entered")
        scope, root = ActualOperation(lambda _: None), object()
        scope.hold(root)
        with actual_owner(scope):
            task = asyncio.create_task(invoke(value))
            scope.retain_task(task)
        value.tasks.append(task)
        scope.release(root)
        await asyncio.wait_for(entered.wait(), 8)
        (exchange,) = value.client._submit_exchanges
        assert isinstance(exchange, _SubmitExchange) and exchange.actual_owner is scope
        original_work, original_close = exchange.task, exchange.close_task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exchange in value.client._submit_exchanges and exchange in scope.handles
        assert not scope.finished and not value.body.closed.is_set()
        value.send_release.set()
        value.body.read_release.set()
        value.body.close_release.set()
        await asyncio.wait_for(joined(value.client), 8)
        await asyncio.wait_for(scope.join(), 8)
        assert exchange.task is original_work and original_work.done()
        assert exchange.close_task.done() and not exchange.close_task.cancelled()
        if original_close is not None:
            assert exchange.close_task is original_close
        assert value.body.closed.is_set() and value.body.close_calls == 1
        assert scope.finished and not scope.failed


@pytest.mark.parametrize("failure", ("error", "cancelled"))
async def test_failed_original_close_is_sticky_and_never_replaced(failure):
    async with scenario() as value:
        value.body.hold_close = True
        value.body.cooperative_close = failure == "cancelled"
        scope, root = ActualOperation(lambda _: None), object()
        scope.hold(root)
        with actual_owner(scope):
            task = asyncio.create_task(invoke(value))
            scope.retain_task(task)
        value.tasks.append(task)
        scope.release(root)
        await asyncio.wait_for(value.body.close_entered.wait(), 8)
        (exchange,) = value.client._submit_exchanges
        close = exchange.close_task
        if failure == "cancelled":
            close.cancel()
        else:
            value.body.close_error = True
            value.body.close_release.set()
        with pytest.raises(StandingUnavailable) as caught:
            await task
        safe(caught.value)
        await asyncio.wait_for(joined(value.client), 8)
        await asyncio.wait_for(scope.join(), 8)
        assert exchange.close_task is close and close.done()
        assert (
            close.cancelled() if failure == "cancelled" else isinstance(close.exception(), OSError)
        )
        assert task.cancelling() == 0 and value.client._submit_cleanup_failed and scope.failed
        assert not value.body.closed.is_set() and value.body.close_calls == 1
        with pytest.raises(StandingUnavailable):
            await invoke(value)
        assert len(value.requests) == 1


async def test_25s_deadline_declines_without_caller_cancel_or_claiming_close_ended(monkeypatch):
    async with scenario() as value:
        value.body.hold_close = True
        loop, offset = asyncio.get_running_loop(), [0.0]
        real_time = loop.time
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: real_time() + offset[0])
            task = asyncio.create_task(invoke(value))
            value.tasks.append(task)
            await asyncio.wait_for(value.body.close_entered.wait(), 8)
            (exchange,) = value.client._submit_exchanges
            original_close = exchange.close_task
            offset[0] = 24
            await asyncio.sleep(0)
            assert not task.done()
            offset[0] = 26
            with pytest.raises(StandingUnavailable) as caught:
                await asyncio.wait_for(asyncio.shield(task), 8)
            safe(caught.value)
            assert task.cancelling() == 0 and exchange in value.client._submit_exchanges
            assert exchange.close_task is original_close and not original_close.done()
            assert not value.body.closed.is_set() and value.body.close_calls == 1
        value.body.close_release.set()
        await asyncio.wait_for(joined(value.client), 8)
        assert original_close.done() and value.body.closed.is_set()


async def test_real_new_and_old_owners_share64_capacity_until_originals_settle():
    operation = "standing_retirement_registration_lookup"
    async with scenario(operation, raw=b"null") as value:
        method(value.client, operation)
        value.hold_send = True
        request = typed(REGISTRATION, value.data[REGISTRATION])
        old = StandingResourceBindingRequest.model_validate(
            copy.deepcopy(value.data["StandingResourceBindingRequest"])
        )
        scope, root = ActualOperation(lambda _: None), object()
        scope.hold(root)
        with actual_owner(scope):
            tasks = [asyncio.create_task(invoke(value, operation, request)) for _ in range(63)]
            tasks.append(asyncio.create_task(value.client.standing_resource_binding_lookup(old)))
            for task in tasks:
                scope.retain_task(task)
        value.tasks.extend(tasks)
        async with asyncio.timeout(20):
            while len(value.requests) != 64 or len(value.client._submit_exchanges) != 64:
                await asyncio.sleep(0)
        assert all(isinstance(item, _SubmitExchange) for item in value.client._submit_exchanges)
        assert all(item.actual_owner is scope for item in value.client._submit_exchanges)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert len(value.client._submit_exchanges) == 64
        with pytest.raises(StandingUnavailable):
            await invoke(value, operation, request)
        assert len(value.requests) == 64
        value.send_release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(result is None for result in results[1:])
        await asyncio.wait_for(joined(value.client), 8)
        scope.release(root)
        await asyncio.wait_for(scope.join(), 8)
        assert scope.finished and not scope.failed
        assert all(stream.closed.is_set() and stream.close_calls == 1 for stream in value.bodies)
