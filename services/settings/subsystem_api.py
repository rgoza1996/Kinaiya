"""Owner-only Core selection for a future host launch; no process management."""

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.core_subsystems import (
    MAX_CORE_SUBSYSTEM_BYTES,
    CoreSubsystemChange,
    core_subsystem_json,
)
from services.settings.subsystems import CoreSubsystemConflict, CoreSubsystemUnavailable


def router(settings, auth):
    routes = APIRouter(prefix="/v1/core/options", dependencies=[Depends(auth.require("owner"))])

    def error(status, detail):
        return HTTPException(status, detail, headers={"Cache-Control": "no-store"})

    def available():
        if settings is None:
            raise error(503, "Core subsystem settings are unavailable")
        return settings

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    async def body(request):
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_CORE_SUBSYSTEM_BYTES:
                        raise error(413, "Core subsystem change exceeds its request bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise error(408, "Core subsystem request body exceeded its deadline") from None
        try:
            return CoreSubsystemChange.model_validate(core_subsystem_json(bytes(raw)))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise error(422, "Core subsystem change requires its strict versioned shape") from None

    @routes.get("/subsystems")
    async def inspect():
        owner = available()
        try:
            return response(owner.snapshot())
        except (CoreSubsystemUnavailable, OSError, ValueError, sqlite3.Error):
            raise error(503, "Core subsystem settings are unavailable") from None

    @routes.post("/subsystems")
    async def change(request: Request):
        owner = available()
        original = await body(request)
        try:
            return response(owner.change(original))
        except CoreSubsystemConflict:
            raise error(
                409, "Core subsystem authority changed; inspect the exact original request"
            ) from None
        except (CoreSubsystemUnavailable, OSError, ValueError, sqlite3.Error):
            raise error(
                503, "Core subsystem save is unconfirmed; inspect its exact receipt"
            ) from None

    @routes.post("/subsystem-changes/lookup")
    async def lookup(request: Request):
        owner = available()
        original = await body(request)
        try:
            return response(owner.lookup(original))
        except CoreSubsystemConflict:
            raise error(
                409, "Core subsystem request belongs to different native authority"
            ) from None
        except (CoreSubsystemUnavailable, OSError, ValueError, sqlite3.Error):
            raise error(503, "Core subsystem request outcome is unavailable") from None

    return routes
