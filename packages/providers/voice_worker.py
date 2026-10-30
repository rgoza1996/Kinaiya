"""Optional local voice service. Launch with an owner-provisioned voice Python.

Models load lazily in this process; Agent and session services never import them.
Audio is transient by default. Client playback, not this server, measures audibility.
"""

import asyncio
import base64
import io
import json
import math
import os
import queue
import threading
import time
from contextlib import asynccontextmanager
from fractions import Fraction
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from packages.providers.speech import RETAINED_TTS
from packages.providers.tts_backends import (
    AudioWaveform,
    TTSBackendError,
    artifact_revision,
    create_tts_backend,
    input_limit,
    pcm_frames,
    produce_synthesis,
    synthesis_mode,
    validate_tts_device,
    validate_voice,
)
from services.common.auth import Auth, load_or_create_tokens


def create_app(
    auth: Auth,
    *,
    stt_model="tiny.en",
    voice=None,
    resource_policy=None,
    tts_profile="Kokoro 82M",
    tts_model_dir=None,
    tts_factory=None,
    device="cpu",
    stt_backend="faster-whisper",
    stt_checkpoint=None,
    first_frame_timeout=300.0,
    idle_frame_timeout=60.0,
    producer_close_timeout=5.0,
):
    for value in (first_frame_timeout, idle_frame_timeout, producer_close_timeout):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("Voice deadlines must be finite positive seconds")
    voice = validate_voice(tts_profile, voice)
    device = validate_tts_device(tts_profile, device)
    if not isinstance(stt_backend, str) or stt_backend not in {"faster-whisper", "openai-whisper"}:
        raise ValueError("Unsupported configured STT backend")
    if device == "cuda":
        if stt_backend != "openai-whisper" or stt_checkpoint is None:
            raise ValueError("GPU voice requires its configured OpenAI Whisper checkpoint")
        if stt_model not in {"tiny", "tiny.en"}:
            raise ValueError("GPU voice requires a supported pinned Whisper model")
    elif stt_backend != "faster-whisper" or stt_checkpoint is not None:
        raise ValueError("The CPU voice profile requires its existing faster-whisper backend")
    if stt_checkpoint is not None:
        if not isinstance(stt_checkpoint, (str, Path)):
            raise ValueError("STT checkpoint must be an absolute owner input")
        checkpoint_text = str(stt_checkpoint)
        if (
            not Path(checkpoint_text).is_absolute()
            or len(checkpoint_text) > 4096
            or any(char in checkpoint_text for char in "\0\r\n")
        ):
            raise ValueError("STT checkpoint must be an absolute owner input")
        stt_checkpoint = Path(checkpoint_text)
    mode = synthesis_mode(tts_profile)
    if tts_factory is not None:
        factory = tts_factory
    elif device == "cpu":
        factory = create_tts_backend
    else:

        def factory(profile, model_dir, selected_voice):
            return create_tts_backend(profile, model_dir, selected_voice, device=device)

    app = FastAPI(title="Kinaiya local voice worker")
    models = {}
    selected_backend = None
    selected_stt_backend = None
    lock = threading.Lock()
    admission = asyncio.Lock()
    sessions = set()
    producers = set()
    quarantined = threading.Event()
    shutting_down = threading.Event()
    completed_producers = 0
    last_producer_seconds = None
    last_producer_cancelled = None
    poll_seconds = 0.01

    def reap_producers():
        nonlocal completed_producers, last_producer_seconds, last_producer_cancelled
        for stream in tuple(producers):
            # A finally block/event in the producer is not proof the thread exited.
            if not stream.thread.is_alive():
                stream.thread.join(timeout=0)
                producers.remove(stream)
                completed_producers += 1
                last_producer_seconds = stream.producer_seconds
                last_producer_cancelled = stream.producer_cancelled

    def allow_inference():
        if quarantined.is_set() or shutting_down.is_set():
            raise HTTPException(503, "Voice producer unavailable; worker restart required")
        if resource_policy is None:
            return None
        from services.resources.state import read_record

        try:
            state = read_record(Path(resource_policy))
        except Exception as exc:
            raise HTTPException(503, "Resource policy unavailable; voice inference paused") from exc
        if state.phase != "ready" or not state.desired_converse:
            raise HTTPException(409, "Voice inference paused for resource coordination")
        return state.revision

    @app.get("/health")
    async def health():
        reap_producers()
        active = sum(stream.thread.is_alive() for stream in producers)
        return {
            "service": "local-voice",
            "device": device,
            "pid": os.getpid(),
            "loaded": list(models),
            "tts_profile": tts_profile,
            "tts_voice": voice,
            "stt_backend": stt_backend,
            "stt_device": (
                getattr(models["stt"], "loaded_device", "cpu") if "stt" in models else None
            ),
            "tts_device": (
                getattr(models["tts"], "loaded_device", "cpu") if "tts" in models else None
            ),
            "synthesis_mode": mode,
            "tts_artifact_revision": artifact_revision(tts_profile),
            "assets_state": getattr(selected_backend, "assets_state", "not_checked"),
            "retained_candidates": list(RETAINED_TTS),
            "other_profiles": "not_configured",
            "synthesis_active": bool(sessions),
            "active_producers": active,
            "producer_stopped": active == 0,
            "synthesis_quarantined": quarantined.is_set(),
            "completed_producers": completed_producers,
            "last_producer_seconds": last_producer_seconds,
            "last_producer_cancelled": last_producer_cancelled,
        }

    def recognize(data):
        nonlocal selected_stt_backend
        allow_inference()
        if stt_backend == "openai-whisper":
            with lock:
                allow_inference()
                from packages.providers.openai_whisper_backend import (
                    OpenAIWhisperBackend,
                    STTInputError,
                )

                try:
                    if selected_stt_backend is None:
                        selected_stt_backend = OpenAIWhisperBackend(
                            stt_checkpoint, model=stt_model, device=device
                        )
                    allow_inference()
                    result = selected_stt_backend.transcribe(data)
                except STTInputError:
                    raise HTTPException(400, "Unsupported or invalid voice audio input") from None
                except HTTPException:
                    raise
                except Exception:
                    raise HTTPException(
                        503, "The configured GPU STT backend is unavailable"
                    ) from None
                finally:
                    if getattr(selected_stt_backend, "loaded_device", None) == "cuda:0":
                        models["stt"] = selected_stt_backend
                if (
                    getattr(selected_stt_backend, "loaded_device", None) != "cuda:0"
                    or not isinstance(result, dict)
                    or result.get("device") != "cuda:0"
                ):
                    raise HTTPException(503, "The configured STT model is not on its required GPU")
                allow_inference()
                return result
        from faster_whisper import WhisperModel

        with lock:
            allow_inference()
            if "stt" not in models:
                models["stt"] = WhisperModel(
                    stt_model,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                    local_files_only=True,
                )
            allow_inference()
            # A named temporary file survives SIGKILL. Decode from memory so
            # forced resource unload cannot leave an audio file on disk.
            segments, info = models["stt"].transcribe(io.BytesIO(data), beam_size=3, language="en")
            text = " ".join(s.text.strip() for s in segments).strip()
            allow_inference()
            return {
                "text": text,
                "language": info.language,
                "final": True,
                "provider": "faster-whisper",
                "model": stt_model,
            }

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(auth.require("converse", "owner"))])
    async def transcribe(file: Annotated[UploadFile, File()]):
        allow_inference()
        data = await file.read(10_000_001)
        if len(data) > 10_000_000:
            raise HTTPException(413, "Audio exceeds temporary buffer limit")
        start = time.perf_counter()
        result = await asyncio.to_thread(recognize, data)
        return {**result, "stt_seconds": time.perf_counter() - start, "retained_audio": False}

    class Synthesis(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        input: str = Field(min_length=1, max_length=16000)
        profile: str = "Kokoro 82M"

    class SynthesisCancelled(RuntimeError):
        pass

    class SynthesisStream:
        def __init__(self, text, request):
            self.text = text
            self.request = request
            self.cancelled = threading.Event()
            self.frames = queue.Queue(maxsize=2)
            self.thread = None
            self.admission = None
            self.owns_slot = False
            self.monitor = None
            self.failure = None
            self.monitor_failure = None
            self.samples = 0
            self.rate = None
            self.index = 0
            self.start_time = time.perf_counter()
            self.load_seconds = 0.0
            self.first_waveform_seconds = None
            self.synthesis_started = None
            self.producer_seconds = None
            self.producer_cancelled = None
            self.policy_revision = allow_inference()
            self.closed = False
            self.close_lock = asyncio.Lock()

        def check(self):
            if self.cancelled.is_set():
                if self.monitor_failure is not None:
                    raise self.monitor_failure
                raise SynthesisCancelled("Voice request cancelled")
            revision = allow_inference()
            if revision != self.policy_revision:
                raise HTTPException(409, "Voice resource admission changed")
            if device == "cuda" and "tts" in models and models["tts"].loaded_device != "cuda:0":
                raise TTSBackendError("The configured voice model is not on its required GPU")

        async def watch(self):
            # FastAPI has already consumed the JSON body. This is the sole
            # ASGI receive consumer until cleanup, including after headers.
            try:
                while True:
                    try:
                        message = await asyncio.wait_for(self.request.receive(), poll_seconds)
                    except TimeoutError:
                        message = None
                    if message is not None and message["type"] == "http.disconnect":
                        raise HTTPException(499, "Voice request disconnected")
                    self.check()
            except HTTPException as exc:
                self.monitor_failure = exc
                self.cancelled.set()
            except SynthesisCancelled:
                pass
            except Exception:
                self.monitor_failure = HTTPException(503, "Voice request monitor unavailable")
                self.cancelled.set()

        def emit(self, waveform):
            self.check()
            if not isinstance(waveform, AudioWaveform):
                raise TTSBackendError("Invalid voice waveform")
            if self.rate is not None and waveform.sample_rate != self.rate:
                raise TTSBackendError("Voice sample rate changed during synthesis")
            remaining = (
                120 if self.rate is None else Fraction(120 * self.rate - self.samples, self.rate)
            )
            if self.first_waveform_seconds is None:
                self.first_waveform_seconds = time.perf_counter() - self.synthesis_started
            frames, count, rate = pcm_frames(waveform, remaining_seconds=remaining)
            self.samples += count
            self.rate = rate
            for pcm in frames:
                deadline = time.perf_counter() + idle_frame_timeout
                while True:
                    self.check()
                    if time.perf_counter() >= deadline:
                        raise TTSBackendError("Voice queue deadline exceeded")
                    try:
                        # pcm_frames owns immutable PCM bytes; the native
                        # producer cannot mutate an already queued waveform.
                        self.frames.put((pcm, rate), timeout=poll_seconds)
                        break
                    except queue.Full:
                        pass
            self.check()

        def produce(self):
            nonlocal selected_backend
            started = time.perf_counter()
            try:
                while not lock.acquire(timeout=poll_seconds):
                    self.check()
                try:
                    self.check()
                    if "tts" not in models:
                        loading = time.perf_counter()
                        backend = factory(tts_profile, tts_model_dir, voice)
                        selected_backend = backend
                        self.check()
                        backend.load()
                        if device == "cuda" and getattr(backend, "loaded_device", None) != "cuda:0":
                            raise TTSBackendError(
                                "The configured voice model is not on its required GPU"
                            )
                        self.load_seconds = time.perf_counter() - loading
                        models["tts"] = backend
                        self.check()
                    self.check()
                    self.synthesis_started = time.perf_counter()
                    # The producer retains the STT/model lock through native
                    # execution, callback backpressure, and adapter cleanup.
                    produce_synthesis(models["tts"], self.text, self.emit, self.cancelled)
                    self.check()
                finally:
                    lock.release()
            except (HTTPException, SynthesisCancelled) as exc:
                self.failure = exc
            except BaseException:
                self.failure = TTSBackendError("Selected voice backend returned invalid audio")
            finally:
                self.producer_seconds = time.perf_counter() - started
                self.producer_cancelled = self.cancelled.is_set()

        async def start(self):
            self.monitor = asyncio.create_task(self.watch())
            self.admission = asyncio.create_task(admission.acquire())
            deadline = self.start_time + first_frame_timeout
            while not self.admission.done():
                self.check()
                if time.perf_counter() >= deadline:
                    raise TTSBackendError("Voice first waveform deadline exceeded")
                await asyncio.wait({self.admission}, timeout=poll_seconds)
            self.owns_slot = self.admission.result()
            self.check()
            if time.perf_counter() >= deadline:
                raise TTSBackendError("Voice first waveform deadline exceeded")
            self.thread = threading.Thread(
                target=self.produce, name="kinaiya-tts-producer", daemon=True
            )
            self.thread.start()
            producers.add(self)

        async def next_frame(self, *, first=False):
            deadline = (
                self.start_time + first_frame_timeout
                if first
                else time.perf_counter() + idle_frame_timeout
            )
            while True:
                self.check()
                if time.perf_counter() >= deadline:
                    raise TTSBackendError("Voice waveform deadline exceeded")
                try:
                    pcm, rate = self.frames.get_nowait()
                except queue.Empty:
                    if self.thread.is_alive():
                        await asyncio.sleep(poll_seconds)
                        continue
                    reap_producers()
                    # The producer may have queued its final frame between
                    # the empty check and confirmed thread exit. Drain after
                    # that boundary before interpreting EOF or its failure.
                    try:
                        pcm, rate = self.frames.get_nowait()
                    except queue.Empty:
                        if self.failure is not None:
                            raise self.failure from None
                        return None
                self.check()
                frame = {
                    "type": "audio",
                    "audio": base64.b64encode(pcm).decode(),
                    "sample_rate": rate,
                    "samples": len(pcm) // 2,
                    "chunk_index": self.index,
                    "elapsed_seconds": time.perf_counter() - self.start_time,
                    "provider": tts_profile,
                    "device": (
                        getattr(models["tts"], "loaded_device", "cpu")
                        if device == "cpu"
                        else models["tts"].loaded_device
                    ),
                    "synthesis_mode": mode,
                    "load_seconds": self.load_seconds,
                    "first_waveform_seconds": self.first_waveform_seconds,
                }
                self.index += 1
                return json.dumps(frame) + "\n"

        async def close(self):
            async with self.close_lock:
                await self._close()

        async def _close(self):
            if self.closed:
                return
            self.cancelled.set()
            if self.monitor is not None:
                self.monitor.cancel()
                await asyncio.gather(self.monitor, return_exceptions=True)
            if self.admission is not None and not self.admission.done():
                self.admission.cancel()
                await asyncio.gather(self.admission, return_exceptions=True)
            # An admission task may have acquired immediately before cancellation.
            if (
                self.admission is not None
                and not self.admission.cancelled()
                and self.admission.done()
            ):
                self.owns_slot = self.admission.result()
            deadline = time.perf_counter() + producer_close_timeout
            while (
                self.thread is not None
                and self.thread.is_alive()
                and time.perf_counter() < deadline
            ):
                await asyncio.sleep(poll_seconds)
            if self.thread is not None and self.thread.is_alive():
                quarantined.set()
                # Keep admission and the real model lock owned. Only restarting
                # the process can establish a usable model after this failure.
            else:
                reap_producers()
                if self.owns_slot:
                    admission.release()
                    self.owns_slot = False
                self.text = None
            self.closed = True
            sessions.discard(self)

    class SynthesisResponse(StreamingResponse):
        def __init__(self, stream, content):
            super().__init__(content, media_type="application/x-ndjson")
            self.stream = stream

        async def __call__(self, scope, receive, send):
            # Do not run StreamingResponse's second disconnect listener. The
            # request monitor owns receive before and after the first headers.
            async def bounded_send(message):
                try:
                    await asyncio.wait_for(send(message), idle_frame_timeout)
                except TimeoutError:
                    raise TTSBackendError("Voice output deadline exceeded") from None

            sender = asyncio.create_task(self.stream_response(bounded_send))
            try:
                done, _ = await asyncio.wait(
                    {sender, self.stream.monitor}, return_when=asyncio.FIRST_COMPLETED
                )
                if sender in done:
                    await sender
                elif self.stream.monitor_failure is not None:
                    if self.stream.monitor_failure.status_code != 499:
                        raise self.stream.monitor_failure
            finally:
                self.stream.cancelled.set()
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                await asyncio.shield(self.stream.close())

    @app.post("/v1/audio/speech", dependencies=[Depends(auth.require("converse", "owner"))])
    async def speech(request: Synthesis, connection: Request):
        allow_inference()
        if request.profile != tts_profile:
            raise HTTPException(503, "Selected retained profile needs its configured backend")
        if len(request.input) > input_limit(tts_profile):
            raise HTTPException(413, "Voice input exceeds the configured profile limit")
        stream = SynthesisStream(request.input, connection)
        sessions.add(stream)
        try:
            await stream.start()
            first = await stream.next_frame(first=True)
            stream.check()
            if first is None:
                raise TTSBackendError("Voice backend returned no audio")
        except asyncio.CancelledError:
            await asyncio.shield(stream.close())
            raise
        except HTTPException:
            await asyncio.shield(stream.close())
            raise
        except Exception:
            await asyncio.shield(stream.close())
            raise HTTPException(
                503, "Selected voice backend is unavailable or returned invalid audio."
            ) from None

        async def generate():
            stream.check()
            yield first
            while True:
                frame = await stream.next_frame()
                if frame is None:
                    return
                stream.check()
                yield frame

        return SynthesisResponse(stream, generate())

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            shutting_down.set()
            remaining = tuple(sessions | producers)
            for stream in remaining:
                stream.cancelled.set()
            await asyncio.gather(*(stream.close() for stream in remaining))

    app.router.lifespan_context = lifespan

    return app


def main():
    import argparse

    import uvicorn

    # Installation is a separate owner action; voice requests never download weights.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--resource-policy", type=Path)
    parser.add_argument("--stt-model", default=os.environ.get("KINAIYA_STT_MODEL", "tiny.en"))
    parser.add_argument("--tts-profile", default="Kokoro 82M")
    parser.add_argument("--tts-model-dir", type=Path)
    parser.add_argument("--tts-voice")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument(
        "--stt-backend", choices=("faster-whisper", "openai-whisper"), default="faster-whisper"
    )
    parser.add_argument("--stt-checkpoint", type=Path)
    args = parser.parse_args()
    app = create_app(
        Auth(load_or_create_tokens(args.data_dir)),
        stt_model=args.stt_model,
        resource_policy=args.resource_policy,
        tts_profile=args.tts_profile,
        tts_model_dir=args.tts_model_dir,
        voice=args.tts_voice,
        device=args.device,
        stt_backend=args.stt_backend,
        stt_checkpoint=args.stt_checkpoint,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
