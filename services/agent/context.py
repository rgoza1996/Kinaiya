"""Trusted workspace broker for the optional, pinned pure selector extension."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from core.extensions import HarnessError
from core.policy import PolicyDenied
from packages.contracts.models import Evidence
from services.agent.tools import ToolFailure


async def select_context(runtime, task, arguments):
    registry = runtime.selector_registry
    if registry is None or task.harness_version == "seed-v1":
        raise ToolFailure("No executable selector configured for this task")
    budget = arguments.get("max_chars", 4000)
    if not isinstance(budget, int) or isinstance(budget, bool) or not 1 <= budget <= 8000:
        raise ToolFailure("select_context max_chars must be 1..8000 characters")
    root = runtime.policy.resolve_path(arguments.get("path", "."))
    if not root.is_dir():
        raise ToolFailure("select_context path must be a workspace directory")
    files, visited = [], 0
    for directory, children, names in os.walk(root, followlinks=False):
        children[:] = sorted(c for c in children if not (Path(directory) / c).is_symlink())[:64]
        for name in sorted(names):
            visited += 1
            if visited > 512 or len(files) >= 32:
                break
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                raw = runtime.policy.read_bytes(path, maximum=2048)
                text = raw.decode("utf-8")
            except (OSError, PolicyDenied, UnicodeError):
                continue
            if "\x00" not in text:
                files.append(
                    {
                        "id": f"f{len(files)}",
                        "path": str(path.relative_to(runtime.policy.workspace)),
                        "text": text,
                    }
                )
        if visited > 512 or len(files) >= 32:
            break
    if not files:
        raise ToolFailure("No readable text files in the bounded workspace scan")
    payload = {"query": task.objective, "files": files, "max_chars": budget}
    try:
        ids = await asyncio.to_thread(registry.run, task.harness_version, payload)
    except HarnessError as exc:
        raise ToolFailure(f"Pinned selector failed; owner may review or roll back: {exc}") from exc
    selected = [next(f for f in files if f["id"] == fid) for fid in ids]
    references = [
        {"id": f["id"], "path": f["path"], "sample_chars": len(f["text"])} for f in selected
    ]
    output = json.dumps(
        {
            "untrusted_reference_context": references,
            "selector_version": task.harness_version,
            "budget_unit": "characters",
            "next": "Read useful selected paths on demand with read_file",
        }
    )
    # A core-authored read receipt proves retrieval only, never an external effect.
    return output, Evidence(
        kind="read",
        summary="Retrieved bounded workspace context using pinned selector",
        sha256=hashlib.sha256(output.encode()).hexdigest(),
        objective_revision=task.objective_revision,
    )
