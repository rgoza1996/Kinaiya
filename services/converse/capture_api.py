"""Owner-only Converse capture preference; saves never start audio or inference."""

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.capture_modes import (
    MAX_CAPTURE_MODE_BYTES,
    MAX_CAPTURE_MODE_RESPONSE_BYTES,
    CaptureModeChange,
    capture_mode_json,
)
from services.converse.capture_settings import CaptureModeConflict, CaptureModeUnavailable


def install_capture_controls(app, runtime, auth):
    routes = APIRouter(prefix="/v1/converse/options", dependencies=[Depends(auth.require("owner"))])

    def error(status, detail):
        return HTTPException(status, detail, headers={"Cache-Control": "no-store"})

    def available():
        settings = getattr(runtime, "capture_settings", None)
        if settings is None or getattr(runtime, "_capture_options_unavailable", False):
            raise error(503, "Converse capture mode settings are unavailable")
        return settings

    def response(value):
        result = JSONResponse(value, headers={"Cache-Control": "no-store"})
        if len(result.body) > MAX_CAPTURE_MODE_RESPONSE_BYTES:
            raise error(503, "Converse capture mode response exceeds its bound")
        return result

    async def body(request):
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_CAPTURE_MODE_BYTES:
                        raise error(413, "Converse capture mode change exceeds its request bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise error(408, "Converse capture mode request body exceeded its deadline") from None
        try:
            return CaptureModeChange.model_validate(capture_mode_json(bytes(raw)))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise error(
                422, "Converse capture mode change requires its strict versioned shape"
            ) from None

    @routes.get("/capture-mode")
    async def inspect():
        owner = available()
        try:
            return response(owner.snapshot())
        except (CaptureModeUnavailable, OSError, ValueError, sqlite3.Error):
            raise error(503, "Converse capture mode settings are unavailable") from None

    @routes.post("/capture-mode")
    async def change(request: Request):
        owner = available()
        original = await body(request)
        try:
            return response(owner.change(original))
        except CaptureModeConflict:
            raise error(
                409, "Converse capture mode authority changed; inspect the exact original request"
            ) from None
        except (CaptureModeUnavailable, OSError, ValueError, sqlite3.Error):
            raise error(
                503, "Converse capture mode save is unconfirmed; inspect its exact receipt"
            ) from None

    @routes.post("/capture-mode-changes/lookup")
    async def lookup(request: Request):
        owner = available()
        original = await body(request)
        try:
            return response(owner.lookup(original))
        except CaptureModeConflict:
            raise error(
                409, "Converse capture mode request belongs to different native authority"
            ) from None
        except (CaptureModeUnavailable, OSError, ValueError, sqlite3.Error):
            raise error(503, "Converse capture mode request outcome is unavailable") from None

    app.include_router(routes)
