"""Native workspace ownership across request parsing and actual operation cleanup."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from core.policy import PolicyDenied

_REQUEST = ContextVar("agent_workspace_request", default=None)
MAX_WORKSPACE_OPERATIONS = 64


@dataclass(eq=False)
class WorkspaceRequest:
    runtime: object
    context: object
    active: bool = True


def captured_context(runtime, *, required=True):
    owner = runtime.workspace_settings
    if owner is None:
        return None
    request = _REQUEST.get()
    if request is not None:
        if request.runtime is not runtime or not request.active:
            raise PolicyDenied("Workspace request ownership was retired")
        context = request.context
        if context is None:
            if required:
                raise PolicyDenied("This request has no authorized workspace")
            return None
        if owner.active_context() is not context:
            raise PolicyDenied("The request targets a retired workspace")
        return context
    try:
        return owner.active_context()
    except RuntimeError:
        if required:
            raise PolicyDenied("Native workspace authority is unavailable") from None
        return None


@dataclass(frozen=True)
class PreparedWorkspaceInstall:
    validate: object
    install: object

    def current(self):
        return bool(self.validate())

    def apply(self):
        # The store already checked this immediately before its commit. No
        # asynchronous or filesystem validation belongs in this installer.
        return self.install()


class WorkspaceRequestOwnership:
    """Reserve before Starlette awaits body/auth; no request selects a new root."""

    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def _workspace_body(self, scope, receive, send):
        """Bound raw owner-save/lookup ingress before FastAPI parses JSON."""
        deadline = asyncio.get_running_loop().time() + 30
        body = bytearray()
        status = None
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > 65_536:
                        status = 413
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            status = 408
        if status is not None:
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"cache-control", b"no-store"),
                        (b"content-type", b"application/json"),
                    ],
                }
            )
            detail = (
                b'{"detail":"Workspace request body exceeds 64 KiB"}'
                if status == 413
                else b'{"detail":"Workspace request body timed out"}'
            )
            await send({"type": "http.response.body", "body": detail})
            return
        frozen = bytes(body)
        first = True

        async def replay():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": frozen, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path
            in {
                "/v1/agent/options/workspace-settings",
                "/v1/agent/options/workspace-changes/lookup",
            }
        ):
            # Save/lookup must not reserve a worker-root request and make their
            # own settled-service barrier permanently busy.
            return await self._workspace_body(scope, receive, send)
        owned = (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and (
                path == "/v1/agent/tasks"
                or path == "/v1/commands/execute"
                or path.startswith("/v1/harness/")
                or path in {"/v1/mcp/control", "/v1/skills/control", "/v1/plugins/control"}
                or path.startswith("/v1/skills/")
                and path.endswith("/invoke")
            )
        )
        runtime = self.runtime
        if not owned or runtime.workspace_settings is None:
            return await self.app(scope, receive, send)
        if runtime._closed or len(runtime._workspace_requests) >= MAX_WORKSPACE_OPERATIONS:
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send(
                {"type": "http.response.body", "body": b"Workspace request capacity unavailable"}
            )
            return
        try:
            context = runtime.workspace_settings.active_context()
        except RuntimeError:
            context = None
        request = WorkspaceRequest(runtime, context)
        runtime._workspace_requests.add(request)
        token = _REQUEST.set(request)
        body_deadline = asyncio.get_running_loop().time() + 30

        async def bounded_receive():
            # A slow body owns its original context only for this finite wait.
            async with asyncio.timeout_at(body_deadline):
                return await receive()

        try:
            await self.app(scope, bounded_receive, send)
        finally:
            request.active = False
            _REQUEST.reset(token)
            runtime._workspace_requests.discard(request)
            runtime.workspace_settings.release_if_settled()
