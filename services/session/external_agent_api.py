"""Authenticated Session routes for external Agent grants and transcript pages."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from packages.contracts.external_agent import (
    MAX_EXTERNAL_NATIVE_REQUEST_BYTES,
    MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES,
    ExternalAgentBind,
    ExternalAgentConnect,
    ExternalAgentReadTranscript,
    ExternalAgentRefusal,
    ExternalAgentRevoke,
)
from packages.contracts.transcript_scan import transcript_json
from services.session.external_agent import (
    ExternalAgentAccess,
    ExternalAgentConflict,
    ExternalAgentScopeChanged,
    ExternalAgentUnavailable,
)


async def _body(incoming, model):
    if incoming.scope.get("query_string"):
        raise HTTPException(422, "External Agent native operations take a JSON body")
    raw = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in incoming.stream():
                if len(raw) + len(chunk) > MAX_EXTERNAL_NATIVE_REQUEST_BYTES:
                    raise HTTPException(413, "External Agent request exceeds its bound")
                raw.extend(chunk)
        bounded = bytes(raw)
        transcript_json(bounded, MAX_EXTERNAL_NATIVE_REQUEST_BYTES)
        return model.model_validate_json(bounded, strict=True)
    except TimeoutError:
        raise HTTPException(408, "External Agent request body is unavailable") from None
    except (ValueError, RecursionError):
        raise HTTPException(422, "Invalid external Agent request") from None


async def _invoke(incoming, model, method):
    request = await _body(incoming, model)
    try:
        result = await run_in_threadpool(method, request)
        body = result.model_dump_json().encode("utf-8")
        if len(body) > MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES:
            raise ExternalAgentUnavailable
        return Response(body, media_type="application/json", headers={"Cache-Control": "no-store"})
    except ExternalAgentScopeChanged:
        status, reason = 409, "scope_changed"
    except ExternalAgentConflict:
        status, reason = 409, "conflict"
    except ExternalAgentUnavailable:
        status, reason = 503, "unavailable"
    result = ExternalAgentRefusal(
        contract_version=1, request_id=request.request_id, kind="refusal", reason=reason
    )
    return Response(
        result.model_dump_json(),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def router(store, auth):
    access = ExternalAgentAccess(store)
    native = APIRouter(prefix="/v1/external-agent")
    owner = Depends(auth.require("owner"))
    decisions = Depends(auth.require("decisions"))

    @native.post("/authorize", dependencies=[owner])
    async def authorize(request: Request):
        return await _invoke(request, ExternalAgentConnect, access.authorize)

    @native.post("/bind", dependencies=[decisions])
    async def bind(request: Request):
        return await _invoke(request, ExternalAgentBind, access.bind)

    @native.post("/revoke", dependencies=[owner])
    async def revoke(request: Request):
        return await _invoke(request, ExternalAgentRevoke, access.revoke)

    @native.post("/transcript", dependencies=[decisions])
    async def transcript(request: Request):
        return await _invoke(request, ExternalAgentReadTranscript, access.transcript)

    return native
