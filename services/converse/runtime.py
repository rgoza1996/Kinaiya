"""Independent personality runtime. It never calls Agent to produce an ordinary reply."""

import asyncio
import re
from collections.abc import Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from itertools import islice

from anyio import CancelScope

from packages.contracts.models import AppendRequest, ConverseRequest, DeliveryRequest, new_id
from packages.providers.disclosure import entry_allowed
from packages.providers.text import ProviderCleanupError, owned_http_stream_cleanup
from services.converse.intent import ConverseIntent


class SpeechChunker:
    def __init__(self):
        self.buffer = ""

    def feed(self, delta: str, final=False):
        self.buffer += delta
        result = []
        boundary = r"[.!?](?:[\"']?)(?=\s|$)" if final else r"[.!?](?:[\"']?)(?=\s)"
        while match := re.search(boundary, self.buffer):
            end = match.end()
            # Avoid splitting decimals and incomplete provider control tags.
            if "<" in self.buffer[:end] or (end < len(self.buffer) and self.buffer[end].isdigit()):
                break
            result.append(self.buffer[:end].strip())
            self.buffer = self.buffer[end:].lstrip()
        if final and self.buffer.strip():
            result.append(self.buffer.strip())
            self.buffer = ""
        return result


@dataclass
class PlaybackStop:
    """One immutable in-flight instruction; newer local fences only raise its target."""

    desired_generation: int
    confirmed_generation: int
    request: AppendRequest | None = None
    retired: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def covers(self, generation):
        return self.retired or self.confirmed_generation >= generation


@dataclass
class PlaybackEpoch:
    generation: int = 0
    chunks: dict = field(default_factory=dict)
    on_begin: Callable | None = field(default=None, repr=False, compare=False)
    scope: tuple[int, str] | None = None
    stop: PlaybackStop | None = None

    def begin(self):
        if self.on_begin is not None:
            self.on_begin()
        self.generation += 1
        self.chunks.clear()
        return self.generation

    def accepts(self, generation):
        return generation == self.generation


@dataclass
class PlaybackStart:
    playback: PlaybackEpoch
    request: AppendRequest | None = None


@dataclass(eq=False)
class SpeechOperation:
    session_id: str
    thread_id: str
    session_epoch: int
    event_id: str
    generation: int
    admission: tuple
    task: asyncio.Task
    queue: asyncio.Queue
    cancelled: bool = False
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup: asyncio.Task | None = None
    cleanup_failed: bool = False

    def cancel(self):
        if self.cancelled:
            return
        self.cancelled = True
        self.stopped.set()
        if not self.task.done():
            self.task.cancel()
        # Wake a reader even when the provider has never produced its first frame.
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(("cancelled", None))


class TextGenerationTask(asyncio.Task):
    """A terminal stop cannot repeatedly cancel a provider's finally block."""

    def cancel(self, msg=None):
        if self.cancelling():
            return False
        return super().cancel(msg)


@dataclass(eq=False)
class TextOperation:
    session_id: str
    thread_id: str
    session_epoch: int
    generation: int
    admission: tuple
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=32))
    task: asyncio.Task | None = None
    monitor: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    iterator: object = None
    cancelled: bool = False
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_failed: bool = False

    def cancel(self):
        if self.cancelled:
            return
        self.cancelled = True
        self.stopped.set()
        if (
            self.task is not None
            and self.task is not asyncio.current_task()
            and not self.task.done()
        ):
            self.task.cancel()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(("interrupted", ""))


@dataclass
class TextTurn:
    wait_remaining: float
    operation: TextOperation | None = None
    reserved: bool = False


