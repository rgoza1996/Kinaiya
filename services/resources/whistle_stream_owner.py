"""Retain each exact broker exchange independently of its HTTP caller."""

import asyncio
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException
from starlette.requests import Request

from packages.contracts.stt_settlement import digest, encode
from packages.contracts.whistle_stream_owner import (
    ORIGINAL_HEADER,
    PCM_HEADERS,
    PROTOCOL,
    WhistleStreamAcknowledgment,
    WhistleStreamOriginal,
    WhistleStreamProof,
    WhistleStreamReservation,
    pcm_request_digest,
)
from packages.providers.whistle_ipc import MAX_RESPONSE_BYTES, decode_json

_CURRENT = ContextVar("whistle_stream_original", default=None)


def retain_native(operation):
    record = _CURRENT.get()
    if record is not None:
        record.native = operation
        if record.cancel_requested:
            raise HTTPException(409, "The exact stream original was cancelled before entry")


@dataclass
class _Record:
    original: WhistleStreamOriginal
    created: float
    state: str = "reserved"
    result: str | None = None
    result_sha256: str | None = None
    task: asyncio.Task | None = None
    native: object = None
    cancel_requested: bool = False
    process_released: bool = False


class WhistleStreamOwner:
    active_limit = 64
    history_limit = 1024
    reservation_seconds = 30

    def __init__(self, broker):
        self.broker = broker
        self.instance = secrets.token_hex(16)
        self.counter = 0
        self.records = {}
        self.closed = False

    def capability(self):
        return {
            "schema_version": "1.0",
            "protocol": PROTOCOL,
            "broker_instance": self.instance,
            "active_limit": self.active_limit,
            "history_limit": self.history_limit,
            "reservation_seconds": self.reservation_seconds,
        }

    @staticmethod
    def _work_done(native):
        return native is not None and all(
            task is None or task.done()
            for task in (native.admission, native.work, *native.work_tasks)
        )

    def _process_released(self, record):
        if record.process_released:
            return True
        native = record.native
        closes = getattr(self.broker.endpoint, "stream_closes", None)
        return bool(
            self._work_done(native)
            and native.cleanup_confirmed
            and not native.cleanup_failed
            and native.cleanup is not None
            and native.cleanup.done()
            and not native.cleanup.cancelled()
            and native.cleanup.exception() is None
            and closes is not None
            and not closes.pending
            and not closes.failed
        )

    def _protected(self, record):
        native = record.native
        return record.state in {"reserved", "running", "completed", "unknown"} or (
            native is not None and not native.stream_finished and not self._process_released(record)
        )

    def _expire(self):
        now = time.monotonic()
        for record in self.records.values():
            if record.state == "reserved" and now - record.created >= self.reservation_seconds:
                record.state = "not_dispatched"
            if (
                record.state == "acknowledged"
                and record.native is not None
                and (record.native.stream_finished or self._process_released(record))
            ):
                record.process_released = self._process_released(record)
                record.native = None
        settled = [key for key, record in self.records.items() if not self._protected(record)]
        for key in settled[: max(0, len(settled) - self.history_limit)]:
            del self.records[key]

    async def _body(self, request, limit):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("content-type", "content-length", "content-encoding", ORIGINAL_HEADER)
            + PCM_HEADERS
        ):
            raise HTTPException(400, "Invalid stream exchange envelope")
        if request.headers.get("content-encoding", "identity").strip().lower() not in {
            "",
            "identity",
        }:
            raise HTTPException(415, "Stream exchange encoding is unsupported")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > limit:
                        raise HTTPException(413, "Stream exchange body exceeds its bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise HTTPException(408, "Stream body acquisition exceeded its deadline") from None
        return bytes(raw)

    async def control(self, request, operation):
        if (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "Stream ownership requires JSON metadata")
        raw = await self._body(request, 4096)
        try:
            model = {
                "reserve": WhistleStreamReservation,
                "lookup": WhistleStreamOriginal,
                "acknowledge": WhistleStreamAcknowledgment,
                "abort": WhistleStreamOriginal,
            }[operation]
            original = model.model_validate(decode_json(raw, 4096))
        except (ValueError, KeyError):
            raise HTTPException(400, "Invalid exact stream ownership metadata") from None
        self._expire()
        if operation == "reserve":
            if self.closed or original.broker_instance != self.instance:
                raise HTTPException(409, "Stream broker instance is unavailable")
            if (
                sum(self._protected(record) for record in self.records.values())
                >= self.active_limit
            ):
                raise HTTPException(409, "Original stream work or recovery is still retained")
            if self.counter >= (1 << 128) - 1:
                raise HTTPException(503, "Stream operation identities are exhausted")
            self.counter += 1
            bound = WhistleStreamOriginal(
                **original.model_dump(), operation_id=f"{self.counter:032x}"
            )
            self.records[bound.operation_id] = _Record(bound, time.monotonic())
            return bound.model_dump()
        bound = WhistleStreamOriginal.model_validate(original.model_dump(exclude={"result_sha256"}))
        record = self._find(bound)
        if operation == "acknowledge":
            if (
                record is None
                or record.state not in {"completed", "acknowledged"}
                or record.result_sha256 != original.result_sha256
            ):
                raise HTTPException(409, "The exact original result is not acknowledged")
            record.state, record.result = "acknowledged", None
            record.process_released = self._process_released(record)
            if bound.phase != "begin":
                record.native = None
        elif operation == "abort" and record is not None:
            record.cancel_requested = True
            if record.state == "reserved":
                record.state = "not_dispatched"
            # A healthy worker can already be serving a different stream with
            # the same process identity. Only this still-current stream may be
            # aborted. Old process coincidence grants no cancellation authority.
            native = record.native
            if native is not None and self.broker.operation is native:
                self.broker.abort(native)
                await asyncio.wait((native.cleanup,), timeout=1)
        return self.proof(bound, record)

    def _find(self, original):
        record = self.records.get(original.operation_id)
        return (
            record
            if original.broker_instance == self.instance
            and record is not None
            and record.original == original
            else None
        )

    def proof(self, original, record):
        return WhistleStreamProof(
            schema_version="1.0",
            protocol=PROTOCOL,
            original=original,
            state="unknown" if record is None else record.state,
            result=None
            if record is None or record.result is None
            else decode_json(record.result.encode("utf-8"), MAX_RESPONSE_BYTES),
            result_sha256=None if record is None else record.result_sha256,
            decoder_finished=bool(
                record is not None
                and original.phase == "finish"
                and record.state in {"completed", "acknowledged"}
            ),
            process_released=record is not None and self._process_released(record),
        ).model_dump(mode="json")

    async def dispatch(self, request, phase, operation):
        if len(request.headers.getlist(ORIGINAL_HEADER)) != 1:
            raise HTTPException(400, "One exact stream original is required")
        try:
            original = WhistleStreamOriginal.model_validate(
                decode_json(request.headers[ORIGINAL_HEADER].encode("ascii"), 2048)
            )
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid exact stream original") from None
        self._expire()
        record = self._find(original)
        if self.closed or record is None or record.state != "reserved" or original.phase != phase:
            raise HTTPException(409, "Stream dispatch requires its unused original reservation")
        if phase == "pcm":
            sequence = request.headers.get("x-kinaiya-sequence", "")
            if not sequence.isascii() or not sequence.isdigit():
                raise HTTPException(400, "Original PCM sequence requires an ASCII integer")
            if len(sequence) > 5 or not 1 <= int(sequence) <= 99999:
                raise HTTPException(413, "Original PCM must reserve a sequence for decoder finish")
        raw = await self._body(request, 32000 if phase == "pcm" else 4096)
        try:
            observed = (
                pcm_request_digest(request.headers, raw)
                if phase == "pcm"
                else digest(decode_json(raw, 4096))
            )
        except ValueError:
            raise HTTPException(400, "Invalid stream original body") from None
        self._expire()
        if observed != original.request_sha256 or record.state != "reserved":
            raise HTTPException(409, "Stream dispatch differs from its original reservation")
        record.state = "running"
        scope = dict(request.scope)

        async def run():
            token = _CURRENT.set(record)
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                # The original bounded broker task owns its lifetime. An outer
                # HTTP disconnect cannot erase a late stream ID/native result.
                await asyncio.Event().wait()

            try:
                result = await operation(Request(scope, receive))
                encoded = encode(result)
                if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise ValueError("Stream original result exceeds its bound")
                record.result, record.result_sha256 = encoded, digest(result)
                record.state = "completed"
                return decode_json(encoded.encode("utf-8"), MAX_RESPONSE_BYTES)
            except BaseException:
                # This sole producer has exited. Native entry is attached
                # before admission/dispatch; a refusal before that marker
                # cannot leave a late model effect. The reservation remains
                # consumed, so lookup proves no dispatch without replay.
                record.state = "not_dispatched" if record.native is None else "unknown"
                raise
            finally:
                _CURRENT.reset(token)

        record.task = asyncio.create_task(run())

        def finished(task):
            if not task.cancelled():
                task.exception()
            record.task = None

        record.task.add_done_callback(finished)
        return await asyncio.shield(record.task)
