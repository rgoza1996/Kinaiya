"""Authenticated Session routes for external Agent grants and transcript pages."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from packages.contracts.external_agent import (
    MAX_EXTERNAL_COMMAND_BYTES,
    MAX_EXTERNAL_NATIVE_REQUEST_BYTES,
    MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES,
    ExternalAgentBind,
    ExternalAgentClaim,
    ExternalAgentConnect,
    ExternalAgentGrantLookup,
    ExternalAgentReadTranscript,
    ExternalAgentRefusal,
    ExternalAgentRevoke,
)
from services.common.external_agent_http import external_agent_body
from services.session.external_agent import (
    ExternalAgentAccess,
    ExternalAgentConflict,
    ExternalAgentScopeChanged,
    ExternalAgentUnavailable,
)


async def _invoke(incoming, model, method, limit=MAX_EXTERNAL_NATIVE_REQUEST_BYTES):
    request = await external_agent_body(incoming, model, limit)
    try:
        result = await run_in_threadpool(method, request)
        if result is None:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
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

    @native.post("/grant", dependencies=[decisions])
    async def lookup_grant(request: Request):
        return await _invoke(request, ExternalAgentGrantLookup, access.lookup_grant)

    @native.post("/revoke", dependencies=[owner])
    async def revoke(request: Request):
        return await _invoke(request, ExternalAgentRevoke, access.revoke)

    @native.post("/transcript", dependencies=[decisions])
    async def transcript(request: Request):
        return await _invoke(request, ExternalAgentReadTranscript, access.transcript)

    @native.post("/claim", dependencies=[decisions])
    async def claim(request: Request):
        return await _invoke(request, ExternalAgentClaim, access.claim, MAX_EXTERNAL_COMMAND_BYTES)

    @native.post("/claim/retained", dependencies=[decisions])
    async def retained_claim(request: Request):
        return await _invoke(
            request, ExternalAgentClaim, access.retained_claim, MAX_EXTERNAL_COMMAND_BYTES
        )

    @native.post("/claim/eligible", dependencies=[decisions])
    async def eligible_claim(request: Request):
        return await _invoke(
            request, ExternalAgentClaim, access.eligible_claim, MAX_EXTERNAL_COMMAND_BYTES
        )

    return native
