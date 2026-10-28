"""Request-owned native producer lifetimes; synthetic callbacks, no model calls."""

import asyncio
import base64
import json
import threading

import httpx
import pytest

from packages.providers import voice_worker
from packages.providers.tts_backends import AudioWaveform, TTSBackendError
from packages.providers.voice_worker import create_app
from services.common.auth import Auth
from services.resources.state import ResourceState

AUTH = Auth({"owner": "o" * 32, "converse": "c" * 32})
HEADERS = {"Authorization": "Bearer " + "c" * 32}
PROFILE = "Supertonic 3"
WAVE = AudioWaveform([0.25] * 80, 8000)


async def entered(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 3), 4)


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


class NativeBackend:
    assets_state = "not_checked"

    def __init__(self, produce):
        self.callback = produce
        self.inputs = []
        self.loads = 0

    def load(self):
        self.loads += 1
        self.assets_state = "verified"

    def produce(self, text, emit, cancelled):
        self.inputs.append(text)
        self.callback(text, emit, cancelled)


def app_for(backend, **options):
    return create_app(AUTH, tts_profile=PROFILE, tts_factory=lambda *_: backend, **options)


async def health(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fixture"
    ) as client:
        return (await client.get("/health")).json()


class Request:
    """Actual ASGI receive/send lifecycle, including a controllable slow consumer.

    HTTPX ASGITransport buffers responses, so it cannot exercise disconnects
    while first/native waveform production is still running.
    """

    def __init__(
        self, app, text="Public synthetic fixture.", *, spec="2.4", slow=False, headers_barrier=None
    ):
        self.app = app
        self.incoming = asyncio.Queue()
        self.messages = []
        self.first = asyncio.Event()
        self.body_read = asyncio.Event()
        self.resume_send = asyncio.Event()
        if not slow:
            self.resume_send.set()
        self.receive_active = self.maximum_receive_active = 0
        self.headers_barrier = headers_barrier
        self.task = asyncio.create_task(self.run(text, spec))

    async def run(self, text, spec):
        payload = json.dumps({"input": text, "profile": PROFILE}).encode()
        await self.incoming.put({"type": "http.request", "body": payload, "more_body": False})

        async def receive():
            self.receive_active += 1
            self.maximum_receive_active = max(self.maximum_receive_active, self.receive_active)
            try:
                message = await self.incoming.get()
                if message["type"] == "http.request":
                    self.body_read.set()
                return message
            finally:
                self.receive_active -= 1

        async def send(message):
            self.messages.append(message.copy())
            if message["type"] == "http.response.start" and self.headers_barrier is not None:
                # Deliberately stall this ASGI consumer without yielding. The
                # producer's queue deadline must work independently of loop
                # scheduling and the async send timeout.
                assert self.headers_barrier.wait(3)
            if message["type"] == "http.response.body" and message.get("body"):
                self.first.set()
                await self.resume_send.wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/audio/speech",
            "raw_path": b"/v1/audio/speech",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"authorization", HEADERS["Authorization"].encode()),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 80),
        }
        await self.app(scope, receive, send)

    async def disconnect(self):
        await self.incoming.put({"type": "http.disconnect"})

    async def finish(self):
        await asyncio.wait_for(self.task, 4)
        assert self.maximum_receive_active == 1

    def frames(self):
        return [
            json.loads(message["body"])
            for message in self.messages
            if message["type"] == "http.response.body"
            and message.get("body")
            and message["body"].startswith(b'{"type": "audio"')
        ]


@pytest.mark.parametrize("spec", ["2.0", "2.4"])
@pytest.mark.parametrize("before_first", [True, False])
async def test_disconnect_reaches_native_stop_while_no_new_frames_and_confirms_thread_exit(
    spec, before_first
):
    started, stopped = threading.Event(), threading.Event()

    def produce(text, emit, cancelled):
        if text == "Later public fixture.":
            emit(WAVE)
            return
        started.set()
        try:
            if not before_first:
                emit(WAVE)
            assert cancelled.wait(3), "native stop signal never arrived"
        finally:
            stopped.set()

    backend = NativeBackend(produce)
    app = app_for(backend)
    request = Request(app, spec=spec)
    try:
        await entered(started)
        if not before_first:
            await asyncio.wait_for(request.first.wait(), 3)
        running = await health(app)
        assert running["active_producers"] == 1
        assert running["producer_stopped"] is False
        assert running["completed_producers"] == 0
        await request.disconnect()
        await request.finish()
        await entered(stopped)
        settled = await health(app)
        assert settled["active_producers"] == 0
        assert settled["producer_stopped"] is True
        assert settled["synthesis_quarantined"] is False
        assert settled["completed_producers"] == 1
        assert settled["last_producer_cancelled"] is True
        assert settled["last_producer_seconds"] >= 0
        assert len(request.frames()) == (0 if before_first else 1)
        later = Request(app, "Later public fixture.")
        await later.finish()
        assert len(later.frames()) == 1
        assert backend.loads == 1
    finally:
        if not request.task.done():
            request.task.cancel()
            await asyncio.gather(request.task, return_exceptions=True)


