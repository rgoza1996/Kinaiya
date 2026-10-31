"""Owner inspection/association, separate from protected harness activation."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core.extensions import HarnessError
from packages.contracts.agent_workspace import AgentSessionCreate
from packages.contracts.models import SessionRequest


def router(runtime, auth, owner_journal):
    routes = APIRouter(prefix="/v1/agent", dependencies=[Depends(auth.require("owner"))])

    @routes.get("/harnesses")
    async def harnesses(
        limit: int = Query(default=20, ge=1, le=50),
        after: str | None = Query(default=None, pattern=r"^(seed-v1|v-[a-f0-9]{24})$"),
    ):
        registry = runtime.selector_registry
        if registry is None:
            items = (
                []
                if after is not None and after >= "seed-v1"
                else [
                    {
                        "version_id": "seed-v1",
                        "kind": "seed_runtime",
                        "name": "Seed Agent",
                        "description": "Fixed native Agent runtime without a generated context selector.",
                        "active": True,
                        "selectable": True,
                        "eligibility": "active",
                        "registered_at": None,
                    }
                ]
            )
            return {
                "schema_version": "1.0",
                "configured": False,
                "items": items,
                "next_cursor": None,
            }
        try:
            page = registry.selection_page(after=after, limit=limit)
        except (HarnessError, OSError, ValueError) as exc:
            raise HTTPException(503, "Protected harness catalog is unavailable") from exc
        return {
            "schema_version": "1.0",
            "configured": True,
            "items": [
                {
                    "version_id": item["version_id"],
                    "kind": "context_selector.v1",
                    "name": item["manifest"]["name"],
                    "description": item["manifest"]["description"],
                    "registered_at": item["registered_at"],
                    "active": item["active"],
                    "eligibility": item["eligibility"],
                    "selectable": item["eligibility"]
                    in {"active", "approved_baseline", "owner_promoted"},
                }
                for item in page["items"]
            ],
            "next_cursor": page["next_cursor"],
        }

    @routes.post("/sessions", status_code=201)
    async def create_session(request: AgentSessionCreate):
        async with runtime.integration_lock:
            try:
                runtime.session_harness(request.harness_version)
            except (HarnessError, OSError, ValueError) as exc:
                raise HTTPException(
                    409, "Selected harness is not eligible for new Agent work"
                ) from exc
            try:
                session = await owner_journal.create_session(
                    SessionRequest(
                        channel="agent", name=request.name, harness_version=request.harness_version
                    )
                )
                if session.channel != "agent" or session.harness_version != request.harness_version:
                    raise HTTPException(
                        502, "Created session identity has a different harness scope"
                    )
                return session
            except PermissionError as exc:
                raise HTTPException(503, "Owner session creation is unavailable") from exc

    return routes
