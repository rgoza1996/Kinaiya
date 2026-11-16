"""Internal native capture permission endpoints; no microphone or model startup."""

import asyncio
import sqlite3

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.live_captures import (
    MAX_REQUEST_BYTES,
    LiveCaptureBackend,
    LiveCaptureFence,
    LiveCaptureRegistration,
    LiveCaptureSourceState,
    LiveCaptureState,
    capture_json,
)
from packages.contracts.live_windows import LiveWindowPublicationRef

BASE = "/v2/transcript/live-captures"


def install_live_captures(app, store, auth):
    read = auth.require("converse", "decisions")
    mutate = auth.require("converse")

    def respond(value):
        return JSONResponse(value.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    def invoke(method, *args):
        try:
            store.live_captures.backend()
        except (ValueError, sqlite3.Error, TypeError):
            raise HTTPException(503, "Original native capture owner is unavailable") from None
        try:
            return respond(getattr(store.live_captures, method)(*args))
        except (ValueError, KeyError, TypeError, sqlite3.Error, RecursionError):
            raise HTTPException(409, "Original native capture request is unavailable") from None

    async def body(request, model, authorize):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("content-type", "content-length", "content-encoding")
        ):
            raise HTTPException(400, "Invalid native capture envelope")
        if request.headers.get("content-type", "").split(";")[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Native capture requires identity JSON")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
                        raise HTTPException(413, "Native capture request exceeds its bound")
                    raw.extend(chunk)
            result = model.model_validate(capture_json(bytes(raw)), strict=True)
        except TimeoutError:
            raise HTTPException(408, "Native capture body deadline expired") from None
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid native capture request") from None
        await authorize(request, request.headers.get("authorization", ""))
        return result

    def schema(model):
        return {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": model.model_json_schema()}},
            }
        }

    @app.get(BASE + "/backend", dependencies=[Depends(read)], response_model=LiveCaptureBackend)
    async def backend(request: Request):
        if request.query_params:
            raise HTTPException(400, "Invalid native capture envelope")
        return invoke("backend")

    @app.post(
        BASE + "/register",
        dependencies=[Depends(mutate)],
        response_model=LiveCaptureState,
        openapi_extra=schema(LiveCaptureRegistration),
    )
    async def register(request: Request):
        return invoke("register", await body(request, LiveCaptureRegistration, mutate))

    @app.post(
        BASE + "/lookup",
        dependencies=[Depends(read)],
        response_model=LiveCaptureState,
        openapi_extra=schema(LiveCaptureRegistration),
    )
    async def lookup(request: Request):
        return invoke("lookup", await body(request, LiveCaptureRegistration, read))

    @app.post(
        BASE + "/fence",
        dependencies=[Depends(mutate)],
        response_model=LiveCaptureState,
        openapi_extra=schema(LiveCaptureFence),
    )
    async def fence(request: Request):
        return invoke("fence", await body(request, LiveCaptureFence, mutate))

    @app.post(
        BASE + "/source",
        dependencies=[Depends(read)],
        response_model=LiveCaptureSourceState,
        openapi_extra=schema(LiveWindowPublicationRef),
    )
    async def source(request: Request):
        return invoke("source_state", await body(request, LiveWindowPublicationRef, read))
