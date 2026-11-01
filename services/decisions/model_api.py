"""Owner-only independent classifier selection; never inference or discovery."""

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.decisions_models import DecisionsModelChange
from services.decisions.model_settings import (
    DecisionsModelConflict,
    DecisionsModelUnavailable,
    _decode,
)


def router(settings, auth):
    routes = APIRouter(
        prefix="/v1/decisions/options", dependencies=[Depends(auth.require("owner"))]
    )

    def error(status, message):
        return HTTPException(status, message, headers={"Cache-Control": "no-store"})

    async def body(request):
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 4096:
                        raise error(413, "Classifier change exceeds its request bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise error(408, "Classifier request body exceeded its deadline") from None
        try:
            return DecisionsModelChange.model_validate(_decode(raw.decode("utf-8"), 4096))
        except (DecisionsModelUnavailable, ValueError, TypeError, UnicodeError, RecursionError):
            raise error(422, "Classifier change requires its strict versioned shape") from None

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @routes.get("/model")
    async def inspect_model():
        try:
            return response(settings.snapshot())
        except (DecisionsModelUnavailable, OSError, ValueError, RuntimeError, sqlite3.Error):
            raise error(503, "Decisions model options are unavailable") from None

    @routes.post("/model")
    async def change_model(request: Request):
        change = await body(request)
        try:
            return response(settings.change(change))
        except DecisionsModelConflict:
            raise error(409, "Classifier authority changed; inspect the original request") from None
        except ValueError:
            raise error(400, "Classifier selection was refused") from None
        except (DecisionsModelUnavailable, OSError, RuntimeError, sqlite3.Error):
            raise error(503, "Classifier save is unconfirmed; inspect its exact receipt") from None

    @routes.post("/model-changes/lookup")
    async def lookup(request: Request):
        change = await body(request)
        try:
            return response(settings.lookup(change))
        except DecisionsModelConflict:
            raise error(409, "Classifier request belongs to different native authority") from None
        except (DecisionsModelUnavailable, OSError, ValueError, RuntimeError, sqlite3.Error):
            raise error(503, "Classifier request outcome is unavailable") from None

    @routes.get("/model-changes/{key}")
    async def receipt(key: str):
        try:
            result = settings.receipt(key)
        except (DecisionsModelUnavailable, OSError, ValueError, RuntimeError, sqlite3.Error):
            raise error(503, "Classifier receipt is unavailable") from None
        if result is None:
            raise error(404, "Classifier receipt is unavailable")
        return response(result)

    return routes