async def test_model_slot_is_owned_through_native_cleanup_after_first_frame():
    cleanup, release = threading.Event(), threading.Event()
    active = maximum = 0

    def produce(text, emit, cancelled):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            emit(WAVE)
            if text == "First public fixture.":
                assert cancelled.wait(3)
                cleanup.set()
                assert release.wait(3)
        finally:
            active -= 1

    backend = NativeBackend(produce)
    app = app_for(backend)
    first = Request(app, "First public fixture.")
    second = None
    try:
        await asyncio.wait_for(first.first.wait(), 3)
        second = Request(app, "Second public fixture.")
        await second.body_read.wait()
        await first.disconnect()
        await entered(cleanup)
        assert backend.inputs == ["First public fixture."]
        assert (await health(app))["active_producers"] == 1
        assert not second.first.is_set()
    finally:
        release.set()
        await first.finish()
        if second is not None:
            await second.finish()
    assert maximum == 1
    assert backend.inputs == ["First public fixture.", "Second public fixture."]


async def test_full_pcm_queue_applies_callback_backpressure_and_disconnect_unblocks_it():
    third_returned, fourth_entered, fourth_returned = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    stopped = threading.Event()

    def produce(text, emit, cancelled):
        try:
            emit(WAVE)
            emit(WAVE)
            emit(WAVE)
            third_returned.set()
            fourth_entered.set()
            emit(WAVE)
            fourth_returned.set()
        finally:
            stopped.set()

    app = app_for(NativeBackend(produce))
    request = Request(app, slow=True)
    try:
        await asyncio.wait_for(request.first.wait(), 3)
        await entered(third_returned)
        await entered(fourth_entered)
        assert not fourth_returned.is_set()
        assert (await health(app))["active_producers"] == 1
        await request.disconnect()
        await request.finish()
        await entered(stopped)
        assert not fourth_returned.is_set()
        assert len(request.frames()) == 1
        assert (await health(app))["producer_stopped"] is True
    finally:
        request.resume_send.set()
        if not request.task.done():
            await request.disconnect()
            await request.finish()


@pytest.mark.parametrize("before_first", [True, False])
async def test_policy_monitor_cancels_native_work_without_waiting_for_a_callback(
    tmp_path, before_first
):
    state = ResourceState(tmp_path / "policy.json")
    state.save(phase="ready")
    started, stopped = threading.Event(), threading.Event()

    def produce(text, emit, cancelled):
        started.set()
        try:
            if not before_first:
                emit(WAVE)
            assert cancelled.wait(3)
        finally:
            stopped.set()

    app = app_for(NativeBackend(produce), resource_policy=state.path)
    request = Request(app)
    try:
        await entered(started)
        if not before_first:
            await request.first.wait()
        state.save(phase="off", desired_converse=False)
        if before_first:
            await request.finish()
            assert (
                next(
                    message["status"]
                    for message in request.messages
                    if message["type"] == "http.response.start"
                )
                == 409
            )
        else:
            with pytest.raises(RuntimeError, match="response already started"):
                await request.finish()
        await entered(stopped)
        assert len(request.frames()) == (0 if before_first else 1)
        settled = await health(app)
        assert settled["producer_stopped"] is True
        assert settled["last_producer_cancelled"] is True
        assert settled["synthesis_quarantined"] is False
    finally:
        state.close()


@pytest.mark.parametrize("first", [True, False])
async def test_waveform_deadline_cancels_a_cooperative_native_producer(first):
    stopped = threading.Event()

    def produce(text, emit, cancelled):
        try:
            if not first:
                emit(WAVE)
            assert cancelled.wait(3)
        finally:
            stopped.set()

    app = app_for(NativeBackend(produce), first_frame_timeout=0.08, idle_frame_timeout=0.08)
    request = Request(app)
    if first:
        await request.finish()
        assert (
            next(
                message["status"]
                for message in request.messages
                if message["type"] == "http.response.start"
            )
            == 503
        )
    else:
        with pytest.raises(TTSBackendError, match="deadline exceeded"):
            await request.finish()
    await entered(stopped)
    settled = await health(app)
    assert settled["producer_stopped"] is True
    assert settled["synthesis_quarantined"] is False
    assert settled["last_producer_cancelled"] is True


