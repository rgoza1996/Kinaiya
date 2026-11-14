"""Final publication fences after the original asynchronous media settlement.

Interface-first candidate from frozen3893b299 and its protocol fixtures; the
prospective implementation/Muse output was not read. The existing bridge is a
protocol test double, not a native Session producer or physical-cleanup proof.
Actual controlled HTTPX response ownership, native Tasks and Timeout objects
are observed. No model, network endpoint, device or external service is used.
"""

import asyncio
import sys
from contextlib import asynccontextmanager

import httpx
import pytest
from test_llama_media import (
    _model_for_mode,
    _response_for_mode,
    _spec_for_mode,
    _StubExchangeOwnerBridge,
)

from packages.providers import llama_media
from packages.providers.llama_media import (
    LlamaMediaChatResult,
    LlamaMediaSystemOneResult,
    LlamaMediaTimeoutError,
    NativeLlamaMediaProvider,
)
from packages.providers.text import ProviderError

_WAIT = 10
_PROVIDER_TIMEOUT = 5.0
_MODES = ("converse_chat", "decisions_systemone")


class _TimeoutObservation:
    """Observe native timeout exit without replacing its object, clock or limit."""

    def __init__(self, monkeypatch):
        self.originals = []
        self.exited = []
        native_exit = asyncio.Timeout.__aexit__

        async def observe_exit(original, *exception):
            try:
                return await native_exit(original, *exception)
            finally:
                if any(original is value for value in self.originals):
                    self.exited.append(original)

        monkeypatch.setattr(asyncio.Timeout, "__aexit__", observe_exit)
        monkeypatch.setattr(llama_media, "asyncio", self)

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def timeout(self, delay):
        original = asyncio.timeout(delay)
        self.originals.append(original)
        return original

    def timeout_at(self, when):
        original = asyncio.timeout_at(when)
        self.originals.append(original)
        return original

    def exited_original(self):
        assert self.originals
        original = self.originals[0]
        assert type(original) is asyncio.Timeout
        assert any(original is value for value in self.exited)
        assert original.when() is not None
        assert original.expired() is False
        return original


class _OriginalResponse(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body
        self.close_calls = 0
        self.close_task = None

    async def __aiter__(self):
        yield self.body

    async def aclose(self):
        self.close_calls += 1
        self.close_task = asyncio.current_task()


class _FinalBridge(_StubExchangeOwnerBridge):
    """Hold only final success settlement; inherited records are protocol data."""

    def __init__(self, fault, wire, timing):
        super().__init__()
        self.fault = fault
        self.wire = wire
        self.timing = timing
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_observed = asyncio.Event()
        self.settle_calls = 0
        self.settle_task = None
        self.registration = None
        self.bundle = None
        self.returned_bundle = False
        self.current = True
        self.returned_at = None

    async def settle_exchange(self, **kwargs):
        self.settle_calls += 1
        self.settle_task = asyncio.current_task()
        self.registration = kwargs["registration"]
        assert kwargs["outcome"] == "completed"
        assert kwargs["candidate"] is not None
        assert kwargs["transport_sole_closed"] is True
        assert kwargs["wire_buffer_retired"] is True
        assert self.wire.close_calls == 1
        self.timing.exited_original()
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_observed.set()
            if self.fault != "suppressed_cancel":
                raise
            await self.release.wait()
        if self.fault == "revoked":
            self.current = False
        self.bundle = super().settle_exchange(**kwargs)
        self.returned_bundle = True
        self.returned_at = asyncio.get_running_loop().time()
        return self.bundle


class _Case:
    def __init__(self, mode, fault, monkeypatch):
        self.mode = mode
        self.fault = fault
        self.timing = _TimeoutObservation(monkeypatch)
        self.wire = _OriginalResponse(_response_for_mode(mode))
        self.bridge = _FinalBridge(fault, self.wire, self.timing)
        self.requests = []
        self.returned_values = []
        self.final_current_seen = False
        self.callback_cancel_target = None
        self.alarm = None

        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, stream=self.wire)

        self.provider = NativeLlamaMediaProvider(
            "http://127.0.0.1:8890",
            _model_for_mode(mode),
            mode=mode,
            timeout=_PROVIDER_TIMEOUT,
            transport=httpx.MockTransport(handle),
            exchange_owner_bridge=self.bridge,
        )
        self.runner = asyncio.create_task(self.invoke())

    def current(self):
        if self.bridge.returned_bundle:
            self.final_current_seen = True
            if self.fault == "current_cancels" and self.callback_cancel_target is None:
                self.callback_cancel_target = asyncio.current_task()
                assert self.callback_cancel_target is self.runner
                self.callback_cancel_target.cancel()
        return self.bridge.current

    async def invoke(self):
        result = await self.provider.complete_media(_spec_for_mode(self.mode), current=self.current)
        # No await can hide a result which escaped before a queued cancellation.
        self.returned_values.append(result)
        return result

    async def wait_for_final_settle(self):
        entered = asyncio.create_task(self.bridge.entered.wait())
        try:
            done, _ = await asyncio.wait(
                (entered, self.runner), timeout=_WAIT, return_when=asyncio.FIRST_COMPLETED
            )
            if self.runner in done:
                await self.runner
                pytest.fail("Media result escaped before held final settlement")
            assert entered in done and entered.result() is True
            assert self.bridge.settle_task is self.runner
            assert not self.runner.done()
        finally:
            if not entered.done():
                entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)

    def release_after_original_deadline(self):
        loop = asyncio.get_running_loop()
        when = self.timing.exited_original().when()

        def release_when_late():
            # Native callbacks may be scheduled up to a clock-resolution early.
            # Observe the actual clock; never reschedule or replace the timeout.
            if loop.time() <= when:
                self.alarm = loop.call_soon(release_when_late)
            else:
                self.bridge.release.set()

        self.alarm = loop.call_at(when, release_when_late)


