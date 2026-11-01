"""Own actual STT work and publication through request cancellation/cleanup."""

import asyncio
from contextlib import asynccontextmanager

from anyio import CancelScope

from packages.contracts.models import AppendRequest, JournalEntry, SpeechWindow
from services.converse.model_settings import _encode


class STTOperation:
    def __init__(self, runtime, lease):
        self.runtime, self.lease = runtime, lease
        self.provider = lease.provider
        self.active = True
        self.dependencies = set()
        self.cleanup_dependencies = set()
        self.cleanup = None
        self.started = False
        self.transcription_confirmed = False
        self.publications = set()
        self.request_task = asyncio.current_task()
        self.admission = runtime.admission()

    async def current(self, validate=None):
        def bound():
            return (
                self.active
                and self.lease.current()
                and self.runtime.admission_current(self.admission)
            )

        if not bound():
            return False
        if validate is not None and not await validate():
            return False
        return bound()

    async def wait(self, awaitable, *, publication=False):
        task = asyncio.create_task(awaitable)
        self.dependencies.add(task)
        if publication:
            self.publications.add(task)
        return await asyncio.shield(task)

    async def publish(self, controller, window):
        window = SpeechWindow.model_validate(window.model_dump())
        expected = AppendRequest(
            session_id=window.session_id,
            thread_id=window.thread_id,
            epoch=window.epoch,
            kind="conversation.window",
            payload=window.model_dump(mode="json"),
            idempotency_key=f"speech-window:{window.epoch}:{window.utterance_id}:{window.window_index}:r{window.revision}",
        )
        if len(expected.model_dump_json().encode("utf-8")) > 65536:
            raise ValueError("STT publication exceeds the native journal bound")

        async def append():
            observed = await controller.ingest(window)
            entry = JournalEntry.model_validate(observed.model_dump(), strict=True)
            if (
                entry.origin != "authenticated-user"
                or type(entry.sequence) is not int
                or not 1 <= entry.sequence <= 9_007_199_254_740_990
                or not 1 <= len(entry.event_id.encode("utf-8")) <= 128
                or _encode(
                    entry.model_dump(
                        mode="json", exclude={"event_id", "sequence", "origin", "created_at"}
                    )
                )
                != _encode(expected.model_dump(mode="json"))
            ):
                raise RuntimeError(
                    "STT publication acknowledgement does not match its original source"
                )
            return entry

        return await self.wait(append(), publication=True)

    def retire(self):
        self.active = False
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._retire())
            self.runtime._stt_finalizers.add(self.cleanup)
            self.cleanup.add_done_callback(self.runtime._stt_finalizers.discard)
            self.cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self.cleanup

    def start_native(self):
        if self.started:
            raise ValueError("STT operation was already dispatched")
        if self.lease.uncertain_native_worker:
            self.lease.owner.begin_native(self.lease)
        self.started = True

    async def _retire(self):
        try:
            if self.dependencies:
                await asyncio.gather(*self.dependencies, return_exceptions=True)
            if any(
                task.cancelled() or task.exception() is not None
                for task in self.cleanup_dependencies
            ):
                self.lease.cleanup_failed = True
            if getattr(self.lease.target, "_cleanup_failed", False) or getattr(
                self.lease.provider, "_cleanup_failed", False
            ):
                self.lease.cleanup_failed = True
            publication_failed = any(
                task.cancelled() or task.exception() is not None for task in self.publications
            )
            if publication_failed:
                self.lease.mark_unknown()
            if (
                self.transcription_confirmed
                and not self.lease.cleanup_failed
                and not self.lease.cleanup_unknown
            ):
                self.lease.owner.finish_native(self.lease)
            elif self.lease.admission_id is not None:
                self.lease.mark_unknown()
            await asyncio.shield(self.lease.start_close())
        finally:
            self.runtime._stt_operations.discard(self)


@asynccontextmanager
async def stt_operation(runtime, content_scope):
    if not getattr(runtime, "_stt_options_configured", False):
        # Explicit old trusted embeddings retain their host-only interface.
        yield None
        return
    if getattr(runtime, "_stt_stopping", False):
        raise RuntimeError("STT owner is shutting down")
    owner = runtime.stt_model_settings
    if runtime._stt_options_unavailable or owner is None:
        raise RuntimeError("STT model selection is unavailable")
    lease = owner.capture(content_scope)
    try:
        operation = STTOperation(runtime, lease)
    except BaseException:
        lease.start_close()
        raise
    runtime._stt_operations.add(operation)
    try:
        yield operation
    finally:
        task = operation.retire()
        with CancelScope(shield=True):
            await asyncio.wait((task,), timeout=1)


async def retire_converse_shared(runtime, journal, speech, windows=None):
    """Retain shared clients until their actual users finish, without a timeout reset."""
    runtime._stt_stopping = True
    runtime._retained_speech_clients = (journal, speech)

    async def finish():
        while True:
            tasks = set(runtime._stt_finalizers) | set(runtime._stt_transport_closures)
            for operation in runtime._stt_operations:
                tasks.update(operation.dependencies)
                tasks.update(
                    task for task in (operation.request_task, operation.cleanup) if task is not None
                )
            for operation in runtime._text.values():
                tasks.update(
                    task
                    for task in (operation.task, operation.monitor, operation.cleanup)
                    if task is not None
                )
            for group in runtime._speech.values():
                for operation in group:
                    tasks.update(
                        task for task in (operation.task, operation.cleanup) if task is not None
                    )
            for lease in runtime.memory.leases:
                tasks.update(lease.tasks)
            if runtime._memory_shutdown is not None:
                tasks.add(runtime._memory_shutdown)
            if windows is not None:
                for prepared in windows.pending.values():
                    tasks.update(
                        task
                        for task in (prepared.task, prepared.cleanup, prepared.model_cleanup)
                        if task is not None
                    )
            pending = {
                task for task in tasks if task is not asyncio.current_task() and not task.done()
            }
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            busy = any(runtime.model_operations().values())
        except Exception:
            busy = True
        owner = runtime.stt_model_settings
        if owner is not None:
            busy = busy or bool(owner.leases) or bool(owner._native_unsettled)
            await owner.close()
        if busy:
            # Native worker/publication/iterator uncertainty retains these
            # clients. A timeout or process-local reconnect is not settlement.
            return
        await journal.close()
        if speech is not None:
            await speech.close()
        runtime._retained_speech_clients = None

    if getattr(runtime, "_shared_speech_shutdown", None) is None:
        runtime._shared_speech_shutdown = asyncio.create_task(finish())
        runtime._shared_speech_shutdown.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
    with CancelScope(shield=True):
        await asyncio.wait((runtime._shared_speech_shutdown,), timeout=1)