async def test_unconfirmed_producer_exit_quarantines_all_generation_until_process_restart():
    started, release = threading.Event(), threading.Event()

    def produce(text, emit, cancelled):
        started.set()
        assert release.wait(3), "test must release the deliberately uncooperative fixture"

    backend = NativeBackend(produce)
    app = app_for(backend, first_frame_timeout=0.06, producer_close_timeout=0.06)
    request = Request(app)
    try:
        await entered(started)
        await request.finish()
        stuck = await health(app)
        assert stuck["synthesis_quarantined"] is True
        assert stuck["active_producers"] == 1
        assert stuck["producer_stopped"] is False
        assert stuck["completed_producers"] == 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://fixture", headers=HEADERS
        ) as client:
            response = await client.post(
                "/v1/audio/speech", json={"input": "Later fixture.", "profile": PROFILE}
            )
            assert response.status_code == 503
            stt = await client.post(
                "/v1/audio/transcriptions", files={"file": ("synthetic.wav", b"synthetic")}
            )
            assert stt.status_code == 503
        assert backend.inputs == ["Public synthetic fixture."]
    finally:
        release.set()
    await eventually(
        lambda: (
            not any(
                thread.name == "kinaiya-tts-producer" and thread.is_alive()
                for thread in threading.enumerate()
            )
        )
    )
    settled = await health(app)
    assert settled["producer_stopped"] is True
    assert settled["completed_producers"] == 1
    assert settled["synthesis_quarantined"] is True


async def test_shutdown_signals_and_joins_native_producer_without_leaking_threads():
    started, stopped = threading.Event(), threading.Event()

    def produce(text, emit, cancelled):
        started.set()
        try:
            assert cancelled.wait(3)
        finally:
            stopped.set()

    app = app_for(NativeBackend(produce))
    request = None
    async with app.router.lifespan_context(app):
        request = Request(app)
        await entered(started)
    await entered(stopped)
    await request.finish()
    settled = await health(app)
    assert settled["producer_stopped"] is True
    assert settled["active_producers"] == 0
    assert settled["synthesis_quarantined"] is False
    assert settled["completed_producers"] == 1


@pytest.mark.parametrize("failure", [RuntimeError, SystemExit])
async def test_native_callback_failure_after_audio_aborts_without_a_successful_eof(failure):
    def produce(text, emit, cancelled):
        emit(WAVE)
        raise failure("synthetic private native failure")

    app = app_for(NativeBackend(produce))
    request = Request(app)
    with pytest.raises(TTSBackendError, match="Selected voice backend returned invalid audio"):
        await request.finish()
    assert len(request.frames()) == 1
    assert not any(
        message["type"] == "http.response.body" and not message.get("more_body", False)
        for message in request.messages
    )
    assert "synthetic private native failure" not in str(request.messages)
    assert (await health(app))["producer_stopped"] is True


async def test_slow_consumer_deadline_cancels_native_producer_without_pending_callbacks():
    stopped = threading.Event()

    def produce(text, emit, cancelled):
        try:
            emit(WAVE)
            assert cancelled.wait(3)
        finally:
            stopped.set()

    app = app_for(NativeBackend(produce), idle_frame_timeout=0.08)
    request = Request(app, slow=True)
    with pytest.raises(TTSBackendError, match="output deadline exceeded"):
        await request.finish()
    await entered(stopped)
    settled = await health(app)
    assert settled["producer_stopped"] is True
    assert settled["last_producer_cancelled"] is True
    assert settled["synthesis_quarantined"] is False
    assert len(request.frames()) == 1


async def test_full_queue_deadline_stops_production_without_any_further_consumption():
    expired, stopped = threading.Event(), threading.Event()

    def produce(text, emit, cancelled):
        try:
            for _ in range(20):
                try:
                    emit(WAVE)
                except TTSBackendError as exc:
                    assert "queue deadline" in str(exc)
                    expired.set()
                    raise
        finally:
            stopped.set()

    app = app_for(NativeBackend(produce), idle_frame_timeout=0.08)
    request = Request(app, headers_barrier=expired)
    with pytest.raises(TTSBackendError, match="Selected voice backend returned invalid audio"):
        await request.finish()
    assert expired.is_set()
    await entered(stopped)
    settled = await health(app)
    assert settled["producer_stopped"] is True
    assert settled["synthesis_quarantined"] is False


async def test_waiting_admission_deadline_does_not_start_or_release_another_producer():
    stopped = threading.Event()

    def produce(text, emit, cancelled):
        try:
            emit(WAVE)
            assert cancelled.wait(3)
        finally:
            stopped.set()

    backend = NativeBackend(produce)
    app = app_for(backend, first_frame_timeout=0.1)
    first = Request(app, "First public fixture.")
    second = None
    try:
        await first.first.wait()
        second = Request(app, "Waiting public fixture.")
        await second.finish()
        status = next(
            message["status"]
            for message in second.messages
            if message["type"] == "http.response.start"
        )
        assert status == 503
        assert backend.inputs == ["First public fixture."]
        running = await health(app)
        assert running["active_producers"] == 1
        assert running["synthesis_quarantined"] is False
    finally:
        await first.disconnect()
        await first.finish()
        await entered(stopped)