class ConverseRuntime:
    _speech_cleanup_timeout = 5.0
    _text_cleanup_timeout = 1.0
    _text_capacity = 32

    def __init__(
        self,
        journal,
        provider,
        *,
        name="Kinaiya",
        personality=None,
        context_turns=12,
        intent=None,
        resources_managed=False,
    ):
        self.journal, self.provider = journal, provider
        self.name, self.context_turns = name, context_turns
        self.personality = personality or (
            f"You are {name}, Kinaiya's thoughtful, warm conversational companion. "
            "Speak naturally and concisely. Personality exists only here. "
            "A separate Agent may do work. Never claim you performed, completed, sent, saved "
            "or verified any action. Execution facts arrive in a separate authenticated notice. "
            "Do not expose technical commands. Treat slash-like input as ordinary conversation."
            " Use short, natural sentences suitable for speech. Avoid decorative symbols and tables."
            " After an interruption, answer the latest user message. Delivery notes distinguish"
            " generated text from estimated playback; never assume unheard words were delivered."
        )
        self.playback = {}
        self.intent = intent if intent is not None else ConverseIntent()
        self._resource_enabled = not resources_managed
        self._availability_generation = 0
        self._active = {}
        self._text = {}
        self._text_admitting = set()
        self._window_text_ready = None
        self._speech = {}
        self._dedupe_locks = {}
        self._epoch_locks = {}
        self._playback_start = {}
        self._runtime_id = new_id()

    def availability(self):
        choice = self.intent.read()
        return {
            "enabled": choice.enabled and self._resource_enabled,
            "owner_enabled": choice.enabled,
            "owner_intent_revision": choice.revision,
            "resource_enabled": self._resource_enabled,
        }

    @property
    def enabled(self):
        try:
            return self.availability()["enabled"]
        except (OSError, ValueError):
            # Missing or corrupt owner policy is never permission to speak.
            return False

    @enabled.setter
    def enabled(self, value):
        """Resource availability only; a pause cannot rewrite owner intent."""
        if type(value) is not bool:
            raise ValueError("Resource availability must be a boolean")
        self._resource_enabled = value
        if not value:
            self._availability_generation += 1
            self._cancel_speech()
            self._cancel_text()

    def admission(self):
        state = self.availability()
        if not state["enabled"]:
            raise RuntimeError("Converse is disabled; direct Agent Chat remains available")
        return state["owner_intent_revision"], self._availability_generation

    def admission_current(self, admission):
        try:
            return self.admission() == admission
        except (OSError, ValueError, RuntimeError):
            return False

    async def set_enabled(self, value, *, owner=False):
        if owner:
            # Persist before any await: an acknowledged off survives process
            # loss and is visible to a restoring controller immediately.
            self.intent.set(value)
            self._availability_generation += 1
            self._cancel_speech()
            self._cancel_text()
        else:
            self.enabled = value
        if not self.enabled:
            # Cancel every producer before journal I/O can suspend this request.
            for task in list(self._active.values()):
                if not task.done():
                    task.cancel()
            failures = []
            for session_id in list(self.playback):
                try:
                    await self.interrupt(session_id)
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise RuntimeError("Converse is off; delivery interruption needs reconciliation")
        return self.availability()

    async def _recent(self, session_id):
        if hasattr(self.journal, "recent"):
            return await self.journal.recent(session_id=session_id, limit=100)
        return await self.journal.read(session_id=session_id, limit=100)

    async def _epoch(self, session_id):
        lock = self._epoch_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = await self.journal.session(session_id)
            scope = session.epoch, session.thread_id
            cached = self.playback.get(session_id)
            if cached is not None:
                if cached.scope == scope:
                    await self.reconcile_stop(session_id, cached)
                    session = await self.journal.session(session_id)
                    scope = session.epoch, session.thread_id
                    if cached.scope == scope:
                        return cached
                self._retire_stop(cached)
                cached.begin()
                self.playback.pop(session_id, None)
            start = self._playback_start.get(session_id)
            if start is not None and start.playback.scope != scope:
                self._retire_stop(start.playback)
                self._playback_start.pop(session_id, None)
                start = None
            if start is None:
                existing = await self._recent(session_id)
                generations = [
                    value
                    for event in existing
                    if event.epoch == session.epoch
                    for key in ("generation_epoch", "next_generation_epoch")
                    if type(value := event.payload.get(key)) is int and value >= 0
                ]
                previous = max(generations, default=0)
                generation = previous + 1 if previous else 0
                playback = PlaybackEpoch(
                    generation,
                    on_begin=lambda: self._cancel_generation(session_id),
                    scope=scope,
                )
                start = PlaybackStart(playback)
                if previous:
                    # A lost acknowledgment may already retain this boundary.
                    # Reusing its exact request avoids deriving a new counter
                    # under the same runtime/epoch idempotency key on retry.
                    start.request = AppendRequest(
                        session_id=session_id,
                        thread_id=session.thread_id,
                        epoch=session.epoch,
                        kind="converse.safety",
                        payload={
                            "reason": "playback_restart_barrier",
                            "generation_epoch": generation,
                            "runtime_id": self._runtime_id,
                        },
                        idempotency_key=f"playback-start:{self._runtime_id}:{session.epoch}",
                    )
                    self._queue_stop(session_id, playback, previous, generation)
                self._playback_start[session_id] = start
            if start.request is not None:
                await self.journal.append(start.request)
            # A safety note alone is not the browser's delivery-generation
            # control. Confirm that instruction before exposing recovered audio.
            await self.reconcile_stop(session_id, start.playback)
            current = await self.journal.session(session_id)
            if (current.epoch, current.thread_id) != scope:
                self._retire_stop(start.playback)
                self._playback_start.pop(session_id, None)
                raise ValueError("Session changed during playback initialization")
            self.playback[session_id] = start.playback
            self._playback_start.pop(session_id, None)
        return self.playback[session_id]

    @staticmethod
    def _retire_stop(playback):
        if playback.stop is not None:
            # Native reset makes this scope obsolete, not audibly delivered.
            playback.stop.retired = True

    def _stop_request(self, session_id, playback, previous, target, source_id=None):
        epoch, thread_id = playback.scope
        return AppendRequest(
            session_id=session_id,
            thread_id=thread_id,
            epoch=epoch,
            kind="delivery.updated",
            causes=[source_id] if source_id is not None else [],
            payload={
                "generation_epoch": previous,
                "next_generation_epoch": target,
                "status": "interrupted",
                "alignment": "delivery_unknown",
                "control": "generation_stop",
                "runtime_id": self._runtime_id,
                **({"source_event_id": source_id} if source_id is not None else {}),
            },
            idempotency_key=f"playback-stop:{self._runtime_id}:{epoch}:{target}",
        )

    def _queue_stop(self, session_id, playback, previous, target, source_id=None):
        stop = playback.stop
        if stop is None or stop.retired:
            stop = playback.stop = PlaybackStop(target, previous)
        stop.desired_generation = max(stop.desired_generation, target)
        if stop.request is None and not stop.covers(target):
            stop.request = self._stop_request(session_id, playback, previous, target, source_id)
        return stop

    def fence_playback(self, session_id, *, source=None, expected_generation=None):
        """Fence local output synchronously, retaining the client instruction before I/O."""
        playback = self.playback[session_id]
        if source is not None and (
            source.session_id != session_id or (source.epoch, source.thread_id) != playback.scope
        ):
            raise ValueError("Obsolete source cannot stop a different playback scope")
        previous = playback.generation
        if expected_generation is not None and previous != expected_generation:
            raise ValueError("Obsolete interruption cannot stop a newer speech generation")
        source_id = source.event_id if source is not None else None
        if source_id is None and playback.chunks:
            entry = next(reversed(playback.chunks.values()))
            source_id = entry.payload.get("window_source_id", entry.event_id)
        target = playback.begin()
        stop = self._queue_stop(session_id, playback, previous, target, source_id)
        task = self._active.get(session_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        return stop, target

    async def reconcile_stop(self, session_id, playback=None):
        playback = playback if playback is not None else self.playback[session_id]
        stop = playback.stop
        if stop is None or stop.retired or stop.covers(stop.desired_generation):
            return
        async with stop.lock:
            # Two writes suffice for the original request and one coalesced
            # higher target. Further concurrent stops remain owned for a tick.
            for _ in range(2):
                if stop.retired or stop.covers(stop.desired_generation):
                    return
                current = await self.journal.session(session_id)
                if (current.epoch, current.thread_id) != playback.scope:
                    self._retire_stop(playback)
                    return
                if stop.retired or stop.covers(stop.desired_generation):
                    return
                if stop.request is None:
                    stop.request = self._stop_request(
                        session_id, playback, stop.confirmed_generation, stop.desired_generation
                    )
                request = stop.request
                await self.journal.append(request)
                current = await self.journal.session(session_id)
                if (current.epoch, current.thread_id) != playback.scope:
                    self._retire_stop(playback)
                    return
                stop.confirmed_generation = request.payload["next_generation_epoch"]
                stop.request = None
            if not stop.covers(stop.desired_generation):
                raise RuntimeError("Newer playback stop needs reconciliation")

    def stop_reconciliation_batch(self, limit):
        # Rotate the existing playback map rather than scan retained history or
        # maintain another growing queue. Even nonpending sessions cost one slot.
        for session_id in list(islice(self.playback, limit)):
            playback = self.playback.pop(session_id, None)
            if playback is None:
                continue
            self.playback[session_id] = playback
            if playback.stop is not None and not playback.stop.covers(
                playback.stop.desired_generation
            ):
                yield session_id, playback

    def _cancel_generation(self, session_id):
        self._cancel_speech(session_id)
        self._cancel_text(session_id)

    def _cancel_text(self, session_id=None):
        operations = (
            list(self._text.values()) if session_id is None else [self._text.get(session_id)]
        )
        for operation in operations:
            if operation is not None:
                operation.cancel()

    def typed_provider_ready(self, session_id):
        """Read-only admission hook; stopping never waits for readiness."""
        return session_id not in self._text and session_id not in self._text_admitting

    def text_status(self):
        operations = list(self._text.values())
        failed = sum(operation.cleanup_failed for operation in operations)
        pending = sum(
            not operation.cleanup_failed
            and (operation.cancelled or (operation.task is not None and operation.task.done()))
            for operation in operations
        )
        return {
            "active": len(operations) - pending - failed,
            "cleanup_pending": pending,
            "cleanup_failed": failed,
            "admitting": len(self._text_admitting),
            "capacity": self._text_capacity,
        }

    def _text_current(self, operation):
        playback = self.playback.get(operation.session_id)
        return (
            not operation.cancelled
            and self.admission_current(operation.admission)
            and playback is not None
            and playback.accepts(operation.generation)
        )

    async def _watch_text(self, operation):
        while not operation.stopped.is_set() and self._text_current(operation):
            try:
                session = await asyncio.wait_for(self.journal.session(operation.session_id), 1)
            except Exception:
                break
            if operation.stopped.is_set():
                return
            if (session.epoch, session.thread_id) != (operation.session_epoch, operation.thread_id):
                break
            await asyncio.sleep(0.25)
        if not operation.stopped.is_set():
            operation.cancel()

    def _release_text(self, operation):
        if self._text.get(operation.session_id) is operation:
            self._text.pop(operation.session_id, None)
        if self._active.get(operation.session_id) is operation.task:
            self._active.pop(operation.session_id, None)

    async def _finish_text_cleanup(self, operation):
        try:
            await asyncio.gather(operation.task, return_exceptions=True)
            if operation.monitor is not None:
                operation.stopped.set()
                operation.monitor.cancel()
                await asyncio.gather(operation.monitor, return_exceptions=True)
            close = getattr(operation.iterator, "aclose", None)
            if callable(close):
                await close()
        except (Exception, asyncio.CancelledError):
            operation.cleanup_failed = True
        if not operation.cleanup_failed:
            self._release_text(operation)

    async def _wait_text_cleanup(self, cleanups, timeout):
        """Bound only this caller's wait; retain the actual close task untouched."""
        deadline = asyncio.get_running_loop().time() + max(0, timeout)
        interrupted = False
        pending = set(cleanups)
        with CancelScope(shield=True):
            while pending:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    _, pending = await asyncio.wait(pending, timeout=remaining)
                except asyncio.CancelledError:
                    interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    async def _wait_text_admission(self, session_id, turn):
        started = asyncio.get_running_loop().time()
        deadline = started + turn.wait_remaining
        try:
            while session_id in self._text or (
                self._window_text_ready is not None and not self._window_text_ready(session_id)
            ):
                operation = self._text.get(session_id)
                if operation is not None and operation.cleanup_failed:
                    raise RuntimeError(
                        "Previous text-provider cleanup failed; inspect Converse health"
                    )
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise RuntimeError(
                        "Previous text-provider cleanup is unconfirmed; try again later"
                    )
                await asyncio.sleep(min(0.01, remaining))
        finally:
            turn.wait_remaining = max(
                0, turn.wait_remaining - (asyncio.get_running_loop().time() - started)
            )

    @asynccontextmanager
    async def _text_admission_lock(self, session_id, turn):
        lock = self._dedupe_locks.setdefault(session_id, asyncio.Lock())
        started = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(turn.wait_remaining):
                await lock.acquire()
        except TimeoutError as exc:
            raise RuntimeError("Text-provider admission/cleanup is busy; try again later") from exc
        finally:
            turn.wait_remaining = max(
                0, turn.wait_remaining - (asyncio.get_running_loop().time() - started)
            )
        try:
            yield
        finally:
            lock.release()

    async def close(self):
        self._cancel_text()
        cleanups = {operation.cleanup for operation in self._text.values() if operation.cleanup}
        if cleanups:
            # One shared deadline, rather than one second for every owner.
            await self._wait_text_cleanup(cleanups, self._text_cleanup_timeout)

    def _cancel_speech(self, session_id=None):
        sessions = list(self._speech) if session_id is None else [session_id]
        for key in sessions:
            for operation in list(self._speech.get(key, ())):
                operation.cancel()

    def speech_status(self):
        operations = [operation for group in self._speech.values() for operation in group]
        return {
            "active": sum(not operation.cancelled for operation in operations),
            "cancellation_pending": sum(operation.cancelled for operation in operations),
            "cleanup_failed": sum(operation.cleanup_failed for operation in operations),
        }

    def check_speech_ready(self):
        if self.speech_status()["cancellation_pending"]:
            raise ValueError("Previous speech cleanup is unconfirmed; inspect Converse health")

    async def _watch_speech(self, operation):
        # Native control hooks cancel synchronously. This bounded monitor also
        # observes owner-file changes and session resets made by another service;
        # it never scans transcript history or blocks an interrupt on journal I/O.
        while not operation.cancelled:
            if not self.admission_current(operation.admission):
                operation.cancel()
                return
            try:
                session = await asyncio.wait_for(
                    self.journal.session(operation.session_id), timeout=1
                )
            except Exception:
                operation.cancel()
                return
            # On Python 3.11 a simultaneous wait_for completion can consume
            # task cancellation. The operation token remains authoritative.
            if operation.cancelled:
                return
            if (session.epoch, session.thread_id) != (
                operation.session_epoch,
                operation.thread_id,
            ):
                operation.cancel()
                return
            await asyncio.sleep(0.25)

    async def _speech_valid(self, operation):
        validation = asyncio.create_task(
            self.validate_speech(operation.event_id, operation.generation)
        )
        stopped = asyncio.create_task(operation.stopped.wait())
        try:
            await asyncio.wait((validation, stopped), return_when=asyncio.FIRST_COMPLETED)
            if operation.cancelled:
                return False
            try:
                await validation
            except ValueError:
                return False
            return True
        finally:
            for task in (validation, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(validation, stopped, return_exceptions=True)

    async def speech_frames(self, event, generation, synthesize):
        """Own one cancellable upstream stream, independent of the response reader.

        Cancelling its task closes the HTTP iterator; it is not confirmation that
        a model kernel or a physical playback device has stopped.
        """
        queue = asyncio.Queue(maxsize=1)
        iterator = None
        close_failed = False

        async def produce():
            nonlocal iterator, close_failed
            try:
                try:
                    await self.validate_speech(event.event_id, generation)
                    iterator = synthesize(event.payload["text"])
                    async for chunk in iterator:
                        if operation is None or operation.cancelled:
                            return
                        await queue.put(("frame", chunk))
                finally:
                    if iterator is not None:
                        try:
                            await iterator.aclose()
                        except Exception:
                            close_failed = True
                            raise
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if operation is not None and operation.cancelled:
                    close_failed = True
                else:
                    await queue.put(("error", exc))
            else:
                await queue.put(("done", None))

        producer = asyncio.create_task(produce())
        monitor = None
        operation = None
        try:
            # No await between the local admission check and registration. A
            # generation/off transition cannot miss this operation while the
            # producer's authoritative asynchronous validation is suspended.
            try:
                admission = self.admission()
                self.check_speech_ready()
            except (OSError, ValueError, RuntimeError):
                return
            playback = self.playback.get(event.session_id)
            if (
                playback is None
                or not playback.accepts(generation)
                or event.payload.get("runtime_id") != self._runtime_id
                or event.payload.get("generation_epoch") != generation
                or event.payload.get("owner_intent_revision", admission[0]) != admission[0]
                or event.payload.get("availability_generation", admission[1]) != admission[1]
            ):
                return
            operation = SpeechOperation(
                event.session_id,
                event.thread_id,
                event.epoch,
                event.event_id,
                generation,
                admission,
                producer,
                queue,
            )
            self._speech.setdefault(event.session_id, set()).add(operation)
            monitor = asyncio.create_task(self._watch_speech(operation))
            while True:
                kind, value = await queue.get()
                if operation.cancelled or kind in {"done", "cancelled"}:
                    return
                if kind == "error":
                    raise value
                if (
                    not await self._speech_valid(operation)
                    or operation.cancelled
                    or not self.admission_current(admission)
                    or not playback.accepts(generation)
                ):
                    return
                yield value
        finally:
            # Starlette disconnect uses a level-triggered cancellation scope.
            # Shield cleanup so its repeated cancellation cannot interrupt the
            # upstream iterator's finally block and orphan the response socket.
            with CancelScope(shield=True):
                if operation is not None:
                    operation.cancel()
                elif not producer.done():
                    producer.cancel()
                if monitor is not None:
                    monitor.cancel()

                async def finish_cleanup():
                    try:
                        await asyncio.gather(
                            producer,
                            *([monitor] if monitor is not None else []),
                            return_exceptions=True,
                        )
                        if iterator is not None:
                            await iterator.aclose()
                        if close_failed:
                            raise RuntimeError("Upstream speech iterator cleanup failed")
                    except Exception:
                        if operation is not None:
                            operation.cleanup_failed = True
                    else:
                        if operation is not None:
                            operations = self._speech.get(event.session_id)
                            if operations is not None:
                                operations.discard(operation)
                                if not operations:
                                    self._speech.pop(event.session_id, None)

                cleanup = asyncio.create_task(finish_cleanup())
                if operation is not None:
                    operation.cleanup = cleanup
                # Wait at most five seconds without cancelling a finally block
                # that may own an HTTP socket. A timed-out operation stays in
                # the registry/health and refuses new speech until confirmed.
                await asyncio.wait((cleanup,), timeout=self._speech_cleanup_timeout)

    async def interrupt(self, session_id, expected_generation=None):
        epoch = self.playback.get(session_id)
        if epoch is None:
            epoch = await self._epoch(session_id)
        self.fence_playback(session_id, expected_generation=expected_generation)
        await self.reconcile_stop(session_id, epoch)
        return {
            "generation_epoch": epoch.generation,
            "speech_stopped": True,
            "agent_cancelled": False,
        }

    async def events(self, request: ConverseRequest):
        turn = TextTurn(self._text_cleanup_timeout)
        try:
            async with aclosing(self._events(request, turn)) as events:
                async for event in events:
                    yield event
        finally:
            if turn.reserved:
                self._text_admitting.discard(request.session_id)
            operation = turn.operation
            if operation is not None:
                operation.cancel()
                if operation.task is None:
                    self._release_text(operation)
                else:
                    await self._wait_text_cleanup((operation.cleanup,), turn.wait_remaining)

    async def _events(self, request, turn):
        admission = self.admission()
        session = await self.journal.session(request.session_id)
        if session.channel != "converse" or (session.epoch, session.thread_id) != (
            request.epoch,
            request.thread_id,
        ):
            raise ValueError("Wrong conversation channel, thread or stale epoch")
        # Serialize only duplicate admission; ordinary turns can interrupt prior generation.
        async with self._text_admission_lock(request.session_id, turn):
            existing = await self._recent(request.session_id)
            session = await self.journal.session(request.session_id)
            if (session.epoch, session.thread_id) != (request.epoch, request.thread_id):
                raise ValueError("Session changed during turn admission")
            prior_user = next(
                (e for e in existing if e.idempotency_key == request.idempotency_key), None
            )
            if prior_user:
                if (
                    prior_user.kind != "conversation.user"
                    or (prior_user.epoch, prior_user.thread_id)
                    != (request.epoch, request.thread_id)
                    or prior_user.payload.get("text") != request.text
                    or prior_user.payload.get("input_mode", "text") != request.input_mode
                ):
                    raise ValueError("Idempotency key reused with different content")
                replies = [
                    e
                    for e in existing
                    if prior_user.event_id in e.causes and e.kind == "conversation.converse"
                ]
                if not self.admission_current(admission):
                    raise RuntimeError("Converse availability changed during turn admission")
            else:
                if not self.admission_current(admission):
                    raise RuntimeError("Converse availability changed during turn admission")
                slots = self._text.keys() | self._text_admitting
                if request.session_id not in slots and len(slots) >= self._text_capacity:
                    raise RuntimeError("Text-provider cleanup capacity is busy; try again later")
                # Reserve before cancellation or any new user/provider side effect. A
                # window cannot acquire this session while old real cleanup unwinds.
                self._text_admitting.add(request.session_id)
                turn.reserved = True
                playback = await self._epoch(request.session_id)
                session = await self.journal.session(request.session_id)
                if (session.epoch, session.thread_id) != (request.epoch, request.thread_id):
                    raise ValueError("Session changed during turn admission")
                if not self.admission_current(admission):
                    raise RuntimeError("Converse availability changed during turn admission")
                old = self._active.get(request.session_id)
                if old and not old.done():
                    old.cancel()
                generation = playback.begin()
                await self._wait_text_admission(request.session_id, turn)
                session = await self.journal.session(request.session_id)
                if (session.epoch, session.thread_id) != (request.epoch, request.thread_id):
                    raise ValueError("Session changed during turn admission")
                if not self.admission_current(admission) or not playback.accepts(generation):
                    raise RuntimeError(
                        "Converse availability or generation changed during admission"
                    )
                operation = TextOperation(
                    request.session_id, request.thread_id, request.epoch, generation, admission
                )
                turn.operation = operation
                self._text[request.session_id] = operation
                self._text_admitting.discard(request.session_id)
                turn.reserved = False
                source = await self.journal.append(
                    AppendRequest(
                        session_id=request.session_id,
                        thread_id=request.thread_id,
                        epoch=request.epoch,
                        kind="conversation.user",
                        payload={
                            "text": request.text,
                            "final": True,
                            "revision": 1,
                            "input_mode": request.input_mode,
                            "generation_epoch": generation,
                        },
                        idempotency_key=request.idempotency_key,
                    )
                )
        if prior_user:
            for reply in replies:
                if not self.admission_current(admission):
                    raise RuntimeError("Converse availability changed during replay")
                yield {"type": "text", "entry": reply.model_dump(mode="json"), "replay": True}
            yield {"type": "done", "replay": True, "pending": not bool(replies)}
            return
        history = [
            e
            for e in existing
            if e.epoch == request.epoch
            and e.kind in {"conversation.user", "conversation.converse"}
            and entry_allowed(self.provider.info, e)
        ][-self.context_turns :]
        messages = [{"role": "system", "content": self.personality}]
        delivery = [
            e for e in existing if e.kind == "delivery.updated" and e.epoch == request.epoch
        ]
        if delivery:
            latest = delivery[-8:]
            notes = [
                {
                    key: e.payload[key]
                    for key in ("event_id", "status", "alignment", "estimated_text_fraction")
                    if key in e.payload
                }
                for e in latest
            ]
            import json

            messages.append(
                {
                    "role": "system",
                    "content": "Playback delivery notes (estimates, not word alignment): "
                    + json.dumps(notes),
                }
            )
        messages += [
            {
                "role": "user" if e.kind == "conversation.user" else "assistant",
                "content": e.payload["text"],
            }
            for e in history
        ]
        messages.append({"role": "user", "content": request.text})
        chunker, index = SpeechChunker(), 0
        queue = operation.queue

        async def produce():
            try:
                # Protect the transport before __anext__: EOF and provider error
                # can enter HTTP close before our independent aclose owner runs.
                with owned_http_stream_cleanup():
                    operation.iterator = aiter(self.provider.stream(messages))
                    async for delta in operation.iterator:
                        if not self._text_current(operation):
                            operation.cancel()
                            return
                        await queue.put(("delta", delta))
                    await queue.put(("final", ""))
            except asyncio.CancelledError:
                operation.cancel()
            except Exception as exc:
                if isinstance(exc, ProviderCleanupError):
                    operation.cleanup_failed = True
                if operation.cancelled or asyncio.current_task().cancelling():
                    operation.cancel()
                    return
                await queue.put(("error", type(exc).__name__))

        if not self._text_current(operation):
            raise RuntimeError("Converse availability changed during turn admission")
        operation.task = TextGenerationTask(produce())
        operation.monitor = asyncio.create_task(self._watch_text(operation))
        operation.cleanup = asyncio.create_task(self._finish_text_cleanup(operation))
        self._active[request.session_id] = operation.task
        yield {
            "type": "accepted",
            "source_event_id": source.event_id,
            "generation_epoch": generation,
        }
        while True:
            kind, value = await queue.get()
            if not self._text_current(operation) or kind == "interrupted":
                yield {"type": "interrupted", "generation_epoch": generation}
                break
            if kind == "error":
                yield {"type": "error", "error": value}
                break
            chunks = chunker.feed(value, final=kind == "final")
            for text in chunks:
                if not self._text_current(operation):
                    yield {"type": "interrupted", "generation_epoch": generation}
                    return
                # Defense in depth for unsupported execution claims. This conservative
                # English guard is not claimed as a proof against every paraphrase.
                if re.search(
                    r"\b(done|completed?|finished|sent|saved|created|deleted|verified)\b",
                    text,
                    re.I,
                ):
                    await self.journal.append(
                        AppendRequest(
                            session_id=request.session_id,
                            thread_id=request.thread_id,
                            epoch=request.epoch,
                            kind="converse.safety",
                            payload={
                                "reason": "ungrounded_action_claim",
                                "generation_epoch": generation,
                            },
                            causes=[source.event_id],
                            idempotency_key=f"guard:{source.event_id}:{index}",
                        )
                    )
                    text = "The Agent has not provided a verified result for that claim."
                if not self._text_current(operation):
                    yield {"type": "interrupted", "generation_epoch": generation}
                    return
                event = await self.journal.append(
                    AppendRequest(
                        session_id=request.session_id,
                        thread_id=request.thread_id,
                        epoch=request.epoch,
                        kind="conversation.converse",
                        payload={
                            "text": text,
                            "final": True,
                            "revision": 1,
                            "generation_epoch": generation,
                            "runtime_id": self._runtime_id,
                            "owner_intent_revision": admission[0],
                            "availability_generation": admission[1],
                            "chunk_id": new_id(),
                            "delivery": "generated",
                        },
                        causes=[source.event_id],
                        idempotency_key=f"reply:{source.event_id}:{index}",
                    )
                )
                if not self._text_current(operation):
                    yield {"type": "interrupted", "generation_epoch": generation}
                    return
                playback.chunks[event.event_id] = event
                yield {"type": "text", "entry": event.model_dump(mode="json")}
                index += 1
            if kind == "final":
                yield {"type": "done", "generation_epoch": generation}
                break

    async def turn(self, request):
        events = [event async for event in self.events(request)]
        return {
            "events": events,
            "text": " ".join(e["entry"]["payload"]["text"] for e in events if e["type"] == "text"),
        }

    async def validate_speech(self, event_id, generation_epoch):
        event = await self.journal.entry(event_id)
        session = await self.journal.session(event.session_id)
        playback = await self._epoch(event.session_id)
        try:
            admission = self.admission()
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError("Obsolete or unauthorized speech") from exc
        if (
            not self.enabled
            or session.epoch != event.epoch
            or event.origin != "converse"
            or event.kind != "conversation.converse"
            or event.payload.get("runtime_id") != self._runtime_id
            or not playback.accepts(generation_epoch)
            or event.payload.get("generation_epoch") != generation_epoch
            or event.payload.get("owner_intent_revision", admission[0]) != admission[0]
            or event.payload.get("availability_generation", admission[1]) != admission[1]
        ):
            raise ValueError("Obsolete or unauthorized speech")
        if event.payload.get("window_source_id"):
            source = await self.journal.resolve(event.payload["window_source_id"])
            if source.event_id != event.payload["window_source_id"] or source.epoch != event.epoch:
                raise ValueError("Speech window was corrected or became obsolete")
            latest = await self.journal.latest_window(
                event.session_id, event.epoch, source.payload["utterance_id"]
            )
            if latest is None or latest.payload["text"] != source.payload["text"]:
                raise ValueError("Speech hypothesis was superseded before playback")
            control = await self.journal.window_control(event.session_id, event.epoch)
            if control is not None and control.payload["source_sequence"] > source.sequence:
                newer = await self.journal.resolve(control.payload["source_event_id"])
                if control.payload["conversation"] == "WAIT_FOR_NEXT_WINDOW" or (
                    newer.payload.get("utterance_id"),
                    newer.payload.get("text"),
                ) != (source.payload.get("utterance_id"), source.payload.get("text")):
                    raise ValueError("Later conversation control revoked this speech")
        if event.payload.get("outcome_event_id"):
            outcome = await self.journal.entry(event.payload["outcome_event_id"])
            task = outcome.payload.get("task", outcome.payload)
            latest = await self.journal.read(task_id=task["task_id"], limit=200)
            newer = [
                e
                for e in latest
                if e.sequence > outcome.sequence
                and e.kind in {"agent.command", "agent.state", "agent.outcome"}
            ]
            if newer:
                raise ValueError("Outcome changed before playback")
        if not self.admission_current(admission) or not playback.accepts(generation_epoch):
            raise ValueError("Obsolete or unauthorized speech")
        return event

    async def acknowledge_delivery(self, request: DeliveryRequest):
        event = await self.validate_speech(request.event_id, request.generation_epoch)
        if request.chunk_id != event.payload["chunk_id"]:
            raise ValueError("Wrong speech chunk")
        return await self.journal.append(
            AppendRequest(
                session_id=event.session_id,
                thread_id=event.thread_id,
                epoch=event.epoch,
                kind="delivery.updated",
                payload={**request.model_dump(mode="json"), "alignment": "conservative_estimate"},
                causes=[event.event_id],
                idempotency_key=f"delivery:{event.event_id}:{request.status}",
            )
        )

    async def outcome_notice(self, outcome):
        task = outcome.payload.get("task", outcome.payload)
        if (
            outcome.kind != "agent.outcome"
            or outcome.origin != "agent-supervisor"
            or task.get("state") != "succeeded"
            or not task.get("evidence")
            or task.get("remaining_required_work")
        ):
            raise ValueError("Only evidenced successful runtime outcomes may announce completion")
        # MCP and compute receipts identify a response/computation, not an
        # independently verified task effect.
        current = [
            e
            for e in (task.get("evidence") or [])
            if isinstance(e, dict) and e.get("objective_revision") == task.get("objective_revision")
        ]
        native = [e for e in current if e.get("kind") not in {"external", "extension"}]
        independent = any(
            e.get("kind") in {"file", "command", "test"}
            or (e.get("kind") == "read" and e.get("path") and e.get("sha256"))
            for e in native
        )
        if not native or (
            any(e.get("kind") in {"external", "extension"} for e in current) and not independent
        ):
            raise ValueError(
                "Only unverified tool/computation receipts support this outcome; "
                "independent native evidence is required"
            )
        session = await self.journal.session(outcome.session_id)
        if session.epoch != outcome.epoch or session.channel != "converse" or not self.enabled:
            return None
        try:
            admission = self.admission()
        except (OSError, ValueError, RuntimeError):
            return None
        playback = await self._epoch(session.session_id)
        scopes = []
        for source_id in task.get("source_event_ids", []):
            try:
                source = await self.journal.resolve(source_id)
                if source.kind == "conversation.window" or "content_scope" in source.payload:
                    scopes.append(source.payload.get("content_scope", "local_only"))
            except (KeyError, ValueError):
                scopes.append("local_only")
        scope_fields = {}
        if scopes:
            scope_fields["content_scope"] = next(
                s
                for s in ("local_only", "private_cloud_authorized", "public_synthetic")
                if s in scopes
            )
        # Completion does not cancel current social speech. It joins the current epoch.
        if not self.admission_current(admission):
            return None
        if playback.generation == 0:
            playback.begin()
        event = await self.journal.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.converse",
                payload={
                    "text": "The Agent reports a verified result: "
                    + task.get("summary", "Task completed."),
                    "final": True,
                    "revision": 1,
                    "generation_epoch": playback.generation,
                    "runtime_id": self._runtime_id,
                    "owner_intent_revision": admission[0],
                    "availability_generation": admission[1],
                    "chunk_id": new_id(),
                    "delivery": "generated",
                    "outcome_event_id": outcome.event_id,
                    "objective_revision": task["objective_revision"],
                    **scope_fields,
                },
                causes=[outcome.event_id],
                task_refs=[task["task_id"]],
                idempotency_key=f"outcome-notice:{outcome.event_id}",
            )
        )
        await self.validate_speech(event.event_id, playback.generation)
        return event
