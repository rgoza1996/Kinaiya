"""Independent #451 buffered native-audio integration oracles.

Only public synthetic PCM/text is used.  Store, LocalJournal, the controller,
context builder and authenticated ASGI routes are real; the external native
model is a controlled async fixture.  No model quality or audible-device claim
is made.  These tests were authored without reading either new implementation.
"""

import asyncio
import base64
import copy
import hashlib
import json
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from packages.contracts.models import (
    AppendRequest,
    DeliveryRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.realtime_audio import NativeAudioInput, NativeAudioReply
from packages.providers.speech import SpeechProviderError
from services import launcher
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.context_budget import ContextBudgetBuilder
from services.converse.native_audio import NativeAudioOwner, NativeClipRequest
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store

PUBLIC_TEXT = "Please prepare the public synthetic checklist."
PUBLIC_REPLY = "Give me some time on it."
INPUT_PCM = struct.pack("<5h", 71, -38, 0, 122, -109)
OUTPUT_PCM = struct.pack("<6h", 211, -277, 49, 0, 18, -25)
AUTH = Auth({"owner": "o" * 32, "agent": "a" * 32, "converse": "c" * 32})


def wav(pcm=INPUT_PCM):
    """Canonical PCM16 mono16k WAV, independently encoded from protocol fields."""
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + len(pcm),
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            16000,
            32000,
            2,
            16,
            b"data",
            len(pcm),
        )
        + pcm
    )


class NoLegacyText:
    info = ProviderInfo(provider="compatible", model="public-native-fixture", local=True)

    def __init__(self):
        self.counts = []
        self.streams = []

    async def measure_prompt(self, messages):
        self.counts.append(copy.deepcopy(messages))
        raise AssertionError("Native audio must not claim text-only context measurement")

    async def stream(self, messages):
        self.streams.append(copy.deepcopy(messages))
        raise AssertionError("Native audio must not fall back to text generation")
        yield "unreachable"


class NativeModel:
    """Only the public native interface, with real controllable async waits."""

    def __init__(self):
        self.info = NoLegacyText.info
        self.model = self.info.model
        self.base_url = "http://127.0.0.1:8817"
        self.transcriptions = []
        self.responses = []
        self.text = PUBLIC_REPLY
        self.asr_entered, self.asr_release = asyncio.Event(), asyncio.Event()
        self.reply_entered, self.reply_release = asyncio.Event(), asyncio.Event()
        self.asr_release.set()
        self.reply_release.set()
        self.closed = 0
        self.asr_error = None

    async def transcribe(self, audio, *, current):
        assert await current() is True
        self.transcriptions.append(audio)
        self.asr_entered.set()
        await self.asr_release.wait()
        if self.asr_error is not None:
            raise self.asr_error
        # Deliberately return even after an owner change: the owner boundary
        # must independently refuse this late fixture result.
        return PUBLIC_TEXT

    async def respond(self, audio, messages, *, current):
        assert await current() is True
        self.responses.append((audio, copy.deepcopy(messages)))
        self.reply_entered.set()
        await self.reply_release.wait()
        return NativeAudioReply(self.text, OUTPUT_PCM, 24000)

    async def close(self):
        self.closed += 1


@asynccontextmanager
async def environment(tmp_path, *, native=None, speech=None, owner_path=None):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    provider = NoLegacyText()
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"),
        provider,
        personality="Public synthetic companion personality.",
        personality_scope="public_synthetic",
        context_budgeted=True,
    )
    native = native or NativeModel()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    path = owner_path or tmp_path / "native-audio.sqlite"
    owner = NativeAudioOwner(runtime, native, path=path, workspace=workspace)
    runtime.native_audio = owner
    controller = WindowConverseController(runtime)
    app = create_app(runtime, AUTH, speech, controller)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://synthetic.invalid",
        headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
    ) as client:
        env = SimpleNamespace(
            store=store,
            session=session,
            provider=provider,
            runtime=runtime,
            native=native,
            owner=owner,
            controller=controller,
            http=client,
            path=path,
            workspace=workspace,
        )
        try:
            yield env
        finally:
            native.asr_release.set()
            native.reply_release.set()
            await controller.close()
            await owner.close()
            await runtime.close()
            store.close()


