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

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from core.policy import ApprovalError, PolicyDenied
from packages.commands import Registry
from packages.contracts.models import (
    ApprovalResolution,
    CommandRequest,
    InterruptRequest,
    PendingApprovalPage,
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
    from services.agent.artifact_api import router as artifact_router
    from services.agent.harness_api import router as harness_router
    from services.agent.library_api import router as library_router
    from services.agent.mcp_controls import router as mcp_router
    from services.agent.options_api import router as options_router
    from services.agent.plugin_controls import router as plugin_router
    from services.agent.skill_controls import router as skill_router
    from services.agent.workspace_api import router as workspace_router

    app.include_router(harness_router(runtime, auth))
    app.include_router(options_router(runtime, auth))
    app.include_router(library_router(runtime, auth, getattr(commands, "journal", runtime.journal)))
    app.include_router(
        workspace_router(runtime, auth, getattr(commands, "journal", runtime.journal))
    )
    app.include_router(mcp_router(runtime, auth))
    app.include_router(skill_router(runtime, auth))
    app.include_router(plugin_router(runtime, auth))
    app.include_router(artifact_router(runtime, auth))
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
            "provider": runtime.model_settings.default_info()
            if runtime.model_settings is not None
            else runtime.provider.info.model_dump(),
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
    async def capabilities(
        task_id: str | None = Query(default=None, min_length=1, max_length=128),
        actor: str = Depends(require_actor),
    ) -> Any:
        _ = actor
        try:
            task = await runtime.status(task_id) if task_id is not None else None
        except Exception as exc:
            raise _http(exc) from exc
        snapshot = runtime.tool_snapshot(task)
        return {
            "harness": task.harness_version
            if task is not None
            else runtime.selector_registry.active
            if runtime.selector_registry
            else "seed-v1",
            "event_protocol": "1.0",
            "contract_schema": "1.0",
            "tools": list(snapshot.available_tools),
            "tool_catalog": {
                "schema_version": snapshot.schema_version,
                "scope": "native_gateways",
                "task_id": task_id,
                "task_state": task.state.value if task is not None else None,
                "items": [
                    {
                        "name": descriptor.name,
                        "qualified_id": descriptor.qualified_id,
                        "protocol_version": descriptor.protocol_version,
                        "kind": descriptor.kind,
                    }
                    for descriptor in snapshot.descriptors
                ],
            },
            "steering": "safe-boundary",
            "cancellation": True,
            "approvals": {"digest_bound": True, "one_use": True, "expiring": True},
            "max_steps": runtime.max_steps,
        }

    @app.get("/v1/agents/tool-catalog")
    async def tool_catalog(
        request: Request,
        actor: str = Depends(require_actor),
    ) -> Any:
        _ = actor
        # Parse GET's string representation explicitly: Pydantic's ordinary
        # integer coercions and query echo errors are not the catalog contract.
        pairs = list(request.query_params.multi_items())
        allowed = {"kind", "name", "server_id", "query", "after", "task_id"}
        if len(pairs) != len({key for key, _ in pairs}) or any(
            key not in allowed for key, _ in pairs
        ):
            raise HTTPException(400, "Tool catalog failed: invalid_query")
        arguments = dict(pairs)
        task_id = arguments.pop("task_id", None)
        if task_id is not None and (not 1 <= len(task_id) <= 128 or "\x00" in task_id):
            raise HTTPException(400, "Tool catalog failed: invalid_query")
        if "after" in arguments:
            after = arguments["after"]
            if not 1 <= len(after) <= 7 or any(char not in "0123456789" for char in after):
                raise HTTPException(400, "Tool catalog failed: invalid_query")
            arguments["after"] = int(after)
        try:
            return await runtime.inspect_tools(arguments, task_id=task_id)
        except Exception as exc:
            from packages.contracts.tool_catalog import CatalogError

            if isinstance(exc, CatalogError):
                raise HTTPException(400, "Tool catalog failed: " + exc.code) from exc
            if isinstance(exc, TaskNotFound):
                raise HTTPException(404, "Task not found") from exc
            raise HTTPException(503, "Tool catalog is unavailable") from exc

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

    @app.get("/v1/resources")
    async def resources(actor: str = Depends(require_owner)):
        if runtime.resource_client is None:
            return {"configured": False}
        try:
            return await runtime.resource_client.status()
        except Exception as exc:
            raise HTTPException(503, "Resource coordination is unavailable") from exc

    @app.get("/v1/approvals", response_model=PendingApprovalPage)
    async def pending_approvals(
        after: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=100, ge=1, le=100),
        actor: str = Depends(require_owner),
    ):
        _ = actor
        try:
            return await runtime.pending_approvals(after=after, limit=limit)
        except Exception as exc:
            raise _http(exc) from exc

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
        await commands.refresh_skill_commands()
        return commands.definitions()

    @app.post("/v1/commands/execute")
    async def command_execute(body: CommandRequest, actor: str = Depends(require_owner)):
        try:
            return await commands.execute(body, actor)
        except Exception as exc:
            raise _http(exc) from exc

    return app
