"""Owned native-submit HTTP transport; no live server or credentials are used.

The new methods/types are resolved inside tests. Their absence is an explicit
capability assertion, never a collection error or a parser-behavior claim.
"""

import asyncio
import gzip
import hashlib
import importlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import AppendRequest, JournalEntry

TASK = "00000000-0000-4000-8000-000000000001"
WINNER = "00000000-0000-4000-8000-000000000002"
MARKER = "native-event-marker-do-not-reflect"
RESPONSE_LIMIT = 69632
PATHS = {
    "admit": "/v1/transcript/agent-submits",
    "lookup": "/v1/transcript/agent-submits/lookup",
}


def wire(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def proposal(*, task_id=TASK, winner=False, objective="Public synthetic receipt.", sources=()):
    logical = {
        "session_id": "synthetic-session",
        "thread_id": "synthetic-thread",
        "epoch": 1,
        "objective": objective,
        "source_event_ids": list(sources),
        "source_revision": 1,
        "content_scope": "local_only",
    }
    return AppendRequest(
        session_id=logical["session_id"],
        thread_id=logical["thread_id"],
        epoch=1,
        kind="agent.command",
        task_refs=[task_id],
        idempotency_key=f"agent:{task_id}:agent.command:1",
        payload={
            **logical,
            "op": "submit",
            "actor": "owner",
            "task_id": task_id,
            "state": "accepted",
            "objective_revision": 1,
            "requested_content_scope": "local_only",
            "idempotency_key": "logical-client-key",
            "payload_digest": digest(logical),
            "harness_version": "v-" + "a" * 24 if winner else "seed-v1",
            "model_pin": {
                "schema_version": "1.0",
                "profile_id": "native-winner" if winner else "native-original",
                "provider": "synthetic",
                "model": "winner" if winner else "original",
                "local": True,
                "reasoning": None,
                "settings_revision": 1 if winner else 0,
            },
            "explicit_skill": None,
            "plugin_versions": {},
        },
    )


def query_dict():
    return {
        "schema_version": "1.0",
        "session_id": "synthetic-session",
        "thread_id": "synthetic-thread",
        "epoch": 1,
        "idempotency_key": "logical-client-key",
    }


def native_entry(body):
    return JournalEntry(
        **body.model_dump(),
        sequence=17,
        origin="agent-supervisor",
        event_id=MARKER,
        created_at=datetime(2026, 10, 31, tzinfo=UTC),
    ).model_dump(mode="json")


def response(operation, mode=None):
    mode = mode or ("created" if operation == "admit" else "recorded")
    if mode == "unobserved":
        return {"schema_version": "1.0", "status": "unobserved", "entry": None}
    body = proposal(task_id=WINNER, winner=True) if mode in {"retained", "recorded"} else proposal()
    return {
        "schema_version": "1.0",
        "disposition" if operation == "admit" else "status": mode,
        "entry": native_entry(body),
    }


class Body(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw
        self.read_entered = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.read_release = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_cancelled = asyncio.Event()
        self.close_completed = asyncio.Event()
        self.hold_read = False
        self.hold_close = False
        self.defer_close_cancellation = False
        self.read_error = False
        self.close_error = False
        self.close_calls = 0
        self.chunks = 0

    async def __aiter__(self):
        self.read_entered.set()
        if self.hold_read:
            await self.read_release.wait()
        for chunk in (self.raw[:32], self.raw[32:]):
            self.chunks += 1
            yield chunk
        if self.read_error:
            raise OSError("transport-marker-do-not-reflect")

    async def aclose(self):
        self.close_calls += 1
        self.close_entered.set()
        if self.hold_close:
            try:
                await self.close_release.wait()
            except asyncio.CancelledError:
                self.close_cancelled.set()
                if not self.defer_close_cancellation:
                    raise
                await self.close_release.wait()
        if self.close_error:
            raise OSError("transport-marker-do-not-reflect")
        self.close_completed.set()


@asynccontextmanager
async def scenario(operation, *, payload=None, raw=None, status=200, extra_headers=None):
    payload = response(operation) if payload is None else payload
    stream = Body(wire(payload) if raw is None else raw)
    requests = []
    acquire_entered, acquire_release = asyncio.Event(), asyncio.Event()
    state = SimpleNamespace(hold_acquire=False)

    async def handle(request):
        requests.append(request)
        acquire_entered.set()
        if state.hold_acquire:
            await acquire_release.wait()
        return httpx.Response(
            status,
            headers={"Content-Type": "application/json", **(extra_headers or {})},
            stream=stream,
        )

    client = JournalClient(
        "http://native.test", "synthetic-agent-credential", transport=httpx.MockTransport(handle)
    )
    operation_task = None
    try:
        method = getattr(client, operation + "_submit", None)
        assert callable(method), "Native submit client method is absent (capability baseline)"
        if operation == "lookup":
            try:
                contract = importlib.import_module("packages.contracts.agent_submit")
            except ModuleNotFoundError:
                pytest.fail("Native submit types are absent (capability baseline)")
            request = contract.AgentSubmitLookupRequest(**query_dict())
        else:
            request = proposal()
        state.client, state.method, state.request = client, method, request
        state.stream, state.requests = stream, requests
        state.acquire_entered, state.acquire_release = acquire_entered, acquire_release
        state.operation_task = operation_task
        yield state
    finally:
        acquire_release.set()
        stream.read_release.set()
        stream.close_release.set()
        operation_task = getattr(state, "operation_task", None)
        if operation_task is not None and not operation_task.done():
            operation_task.cancel()
            _done, pending = await asyncio.wait({operation_task}, timeout=1)
            assert not pending, "Synthetic client operation was not reaped"
        await client.close()


def safe_error(error):
    value = str(error)
    assert MARKER not in value
    assert "transport-marker-do-not-reflect" not in value
    assert "body-marker-do-not-reflect" not in value
    assert " column " not in value
    assert len(value) <= 256


async def pump():
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)
    await event.wait()


async def done(task):
    _finished, pending = await asyncio.wait({task}, timeout=0.25)
    assert not pending, "The real client total deadline did not expire"
    return task.result()


@pytest.mark.parametrize(
    "operation,mode",
    [("admit", "created"), ("admit", "retained"), ("lookup", "recorded"), ("lookup", "unobserved")],
)
async def test_native_submit_success_is_nested_bound_and_owned_closed(operation, mode):
    payload = response(operation, mode)
    async with scenario(operation, payload=payload) as env:
        result = await env.method(env.request)
        assert result.model_dump(mode="json") == payload
        assert env.stream.close_completed.is_set() and env.stream.close_calls == 1
        assert len(env.requests) == 1
        sent = env.requests[0]
        assert sent.method == "POST" and sent.url.path == PATHS[operation]
        assert sent.headers["Content-Type"].split(";")[0] == "application/json"
        assert sent.headers["Accept-Encoding"] == "identity"
        assert json.loads(await sent.aread()) == env.request.model_dump(mode="json")


@pytest.mark.parametrize("operation", ["admit", "lookup"])
@pytest.mark.parametrize("status", [302, 401, 403, 409, 503])
async def test_native_status_refusal_never_follows_or_reflects_error_body(operation, status):
    async with scenario(
        operation,
        payload={"detail": "body-marker-do-not-reflect"},
        status=status,
        extra_headers={"Location": "http://must-not-follow.test/"},
    ) as env:
        with pytest.raises((ValueError, httpx.HTTPStatusError)) as caught:
            await env.method(env.request)
        safe_error(caught.value)
        if isinstance(caught.value, httpx.HTTPStatusError):
            assert caught.value.response.status_code == status
        assert len(env.requests) == 1
        assert env.stream.close_calls == 1


@pytest.mark.parametrize("operation", ["admit", "lookup"])
@pytest.mark.parametrize("bad", ["content_type", "compression", "declared_oversize", "length"])
async def test_native_response_headers_are_checked_with_owned_close(operation, bad):
    headers = {
        "content_type": {"Content-Type": "text/plain"},
        "compression": {"Content-Encoding": "gzip"},
        "declared_oversize": {"Content-Length": str(RESPONSE_LIMIT + 1)},
        "length": {"Content-Length": "1"},
    }[bad]
    # Correct gzip data would otherwise be transparently decoded by httpx;
    # malformed compressed bytes would not isolate the identity-only rule.
    raw = gzip.compress(wire(response(operation)), mtime=0) if bad == "compression" else None
    async with scenario(operation, extra_headers=headers, raw=raw) as env:
        with pytest.raises((ValueError, httpx.DecodingError)) as caught:
            await env.method(env.request)
        safe_error(caught.value)
        assert env.stream.close_calls == 1


@pytest.mark.parametrize("operation", ["admit", "lookup"])
@pytest.mark.parametrize(
    "bad",
    [
        "root_duplicate",
        "nested_duplicate",
        "non_finite",
        "overflow",
        "invalid_utf8",
        "surrogate",
        "trailing_json",
    ],
)
async def test_native_json_is_strict_and_diagnostics_do_not_expose_payload(operation, bad):
    raw = wire(response(operation))
    if bad == "root_duplicate":
        raw = b'{"schema_version":"1.0",' + raw[1:]
    elif bad == "nested_duplicate":
        needle = b'"event_id":"' + MARKER.encode() + b'"'
        raw = raw.replace(needle, needle + b"," + needle)
    elif bad in {"non_finite", "overflow"}:
        raw = raw.replace(
            b'"sequence":17', b'"sequence":' + (b"NaN" if bad == "non_finite" else b"1e999")
        )
    elif bad in {"invalid_utf8", "surrogate"}:
        raw = raw.replace(MARKER.encode(), b"bad\xffutf8" if bad == "invalid_utf8" else b"\\ud800")
    else:
        raw += b"{}"
    async with scenario(operation, raw=raw) as env:
        with pytest.raises(ValueError) as caught:
            await env.method(env.request)
        safe_error(caught.value)
        assert env.stream.close_calls == 1


@pytest.mark.parametrize("field", ["task_identity", "model_pin", "harness", "native_key", "causes"])
async def test_created_receipt_must_equal_the_entire_frozen_request(field):
    payload = response("admit")
    entry = payload["entry"]
    if field == "task_identity":
        entry.update(native_entry(proposal(task_id=WINNER)))
    elif field == "model_pin":
        entry["payload"]["model_pin"]["model"] = "different-native-model"
    elif field == "harness":
        entry["payload"]["harness_version"] = "v-" + "b" * 24
    elif field == "native_key":
        entry["idempotency_key"] += "-different"
    else:
        entry["causes"] = ["different-native-cause"]
    async with scenario("admit", payload=payload) as env:
        with pytest.raises(ValueError) as caught:
            await env.method(env.request)
        safe_error(caught.value)
        assert env.stream.close_calls == 1


def change_logical(entry, field, value):
    entry["payload"][field] = value
    if field in {"session_id", "thread_id", "epoch"}:
        entry[field] = value
    if field == "content_scope":
        entry["payload"]["requested_content_scope"] = value
    logical = {
        key: entry["payload"][key]
        for key in (
            "session_id",
            "thread_id",
            "epoch",
            "objective",
            "source_event_ids",
            "source_revision",
        )
    }
    logical["content_scope"] = entry["payload"]["requested_content_scope"]
    entry["payload"]["payload_digest"] = digest(logical)


@pytest.mark.parametrize(
    "field,value",
    [
        ("objective", "Another valid objective."),
        ("content_scope", "public_synthetic"),
        ("epoch", 2),
    ],
)
async def test_retained_different_metadata_does_not_permit_a_different_logical_request(
    field, value
):
    payload = response("admit", "retained")
    change_logical(payload["entry"], field, value)
    async with scenario("admit", payload=payload) as env:
        with pytest.raises(ValueError) as caught:
            await env.method(env.request)
        safe_error(caught.value)


@pytest.mark.parametrize("field", ["session_id", "thread_id", "epoch", "idempotency_key"])
async def test_recorded_lookup_is_bound_to_exact_requested_native_scope(field):
    payload = response("lookup")
    if field == "idempotency_key":
        payload["entry"]["payload"][field] = "other-logical-key"
    else:
        change_logical(payload["entry"], field, 2 if field == "epoch" else "another-native-scope")
    async with scenario("lookup", payload=payload) as env:
        with pytest.raises(ValueError) as caught:
            await env.method(env.request)
        safe_error(caught.value)


@pytest.mark.parametrize("operation", ["admit", "lookup"])
@pytest.mark.parametrize("bad", ["encoded_entry", "origin", "bool_counter", "root_array"])
async def test_nested_native_receipt_shape_and_authority_are_strict(operation, bad):
    payload = response(operation)
    if bad == "encoded_entry":
        payload["entry"] = wire(payload["entry"]).decode()
    elif bad == "origin":
        payload["entry"]["origin"] = "untrusted"
    elif bad == "bool_counter":
        payload["entry"]["sequence"] = True
    else:
        payload = [payload]
    async with scenario(operation, payload=payload) as env:
        with pytest.raises(ValueError) as caught:
            await env.method(env.request)
        safe_error(caught.value)


@pytest.mark.parametrize("operation", ["admit", "lookup"])
@pytest.mark.parametrize("extra", [0, 1], ids=["exact_limit", "over_limit"])
async def test_streamed_response_byte_bound(operation, extra):
    raw = wire(response(operation))
    raw += b" " * (RESPONSE_LIMIT + extra - len(raw))
    async with scenario(operation, raw=raw) as env:
        if extra:
            with pytest.raises(ValueError) as caught:
                await env.method(env.request)
            safe_error(caught.value)
        else:
            assert (await env.method(env.request)).entry is not None
        assert env.stream.close_calls == 1


async def test_over_limit_admit_request_is_never_sent():
    # Every logical field stays within TaskRequest's character/cardinality
    # limits, but its minimum UTF-8 envelope exceeds the native 64 KiB cap.
    oversized = proposal(
        objective="\U0001f600" * 16000,
        sources=[f"{index:03}" + "s" * 125 for index in range(32)],
    )
    assert len(wire(oversized.model_dump(mode="json"))) > 65536
    async with scenario("admit") as env:
        with pytest.raises(ValueError) as caught:
            await env.method(oversized)
        safe_error(caught.value)
        assert env.requests == []


async def test_over_limit_lookup_input_is_never_sent():
    async with scenario("lookup") as env:
        malformed = env.request.model_copy(update={"idempotency_key": "x" * 4097})
        assert len(wire(malformed.model_dump(mode="json"))) > 4096
        with pytest.raises(ValueError) as caught:
            await env.method(malformed)
        safe_error(caught.value)
        assert env.requests == []


async def test_valid_utf8_lookup_stays_within_wire_cap_without_coercion():
    async with scenario("lookup", payload=response("lookup", "unobserved")) as env:
        valid = type(env.request)(
            session_id="\U0001f600" * 128,
            thread_id="\U0001f601" * 128,
            epoch=1,
            idempotency_key="\U0001f602" * 256,
        )
        result = await env.method(valid)
        assert result.status == "unobserved" and result.entry is None
        raw = await env.requests[0].aread()
        assert len(raw) <= 4096
        assert json.loads(raw) == valid.model_dump(mode="json")


@pytest.mark.parametrize("operation", ["admit", "lookup"])
async def test_success_waits_for_the_owned_response_close(operation):
    async with scenario(operation) as env:
        env.stream.hold_close = True
        task = env.operation_task = asyncio.create_task(env.method(env.request))
        await asyncio.wait_for(env.stream.close_entered.wait(), timeout=1)
        assert not task.done()
        env.stream.close_release.set()
        result = await done(task)
        assert result.entry is not None
        assert env.stream.close_completed.is_set()


@pytest.mark.parametrize("operation,budget", [("admit", 15), ("lookup", 5)])
@pytest.mark.parametrize("phase", ["acquire", "read", "close"])
async def test_real_total_deadline_includes_acquire_read_and_owned_close(
    operation, budget, phase, monkeypatch
):
    async with scenario(operation) as env:
        env.hold_acquire = phase == "acquire"
        env.stream.hold_read = phase == "read"
        env.stream.hold_close = phase == "close"
        entered = (
            env.acquire_entered if phase == "acquire" else getattr(env.stream, phase + "_entered")
        )
        loop = asyncio.get_running_loop()
        monotonic, offset = loop.time, [0.0]
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: monotonic() + offset[0])
            task = env.operation_task = asyncio.create_task(env.method(env.request))
            await asyncio.wait_for(entered.wait(), timeout=1)
            offset[0] = budget - 1
            await pump()
            assert not task.done(), "Client total deadline expired before its declared budget"
            offset[0] = budget + 1
            with pytest.raises((TimeoutError, ValueError)) as caught:
                await done(task)
            safe_error(caught.value)
        if phase != "acquire":
            assert env.stream.close_calls == 1