def body(env, *, pcm=INPUT_PCM, **changes):
    return {
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
        "utterance_id": str(uuid4()),
        "request_id": str(uuid4()),
        "content_scope": "public_synthetic",
        "audio_base64": base64.b64encode(wav(pcm)).decode(),
        **changes,
    }


async def admit(env, request=None):
    return await env.owner.admit(
        NativeClipRequest.model_validate(request or body(env)), env.controller
    )


def source(env, result):
    return env.store.entry(result["entry"]["event_id"])


def decide(env, entry, behavior="RESPOND_WITHOUT_INTERRUPTING_USER"):
    return env.store.append(
        AppendRequest(
            session_id=entry.session_id,
            thread_id=entry.thread_id,
            epoch=entry.epoch,
            kind="routing.window_decided",
            causes=[entry.event_id],
            payload=WindowDecision(
                source_event_id=entry.event_id,
                source_sequence=entry.sequence,
                source_revision=entry.payload["revision"],
                conversation=behavior,
                agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
            ).model_dump(mode="json"),
            idempotency_key="public-decision:" + entry.event_id,
        ),
        "decisions",
    )


async def settle(env, *, cancelled=False):
    jobs = [prepared.task for prepared in env.controller.pending.values()]
    if jobs:
        outcomes = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 2)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                assert cancelled and isinstance(outcome, asyncio.CancelledError), outcome
    await env.controller.tick()


def previous_user(env, index, *, text=None, scope="public_synthetic"):
    return env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.user",
            payload={
                "text": text or f"Public earlier thought {index}.",
                "revision": 1,
                "final": True,
                "content_scope": scope,
            },
            idempotency_key=f"public-earlier:{index}",
        ),
        "owner",
    )


async def test_admission_retains_original_pcm_and_journals_only_actual_asr(tmp_path):
    async with environment(tmp_path) as env:
        request = body(env)
        result = await admit(env, request)
        entry = source(env, result)
        assert result["replay"] is False
        # Baseline Store._append authenticates every conversation.window as
        # user input, including a Converse service writer (store.py:969-970).
        assert entry.kind == "conversation.window" and entry.origin == "authenticated-user"
        assert entry.session_id == env.session.session_id
        assert entry.thread_id == env.session.thread_id and entry.epoch == env.session.epoch
        expected = {
            "text": PUBLIC_TEXT,
            "utterance_id": request["utterance_id"],
            "window_index": 0,
            "revision": 1,
            "start_ms": 0,
            "end_ms": 1,
            "capture_ended": True,
            "user_speaking": False,
            "content_scope": "public_synthetic",
            "audio_sha256": hashlib.sha256(wav()).hexdigest(),
        }
        assert {key: entry.payload[key] for key in expected} == expected
        assert env.native.transcriptions == [NativeAudioInput(INPUT_PCM, 16000)]
        assert env.owner.clip_for(entry) == NativeAudioInput(INPUT_PCM, 16000)
        assert env.native.responses == []
        assert env.provider.streams == env.provider.counts == []
        assert env.store.read(kinds=["conversation.converse"]) == []


async def test_exact_duplicate_joins_original_asr_then_replays_same_journal_entry(tmp_path):
    model = NativeModel()
    model.asr_release.clear()
    async with environment(tmp_path, native=model) as env:
        request = body(env)
        original = asyncio.create_task(admit(env, request))
        duplicate = None
        try:
            await asyncio.wait_for(model.asr_entered.wait(), 1)
            duplicate = asyncio.create_task(admit(env, copy.deepcopy(request)))
            await asyncio.sleep(0)
            assert not original.done() and not duplicate.done()
            assert len(model.transcriptions) == 1
            assert env.store.read(kinds=["conversation.window"]) == []
            model.asr_release.set()
            first, replay = await asyncio.wait_for(asyncio.gather(original, duplicate), 1)
            assert first["replay"] is False and replay["replay"] is True
            assert replay["entry"] == first["entry"]
            assert (await admit(env, request))["entry"] == first["entry"]
            assert len(model.transcriptions) == 1
            assert len(env.store.read(kinds=["conversation.window"])) == 1
        finally:
            model.asr_release.set()
            await asyncio.gather(*(t for t in (original, duplicate) if t), return_exceptions=True)


