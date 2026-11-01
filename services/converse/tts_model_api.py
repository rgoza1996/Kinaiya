"""Bounded owner-only TTS settings and original-request recovery."""

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.tts_models import TTSModelChange
from services.converse.model_settings import _decode
from services.converse.tts_model_settings import TTSModelConflict


def install_tts_model_controls(app, runtime, auth):
    routes = APIRouter(prefix="/v1/converse/options", dependencies=[Depends(auth.require("owner"))])

    def error(status, message):
        return HTTPException(status, message, headers={"Cache-Control": "no-store"})

    def owner():
        settings = getattr(runtime, "tts_model_settings", None)
        if settings is None or getattr(runtime, "_tts_options_unavailable", False):
            raise error(503, "TTS model options are unavailable")
        return settings

    async def body(request):
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 4096:
                        raise error(413, "TTS owner request exceeds its bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise error(408, "TTS owner request body deadline expired") from None
        try:
            return TTSModelChange.model_validate(_decode(raw.decode("utf-8"), 4096))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise error(422, "TTS owner request requires its strict versioned shape") from None

    def response(value):
        result = JSONResponse(value, headers={"Cache-Control": "no-store"})
        if len(result.body) > 65536:
            raise error(503, "TTS owner response exceeds its bound")
        return result

    @routes.get("/tts-model")
    async def inspect():
        try:
            return response(owner().snapshot())
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            raise error(503, "TTS model options are unavailable") from None

    async def apply(request, *, lookup):
        settings = owner()
        original = await body(request)
        try:
            return response(settings.lookup(original) if lookup else settings.change(original))
        except TTSModelConflict:
            raise error(
                409, "TTS authority or settings changed; check the original request"
            ) from None
        except ValueError:
            raise error(400, "TTS selection or original request was refused") from None
        except (RuntimeError, OSError, sqlite3.Error):
            raise error(503, "TTS outcome is unconfirmed; check the original request") from None

    @routes.post("/tts-model")
    async def change(request: Request):
        return await apply(request, lookup=False)

    @routes.post("/tts-model-changes/lookup")
    async def lookup(request: Request):
        return await apply(request, lookup=True)

    app.include_router(routes)