@asynccontextmanager
async def _case(mode, fault, monkeypatch):
    case = _Case(mode, fault, monkeypatch)
    try:
        yield case
    finally:
        primary = sys.exception()
        try:
            if case.alarm is not None:
                case.alarm.cancel()
            case.bridge.release.set()
            if not case.runner.done():
                case.runner.cancel()
            await asyncio.wait_for(asyncio.gather(case.runner, return_exceptions=True), _WAIT)
            await case.provider.close()
            assert case.wire.close_calls == 1
            assert len(case.requests) == 1
            assert case.bridge.settle_calls == 1
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note("Additional final-fence fixture teardown: " + type(error).__name__)


def _assert_protocol_settlement_preserved(case):
    exchange = case.provider.last_exchange
    registration = case.bridge.registration
    bundle = case.bridge.bundle
    assert exchange is not None and registration is not None and bundle is not None
    assert len(case.provider.exchanges) == 1
    assert exchange.exchange_id == registration.body_owner.exchange_id
    assert exchange.local_task_id == registration.local_task_id
    assert exchange.transport_owner_id == registration.transport_owner_id
    assert exchange.body_owner_id == registration.body_owner.body_owner_id
    assert exchange.binding_sha256 == registration.binding_sha256
    assert exchange.body_owner_record == registration.body_owner
    assert exchange.body_close_record == bundle.body_close
    assert exchange.cleanup_receipt == bundle.cleanup
    assert exchange.state == "settled"
    assert exchange.outcome == "completed"
    assert exchange.transport_sole_closed is True
    assert exchange.local_task_joined is True
    assert exchange.body_settled is True
    assert exchange.all_readers_settled is True
    assert exchange.publications_settled is True
    assert exchange.body_proof == "wire_buffer_retired"
    return exchange


@pytest.mark.parametrize("mode", _MODES)
async def test_healthy_final_settlement_returns_result_and_preserves_original_proof(
    mode, monkeypatch
):
    async with _case(mode, "healthy", monkeypatch) as case:
        await case.wait_for_final_settle()
        case.bridge.release.set()
        result = await asyncio.wait_for(asyncio.shield(case.runner), _WAIT)
        expected_type = (
            LlamaMediaChatResult if mode == "converse_chat" else LlamaMediaSystemOneResult
        )
        assert type(result) is expected_type
        assert case.returned_values == [result]
        exchange = _assert_protocol_settlement_preserved(case)
        assert exchange.attempt_disposition == "completed"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("fault", ("suppressed_cancel", "deadline", "revoked", "current_cancels"))
