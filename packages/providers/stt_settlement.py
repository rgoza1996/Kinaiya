"""Bounded first-party STT reservations and actual native-thread ownership."""

import asyncio
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.stt_settlement import (
    PROTOCOL,
    STTWorkerOperation,
    STTWorkerReservation,
    digest,
)
from packages.providers.whistle_ipc import decode_json


class STTWorkerCleanupUnknown(RuntimeError):
    """The thread returned, but native device cleanup could not be proved."""


@dataclass
class _Invocation:
    descriptor: STTWorkerOperation
    resource_revision: object
    expires: float
    task: asyncio.Task | None = None
    thread: threading.Thread | None = None
    native_outcome: str | None = None
    state: str = "reserved"
    outcome: str | None = None
    result: dict | None = None
    response_owned: bool = False
    error_status: int = 503


class STTWorkerOperations:
    ACTIVE_LIMIT, RECEIPT_LIMIT, RESERVATION_SECONDS = 32, 1024, 30

    def __init__(self, recognize, allow_inference):
        self.recognize, self.allow_inference = recognize, allow_inference
        self.instance = secrets.token_hex(16)
        self.sequence = 0
        self.records = {}
        self.tasks = set()
        self.closed = False

    def _prune(self):
        now = time.monotonic()
        for record in self.records.values():
            if record.thread is not None and not record.thread.is_alive():
                record.thread.join(timeout=0)
                record.state = "settled" if record.native_outcome is not None else "unknown"
                record.outcome = record.native_outcome
        for record in self.records.values():
            if record.state == "reserved" and now >= record.expires:
                record.state, record.outcome = "settled", "not_dispatched"
        terminal = [
            key
            for key, record in self.records.items()
            if record.state == "settled" and not record.response_owned
        ]
        for key in terminal[: max(0, len(terminal) - self.RECEIPT_LIMIT)]:
            del self.records[key]

    def reserve(self, request):
        self._prune()
        if self.closed or request.worker_instance != self.instance:
            raise HTTPException(409, "Native STT worker instance changed")
        revision = self.allow_inference()
        if (
            sum(record.state != "settled" for record in self.records.values()) >= self.ACTIVE_LIMIT
            or self.sequence >= (1 << 128) - 1
        ):
            raise HTTPException(503, "Native STT operation capacity is retained")
        # Sequence IDs are never reused in this instance, even after pruning.
        self.sequence += 1
        body = {**request.model_dump(), "operation_id": f"{self.sequence:032x}"}
        descriptor = STTWorkerOperation(**body, request_sha256=digest(body))
        self.records[descriptor.operation_id] = _Invocation(
            descriptor, revision, time.monotonic() + self.RESERVATION_SECONDS
        )
        return descriptor

    def _record(self, descriptor):
        self._prune()
        record = self.records.get(descriptor.operation_id)
        if (
            descriptor.worker_instance != self.instance
            or record is None
            or record.descriptor != descriptor
        ):
            return None
        return record

    def proof(self, descriptor):
        record = self._record(descriptor)
        return {
            **descriptor.model_dump(),
            "state": "unknown" if record is None else record.state,
            "settled": record is not None and record.state == "settled",
            "outcome": None if record is None else record.outcome,
        }

    def _recognize_thread(self, record, data):
        try:
            value = self.recognize(data, record.resource_revision)
            if (
                type(value) is not dict
                or type(value.get("text")) is not str
                or len(value["text"]) > 16000
                or len(value["text"].encode("utf-8")) > 64000
            ):
                raise ValueError("Invalid native STT result")
            record.native_outcome = "completed"
            if record.response_owned:
                record.result = value
        except STTWorkerCleanupUnknown:
            record.native_outcome = None
        except BaseException as exc:
            record.native_outcome = "failed"
            record.error_status = (
                exc.status_code
                if isinstance(exc, HTTPException) and exc.status_code in {400, 409, 413, 415, 503}
                else 503
            )
        finally:
            data = None
            if not record.response_owned:
                record.result = None

    async def _run(self, record):
        # A cancelled coroutine/executor Future is not a native-exit proof.
        # The registry retains the actual Thread and joins it before issuing
        # settled metadata, including when this async monitor has disappeared.
        while record.thread.is_alive():
            await asyncio.sleep(0.01)
        self._prune()

    async def dispatch(self, descriptor, data):
        record = self._record(descriptor)
        if record is None or record.state != "reserved" or self.closed:
            raise HTTPException(
                409, "Native STT reservation is absent, retired or already dispatched"
            )
        if (
            hashlib.sha256(data).hexdigest() != descriptor.audio_sha256
            or self.allow_inference() != record.resource_revision
        ):
            raise HTTPException(409, "Native STT reservation content or resource admission changed")
        record.state, record.response_owned = "running", True
        record.thread = threading.Thread(
            target=self._recognize_thread,
            args=(record, data),
            name="kinaiya-stt-producer",
            daemon=True,
        )
        try:
            record.thread.start()
        except BaseException:
            if record.thread.ident is None:
                record.thread = None
                record.state, record.outcome = "settled", "not_dispatched"
            record.response_owned = False
            raise
        data = None
        try:
            run = self._run(record)
            try:
                record.task = asyncio.create_task(run)
            except BaseException:
                run.close()
                raise
            self.tasks.add(record.task)
            record.task.add_done_callback(self.tasks.discard)
            record.task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
            await asyncio.shield(record.task)
            if record.result is None:
                raise HTTPException(
                    record.error_status, "Native STT operation did not return a transcript"
                )
            return {"transcript": record.result, "operation": self.proof(descriptor)}
        finally:
            record.response_owned = False
            record.result = None

    async def legacy(self, data):
        request = STTWorkerReservation(
            schema_version="1.0",
            protocol=PROTOCOL,
            worker_instance=self.instance,
            audio_sha256=hashlib.sha256(data).hexdigest(),
        )
        descriptor = self.reserve(request)
        result = await self.dispatch(descriptor, data)
        return result["transcript"]

    async def close(self):
        self.closed = True
        # A cancelled HTTP request cannot cancel the actual recognize owner.
        while self.tasks or any(
            record.thread is not None and record.thread.is_alive()
            for record in self.records.values()
        ):
            await asyncio.sleep(0.01)
            self._prune()


