"""Owner access to the managed external Pi attachment."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from packages.contracts.external_agent import (
    MAX_EXTERNAL_COMMAND_BYTES,
    ExternalAgentAttach,
    ExternalAgentClaim,
    ExternalAgentDisconnect,
    ExternalAgentInspect,
    ExternalAgentRefusal,
    ExternalAgentReplay,
)
from services.common.external_agent_http import external_agent_body


def _reply(result):
    status = 200
    if isinstance(result, ExternalAgentRefusal):
        status = {"unauthorized": 403, "unavailable": 503}.get(result.reason, 409)
    return Response(
        result.model_dump_json(),
        media_type="application/json",
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def router(manager, auth):
    native = APIRouter(prefix="/v1/decisions/external-agent")
    owner = Depends(auth.require("owner"))
    decisions = Depends(auth.require("decisions"))

    @native.post("/attach", dependencies=[owner])
    async def attach(request: Request):
        return _reply(await manager.attach(await external_agent_body(request, ExternalAgentAttach)))

    @native.post("/status", dependencies=[owner])
    async def status(request: Request):
        return _reply(
            await manager.status(await external_agent_body(request, ExternalAgentInspect))
        )

    @native.post("/disconnect", dependencies=[owner])
    async def disconnect(request: Request):
        return _reply(
            await manager.disconnect(await external_agent_body(request, ExternalAgentDisconnect))
        )

    @native.post("/events", dependencies=[owner])
    async def events(request: Request):
        return _reply(await manager.events(await external_agent_body(request, ExternalAgentReplay)))

    @native.post("/submit", dependencies=[decisions])
    async def submit(request: Request):
        original = await external_agent_body(
            request, ExternalAgentClaim, MAX_EXTERNAL_COMMAND_BYTES
        )
        return _reply(await manager.submit(original))

    return native
