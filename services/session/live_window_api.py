"""Explicit authenticated v2 capture publication and positive-only readback."""

import asyncio
import sqlite3

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.live_windows import (
    MAX_REQUEST_BYTES,
    LiveWindowCandidate,
    LiveWindowPublication,
    LiveWindowPublicationProof,
    LiveWindowPublicationRef,
    LiveWindowPublicationState,
    LiveWindowScope,
    live_window_json,
)
from services.session import live_windows
from services.session.store import JournalError

BASE = "/v2/transcript/live-windows"


def install_live_windows(app, store, auth):
    publish_auth = auth.require("converse")
    source_auth = auth.require("converse", "decisions")

    async def body(request, model, authorize, maximum=4096):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("content-type", "content-length", "content-encoding")
        ):
            raise HTTPException(400, "Invalid live publication envelope")
        if request.headers.get("content-type", "").split(";")[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Live publication requires identity JSON")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > maximum:
                        raise HTTPException(413, "Live publication exceeds its bound")
                    raw.extend(chunk)
            result = model.model_validate(live_window_json(bytes(raw), maximum), strict=True)
        except TimeoutError:
            raise HTTPException(408, "Live publication body deadline expired") from None
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid live publication request") from None
        await authorize(request, request.headers.get("authorization", ""))
        return result

    def invoke(operation, *args):
        try:
            result = operation(store, *args)
            return JSONResponse(
                None if result is None else result.model_dump(mode="json"),
                headers={"Cache-Control": "no-store"},
            )
        except (JournalError, KeyError, ValueError, TypeError, sqlite3.Error, RecursionError):
            # This refusal never claims that an earlier original publication had no effect.
            raise HTTPException(409, "Original live publication is unavailable") from None

    @app.post(
        BASE,
        dependencies=[Depends(publish_auth)],
        response_model=LiveWindowPublicationProof,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": LiveWindowPublication.model_json_schema()}
                },
            }
        },
    )
    async def publish(request: Request):
        original = await body(request, LiveWindowPublication, publish_auth, MAX_REQUEST_BYTES)
        return invoke(live_windows.publish, original)

    @app.post(
        BASE + "/lookup",
        dependencies=[Depends(publish_auth)],
        response_model=LiveWindowPublicationState,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": LiveWindowPublicationRef.model_json_schema()}
                },
            }
        },
    )
    async def lookup(request: Request):
        original = await body(request, LiveWindowPublicationRef, publish_auth)
        return invoke(live_windows.lookup, original)

    @app.post(
        BASE + "/latest",
        dependencies=[Depends(source_auth)],
        response_model=LiveWindowCandidate | None,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": LiveWindowScope.model_json_schema()}},
            }
        },
    )
    async def latest(request: Request):
        scope = await body(request, LiveWindowScope, source_auth)
        return invoke(
            live_windows.latest, scope.session_id, scope.thread_id, scope.epoch, scope.utterance_id
        )
