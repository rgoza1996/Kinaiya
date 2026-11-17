"""Internal Agent-only advice surface; the launcher does not install it yet."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.requests import ClientDisconnect

from packages.contracts.agent_advice import encoded
from packages.contracts.agent_advice_operations import (
    MAX_ADVICE_CALL_BYTES,
    MAX_ADVICE_LOOKUP_BYTES,
    MAX_ADVICE_OBSERVATION_BYTES,
    AgentAdviceCall,
    AgentAdviceLookup,
    AgentAdviceLookupResponse,
)
from services.decisions.agent_operations import AgentAdviceConflict, AgentAdviceUnavailable

BASE = "/v1/decisions/agent-advice"


def router(owner, auth):
    native = auth.require("agent")
    routes = APIRouter(prefix=BASE, dependencies=[Depends(native)])

    def refused(status, message):
        return HTTPException(status, message, headers={"Cache-Control": "no-store"})

    def envelope(request):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("authorization", "content-type", "content-length", "content-encoding")
        ):
            raise refused(400, "Agent advice requires one exact request envelope")

    async def body(request, model, maximum):
        envelope(request)
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise refused(415, "Agent advice requires identity JSON")
        declared = request.headers.get("content-length")
        if declared is not None and (
            "transfer-encoding" in request.headers
            or not declared.isascii()
            or not declared.isdigit()
            or len(declared) > 8
        ):
            raise refused(400, "Agent advice requires an exact body length")
        if declared is not None and int(declared) > maximum:
            raise refused(413, "Agent advice request exceeds its resource bound")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > maximum:
                        raise refused(413, "Agent advice request exceeds its resource bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise refused(408, "Agent advice ingress deadline expired") from None
        except ClientDisconnect:
            raise refused(400, "Agent advice ingress did not complete") from None
        if declared is not None and len(raw) != int(declared):
            raise refused(400, "Agent advice body length changed")
        try:
            original = model.from_json(bytes(raw))
        except (ValueError, RecursionError, UnicodeError):
            raise refused(422, "Agent advice requires its strict versioned shape") from None
        # Ingress may yield after initial authentication. Recheck the original
        # service credential before claiming a key or requesting cancellation.
        await native(request, request.headers.get("authorization", ""))
        return original

    def response(value, status=200):
        raw = encoded(value.model_dump(mode="json"))
        if len(raw) > MAX_ADVICE_OBSERVATION_BYTES:
            raise refused(503, "Agent advice observation exceeds its resource bound")
        return Response(
            raw,
            status_code=status,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    def invoke(method, original):
        try:
            return getattr(owner, method)(original)
        except AgentAdviceConflict:
            raise refused(409, "Original Agent advice identity changed") from None
        except AgentAdviceUnavailable:
            raise refused(503, "Original Agent advice custody is unavailable") from None

    @routes.get("/runtime")
    async def runtime(request: Request):
        envelope(request)
        return response(owner.runtime_snapshot())

    @routes.post("/requests")
    async def submit(request: Request):
        original = await body(request, AgentAdviceCall, MAX_ADVICE_CALL_BYTES)
        return response(invoke("submit", original), 202)

    @routes.post("/lookup")
    async def lookup(request: Request):
        original = await body(request, AgentAdviceLookup, MAX_ADVICE_LOOKUP_BYTES)
        return response(
            AgentAdviceLookupResponse(request=original, observation=invoke("lookup", original))
        )

    @routes.post("/cancel")
    async def cancel(request: Request):
        original = await body(request, AgentAdviceLookup, MAX_ADVICE_LOOKUP_BYTES)
        return response(
            AgentAdviceLookupResponse(request=original, observation=invoke("cancel", original))
        )

    return routes
