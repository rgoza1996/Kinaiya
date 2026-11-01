"""Owner libraries from native write receipts; no workspace traversal or execution."""

import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from packages.contracts.workspace_settings import WorkspacePin
from services.agent.projects import ProjectChange, ProjectConflict

DOCUMENT_TYPES = frozenset(
    "txt md csv tsv json yaml yml pdf doc docx xls xlsx ods odt ppt pptx rtf html".split()
)
MEDIA_TYPES = frozenset("png jpg jpeg webp gif svg mp3 wav ogg flac m4a mp4 webm mov".split())


def output_record(entry, workspace, kind, *, managed=False, active_pin=None):
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
    if (
        not path.name
        or len(path.name) > 1024
        or any(ord(character) < 32 for character in path.name)
    ):
        return None
    provenance = {}
    if managed:
        if "workspace_pin" not in payload:
            # The old absolute file evidence proves a write, not its original
            # root. A new root must never reinterpret that historical path.
            relative = None
            provenance = {"workspace_pin": None, "workspace_status": "historical_unknown"}
        else:
            try:
                pin = WorkspacePin.model_validate(payload["workspace_pin"])
                value = payload["workspace_relative_path"]
                if (
                    type(value) is not str
                    or pin.model_dump(mode="json") != payload["workspace_pin"]
                ):
                    return None
                relative = Path(value)
                if relative.is_absolute() or relative.name != path.name:
                    return None
                provenance = {
                    "workspace_pin": pin.model_dump(mode="json"),
                    "workspace_status": "current" if pin == active_pin else "historical",
                }
            except (ValueError, KeyError, TypeError):
                return None
    else:
        try:
            relative = path.relative_to(workspace)
        except ValueError:
            return None
    if relative is not None and (
        ".." in relative.parts
        or not relative.parts
        or len(str(relative)) > 1024
        or any(ord(character) < 32 for character in str(relative))
    ):
        return None
    extension = path.suffix.lstrip(".").lower()
    if extension not in (DOCUMENT_TYPES if kind == "documents" else MEDIA_TYPES):
        return None
    return {
        "event_id": entry.event_id,
        "sequence": entry.sequence,
        "name": path.name,
        "relative_path": None if relative is None else str(relative),
        "file_type": extension.upper(),
        "created_at": entry.created_at.isoformat(),
        "sha256": evidence["sha256"],
        "task_id": entry.task_refs[0],
        "session_id": entry.session_id,
        "thread_id": entry.thread_id,
        "epoch": entry.epoch,
        "objective_revision": evidence.get("objective_revision"),
        **provenance,
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
            managed = runtime.workspace_settings is not None
            active_pin = None
            if managed:
                try:
                    active_pin = runtime.workspace_settings.active_context().pin
                except RuntimeError:
                    pass
            records = [
                output_record(
                    entry, runtime.policy.workspace, kind, managed=managed, active_pin=active_pin
                )
                for entry in entries
            ]
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
