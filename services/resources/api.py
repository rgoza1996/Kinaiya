"""Owner-only explicit resource actions. No arbitrary process parameters."""

import asyncio
from typing import Literal

from anyio import CancelScope
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.standing_work import StandingConflict, StandingUnavailable


class ResourceAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["profile", "handoff", "restore", "converse", "task_finished"]
    idempotency_key: str = Field(min_length=1, max_length=256)
    profile: (
        Literal["conversation_priority", "local_work_priority", "local_only", "configured_hybrid"]
        | None
    ) = None
    enabled: bool | None = None
    expected_owner_intent_revision: int | None = Field(default=None, ge=0, le=(1 << 63) - 1)
    required_bytes: int = Field(default=0, ge=0, le=1 << 46)
    required_gpu_bytes: int = Field(default=0, ge=0, le=1 << 46)
    task_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    reason: str = Field(default="Explicit owner resource control", max_length=160)

    @model_validator(mode="after")
    def valid_operation_fields(self):
        allowed = {
            "profile": {"profile"},
            "handoff": {"required_bytes", "required_gpu_bytes", "task_id", "reason"},
            "restore": set(),
            "converse": {"enabled", "expected_owner_intent_revision"},
            "task_finished": {"task_id"},
        }
        if self.model_fields_set - {"operation", "idempotency_key"} - allowed[self.operation]:
            raise ValueError("Fields do not apply to this resource operation")
        required = {"profile": "profile", "converse": "enabled", "task_finished": "task_id"}.get(
            self.operation
        )
        if required and getattr(self, required) is None:
            raise ValueError("Missing required resource operation field")
        return self


class ManagedTextResponse(StreamingResponse):
    def __init__(self, body, *, timeout_s, media_type="application/x-ndjson"):
        super().__init__(body, media_type=media_type, headers={"Cache-Control": "no-store"})
        self.timeout_s = timeout_s

    async def __call__(self, scope, receive, send):
        # The broker alone receives disconnect after consuming the JSON body.
        # In particular, do not invoke StreamingResponse's competing receiver.
        try:
            async with asyncio.timeout(self.timeout_s):
                await self.stream_response(send)
        finally:
            # Even a failure sending headers closes a never-iterated body.
            # The broker retains any cleanup that outlasts this bounded wait.
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


def create_app(coordinator, auth, whistle=None, qwen3=None, gguf=None):
    app = FastAPI(title="Kinaiya local resources")
    owner = [Depends(auth.require("owner"))]

    @app.get("/health")
    async def health():
        return {"service": "resources", "phase": coordinator.state.record.phase}

    @app.get("/v1/resources", dependencies=owner)
    async def status():
        try:
            result = coordinator.status()
            if whistle is not None:
                result["whistle"] = whistle.status()
            if qwen3 is not None:
                result["qwen3"] = qwen3.status()
            if gguf is not None:
                result["gguf"] = gguf.status()
            return result
        except (OSError, ValueError) as exc:
            raise HTTPException(503, "Resource owner intent is unavailable") from exc

    @app.post("/v1/resources/actions", dependencies=owner)
    async def action(body: ResourceAction):
        if body.operation == "converse" and body.enabled is None:
            raise HTTPException(400, "converse requires enabled")
        try:
            return await coordinator.action(body.model_dump(exclude_unset=True))
        except StandingUnavailable:
            raise HTTPException(
                503, str(StandingUnavailable()), headers={"Cache-Control": "no-store"}
            ) from None
        except StandingConflict:
            raise HTTPException(
                409, str(StandingConflict()), headers={"Cache-Control": "no-store"}
            ) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/audio/transcriptions", dependencies=[Depends(auth.require("owner", "converse"))])
    async def transcribe(request: Request):
        if whistle is None:
            raise HTTPException(503, "Managed Whistle STT is not selected")
        from packages.providers.whistle_ipc import MAX_RESPONSE_BYTES, encode_json

        result = await whistle.transcribe(request)
        return Response(encode_json(result, MAX_RESPONSE_BYTES), media_type="application/json")

    @app.post("/v1/converse/completions", dependencies=[Depends(auth.require("owner", "converse"))])
    async def complete(request: Request):
        if qwen3 is None:
            raise HTTPException(503, "Managed Qwen3 dialogue is not selected")
        body = await qwen3.stream(request)
        return ManagedTextResponse(body, timeout_s=body.remaining_timeout_s + 1)

    @app.post("/v1/chat/completions", dependencies=[Depends(auth.require("owner", "converse"))])
    async def native_complete(request: Request):
        if gguf is None:
            raise HTTPException(503, "Managed GGUF dialogue is not selected")
        body = await gguf.stream(request)
        return ManagedTextResponse(
            body, timeout_s=body.remaining_timeout_s + 1, media_type="text/event-stream"
        )

    @app.post(
        "/v1/chat/completions/input_tokens",
        dependencies=[Depends(auth.require("owner", "converse"))],
    )
    async def native_measure(request: Request):
        if gguf is None:
            raise HTTPException(503, "Prompt measurement is unavailable")
        from packages.providers.prompt_budget import MAX_COUNT_BYTES
        from packages.providers.qwen3_ipc import encode_json

        result = await gguf.measure_prompt(request)
        return Response(
            encode_json(result, MAX_COUNT_BYTES),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return app
