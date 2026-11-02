"""Agent-only scans on Session's shared retained native operation registry."""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from packages.contracts.standing_work import StandingUnavailable
from packages.contracts.transcript_scan import (
    TranscriptScanConflict,
    TranscriptScanPage,
    TranscriptScanRequest,
    TranscriptScanUnavailable,
    checked_scan,
)
from services.session.standing_work_api import STANDING_INGRESS_SECONDS, StandingOwnerOperations

_INVALID = "Invalid transcript scan request"


class TranscriptScanOperations(StandingOwnerOperations):
    def __init__(self, store, *, shared):
        super().__init__(store)
        self._shared = shared
        self._operations = shared._operations

    async def run(self, request, action="scan", argument=None):
        if self._shared._closed:
            raise TranscriptScanUnavailable
        try:
            return await super().run(request, action, argument)
        except StandingUnavailable:
            raise TranscriptScanUnavailable from None

    async def close(self):
        self._shared._closed = True
        await super().close()

    async def _body(self, request, event, deadline, started):
        media = request.headers.get("content-type", "").lower().split(";")
        if (
            media[0].strip() != "application/json"
            or any(part.strip() not in {"charset=utf-8", 'charset="utf-8"'} for part in media[1:])
            or request.headers.get("content-encoding", "identity").strip().lower()
            not in {"", "identity"}
        ):
            raise HTTPException(415, "Transcript scan requires identity UTF-8 JSON")
        ingress = min(deadline, started + STANDING_INGRESS_SECONDS)
        raw = bytearray()
        try:
            async with asyncio.timeout(max(0, ingress - time.monotonic())):
                async for chunk in request.stream():
                    self._guard(event, ingress)
                    if (
                        type(chunk) is not bytes
                        or len(raw) + len(chunk) > TranscriptScanRequest.wire_bytes
                    ):
                        raise HTTPException(422, _INVALID)
                    raw.extend(chunk)
            self._guard(event, ingress)
        except TimeoutError:
            event.set()
            raise TranscriptScanUnavailable from None
        try:
            return TranscriptScanRequest.from_json(bytes(raw))
        except ValueError:
            raise HTTPException(422, _INVALID) from None

    def _call(self, argument, event, deadline):
        self._guard(event, deadline)
        try:
            argument = checked_scan(argument, TranscriptScanRequest)
            result = self.owner.transcript_scan(argument)
            self._guard(event, deadline)
            result = checked_scan(result, TranscriptScanPage).validate_request(argument)
            raw = result.canonical_bytes()
            self._guard(event, deadline)
            return raw
        except (TranscriptScanConflict, TranscriptScanUnavailable, StandingUnavailable):
            raise
        except Exception:
            raise TranscriptScanUnavailable from None

    async def _execute(
        self, request, action, argument, event, deadline, started, ingress_done, disconnected
    ):
        monitor = None
        try:
            if request.scope.get("query_string") or any(
                key.lower().startswith(b"x-kinaiya-") for key, _ in request.scope["headers"]
            ):
                raise HTTPException(422, _INVALID)
            argument = await self._body(request, event, deadline, started)
            self._guard(event, min(deadline, started + STANDING_INGRESS_SECONDS))
            ingress_done.set()
            task = asyncio.create_task(asyncio.to_thread(self._call, argument, event, deadline))
            monitor = asyncio.create_task(self._disconnect(request, event, disconnected))
            return await self._thread_result(task, event)
        except (
            HTTPException,
            TranscriptScanConflict,
            TranscriptScanUnavailable,
            StandingUnavailable,
        ):
            raise
        except BaseException as error:
            event.set()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise TranscriptScanUnavailable from None
        finally:
            ingress_done.set()
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)


def install_transcript_scan_routes(app, store, auth, *, shared):
    operations = TranscriptScanOperations(store, shared=shared)
    router = APIRouter(dependencies=[Depends(auth.require("agent"))])

    @router.post("/v1/transcript/scan")
    async def scan(request: Request):
        try:
            raw = await operations.run(request)
        except TranscriptScanConflict:
            raise HTTPException(409, str(TranscriptScanConflict())) from None
        except (TranscriptScanUnavailable, StandingUnavailable):
            raise HTTPException(503, str(TranscriptScanUnavailable())) from None
        return Response(raw, media_type="application/json", headers={"Cache-Control": "no-store"})

    app.include_router(router)
    return operations
