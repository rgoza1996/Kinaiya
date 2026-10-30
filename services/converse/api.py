import asyncio
import base64
import hashlib
import json
import time
from contextlib import aclosing
from typing import Annotated

from anyio import CancelScope
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from packages.contracts.converse_setup import ConverseSetup
from packages.contracts.models import ContentScope, ConverseRequest, DeliveryRequest, SpeechWindow
from packages.providers.disclosure import scope_allowed
from packages.providers.speech import SpeechProviderError

_STT_CLOSE_OWNERS = set()
_STT_OWNERS = set()


def _cloud_stt_current(runtime, speech, content_scope, validate=None):
    """Capture owner availability before decoding; recheck immediately before export.

    This fences late results, not an already admitted remote provider's processing.
    Local SpeechClient keeps its existing, independent interface.
    """
    info = getattr(speech, "stt_info", None)
    if info is None:
        return None
    if info.local is not False or not scope_allowed(info, content_scope):
        raise HTTPException(409, "Recording scope is not permitted for the selected STT provider")
    stamp, target = info.model_dump(), speech.stt
    try:
        admission = runtime.admission()
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(409, "Converse availability changed") from exc

    def bound():
        return (
            runtime.admission_current(admission)
            and speech.stt is target
            and speech.stt_info.model_dump() == stamp
        )

    async def current():
        if not bound():
            return False
        if validate is not None and not await validate():
            return False
        return bound()

    return current


def _transcription(speech, data, filename, content_scope, current):
    if current is None:
        return speech.transcribe(data, filename)
    return speech.transcribe(data, filename, content_scope=content_scope, current=current)


def _cloud_tts_current(speech, event, scoped):
    """Bind the selected backend; additionally authorize cloud reply disclosure."""
    info = getattr(speech, "tts_info", getattr(speech, "info", None))
    content_scope = event.payload.get("content_scope", "local_only")
    if info is not None and info.local is not True:
        if (
            info.local is not False
            or not scope_allowed(info, content_scope)
            or not callable(scoped)
        ):
            raise HTTPException(403, "Reply scope is not permitted for the selected TTS provider")
    stamp = None if info is None else info.model_dump()
    target, unscoped = getattr(speech, "tts", speech), speech.synthesize

    def current():
        selected_info = getattr(speech, "tts_info", getattr(speech, "info", None))
        return (
            getattr(speech, "tts", speech) is target
            and (None if selected_info is None else selected_info.model_dump()) == stamp
            and speech.synthesize == unscoped
            and getattr(speech, "synthesize_scoped", None) == scoped
            and event.payload.get("content_scope", "local_only") == content_scope
        )

    return current


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


def create_app(runtime, auth, speech=None, window_controller=None, *, setup=None):
    from services.converse.memory_api import install_memory_controls

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
        from packages.providers.text import ProviderError
        from services.resources.providers import managed_text_binding

        try:
            availability = runtime.availability()
        except (OSError, ValueError):
            availability = {"enabled": False, "owner_intent_unavailable": True}
        result = {
            "service": "converse",
            **availability,
            "provider": runtime.provider.info.model_dump(),
            "voice_configured": speech is not None,
            "speech_operations": runtime.speech_status(),
            "text_operations": runtime.text_status(),
            "streaming_windows": window_controller is not None,
            "context_history": (
                "measured" if getattr(runtime, "context_budgeted", False) else "legacy_embedded"
            ),
        }
        try:
            binding = managed_text_binding(runtime.provider)
        except ProviderError as exc:
            raise HTTPException(503, "Managed text configuration identity is unavailable") from exc
        if binding is not None:
            result["managed_text"] = binding
        return result

    @app.get("/v1/converse/setup", dependencies=owner, response_model=ConverseSetup)
    async def model_setup():
        from services.converse.setup import setup_state

        try:
            value = setup_state(runtime, setup)
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise HTTPException(
                503,
                "Converse setup declarations are unavailable",
                headers={"Cache-Control": "no-store"},
            ) from None
        return JSONResponse(value.model_dump(), headers={"Cache-Control": "no-store"})

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
    async def transcribe(
        request: Request,
        file: UploadFile,
        content_scope: Annotated[ContentScope, Form()] = "local_only",
    ):
        if not runtime.enabled:
            raise HTTPException(409, "Converse is off; direct Agent Chat remains available")
        if speech is None:
            raise HTTPException(503, "Configure an STT provider in settings")
        current = _cloud_stt_current(runtime, speech, content_scope)
        data = await file.read(10_000_001)
        if len(data) > 10_000_000:
            raise HTTPException(413, "Recording exceeds 10 MB temporary limit")
        if not runtime.enabled:
            raise HTTPException(409, "Converse is off; direct Agent Chat remains available")
        transcript = await _transcribe_connected(
            request,
            lambda: _transcription(
                speech, data, file.filename or "recording.wav", content_scope, current
            ),
        )
        return {**transcript, "content_scope": content_scope}

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
            if session.thread_id != request.thread_id or session.epoch != request.epoch:
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

        async def window_current():
            try:
                session = await runtime.journal.session(request.session_id)
                latest = await runtime.journal.latest_window(
                    request.session_id, request.epoch, request.utterance_id, request.window_index
                )
                return (
                    session.thread_id == request.thread_id
                    and session.epoch == request.epoch
                    and (latest.event_id if latest else None)
                    == (previous.event_id if previous else None)
                )
            except (KeyError, ValueError):
                return False

        current = _cloud_stt_current(runtime, speech, request.content_scope, window_current)
        started = time.monotonic()
        transcript = await _transcribe_connected(
            http_request,
            lambda: _transcription(speech, data, "window.wav", request.content_scope, current),
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
        scoped = getattr(speech, "synthesize_scoped", None)
        current = _cloud_tts_current(speech, event, scoped)

        async def generate():
            async with aclosing(
                runtime.speech_frames(
                    event,
                    request.generation_epoch,
                    speech.synthesize,
                    synthesize_scoped=scoped,
                    current=current,
                )
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

    install_memory_controls(app, runtime, auth)
    return app
