import asyncio
import base64
import hashlib
import json
import time
from contextlib import aclosing

from anyio import CancelScope
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from packages.contracts.models import ConverseRequest, DeliveryRequest, SpeechWindow
from packages.providers.speech import SpeechProviderError

_STT_CLOSE_OWNERS = set()
_STT_OWNERS = set()


async def _transcribe_connected(request, operation):
    """Bodies are already parsed; one receive owner now watches disconnect.

    HTTP cleanup may outlive the bounded request wait. Retain that close task;
    Whistle's broker separately owns native cancellation/reaping. This does
    not establish that a legacy Whisper native thread has stopped.
    """
    if len(_STT_OWNERS) >= 32:
        raise HTTPException(503, "STT transport cleanup is busy")
    owner = object()
    _STT_OWNERS.add(owner)

    async def disconnected():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    work = asyncio.create_task(operation())
    watcher = asyncio.create_task(disconnected())
    try:
        await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        # Give a simultaneously available disconnect its receive turn before
        # accepting text or allowing the audio-window append below.
        await asyncio.sleep(0)
        if watcher.done():
            raise HTTPException(499, "STT request disconnected")
        return work.result()
    except SpeechProviderError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    finally:
        watcher.cancel()
        if not work.done():
            work.cancel()

        async def close():
            try:
                await asyncio.gather(work, watcher, return_exceptions=True)
            finally:
                _STT_OWNERS.discard(owner)

        cleanup = asyncio.create_task(close())
        _STT_CLOSE_OWNERS.add(cleanup)
        cleanup.add_done_callback(_STT_CLOSE_OWNERS.discard)
        with CancelScope(shield=True):
            await asyncio.wait((cleanup,), timeout=1)


class SessionControl(BaseModel):
    session_id: str
    expected_generation: int | None = Field(default=None, ge=1)


class SpeechRequest(BaseModel):
    event_id: str
    generation_epoch: int = Field(ge=1)


class EnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class AudioWindow(SpeechWindow):
    audio_base64: str = Field(min_length=1, max_length=14_000_000)


class TypedTurnResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Disconnect can interrupt send while the generator is paused at
            # accepted/text. Own that close explicitly, rather than relying on
            # asynchronous-generator garbage collection after the ASGI return.
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