@pytest.mark.parametrize("change", ["audio", "scope", "utterance", "request"])
async def test_reused_identity_with_different_admission_never_transcribes_again(tmp_path, change):
    async with environment(tmp_path) as env:
        request = body(env)
        await admit(env, request)
        changed = dict(request)
        if change == "audio":
            changed["audio_base64"] = base64.b64encode(wav(b"\x01\x00")).decode()
        elif change == "scope":
            changed["content_scope"] = "local_only"
        elif change == "utterance":
            changed["utterance_id"] = str(uuid4())
        else:
            changed["request_id"] = str(uuid4())
        with pytest.raises(SpeechProviderError) as refused:
            await admit(env, changed)
        assert refused.value.status_code == 409
        assert len(env.native.transcriptions) == 1
        assert len(env.store.read(kinds=["conversation.window"])) == 1


async def test_cancelled_waiter_cannot_duplicate_original_admission(tmp_path):
    model = NativeModel()
    model.asr_release.clear()
    async with environment(tmp_path, native=model) as env:
        request = body(env)
        waiter = asyncio.create_task(admit(env, request))
        replay = None
        try:
            await asyncio.wait_for(model.asr_entered.wait(), 1)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            replay = asyncio.create_task(admit(env, request))
            await asyncio.sleep(0)
            assert not replay.done() and len(model.transcriptions) == 1
            model.asr_release.set()
            result = await asyncio.wait_for(replay, 1)
            assert result["replay"] is True
            assert len(env.store.read(kinds=["conversation.window"])) == 1
            assert len(model.transcriptions) == 1
        finally:
            model.asr_release.set()
            await asyncio.gather(*(t for t in (waiter, replay) if t), return_exceptions=True)


@pytest.mark.parametrize("change", ["off-on", "epoch", "host-provider", "native-provider"])
async def test_late_asr_cannot_publish_across_owner_or_source_change(tmp_path, change):
    model = NativeModel()
    model.asr_release.clear()
    async with environment(tmp_path, native=model) as env:
        request = body(env)
        pending = asyncio.create_task(admit(env, request))
        try:
            await asyncio.wait_for(model.asr_entered.wait(), 1)
            if change == "off-on":
                await env.runtime.set_enabled(False, owner=True)
                await env.runtime.set_enabled(True, owner=True)
            elif change == "epoch":
                env.store.reset(env.session.session_id)
            elif change == "host-provider":
                env.runtime.provider = NoLegacyText()
            else:
                env.runtime.native_audio = object()
            model.asr_release.set()
            with pytest.raises(SpeechProviderError) as refused:
                await asyncio.wait_for(pending, 1)
            assert refused.value.status_code == 409
            assert env.store.read(kinds=["conversation.window"]) == []
            with pytest.raises(SpeechProviderError):
                await admit(env, request)
            assert len(model.transcriptions) == 1
        finally:
            model.asr_release.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_restart_never_reconstructs_lost_original_audio_or_repeats_asr(tmp_path):
    async with environment(tmp_path) as env:
        request = body(env)
        old = source(env, await admit(env, request))
        await env.owner.close()
        replacement_model = NativeModel()
        replacement = NativeAudioOwner(
            env.runtime, replacement_model, path=env.path, workspace=env.workspace
        )
        env.runtime.native_audio = replacement
        try:
            with pytest.raises(SpeechProviderError) as refused:
                await replacement.admit(NativeClipRequest.model_validate(request), env.controller)
            assert refused.value.status_code == 409
            with pytest.raises(SpeechProviderError):
                replacement.clip_for(old)
            assert replacement_model.transcriptions == []
            assert replacement_model.responses == []
            assert len(env.store.read(kinds=["conversation.window"])) == 1
        finally:
            await replacement.close()


async def test_failed_original_asr_is_durably_not_replayed_as_new_inference(tmp_path):
    model = NativeModel()
    model.asr_error = SpeechProviderError(503)
    async with environment(tmp_path, native=model) as env:
        request = body(env)
        with pytest.raises(SpeechProviderError):
            await admit(env, request)
        with pytest.raises(SpeechProviderError):
            await admit(env, request)
        assert len(model.transcriptions) == 1
        assert env.store.read(kinds=["conversation.window"]) == []


