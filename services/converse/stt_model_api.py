"""Bounded owner-only STT settings and original-request recovery."""

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.stt_models import STTModelChange
from packages.contracts.stt_settlement import (
    STTAdmissionInventory,
    STTAdmissionReconcile,
    STTAdmissionResult,
)
from services.converse.model_settings import _decode
from services.converse.stt_model_settings import STTModelConflict


def install_stt_model_controls(app, runtime, auth):
    routes = APIRouter(prefix="/v1/converse/options", dependencies=[Depends(auth.require("owner"))])

    def error(status, message):
        return HTTPException(status, message, headers={"Cache-Control": "no-store"})

    def owner():
        settings = getattr(runtime, "stt_model_settings", None)
        if settings is None or getattr(runtime, "_stt_options_unavailable", False):
            raise error(503, "STT model options are unavailable")
        return settings

    async def body(request, model=STTModelChange):
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 4096:
                        raise error(413, "STT owner request exceeds its bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise error(408, "STT owner request body deadline expired") from None
        try:
            return model.model_validate(_decode(raw.decode("utf-8"), 4096))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise error(422, "STT owner request requires its strict versioned shape") from None

    def response(value):
        result = JSONResponse(value, headers={"Cache-Control": "no-store"})
        if len(result.body) > 65536:
            raise error(503, "STT owner response exceeds its bound")
        return result

    @routes.get("/stt-model")
    async def inspect():
        try:
            return response(owner().snapshot())
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            raise error(503, "STT model options are unavailable") from None

    async def apply(request, *, lookup):
        settings = owner()
        original = await body(request)
        try:
            return response(settings.lookup(original) if lookup else settings.change(original))
        except STTModelConflict:
            raise error(
                409, "STT authority or settings changed; check the original request"
            ) from None
        except ValueError:
            raise error(400, "STT selection or original request was refused") from None
        except (RuntimeError, OSError, sqlite3.Error):
            raise error(503, "STT outcome is unconfirmed; check the original request") from None

    @routes.post("/stt-model")
    async def change(request: Request):
        return await apply(request, lookup=False)

    @routes.post("/stt-model-changes/lookup")
    async def lookup(request: Request):
        return await apply(request, lookup=True)

    @routes.get("/stt-admissions")
    async def admissions():
        try:
            return response(
                STTAdmissionInventory.model_validate(owner().admission_inventory()).model_dump()
            )
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            raise error(503, "STT admission metadata is unavailable") from None

    @routes.post("/stt-admissions/reconcile")
    async def reconcile(request: Request):
        settings = owner()
        original = await body(request, STTAdmissionReconcile)
        try:
            # The owner retains actual reconciliation/response cleanup if this
            # bounded HTTP wait ends first. No audio or inference is retried.
            async with asyncio.timeout(5):
                return response(
                    STTAdmissionResult.model_validate(
                        await settings.reconcile_admission(original)
                    ).model_dump()
                )
        except TimeoutError:
            return response(
                settings._reconcile_result(
                    original, reason="Original settlement check is still locally owned"
                )
            )
        except ValueError:
            raise error(400, "Original STT admission identity was refused") from None
        except (RuntimeError, OSError, sqlite3.Error):
            raise error(503, "Original STT settlement is unconfirmed") from None

    app.include_router(routes)
