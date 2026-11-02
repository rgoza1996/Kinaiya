"""Independent #228 transport oracles, frozen without new client source.

Synthetic packets and real owned stream gates establish transport behavior,
not provider readiness, a scheduler, worker launch or positive settlement.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.agent_control import AgentModeChange
from packages.contracts.client import JournalClient
from packages.contracts.models import SessionRequest
from packages.contracts.standing_admission import (
    StandingInitialProof,
    StandingTaskAdmission,
    standing_initial_proof,
)
from packages.contracts.standing_dispatch import (
    StandingDispatchActivation,
    StandingProducerReceipt,
    StandingProducerRenewal,
)
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionClaimAdmission,
    StandingExecutionIntent,
    capability_digest,
    claim_digest,
    claim_id,
    intent_digest,
    occurrence_id,
    standing_initial_request,
)
from packages.contracts.standing_work import StandingChange, StandingConflict, StandingUnavailable
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import Store

SECRET = bytes(range(32))  # Public synthetic capability, never a real credential.
TOKEN = "synthetic-standing-agent-credential"
MARKER = "untrusted-response-detail-do-not-reflect"
BACKEND = "a" * 32
INSTANCE = "11111111-1111-1111-1111-111111111111"
RESPONSIBILITY = "22222222-2222-2222-2222-222222222222"
PRODUCER = "33333333-3333-3333-3333-333333333333"
EXECUTOR = "55555555-5555-5555-5555-555555555555"
TASK = "66666666-6666-6666-6666-666666666666"
OPERATIONS = {
    "standing_renew": ("POST", "/v1/standing/producer/renew", StandingProducerReceipt),
    "standing_prepare": (
        "POST",
        "/v1/standing/executions/prepare",
        StandingExecutionClaimAdmission,
    ),
    "standing_claim_lookup": ("POST", "/v1/standing/executions/lookup", StandingExecutionClaim),
    "standing_unresolved": ("GET", "/v1/standing/executions/unresolved", StandingExecutionClaim),
    "standing_admit": ("POST", "/v1/standing/tasks/admit", StandingTaskAdmission),
    "standing_task_lookup": ("POST", "/v1/standing/tasks/lookup", StandingInitialProof),
}
NULLABLE = ("standing_claim_lookup", "standing_unresolved", "standing_task_lookup")


def wire(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def renewal(backend=BACKEND, instance=INSTANCE, sequence=1):
    return StandingProducerRenewal(
        schema_version="1.0",
        expected_backend=backend,
        expected_instance=instance,
        producer_id=PRODUCER,
        sequence=sequence,
    )


def workspace():
    return {
        "schema_version": "1.0",
        "workspace_id": "f" * 64,
        "root_identity": "0" * 64,
        "settings_revision": 0,
        "source": "host",
        "shell_backend": "bubblewrap",
        "mode": "direct",
    }


def make_intent(
    *,
    key="transport-original",
    backend=BACKEND,
    instance=INSTANCE,
    task_session=None,
    producer=None,
    authorization_digest="b" * 64,
    record_digest="c" * 64,
    activation_digest="d" * 64,
):
    producer = producer or StandingProducerReceipt(
        schema_version="1.0",
        request=renewal(backend, instance),
        lease_token="44444444-4444-4444-4444-444444444444",
        issued_monotonic_ms=1000,
        expires_monotonic_ms=16000,
    )
    return StandingExecutionIntent.model_validate(
        {
            "schema_version": "1.0",
            "idempotency_key": key,
            "expected_backend": backend,
            "expected_instance": instance,
            "responsibility_id": RESPONSIBILITY,
            "authorization_revision": 1,
            "authorization_digest": authorization_digest,
            "record_digest": record_digest,
            "occurrence": {
                "schema_version": "1.0",
                "local_date": "2024-01-01",
                "due_utc": "2024-01-01T09:00:00Z",
                "utc_offset_seconds": 0,
            },
            "dispatch_epoch": 1,
            "activation_digest": activation_digest,
            "control_revision": 1,
            "producer": producer,
            "executor_id": EXECUTOR,
            "capability_sha256": capability_digest(backend, key, EXECUTOR, SECRET),
            "task": {
                "schema_version": "1.0",
                "task_id": TASK,
                "task_session": task_session
                or {"session_id": "synthetic-session", "thread_id": "synthetic-thread", "epoch": 1},
                "objective": 'Inspect synthetic résumé 😀 and "quoted" paths',
                "content_scope": "public_synthetic",
                "workspace_pin": workspace(),
                "harness_version": "seed-v1",
                "tool_subset": "native-files-v1",
                "model_pin": {
                    "schema_version": "1.0",
                    "profile_id": "synthetic-local",
                    "provider": "synthetic",
                    "model": "fixture-only",
                    "local": True,
                    "reasoning": None,
                    "settings_revision": 0,
                    "catalog_binding": None,
                    "catalog_revision": None,
                },
            },
        }
    )


def packets(original=None):
    original = original or make_intent()
    prepared = "2024-01-01T09:00:01Z"
    claim = StandingExecutionClaim(
        schema_version="1.0",
        request=original,
        claim_id=claim_id(original),
        occurrence_id=occurrence_id(original),
        intent_digest=intent_digest(original),
        prepared_utc=prepared,
        claim_digest=claim_digest(original, prepared),
    )
    entry = {
        **standing_initial_request(claim).model_dump(mode="json"),
        "event_id": "synthetic-native-é😀",
        "sequence": 7,
        "origin": "standing-supervisor",
        "created_at": "2024-01-01T09:00:02.000123Z",
    }
    proof = standing_initial_proof(claim, entry)
    return SimpleNamespace(
        original=original,
        claim=claim,
        proof=proof,
        results={
            "standing_renew": original.producer,
            "standing_prepare": StandingExecutionClaimAdmission(
                schema_version="1.0", disposition="created", claim=claim
            ),
            "standing_claim_lookup": claim,
            "standing_unresolved": claim,
            "standing_admit": StandingTaskAdmission(
                schema_version="1.0", disposition="created", proof=proof
            ),
            "standing_task_lookup": proof,
        },
    )


def call(client, operation, data, *, request=None, **kwargs):
    method = getattr(client, operation, None)
    assert callable(method), "Frozen standing transport capability is absent"
    if operation == "standing_unresolved":
        return method(**kwargs)
    if request is None:
        request = data.original.producer.request if operation == "standing_renew" else data.original
    return method(request, **kwargs)


async def resistant_gate(gate, cancelled):
    while not gate.is_set():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.set()


class Body(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.chunks = [raw[:31], raw[31:]]
        self.read_entered, self.read_release, self.read_cancelled = (
            asyncio.Event() for _ in range(3)
        )
        self.close_entered, self.close_release, self.close_cancelled = (
            asyncio.Event() for _ in range(3)
        )
        self.close_completed = asyncio.Event()
        self.hold_read = self.hold_close = self.close_error = False
        self.close_calls = 0

    async def __aiter__(self):
        self.read_entered.set()
        if self.hold_read:
            await resistant_gate(self.read_release, self.read_cancelled)
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.close_calls += 1
        self.close_entered.set()
        if self.hold_close:
            await resistant_gate(self.close_release, self.close_cancelled)
        if self.close_error:
            raise OSError(MARKER)
        self.close_completed.set()


async def join_exchanges(client):
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
async def scenario(operation="standing_prepare", *, raw=None, status=200, headers=None, data=None):
    data = data or packets()
    stream = Body(wire(data.results[operation].model_dump(mode="json")) if raw is None else raw)
    env = SimpleNamespace(
        data=data,
        body=stream,
        requests=[],
        hold_acquire=False,
        acquire_entered=asyncio.Event(),
        acquire_release=asyncio.Event(),
        acquire_cancelled=asyncio.Event(),
        tasks=[],
    )

    async def handle(request):
        env.requests.append(request)
        env.acquire_entered.set()
        if env.hold_acquire:
            await resistant_gate(env.acquire_release, env.acquire_cancelled)
        response_headers = {"Content-Type": "application/json", **(headers or {})}
        env.response = httpx.Response(
            status,
            stream=stream,
            headers=[
                (key.encode("ascii"), value.encode("utf-8"))
                for key, value in response_headers.items()
            ],
        )
        return env.response

    env.client = JournalClient(
        "http://synthetic.invalid", TOKEN, transport=httpx.MockTransport(handle)
    )
    try:
        yield env
    finally:
        env.acquire_release.set()
        stream.read_release.set()
        stream.close_release.set()
        for task in env.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*env.tasks, return_exceptions=True)
        await env.client.close()
        await asyncio.wait_for(join_exchanges(env.client), 1)


def safe(error):
    assert MARKER not in str(error) and SECRET.hex() not in str(error) and TOKEN not in str(error)
    assert len(str(error)) <= 256 and error.__cause__ is None


async def finished(task):
    _, pending = await asyncio.wait({task}, timeout=0.5)
    assert not pending, "Public standing deadline/cancellation did not settle"
    return task.result()


async def pump():
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)
    await event.wait()


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_six_exact_routes_canonical_bodies_identity_and_closed_owned_response(operation):
    async with scenario(operation) as env:
        kwargs = {"capability": SECRET} if operation == "standing_admit" else {}
        result = await call(env.client, operation, env.data, **kwargs)
        assert result == env.data.results[operation]
        assert len(env.requests) == 1
        request = env.requests[0]
        assert (request.method, request.url.path) == OPERATIONS[operation][:2]
        assert request.url.query == b"" and request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept-encoding"] == "identity"
        expected = (
            env.data.original.producer.request
            if operation == "standing_renew"
            else env.data.original
        )
        assert request.content == (
            b"" if operation == "standing_unresolved" else wire(expected.model_dump(mode="json"))
        )
        if request.method == "POST":
            assert request.headers["content-type"] == "application/json"
        assert request.headers.get("x-kinaiya-standing-capability") == (
            SECRET.hex() if operation == "standing_admit" else None
        )
        assert SECRET.hex().encode() not in request.content
        assert env.body.close_calls == 1 and env.body.close_completed.is_set()
        assert not env.client._submit_exchanges


@pytest.mark.parametrize("operation", NULLABLE)
async def test_only_real_json_null_is_observed_absence(operation):
    async with scenario(operation, raw=b" \nnull\t ") as env:
        assert await call(env.client, operation, env.data) is None
        assert env.body.close_completed.is_set() and env.body.close_calls == 1


@pytest.mark.parametrize("operation", ("standing_renew", "standing_prepare", "standing_admit"))
async def test_null_never_fabricates_a_required_admission_or_renewal(operation):
    async with scenario(operation, raw=b"null") as env:
        with pytest.raises(StandingUnavailable) as caught:
            await call(env.client, operation, env.data)
        safe(caught.value)
        assert env.body.close_completed.is_set()


@pytest.mark.parametrize(
    "bad",
    (
        "dict",
        "subclass",
        "outer_extra",
        "nested_extra",
        "forged_fields",
        "constructed_missing",
        "unsafe_integer",
        "oversized",
        "wrong_type",
    ),
)
async def test_complete_actual_typed_request_graph_is_validated_before_send(bad):
    data = packets()
    value = data.original
    if bad == "dict":
        value = value.model_dump(mode="json")
    elif bad == "subclass":

        class Derived(StandingExecutionIntent):
            pass

        value = Derived.model_validate(value.model_dump(mode="python"))
    elif bad == "outer_extra":
        value = value.model_copy(update={"unexpected": 1})
    elif bad == "nested_extra":
        value = value.model_copy(
            update={
                "task": value.task.model_copy(
                    update={"model_pin": value.task.model_pin.model_copy(update={"unexpected": 1})}
                )
            }
        )
    elif bad == "forged_fields":
        value = value.model_copy(deep=True)
        value.__pydantic_fields_set__.add("unexpected")
    elif bad == "constructed_missing":
        value = StandingExecutionIntent.model_construct(
            **{key: child for key, child in value.__dict__.items() if key != "task"}
        )
    elif bad == "unsafe_integer":
        value = value.model_copy(update={"authorization_revision": True})
    elif bad == "oversized":
        value = value.model_copy(
            update={"task": value.task.model_copy(update={"objective": "\x01" * 16000})}
        )
    elif bad == "wrong_type":
        value = data.original.producer.request
    async with scenario(data=data) as env:
        with pytest.raises(StandingUnavailable) as caught:
            await call(env.client, "standing_prepare", data, request=value)
        safe(caught.value)
        assert env.requests == []


@pytest.mark.parametrize("capability", (b"x" * 31, b"x" * 33, bytearray(SECRET), SECRET.hex()))
async def test_private_capability_is_exact_bytes32_before_any_send(capability):
    async with scenario("standing_admit") as env:
        with pytest.raises(StandingUnavailable) as caught:
            await call(env.client, "standing_admit", env.data, capability=capability)
        safe(caught.value)
        assert env.requests == []


async def test_missing_private_capability_is_omitted_for_historical_replay():
    data = packets()
    raw = wire(
        StandingTaskAdmission(
            schema_version="1.0", disposition="retained", proof=data.proof
        ).model_dump(mode="json")
    )
    async with scenario("standing_admit", raw=raw, data=data) as env:
        assert (await call(env.client, "standing_admit", data)).disposition == "retained"
        assert "x-kinaiya-standing-capability" not in env.requests[0].headers


@pytest.mark.parametrize(
    "operation",
    (
        "standing_renew",
        "standing_prepare",
        "standing_claim_lookup",
        "standing_admit",
        "standing_task_lookup",
    ),
)
async def test_internally_valid_other_original_proof_is_refused(operation):
    wrong = packets(make_intent(key="different-original"))
    if operation == "standing_renew":
        wrong.results[operation] = StandingProducerReceipt(
            schema_version="1.0",
            request=renewal(sequence=2),
            lease_token="44444444-4444-4444-4444-444444444444",
            issued_monotonic_ms=1000,
            expires_monotonic_ms=16000,
        )
    async with scenario(
        operation, raw=wire(wrong.results[operation].model_dump(mode="json"))
    ) as env:
        with pytest.raises(StandingUnavailable) as caught:
            await call(env.client, operation, env.data)
        safe(caught.value)
        assert env.body.close_completed.is_set()


@pytest.mark.parametrize("status", (409, 401, 403, 302, 503))
async def test_fixed_native_errors_do_not_read_echo_retry_or_follow(status):
    async with scenario(
        status=status,
        raw=wire({"detail": MARKER}),
        headers={"Location": "https://untrusted.invalid"},
    ) as env:
        with pytest.raises(StandingConflict if status == 409 else StandingUnavailable) as caught:
            await call(env.client, "standing_prepare", env.data)
        safe(caught.value)
        assert len(env.requests) == 1 and not env.body.read_entered.is_set()
        assert env.body.close_calls == 1 and env.body.close_completed.is_set()


@pytest.mark.parametrize(
    "case",
    (
        "media",
        "encoding",
        "unicode_length",
        "long_length",
        "transfer_length",
        "short",
        "extra",
        "nonbytes",
    ),
)
async def test_identity_raw_http_length_and_chunk_contract(case):
    raw = wire(packets().results["standing_prepare"].model_dump(mode="json"))
    headers = {
        "media": {"Content-Type": "text/plain"},
        "encoding": {"Content-Encoding": "gzip"},
        "unicode_length": {"Content-Length": "１２"},
        "long_length": {"Content-Length": "000000004"},
        "transfer_length": {"Content-Length": str(len(raw)), "Transfer-Encoding": "chunked"},
        "short": {"Content-Length": str(len(raw) + 1)},
        "extra": {"Content-Length": str(len(raw) - 1)},
    }.get(case, {})
    async with scenario(raw=raw, headers=headers) as env:
        if case == "nonbytes":
            env.body.chunks = [bytearray(raw)]
        with pytest.raises(StandingUnavailable) as caught:
            await call(env.client, "standing_prepare", env.data)
        safe(caught.value)
        assert env.body.close_completed.is_set() and env.body.close_calls == 1


@pytest.mark.parametrize(
    "operation",
    (
        "standing_renew",
        "standing_prepare",
        "standing_claim_lookup",
        "standing_admit",
        "standing_task_lookup",
    ),
)
async def test_actual_raw_body_limit_is_result_specific(operation):
    data = packets()
    raw = wire(data.results[operation].model_dump(mode="json"))
    maximum = OPERATIONS[operation][2].wire_bytes
    async with scenario(
        operation,
        raw=raw + b" " * (maximum - len(raw)),
        data=data,
        headers={"Content-Type": "APPLICATION/JSON; charset=utf-8", "Content-Encoding": "identity"},
    ) as env:
        assert await call(env.client, operation, data) == data.results[operation]
    async with scenario(operation, raw=raw + b" " * (maximum - len(raw) + 1), data=data) as env:
        with pytest.raises(StandingUnavailable):
            await call(env.client, operation, data)
        assert env.body.close_completed.is_set()


@pytest.mark.parametrize(
    "raw",
    (
        b"",
        b"{}",
        b'{"schema_version":"1.0","schema_version":"1.0"}',
        b"\xff",
        b"NaN",
        b"null trailing",
    ),
)
async def test_malformed_or_nonproof_bytes_never_become_absence(raw):
    async with scenario("standing_claim_lookup", raw=raw) as env:
        with pytest.raises(StandingUnavailable) as caught:
            await call(env.client, "standing_claim_lookup", env.data)
        safe(caught.value)
        assert env.body.close_completed.is_set()


@pytest.mark.parametrize("bad", ("bool", "float", "outer_extra", "nested_extra", "digest"))
async def test_response_proof_actual_json_types_closure_and_hash_are_checked(bad):
    payload = packets().results["standing_prepare"].model_dump(mode="json")
    if bad in {"bool", "float"}:
        payload["claim"]["request"]["authorization_revision"] = True if bad == "bool" else 1.0
    elif bad == "outer_extra":
        payload["unexpected"] = MARKER
    elif bad == "nested_extra":
        payload["claim"]["request"]["task"]["model_pin"]["unexpected"] = MARKER
    else:
        payload["claim"]["claim_digest"] = "f" * 64
    async with scenario(raw=wire(payload)) as env:
        with pytest.raises(StandingUnavailable) as caught:
            await call(env.client, "standing_prepare", env.data)
        safe(caught.value)
        assert env.body.close_calls == 1 and env.body.close_completed.is_set()


async def test_full_original_is_frozen_before_transport_acquisition():
    async with scenario() as env:
        original_wire = wire(env.data.original.model_dump(mode="json"))
        env.hold_acquire = True
        task = asyncio.create_task(call(env.client, "standing_prepare", env.data))
        env.tasks.append(task)
        await asyncio.wait_for(env.acquire_entered.wait(), 1)
        object.__setattr__(env.data.original.task, "objective", "mutated after admission")
        env.acquire_release.set()
        result = await finished(task)
        assert wire(result.claim.request.model_dump(mode="json")) == original_wire
        assert env.requests[0].content == original_wire


@pytest.mark.parametrize("phase", ("acquire", "read", "close"))
@pytest.mark.parametrize("refusal", ("cancel", "deadline"))
async def test_actual_late_owners_survive_public_refusal_and_close_exactly_once(
    phase, refusal, monkeypatch
):
    async with scenario("standing_admit") as env:
        if phase == "acquire":
            env.hold_acquire = True
        else:
            setattr(env.body, "hold_" + phase, True)
        entered = (
            env.acquire_entered if phase == "acquire" else getattr(env.body, phase + "_entered")
        )
        loop = asyncio.get_running_loop()
        real_time, offset = loop.time, [0.0]
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: real_time() + offset[0])
            task = asyncio.create_task(
                call(env.client, "standing_admit", env.data, capability=SECRET)
            )
            env.tasks.append(task)
            await asyncio.wait_for(entered.wait(), 1)
            if refusal == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await finished(task)
            else:
                offset[0] = 24
                await pump()
                assert not task.done(), "25-second total deadline expired early"
                offset[0] = 26
                with pytest.raises(StandingUnavailable) as caught:
                    await finished(task)
                safe(caught.value)
        assert len(env.client._submit_exchanges) == 1
        if phase == "close":
            assert env.response.is_closed and not env.body.close_completed.is_set()
        env.acquire_release.set()
        env.body.read_release.set()
        env.body.close_release.set()
        await asyncio.wait_for(join_exchanges(env.client), 1)
        assert env.body.close_completed.is_set() and env.body.close_calls == 1
        assert len(env.requests) == 1 and task.done()


async def test_deadline_starts_before_full_request_serialization(monkeypatch):
    async with scenario() as env:
        loop = asyncio.get_running_loop()
        real_time, offset = loop.time, [0.0]
        canonical = StandingExecutionIntent.canonical_bytes

        def delayed(value):
            offset[0] = 26
            return canonical(value)

        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: real_time() + offset[0])
            patch.setattr(StandingExecutionIntent, "canonical_bytes", delayed)
            with pytest.raises(StandingUnavailable):
                await call(env.client, "standing_prepare", env.data)
        assert env.requests == []


async def test_success_waits_for_real_close_and_failed_close_latches_shared_owner():
    async with scenario() as env:
        env.body.hold_close = True
        task = asyncio.create_task(call(env.client, "standing_prepare", env.data))
        env.tasks.append(task)
        await asyncio.wait_for(env.body.close_entered.wait(), 1)
        assert not task.done() and env.response.is_closed and not env.body.close_completed.is_set()
        env.body.close_error = True
        env.body.close_release.set()
        with pytest.raises(StandingUnavailable) as caught:
            await finished(task)
        safe(caught.value)
        await asyncio.wait_for(join_exchanges(env.client), 1)
        assert env.client._submit_cleanup_failed and env.body.close_calls == 1
        for operation in OPERATIONS:
            with pytest.raises(StandingUnavailable):
                await call(env.client, operation, env.data)
        assert len(env.requests) == 1


async def test_closed_shared_owner_refuses_before_send():
    async with scenario() as env:
        await env.client.close()
        with pytest.raises(StandingUnavailable):
            await call(env.client, "standing_prepare", env.data)
        assert env.requests == []


async def test_global64_counts_actual_old_and_new_late_exchanges_until_cleanup():
    data = packets()
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests, streams, public_tasks = [], [], []

    async def handle(request):
        requests.append(request)
        if len(requests) == 64:
            entered.set()
        await resistant_gate(release, cancelled)
        stream = Body(wire(data.results["standing_prepare"].model_dump(mode="json")))
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"Content-Type": "application/json"})

    client = JournalClient("http://synthetic.invalid", TOKEN, transport=httpx.MockTransport(handle))
    try:
        public_tasks.append(asyncio.create_task(client.agent_control_state()))
        for _ in range(63):
            public_tasks.append(asyncio.create_task(call(client, "standing_prepare", data)))
        await asyncio.wait_for(entered.wait(), 1)
        assert len(client._submit_exchanges) == 64 and len(requests) == 64
        for task in public_tasks:
            task.cancel()
        results = await asyncio.gather(*public_tasks, return_exceptions=True)
        assert all(isinstance(value, asyncio.CancelledError) for value in results)
        assert len(client._submit_exchanges) == 64
        with pytest.raises(StandingUnavailable):
            await call(client, "standing_prepare", data)
        assert len(requests) == 64
        release.set()
        await asyncio.wait_for(join_exchanges(client), 1)
        assert len(streams) == 64 and all(
            s.close_calls == 1 and s.close_completed.is_set() for s in streams
        )
        assert await call(client, "standing_prepare", data) == data.results["standing_prepare"]
        assert len(requests) == 65
    finally:
        release.set()
        for task in public_tasks:
            task.cancel()
        await asyncio.gather(*public_tasks, return_exceptions=True)
        await client.close()
        await asyncio.wait_for(join_exchanges(client), 1)


@pytest.fixture
async def native(tmp_path):
    now = [datetime(2024, 1, 1, 8, 59, 0, tzinfo=UTC)]
    store = Store(tmp_path / "native.sqlite")
    tokens = {
        role: role + "-synthetic-" + "x" * 32
        for role in ("owner", "agent", "converse", "decisions", "resources")
    }
    app = create_app(
        store, Auth(tokens), standing_clock=lambda: now[0], standing_monotonic_ms=lambda: 1000
    )
    try:
        async with app.router.lifespan_context(app):
            control = app.state.agent_control.snapshot()
            session = store.create_session(SessionRequest(channel="agent"))
            scope = {
                field: getattr(session, field) for field in ("session_id", "thread_id", "epoch")
            }
            original = make_intent(
                backend=control.backend_id, instance=control.instance_id, task_session=scope
            )
            change = StandingChange.model_validate(
                {
                    "schema_version": "1.0",
                    "idempotency_key": "native-owner",
                    "expected_backend": control.backend_id,
                    "expected_instance": control.instance_id,
                    "expected_revision": 0,
                    "responsibility_id": RESPONSIBILITY,
                    "authorization": {
                        "label": "Native synthetic client fixture",
                        "objective": original.task.objective,
                        "content_scope": original.task.content_scope,
                        "enabled": True,
                        "task_session": scope,
                        "workspace_pin": workspace(),
                        "schedule": {
                            "kind": "daily_local",
                            "timezone": "UTC",
                            "local_time": "09:00",
                            "fold_policy": "earlier",
                            "gap_policy": "skip",
                            "grace_seconds": 600,
                            "missed_run_policy": "latest_within_grace",
                            "start_local_date": "2024-01-01",
                            "valid_until_utc": "2024-01-02T10:00:00Z",
                        },
                        "delivery": {
                            "kind": "in_app",
                            "attention": "routine",
                            "preview": "status_only",
                            "quiet_hours": None,
                        },
                        "overlap_policy": "one_unresolved_per_authorization",
                        "maximum_runs": 1,
                        "inference_authorization": "configured_agent_profile",
                    },
                }
            )
            app.state.standing_owner.change(change)
            producer_request = renewal(control.backend_id, control.instance_id)
            producer = app.state.standing_dispatch_owner.renew(producer_request)
            dispatch = app.state.standing_dispatch_owner.snapshot()
            activation = app.state.standing_dispatch_owner.activate(
                StandingDispatchActivation(
                    schema_version="1.0",
                    expected_epoch=dispatch.epoch,
                    expected_fence_token=dispatch.fence_token,
                    mode_change=AgentModeChange(
                        schema_version="1.0",
                        idempotency_key="native-activation",
                        expected_backend=control.backend_id,
                        expected_instance=control.instance_id,
                        expected_revision=control.revision,
                        mode="autonomous",
                        active_work_policy="finish_already_accepted",
                    ),
                )
            )
            record = app.state.standing_owner.snapshot(RESPONSIBILITY).record
            original = make_intent(
                backend=control.backend_id,
                instance=control.instance_id,
                task_session=scope,
                producer=producer,
                authorization_digest=record.receipt.authorization_digest,
                record_digest=record.record_digest,
                activation_digest=activation.activation_digest,
            )
            now[0] = datetime(2024, 1, 1, 9, 0, 1, tzinfo=UTC)
            yield SimpleNamespace(
                store=store,
                app=app,
                tokens=tokens,
                original=original,
                producer_request=producer_request,
                now=now,
            )
    finally:
        store.close()


async def test_existing_pure_packets_and_actual_native_fixture_control(native):
    data = packets()
    assert (
        data.proof.validate_request(data.original).journal_entry().kind == "agent.standing-command"
    )
    assert (
        native.app.state.standing_execution_initialized
        and native.app.state.standing_task_initialized
    )
    assert native.app.state.standing_execution_owner.unresolved() is None
    assert native.app.state.standing_task_owner.lookup(native.original) is None
    assert (
        native.store.db.execute(
            "SELECT count(*) FROM entries WHERE kind='agent.standing-command'"
        ).fetchone()[0]
        == 0
    )


async def test_production_client_to_actual_asgi_sqlite_native_chain_and_retained_history(native):
    client = JournalClient(
        "http://synthetic.invalid",
        native.tokens["agent"],
        transport=httpx.ASGITransport(app=native.app),
    )
    data = SimpleNamespace(original=native.original)
    try:
        assert await call(client, "standing_unresolved", data) is None
        assert await call(client, "standing_claim_lookup", data) is None
        assert await call(client, "standing_task_lookup", data) is None
        assert (
            await call(client, "standing_renew", data, request=native.producer_request)
            == native.original.producer
        )
        claim = await call(client, "standing_prepare", data)
        assert claim.disposition == "created" and claim.claim.validate_request(native.original)
        assert await call(client, "standing_claim_lookup", data) == claim.claim
        assert await call(client, "standing_unresolved", data) == claim.claim
        with pytest.raises(StandingConflict):
            await call(client, "standing_admit", data)
        admitted = await call(client, "standing_admit", data, capability=SECRET)
        assert admitted.disposition == "created" and admitted.proof.claim == claim.claim
        assert await call(client, "standing_task_lookup", data) == admitted.proof
        native.app.state.standing_dispatch_owner.stop(
            native.app.state.standing_dispatch_owner.stop_context()
        )
        replay = await call(client, "standing_admit", data)
        assert replay.disposition == "retained" and replay.proof == admitted.proof
        assert (await call(client, "standing_prepare", data)).disposition == "retained"
        assert (
            native.store.db.execute(
                "SELECT count(*) FROM entries WHERE kind='agent.standing-command'"
            ).fetchone()[0]
            == 1
        )
        assert not client._submit_exchanges
    finally:
        await client.close()
