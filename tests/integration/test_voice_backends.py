"""Synthetic local TTS coverage: no optional runtime, model assets, or network."""

import asyncio
import base64
import builtins
import json
import struct
import threading

import httpx
import pytest

from packages.providers.tts_backends import AudioWaveform, TTSBackendError
from packages.providers.voice_worker import create_app
from services.common.auth import Auth
from services.resources.state import ResourceState

AUTH = Auth(
    {role: letter * 32 for role, letter in [("owner", "o"), ("converse", "c"), ("agent", "a")]}
)
HEADERS = {"Authorization": "Bearer " + "c" * 32}
PROFILE = "Supertonic 3"


class SyntheticBackend:
    assets_state = "not_checked"

    def __init__(self, waveforms=None, *, load_error=None):
        self.waveforms = waveforms if waveforms is not None else [AudioWaveform([0.25] * 8, 8000)]
        self.load_error = load_error
        self.loads = 0
        self.inputs = []

    def load(self):
        self.loads += 1
        if self.load_error:
            raise self.load_error
        self.assets_state = "verified"

    def synthesize(self, text):
        self.inputs.append(text)
        yield from self.waveforms


class SyntheticFactory:
    def __init__(self, backend=None):
        self.backend = backend or SyntheticBackend()
        self.calls = []

    def __call__(self, profile, model_dir, voice):
        self.calls.append((profile, model_dir, voice))
        return self.backend


def client_for(factory, *, profile=PROFILE, messages=None, on_send=None, **options):
    app = create_app(AUTH, tts_profile=profile, tts_factory=factory, **options)

    async def recorded_app(scope, receive, send):
        async def record(message):
            if messages is not None:
                messages.append(message.copy())
            if on_send is not None:
                on_send(message)
            await send(message)

        await app(scope, receive, record)

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recorded_app), base_url="http://test", headers=HEADERS
    )


async def synthesize(client, text="Public synthetic speech.", **fields):
    return await client.post("/v1/audio/speech", json={"input": text, "profile": PROFILE, **fields})


def audio_frames(response):
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines()]


async def entered(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 3), 4)


@pytest.mark.parametrize(
    "profile, voice, mode",
    [("Kokoro 82M", "af_heart", "sentence_buffered"), (PROFILE, "F1", "buffered")],
)
async def test_health_reports_selected_profile_without_constructing_backend(profile, voice, mode):
    factory = SyntheticFactory()
    app = create_app(AUTH, tts_profile=profile, tts_factory=factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    health = response.json()
    assert health["service"] == "local-voice"
    assert health["device"] == "cpu"
    assert health["pid"] > 0
    assert health["loaded"] == []
    assert health["tts_profile"] == profile
    assert health["tts_voice"] == voice
    assert health["synthesis_mode"] == mode
    assert health["assets_state"] == "not_checked"
    assert health["tts_artifact_revision"] == (
        "aafc6e32416a594460b32413efc49d7fe4ce6d46" if profile == PROFILE else None
    )
    assert factory.calls == []


@pytest.mark.parametrize("profile", ["Ming 3 Tiny", "unknown"])
def test_unimplemented_or_unknown_configured_profile_fails_without_factory(profile):
    factory = SyntheticFactory()
    with pytest.raises(ValueError):
        create_app(AUTH, tts_profile=profile, tts_factory=factory)
    assert factory.calls == []


@pytest.mark.parametrize(
    "headers, status",
    [
        ({}, 401),
        ({"Authorization": "Bearer synthetic-invalid"}, 401),
        ({"Authorization": "Bearer " + "a" * 32}, 403),
        ({**HEADERS, "Origin": "https://example.invalid"}, 403),
    ],
)
async def test_unauthorized_synthesis_never_constructs_backend(headers, status):
    factory = SyntheticFactory()
    async with client_for(factory) as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"input": "Public synthetic speech.", "profile": PROFILE},
            headers={"Authorization": "", **headers},
        )
    assert response.status_code == status
    assert factory.calls == []


@pytest.mark.parametrize("text", ["", "x" * 16001], ids=["empty", "over-bound"])
async def test_invalid_text_never_constructs_backend(text):
    factory = SyntheticFactory()
    async with client_for(factory) as client:
        response = await synthesize(client, text)
    assert response.status_code == 422
    assert factory.calls == []


