"""Owner-only explicit resource actions. No arbitrary process parameters."""

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResourceAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["profile", "handoff", "restore", "converse", "task_finished"]
    idempotency_key: str = Field(min_length=1, max_length=256)
    profile: (
        Literal["conversation_priority", "local_work_priority", "local_only", "configured_hybrid"]
        | None
    ) = None
    enabled: bool | None = None
    required_bytes: int = Field(default=0, ge=0, le=1 << 46)
    task_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    reason: str = Field(default="Explicit owner resource control", max_length=160)

    @model_validator(mode="after")
    def valid_operation_fields(self):
        allowed = {
            "profile": {"profile"},
            "handoff": {"required_bytes", "task_id", "reason"},
            "restore": set(),
            "converse": {"enabled"},
            "task_finished": {"task_id"},
        }
        if self.model_fields_set - {"operation", "idempotency_key"} - allowed[self.operation]:
            raise ValueError("Fields do not apply to this resource operation")
        required = {"profile": "profile", "converse": "enabled", "task_finished": "task_id"}.get(
            self.operation
        )
        if required and getattr(self, required) is None:
            raise ValueError("Missing required resource operation field")
        return self


def create_app(coordinator, auth):
    app = FastAPI(title="Kinaiya local resources")
    owner = [Depends(auth.require("owner"))]

    @app.get("/health")
    async def health():
        return {"service": "resources", "phase": coordinator.state.record.phase}

    @app.get("/v1/resources", dependencies=owner)
    async def status():
        return coordinator.status()

    @app.post("/v1/resources/actions", dependencies=owner)
    async def action(body: ResourceAction):
        if body.operation == "converse" and body.enabled is None:
            raise HTTPException(400, "converse requires enabled")
        try:
            return await coordinator.action(body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app