@pytest.mark.parametrize("behavior", ["IGNORE", "WAIT_FOR_NEXT_WINDOW"])
async def test_ignored_or_waiting_decision_keeps_original_without_native_response(
    tmp_path, behavior
):
    async with environment(tmp_path) as env:
        entry = source(env, await admit(env))
        await env.controller.process(decide(env, entry, behavior))
        await settle(env)
        assert env.native.responses == []
        assert env.store.read(kinds=["conversation.converse"]) == []
        assert env.owner.clip_for(entry).pcm16 == INPUT_PCM
        assert env.store.entry(entry.event_id).payload["text"] == PUBLIC_TEXT


async def test_model_shaped_decision_cannot_release_native_audio_without_journal_authority(
    tmp_path,
):
    async with environment(tmp_path) as env:
        entry = source(env, await admit(env))
        authorized = decide(env, entry)
        forged = authorized.model_copy(update={"origin": "untrusted-model"}, deep=True)
        await env.controller.process(forged)
        await settle(env)
        assert env.native.responses == []
        assert env.store.read(kinds=["conversation.converse"]) == []
        await env.controller.process(authorized)
        await settle(env)
        assert len(env.native.responses) == 1
        assert len(env.store.read(kinds=["conversation.converse"])) == 1


async def test_authenticated_release_passes_raw_audio_and_complete_history_without_counting(
    tmp_path,
):
    async with environment(tmp_path) as env:
        history = [f"Public complete history {index}: " + "x" * 2101 for index in range(21)]
        for index, text in enumerate(history):
            previous_user(env, index, text=text)
        entry = source(env, await admit(env))
        assert env.native.responses == []
        decision = decide(env, entry)
        await env.controller.process(decision)
        await settle(env)
        assert len(env.native.responses) == 1
        audio, messages = env.native.responses[0]
        assert audio == NativeAudioInput(INPUT_PCM, 16000)
        assert [m["content"] for m in messages if m["role"] == "user"] == [*history, PUBLIC_TEXT]
        assert messages[0] == {"role": "system", "content": env.runtime.personality}
        assert env.provider.counts == env.provider.streams == []
        replies = env.store.read(kinds=["conversation.converse"])
        assert len(replies) == 1
        reply = replies[0]
        assert reply.payload["text"] == PUBLIC_REPLY
        assert reply.payload["native_audio"] is True
        assert reply.payload["native_audio_sha256"] == hashlib.sha256(OUTPUT_PCM).hexdigest()
        assert reply.payload["window_source_id"] == entry.event_id
        assert reply.payload["window_utterance_id"] == entry.payload["utterance_id"]
        await env.controller.process(decision)
        await settle(env)
        assert len(env.native.responses) == 1


async def test_complete_unmeasured_builder_has_no_token_fit_or_selection_claim(tmp_path):
    async with environment(tmp_path) as env:
        values = [f"Public unmeasured thought {index}." for index in range(23)]
        for index, text in enumerate(values):
            previous_user(env, index, text=text)
        entry = source(env, await admit(env))
        builder = ContextBudgetBuilder(
            env.runtime.journal,
            entry,
            env.provider,
            personality=env.runtime.personality,
            runtime_id=env.runtime._runtime_id,
            current=lambda: True,
        )
        projection = await builder.complete_unmeasured()
        assert [m["content"] for m in projection.messages if m["role"] == "user"] == [
            *values,
            PUBLIC_TEXT,
        ]
        assert projection.measurement is None
        assert projection.history_selection is None
        assert projection.history_watermark is not None
        assert env.provider.counts == []


async def test_text_only_window_cannot_invent_original_native_audio(tmp_path):
    async with environment(tmp_path) as env:
        entry = await env.controller.ingest(
            SpeechWindow(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                utterance_id="public-text-with-no-recording",
                window_index=0,
                revision=1,
                text=PUBLIC_TEXT,
                start_ms=0,
                end_ms=1000,
                capture_ended=True,
                user_speaking=False,
                content_scope="public_synthetic",
            )
        )
        await env.controller.process(decide(env, entry))
        await settle(env)
        assert env.native.responses == []
        assert env.provider.streams == env.provider.counts == []
        assert env.store.read(kinds=["conversation.converse"]) == []


