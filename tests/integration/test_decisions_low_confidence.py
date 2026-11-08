"""#475: valid below-floor answers are refusals, not endpoint-unavailable claims."""

import json
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow
from packages.providers.classification_errors import (
    LowConfidenceRefusal,
    classification_refusal_reason,
    retained_refusal_reason,
)
from packages.providers.native_d1 import NativeD1RoutingProvider
from packages.providers.text import ProviderRefusal
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.decisions.window_routing import WindowClassifier
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import LocalJournal, Store

MODEL = "native/d1-Omni_2B.Q8"
MARKER = "PRIVATE_SYNTHETIC_SOURCE_475"


class NoAgent:
    async def status(self):
        return []

    async def submit(self, *_args, **_kwargs):
        pytest.fail("Classification refusal cannot submit a task")

    async def steer(self, *_args, **_kwargs):
        pytest.fail("Classification refusal cannot steer a task")

    async def interrupt(self, *_args, **_kwargs):
        pytest.fail("Classification refusal cannot interrupt a task")


class Wire:
    def __init__(self):
        self.confidence, self.fault, self.requests = 1.0, None, []

    def __call__(self, request):
        self.requests.append(request)
        if self.fault == "transport":
            raise httpx.ConnectError(MARKER)
        body = json.loads(request.content)
        answers = {}
        for name, question in body["questions"].items():
            labels = list(question["criteria"])
            choice = "ready" if name == "conversation" else "conversation"
            selected = self.confidence
            probabilities = {
                label: selected if label == choice else (1 - selected) / (len(labels) - 1)
                for label in labels
            }
            answers[name] = {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities,
                "confidence": selected,
            }
        if self.fault == "malformed":
            answers[next(iter(answers))]["confidence"] = True
        value = {
            "model": MODEL,
            "answers": answers,
            "usage": {"input_tokens": 123, "output_tokens": 0},
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(value).encode()))


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    wire = Wire()
    provider = NativeD1RoutingProvider(
        "http://127.0.0.1:8816",
        MODEL,
        min_confidence=0.8,
        transport=httpx.MockTransport(wire),
    )
    journal = LocalJournal(store, "decisions")
    state = SimpleNamespace(
        store=store,
        session=session,
        wire=wire,
        provider=provider,
        journal=journal,
        agent=NoAgent(),
        workers=[],
    )

    def source(mode):
        payload = {"text": MARKER, "revision": 1, "final": True, "content_scope": "local_only"}
        if mode == "window":
            payload = SpeechWindow(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                utterance_id="synthetic-475",
                window_index=0,
                text=MARKER,
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                capture_ended=True,
                content_scope="local_only",
            ).model_dump(mode="json")
        return store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window" if mode == "window" else "conversation.user",
                payload=payload,
                idempotency_key="source-475-" + mode,
            ),
            "owner",
        )

    state.source = source
    yield state
    for worker in state.workers:
        await worker.close()
    await provider.close()
    store.close()


def captures(env):
    return [
        json.loads(row[0])
        for row in env.store.db.execute("SELECT value FROM decisions_calls ORDER BY call_sequence")
    ]


async def classify(env, mode, source, *, journal=None):
    journal = env.journal if journal is None else journal
    if mode == "window":
        worker = WindowDecisionsWorker(journal, env.agent, WindowClassifier(env.provider))
        env.workers.append(worker)
        await worker._job(source)
    else:
        worker = DecisionsWorker(journal, env.agent, ModelClassifier(env.provider))
        await worker.process(source)
    return worker


@pytest.mark.parametrize("mode", ["window", "finalized"])
@pytest.mark.parametrize("confidence", [0.61, 0.3772113532])
async def test_valid_below_point_eight_is_single_refusal_with_private_typed_inspection(
    env, mode, confidence
):
    env.wire.confidence = confidence
    source = env.source(mode)
    worker = await classify(env, mode, source)
    assert env.provider.min_confidence == 0.8 and len(env.wire.requests) == 1
    (capture,) = captures(env)
    assert capture["status"] == "refused" and capture["error_type"] == "LowConfidenceRefusal"
    assert capture["validated_output"] is None
    assert capture["request_format"] == "native_d1_systemone"
    kind = "routing.window_status" if mode == "window" else "routing.classification_refused"
    rows = env.store.read(kinds=[kind])
    refusal = rows[-1]
    assert refusal.causes == [source.event_id]
    assert refusal.payload["reason"] == "provider_low_confidence"
    assert MARKER not in json.dumps(refusal.payload)
    assert env.store.read(kinds=["routing.decided", "routing.window_decided"]) == []
    if mode == "window":
        assert worker.last_error == "provider_low_confidence"
        assert refusal.payload["phase"] == "error"
        assert env.store.pending_windows() == []
        restarted = WindowDecisionsWorker(env.journal, env.agent, WindowClassifier(env.provider))
        env.workers.append(restarted)
        await restarted.tick()
        assert not restarted.jobs
    else:
        restarted = DecisionsWorker(env.journal, env.agent, ModelClassifier(env.provider))
        await restarted.process(source)
    assert len(env.wire.requests) == 1 and len(captures(env)) == 1


