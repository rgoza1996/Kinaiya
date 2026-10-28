"""Optional local CPU voice service. Launch with a Python containing the voice extra.

Models load lazily in this process; Agent and session services never import them.
Audio is transient by default. Client playback, not this server, measures audibility.
"""

import asyncio
import base64
import io
import json
import os
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from packages.providers.speech import RETAINED_TTS
from packages.providers.tts_backends import (
    SUPERTONIC_REVISION,
    AudioWaveform,
    TTSBackendError,
    create_tts_backend,
    pcm_frames,
    synthesis_mode,
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
):
    voice = validate_voice(tts_profile, voice)
    mode = synthesis_mode(tts_profile)
    factory = tts_factory if tts_factory is not None else create_tts_backend
    app = FastAPI(title="Kinaiya local voice worker")
    models = {}
    selected_backend = None
    lock = threading.Lock()

    def allow_inference():
        if resource_policy is None:
            return
        from services.resources.state import read_record

        try:
            state = read_record(Path(resource_policy))
        except Exception as exc:
            raise HTTPException(503, "Resource policy unavailable; voice inference paused") from exc
        if state.phase != "ready" or not state.desired_converse:
            raise HTTPException(409, "Voice inference paused for resource coordination")

    @app.get("/health")
    async def health():
        return {
            "service": "local-voice",
            "device": "cpu",
            "pid": os.getpid(),
            "loaded": list(models),
            "tts_profile": tts_profile,
            "tts_voice": voice,
            "synthesis_mode": mode,
            "tts_artifact_revision": SUPERTONIC_REVISION if tts_profile == "Supertonic 3" else None,
            "assets_state": getattr(selected_backend, "assets_state", "not_checked"),
            "retained_candidates": list(RETAINED_TTS),
            "other_profiles": "not_configured",
        }

    def recognize(data):
        allow_inference()
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
        def __init__(self, text):
            self.text = text
            self.cancelled = threading.Event()
            self.iterator = None
            self.pending = []
            self.samples = 0
            self.rate = None
            self.index = 0
            self.start = time.perf_counter()
            self.load_seconds = 0.0
            self.first_waveform_seconds = None
            self.synthesis_started = None

        def check(self):
            if self.cancelled.is_set():
                raise SynthesisCancelled("Voice request cancelled")
            allow_inference()

        def close(self):
            self.cancelled.set()
            if lock.acquire(blocking=False):
                try:
                    if self.iterator is not None and hasattr(self.iterator, "close"):
                        self.iterator.close()
                finally:
                    lock.release()

        def next_frame(self):
            nonlocal selected_backend
            self.check()
            while not lock.acquire(timeout=0.05):
                self.check()
            try:
                self.check()
                if "tts" not in models:
                    started = time.perf_counter()
                    backend = factory(tts_profile, tts_model_dir, voice)
                    selected_backend = backend
                    self.check()
                    backend.load()
                    self.load_seconds = time.perf_counter() - started
                    models["tts"] = backend
                    self.check()
                self.check()
                if self.iterator is None:
                    self.synthesis_started = time.perf_counter()
                    self.iterator = iter(models["tts"].synthesize(self.text))
                if not self.pending:
                    waveform = next(self.iterator, None)
                    self.check()
                    if waveform is None:
                        return None
                    if self.first_waveform_seconds is None:
                        self.first_waveform_seconds = time.perf_counter() - self.synthesis_started
                    if not isinstance(waveform, AudioWaveform):
                        raise TTSBackendError("Invalid voice waveform")
                    if self.rate is not None and waveform.sample_rate != self.rate:
                        raise TTSBackendError("Voice sample rate changed during synthesis")
                    # Count duration from actual samples, never model duration predictions.
                    remaining = (
                        120
                        if self.rate is None
                        else Fraction(120 * self.rate - self.samples, self.rate)
                    )
                    frames, count, rate = pcm_frames(waveform, remaining_seconds=remaining)
                    self.pending = frames
                    self.samples += count
                    self.rate = rate
                self.check()
                pcm = self.pending.pop(0)
                frame = {
                    "type": "audio",
                    "audio": base64.b64encode(pcm).decode(),
                    "sample_rate": self.rate,
                    "samples": len(pcm) // 2,
                    "chunk_index": self.index,
                    "elapsed_seconds": time.perf_counter() - self.start,
                    "provider": tts_profile,
                    "device": "cpu",
                    "synthesis_mode": mode,
                    "load_seconds": self.load_seconds,
                    "first_waveform_seconds": self.first_waveform_seconds,
                }
                self.index += 1
                return json.dumps(frame) + "\n"
            except (HTTPException, SynthesisCancelled):
                raise
            except Exception:
                raise TTSBackendError("Selected voice backend returned invalid audio") from None
            finally:
                try:
                    if (
                        self.cancelled.is_set()
                        and self.iterator is not None
                        and hasattr(self.iterator, "close")
                    ):
                        self.iterator.close()
                finally:
                    lock.release()

    @app.post("/v1/audio/speech", dependencies=[Depends(auth.require("converse", "owner"))])
    async def speech(request: Synthesis):
        allow_inference()
        if request.profile != tts_profile:
            raise HTTPException(503, "Selected retained profile needs its configured backend")
        if tts_profile == "Supertonic 3" and len(request.input) > 1200:
            raise HTTPException(413, "Buffered voice input exceeds the 1200 character limit")
        stream = SynthesisStream(request.input)
        try:
            first = await asyncio.to_thread(stream.next_frame)
            allow_inference()
            if first is None:
                raise TTSBackendError("Voice backend returned no audio")
        except asyncio.CancelledError:
            stream.close()
            raise
        except HTTPException:
            stream.close()
            raise
        except Exception:
            stream.close()
            raise HTTPException(
                503, "Selected voice backend is unavailable or returned invalid audio."
            ) from None

        async def generate():
            try:
                allow_inference()
                yield first
                while True:
                    frame = await asyncio.to_thread(stream.next_frame)
                    if frame is None:
                        return
                    allow_inference()
                    yield frame
            finally:
                stream.close()

        return StreamingResponse(generate(), media_type="application/x-ndjson")

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
    args = parser.parse_args()
    app = create_app(
        Auth(load_or_create_tokens(args.data_dir)),
        stt_model=args.stt_model,
        resource_policy=args.resource_policy,
        tts_profile=args.tts_profile,
        tts_model_dir=args.tts_model_dir,
        voice=args.tts_voice,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