@pytest.mark.parametrize("change", ["off-on", "epoch", "interrupt"])
async def test_late_native_reply_cannot_restore_fenced_audio(tmp_path, change):
    model = NativeModel()
    model.reply_release.clear()
    async with environment(tmp_path, native=model) as env:
        entry = source(env, await admit(env))
        await env.controller.process(decide(env, entry))
        try:
            await asyncio.wait_for(model.reply_entered.wait(), 1)
            if change == "off-on":
                await env.runtime.set_enabled(False, owner=True)
                await env.runtime.set_enabled(True, owner=True)
            elif change == "epoch":
                env.store.reset(env.session.session_id)
            else:
                await env.runtime.interrupt(env.session.session_id)
            model.reply_release.set()
            await settle(env, cancelled=True)
            assert env.store.read(kinds=["conversation.converse"]) == []
            assert len(model.responses) == 1
            assert env.provider.streams == []
        finally:
            model.reply_release.set()


async def released(env):
    entry = source(env, await admit(env))
    await env.controller.process(decide(env, entry))
    await settle(env)
    replies = env.store.read(kinds=["conversation.converse"])
    assert len(replies) == 1
    return replies[0]


async def request_speech(env, reply, **changes):
    return await env.http.post(
        "/v1/converse/speech",
        json={
            "event_id": reply.event_id,
            "generation_epoch": reply.payload["generation_epoch"],
            **changes,
        },
    )


async def test_native_speech_uses_original_cached_pcm_without_any_tts_configuration(tmp_path):
    async with environment(tmp_path) as env:
        reply = await released(env)
        response = await request_speech(env, reply)
        assert response.status_code == 200, response.text
        frames = [json.loads(line) for line in response.text.splitlines() if line]
        assert frames and b"".join(base64.b64decode(f["audio"]) for f in frames) == OUTPUT_PCM
        assert all(f["sample_rate"] == 24000 for f in frames)
        assert all(f["event_id"] == reply.event_id for f in frames)
        assert all(f["generation_epoch"] == reply.payload["generation_epoch"] for f in frames)
        assert len(env.native.responses) == 1
        assert env.provider.streams == []
        assert env.store.read(kinds=["delivery.updated"]) == [], "Generation is not delivery"
        await env.runtime.acknowledge_delivery(
            DeliveryRequest(
                event_id=reply.event_id,
                generation_epoch=reply.payload["generation_epoch"],
                chunk_id=reply.payload["chunk_id"],
                status="played",
                played_seconds=0.00025,
            )
        )
        assert env.store.read(kinds=["delivery.updated"])[-1].payload["status"] == "played"


@pytest.mark.parametrize(
    "generated,guarded",
    [
        ("I saved your file.", "The Agent has not provided a verified result for that claim."),
        ("No Agent is available.", "I can't verify that Agent or task status."),
    ],
)
async def test_guard_rewrite_drops_contradictory_native_pcm_without_tts_fallback(
    tmp_path, generated, guarded
):
    class NoTTS:
        calls = []

        async def synthesize(self, text):
            self.calls.append(text)
            raise AssertionError("A guarded native reply cannot fall back to TTS")
            yield {}

    model, tts = NativeModel(), NoTTS()
    model.text = generated
    async with environment(tmp_path, native=model, speech=tts) as env:
        reply = await released(env)
        assert reply.payload["text"] == guarded
        assert reply.payload.get("native_audio") is False
        assert not reply.payload.get("native_audio_sha256")
        response = await request_speech(env, reply)
        assert response.status_code == 409
        assert response.content and OUTPUT_PCM not in response.content
        assert tts.calls == []
        assert len(model.responses) == 1


async def test_cached_native_speech_stays_invalid_after_stop_and_owner_on_again(tmp_path):
    async with environment(tmp_path) as env:
        reply = await released(env)
        await env.runtime.interrupt(env.session.session_id)
        await env.runtime.set_enabled(False, owner=True)
        await env.runtime.set_enabled(True, owner=True)
        response = await request_speech(env, reply)
        assert response.status_code == 409
        assert len(env.native.responses) == 1
        assert env.provider.streams == []


