"""Explicit v2 native inference ledger; generic append never grants this path."""

import asyncio
import sqlite3

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.decisions_attempts import MAX_ADMISSION_BYTES, MAX_ATTEMPT_BYTES
from packages.contracts.live_decisions_attempts import (
    LiveDecisionsAttempt,
    LiveDecisionsAttemptAdmit,
    LiveDecisionsAttemptFinish,
    LiveDecisionsAttemptLookup,
    LiveDecisionsAttemptState,
    LiveDecisionsPublication,
    LiveDecisionsPublicationState,
    live_attempt_json,
)

BASE = "/v2/transcript/live-decisions"


def install_live_decisions(app, store, auth):
    native = auth.require("decisions")
    read_outcome = auth.require("decisions", "converse")

    async def body(request, model, authorize, maximum):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("content-type", "content-length", "content-encoding")
        ):
            raise HTTPException(400, "Invalid native live inference envelope")
        if request.headers.get("content-type", "").split(";")[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Native live inference requires identity JSON")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > maximum:
                        raise HTTPException(413, "Native live inference metadata exceeds its bound")
                    raw.extend(chunk)
            result = model.model_validate(live_attempt_json(bytes(raw), maximum), strict=True)
        except TimeoutError:
            raise HTTPException(408, "Native live inference body deadline expired") from None
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid native live inference request") from None
        await authorize(request, request.headers.get("authorization", ""))
        return result

    def invoke(method, original):
        try:
            result = getattr(store.live_decisions_attempts, method)(original)
            return JSONResponse(
                result.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
            )
        except (ValueError, KeyError, TypeError, sqlite3.Error, RecursionError):
            raise HTTPException(409, "Original native live inference is unavailable") from None

    def schema(model):
        return {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": model.model_json_schema()}},
            }
        }

    @app.post(
        BASE + "/attempts/lookup",
        dependencies=[Depends(native)],
        response_model=LiveDecisionsAttemptState,
        openapi_extra=schema(LiveDecisionsAttemptLookup),
    )
    async def lookup(request: Request):
        return invoke("lookup", await body(request, LiveDecisionsAttemptLookup, native, 16384))

    @app.post(
        BASE + "/attempts/admit",
        dependencies=[Depends(native)],
        response_model=LiveDecisionsAttempt,
        openapi_extra=schema(LiveDecisionsAttemptAdmit),
    )
    async def admit(request: Request):
        return invoke(
            "admit", await body(request, LiveDecisionsAttemptAdmit, native, MAX_ADMISSION_BYTES)
        )

    @app.post(
        BASE + "/attempts/finish",
        dependencies=[Depends(native)],
        response_model=LiveDecisionsAttempt,
        openapi_extra=schema(LiveDecisionsAttemptFinish),
    )
    async def finish(request: Request):
        return invoke(
            "finish", await body(request, LiveDecisionsAttemptFinish, native, MAX_ATTEMPT_BYTES)
        )

    @app.post(
        BASE + "/publish",
        dependencies=[Depends(native)],
        response_model=LiveDecisionsPublicationState,
        openapi_extra=schema(LiveDecisionsPublication),
    )
    async def publish(request: Request):
        return invoke(
            "publish_result", await body(request, LiveDecisionsPublication, native, 16384)
        )

    @app.post(
        BASE + "/publication",
        dependencies=[Depends(read_outcome)],
        response_model=LiveDecisionsPublicationState,
        openapi_extra=schema(LiveDecisionsPublication),
    )
    async def publication(request: Request):
        return invoke(
            "lookup_publication", await body(request, LiveDecisionsPublication, read_outcome, 16384)
        )
