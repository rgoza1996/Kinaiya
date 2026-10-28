"""Content-controlled speech, independent of work dispatch and clip silence timers."""

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from packages.contracts.models import AppendRequest, SpeechWindow, WindowDecision
from packages.providers.disclosure import scope_allowed
from packages.providers.text import ProviderCleanupError, owned_http_stream_cleanup
from services.converse.runtime import SpeechChunker


class NativeJournalUnavailable(Exception):
    """A journal operation has no confirmed result; provider work is separate."""


class ProviderCleanupPending(Exception):
    """The previous window still owns unconfirmed provider cleanup."""


class WindowGenerationTask(asyncio.Task):
    """Cancellation is terminal; repeated controls cannot cancel its cleanup."""

    def cancel(self, msg=None):
        if self.cancelling():
            return False
        return super().cancel(msg)


def allowed_scope(provider, scope):
    return scope_allowed(provider.info, scope)


def unverified_work_claim(text, work_routed):
    # This is a conservative guard for explicit effect claims, not a blanket
    # ban on ordinary words such as "well done" or "finished my introduction".
    actor = r"(?:i(?:'ve)?|we(?:'ve)?|the agent)"
    verb = r"(?:(?:have|has|just|already)\s+){0,2}(?:completed?|finished|sent|saved|created|deleted|verified)"
    obj = r"(?:\s+(?:the|your|a|an|those|that|requested)){0,2}\s+(?:task|work|job|file|email|message|document|script|changes|report)\b"
    return bool(
        re.search(rf"\b{actor}\s+{verb}{obj}", text, re.I)
        or re.search(
            r"\b(?:the )?(?:agent|task|work|job)\s+(?:(?:is|was|has|been|now)\s+){0,3}(?:done|completed?|finished)\b",
            text,
            re.I,
        )
        or (work_routed and re.fullmatch(r"\s*(?:done|completed?|finished)[.!]?\s*", text, re.I))
    )


@dataclass
class PreparedReply:
    source: object
    decision: object
    behavior: str
    base_generation: int
    admission: tuple[int, int] | None = None
    chunks: list[str] = field(default_factory=list)
    published: int = 0
    generation: int | None = None
    started: bool = False
    task: object = None
    abandoned: bool = False
    prepare_request: object = None
    start_request: object = None
    reply_request: object = None
    provider_started: bool = False
    cleanup: object = None
    cleanup_failed: bool = False