@pytest.mark.parametrize("length, status", [(1200, 200), (1201, 413), (16000, 413)])
async def test_supertonic_buffered_input_cap_is_checked_before_factory(length, status):
    factory = SyntheticFactory()
    async with client_for(factory) as client:
        response = await synthesize(client, "x" * length)
    assert response.status_code == status
    if status == 413:
        assert factory.calls == []
    else:
        assert factory.backend.inputs == ["x" * length]


async def test_kokoro_retains_text_bound_and_sentence_buffered_metadata():
    factory = SyntheticFactory()
    async with client_for(factory, profile="Kokoro 82M") as client:
        frames = audio_frames(await synthesize(client, "x" * 16000, profile="Kokoro 82M"))
    assert frames[0]["provider"] == "Kokoro 82M"
    assert frames[0]["synthesis_mode"] == "sentence_buffered"
    assert factory.backend.loads == 1


@pytest.mark.parametrize("profile", ["Kokoro 82M", "Ming 3 Tiny", "unknown"])
async def test_request_profile_mismatch_never_constructs_backend(profile):
    factory = SyntheticFactory()
    async with client_for(factory) as client:
        response = await synthesize(client, profile=profile)
    assert response.status_code == 503
    assert factory.calls == []


async def test_injected_backend_stays_lazy_and_loads_once_without_optional_imports(
    monkeypatch, tmp_path
):
    original_import = builtins.__import__

    def import_without_runtime(name, *args, **kwargs):
        if name.split(".", 1)[0] in {
            "numpy",
            "kokoro",
            "torch",
            "onnxruntime",
            "supertonic",
            "faster_whisper",
            "vibevoice",
            "transformers",
            "diffusers",
        }:
            raise AssertionError(f"Optional runtime imported: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_runtime)
    factory = SyntheticFactory()
    async with client_for(factory, tts_model_dir=tmp_path, voice="F2") as client:
        assert (await client.get("/health")).json()["loaded"] == []
        assert factory.calls == []
        assert len(audio_frames(await synthesize(client))) == 1
        assert len(audio_frames(await synthesize(client, "Another public fixture."))) == 1
        health = (await client.get("/health")).json()
    assert factory.calls == [(PROFILE, tmp_path, "F2")]
    assert factory.backend.loads == 1
    assert factory.backend.inputs == ["Public synthetic speech.", "Another public fixture."]
    assert health["loaded"] == ["tts"]
    assert health["assets_state"] == "verified"


async def test_backend_load_failure_is_sanitized_http_error_before_stream_headers():
    factory = SyntheticFactory(SyntheticBackend(load_error=RuntimeError("synthetic private path")))
    async with client_for(factory) as client:
        response = await synthesize(client)
        health = (await client.get("/health")).json()
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "detail": "Selected voice backend is unavailable or returned invalid audio."
    }
    assert "synthetic private path" not in response.text
    assert "tts" not in health["loaded"]
    assert factory.backend.inputs == []


async def test_factory_failure_is_sanitized_before_stream_headers():
    def unavailable(*args):
        raise ImportError("synthetic optional runtime missing")

    async with client_for(unavailable) as client:
        response = await synthesize(client)
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert "synthetic optional runtime missing" not in response.text


async def test_first_inference_failure_is_sanitized_before_stream_headers():
    class BrokenBackend(SyntheticBackend):
        def synthesize(self, text):
            raise RuntimeError("synthetic private inference details")
            yield  # Keep the backend's lazy iterator protocol.

    async with client_for(SyntheticFactory(BrokenBackend())) as client:
        response = await synthesize(client)
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Selected voice backend is unavailable or returned invalid audio."
    }
    assert "synthetic private inference details" not in response.text


async def test_pcm_frames_preserve_sample_rate_order_and_clipped_little_endian_values():
    samples = [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0] + [0.25] * 10000
    factory = SyntheticFactory(
        SyntheticBackend([AudioWaveform(samples, 8000), AudioWaveform([[0.5] * 3], 8000)])
    )
    async with client_for(factory) as client:
        response = await synthesize(client)
    assert response.headers["content-type"].startswith("application/x-ndjson")
    frames = audio_frames(response)
    assert [frame["chunk_index"] for frame in frames] == list(range(len(frames)))
    assert [frame["sample_rate"] for frame in frames] == [8000, 8000, 8000]
    assert [frame["samples"] for frame in frames] == [8000, len(samples) - 8000, 3]
    decoded = []
    for frame in frames:
        raw = base64.b64decode(frame["audio"], validate=True)
        assert len(raw) == frame["samples"] * 2
        assert 0 < frame["samples"] <= frame["sample_rate"]
        assert frame["type"] == "audio"
        assert frame["provider"] == PROFILE
        assert frame["device"] == "cpu"
        assert frame["synthesis_mode"] == "buffered"
        assert frame["elapsed_seconds"] >= 0
        decoded.extend(struct.unpack("<" + "h" * frame["samples"], raw))
    assert decoded[:7] == [-32767, -32767, -16383, 0, 16383, 32767, 32767]
    assert len(decoded) == len(samples) + 3


