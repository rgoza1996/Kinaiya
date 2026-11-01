"""Task-local inspection hooks at real provider boundaries, never permissions.

The scope is bound inside each classification attempt, including worker tasks.
No observer, HTTP probe or replay is created by the inspection read API.
"""

import asyncio
import copy
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import uuid4

from packages.contracts.decisions import (
    MAX_OUTPUT_BYTES,
    MAX_RESPONSE_BYTES,
    DecisionsCallFinish,
    DecisionsCallStart,
    encoded,
)
from packages.providers.disclosure import combine_scopes, scope_allowed, source_scope
from packages.providers.text import ProviderRefusal

_capture = ContextVar("decisions_inspection_capture", default=None)


class CaptureNativeUnavailable(Exception):
    """Native admission could not be checked; preserve the durable backlog."""


class DecisionCapture:
    def __init__(
        self, journal, source, provider, attempt, reused_from=None, model_pin=None, current=None
    ):
        self.journal, self.source = journal, source
        self.call_id, self.attempt_id = str(uuid4()), str(uuid4())
        self.attempt, self.reused_from = attempt or 1, reused_from
        self.provider = provider
        self.model_pin, self.model_current = model_pin, current
        self.started = False
        self.raw_response, self.validated_output = None, None
        self.artifact_sha256 = None
        self._pending_start = None
        self._request_boundary_seen = False
        self.disclosure_sources = [source.model_copy(deep=True)]

    async def start(self, provider, request, request_format):
        if self.started:
            return
        info = getattr(provider, "info", None)
        source = self.source
        self._request_boundary_seen |= request_format not in {"not_submitted", "reused"}
        try:
            body = self._pending_start or DecisionsCallStart(
                call_id=self.call_id,
                attempt_id=self.attempt_id,
                attempt=self.attempt,
                source_event_id=source.event_id,
                source_sequence=source.sequence,
                source_revision=source.payload.get("revision", 1),
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                provider=info.provider if info else None,
                model=request.get("model", info.model if info else None)
                if request is not None
                else info.model
                if info
                else None,
                artifact_sha256=None,
                request_format=request_format,
                request=copy.deepcopy(request),
                reused_from_call_id=self.reused_from,
                model_pin=self.model_pin,
            )
            # Retry/reconcile the identical immutable request after an
            # uncertain capture ACK. Never relabel a real wire request as
            # not_submitted merely because inspection storage was unavailable.
            self._pending_start = body.model_copy(deep=True)
            async with asyncio.timeout(0.5):
                await self.journal.decisions_call_start(body)
        except Exception:
            # Inspection availability never grants work, changes a classifier
            # answer, or fabricates a retained script. Missing capture stays
            # explicit in the native source's owner projection.
            return
        self.started = True

    async def check_current(self, provider):
        # Audit writes add an await immediately before network disclosure.
        # Recheck the actual exported native sources after that await, including
        # sticky scope restrictions on earlier conversation revisions.
        try:
            if self.model_current is not None and not self.model_current():
                raise CaptureNativeUnavailable("Decisions model ownership changed")
            for index, bound in enumerate(self.disclosure_sources):
                actual = await self.journal.entry(bound.event_id)
                if (
                    actual.session_id,
                    actual.thread_id,
                    actual.epoch,
                    actual.sequence,
                    actual.kind,
                ) != (bound.session_id, bound.thread_id, bound.epoch, bound.sequence, bound.kind):
                    raise ProviderRefusal("Decisions source identity changed")
                source_id = (
                    bound.payload.get("target_event_id")
                    if bound.kind == "conversation.revision"
                    else bound.event_id
                )
                resolved = await self.journal.resolve(source_id)
                if bound.kind == "conversation.window":
                    latest = await self.journal.latest_window(
                        bound.session_id,
                        bound.epoch,
                        bound.payload["utterance_id"],
                        None if index == 0 else bound.payload["window_index"],
                    )
                    if latest is None or latest.event_id != bound.event_id:
                        raise ProviderRefusal("Decisions window is obsolete")
                elif index == 0 and resolved.payload.get("revision", 1) != bound.payload.get(
                    "revision", 1
                ):
                    raise ProviderRefusal("Decisions source revision changed")
                floor = await source_scope(self.journal, source_id, resolved=resolved)
                info = getattr(provider, "info", None)
                if info is not None and not scope_allowed(
                    info, combine_scopes(bound.payload.get("content_scope", "local_only"), floor)
                ):
                    raise ProviderRefusal("Decisions disclosure scope changed")
            session = await self.journal.session(self.source.session_id)
            if (session.thread_id, session.epoch, session.channel) != (
                self.source.thread_id,
                self.source.epoch,
                "converse",
            ):
                raise ProviderRefusal("Decisions session is obsolete")
            if self.model_current is not None and not self.model_current():
                raise CaptureNativeUnavailable("Decisions model ownership changed")
        except ProviderRefusal:
            raise
        except Exception as exc:
            raise CaptureNativeUnavailable("Decisions native currentness is unavailable") from exc

    async def finish(self, status, error=None):
        if not self.started:
            if self._request_boundary_seen and self._pending_start is None:
                return  # Oversized/unrepresentable capture stays unavailable.
            await self.start(self.provider, None, "reused" if self.reused_from else "not_submitted")
        if not self.started:
            return
        try:
            output = self.validated_output
            if len(encoded(output)) > MAX_OUTPUT_BYTES:
                output = None
            completion = DecisionsCallFinish(
                source_event_id=self.source.event_id,
                status=status,
                raw_response=self.raw_response,
                validated_output=output,
                error_type=type(error).__name__ if error else None,
                artifact_sha256=self.artifact_sha256,
            )
            async with asyncio.timeout(0.5):
                await self.journal.decisions_call_finish(self.call_id, completion)
        except Exception:
            pass  # An unsettled start remains unknown, never a false success.


