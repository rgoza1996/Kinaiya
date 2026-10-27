"""HTTP surface for the seed Agent.

``create_app(runtime, auth)`` builds the FastAPI app. ``auth`` is
``services.common.auth.Auth`` when the lead has provided it, else any object
exposing ``require(role)`` (see ``auth_adapter.StubAuth``). Only the owner
may submit, steer, interrupt, or resolve approvals over direct HTTP; the
decisions service identity will ride the same ``require`` seam once the
lead wires it.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from core.policy import ApprovalError, PolicyDenied
from packages.contracts.models import (
    ApprovalResolution,
    InterruptRequest,
    SteerRequest,
    TaskRequest,
)
from services.agent.auth_adapter import default_auth
from services.agent.runtime import (
    AgentRuntime,
    IdemConflict,
    StaleRevision,
    TaskNotFound,
    TaskTerminal,
)


def create_app(runtime: AgentRuntime, auth: Any = None) -> FastAPI:
    auth = auth if auth is not None else default_auth()
    require_owner = auth.require("owner")

    app = FastAPI(title="Kinaiya Seed Agent", version="0.1.0")

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
    async def create_task(body: TaskRequest, actor: str = Depends(require_owner)) -> Any:
        try:
            return await runtime.submit(body, actor=actor)
        except Exception as exc:  # mapped above
            raise _http(exc) from exc

    @app.get("/v1/agent/tasks")
    async def list_tasks(actor: str = Depends(require_owner)) -> Any:
        _ = actor
        return runtime.status(None)

    @app.get("/v1/agent/tasks/{task_id}")
    async def get_task(task_id: str, actor: str = Depends(require_owner)) -> Any:
        _ = actor
        try:
            result = runtime.status(task_id)
        except Exception as exc:
            raise _http(exc) from exc
        return result

    @app.post("/v1/agent/tasks/{task_id}/steer")
    async def steer_task(
        task_id: str, body: SteerRequest, actor: str = Depends(require_owner)
    ) -> Any:
        try:
            return await runtime.steer(task_id, body, actor=actor)
        except Exception as exc:
            raise _http(exc) from exc

    @app.post("/v1/agent/tasks/{task_id}/interrupt")
    async def interrupt_task(
        task_id: str, body: InterruptRequest, actor: str = Depends(require_owner)
    ) -> Any:
        try:
            return await runtime.interrupt(task_id, body, actor=actor)
        except Exception as exc:
            raise _http(exc) from exc

    @app.get("/v1/agents/capabilities")
    async def capabilities(actor: str = Depends(require_owner)) -> Any:
        _ = actor
        return {
            "harness": "seed-v1",
            "event_protocol": "1.0",
            "contract_schema": "1.0",
            "tools": ["read_file", "write_file", "search", "shell", "finish"],
            "steering": "safe-boundary",
            "cancellation": True,
            "approvals": {"digest_bound": True, "one_use": True, "expiring": True},
            "max_steps": runtime.max_steps,
        }

    @app.post("/v1/approvals/{approval_id}/resolve")
    async def resolve_approval(
        approval_id: str, body: ApprovalResolution, actor: str = Depends(require_owner)
    ) -> Any:
        try:
            return await runtime.resolve_approval(approval_id, body, actor=actor)
        except Exception as exc:
            raise _http(exc) from exc

    return app