@pytest.mark.parametrize("rate", [16000, 44100, 96000])
async def test_frames_report_actual_backend_rate(rate):
    factory = SyntheticFactory(SyntheticBackend([AudioWaveform([[0.5] * 8], rate)]))
    async with client_for(factory) as client:
        frames = audio_frames(await synthesize(client))
    assert len(frames) == 1
    assert frames[0]["sample_rate"] == rate
    assert frames[0]["samples"] == 8


@pytest.mark.parametrize(
    "waveform",
    [
        AudioWaveform([], 8000),
        AudioWaveform([0.1], 7999),
        AudioWaveform([0.1], 96001),
        AudioWaveform([0.1], 8000.5),
        AudioWaveform([0.1], True),
        AudioWaveform([[0.1], [0.2]], 8000),
        AudioWaveform([[[0.1]]], 8000),
        AudioWaveform(["synthetic"], 8000),
        AudioWaveform([float("nan")], 8000),
        AudioWaveform([float("inf")], 8000),
        AudioWaveform([float("-inf")], 8000),
        AudioWaveform([0.1] * 8000 + [float("nan")], 8000),
    ],
)
async def test_invalid_first_waveform_fails_before_stream_headers(waveform):
    factory = SyntheticFactory(SyntheticBackend([waveform]))
    async with client_for(factory) as client:
        response = await synthesize(client)
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "detail": "Selected voice backend is unavailable or returned invalid audio."
    }


async def test_empty_backend_output_fails_before_stream_headers():
    async with client_for(SyntheticFactory(SyntheticBackend([]))) as client:
        response = await synthesize(client)
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")


async def test_first_waveform_over_total_duration_bound_fails_before_stream_headers():
    waveform = AudioWaveform([0.0] * (8000 * 120 + 1), 8000)
    async with client_for(SyntheticFactory(SyntheticBackend([waveform]))) as client:
        response = await synthesize(client)
    assert response.status_code == 503


@pytest.mark.parametrize("failure", ["non-finite", "rate-change", "runtime"])
async def test_later_backend_failure_aborts_stream_with_sanitized_error(failure):
    class BrokenBackend(SyntheticBackend):
        closed = False

        def synthesize(self, text):
            try:
                yield AudioWaveform([0.5] * 8, 8000)
                if failure == "runtime":
                    raise RuntimeError("synthetic private runtime details")
                yield AudioWaveform(
                    [float("nan")] if failure == "non-finite" else [0.5],
                    16000 if failure == "rate-change" else 8000,
                )
            finally:
                self.closed = True

    backend = BrokenBackend()
    messages = []
    async with client_for(SyntheticFactory(backend), messages=messages) as client:
        with pytest.raises(TTSBackendError, match="Selected voice backend returned invalid audio"):
            await synthesize(client)
    assert backend.closed
    bodies = [message for message in messages if message["type"] == "http.response.body"]
    assert len(bodies) == 1
    assert bodies[0]["more_body"] is True
    assert json.loads(bodies[0]["body"])["chunk_index"] == 0
    assert "synthetic private runtime details" not in str(messages)


async def test_total_duration_bound_applies_across_waveforms_without_successful_stream_end():
    backend = SyntheticBackend(
        [AudioWaveform([0.0] * (8000 * 120), 8000), AudioWaveform([0.0], 8000)]
    )
    messages = []
    async with client_for(SyntheticFactory(backend), messages=messages) as client:
        with pytest.raises(TTSBackendError, match="Selected voice backend returned invalid audio"):
            await synthesize(client)
    bodies = [message for message in messages if message["type"] == "http.response.body"]
    frames = [json.loads(message["body"]) for message in bodies]
    assert len(frames) == 120
    assert [frame["chunk_index"] for frame in frames] == list(range(120))
    assert sum(frame["samples"] for frame in frames) == 8000 * 120
    assert all(message["more_body"] for message in bodies)


