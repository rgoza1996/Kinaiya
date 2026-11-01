"""Content-controlled speech, independent of work dispatch and clip silence timers."""

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from packages.contracts.memory import MemoryBinding
from packages.contracts.models import AppendRequest, SpeechWindow, WindowDecision
from packages.providers.disclosure import combine_scopes, scope_allowed
from packages.providers.text import ProviderCleanupError, owned_http_stream_cleanup
from services.converse.context_budget import ContextBudgetBuilder
from services.converse.context_projection import ContextProjection
from services.converse.delivery_context import window_context_projection
from services.converse.runtime import SpeechChunker
from services.converse.state_guard import UNVERIFIED_STATE_REPLY, negative_runtime_state_claim


class NativeJournalUnavailable(Exception):
    """A journal operation has no confirmed result; provider work is separate."""


class ProviderCleanupPending(Exception):
    """Bounded window ownership does not permit another provider stream yet."""


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
    stop: object = None
    stop_generation: int = 0
    content_scope: str = "local_only"
    provider: object = None
    model_lease: object = None
    model_dependencies: set = field(default_factory=set)
    model_cleanup: object = None
    consumer_done: asyncio.Event = field(default_factory=asyncio.Event)
    memory_lease: object = None
    messages: object = None


class WindowConverseController:
    _journal_timeout_s = 1.0
    _journal_retry_delay_s = 0.1
    _journal_attempts = 3
    _provider_cleanup_timeout_s = 1.0
    _stop_batch_size = 32

    def __init__(self, runtime):
        self.runtime, self.journal = runtime, runtime.journal
        self.pending = {}
        self.after = 0
        self.last_error = None
        self.locks = {}
        runtime._window_text_ready = self.provider_released

    def provider_released(self, session_id):
        """Read-only typed admission hook, including retained real cleanup."""
        prepared = self.pending.get(session_id)
        return prepared is None or self._released(prepared)

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
        published_cache = (
            prepared.model_lease is not None
            and prepared.model_cleanup is not None
            and not prepared.cleanup_failed
            and not prepared.model_lease.cleanup_failed
            and prepared.task is not None
            and prepared.task.done()
            and prepared.consumer_done.is_set()
            and prepared.published == len(prepared.chunks)
        )
        if (
            prepared.provider is not None
            and not published_cache
            and not self.runtime.model_current(prepared)
        ):
            return False
        if prepared.memory_lease is not None and not prepared.memory_lease.current():
            return False
        epoch = self.runtime.playback.get(prepared.source.session_id)
        expected = (
            prepared.generation if prepared.generation is not None else prepared.base_generation
        )
        return (
            epoch is not None
            and epoch.scope == (prepared.source.epoch, prepared.source.thread_id)
            and epoch.accepts(expected)
        )

    def _abandon(self, prepared):
        if prepared.abandoned:
            return
        prepared.abandoned = True
        prepared.consumer_done.set()
        epoch = self.runtime.playback.get(prepared.source.session_id)
        if (
            prepared.generation is not None
            and epoch is not None
            and epoch.scope == (prepared.source.epoch, prepared.source.thread_id)
            and epoch.accepts(prepared.generation)
        ):
            # Invalidate local playback immediately, before any best-effort
            # diagnostic I/O. This is not an audible-delivery acknowledgment.
            prepared.stop, prepared.stop_generation = self.runtime.fence_playback(
                prepared.source.session_id,
                source=prepared.source,
                expected_generation=prepared.generation,
            )
        elif (
            prepared.generation is not None
            and epoch is not None
            and epoch.scope == (prepared.source.epoch, prepared.source.thread_id)
            and epoch.stop is not None
            and epoch.stop.desired_generation > prepared.generation
        ):
            # An owner control may already have fenced this reply. Its unknown
            # stop outcome still belongs to the original bounded reply slot.
            prepared.stop = epoch.stop
            prepared.stop_generation = prepared.generation + 1
        task = prepared.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    @staticmethod
    def _released(prepared):
        if prepared.stop is not None and not prepared.stop.covers(prepared.stop_generation):
            return False
        if prepared.task is not None and not prepared.task.done():
            return False
        if prepared.cleanup_failed:
            return False
        if prepared.cleanup is not None:
            if not prepared.cleanup.done() or prepared.cleanup.cancelled():
                return False
            if prepared.cleanup.exception() is not None:
                return False
        if prepared.model_lease is not None:
            if prepared.model_cleanup is None or not prepared.model_cleanup.done():
                return False
            if prepared.model_cleanup.cancelled() or prepared.model_cleanup.exception() is not None:
                return False
            if prepared.model_lease.cleanup_failed:
                return False
        return True

    def _reclaimable(self, prepared):
        return self._released(prepared) and (
            prepared.abandoned or prepared.published == len(prepared.chunks)
        )

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
                    else str(exc) or "provider_cleanup_unconfirmed"
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

    async def yield_speech(self, session_id, *, force=False):
        epoch = self.runtime.playback.get(session_id)
        if epoch is None:
            epoch = await self._native_call(self.runtime._epoch, session_id)
        already_fenced = (
            epoch.stop is not None
            and not epoch.stop.retired
            and epoch.stop.desired_generation == epoch.generation
        )
        if not already_fenced and (
            force or self.runtime._active.get(session_id) is not None or epoch.chunks
        ):
            await self._native_call(self.runtime.interrupt, session_id)
        else:
            await self._native_call(self.runtime.reconcile_stop, session_id, epoch)

    async def _reconcile_stops(self):
        batch = self.runtime.stop_reconciliation_batch(self._stop_batch_size)

        async def reconcile():
            for session_id, epoch in batch:
                try:
                    await self.runtime.reconcile_stop(session_id, epoch)
                except Exception:
                    # One failed session does not consume another's chance,
                    # subject to the single native time budget for this batch.
                    self.last_error = "journal_unavailable"

        try:
            await self._native_call(reconcile)
        except NativeJournalUnavailable:
            self.last_error = "journal_unavailable"

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
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
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
            if not self._local_current(prepared):
                return False
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
                if not self._local_current(prepared):
                    return False
                prepared.generation = epoch.begin()
                self.runtime._active[source.session_id] = prepared.task
                prepared.start_request = self._state_request(
                    source,
                    "started",
                    generation_epoch=prepared.generation,
                    behavior=prepared.behavior,
                    **self.runtime.model_payload(prepared),
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
                positive_hit = unverified_work_claim(
                    text, prepared.decision.payload["agent_route"] != "IGNORE_FOR_IMMEDIATE_ROUTING"
                )
                if positive_hit:
                    text = "The Agent has not provided a verified result for that claim."
                    negative_state = False
                elif negative_runtime_state_claim(text):
                    negative_state = True
                    text = UNVERIFIED_STATE_REPLY
                else:
                    negative_state = False
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
                            **self.runtime.model_payload(prepared),
                            **(
                                {"guard_reason": "ungrounded_runtime_state_claim"}
                                if negative_state
                                else {}
                            ),
                            "content_scope": combine_scopes(
                                prepared.content_scope,
                                latest.content_scope
                                if isinstance(latest.content_scope, str)
                                else "local_only",
                                self.runtime.personality_scope,
                            ),
                            **self.runtime.memory_payload(prepared.memory_lease),
                        },
                        idempotency_key=f"window-reply:{source.event_id}:{index}",
                    )
                if prepared.memory_lease is None:
                    entry = await self._native("append", prepared.reply_request)
                else:
                    entry = await self._native_call(
                        prepared.memory_lease.publish, self.journal, prepared.reply_request
                    )
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
        if prepared.messages is not None:
            return prepared.messages
        if prepared.prepare_request is None:
            prepared.prepare_request = self._state_request(
                source,
                "preparing",
                behavior=prepared.behavior,
                **self.runtime.model_payload(prepared),
            )
        await self._native("append", prepared.prepare_request)
        recent = (
            []
            if self.runtime.context_budgeted
            else await self._native("recent", session_id=source.session_id, limit=100)
        )
        w = SpeechWindow.model_validate(source.payload)
        budget = (
            ContextBudgetBuilder(
                self.journal,
                source,
                prepared.provider,
                personality=self.runtime.personality,
                runtime_id=self.runtime._runtime_id,
                current=lambda: self._local_current(prepared),
                native=self._native,
            )
            if self.runtime.context_budgeted
            else None
        )

        def compile_context(pin):
            projection = window_context_projection(
                recent,
                source,
                prepared.provider.info,
                memory_context=pin,
                runtime_id=self.runtime._runtime_id,
            )
            return ContextProjection(
                [
                    {"role": "system", "content": self.runtime.personality},
                    *(
                        [{"role": "system", "content": projection.delivery}]
                        if projection.delivery is not None
                        else []
                    ),
                    *projection.messages,
                    {"role": "user", "content": w.text},
                ],
                projection.consumed_scopes,
                consumed_memory=projection.consumed_memory,
            )

        projection, lease = await self.runtime.memory.prepare(
            MemoryBinding(
                session_id=source.session_id, thread_id=source.thread_id, epoch=source.epoch
            ),
            prepared.provider,
            compile_context,
            lambda: self._abandon(prepared),
            lambda: self._local_current(prepared),
            lambda lease: self.runtime.attach_memory_lease(prepared, lease),
            budgeted_builder=budget.build if budget is not None else None,
        )
        prepared.memory_lease = lease
        if budget is not None:
            projection = (
                await lease.run(budget.finalize(projection))
                if lease is not None
                else await budget.finalize(projection)
            )
        prepared.content_scope = combine_scopes(
            w.content_scope, self.runtime.personality_scope, *projection.consumed_scopes
        )
        prepared.messages = projection.messages
        if await self.current(prepared) is None:
            self._abandon(prepared)
            return None
        self.runtime.observe_context(prepared, source, projection)
        return projection.messages

    async def generate(self, prepared):
        source = prepared.source
        try:
            w = SpeechWindow.model_validate(source.payload)
            selected = self.runtime.selected_provider_info()
            if not scope_allowed(selected, w.content_scope) or not scope_allowed(
                selected, self.runtime.personality_scope
            ):
                raise ValueError("Window disclosure does not permit the selected provider")
            self.runtime.capture_model(prepared)
            if not allowed_scope(prepared.provider, w.content_scope):
                raise ValueError("Window disclosure does not permit this provider")
            if not allowed_scope(prepared.provider, self.runtime.personality_scope):
                raise ValueError("Personality scope is not permitted for the selected provider")
            messages = await self._recover(lambda: self._prepare(prepared), prepared=prepared)
            if (
                messages is None
                or not self._local_current(prepared)
                or not self.runtime.typed_provider_ready(source.session_id)
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
            if asyncio.current_task().cancelling():
                # A provider cleanup failure must not swallow terminal stop
                # or start a diagnostic timeout under its cancellation count.
                self._abandon(prepared)
                raise asyncio.CancelledError from exc
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
            if prepared.abandoned or prepared.published == len(prepared.chunks):
                prepared.consumer_done.set()
            if prepared.model_lease is not None:
                prepared.model_cleanup = asyncio.create_task(self._finish_model(prepared))
            if prepared.memory_lease is not None:
                prepared.memory_lease.retain_cleanup(
                    prepared.cleanup, lambda: prepared.cleanup_failed
                )
                prepared.memory_lease.retain_cleanup(
                    prepared.model_cleanup, lambda: prepared.cleanup_failed
                )
                prepared.memory_lease.finish()
            if self.runtime._active.get(source.session_id) is prepared.task:
                self.runtime._active.pop(source.session_id, None)

    async def _finish_model(self, prepared):
        """Keep buffered publication and the actual adapter close owned at EOF."""
        try:
            if prepared.task is not None:
                await asyncio.gather(prepared.task, return_exceptions=True)
            if prepared.cleanup is not None:
                await asyncio.shield(prepared.cleanup)
            await prepared.consumer_done.wait()
            await self.runtime.settle_model_dependencies(prepared)
            if prepared.cleanup_failed:
                prepared.model_lease.cleanup_failed = True
                return
            await asyncio.shield(prepared.model_lease.start_close())
            if prepared.model_lease.cleanup_failed:
                prepared.cleanup_failed = True
        except (Exception, asyncio.CancelledError):
            prepared.cleanup_failed = True
            prepared.model_lease.cleanup_failed = True

    @asynccontextmanager
    async def _provider_stream(self, messages, prepared):
        with owned_http_stream_cleanup():
            async with self._owned_provider_stream(messages, prepared) as stream:
                yield stream

    @asynccontextmanager
    async def _owned_provider_stream(self, messages, prepared):
        if not allowed_scope(prepared.provider, prepared.content_scope):
            raise ValueError("Compiled context scope is not permitted for the provider")
        stream = prepared.provider.stream(messages)
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
        with self.runtime.model_preparation():
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
            await self._native_call(self.runtime.reconcile_stop, source.session_id)
            if not self._released(old):
                raise ProviderCleanupPending
            self.pending.pop(source.session_id, None)
            old = None
        if d.conversation == "WAIT_FOR_NEXT_WINDOW":
            if old is not None:
                self._abandon(old)
            # Typed admission may reserve a generation before its native user
            # append, active task or first chunk exists. WAIT fences that too.
            await self.yield_speech(source.session_id, force=True)
            if old is not None:
                if self._released(old):
                    self.pending.pop(source.session_id, None)
            await self.state(source, "waiting")
            return
        if not self.runtime.typed_provider_ready(source.session_id):
            raise ProviderCleanupPending("typed_cleanup_pending")
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
            await self.yield_speech(source.session_id)
            if not self._released(old):
                raise ProviderCleanupPending
        if source.session_id not in self.pending and len(self.pending) >= 32:
            # A completed cache entry owns no outstanding provider work. Drop
            # only its cache reference; its published audio remains current.
            key = next((key for key, p in self.pending.items() if self._reclaimable(p)), None)
            if key is None:
                raise ProviderCleanupPending("window_capacity_busy")
            self.pending.pop(key, None)
        await self.yield_speech(source.session_id)
        candidate.base_generation = (
            await self._native_call(self.runtime._epoch, source.session_id)
        ).generation
        candidate.task = WindowGenerationTask(self.generate(candidate))
        self.pending[source.session_id] = candidate

    async def tick(self):
        # Stops survive a completed/evicted reply and a failed transcript read.
        # Scan at most 32 cached epochs, rotating fairly, with one native budget.
        await self._reconcile_stops()
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
            if p.task is not None and p.task.done() and p.published == len(p.chunks):
                p.consumer_done.set()
            if p.abandoned and self._released(p) and self.pending.get(p.source.session_id) is p:
                self.pending.pop(p.source.session_id, None)
        return len(entries)

    async def close(self):
        for p in self.pending.values():
            self._abandon(p)
        tasks = {p.task for p in self.pending.values() if p.task is not None}
        if tasks:
            await asyncio.wait(tasks, timeout=self._provider_cleanup_timeout_s)
        await self._reconcile_stops()
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