@pytest.mark.parametrize("mode", ["window", "finalized"])
@pytest.mark.parametrize("confidence", [0.8, 0.8001, 1.0])
async def test_exact_and_above_floor_keep_existing_eligible_routing(env, mode, confidence):
    env.wire.confidence = confidence
    await classify(env, mode, env.source(mode))
    assert env.provider.min_confidence == 0.8 and len(env.wire.requests) == 1
    (capture,) = captures(env)
    assert capture["status"] == "completed" and capture["error_type"] is None
    decisions = env.store.read(kinds=["routing.decided", "routing.window_decided"])
    assert len(decisions) == 1
    assert env.store.read(kinds=["routing.classification_refused"]) == []


@pytest.mark.parametrize(
    "fault,reason,calls",
    [
        ("malformed", "provider_refused", 1),
        ("transport", "provider_unavailable", 2),
    ],
)
async def test_window_malformed_and_existing_bounded_transport_failure_are_distinct(
    env, fault, reason, calls
):
    env.wire.fault = fault
    source = env.source("window")
    worker = await classify(env, "window", source)
    assert len(env.wire.requests) == calls and worker.last_error == reason
    refusal = env.store.read(kinds=["routing.window_status"])[-1]
    assert refusal.payload["phase"] == "error" and refusal.payload["reason"] == reason
    assert MARKER not in json.dumps(refusal.payload)
    assert all(row["error_type"] != "LowConfidenceRefusal" for row in captures(env))
    assert env.store.read(kinds=["routing.decided", "routing.window_decided"]) == []


async def test_finalized_restart_recovers_typed_refusal_after_status_append_outage_without_replay(
    env,
):
    class OnceUnavailable(LocalJournal):
        async def append(self, request):
            if request.kind == "routing.classification_refused":
                raise OSError("Synthetic refusal receipt unavailable")
            return await super().append(request)

    env.wire.confidence = 0.61
    source = env.source("finalized")
    with pytest.raises(OSError, match="Synthetic refusal receipt unavailable"):
        await classify(env, "finalized", source, journal=OnceUnavailable(env.store, "decisions"))
    assert len(env.wire.requests) == 1 and captures(env)[0]["error_type"] == "LowConfidenceRefusal"
    assert env.store.read(kinds=["routing.classification_refused"]) == []
    restarted = DecisionsWorker(env.journal, env.agent, ModelClassifier(env.provider))
    await restarted.process(source)
    (refusal,) = env.store.read(kinds=["routing.classification_refused"])
    assert refusal.payload["reason"] == "provider_low_confidence"
    assert refusal.payload["error_type"] == "LowConfidenceRefusal"
    assert len(env.wire.requests) == 1 and len(captures(env)) == 1
    assert MARKER not in json.dumps(refusal.payload)


def test_refusal_labels_are_closed_and_never_copied_from_exception_text():
    error = LowConfidenceRefusal()
    error.reason = MARKER
    assert classification_refusal_reason(error) == "provider_low_confidence"
    assert str(error) == "Native d1 answer was below the owner confidence floor"

    class UnprovenSubclass(LowConfidenceRefusal):
        pass

    for other in (ProviderRefusal(MARKER), UnprovenSubclass(), Exception(MARKER), None):
        assert classification_refusal_reason(other) == "provider_refused"
    for other in (MARKER, True, None, {}, "ProviderRefusal"):
        assert (
            retained_refusal_reason(
                other, provider="native_d1", request_format="native_d1_systemone"
            )
            == "provider_refused"
        )
    assert (
        retained_refusal_reason(
            "LowConfidenceRefusal", provider="native_d1", request_format="native_d1_systemone"
        )
        == "provider_low_confidence"
    )
    for provider, protocol in (
        ("other", "native_d1_systemone"),
        ("native_d1", "responses"),
        (None, None),
    ):
        assert (
            retained_refusal_reason(
                "LowConfidenceRefusal", provider=provider, request_format=protocol
            )
            == "provider_refused"
        )
    with pytest.raises(TypeError):
        LowConfidenceRefusal(MARKER)