async def test_returned_final_bundle_cannot_bypass_cancellation_deadline_or_currentness(
    mode, fault, monkeypatch
):
    async with _case(mode, fault, monkeypatch) as case:
        await case.wait_for_final_settle()
        if fault == "suppressed_cancel":
            case.runner.cancel()
            await asyncio.wait_for(case.bridge.cancel_observed.wait(), _WAIT)
            assert not case.runner.done()
            case.bridge.release.set()
        elif fault == "deadline":
            case.release_after_original_deadline()
        else:
            case.bridge.release.set()

        expected_error = (
            LlamaMediaTimeoutError
            if fault == "deadline"
            else ProviderError
            if fault == "revoked"
            else asyncio.CancelledError
        )
        with pytest.raises(expected_error):
            await asyncio.wait_for(asyncio.shield(case.runner), _WAIT)

        assert case.returned_values == []
        assert case.bridge.returned_bundle is True
        assert case.final_current_seen is True
        exchange = _assert_protocol_settlement_preserved(case)
        assert exchange.attempt_disposition != "completed"
        if fault == "deadline":
            assert case.bridge.returned_at > case.timing.exited_original().when()
        if fault in {"suppressed_cancel", "current_cancels"}:
            assert case.runner.cancelling() > 0
        if fault == "current_cancels":
            assert case.callback_cancel_target is case.runner


@pytest.mark.parametrize("mode", _MODES)
async def test_final_settlement_cancellation_without_bundle_retains_original_unknown(
    mode, monkeypatch
):
    async with _case(mode, "propagated_cancel", monkeypatch) as case:
        await case.wait_for_final_settle()
        case.runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(case.runner), _WAIT)
        assert case.bridge.cancel_observed.is_set()
        assert case.bridge.bundle is None
        assert case.returned_values == []
        exchange = case.provider.last_exchange
        registration = case.bridge.registration
        assert exchange is not None and registration is not None
        assert len(case.provider.exchanges) == 1
        assert exchange.exchange_id == registration.body_owner.exchange_id
        assert exchange.local_task_id == registration.local_task_id
        assert exchange.transport_owner_id == registration.transport_owner_id
        assert exchange.body_owner_id == registration.body_owner.body_owner_id
        assert exchange.binding_sha256 == registration.binding_sha256
        assert exchange.body_owner_record == registration.body_owner
        assert exchange.transport_sole_closed is True
        assert exchange.state == "unknown"
        assert exchange.outcome is None
        assert exchange.attempt_disposition != "completed"
        assert exchange.body_proof == "none"
        assert exchange.body_close_record is None
        assert exchange.cleanup_receipt is None
        assert exchange.local_task_joined is False
        assert exchange.body_settled is False
        assert exchange.all_readers_settled is False
        assert exchange.publications_settled is False
        assert exchange in case.provider.unresolved_exchanges