@pytest.mark.parametrize("phase, desired", [("unloading", True), ("off", False), ("ready", False)])
async def test_paused_resource_policy_blocks_factory(tmp_path, phase, desired):
    state = ResourceState(tmp_path / "resources-state.json")
    try:
        state.save(phase=phase, desired_converse=desired)
        factory = SyntheticFactory()
        async with client_for(factory, resource_policy=state.path) as client:
            response = await synthesize(client)
        assert response.status_code == 409
        assert factory.calls == []
    finally:
        state.close()


async def test_missing_resource_policy_blocks_factory(tmp_path):
    factory = SyntheticFactory()
    async with client_for(factory, resource_policy=tmp_path / "missing.json") as client:
        response = await synthesize(client)
    assert response.status_code == 503
    assert factory.calls == []


@pytest.mark.parametrize("pause_at", ["headers", "first-frame"])
async def test_policy_change_before_output_blocks_prefetched_and_pending_frames(tmp_path, pause_at):
    state = ResourceState(tmp_path / "resources-state.json")
    messages = []

    def pause_before_body(message):
        if (
            pause_at == "headers"
            and message["type"] == "http.response.start"
            and message["status"] == 200
        ) or (pause_at == "first-frame" and message["type"] == "http.response.body"):
            state.save(phase="off", desired_converse=False)

    try:
        state.save(phase="ready")
        async with client_for(
            SyntheticFactory(SyntheticBackend([AudioWaveform([0.25] * 16000, 8000)])),
            resource_policy=state.path,
            messages=messages,
            on_send=pause_before_body,
        ) as client:
            with pytest.raises(RuntimeError, match="response already started"):
                await synthesize(client)
        assert [
            message["status"] for message in messages if message["type"] == "http.response.start"
        ] == [200]
        bodies = [message for message in messages if message["type"] == "http.response.body"]
        assert len(bodies) == (0 if pause_at == "headers" else 1)
        assert all(message["more_body"] for message in bodies)
    finally:
        state.close()


@pytest.mark.parametrize("stage", ["load", "inference"])
async def test_policy_is_rechecked_after_blocking_backend_work(tmp_path, stage):
    state = ResourceState(tmp_path / "resources-state.json")
    waiting, release = threading.Event(), threading.Event()

    class BlockedBackend(SyntheticBackend):
        def load(self):
            if stage == "load":
                waiting.set()
                assert release.wait(3)
            super().load()

        def synthesize(self, text):
            self.inputs.append(text)
            if stage == "inference":
                waiting.set()
                assert release.wait(3)
            yield AudioWaveform([0.5] * 8, 8000)

    backend = BlockedBackend()
    try:
        state.save(phase="ready")
        async with client_for(SyntheticFactory(backend), resource_policy=state.path) as client:
            task = asyncio.create_task(synthesize(client))
            try:
                await entered(waiting)
                state.save(phase="off", desired_converse=False)
            finally:
                release.set()
            response = await asyncio.wait_for(task, 4)
        assert response.status_code == 409
        if stage == "load":
            assert backend.inputs == []
    finally:
        release.set()
        state.close()


async def test_waiting_request_rechecks_policy_before_loading_or_synthesizing(tmp_path):
    state = ResourceState(tmp_path / "resources-state.json")
    waiting, release = threading.Event(), threading.Event()

    class BlockedBackend(SyntheticBackend):
        def load(self):
            waiting.set()
            assert release.wait(3)
            super().load()

    backend = BlockedBackend()
    try:
        state.save(phase="ready")
        async with client_for(SyntheticFactory(backend), resource_policy=state.path) as client:
            first = asyncio.create_task(synthesize(client, "First public fixture."))
            second = None
            try:
                await entered(waiting)
                second = asyncio.create_task(synthesize(client, "Waiting public fixture."))
                await asyncio.sleep(0.02)
                state.save(phase="off", desired_converse=False)
            finally:
                release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), 4)
        assert [response.status_code for response in responses] == [409, 409]
        assert backend.loads == 1
        assert backend.inputs == []
    finally:
        release.set()
        state.close()


