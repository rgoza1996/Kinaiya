"""A distinct authenticated export boundary; legacy Agent admission stays Agent-only."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from packages.contracts.agent_advice import MAX_ADVICE_EXPORT_BYTES, AgentAdviceExportRequest
from services.session.agent_advice import AgentAdviceConflict, AgentAdviceUnavailable, admit_export


def router(store, auth):
    routes = APIRouter()

    def refused(status, message):
        return HTTPException(status, message, headers={"Cache-Control": "no-store"})

    @routes.post(
        "/v1/transcript/agent-advice-export-admission",
        dependencies=[Depends(auth.require("decisions"))],
    )
    async def admit(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise refused(415, "Agent advice export requires identity JSON")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_ADVICE_EXPORT_BYTES:
                        raise refused(413, "Agent advice export metadata exceeds its bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise refused(408, "Agent advice export ingress deadline expired") from None
        try:
            original = AgentAdviceExportRequest.from_json(bytes(raw))
        except (ValueError, RecursionError, UnicodeError):
            raise refused(422, "Agent advice export requires its strict versioned shape") from None
        try:
            result = admit_export(store, original)
        except AgentAdviceConflict:
            raise refused(409, "Original Agent advice task authority changed") from None
        except PermissionError:
            raise refused(403, "Agent advice export disclosure was refused") from None
        except AgentAdviceUnavailable:
            raise refused(503, "Native Agent advice export authority is unavailable") from None
        return Response(
            result.canonical_bytes(),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return routes