async def test_callback_transfers_owned_pcm_even_when_native_waveform_buffer_is_reused():
    samples = [0.25] * 80

    def produce(text, emit, cancelled):
        emit(AudioWaveform(samples, 8000))
        samples[:] = [-0.75] * 80

    app = app_for(NativeBackend(produce))
    request = Request(app)
    await request.finish()
    pcm = base64.b64decode(request.frames()[0]["audio"], validate=True)
    assert pcm == int(0.25 * 32767).to_bytes(2, "little", signed=True) * 80
    assert samples[0] == -0.75
    assert (await health(app))["last_producer_cancelled"] is False


async def test_final_frame_queued_between_empty_check_and_producer_exit_is_not_lost(monkeypatch):
    empty_seen, returned = threading.Event(), threading.Event()
    producer_thread = []
    original_queue = voice_worker.queue.Queue

    class ExitRaceQueue(original_queue):
        raced = False

        def get_nowait(self):
            try:
                return super().get_nowait()
            except voice_worker.queue.Empty:
                if not self.raced:
                    self.raced = True
                    empty_seen.set()
                    assert returned.wait(3)
                    producer_thread[0].join(timeout=3)
                    assert not producer_thread[0].is_alive()
                raise

    def produce(text, emit, cancelled):
        producer_thread.append(threading.current_thread())
        assert empty_seen.wait(3)
        emit(WAVE)
        returned.set()

    monkeypatch.setattr(voice_worker.queue, "Queue", ExitRaceQueue)
    app = app_for(NativeBackend(produce))
    request = Request(app)
    await request.finish()
    assert len(request.frames()) == 1
    assert request.frames()[0]["samples"] == 80
    assert (await health(app))["completed_producers"] == 1


async def test_ready_policy_revision_change_revokes_existing_native_admission(tmp_path):
    state = ResourceState(tmp_path / "policy.json")
    state.save(phase="ready")
    started, stopped = threading.Event(), threading.Event()

    def produce(text, emit, cancelled):
        started.set()
        try:
            assert cancelled.wait(3)
        finally:
            stopped.set()

    app = app_for(NativeBackend(produce), resource_policy=state.path)
    request = Request(app)
    try:
        await entered(started)
        # A ready -> off -> ready transition between polls must not revive a
        # request admitted under the earlier resource generation.
        state.save(phase="off", desired_converse=False)
        state.save(phase="ready", desired_converse=True)
        await request.finish()
        status = next(
            message["status"]
            for message in request.messages
            if message["type"] == "http.response.start"
        )
        assert status == 409
        await entered(stopped)
        assert not request.frames()
        assert (await health(app))["last_producer_cancelled"] is True
    finally:
        state.close()


async def test_vibevoice_profile_accepts_injected_native_producer_and_fails_closed_on_missing_assets():
    calls = []

    def factory(profile, model_dir, voice):
        calls.append((profile, voice))
        return NativeBackend(lambda text, emit, cancelled: emit(WAVE))

    app = create_app(AUTH, tts_profile="VibeVoice Realtime 0.5B", tts_factory=factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fixture", headers=HEADERS
    ) as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"input": "Public fixture.", "profile": "VibeVoice Realtime 0.5B"},
        )
        assert response.status_code == 200
        frame = json.loads(response.text)
        assert frame["provider"] == "VibeVoice Realtime 0.5B"
        assert frame["synthesis_mode"] == "native_streaming"
        assert len(base64.b64decode(frame["audio"], validate=True)) == frame["samples"] * 2
    assert calls == [("VibeVoice Realtime 0.5B", "en-Carter_man")]

    class MissingAssets(NativeBackend):
        def load(self):
            raise FileNotFoundError("synthetic private model path")

    app = create_app(
        AUTH, tts_profile="VibeVoice Realtime 0.5B", tts_factory=lambda *_: MissingAssets(None)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fixture", headers=HEADERS
    ) as client:
        response = await client.post(
            "/v1/audio/speech",
            json={"input": "Public fixture.", "profile": "VibeVoice Realtime 0.5B"},
        )
        assert response.status_code == 503
        assert "synthetic private model path" not in response.text


@pytest.mark.parametrize(
    "name", ["first_frame_timeout", "idle_frame_timeout", "producer_close_timeout"]
)
@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), float("nan"), "1"])
def test_internal_deadlines_are_finite_positive_numbers(name, value):
    with pytest.raises(ValueError, match="finite positive"):
        app_for(NativeBackend(None), **{name: value})
