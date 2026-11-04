"""Fixed authenticated media storage routes on ordinary Session.

No route accepts a native producer claim, decoder availability or caller path.
Actual operations survive response cancellation/deadlines in this registry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.responses import Response

from packages.contracts import media as m
from services.common.media_wire import MediaWireError, open_ingress
from services.session.media_clients import MediaSessionError, private_header, refuse
from services.session.media_owner import MediaSessionOwner
from services.session.media_spool import MediaSpoolError

_STATUS = {
    "bad_packet": 400,
    "denied": 403,
    "unknown_ref": 404,
    "stale": 409,
    "conflict": 409,
    "expired": 410,
    "unsupported": 415,
    "bound": 413,
    "capacity": 429,
    "unavailable": 503,
}


def _response(value, *, status=200, headers=None):
    return Response(
        m.media_bytes(value),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def _error(error):
    code = getattr(error, "code", "unavailable")
    if isinstance(error, HTTPException):
        code = "denied"
    if code not in _STATUS:
        code = "unavailable"
    return _response(m.MediaError(v=1, error=code), status=_STATUS[code])


async def _json(request, kind):
    if request.headers.get("content-encoding", "identity") != "identity":
        refuse("unsupported")
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        refuse("unsupported")
    raw = bytearray()
    limit = 262_144 if kind is m.MediaAnchorAdmission else m.MAX_MEDIA_CONTROL_BYTES
    loop = asyncio.get_running_loop()
    deadline = loop.time() + (
        25.0 if kind in {m.MediaAdmit, m.MediaAnchorAdmission, m.MediaSealRequest} else 5.0
    )
    fetched = 0
    async with asyncio.timeout_at(deadline):
        async for chunk in request.stream():
            if loop.time() >= deadline:
                refuse()
            fetched = (fetched + 1) % 64
            if fetched == 0:
                await asyncio.sleep(0)
            if len(raw) + len(chunk) > limit:
                refuse("bound")
            raw.extend(chunk)
    if loop.time() >= deadline:
        refuse()
    return m.media_json(bytes(raw), kind)


@dataclass(slots=True, eq=False)
class _Original:
    task: asyncio.Task | None = None


class MediaOperations:
    def __init__(self, owner):
        self._owner = owner
        self._operations = set()
        self._closed = False
        self._close_confirmed = False

    @property
    def owner(self):
        return self._owner

    def store_close_confirmed(self) -> bool:
        return self._close_confirmed is True

    async def run(self, factory, deadline):
        if self._closed or len(self._operations) >= 16:
            refuse("unavailable" if self._closed else "capacity")
        original = _Original()
        self._operations.add(original)  # Before eager task creation/OS work.
        try:
            original.task = asyncio.create_task(factory())
        except BaseException:
            self._operations.discard(original)
            raise

        def done(task):
            if not task.cancelled():
                task.exception()  # Consume failures; never log request secrets.
            self._operations.discard(original)

        original.task.add_done_callback(done)
        done_tasks, _ = await asyncio.wait({original.task}, timeout=deadline)
        if not done_tasks:
            refuse()
        return original.task.result()

    async def close(self):
        self._closed = True
        self.owner._closing = True
        self.owner._fence_all_memory("disconnect")
        tasks = {original.task for original in self._operations if original.task is not None}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            if pending:
                return False
        self._close_confirmed = True
        return True


async def _session_exception_handler(request, error):
    return _error(error)


def install_media_exception_handler(app):
    """Keep fixed refusal mapping when an old native owner remains installed."""
    existing = app.exception_handlers.get(MediaSessionError)
    if existing is not None and existing is not _session_exception_handler:
        raise ValueError("Native media error handler is already registered")
    app.add_exception_handler(MediaSessionError, _session_exception_handler)


def install_media_routes(app, store, auth, owner):
    if type(owner) is not MediaSessionOwner or owner.store is not store or owner.auth is not auth:
        raise ValueError("Invalid native media route owner")
    install_media_exception_handler(app)
    operations = MediaOperations(owner)

    async def dispatch(request, roles, factory, deadline=5.0, *, capability=False):
        try:
            principal = await owner.authenticate(request, roles, capability=capability)
            return await operations.run(lambda: factory(principal), deadline)
        except (
            MediaSessionError,
            MediaSpoolError,
            MediaWireError,
            m.MediaContractError,
            HTTPException,
        ) as error:
            return _error(error)
        except Exception:
            return _error(MediaSessionError())

    @app.post("/v1/media/sources/admit")
    async def admit(request: Request):
        async def invoke(principal):
            nonce = bytes.fromhex(
                private_header(request.headers, "X-Kinaiya-Media-Bootstrap-Nonce")
            )
            value = await _json(request, m.MediaAdmit)
            receipt = await owner.admit(value, principal, nonce)
            grant = await owner.run_native(owner.admission_capability, receipt, principal, nonce)
            headers = {} if grant is None else {"X-Kinaiya-Media-Client-Capability": grant}
            return _response(receipt, headers=headers)

        return await dispatch(request, ("owner",), invoke, 25.0)

    @app.post("/v1/media/uploads/put")
    async def upload(request: Request):
        async def invoke(principal):
            if (
                request.headers.get("content-encoding", "identity") != "identity"
                or request.headers.get("content-type", "") != "application/vnd.kinaiya.media-frame"
            ):
                refuse("unsupported")
            body = await open_ingress(request.stream(), m.MediaUploadPut)
            return _response(await owner.upload(body, principal))

        return await dispatch(request, ("owner",), invoke, 60.0, capability=True)

    @app.post("/v1/media/sources/seal")
    async def seal(request: Request):
        async def invoke(principal):
            value = await _json(request, m.MediaSealRequest)
            return _response(await owner.seal(value, principal))

        return await dispatch(request, ("converse",), invoke, 25.0, capability=True)

    @app.post("/v1/media/anchors/task-proof")
    async def task_proof(request: Request):
        async def invoke(principal):
            value = await _json(request, m.MediaTaskProofRequest)
            return _response(await owner.run_native(owner.task_proof, value, principal))

        return await dispatch(request, ("owner", "agent"), invoke)

    @app.post("/v1/media/anchors/admit")
    async def anchor(request: Request):
        async def invoke(principal):
            value = await _json(request, m.MediaAnchorAdmission)
            return _response(await owner.run_native(owner.anchor, value, principal))

        return await dispatch(request, ("owner",), invoke, 25.0)

    @app.post("/v1/media/control")
    async def control(request: Request):
        async def invoke(principal):
            value = await _json(request, m.MediaControl)
            # Heartbeat/start require a grant; owner revocation may lack it.
            if (
                value.command in {"heartbeat", "start", "resume"}
                and principal.client_capability is None
            ):
                refuse("denied")
            return _response(await owner.control(value, principal))

        return await dispatch(request, ("owner",), invoke)

    @app.post("/v1/media/delete")
    async def delete(request: Request):
        async def invoke(principal):
            return _response(
                await owner.delete(await _json(request, m.MediaDeleteRequest), principal)
            )

        return await dispatch(request, ("owner",), invoke)

    @app.post("/v1/media/lookup")
    async def lookup(request: Request):
        async def invoke(principal):
            value = await _json(request, m.MediaLookup)
            return _response(await owner.run_native(owner.lookup, value, principal))

        return await dispatch(request, ("owner", "converse"), invoke)

    @app.post("/v1/media/lookup-key")
    async def lookup_key(request: Request):
        async def invoke(principal):
            value = await _json(request, m.MediaKeyLookup)
            return _response(await owner.run_native(owner.lookup_key, value, principal))

        return await dispatch(request, ("owner", "converse"), invoke)

    @app.get("/v1/media/sources/{source_id}/status")
    async def status(source_id: str, request: Request):
        async def invoke(principal):
            query = request.query_params
            if len(query.multi_items()) != 1 or "source_receipt_id" not in query:
                refuse("bad_packet")
            value = await owner.run_native(
                owner.current_status, source_id, query["source_receipt_id"], principal
            )
            return _response(value)

        return await dispatch(request, ("owner",), invoke)

    return operations
