import json

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from packages.contracts.models import ConverseRequest, DeliveryRequest


class SessionControl(BaseModel):
    session_id: str
    expected_generation: int | None = Field(default=None, ge=1)


class SpeechRequest(BaseModel):
    event_id: str
    generation_epoch: int = Field(ge=1)


class EnabledRequest(BaseModel):
    enabled: bool


def create_app(runtime, auth, speech=None):
    app = FastAPI(title="Kinaiya Converse", version="1.0")
    owner = [Depends(auth.require("owner"))]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(auth.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/health")
    async def health():
        return {
            "service": "converse",
            "enabled": runtime.enabled,
            "provider": runtime.provider.info.model_dump(),
            "voice_configured": speech is not None,
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
                async for event in runtime.events(request):
                    yield "data: " + json.dumps(event) + "\n\n"
            except (ValueError, RuntimeError) as exc:
                yield "data: " + json.dumps({"type": "error", "error": str(exc)}) + "\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/v1/converse/interrupt", dependencies=owner)
    async def interrupt(request: SessionControl):
        try:
            return await runtime.interrupt(request.session_id, request.expected_generation)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/converse/enabled", dependencies=[Depends(auth.require("owner", "resources"))])
    async def enabled(request: EnabledRequest):
        runtime.enabled = request.enabled
        if not request.enabled:
            for session_id in list(runtime.playback):
                await runtime.interrupt(session_id)
        return {"enabled": runtime.enabled}

    @app.post("/v1/converse/delivery", dependencies=owner)
    async def delivery(request: DeliveryRequest):
        try:
            return await runtime.acknowledge_delivery(request)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/converse/transcribe", dependencies=owner)
    async def transcribe(file: UploadFile):
        if not runtime.enabled:
            raise HTTPException(409, "Converse is off; direct Agent Chat remains available")
        if speech is None:
            raise HTTPException(503, "Configure an STT provider in settings")
        data = await file.read(10_000_001)
        if len(data) > 10_000_000:
            raise HTTPException(413, "Recording exceeds 10 MB temporary limit")
        if not runtime.enabled:
            raise HTTPException(409, "Converse is off; direct Agent Chat remains available")
        return await speech.transcribe(data, file.filename or "recording.wav")

    @app.post("/v1/converse/speech", dependencies=owner)
    async def audio(request: SpeechRequest):
        if speech is None:
            raise HTTPException(503, "Configure a TTS provider in settings")
        try:
            event = await runtime.validate_speech(request.event_id, request.generation_epoch)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        async def generate():
            async for chunk in speech.synthesize(event.payload["text"]):
                try:
                    await runtime.validate_speech(request.event_id, request.generation_epoch)
                except ValueError:
                    return
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