def create_app(runtime, auth, speech=None, window_controller=None):
    app = FastAPI(title="Kinaiya Converse", version="1.0")
    owner = [Depends(auth.require("owner"))]
    require_control = auth.require("owner", "resources")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(auth.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/health")
    async def health():
        try:
            availability = runtime.availability()
        except (OSError, ValueError):
            availability = {"enabled": False, "owner_intent_unavailable": True}
        return {
            "service": "converse",
            **availability,
            "provider": runtime.provider.info.model_dump(),
            "voice_configured": speech is not None,
            "speech_operations": runtime.speech_status(),
            "text_operations": runtime.text_status(),
            "streaming_windows": window_controller is not None,
        }

    @app.post("/v1/converse/turns", dependencies=owner)
    async def turn(request: ConverseRequest):
        try:
            return await runtime.turn(request)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/converse/turns/stream", dependencies=owner)
    async def stream(request: ConverseRequest):
        async def generate():
            try:
                async with aclosing(runtime.events(request)) as events:
                    async for event in events:
                        yield "data: " + json.dumps(event) + "\n\n"
            except (ValueError, RuntimeError) as exc:
                yield "data: " + json.dumps({"type": "error", "error": str(exc)}) + "\n\n"

        return TypedTurnResponse(generate(), media_type="text/event-stream")

    @app.post("/v1/converse/interrupt", dependencies=owner)
    async def interrupt(request: SessionControl):
        try:
            return await runtime.interrupt(request.session_id, request.expected_generation)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/converse/enabled")
    async def enabled(request: EnabledRequest, actor: str = Depends(require_control)):
        try:
            return await runtime.set_enabled(request.enabled, owner=actor == "owner")
        except (OSError, ValueError, RuntimeError, OverflowError) as exc:
            raise HTTPException(
                503, "Converse control is unconfirmed; inspect availability"
            ) from exc

    @app.post("/v1/converse/delivery", dependencies=owner)
    async def delivery(request: DeliveryRequest):
        try:
            return await runtime.acknowledge_delivery(request)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/converse/transcribe", dependencies=owner)
    async def transcribe(request: Request, file: UploadFile):
        if not runtime.enabled:
            raise HTTPException(409, "Converse is off; direct Agent Chat remains available")
        if speech is None:
            raise HTTPException(503, "Configure an STT provider in settings")
        data = await file.read(10_000_001)
        if len(data) > 10_000_000:
            raise HTTPException(413, "Recording exceeds 10 MB temporary limit")
        if not runtime.enabled:
            raise HTTPException(409, "Converse is off; direct Agent Chat remains available")
        return await _transcribe_connected(
            request, lambda: speech.transcribe(data, file.filename or "recording.wav")
        )

    @app.post("/v1/converse/windows", dependencies=owner)
    async def window(request: SpeechWindow):
        if window_controller is None:
            raise HTTPException(503, "Streaming conversation control is unavailable")
        if not runtime.enabled:
            raise HTTPException(409, "Converse is off; direct Agent Chat remains available")
        try:
            entry = await window_controller.ingest(request)
            return {"entry": entry.model_dump(mode="json")}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/converse/audio/windows", dependencies=owner)
    async def audio_window(request: AudioWindow, http_request: Request):
        if window_controller is None or speech is None:
            raise HTTPException(503, "Configure streaming conversation control and STT")
        if not runtime.enabled:
            raise HTTPException(409, "Converse is off; direct Agent Chat remains available")
        try:
            data = base64.b64decode(request.audio_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(400, "Invalid window audio") from exc
        if len(data) > 10_000_000:
            raise HTTPException(413, "Audio window exceeds temporary buffer limit")
        digest = hashlib.sha256(data).hexdigest()
        try:
            session = await runtime.journal.session(request.session_id)
            if session.thread_id != request.thread_id:
                raise ValueError("Wrong window thread")
            previous = await runtime.journal.latest_window(
                request.session_id, request.epoch, request.utterance_id, request.window_index
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, "Unknown or obsolete audio-window scope") from exc
        if previous is not None and previous.payload.get("revision") == request.revision:
            expected = request.model_dump(exclude={"audio_base64", "text", "audio_sha256"})
            if previous.payload.get("audio_sha256") != digest or any(
                previous.payload.get(k) != v for k, v in expected.items()
            ):
                raise HTTPException(409, "Audio window identity reused with different content")
            return {"entry": previous.model_dump(mode="json"), "replay": True}
        if request.revision != (previous.payload["revision"] + 1 if previous else 1):
            raise HTTPException(409, "Audio-window revision must increase by one")
        started = time.monotonic()
        transcript = await _transcribe_connected(
            http_request, lambda: speech.transcribe(data, "window.wav")
        )
        fields = request.model_dump(exclude={"audio_base64"})
        fields.update(text=transcript.get("text", "").strip(), audio_sha256=digest)
        try:
            entry = await window_controller.ingest(SpeechWindow.model_validate(fields))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "entry": entry.model_dump(mode="json"),
            "stt_ms": (time.monotonic() - started) * 1000,
            "retained_audio": False,
        }

    @app.post("/v1/converse/speech", dependencies=owner)
    async def audio(request: SpeechRequest):
        if speech is None:
            raise HTTPException(503, "Configure a TTS provider in settings")
        try:
            runtime.check_speech_ready()
            event = await runtime.validate_speech(request.event_id, request.generation_epoch)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        async def generate():
            async with aclosing(
                runtime.speech_frames(event, request.generation_epoch, speech.synthesize)
            ) as frames:
                async for chunk in frames:
                    yield (
                        json.dumps(
                            {
                                **chunk,
                                "event_id": event.event_id,
                                "generation_epoch": request.generation_epoch,
                                "chunk_id": event.payload["chunk_id"],
                            }
                        )
                        + "\n"
                    )

        return StreamingResponse(generate(), media_type="application/x-ndjson")

    return app