class _RegistrationGateBridge(_StubExchangeOwnerBridge):
    """Suppress caller cancellation before returning one valid registration."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_observed = asyncio.Event()
        self.register_task = None
        self.returned_registration = None
        self.settle_calls = 0

    async def register_exchange(self, **kwargs):
        original = super().register_exchange(**kwargs)
        self.register_task = asyncio.current_task()
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_observed.set()
            await self.release.wait()
        self.returned_registration = original
        return original

    def settle_exchange(self, **kwargs):
        self.settle_calls += 1
        assert kwargs["registration"] == self.returned_registration
        assert kwargs["outcome"] != "completed"
        assert kwargs["transport_sole_closed"] is False
        assert kwargs["wire_buffer_retired"] is False
        # No native component proof is supplied by this protocol fixture.
        return None


@pytest.mark.parametrize("mode", _MODES)
async def test_new_cancellation_suppressed_by_registration_never_reaches_original_http(mode):
    wire = _OriginalResponse(_response_for_mode(mode))
    bridge = _RegistrationGateBridge()
    requests = []
    returned_values = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=wire)

    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        _model_for_mode(mode),
        mode=mode,
        timeout=_PROVIDER_TIMEOUT,
        transport=httpx.MockTransport(handle),
        exchange_owner_bridge=bridge,
    )

    async def invoke():
        result = await provider.complete_media(_spec_for_mode(mode), current=lambda: True)
        returned_values.append(result)
        return result

    runner = asyncio.create_task(invoke())
    entered = None
    try:
        entered = asyncio.create_task(bridge.entered.wait())
        done, _ = await asyncio.wait(
            (entered, runner), timeout=_WAIT, return_when=asyncio.FIRST_COMPLETED
        )
        if runner in done:
            await runner
            pytest.fail("Media call finished before held registration")
        assert entered in done and entered.result() is True
        assert bridge.register_task is runner
        assert runner.cancelling() == 0
        runner.cancel()
        await asyncio.wait_for(bridge.cancel_observed.wait(), _WAIT)
        assert runner.cancelling() > 0
        assert not runner.done()
        bridge.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(runner), _WAIT)

        assert returned_values == []
        assert requests == []
        assert wire.close_calls == 0
        assert bridge.settle_calls == 1
        registration = bridge.returned_registration
        exchange = provider.last_exchange
        assert registration is not None and exchange is not None
        assert len(provider.exchanges) == 1
        assert exchange.exchange_id == registration.body_owner.exchange_id
        assert exchange.local_task_id == registration.local_task_id
        assert exchange.transport_owner_id == registration.transport_owner_id
        assert exchange.body_owner_id == registration.body_owner.body_owner_id
        assert exchange.binding_sha256 == registration.binding_sha256
        assert exchange.body_owner_record == registration.body_owner
        assert exchange.attempt_disposition != "completed"
        assert exchange.transport_sole_closed is False
        assert exchange.state == "unknown"
        assert exchange.outcome is None
        assert exchange.body_proof == "none"
        assert exchange.body_close_record is None
        assert exchange.cleanup_receipt is None
        assert exchange.local_task_joined is False
        assert exchange.body_settled is False
        assert exchange.all_readers_settled is False
        assert exchange.publications_settled is False
        assert exchange in provider.unresolved_exchanges
    finally:
        primary = sys.exception()
        try:
            bridge.release.set()
            if entered is not None:
                if not entered.done():
                    entered.cancel()
                await asyncio.gather(entered, return_exceptions=True)
            if not runner.done():
                runner.cancel()
            await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), _WAIT)
            await provider.close()
            assert wire.close_calls == 0
            assert requests == []
            assert bridge.settle_calls == 1
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note("Additional pre-wire fixture teardown: " + type(error).__name__)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "fault", ("healthy", "suppressed_cancel", "deadline", "revoked", "current_cancels")
)
async def test_caller_native_publication_never_emits_converse_or_routing_on_late_or_cancelled_media(
    mode, fault, monkeypatch, tmp_path
):
    from packages.contracts.models import AppendRequest, Decision, SessionRequest
    from services.session.store import Store

    store = Store(tmp_path / f"journal-{mode}-{fault}.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    source = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={
                "text": "Count the beeps in the clip.",
                "final": True,
                "revision": 1,
                "content_scope": "local_only",
            },
            idempotency_key=f"user-media:{session.session_id}:1",
        ),
        "owner",
    )
    agent_dispatches = []

    try:
        async with _case(mode, fault, monkeypatch) as case:
            await case.wait_for_final_settle()
            if fault == "suppressed_cancel":
                case.runner.cancel()
                await asyncio.wait_for(case.bridge.cancel_observed.wait(), _WAIT)
                case.bridge.release.set()
            elif fault == "deadline":
                case.release_after_original_deadline()
            else:
                case.bridge.release.set()

            if fault == "healthy":
                result = await asyncio.wait_for(asyncio.shield(case.runner), _WAIT)
                if mode == "converse_chat":
                    assert isinstance(result, LlamaMediaChatResult)
                    store.append(
                        AppendRequest(
                            session_id=session.session_id,
                            thread_id=session.thread_id,
                            epoch=session.epoch,
                            kind="conversation.converse",
                            payload={
                                "text": result.text,
                                "final": True,
                                "content_scope": "local_only",
                            },
                            causes=[source.event_id],
                            idempotency_key=f"converse-media:{session.session_id}:1",
                        ),
                        "converse",
                    )
                    assert len(store.read(kinds=["conversation.converse"])) == 1
                else:
                    assert isinstance(result, LlamaMediaSystemOneResult)
                    decision = Decision(
                        route="ignore",
                        reason_code="media_classified",
                        confidence=float(result.answers["route"]["confidence"]),
                        source_event_ids=[source.event_id],
                        source_revision=1,
                        instruction="",
                        idempotency_key=f"decisions-media:{source.event_id}:r1",
                    )
                    store.append(
                        AppendRequest(
                            session_id=session.session_id,
                            thread_id=session.thread_id,
                            epoch=session.epoch,
                            kind="routing.decided",
                            payload=decision.model_dump(mode="json"),
                            causes=[source.event_id],
                            idempotency_key=decision.idempotency_key,
                        ),
                        "decisions",
                    )
                    agent_dispatches.append(dict(result.answers))
                    assert len(store.read(kinds=["routing.decided"])) == 1
                    assert len(agent_dispatches) == 1
            else:
                expected_error = (
                    LlamaMediaTimeoutError
                    if fault == "deadline"
                    else ProviderError
                    if fault == "revoked"
                    else asyncio.CancelledError
                )
                with pytest.raises(expected_error):
                    await asyncio.wait_for(asyncio.shield(case.runner), _WAIT)
                assert case.returned_values == []
                assert store.read(kinds=["conversation.converse", "routing.decided"]) == []
                assert agent_dispatches == []
    finally:
        store.close()
