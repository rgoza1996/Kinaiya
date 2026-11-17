"""Bounded transport for an explicitly launched Pi SDK peer."""

import asyncio
import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from packages.contracts.external_agent import (
    MAX_CURSOR,
    MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES,
    ExternalAgentTranscriptPage,
    ExternalAgentTranscriptPosition,
)

PI_SDK_PEER = Path(__file__).with_name("pi_peer.mjs")
PI_SDK_VERSION = "1.5.2"
MAX_FRAME_BYTES = 2_097_152
_MAX_INPUT_BYTES = 131_072
_MAX_CALLBACK_BYTES = MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES + 8192
_MAX_REQUESTS = 8192
_MAX_EVENTS = 512
_MAX_EVENT_BYTES = 8 * MAX_FRAME_BYTES
_ID = re.compile(r"[0-9a-f]{32}")
_COMMON = {"version", "type"}
_RESPONSES = {
    "status": {"observed"},
    "submit": {"accepted", "refused", "uncertain"},
    "interrupt": {"requested", "refused"},
    "close": {"stopping", "refused"},
}
_EVENT_FIELDS = {
    "running": set(),
    "settled": set(),
    "failed": set(),
    "interrupted": set(),
    "uncertain": set(),
    "output_unavailable": set(),
    "assistant_message": {"text"},
    "tool_activity": {"tool_call_id", "tool_name", "phase"},
}


class PiPeerUnavailable(RuntimeError):
    """Startup or new request admission is unavailable."""


class PiCommandUnconfirmed(RuntimeError):
    """The original request may have reached Pi."""

    def __init__(self, request_id):
        super().__init__("The original Pi request remains unconfirmed")
        self.request_id = request_id


class PiEventReplayExpired(ValueError):
    pass


@dataclass(frozen=True)
class PiReply:
    disposition: str
    reason: str | None
    busy: bool | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class PiEvent:
    cursor: int
    run_id: str
    kind: str
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    phase: str | None = None


@dataclass(frozen=True)
class _Request:
    command: str
    digest: bytes
    reply: asyncio.Future


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate Pi frame field")
        result[key] = value
    return result


def _identifier(value, maximum=256):
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and all(ord(char) >= 32 and char != "\x7f" for char in value)
    )


async def _read_frame(reader):
    line = await reader.readline()
    if not line.endswith(b"\n") or len(line) > MAX_FRAME_BYTES:
        raise ValueError("Missing, incomplete or oversized Pi frame")
    frame = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_fields)
    if type(frame) is not dict or type(frame.get("version")) is not int or frame["version"] != 1:
        raise ValueError("Unsupported Pi frame version")
    return frame, len(line)


