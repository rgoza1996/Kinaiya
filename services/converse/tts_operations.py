"""Actual synthesis, publication and playback ownership for one captured voice."""

import asyncio
import hashlib
import secrets

from packages.contracts.models import AppendRequest, DeliveryRequest, JournalEntry
from packages.contracts.tts_models import TTSSynthesisAdmission
from services.converse.model_settings import _encode


class TTSTurn:
    def __init__(self, runtime, lease, event, generation):
        self.runtime, self.lease, self.event, self.generation = runtime, lease, event, generation
        self._source_identity = _encode(event.model_dump(mode="json"))
        self.operation_id = secrets.token_hex(16)
        self.started = False
        self.admitted = False
        self.producer_completed = False
        self.completion_check = lambda: False
        self.producer_cleanup = None
        self.publications = set()
        self.admission_publication = None
        self.request_done = asyncio.Event()
        self.delivery_done = asyncio.Event()
        self.cleanup = None
        self.request_task = asyncio.current_task()
        self.active = True
        self.delivery_requests = {}
        self.delivery_tasks = {}
        self.delivery_unknown = set()
        self.publication_changed = asyncio.Event()
        self.publication_closed = False
        self.admission = runtime.admission()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.request_done.set()
        self.retire()

    def current(self):
        return (
            self.active and self.lease.current() and self.runtime.admission_current(self.admission)
        )

    async def admit(self, text):
        if self.admitted or not self.current():
            raise ValueError("TTS captured admission changed")
        self.admitted = True
        payload = TTSSynthesisAdmission(
            operation_id=self.operation_id,
            source_event_id=self.event.event_id,
            generation_epoch=self.generation,
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            tts_model=self.lease.pin,
            content_scope=self.event.payload.get("content_scope", "local_only"),
        )
        expected = AppendRequest(
            session_id=self.event.session_id,
            thread_id=self.event.thread_id,
            epoch=self.event.epoch,
            kind="converse.synthesis",
            payload=payload.model_dump(mode="json"),
            causes=[self.event.event_id],
            idempotency_key=f"tts-admission:{self.operation_id}",
        )

        async def publish():
            observed = await self.runtime.journal.append(expected)
            entry = JournalEntry.model_validate(observed.model_dump(), strict=True)
            if entry.origin != "converse" or _encode(
                entry.model_dump(
                    mode="json", exclude={"event_id", "sequence", "origin", "created_at"}
                )
            ) != _encode(expected.model_dump(mode="json")):
                raise RuntimeError("TTS admission publication is unconfirmed")
            return entry

        task = asyncio.create_task(publish())
        self.admission_publication = task
        self.publications.add(task)
        await asyncio.shield(task)
        if not self.current():
            raise ValueError("TTS source changed before synthesis")
        if not self.exact_openrouter:
            self.begin_native()

    @property
    def exact_openrouter(self):
        from packages.providers.openrouter_speech import OpenRouterTTSClient

        return isinstance(self.lease.target, OpenRouterTTSClient)

    def begin_native(self):
        if self.started or not self.current():
            raise ValueError("TTS actual dispatch is stale or already admitted")
        self.lease.owner.begin_native(self.lease)
        self.started = True

    def scoped_synthesis(self, text, *, content_scope, current):
        return self.lease.target.synthesize_scoped(
            text, content_scope=content_scope, current=current, before_export=self.begin_native
        )

    def frame(self, frame):
        if type(frame.get("audio")) is str and frame["audio"]:
            self.lease.audio_exposed = True
        return {
            **frame,
            "tts_model": self.lease.pin.model_dump(mode="json"),
            "tts_operation_id": self.operation_id,
        }

    def acknowledge(self):
        self.lease.delivery_confirmed = True
        self.delivery_done.set()
        self.publication_changed.set()

    async def publish_terminal_delivery(self, request):
        """Record an old turn's terminal playback report without reviving it.

        A local generation fence can precede the stop journal acknowledgement.
        These owner reports settle only the captured playback obligation; they
        cannot admit more synthesis or report stale audio as currently played.
        Session still requires the original epoch when committing the receipt.
        """
        request = DeliveryRequest.model_validate(request.model_dump(), strict=True)

        def bound():
            return (
                request.status in {"interrupted", "delivery_unknown"}
                and request.event_id == self.event.event_id
                and request.generation_epoch == self.generation
                and request.chunk_id == self.event.payload.get("chunk_id")
                and self.lease.audio_exposed
                and self.runtime._tts_history.get((request.event_id, self.generation)) is self
                and self.event.payload.get("runtime_id") == self.runtime._runtime_id
                and _encode(self.event.model_dump(mode="json")) == self._source_identity
            )

        if not bound():
            raise ValueError("Terminal delivery does not bind the captured speech turn")
        source = await self.runtime.journal.entry(request.event_id)
        session = await self.runtime.journal.session(self.event.session_id)
        if (
            not bound()
            or _encode(source.model_dump(mode="json")) != self._source_identity
            or source.kind != "conversation.converse"
            or source.origin != "converse"
            or source.payload.get("generation_epoch") != self.generation
            or session.channel != "converse"
            or (session.session_id, session.thread_id, session.epoch)
            != (source.session_id, source.thread_id, source.epoch)
        ):
            raise ValueError("Terminal delivery source is obsolete or unavailable")
        return await self.publish_delivery(request)

    async def publish_delivery(self, request):
        request = DeliveryRequest.model_validate(request.model_dump(), strict=True)
        status = request.status
        body = request.model_dump(mode="json")
        previous = self.delivery_requests.get(status)
        if previous is not None and _encode(previous.payload) != _encode(
            {
                **body,
                "alignment": "conservative_estimate",
                "tts_model": self.lease.pin.model_dump(mode="json"),
                "tts_operation_id": self.operation_id,
            }
        ):
            raise ValueError("Delivery identity was reused with different content")
        prior_task = self.delivery_tasks.get(status)
        if prior_task is not None:
            if not prior_task.done():
                return await asyncio.shield(prior_task)
            self._delivery_finished(status, prior_task)
            if not prior_task.cancelled() and prior_task.exception() is None:
                return prior_task.result()
            if self.publication_closed or self.lease.settled:
                return await asyncio.shield(prior_task)
        elif (
            self.publication_closed
            or self.lease.settled
            or (self.cleanup is not None and self.cleanup.done())
        ):
            raise ValueError("The captured turn cannot admit a new delivery status")
        expected = previous or AppendRequest(
            session_id=self.event.session_id,
            thread_id=self.event.thread_id,
            epoch=self.event.epoch,
            kind="delivery.updated",
            payload={
                **body,
                "alignment": "conservative_estimate",
                "tts_model": self.lease.pin.model_dump(mode="json"),
                "tts_operation_id": self.operation_id,
            },
            causes=[self.event.event_id],
            idempotency_key=f"delivery:{self.event.event_id}:{self.operation_id}:{status}",
        )
        self.delivery_requests[status] = expected

        async def publish():
            observed = await self.runtime.journal.append(expected)
            entry = JournalEntry.model_validate(observed.model_dump(), strict=True)
            if entry.origin != "converse" or _encode(
                entry.model_dump(
                    mode="json", exclude={"event_id", "sequence", "origin", "created_at"}
                )
            ) != _encode(expected.model_dump(mode="json")):
                raise RuntimeError("TTS delivery publication is unconfirmed")
            if status in {"played", "interrupted", "delivery_unknown"}:
                self.acknowledge()
            return entry

        task = asyncio.create_task(publish())
        if prior_task is not None:
            # Its actual coroutine has settled and its uncertain outcome was
            # recorded above. Retry only this original frozen AppendRequest.
            self.publications.discard(prior_task)
        self.delivery_tasks[status] = task
        self.publications.add(task)
        task.add_done_callback(lambda completed: self._delivery_finished(status, completed))
        self.publication_changed.set()
        return await asyncio.shield(task)

    def _delivery_finished(self, status, task):
        if not task.done() or self.delivery_tasks.get(status) is not task:
            return
        was_unknown = status in self.delivery_unknown
        if task.cancelled() or task.exception() is not None:
            self.delivery_unknown.add(status)
        else:
            self.delivery_unknown.discard(status)
        # An exact receipt clears only its publication uncertainty. A native
        # worker/transport failure cannot be cleared by unrelated journal I/O.
        self.lease.publication_unknown = bool(self.delivery_unknown)
        if was_unknown != (status in self.delivery_unknown):
            self.publication_changed.set()

    def synthesis_finished(self, cleanup, completed):
        self.producer_cleanup = cleanup
        self.completion_check = completed

    def retire(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._retire())
            self.runtime._tts_finalizers.add(self.cleanup)
            self.cleanup.add_done_callback(self.runtime._tts_finalizers.discard)
            self.cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self.cleanup

    async def _retire(self):
        try:
            await self.request_done.wait()
            if self.producer_cleanup is not None:
                await asyncio.shield(self.producer_cleanup)
                self.producer_completed = self.completion_check() is True
            if self.publications:
                await asyncio.gather(*self.publications, return_exceptions=True)
                admitted = self.admission_publication
                if admitted is not None and (
                    admitted.cancelled() or admitted.exception() is not None
                ):
                    self.lease.mark_unknown()
            await asyncio.shield(self.lease.start_transport_close())
            if self.started and not self.producer_completed:
                self.lease.mark_unknown()
            while True:
                self.publication_changed.clear()
                for status, task in self.delivery_tasks.items():
                    self._delivery_finished(status, task)
                pending = [task for task in self.publications if not task.done()]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                    continue
                if self.lease.cleanup_failed or self.lease.native_cleanup_unknown:
                    break
                if self.lease.playback_pending or self.lease.publication_unknown:
                    await self.publication_changed.wait()
                    continue
                # No await separates this final admission fence from deciding
                # settlement. A late first status cannot reopen a retired turn.
                self.publication_closed = True
                break
            if not self.lease.cleanup_failed and not self.lease.cleanup_unknown:
                self.lease.owner.finish_native(self.lease)
            await asyncio.shield(self.lease.start_close())
        except BaseException:
            self.lease.mark_unknown()
            raise
        finally:
            self.active = False
            if self.lease.settled:
                self.runtime._tts_turns.discard(self)


def capture_tts_turn(runtime, event, generation, request):
    if not getattr(runtime, "_tts_options_configured", False):
        return None
    owner = runtime.tts_model_settings
    if owner is None or runtime._tts_options_unavailable or runtime._stt_stopping:
        raise RuntimeError("TTS model owner is unavailable")
    for key, prior in tuple(runtime._tts_history.items()):
        playback = runtime.playback.get(prior.event.session_id)
        if prior not in runtime._tts_turns and (
            playback is None or not playback.accepts(prior.generation)
        ):
            runtime._tts_history.pop(key)
    identity = (event.event_id, generation)
    if identity in runtime._tts_history or len(runtime._tts_history) >= 1024:
        raise ValueError("This speech attempt is already captured or its history is full")
    lease = owner.capture(event.payload.get("content_scope", "local_only"))
    try:
        turn = TTSTurn(runtime, lease, event, generation)
        preparations = request.scope.get("kinaiya.converse.model_preparations")
        if not isinstance(preparations, list):
            raise RuntimeError("TTS request ownership is unavailable")
        runtime._tts_turns.add(turn)
        runtime._tts_history[identity] = turn
        preparations.append(turn)
        return turn
    except BaseException:
        lease.start_close()
        raise
