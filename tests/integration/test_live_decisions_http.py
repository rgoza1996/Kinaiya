"""Authenticated live ledger and actual request-bound compatible HTTP ownership."""

import asyncio
import json

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_decisions_attempts import (
    LiveDecisionsAttemptLookup,
    LiveDecisionsPublication,
)
from packages.providers.decision_capture import CaptureNativeUnavailable, decision_attempt
from packages.providers.live_decision_admission import NativeLiveDecisionAttempt
from packages.providers.text import CompatibleTextProvider
from services.common.auth import Auth
from services.decisions.model_settings import DecisionsModelSettings
from services.decisions.window_routing import WindowClassifier
from services.session.api import create_app
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_decisions_attempts import admission, finish, publication
from tests.integration.test_live_decisions_attempts import live as live

BASE = "/v2/transcript/live-decisions"


@pytest.fixture
def wire(live):
    auth = Auth(
        {
            role: role + "-public-live-inference-token-000000000"
            for role in ("owner", "converse", "decisions", "agent", "resources")
        }
    )
    return auth, httpx.ASGITransport(app=create_app(live.store, auth))


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", "none"])
async def test_native_ledger_role_parity_and_positive_original_publication(live, wire, role):
    auth, transport = wire
    admitted = live.owner.admit(admission(live))
    completion = finish(live, admitted)
    completed = live.owner.finish(completion)
    original = publication(live, completed)
    positive = live.owner.publish_result(original)
    headers = {} if role == "none" else {"Authorization": "Bearer " + auth.tokens[role]}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://session.local", headers=headers
    ) as client:
        responses = [
            await client.post(BASE + path, json=body.model_dump(mode="json"))
            for path, body in (
                (
                    "/attempts/lookup",
                    LiveDecisionsAttemptLookup(contract_version=2, source=live.source),
                ),
                ("/attempts/admit", admitted.admission),
                ("/attempts/finish", completion),
                ("/publish", original),
            )
        ]
        read = await client.post(BASE + "/publication", json=original.model_dump(mode="json"))
    denied = 401 if role == "none" else 403
    assert [r.status_code for r in responses] == [200 if role == "decisions" else denied] * 4
    assert read.status_code == (200 if role in {"decisions", "converse"} else denied)
    local = LocalJournal(live.store, role)
    if role == "decisions":
        assert (
            await local.live_decisions_attempt_lookup(
                LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
            )
        ).last_attempt == completed
        assert await local.publish_live_decision(original) == positive
        assert read.headers["cache-control"] == "no-store"
    else:
        with pytest.raises(PermissionError):
            await local.live_decisions_attempt_admit(admitted.admission)
        with pytest.raises(PermissionError):
            await local.publish_live_decision(original)
    assert live.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 1


def labels():
    return dict(
        schema_version="2.0",
        conversation="WAIT_WITH_BACKCHANNEL",
        agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
        target_task_id=None,
        expected_objective_revision=None,
        reason_code="public_native_http",
    )


def response():
    return json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": json.dumps(labels())},
                    "finish_reason": "stop",
                }
            ]
        }
    ).encode()


async def classify_once(live, journal, settings, holder):
    async with settings.operation() as lease:
        native = NativeLiveDecisionAttempt(journal, live.source, expected_attempts=0)
        holder["native"] = native
        classifier = WindowClassifier(lease.provider, window_contract_version=2)
        async with decision_attempt(
            journal,
            live.pair.source,
            lease.provider,
            model_pin=lease.pin,
            current=lease.current,
            admission=native,
            live_source_current=native.check_source,
        ) as capture:
            result = await classifier.classify(live.pair.source, [], [])
            capture.validated_output = result
        assert native.entered and native.settled and lease.current()
        request = native.publication()
        published = await journal.publish_live_decision(request)
        assert published.receipt.payload["decision"] == result
        return published


async def test_actual_bound_provider_dispatches_once_and_publishes_unchanged_output(live, wire):
    auth, transport = wire
    journal = JournalClient("http://session.local", auth.tokens["decisions"], transport=transport)
    calls = []

    def model(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, stream=httpx.ByteStream(response()))

    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1", "public-model", transport=httpx.MockTransport(model)
    )
    settings = DecisionsModelSettings(provider, None)
    holder = {}
    try:
        published = await classify_once(live, journal, settings, holder)
        assert len(calls) == 1
        assert published.receipt.payload["decision"]["conversation"] == "WAIT_WITH_BACKCHANNEL"
        state = await journal.live_decisions_attempt_lookup(
            LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
        )
        assert state.state == "completed" and state.charged_attempts == 1
        assert state.last_attempt.admission.model_pin is not None
        assert (
            await journal.lookup_live_decision(LiveDecisionsPublication.of(state.last_attempt))
            == published
        )
        assert not journal._submit_exchanges and not settings.leases
    finally:
        await settings.close()
        await journal.close()


