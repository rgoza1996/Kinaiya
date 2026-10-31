"""Owner libraries from native write receipts; no workspace traversal or execution."""

import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from services.agent.projects import ProjectChange, ProjectConflict

DOCUMENT_TYPES = frozenset(
    "txt md csv tsv json yaml yml pdf doc docx xls xlsx ods odt ppt pptx rtf html".split()
)
MEDIA_TYPES = frozenset("png jpg jpeg webp gif svg mp3 wav ogg flac m4a mp4 webm mov".split())


def output_record(entry, workspace, kind):
    payload = entry.payload
    evidence = payload.get("evidence")
    if (
        entry.origin != "agent-supervisor"
        or entry.kind != "agent.tool"
        or payload.get("tool") != "write_file"
        or payload.get("ok") is not True
        or not isinstance(evidence, dict)
        or evidence.get("kind") != "file"
        or type(evidence.get("path")) is not str
        or re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("sha256", ""))) is None
        or len(entry.task_refs) != 1
    ):
        return None
    # Lexical historic metadata only. No stat, read, symlink walk or download.
    path = Path(evidence["path"])
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        return None
    if ".." in relative.parts or not relative.parts or len(str(relative)) > 1024:
        return None
    extension = path.suffix.lstrip(".").lower()
    if extension not in (DOCUMENT_TYPES if kind == "documents" else MEDIA_TYPES):
        return None
    return {
        "event_id": entry.event_id,
        "sequence": entry.sequence,
        "name": path.name,
        "relative_path": str(relative),
        "file_type": extension.upper(),
        "created_at": entry.created_at.isoformat(),
        "sha256": evidence["sha256"],
        "task_id": entry.task_refs[0],
        "session_id": entry.session_id,
        "thread_id": entry.thread_id,
        "epoch": entry.epoch,
        "objective_revision": evidence.get("objective_revision"),
    }


def router(runtime, auth, owner_journal):
    routes = APIRouter(prefix="/v1/agent", dependencies=[Depends(auth.require("owner"))])

    @routes.get("/library")
    async def library(
        kind: Literal["documents", "media"],
        after: int = Query(default=0, ge=0, le=9_007_199_254_740_990),
    ):
        try:
            entries = await owner_journal.read(after=after, limit=100, kinds=["agent.tool"])
            records = [output_record(entry, runtime.policy.workspace, kind) for entry in entries]
            return {
                "schema_version": "1.0",
                "kind": kind,
                "items": [record for record in records if record is not None],
                "next_cursor": entries[-1].sequence if entries else None,
            }
        except Exception as exc:
            raise HTTPException(503, "Generated output records are unavailable") from exc

    def projects():
        if getattr(runtime, "projects", None) is None:
            raise HTTPException(503, "Project storage is not configured")
        return runtime.projects

    @routes.get("/projects")
    async def list_projects(after: str | None = Query(default=None, pattern=r"^[a-f0-9-]{36}$")):
        try:
            return projects().page(after=after)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Project records are unavailable") from exc

    @routes.get("/projects/changes/{key}")
    async def project_receipt(key: str):
        if len(key) > 128:
            raise HTTPException(400, "Invalid project receipt key")
        result = projects().receipt(key)
        if result is None:
            raise HTTPException(404, "No committed project receipt is visible")
        return result

    @routes.post("/projects")
    async def change_project(body: ProjectChange):
        store = projects()
        try:
            # A committed exact request can be recovered even if its old session
            # was subsequently removed. The store still checks the body digest.
            if body.session_id is not None and store.receipt(body.idempotency_key) is None:
                session = await owner_journal.session(body.session_id)
                if session.channel != "agent":
                    raise ValueError("Only an Agent session belongs in a project")
            return store.change(body)
        except ProjectConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, "Invalid project or Agent session selection") from exc
        except Exception as exc:
            raise HTTPException(503, "Project change could not be confirmed") from exc

    return routes