async def test_parallel_requests_serialize_backend_and_load_once():
    waiting, release = threading.Event(), threading.Event()
    active, maximum_active = 0, 0

    class SerializedBackend(SyntheticBackend):
        def synthesize(self, text):
            nonlocal active, maximum_active
            self.inputs.append(text)
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if text == "First public fixture.":
                    waiting.set()
                    assert release.wait(3)
                waveform = AudioWaveform([0.5] * 8, 8000)
            finally:
                active -= 1
            yield waveform

    factory = SyntheticFactory(SerializedBackend())
    async with client_for(factory) as client:
        first = asyncio.create_task(synthesize(client, "First public fixture."))
        second = None
        try:
            await entered(waiting)
            second = asyncio.create_task(synthesize(client, "Second public fixture."))
            await asyncio.sleep(0.02)
        finally:
            release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 4)
    assert all(response.status_code == 200 for response in responses)
    assert factory.backend.inputs == ["First public fixture.", "Second public fixture."]
    assert maximum_active == 1
    assert factory.backend.loads == 1
    assert len(factory.calls) == 1


@pytest.mark.parametrize("stage", ["load", "inference"])
async def test_cancelled_request_stops_before_further_inference_or_output(stage):
    waiting, release = threading.Event(), threading.Event()

    class CancelledBackend(SyntheticBackend):
        def load(self):
            if stage == "load":
                waiting.set()
                assert release.wait(3)
            super().load()

        def synthesize(self, text):
            self.inputs.append(text)
            if text == "Cancelled public fixture.":
                waiting.set()
                assert release.wait(3)
            yield AudioWaveform([0.5] * 8, 8000)

    backend = CancelledBackend()
    async with client_for(SyntheticFactory(backend)) as client:
        task = asyncio.create_task(synthesize(client, "Cancelled public fixture."))
        try:
            await entered(waiting)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        response = await asyncio.wait_for(synthesize(client, "Later public fixture."), 4)
    assert response.status_code == 200
    assert backend.loads == 1
    assert backend.inputs == (
        ["Later public fixture."]
        if stage == "load"
        else ["Cancelled public fixture.", "Later public fixture."]
    )


async def test_cancelled_waiting_request_never_enters_backend_after_lock_release():
    waiting, release = threading.Event(), threading.Event()

    class BlockedBackend(SyntheticBackend):
        def synthesize(self, text):
            self.inputs.append(text)
            if text == "First public fixture.":
                waiting.set()
                assert release.wait(3)
            yield AudioWaveform([0.5] * 8, 8000)

    backend = BlockedBackend()
    async with client_for(SyntheticFactory(backend)) as client:
        first = asyncio.create_task(synthesize(client, "First public fixture."))
        try:
            await entered(waiting)
            cancelled = asyncio.create_task(synthesize(client, "Cancelled waiting fixture."))
            await asyncio.sleep(0.02)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
        finally:
            release.set()
        assert (await asyncio.wait_for(first, 4)).status_code == 200
        assert (await synthesize(client, "Later public fixture.")).status_code == 200
    assert backend.inputs == ["First public fixture.", "Later public fixture."]


async def test_cancelled_stream_emits_no_late_audio_or_further_waveform_request():
    waiting, release = threading.Event(), threading.Event()
    messages, resumed_after_second, closed = [], [], []

    class BlockedBackend(SyntheticBackend):
        def synthesize(self, text):
            self.inputs.append(text)
            try:
                yield AudioWaveform([0.5] * 8, 8000)
                if text == "Cancelled streaming fixture.":
                    waiting.set()
                    assert release.wait(3)
                    yield AudioWaveform([0.5] * 8, 8000)
                    resumed_after_second.append(text)
                    yield AudioWaveform([0.5] * 8, 8000)
            finally:
                closed.append(text)

    backend = BlockedBackend()
    async with client_for(SyntheticFactory(backend), messages=messages) as client:
        task = asyncio.create_task(synthesize(client, "Cancelled streaming fixture."))
        try:
            await entered(waiting)
            bodies = [message for message in messages if message["type"] == "http.response.body"]
            assert len(bodies) == 1 and json.loads(bodies[0]["body"])["chunk_index"] == 0
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert (
            await asyncio.wait_for(synthesize(client, "Later public fixture."), 4)
        ).status_code == 200
    assert resumed_after_second == []
    assert "Cancelled streaming fixture." in closed
    audio_bodies = [
        message
        for message in messages
        if message["type"] == "http.response.body" and message["body"]
    ]
    assert len(audio_bodies) == 2  # One frame before cancellation, one from the later request.
    assert backend.inputs == ["Cancelled streaming fixture.", "Later public fixture."]