def install_stt_settlement(app, auth, operations):
    access = [Depends(auth.require("owner", "converse"))]

    def response(value):
        result = JSONResponse(value, headers={"Cache-Control": "no-store"})
        if len(result.body) > 128 * 1024:
            raise HTTPException(503, "Native STT response exceeds its bound")
        return result

    async def body(request, model):
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 4096:
                        raise HTTPException(413, "Native STT metadata exceeds its bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Native STT metadata deadline expired") from None
        try:
            return model.model_validate(decode_json(bytes(raw), 4096))
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid native STT descriptor") from None

    @app.get("/v1/audio/stt-operations/capability", dependencies=access)
    async def capability():
        return response(
            {
                "schema_version": "1.0",
                "protocol": PROTOCOL,
                "worker_instance": operations.instance,
                "active_limit": operations.ACTIVE_LIMIT,
                "receipt_limit": operations.RECEIPT_LIMIT,
                "reservation_seconds": operations.RESERVATION_SECONDS,
            }
        )

    @app.post("/v1/audio/stt-operations/reserve", dependencies=access)
    async def reserve(request: Request):
        return response(operations.reserve(await body(request, STTWorkerReservation)).model_dump())

    @app.post("/v1/audio/stt-operations/lookup", dependencies=access)
    async def lookup(request: Request):
        return response(operations.proof(await body(request, STTWorkerOperation)))

    @app.post("/v1/audio/stt-operations/dispatch", dependencies=access)
    async def dispatch(request: Request):
        try:
            descriptor = STTWorkerOperation.model_validate(
                {
                    "schema_version": "1.0",
                    "protocol": PROTOCOL,
                    **{
                        name: request.headers.get("x-kinaiya-" + name.replace("_", "-"))
                        for name in (
                            "worker_instance",
                            "operation_id",
                            "audio_sha256",
                            "request_sha256",
                        )
                    },
                }
            )
        except ValueError:
            raise HTTPException(422, "Invalid native STT dispatch descriptor") from None
        raw = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 10_000_000:
                        raise HTTPException(413, "Native STT audio exceeds its transient bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Native STT audio body deadline expired") from None
        data = bytes(raw)
        raw.clear()
        return response(await operations.dispatch(descriptor, data))
