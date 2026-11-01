"""Owner management shared by the API, Agent CLI and Agent settings."""

from fastapi import APIRouter, Depends, HTTPException

from packages.contracts.models import TERMINAL_STATES, MCPControl


class MCPManagementBusy(ValueError):
    pass


def require_mcp_controller(runtime):
    if runtime.mcp_controller is None:
        raise ValueError("MCP management is not enabled in owner configuration")
    return runtime.mcp_controller


def mcp_snapshot(runtime):
    controller = runtime.mcp_controller
    if controller is None:
        return {"configured": False, "revision": 0, "blocked": False, "servers": []}
    return {"configured": True, **controller.snapshot()}


async def apply_mcp_control(runtime, request: MCPControl):
    return await runtime.owned_workspace_operation(
        lambda: _apply_mcp_control(runtime, request),
        require_root=request.operation in {"test", "reconnect"},
    )


async def _apply_mcp_control(runtime, request):
    controller = require_mcp_controller(runtime)
    async with runtime.integration_lock:
        if runtime._closed:
            raise ValueError("Agent is closing")
        active = [task for task in await runtime.status() if task.state not in TERMINAL_STATES]
        if active:
            raise MCPManagementBusy(
                "Finish or stop active and recoverable tasks before managing MCP"
            )
        result = await controller.control(
            request.operation,
            actor="owner",
            idempotency_key=request.idempotency_key,
            expected_revision=request.expected_revision,
            server_id=request.server_id,
            config=request.config,
            reviewed=request.reviewed,
        )
        return result


def router(runtime, auth):
    routes = APIRouter(prefix="/v1/mcp", dependencies=[Depends(auth.require("owner"))])

    @routes.get("/management")
    async def snapshot():
        return mcp_snapshot(runtime)

    @routes.post("/control")
    async def control(body: MCPControl):
        try:
            return await apply_mcp_control(runtime, body)
        except MCPManagementBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return routes