class WindowConverseController:
    _journal_timeout_s = 1.0
    _journal_retry_delay_s = 0.1
    _journal_attempts = 3
    _provider_cleanup_timeout_s = 1.0

    def __init__(self, runtime):
        self.runtime, self.journal = runtime, runtime.journal
        self.pending = {}
        self.after = 0
        self.last_error = None
        self.locks = {}

    async def _native_call(self, operation, *args, **kwargs):
        try:
            async with asyncio.timeout(self._journal_timeout_s):
                return await operation(*args, **kwargs)
        except Exception as exc:
            raise NativeJournalUnavailable("Native journal result is unconfirmed") from exc

    async def _native(self, operation, *args, **kwargs):
        return await self._native_call(getattr(self.journal, operation), *args, **kwargs)

    def _local_current(self, prepared):
        if prepared.abandoned or not self.runtime.admission_current(prepared.admission):
            return False
        epoch = self.runtime.playback.get(prepared.source.session_id)
        expected = (
            prepared.generation if prepared.generation is not None else prepared.base_generation
        )
        return epoch is not None and epoch.accepts(expected)

    def _abandon(self, prepared):
        if prepared.abandoned:
            return
        prepared.abandoned = True
        epoch = self.runtime.playback.get(prepared.source.session_id)
        if (
            prepared.generation is not None
            and epoch is not None
            and epoch.accepts(prepared.generation)
        ):
            # Invalidate local playback immediately, before any best-effort
            # diagnostic I/O. This is not an audible-delivery acknowledgment.
            epoch.begin()
        task = prepared.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    @staticmethod
    def _released(prepared):
        if prepared.task is not None and not prepared.task.done():
            return False
        if prepared.cleanup_failed:
            return False
        if prepared.cleanup is not None:
            if not prepared.cleanup.done() or prepared.cleanup.cancelled():
                return False
            if prepared.cleanup.exception() is not None:
                return False
        return True

    async def _recover(self, operation, *, prepared=None):
        for attempt in range(self._journal_attempts):
            if prepared is not None and (
                not self._local_current(prepared)
                or (
                    not prepared.provider_started
                    and (datetime.now(UTC) - prepared.source.created_at).total_seconds() > 60
                )
            ):
                self._abandon(prepared)
                return None
            try:
                return await operation()
            except (NativeJournalUnavailable, ProviderCleanupPending) as exc:
                self.last_error = (
                    "journal_unavailable"
                    if isinstance(exc, NativeJournalUnavailable)
                    else "provider_cleanup_unconfirmed"
                )
                if attempt == self._journal_attempts - 1:
                    raise
                # The operation's lock has already unwound. Stop/off and newer
                # intent can run while this same inference waits to reconcile.
                await asyncio.sleep(self._journal_retry_delay_s)

    async def ingest(self, window: SpeechWindow):
        return await self.journal.append(
            AppendRequest(
                session_id=window.session_id,
                thread_id=window.thread_id,
                epoch=window.epoch,
                kind="conversation.window",
                payload=window.model_dump(mode="json"),
                idempotency_key=f"speech-window:{window.epoch}:{window.utterance_id}:{window.window_index}:r{window.revision}",
            )
        )

    def _state_request(self, source, phase, **fields):
        w = SpeechWindow.model_validate(source.payload)
        return AppendRequest(
            session_id=source.session_id,
            thread_id=source.thread_id,
            epoch=source.epoch,
            kind="converse.window_state",
            causes=[source.event_id],
            payload={
                "source_event_id": source.event_id,
                "utterance_id": w.utterance_id,
                "phase": phase,
                "runtime_id": self.runtime._runtime_id,
                **fields,
            },
            idempotency_key=f"window-response:{source.event_id}:{phase}:{self.runtime._runtime_id}",
        )

    async def state(self, source, phase, **fields):
        return await self._native("append", self._state_request(source, phase, **fields))

    async def current(self, prepared):
        source = prepared.source
        if not self._local_current(prepared):
            return None
        session = await self._native("session", source.session_id)
        if not self.runtime.enabled or (session.epoch, session.thread_id) != (
            source.epoch,
            source.thread_id,
        ):
            return None
        original = await self._native("resolve", source.event_id)
        if original.event_id != source.event_id:
            return None
        control = await self._native("window_control", source.session_id, source.epoch)
        if control is not None and control.payload["source_sequence"] > source.sequence:
            newer = await self._native("resolve", control.payload["source_event_id"])
            if control.payload["conversation"] == "WAIT_FOR_NEXT_WINDOW" or (
                newer.payload.get("utterance_id"),
                newer.payload.get("text"),
            ) != (source.payload.get("utterance_id"), source.payload.get("text")):
                return None
        w = SpeechWindow.model_validate(source.payload)
        latest = await self._native(
            "latest_window", source.session_id, source.epoch, w.utterance_id
        )
        latest_window = SpeechWindow.model_validate(latest.payload) if latest is not None else None
        if (
            latest_window is None
            or latest_window.text != w.text
            or latest_window.content_scope != w.content_scope
        ):
            return None
        if not self._local_current(prepared):
            return None
        return latest_window

    async def yield_speech(self, session_id):
        epoch = await self._native_call(self.runtime._epoch, session_id)
        if self.runtime._active.get(session_id) is not None or epoch.chunks:
            await self._native_call(self.runtime.interrupt, session_id)

    async def flush(self, prepared):
        try:
            valid = await self._recover(lambda: self._flush_once(prepared), prepared=prepared)
            if valid is False:
                self._abandon(prepared)
        except NativeJournalUnavailable:
            await self._native_failure(prepared)

    async def _native_failure(self, prepared):
        self.last_error = "journal_unavailable"
        self._abandon(prepared)
        try:
            await self.state(
                prepared.source,
                "error",
                reason="journal_unavailable",
                error_source="journal",
                generation_epoch=prepared.generation,
                confirmed_chunks=prepared.published,
                append_unconfirmed=prepared.reply_request is not None,
                delivery="delivery_unknown",
            )
        except NativeJournalUnavailable:
            pass

    async def _flush_once(self, prepared):
        source = prepared.source
        lock = self.locks.setdefault(source.session_id, asyncio.Lock())
        async with lock:
            latest = await self.current(prepared)
            if latest is None:
                return False
            epoch = await self._native_call(self.runtime._epoch, source.session_id)
            if prepared.generation is None:
                if (
                    prepared.behavior == "RESPOND_WITHOUT_INTERRUPTING_USER"
                    and latest.user_speaking
                ):
                    return True
                if epoch.generation != prepared.base_generation:
                    return False
                if not prepared.chunks:
                    return True
                if await self._native(
                    "window_response", source.session_id, source.epoch, latest.utterance_id
                ):
                    return False
                prepared.generation = epoch.begin()
                self.runtime._active[source.session_id] = prepared.task
                prepared.start_request = self._state_request(
                    source,
                    "started",
                    generation_epoch=prepared.generation,
                    behavior=prepared.behavior,
                )
            if not prepared.started:
                recorded = await self._native(
                    "window_response", source.session_id, source.epoch, latest.utterance_id
                )
                if recorded is None:
                    await self._native("append", prepared.start_request)
                elif (
                    recorded.payload.get("source_event_id") != source.event_id
                    or recorded.payload.get("runtime_id") != self.runtime._runtime_id
                    or recorded.payload.get("generation_epoch") != prepared.generation
                ):
                    return False
                prepared.started = True
            if not epoch.accepts(prepared.generation):
                return False
            while prepared.published < len(prepared.chunks):
                if await self.current(prepared) is None:
                    return False
                index = prepared.published
                text = prepared.chunks[index]
                if unverified_work_claim(
                    text, prepared.decision.payload["agent_route"] != "IGNORE_FOR_IMMEDIATE_ROUTING"
                ):
                    text = "The Agent has not provided a verified result for that claim."
                if prepared.reply_request is None:
                    prepared.reply_request = AppendRequest(
                        session_id=source.session_id,
                        thread_id=source.thread_id,
                        epoch=source.epoch,
                        kind="conversation.converse",
                        causes=[source.event_id],
                        payload={
                            "text": text,
                            "final": True,
                            "revision": 1,
                            "generation_epoch": prepared.generation,
                            "runtime_id": self.runtime._runtime_id,
                            "owner_intent_revision": prepared.admission[0],
                            "availability_generation": prepared.admission[1],
                            "chunk_id": hashlib.sha256(
                                f"{source.event_id}:{index}".encode()
                            ).hexdigest()[:32],
                            "delivery": "generated",
                            "window_source_id": source.event_id,
                            "window_utterance_id": latest.utterance_id,
                            "window_behavior": prepared.behavior,
                            "content_scope": latest.content_scope,
                        },
                        idempotency_key=f"window-reply:{source.event_id}:{index}",
                    )
                entry = await self._native("append", prepared.reply_request)
                if await self.current(prepared) is None or not epoch.accepts(prepared.generation):
                    return False
                epoch.chunks[entry.event_id] = entry
                prepared.published += 1
                prepared.reply_request = None
            return True

    async def _prepare(self, prepared):
        source = prepared.source
        if await self.current(prepared) is None:
            self._abandon(prepared)
            return None
        if prepared.prepare_request is None:
            prepared.prepare_request = self._state_request(
                source, "preparing", behavior=prepared.behavior
            )
        await self._native("append", prepared.prepare_request)
        recent = await self._native("recent", session_id=source.session_id, limit=100)
        w = SpeechWindow.model_validate(source.payload)
        context = []
        seen = set()
        for entry in reversed(recent):
            if entry.epoch != source.epoch or entry.sequence >= source.sequence:
                continue
            scope = entry.payload.get("content_scope", "local_only")
            if not allowed_scope(self.runtime.provider, scope):
                continue
            if entry.kind == "conversation.window":
                old = SpeechWindow.model_validate(entry.payload)
                if old.utterance_id == w.utterance_id or old.utterance_id in seen:
                    continue
                seen.add(old.utterance_id)
                context.append({"role": "user", "content": old.text[:2000]})
            elif entry.kind in {"conversation.user", "conversation.converse"}:
                context.append(
                    {
                        "role": "user" if entry.kind == "conversation.user" else "assistant",
                        "content": entry.payload["text"][:2000],
                    }
                )
            if len(context) >= 8:
                break
        if await self.current(prepared) is None:
            self._abandon(prepared)
            return None
        return [
            {"role": "system", "content": self.runtime.personality},
            *reversed(context),
            {"role": "user", "content": w.text},
        ]

    async def generate(self, prepared):
        source = prepared.source
        try:
            w = SpeechWindow.model_validate(source.payload)
            if not allowed_scope(self.runtime.provider, w.content_scope):
                raise ValueError("Window disclosure does not permit this provider")
            messages = await self._recover(lambda: self._prepare(prepared), prepared=prepared)
            if (
                messages is None
                or not self._local_current(prepared)
                or (datetime.now(UTC) - source.created_at).total_seconds() > 60
            ):
                self._abandon(prepared)
                return
            chunker = SpeechChunker()
            size = 0
            # Owner/off and explicit interruption must own an admitted stream
            # even before its first delta, including while journal reads fail.
            self.runtime._active[source.session_id] = prepared.task
            prepared.provider_started = True
            async with self._provider_stream(messages, prepared) as stream:
                async for delta in stream:
                    if not self._local_current(prepared):
                        self._abandon(prepared)
                        return
                    size += len(delta)
                    if size > 16000:
                        raise ValueError("Prepared reply exceeded its bound")
                    prepared.chunks.extend(chunker.feed(delta))
                    await self.flush(prepared)
                    if prepared.abandoned:
                        return
            prepared.chunks.extend(chunker.feed("", final=True))
            await self.flush(prepared)
        except asyncio.CancelledError:
            raise
        except NativeJournalUnavailable:
            await self._native_failure(prepared)
        except Exception as exc:
            if isinstance(exc, ProviderCleanupError):
                prepared.cleanup_failed = True
                self._abandon(prepared)
            self.last_error = type(exc).__name__
            try:
                await self.state(
                    source,
                    "error",
                    reason=type(exc).__name__,
                    error_source=("provider_cleanup" if prepared.cleanup_failed else "generation"),
                )
            except Exception:
                pass
        finally:
            if self.runtime._active.get(source.session_id) is prepared.task:
                self.runtime._active.pop(source.session_id, None)

    @asynccontextmanager
    async def _provider_stream(self, messages, prepared):
        with owned_http_stream_cleanup():
            async with self._owned_provider_stream(messages, prepared) as stream:
                yield stream

    @asynccontextmanager
    async def _owned_provider_stream(self, messages, prepared):
        stream = self.runtime.provider.stream(messages)
        try:
            yield stream
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                try:
                    prepared.cleanup = asyncio.ensure_future(close())
                except Exception:
                    prepared.cleanup_failed = True
                    self.last_error = "provider_cleanup_unconfirmed"
                else:
                    # Parent cancellation must not cut off a finally block
                    # owning an HTTP stream. Retain the cleanup handle even if
                    # this bounded wait cannot confirm it; replacement waits.
                    deadline = asyncio.get_running_loop().time() + self._provider_cleanup_timeout_s
                    interrupted = False
                    while not prepared.cleanup.done():
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            await asyncio.wait((prepared.cleanup,), timeout=remaining)
                        except asyncio.CancelledError:
                            interrupted = True
                    if not prepared.cleanup.done() or prepared.cleanup.cancelled():
                        self.last_error = "provider_cleanup_unconfirmed"
                    elif prepared.cleanup.exception() is not None:
                        prepared.cleanup_failed = True
                        self.last_error = "provider_cleanup_unconfirmed"
                    if interrupted:
                        raise asyncio.CancelledError

    async def process(self, entry):
        return await self._recover(lambda: self._process_once(entry))

    async def _process_once(self, entry):
        if (datetime.now(UTC) - entry.created_at).total_seconds() > 60:
            return
        if entry.kind == "conversation.window":
            w = SpeechWindow.model_validate(entry.payload)
            session = await self._native("session", entry.session_id)
            if session.epoch != entry.epoch:
                return
            latest = await self._native(
                "latest_window", entry.session_id, entry.epoch, w.utterance_id
            )
            if latest is None or latest.event_id != entry.event_id:
                return
            p = self.pending.get(entry.session_id)
            if p is not None:
                old = SpeechWindow.model_validate(p.source.payload)
                if w.utterance_id == old.utterance_id:
                    if w.text != old.text and p.generation is None:
                        self._abandon(p)
                        if self._released(p):
                            self.pending.pop(entry.session_id, None)
                    else:
                        await self.flush(p)
            return
        if entry.kind != "routing.window_decided" or entry.origin != "decisions":
            return
        d = WindowDecision.model_validate(entry.payload)
        source = await self._native("resolve", d.source_event_id)
        if (
            source.kind != "conversation.window"
            or source.origin != "authenticated-user"
            or (
                source.session_id,
                source.thread_id,
                source.epoch,
                source.sequence,
                source.payload["revision"],
            )
            != (
                entry.session_id,
                entry.thread_id,
                entry.epoch,
                d.source_sequence,
                d.source_revision,
            )
        ):
            return
        if (datetime.now(UTC) - source.created_at).total_seconds() > 60 or not self.runtime.enabled:
            return
        w = SpeechWindow.model_validate(source.payload)
        epoch = await self._native_call(self.runtime._epoch, source.session_id)
        try:
            admission = self.runtime.admission()
        except (OSError, ValueError, RuntimeError):
            return
        candidate = PreparedReply(
            source, entry, d.conversation, epoch.generation, admission=admission
        )
        if await self.current(candidate) is None:
            return
        if d.conversation == "IGNORE":
            return
        old = self.pending.get(source.session_id)
        if old is not None and old.abandoned:
            if not self._released(old):
                raise ProviderCleanupPending
            self.pending.pop(source.session_id, None)
            old = None
        if d.conversation == "WAIT_FOR_NEXT_WINDOW":
            if old is not None:
                self._abandon(old)
                if self._released(old):
                    self.pending.pop(source.session_id, None)
            await self.yield_speech(source.session_id)
            await self.state(source, "waiting")
            return
        if await self._native("window_response", source.session_id, source.epoch, w.utterance_id):
            return
        if (
            old is not None
            and old.source.payload["utterance_id"] == w.utterance_id
            and old.source.payload["text"] == w.text
        ):
            old.behavior = d.conversation
            await self.flush(old)
            return
        if old is not None:
            self._abandon(old)
            if not self._released(old):
                raise ProviderCleanupPending
        if source.session_id not in self.pending and len(self.pending) >= 32:
            key = next(iter(self.pending))
            dropped = self.pending[key]
            self._abandon(dropped)
            if not self._released(dropped):
                raise ProviderCleanupPending
            self.pending.pop(key, None)
        await self.yield_speech(source.session_id)
        candidate.base_generation = (
            await self._native_call(self.runtime._epoch, source.session_id)
        ).generation
        candidate.task = WindowGenerationTask(self.generate(candidate))
        self.pending[source.session_id] = candidate

    async def tick(self):
        try:
            entries = await self._recover(lambda: self._native("read", after=self.after, limit=100))
        except NativeJournalUnavailable:
            return 0
        for entry in entries:
            try:
                await self.process(entry)
            except (NativeJournalUnavailable, ProviderCleanupPending):
                # No admission is confirmed. Retain this source before moving
                # the cursor; another healthy tick can recover it without new
                # user speech or repeating an admitted provider inference.
                break
            except Exception as exc:
                self.last_error = type(exc).__name__
            self.after = entry.sequence
        for p in list(self.pending.values()):
            await self.flush(p)
            if p.abandoned and self._released(p) and self.pending.get(p.source.session_id) is p:
                self.pending.pop(p.source.session_id, None)
        return len(entries)

    async def close(self):
        for p in self.pending.values():
            self._abandon(p)
        tasks = {p.task for p in self.pending.values() if p.task is not None}
        if tasks:
            await asyncio.wait(tasks, timeout=self._provider_cleanup_timeout_s)
        self.pending = {key: p for key, p in self.pending.items() if not self._released(p)}

    async def run(self):
        try:
            while True:
                try:
                    await self.tick()
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    await asyncio.sleep(0.2)
        finally:
            await self.close()