async def test_native_cache_binds_original_event_generation_and_cannot_be_forged(tmp_path):
    async with environment(tmp_path) as env:
        reply = await released(env)
        with pytest.raises(SpeechProviderError):
            env.owner.speech_source(reply, reply.payload["generation_epoch"] + 1)
        forged = reply.model_copy(deep=True)
        forged.payload["native_audio_sha256"] = "0" * 64
        with pytest.raises(SpeechProviderError):
            env.owner.speech_source(forged, reply.payload["generation_epoch"])
        forged = reply.model_copy(deep=True)
        forged.payload["text"] = "Different caption."
        with pytest.raises(SpeechProviderError):
            env.owner.speech_source(forged, reply.payload["generation_epoch"])
        assert len(env.native.responses) == 1


async def test_native_ingress_and_cached_speech_keep_owner_auth_boundary(tmp_path):
    async with environment(tmp_path) as env:
        request = body(env)
        for token, status in [(None, 401), (AUTH.tokens["agent"], 403)]:
            headers = {"Authorization": "Bearer " + token} if token else {"Authorization": ""}
            denied = await env.http.post(
                "/v1/converse/native-audio/clips", json=request, headers=headers
            )
            assert denied.status_code == status
        assert env.native.transcriptions == []
        admitted = await env.http.post("/v1/converse/native-audio/clips", json=request)
        assert admitted.status_code == 200, admitted.text
        await env.controller.process(decide(env, source(env, admitted.json())))
        await settle(env)
        reply = env.store.read(kinds=["conversation.converse"])[0]
        denied = await env.http.post(
            "/v1/converse/speech",
            json={
                "event_id": reply.event_id,
                "generation_epoch": reply.payload["generation_epoch"],
            },
            headers={"Authorization": "Bearer " + AUTH.tokens["agent"]},
        )
        assert denied.status_code == 403
        assert len(env.native.transcriptions) == len(env.native.responses) == 1


async def assert_bad_clip_request(env, request):
    """The HTTP refusal owns diagnostics; Pydantic's caller envelope is separate."""
    response = await env.http.post("/v1/converse/native-audio/clips", json=request)
    assert response.status_code == 400
    diagnostic = response.json()
    assert type(diagnostic) is dict and set(diagnostic) == {"detail"}
    assert type(diagnostic["detail"]) is str and 1 <= len(diagnostic["detail"]) <= 160
    marker = "UNTRUSTED-INVALID-NATIVE-AUDIO-REQUEST-451"
    control = await env.http.post(
        "/v1/converse/native-audio/clips", json=body(env, audio_base64=marker)
    )
    assert control.status_code == 400
    assert response.content == control.content, "Malformed request diagnostics must be constant"
    assert marker not in response.text
    for field in ("session_id", "thread_id", "utterance_id", "request_id", "audio_base64", "text"):
        value = request.get(field)
        if type(value) is str and len(value) >= 8:
            assert value not in response.text
    assert env.native.transcriptions == []
    assert env.store.read(kinds=["conversation.window"]) == []


@pytest.mark.parametrize(
    "change",
    [
        {"audio_base64": "not canonical base64!"},
        {"audio_base64": base64.b64encode(b"not a WAV").decode()},
        {"audio_base64": base64.b64encode(wav(b"")).decode()},
        {"audio_base64": base64.b64encode(wav(b"\x00")).decode()},
        {"audio_base64": base64.b64encode(wav() + b"trailing").decode()},
        {"audio_base64": base64.b64encode(wav()).decode() + "\n"},
    ],
)
async def test_noncanonical_or_empty_audio_is_refused_before_asr(tmp_path, change):
    async with environment(tmp_path) as env:
        await assert_bad_clip_request(env, body(env, **change))