@asynccontextmanager
async def decision_attempt(
    journal, source, provider, *, attempt=1, reused_from=None, model_pin=None, current=None
):
    capture = DecisionCapture(journal, source, provider, attempt, reused_from, model_pin, current)
    token = _capture.set(capture)
    try:
        yield capture
    except BaseException as exc:
        status = (
            "cancelled"
            if isinstance(exc, asyncio.CancelledError)
            else "refused"
            if isinstance(exc, ProviderRefusal)
            else "error"
        )
        await capture.finish(status, exc)
        raise
    else:
        await capture.finish("reused" if reused_from else "completed")
    finally:
        _capture.reset(token)


async def capture_request(provider, body, request_format):
    capture = _capture.get()
    if capture is not None:
        await capture.start(provider, body, request_format)
        try:
            async with asyncio.timeout(0.5):
                await capture.check_current(provider)
        except TimeoutError as exc:
            raise CaptureNativeUnavailable(
                "Decisions currentness check exceeded its bound"
            ) from exc


def capture_sources(source, context):
    """Pin only the native entries actually included in the concrete prompt."""
    capture = _capture.get()
    if capture is not None:
        capture.disclosure_sources = [entry.model_copy(deep=True) for entry in [source, *context]]


def capture_response(raw):
    capture = _capture.get()
    if capture is None:
        return
    try:
        raw = raw.encode() if isinstance(raw, str) else raw
        if len(raw) <= MAX_RESPONSE_BYTES:
            text = raw.decode("utf-8", errors="strict")
            if len(encoded(text)) <= MAX_RESPONSE_BYTES + 2:
                capture.raw_response = text
    except (UnicodeError, TypeError):
        pass


def capture_artifact(sha256):
    capture = _capture.get()
    if capture is not None:
        capture.artifact_sha256 = sha256


async def capture_error_response(response, maximum):
    """Keep bounded error text without changing native HTTP refusal policy."""
    if _capture.get() is None or response.headers.get(
        "content-encoding", "identity"
    ).strip().lower() not in {"", "identity"}:
        return
    raw = bytearray()
    try:
        async with asyncio.timeout(0.5):
            async for part in response.aiter_raw(chunk_size=4096):
                if len(raw) + len(part) > maximum:
                    return
                raw.extend(part)
    except Exception:
        return
    capture_response(bytes(raw))