async def test_model_read_and_sole_close_remain_owned_through_cancellation(live, wire):
    auth, transport = wire
    journal = JournalClient("http://session.local", auth.tokens["decisions"], transport=transport)
    closing, release = asyncio.Event(), asyncio.Event()

    class HeldClose(httpx.AsyncByteStream):
        closes = 0

        async def __aiter__(self):
            yield response()

        async def aclose(self):
            self.closes += 1
            closing.set()
            await release.wait()

    stream = HeldClose()
    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1",
        "public-model",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    settings = DecisionsModelSettings(provider, None)
    holder = {}
    task = asyncio.create_task(classify_once(live, journal, settings, holder))
    try:
        await asyncio.wait_for(closing.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and stream.closes == 1 and len(settings.leases) == 1
        assert holder["native"].entered
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert not settings.leases and stream.closes == 1
        state = await journal.live_decisions_attempt_lookup(
            LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
        )
        assert state.state == "uncertain" and state.charged_attempts == 1
        assert live.store.read(kinds=["routing.live_window_decided"]) == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await settings.close()
        await journal.close()


async def test_native_admission_retains_its_detached_original_after_caller_list_mutation(
    live, wire
):
    auth, asgi = wire
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldAck(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            result = await asgi.handle_async_request(request)
            if request.url.path.endswith("/admit"):
                entered.set()
                await release.wait()
            return result

    journal = JournalClient("http://session.local", auth.tokens["decisions"], transport=HeldAck())
    request = admission(live)
    expected = request.model_dump(mode="json")
    task = asyncio.create_task(journal.live_decisions_attempt_admit(request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        request.disclosure_sources.clear()
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.admission.model_dump(mode="json") == expected
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await journal.close()


@pytest.mark.parametrize("operation", ["admit", "finish", "publish"])
@pytest.mark.parametrize("committed", [False, True])
async def test_lost_original_ack_is_observed_without_replaying_inference_or_publication(
    live, wire, operation, committed
):
    await exercise_lost_ack(live, wire, operation, committed)


async def test_pre_admission_read_timeout_does_not_count_as_a_lost_finish_ack(
    live, wire, monkeypatch
):
    entry = JournalClient.entry
    held = False

    async def hold_first_entry(client, event_id):
        nonlocal held
        result = await entry(client, event_id)
        if not held:
            held = True
            await asyncio.Event().wait()  # The real currentness deadline cancels this read.
        return result

    monkeypatch.setattr(JournalClient, "entry", hold_first_entry)
    await exercise_lost_ack(live, wire, "finish", True)
    assert held


async def exercise_lost_ack(live, wire, operation, committed):
    auth, asgi = wire
    effects, closes, calls = [], [], []
    lost_path = BASE + ("/publish" if operation == "publish" else "/attempts/" + operation)
    lost_ack = httpx.ReadError("Public synthetic lost native inference acknowledgment")

    class Lost(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            raise lost_ack
            yield b""

        async def aclose(self):
            closes.append(True)
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path != lost_path:
                return await asgi.handle_async_request(request)
            effects.append(request.content)
            result = (
                await asgi.handle_async_request(request)
                if committed
                else httpx.Response(200, stream=httpx.ByteStream(b""))
            )
            assert result.status_code == 200
            result.stream = Lost(result.stream)
            return result

    def model(request):
        calls.append(request.content)
        return httpx.Response(200, content=response())

    journal = JournalClient("http://session.local", auth.tokens["decisions"], transport=Transport())
    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1", "public-model", transport=httpx.MockTransport(model)
    )
    settings, holder = DecisionsModelSettings(provider, None), {}
    try:
        lookup = LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
        for observation in range(2):
            with pytest.raises((CaptureNativeUnavailable, httpx.ReadError)) as seen:
                await classify_once(live, journal, settings, holder)
            if seen.value is lost_ack or seen.value.__cause__ is lost_ack:
                break
            # A slow source read can refuse before the native admission is even
            # constructed. Only a proven pre-dispatch refusal permits a fresh
            # test observation. Never replay an attempted admit/finish/publish.
            if (
                observation
                or str(seen.value) != "Decisions currentness check exceeded its bound"
                or not isinstance(seen.value.__cause__, TimeoutError)
            ):
                raise seen.value
            assert holder["native"].request is None and not holder["native"].entered
            assert not effects and not closes and not calls
            assert not settings.leases and not journal._submit_exchanges
            state = await journal.live_decisions_attempt_lookup(lookup)
            assert state.state == "known_absent" and state.charged_attempts == 0
            assert state.last_attempt is None
            assert not live.store.read(kinds=["routing.live_window_decided"])
        expected = (
            "known_absent"
            if operation == "admit" and not committed
            else "admitted"
            if operation == "admit" or operation == "finish" and not committed
            else "completed"
        )
        first = await journal.live_decisions_attempt_lookup(lookup)
        assert first.state == expected
        assert first.charged_attempts == (0 if expected == "known_absent" else 1)
        for _ in range(2):
            assert (
                await journal.live_decisions_attempt_lookup(
                    LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
                )
                == first
            )
            if expected == "completed":
                observed = await journal.lookup_live_decision(
                    LiveDecisionsPublication.of(first.last_attempt)
                )
                assert (observed.receipt is not None) == (operation == "publish" and committed)
        assert len(effects) == len(closes) == 1
        assert len(calls) == (0 if operation == "admit" else 1)
        assert len(live.store.read(kinds=["routing.live_window_decided"])) == (
            1 if operation == "publish" and committed else 0
        )
        assert not journal._submit_exchanges and not settings.leases
    finally:
        await settings.close()
        await journal.close()


@pytest.mark.parametrize(
    "case,status",
    [
        ("duplicate", 422),
        ("nonfinite", 422),
        ("unknown", 422),
        ("version", 422),
        ("missing-version", 422),
        ("fractional", 422),
        ("boolean", 422),
        ("oversize", 413),
        ("encoding", 415),
        ("content-type", 415),
        ("duplicate-type", 400),
        ("query", 400),
    ],
)
async def test_untrusted_native_admission_is_rejected_before_any_charge(live, wire, case, status):
    auth, transport = wire
    body = admission(live).model_dump(mode="json")
    headers = {
        "Authorization": "Bearer " + auth.tokens["decisions"],
        "Content-Type": "application/json",
    }
    path = BASE + "/attempts/admit"
    if case == "unknown":
        body["grants_agent_permission"] = True
    elif case == "version":
        body["contract_version"] = 1
    elif case == "missing-version":
        del body["contract_version"]
    elif case in {"fractional", "boolean"}:
        body["expected_attempts"] = 0.0 if case == "fractional" else False
    raw = json.dumps(body).encode()
    if case == "duplicate":
        raw = b'{"contract_version":2,"contract_version":2}'
    elif case == "nonfinite":
        raw = b'{"expected_attempts":1e9999}'
    elif case == "oversize":
        raw = b" " * (128 * 1024 + 1)
    elif case == "encoding":
        headers["Content-Encoding"] = "gzip"
    elif case == "content-type":
        headers["Content-Type"] = "text/plain"
    elif case == "duplicate-type":
        headers = list(headers.items()) + [("Content-Type", "application/json")]
    elif case == "query":
        path += "?grant=true"
    async with httpx.AsyncClient(transport=transport, base_url="http://session.local") as client:
        result = await client.post(path, content=raw, headers=headers)
    assert result.status_code == status
    assert live.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 0
    assert live.store.read(kinds=["routing.live_window_decided"]) == []


async def test_native_service_credential_is_rechecked_after_awaited_body(live, wire):
    auth, transport = wire
    entered, release = asyncio.Event(), asyncio.Event()

    async def body():
        raw = admission(live).model_dump_json().encode()
        yield raw[:10]
        entered.set()
        await release.wait()
        yield raw[10:]

    async with httpx.AsyncClient(transport=transport, base_url="http://session.local") as client:
        task = asyncio.create_task(
            client.post(
                BASE + "/attempts/admit",
                content=body(),
                headers={
                    "Authorization": "Bearer " + auth.tokens["decisions"],
                    "Content-Type": "application/json",
                },
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            auth.tokens["decisions"] = "rotated-decisions-public-token-000000000"
            release.set()
            assert (await asyncio.wait_for(task, 3)).status_code == 401
            assert (
                live.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 0
            )
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
