"""Optional local CPU voice service. Launch with a Python containing the voice extra.

Models load lazily in this process; Agent and session services never import them.
Audio is transient by default. Client playback, not this server, measures audibility.
"""

import asyncio
import base64
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from packages.providers.speech import RETAINED_TTS
from services.common.auth import Auth, load_or_create_tokens


def create_app(auth: Auth, *, stt_model="tiny.en", voice="af_heart"):
    app = FastAPI(title="Kinaiya local voice worker")
    models = {}
    lock = threading.Lock()

    @app.get("/health")
    async def health():
        return {
            "service": "local-voice",
            "device": "cpu",
            "loaded": list(models),
            "tts_profile": "Kokoro 82M",
            "retained_candidates": list(RETAINED_TTS),
            "other_profiles": "not_configured",
        }

    def recognize(path):
        from faster_whisper import WhisperModel

        with lock:
            if "stt" not in models:
                models["stt"] = WhisperModel(
                    stt_model,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                    local_files_only=True,
                )
            segments, info = models["stt"].transcribe(str(path), beam_size=3, language="en")
            return {
                "text": " ".join(s.text.strip() for s in segments).strip(),
                "language": info.language,
                "final": True,
                "provider": "faster-whisper",
                "model": stt_model,
            }

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(auth.require("converse", "owner"))])
    async def transcribe(file: Annotated[UploadFile, File()]):
        data = await file.read(10_000_001)
        if len(data) > 10_000_000:
            raise HTTPException(413, "Audio exceeds temporary buffer limit")
        suffix = Path(file.filename or "audio.wav").suffix
        if suffix not in {".wav", ".webm", ".ogg", ".mp3", ".m4a"}:
            suffix = ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix) as temporary:
            temporary.write(data)
            temporary.flush()
            start = time.perf_counter()
            result = await asyncio.to_thread(recognize, temporary.name)
            return {**result, "stt_seconds": time.perf_counter() - start, "retained_audio": False}

    class Synthesis(BaseModel):
        input: str = Field(min_length=1, max_length=16000)
        profile: str = "Kokoro 82M"

    def generate(text):
        import numpy as np
        from kokoro import KPipeline

        start = time.perf_counter()
        with lock:
            if "tts" not in models:
                models["tts"] = KPipeline(lang_code="a", device="cpu", repo_id="hexgrad/Kokoro-82M")
            pipeline = models["tts"]
            for index, (_, _, audio) in enumerate(pipeline(text, voice=voice)):
                samples = np.clip(audio.numpy() if hasattr(audio, "numpy") else audio, -1, 1)
                pcm = (samples * 32767).astype("<i2").tobytes()
                yield (
                    json.dumps(
                        {
                            "type": "audio",
                            "audio": base64.b64encode(pcm).decode(),
                            "sample_rate": 24000,
                            "samples": len(pcm) // 2,
                            "chunk_index": index,
                            "elapsed_seconds": time.perf_counter() - start,
                            "provider": "Kokoro 82M",
                            "device": "cpu",
                        }
                    )
                    + "\n"
                )

    @app.post("/v1/audio/speech", dependencies=[Depends(auth.require("converse", "owner"))])
    async def speech(request: Synthesis):
        if request.profile != "Kokoro 82M":
            raise HTTPException(503, "Selected retained profile needs its configured backend")
        return StreamingResponse(generate(request.input), media_type="application/x-ndjson")

    return app


def main():
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--stt-model", default=os.environ.get("KINAIYA_STT_MODEL", "tiny.en"))
    args = parser.parse_args()
    app = create_app(Auth(load_or_create_tokens(args.data_dir)), stt_model=args.stt_model)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
