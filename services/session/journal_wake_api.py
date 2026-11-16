"""Authenticated metadata hints; no source content or inference admission."""

import asyncio

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.journal_wake import (
    MAX_BYTES,
    JournalWakeCursor,
    JournalWakeObservation,
    JournalWakeWait,
    wake_json,
)
from services.session.journal_wake import WakeUnavailable

BASE = "/v1/transcript/wake"


def install_journal_wake(app, store, auth):
    authorize = auth.require("owner", "converse", "decisions")
    allowed = [Depends(authorize)]

    def respond(value):
        return JSONResponse(value.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get(BASE, dependencies=allowed, response_model=JournalWakeCursor)
    async def snapshot(request: Request):
        if request.query_params:
            raise HTTPException(400, "Invalid journal wake envelope")
        try:
            return respond(store.journal_wake.snapshot())
        except WakeUnavailable:
            raise HTTPException(503, "Original journal wake owner is unavailable") from None

    @app.post(
        BASE + "/wait",
        dependencies=allowed,
        response_model=JournalWakeObservation,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": JournalWakeWait.model_json_schema()}},
            }
        },
    )
    async def wait(request: Request):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("content-type", "content-length", "content-encoding")
        ):
            raise HTTPException(400, "Invalid journal wake envelope")
        if request.headers.get("content-type", "").split(";")[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Journal wake requires identity JSON")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_BYTES:
                        raise HTTPException(413, "Journal wake metadata exceeds its bound")
                    raw.extend(chunk)
            original = JournalWakeWait.model_validate(wake_json(bytes(raw)))
        except TimeoutError:
            raise HTTPException(408, "Journal wake body deadline expired") from None
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid journal wake request") from None
        try:
            result = await store.journal_wake.wait(original)
            # A long-poll result does not outlive credential or origin changes.
            await authorize(request, request.headers.get("authorization", ""))
            return respond(result)
        except WakeUnavailable:
            raise HTTPException(503, "Original journal wake observation is unavailable") from None