@pytest.mark.parametrize("offset", [0, 4, 8, 12, 16, 20, 22, 24, 28, 32, 34, 36, 40])
async def test_each_canonical_wav_header_field_is_validated_before_asr(tmp_path, offset):
    async with environment(tmp_path) as env:
        altered = bytearray(wav())
        altered[offset] ^= 1
        await assert_bad_clip_request(
            env, body(env, audio_base64=base64.b64encode(altered).decode())
        )


async def test_real_thirty_second_input_ceiling_and_ceil_duration(tmp_path):
    async with environment(tmp_path) as env:
        pcm = b"\x25\x00" * 480000
        entry = source(env, await admit(env, body(env, pcm=pcm)))
        assert entry.payload["end_ms"] == 30000
        assert env.native.transcriptions[0].pcm16 == pcm
        with pytest.raises((SpeechProviderError, ValidationError)):
            await admit(env, body(env, pcm=pcm + b"\x00\x00"))
        assert len(env.native.transcriptions) == 1


async def test_retained_byte_capacity_refuses_without_evicting_original(tmp_path):
    async with environment(tmp_path) as env:
        requests = [body(env, pcm=b"\x00\x00") for _ in range(8)]
        results = [await admit(env, request) for request in requests[:7]]
        with pytest.raises(SpeechProviderError) as refused:
            await admit(env, requests[7])
        assert refused.value.status_code == 503
        assert len(env.native.transcriptions) == 7
        replay = await admit(env, requests[0])
        assert replay["replay"] is True and replay["entry"] == results[0]["entry"]
        assert env.owner.clip_for(source(env, replay)).pcm16 == b"\x00\x00"
        assert len(env.native.transcriptions) == 7


@pytest.mark.parametrize(
    "change",
    [
        {"epoch": 99},
        {"thread_id": "00000000-0000-4000-8000-000000000001"},
        {"session_id": "00000000-0000-4000-8000-000000000002"},
    ],
)
async def test_obsolete_scope_never_starts_native_asr(tmp_path, change):
    async with environment(tmp_path) as env:
        with pytest.raises(SpeechProviderError) as refused:
            await admit(env, body(env, **change))
        assert refused.value.status_code == 409
        assert env.native.transcriptions == []


@pytest.mark.parametrize(
    "change",
    [
        {"utterance_id": "not-a-UUID"},
        {"request_id": "not-a-UUID"},
        {"session_id": "not-a-UUID"},
        {"thread_id": "not-a-UUID"},
        {"epoch": True},
        {"content_scope": "model-granted"},
        {"text": "Never accept a caller-supplied ASR transcript"},
        {"native_audio": True},
    ],
)
async def test_native_request_schema_does_not_accept_authority_or_transcript_substitution(
    tmp_path, change
):
    async with environment(tmp_path) as env:
        request = body(env, **change)
        with pytest.raises(ValidationError):
            NativeClipRequest.model_validate(request)
        await assert_bad_clip_request(env, request)


async def test_health_declares_buffered_import_capability_without_claiming_duplex(tmp_path):
    async with environment(tmp_path) as env:
        state = (await env.http.get("/health")).json()["native_audio"]
        expected = {
            "configured": True,
            "available": True,
            "mode": "buffered_native_audio",
            "input_sample_rate": 16000,
            "max_seconds": 30,
        }
        assert {key: state[key] for key in expected} == expected
        assert state["pending"] is False
        assert state["active"] == 0 and state["cleanup_failed"] is False
        assert "duplex" not in json.dumps(state)


def native_config():
    return {
        "providers": {
            "converse": {
                "kind": "compatible",
                "base_url": "http://127.0.0.1:8817/v1",
                "model": "public-native-fixture",
                "api_key_env": "KINAIYA_PUBLIC_NATIVE_FIXTURE",
                "allow_cloud": False,
            }
        },
        "native_audio": {
            "kind": "realtime_v1",
            "base_url": "http://127.0.0.1:8817",
            "model": "public-native-fixture",
            "api_key_env": "KINAIYA_PUBLIC_NATIVE_FIXTURE",
            "max_output_tokens": 256,
            "timeout": 45,
        },
    }


