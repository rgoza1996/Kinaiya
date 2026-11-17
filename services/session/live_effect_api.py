"""Authenticated bounded native live-result consumption; labels grant no work."""

import asyncio
import sqlite3

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.live_agent_sources import LiveAgentSourceQuery
from packages.contracts.live_effect_pending import MAX_PAGE_BYTES, LiveEffectPendingQuery
from packages.contracts.live_effects import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    LiveEffectAdmit,
    LiveEffectFinish,
    LiveEffectQuery,
    LiveEffectReceipt,
    LiveEffectStart,
    effect_json,
)
from services.session.live_agent_sources import authority
from services.session.live_effects import LiveEffectConflict

BASE = "/v2/transcript/live-effects"


def install_live_effects(app, store, auth):
    read = auth.require("owner", "converse", "decisions", "agent")
    mutate = auth.require("converse", "decisions")

    async def body(request, model, authorize):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("content-type", "content-length", "content-encoding")
        ):
            raise HTTPException(400, "Invalid live consumer request envelope")
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Live consumer requests require identity JSON")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
                        raise HTTPException(413, "Live consumer request exceeds its bound")
                    raw.extend(chunk)
            result = model.model_validate(effect_json(bytes(raw)), strict=True)
        except TimeoutError:
            raise HTTPException(408, "Live consumer request body deadline expired") from None
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid original live consumer request") from None
        role = await authorize(request, request.headers.get("authorization", ""))
        return result, role

    def invoke(method, original, role=None):
        try:
            operation = getattr(store.live_effects, method)
            result = operation(original) if role is None else operation(original, role)
            response = JSONResponse(
                result.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
            )
            if len(response.body) > (MAX_PAGE_BYTES if method == "pending" else MAX_RESPONSE_BYTES):
                raise ValueError("Live consumer response exceeds its bound")
            return response
        except LiveEffectConflict:
            raise HTTPException(409, "Original native live consumption conflicts") from None
        except (ValueError, KeyError, TypeError, sqlite3.Error, RecursionError):
            raise HTTPException(503, "Original native live consumption is unconfirmed") from None

    def schema(model):
        return {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": model.model_json_schema()}},
            }
        }

    @app.post(BASE + "/lookup", dependencies=[Depends(read)], openapi_extra=schema(LiveEffectQuery))
    async def lookup(request: Request):
        original, _ = await body(request, LiveEffectQuery, read)
        return invoke("lookup", original)

    @app.post(
        BASE + "/pending",
        dependencies=[Depends(read)],
        openapi_extra=schema(LiveEffectPendingQuery),
    )
    async def pending(request: Request):
        original, _ = await body(request, LiveEffectPendingQuery, read)
        return invoke("pending", original)

    @app.post(
        BASE + "/evidence", dependencies=[Depends(read)], openapi_extra=schema(LiveEffectReceipt)
    )
    async def evidence(request: Request):
        original, _ = await body(request, LiveEffectReceipt, read)
        return invoke("evidence", original)

    @app.post(
        BASE + "/source-authority",
        dependencies=[Depends(read)],
        openapi_extra=schema(LiveAgentSourceQuery),
    )
    async def source_authority(request: Request):
        original, _ = await body(request, LiveAgentSourceQuery, read)
        try:
            result = authority(store, original)
            return JSONResponse(
                result.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
            )
        except (ValueError, KeyError, TypeError, sqlite3.Error, RecursionError):
            raise HTTPException(503, "Native live Agent source remains unqualified") from None

    @app.post(
        BASE + "/admit", dependencies=[Depends(mutate)], openapi_extra=schema(LiveEffectAdmit)
    )
    async def admit(request: Request):
        original, role = await body(request, LiveEffectAdmit, mutate)
        return invoke("admit", original, role)

    @app.post(
        BASE + "/start", dependencies=[Depends(mutate)], openapi_extra=schema(LiveEffectStart)
    )
    async def start(request: Request):
        original, role = await body(request, LiveEffectStart, mutate)
        return invoke("start", original, role)

    @app.post(
        BASE + "/finish", dependencies=[Depends(mutate)], openapi_extra=schema(LiveEffectFinish)
    )
    async def finish(request: Request):
        original, role = await body(request, LiveEffectFinish, mutate)
        return invoke("finish", original, role)
