"""Owner-only artifact inspection and presentation state; never task execution."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from services.agent.artifacts import (
    MAX_SAFE_INTEGER,
    ArtifactConflict,
    ArtifactInvalid,
    ArtifactNotFound,
)


class ArtifactAction(BaseModel):
    """The exact interaction contract. Native storage validates component values."""

    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=MAX_SAFE_INTEGER)
    expected_revision: int = Field(ge=1, le=MAX_SAFE_INTEGER)
    component_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    value: Any
    idempotency_key: str = Field(min_length=1, max_length=128)


def router(runtime, auth):
    routes = APIRouter(prefix="/v1/artifacts", dependencies=[Depends(auth.require("owner"))])

    def store():
        if runtime.artifact_store is None:
            raise HTTPException(503, "UI artifacts are not configured")
        return runtime.artifact_store

    def envelope(value):
        # This order reference comes only from the authenticated native
        # submit journal receipt. Historical artifacts without a reconstructed
        # task remain browsable but cannot automatically replace a newer view.
        rec = runtime._tasks.get(value["task_id"])
        sequence = 0
        if rec is not None and (rec.task.session_id, rec.task.thread_id, rec.task.epoch) == (
            value["session_id"],
            value["thread_id"],
            value["epoch"],
        ):
            sequence = rec.submit_sequence
        return {**value, "task_submit_sequence": sequence}

    def error(exc):
        # Storage errors can include SQL, paths, or private data. Only fixed
        # public descriptions cross the owner HTTP boundary.
        if isinstance(exc, ArtifactNotFound):
            return HTTPException(404, "Artifact not found in this scope")
        if isinstance(exc, ArtifactConflict):
            return HTTPException(409, "Artifact revision or idempotency conflict")
        if isinstance(exc, ArtifactInvalid):
            return HTTPException(400, "Invalid artifact request")
        return HTTPException(503, "UI artifact storage is unavailable")

    @routes.get("")
    async def artifacts(
        session_id: str = Query(min_length=1, max_length=128),
        thread_id: str = Query(min_length=1, max_length=128),
        epoch: int = Query(ge=1, le=MAX_SAFE_INTEGER),
        after: int = Query(default=0, ge=0, le=MAX_SAFE_INTEGER),
        limit: int = Query(default=20, ge=1, le=20),
    ):
        artifact_store = store()
        try:
            page = artifact_store.page(
                session_id=session_id, thread_id=thread_id, epoch=epoch, after=after, limit=limit
            )
            return {**page, "items": [envelope(item) for item in page["items"]]}
        except Exception as exc:
            raise error(exc) from exc

    @routes.get("/{artifact_id}")
    async def artifact(
        artifact_id: str = Path(min_length=1, max_length=128),
        session_id: str = Query(min_length=1, max_length=128),
        thread_id: str = Query(min_length=1, max_length=128),
        epoch: int = Query(ge=1, le=MAX_SAFE_INTEGER),
    ):
        artifact_store = store()
        try:
            return envelope(
                artifact_store.get(
                    artifact_id, session_id=session_id, thread_id=thread_id, epoch=epoch
                )
            )
        except Exception as exc:
            raise error(exc) from exc

    @routes.post("/{artifact_id}/actions")
    async def action(body: ArtifactAction, artifact_id: str = Path(min_length=1, max_length=128)):
        artifact_store = store()
        try:
            return envelope(artifact_store.action(artifact_id, body.model_dump()))
        except Exception as exc:
            raise error(exc) from exc

    return routes