@pytest.mark.parametrize("operation", ["admit", "lookup"])
@pytest.mark.parametrize("phase", ["read", "close"])
async def test_cancellation_is_propagated_and_never_becomes_an_unobserved_lookup(operation, phase):
    async with scenario(operation) as env:
        setattr(env.stream, "hold_" + phase, True)
        task = env.operation_task = asyncio.create_task(env.method(env.request))
        await asyncio.wait_for(getattr(env.stream, phase + "_entered").wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await done(task)
        assert env.stream.close_calls == 1


@pytest.mark.parametrize("operation,budget", [("admit", 15), ("lookup", 5)])
async def test_cleanup_that_finishes_after_deadline_cannot_return_a_native_receipt(
    operation, budget, monkeypatch
):
    async with scenario(operation) as env:
        env.stream.hold_close = True
        env.stream.defer_close_cancellation = True
        loop = asyncio.get_running_loop()
        monotonic, offset = loop.time, [0.0]
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: monotonic() + offset[0])
            task = env.operation_task = asyncio.create_task(env.method(env.request))
            await asyncio.wait_for(env.stream.close_entered.wait(), timeout=1)
            offset[0] = budget + 1
            await asyncio.wait_for(env.stream.close_cancelled.wait(), timeout=1)
            env.stream.close_release.set()
            with pytest.raises((TimeoutError, ValueError)) as caught:
                await done(task)
            safe_error(caught.value)
        assert env.stream.close_completed.is_set()


@pytest.mark.parametrize("operation", ["admit", "lookup"])
@pytest.mark.parametrize("phase", ["read", "close"])
async def test_transport_or_close_failure_never_yields_a_native_receipt(operation, phase):
    async with scenario(operation) as env:
        setattr(env.stream, phase + "_error", True)
        with pytest.raises((ValueError, OSError, httpx.HTTPError)) as caught:
            await env.method(env.request)
        safe_error(caught.value)
        assert env.stream.close_calls == 1