@pytest.fixture
def native_constructions(monkeypatch):
    from packages.providers import realtime_audio
    from services.converse import native_audio

    constructions, owners = [], []

    class Adapter:
        def __init__(self, base_url, model, **options):
            self.arguments = {"base_url": base_url, "model": model, **options}
            constructions.append(self)

    class Owner:
        def __init__(self, runtime, provider, *, path, workspace):
            self.arguments = (runtime, provider, path, workspace)
            owners.append(self)

    monkeypatch.setattr(realtime_audio, "RealtimeAudioProvider", Adapter)
    monkeypatch.setattr(native_audio, "NativeAudioOwner", Owner)
    monkeypatch.setenv("KINAIYA_PUBLIC_NATIVE_FIXTURE", "public-synthetic-local-bearer")
    return constructions, owners


@pytest.fixture
async def host_runtime(tmp_path):
    store = Store(tmp_path / "host-journal.sqlite")
    runtime = ConverseRuntime(LocalJournal(store, "converse"), NoLegacyText())
    try:
        yield runtime
    finally:
        await runtime.close()
        store.close()


@pytest.mark.parametrize("kind", ["compatible", "measured_chat"])
async def test_explicit_host_pair_constructs_native_provider_without_loading_inference(
    tmp_path, native_constructions, host_runtime, kind
):
    config = native_config()
    config["providers"]["converse"]["kind"] = kind
    if kind == "measured_chat":
        config["providers"]["converse"]["context_tokens"] = 8192
    data_dir, workspace = tmp_path / "protected", tmp_path / "workspace"
    data_dir.mkdir()
    workspace.mkdir()
    owner = launcher.converse_native_audio(config, host_runtime, data_dir, workspace)
    providers, owners = native_constructions
    assert len(providers) == len(owners) == 1
    assert owner is owners[0]
    assert providers[0].arguments == {
        "base_url": "http://127.0.0.1:8817",
        "model": "public-native-fixture",
        "api_key": "public-synthetic-local-bearer",
        "max_output_tokens": 256,
        "timeout": 45,
    }
    assert owner.arguments == (
        host_runtime,
        providers[0],
        data_dir / "native-audio.sqlite",
        workspace,
    )


@pytest.mark.parametrize("declaration", ["absent", "none"])
async def test_absent_native_declaration_leaves_existing_modular_stack_unchanged(
    tmp_path, native_constructions, host_runtime, declaration
):
    config = native_config()
    if declaration == "absent":
        del config["native_audio"]
    else:
        config["native_audio"] = None
    assert (
        launcher.converse_native_audio(config, host_runtime, tmp_path, tmp_path / "workspace")
        is None
    )
    assert native_constructions == ([], [])


@pytest.mark.parametrize(
    "case",
    [
        "wrong-adapter",
        "cloud-host",
        "cloud-native",
        "different-port",
        "different-model",
        "different-key-reference",
        "inline-native-key",
        "inline-host-key",
        "resources-enabled",
        "unknown-native-field",
        "missing-host",
        "nonmapping-native",
    ],
)
async def test_unsupported_native_configuration_refuses_before_any_provider_fallback(
    tmp_path, native_constructions, host_runtime, case
):
    config = native_config()
    native, host = config["native_audio"], config["providers"]["converse"]
    if case == "wrong-adapter":
        host["kind"] = "meta"
    elif case == "cloud-host":
        host["allow_cloud"] = True
    elif case == "cloud-native":
        native["base_url"] = "https://public.synthetic.invalid"
    elif case == "different-port":
        native["base_url"] = "http://127.0.0.1:8818"
    elif case == "different-model":
        native["model"] = "another-public-model"
    elif case == "different-key-reference":
        native["api_key_env"] = "KINAIYA_ANOTHER_PUBLIC_FIXTURE"
    elif case == "inline-native-key":
        native["api_key"] = "public-inline-key"
    elif case == "inline-host-key":
        host["api_key"] = "public-inline-key"
    elif case == "resources-enabled":
        config["resources"] = {"enabled": True}
    elif case == "unknown-native-field":
        native["automatic_text_fallback"] = True
    elif case == "missing-host":
        del config["providers"]["converse"]
    else:
        config["native_audio"] = True
    with pytest.raises(ValueError):
        launcher.converse_native_audio(config, host_runtime, tmp_path, tmp_path / "workspace")
    assert native_constructions == ([], [])
