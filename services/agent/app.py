"""HTTP surface for the seed Agent.

``create_app(runtime, auth)`` builds the FastAPI app. ``auth`` must be a
configured ``services.common.auth.Auth``; there is no fallback. Passing
``None`` (or anything without ``require``) fails closed at construction so
the service can never listen without real Bearer [REDACTED] Owner and the
decisions service identity may submit, steer, interrupt, and read status;
only the owner may resolve approvals. The decisions actor must additionally
carry validated journal source references, enforced by the runtime.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from core.policy import ApprovalError, PolicyDenied
from packages.commands import Registry
from packages.contracts.models import (
    ApprovalResolution,
    CommandRequest,
    InterruptRequest,
    SteerRequest,
    TaskRequest,
)
from services.agent.runtime import (
    AgentRuntime,
    IdemConflict,
    StaleRevision,
    TaskNotFound,
    TaskTerminal,
)


def create_app(runtime: AgentRuntime, auth: Any, commands=None) -> FastAPI:
    if auth is None or not hasattr(auth, "require"):
        raise RuntimeError(
            "seed Agent HTTP requires a configured Auth; "
            "refusing to start without bearer verification"
        )
    require_owner = auth.require("owner")
    require_actor = auth.require("owner", "decisions")
    commands = commands or Registry(runtime)

    app = FastAPI(title="Kinaiya Seed Agent", version="0.1.0")
    from services.agent.harness_api import router as harness_router

    app.include_router(harness_router(runtime, auth))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(auth.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/health")
    async def health():
        return {
            "service": "agent",
            "provider": runtime.provider.info.model_dump(),
            "status": "ready",
        }

    def _http(exc: Exception) -> HTTPException:
        if isinstance(exc, TaskNotFound):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, (StaleRevision, IdemConflict, TaskTerminal)):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, (PolicyDenied, ApprovalError)):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.post("/v1/agent/tasks", status_code=201)
    async def create_task(body: TaskRequest, actor: str = Depends(require_actor)) -> Any:
        try:
            return await runtime.submit(body, actor=actor)
        except Exception as exc:  # mapped above
            raise _http(exc) from exc

    @app.get("/v1/agent/tasks")
    async def list_tasks(actor: str = Depends(require_actor)) -> Any:
        _ = actor
        return await runtime.status(None)

    @app.get("/v1/agent/tasks/{task_id}")
    async def get_task(task_id: str, actor: str = Depends(require_actor)) -> Any:
        _ = actor
        try:
            result = await runtime.status(task_id)
        except Exception as exc:
            raise _http(exc) from exc
        return result

    @app.post("/v1/agent/tasks/{task_id}/steer")
    async def steer_task(
        task_id: str, body: SteerRequest, actor: str = Depends(require_actor)
    ) -> Any:
        try:
            return await runtime.steer(task_id, body, actor=actor)
        except Exception as exc:
            raise _http(exc) from exc

    @app.post("/v1/agent/tasks/{task_id}/interrupt")
    async def interrupt_task(
        task_id: str, body: InterruptRequest, actor: str = Depends(require_actor)
    ) -> Any:
        try:
            return await runtime.interrupt(task_id, body, actor=actor)
        except Exception as exc:
            raise _http(exc) from exc

    @app.get("/v1/agents/capabilities")
    async def capabilities(actor: str = Depends(require_actor)) -> Any:
        _ = actor
        tools = ["read_file", "write_file", "search", "shell", "read_transcript", "finish"]
        if runtime.selector_registry:
            tools.append("select_context")
        if runtime.mcp_host is not None:
            # MCP tools exist only when owner-configured servers are present.
            tools += ["mcp_list", "mcp_call"]
        return {
            "harness": runtime.selector_registry.active if runtime.selector_registry else "seed-v1",
            "event_protocol": "1.0",
            "contract_schema": "1.0",
            "tools": tools,
            "steering": "safe-boundary",
            "cancellation": True,
            "approvals": {"digest_bound": True, "one_use": True, "expiring": True},
            "max_steps": runtime.max_steps,
        }

    @app.get("/v1/mcp/servers")
    async def mcp_servers(actor: str = Depends(require_owner)) -> Any:
        """Owner-only MCP server metadata. No grant-changing API.

        Returns bounded owner configuration metadata. Resolved secret values
        are never exposed. Model-facing discovery uses a separate projection.
        """
        _ = actor
        host = runtime.mcp_host
        if host is None:
            return {"servers": []}
        return {"servers": host.list_servers()}

    @app.post("/v1/approvals/{approval_id}/resolve")
    async def resolve_approval(
        approval_id: str, body: ApprovalResolution, actor: str = Depends(require_owner)
    ) -> Any:
        try:
            return await runtime.resolve_approval(approval_id, body, actor=actor)
        except Exception as exc:
            raise _http(exc) from exc

    @app.get("/v1/commands", dependencies=[Depends(require_owner)])
    async def command_definitions():
        return commands.definitions()

    @app.post("/v1/commands/execute")
    async def command_execute(body: CommandRequest, actor: str = Depends(require_owner)):
        try:
            return await commands.execute(body, actor)
        except Exception as exc:
            raise _http(exc) from exc

    return app