class PiSdkPeer:
    """One SDK process and one response reader across caller timeouts."""

    def __init__(self, reader, writer, session_id, transcript_reader):
        self.session_id = session_id
        self._reader, self._writer = reader, writer
        self._send_lock = asyncio.Lock()
        self._requests: dict[str, _Request] = {}
        self._events: deque[tuple[PiEvent, int]] = deque()
        self._cursor = self._event_bytes = 0
        self._unavailable = False
        self._active_run_id = None
        self._transcript_reader = transcript_reader
        self._transcript_tasks: dict[str, asyncio.Task] = {}
        self._reader_task = asyncio.create_task(self._receive())

    @classmethod
    async def attach(
        cls, reader, writer, *, expected_session_id=None, transcript_reader=None, timeout=5
    ):
        """Observe one new process. The launcher still owns its lifetime."""
        try:
            frame, _ = await asyncio.wait_for(_read_frame(reader), timeout)
        except (ValueError, OSError, TimeoutError, RecursionError):
            raise PiPeerUnavailable("The Pi SDK peer did not supply a valid identity") from None
        if frame == {
            "version": 1,
            "type": "startup_refused",
            "reason": "model_selection_unavailable",
        }:
            raise PiPeerUnavailable("The Pi-selected model is unavailable")
        if (
            set(frame) != _COMMON | {"session_id", "sdk_version", "transcript"}
            or frame["type"] != "ready"
            or not _identifier(frame["session_id"])
            or frame["sdk_version"] != PI_SDK_VERSION
            or frame["transcript"] is not (transcript_reader is not None)
            or (expected_session_id is not None and frame["session_id"] != expected_session_id)
        ):
            raise PiPeerUnavailable("The Pi session identity did not match")
        return cls(reader, writer, frame["session_id"], transcript_reader)

    async def status(self, request_id, *, timeout=5):
        return await self._call(request_id, "status", {}, timeout)

    async def submit(self, request_id, instruction, *, timeout=5):
        if type(instruction) is not str or not 1 <= len(instruction) <= 16000:
            raise ValueError("Pi instruction is outside its declared bound")
        return await self._call(request_id, "submit", {"text": instruction}, timeout)

    async def interrupt(self, request_id, run_id, *, timeout=5):
        if type(run_id) is not str or _ID.fullmatch(run_id) is None:
            raise ValueError("Invalid Pi run identity")
        return await self._call(request_id, "interrupt", {"run_id": run_id}, timeout)

    async def request_close(self, request_id, *, timeout=5):
        return await self._call(request_id, "close", {}, timeout)

    def submission_reply(self, request_id):
        original = self._requests.get(request_id)
        if original is not None and original.command == "submit" and original.reply.done():
            return original.reply.result()
        return None

    async def _call(self, request_id, command, detail, timeout):
        if type(request_id) is not str or _ID.fullmatch(request_id) is None:
            raise ValueError("Invalid Pi request identity")
        wire = (
            json.dumps(
                {"version": 1, "command": command, "id": request_id, **detail},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if len(wire) > _MAX_INPUT_BYTES:
            raise ValueError("Pi command exceeds the wire limit")
        digest = hashlib.sha256(wire).digest()
        try:
            async with asyncio.timeout(timeout):
                async with self._send_lock:
                    original = self._requests.get(request_id)
                    if original is not None:
                        if original.digest != digest:
                            raise ValueError(
                                "Pi request identity was reused with different content"
                            )
                    else:
                        if self._unavailable or len(self._requests) >= _MAX_REQUESTS:
                            raise PiPeerUnavailable("Pi request admission is unavailable")
                        original = _Request(
                            command, digest, asyncio.get_running_loop().create_future()
                        )
                        self._requests[request_id] = original
                        self._writer.write(wire)
                        await self._writer.drain()
                reply = await asyncio.shield(original.reply)
        except (OSError, TimeoutError):
            if request_id in self._requests:
                raise PiCommandUnconfirmed(request_id) from None
            raise PiPeerUnavailable("Pi request did not enter the command stream") from None
        if reply is None:
            raise PiCommandUnconfirmed(request_id)
        return reply

    def events(self, after, limit=128):
        if type(after) is not int or not 0 <= after <= self._cursor:
            raise ValueError("Unknown Pi event cursor")
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("Invalid Pi replay limit")
        if after < self._cursor - len(self._events):
            raise PiEventReplayExpired("The Pi peer replay window moved past this cursor")
        return tuple(event for event, _ in self._events if event.cursor > after)[:limit]

    async def _receive(self):
        try:
            while True:
                frame, size = await _read_frame(self._reader)
                if frame.get("type") == "response":
                    self._record_response(frame)
                elif frame.get("type") == "event":
                    self._record_event(frame, size)
                elif frame.get("type") == "transcript_request":
                    self._transcript_request(frame)
                else:
                    raise ValueError("Unexpected Pi frame")
        except (ValueError, OSError, TypeError, RecursionError):
            pass
        finally:
            self._unavailable = True
            for original in self._requests.values():
                if not original.reply.done():
                    original.reply.set_result(None)
            pending = tuple(self._transcript_tasks.values())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    def _record_response(self, frame):
        identifier = frame.get("id")
        original = self._requests.get(identifier)
        disposition, reason = frame.get("disposition"), frame.get("reason")
        expected = _COMMON | {"id", "disposition", "reason"}
        if (
            original is None
            or original.reply.done()
            or disposition not in _RESPONSES[original.command]
            or (
                not _identifier(reason, 64)
                if disposition in {"refused", "uncertain"}
                else reason is not None
            )
        ):
            raise ValueError("Pi reply does not identify a pending original")
        busy, run_id = None, None
        if original.command == "status":
            expected |= {"session_id", "busy", "run_id"}
            busy, run_id = frame.get("busy"), frame.get("run_id")
            if (
                frame.get("session_id") != self.session_id
                or type(busy) is not bool
                or (run_id is not None and (not busy or not self._is_submit(run_id)))
            ):
                raise ValueError("Pi status changed its observed session or run")
        if set(frame) != expected:
            raise ValueError("Unexpected Pi response fields")
        original.reply.set_result(PiReply(disposition, reason, busy, run_id))

    def _is_submit(self, run_id):
        return type(run_id) is str and (
            (original := self._requests.get(run_id)) is not None and original.command == "submit"
        )

    def _record_event(self, frame, size):
        kind, run_id = frame.get("kind"), frame.get("run_id")
        expected = _EVENT_FIELDS.get(kind)
        if (
            expected is None
            or not self._is_submit(run_id)
            or set(frame) != _COMMON | {"kind", "run_id"} | expected
        ):
            raise ValueError("Pi event does not identify an original run")
        detail = {key: frame[key] for key in expected}
        if kind == "assistant_message" and (
            type(detail["text"]) is not str or len(detail["text"]) > 262144
        ):
            raise ValueError("Pi assistant message is outside its bound")
        if kind == "tool_activity" and (
            not _identifier(detail["tool_call_id"])
            or not _identifier(detail["tool_name"])
            or detail["phase"] not in {"started", "finished", "failed"}
        ):
            raise ValueError("Invalid Pi tool activity")
        if kind == "running":
            if self._active_run_id not in (None, run_id):
                raise ValueError("Pi reported overlapping original runs")
            self._active_run_id = run_id
        elif kind in {"settled", "failed", "interrupted", "uncertain"}:
            if self._active_run_id == run_id:
                self._active_run_id = None
        self._cursor += 1
        self._events.append((PiEvent(self._cursor, run_id, kind, **detail), size))
        self._event_bytes += size
        while len(self._events) > _MAX_EVENTS or self._event_bytes > _MAX_EVENT_BYTES:
            _, evicted_size = self._events.popleft()
            self._event_bytes -= evicted_size

    def _transcript_request(self, frame):
        identifier, run_id = frame.get("id"), frame.get("run_id")
        through, limit = frame.get("through_sequence"), frame.get("limit")
        if (
            set(frame)
            != _COMMON | {"id", "run_id", "sequence", "byte_offset", "through_sequence", "limit"}
            or self._transcript_reader is None
            or self._active_run_id is None
            or run_id != self._active_run_id
            or type(identifier) is not str
            or _ID.fullmatch(identifier) is None
            or identifier in self._transcript_tasks
            or len(self._transcript_tasks) >= 4
            or type(limit) is not int
            or not 1 <= limit <= 128
            or (
                through is not None and (type(through) is not int or not 0 <= through <= MAX_CURSOR)
            )
        ):
            raise ValueError("Pi transcript request has no active attachment capability")
        position = ExternalAgentTranscriptPosition(
            sequence=frame["sequence"], byte_offset=frame["byte_offset"]
        )
        task = asyncio.create_task(
            self._provide_transcript(identifier, run_id, position, through, limit)
        )
        self._transcript_tasks[identifier] = task
        task.add_done_callback(lambda _: self._transcript_tasks.pop(identifier, None))

    async def _provide_transcript(self, identifier, run_id, position, through, limit):
        page = None
        try:
            async with asyncio.timeout(5):
                result = await self._transcript_reader(
                    after=position, through_sequence=through, limit=limit
                )
                result = ExternalAgentTranscriptPage.model_validate(result, strict=True)
                if (
                    result.after == position
                    and result.target.backend_session_id == self.session_id
                    and through in (None, result.through_sequence)
                    and len(result.model_dump_json().encode("utf-8"))
                    <= MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES
                ):
                    page = result.model_dump(mode="json")
        except Exception:
            # The peer receives one typed refusal. Native failures and identities stay local.
            pass
        message = (
            json.dumps(
                {
                    "version": 1,
                    "command": "transcript_result",
                    "id": identifier,
                    "run_id": run_id,
                    "page": page,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if len(message) > _MAX_CALLBACK_BYTES:
            return
        try:
            async with asyncio.timeout(2), self._send_lock:
                if not self._unavailable and self._active_run_id == run_id:
                    self._writer.write(message)
                    await self._writer.drain()
        except (OSError, TimeoutError):
            pass
