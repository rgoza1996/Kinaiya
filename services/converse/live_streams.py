"""One logical microphone and STT lease across bounded native decoder segments."""

import asyncio

from fastapi import HTTPException

from packages.contracts.live_captures import LiveCaptureRegistration
from packages.contracts.live_streams import LiveStreamNextUtterance, LiveStreamScope
from packages.contracts.live_windows import LiveWindowPublicationProof
from packages.providers.whistle_stream_owner import CapturedWhistleStream
from services.converse.incremental_streams import IncrementalStreams
from services.converse.live_capture import LiveMicCapture
from services.converse.stt_operations import stt_operation


class LiveIncrementalStreams(IncrementalStreams):
    def __init__(self, runtime, speech, scope, *, dependency):
        self.scope = LiveStreamScope.model_validate(scope.model_dump(mode="json"), strict=True)
        self.initial_utterance_id = self.scope.utterance_id
        super().__init__(runtime, speech, self)
        self.dependency = dependency
        self.admission = runtime.admission()
        self.mic = self.native = self.capture_task = None
        self.capture_ready = asyncio.Event()
        self.capture_release = asyncio.Event()
        self.capture_fenced = False
        self.segment_index = -1
        self.segment_sample_offset = 0
        self._next_segment = None

    def current(self):
        return bool(
            not self.capture_fenced
            and self.runtime.enabled
            and self.runtime.admission_current(self.admission)
            and self.dependency.ready is True
            and getattr(self.runtime, "_stt_options_configured", False)
            and not self.runtime._stt_options_unavailable
            and self.runtime.stt_model_settings is not None
            and not self.runtime.stt_model_settings.closed
            and not self.runtime._stt_stopping
        )

    @property
    def busy_count(self):
        return int(self.capture_task is not None and not self.capture_task.done())

    def tasks(self):
        tasks = super().tasks()
        if self.capture_task is not None:
            tasks.add(self.capture_task)
        if self.mic is not None:
            tasks.update(self.mic.tasks)
        return tasks

    async def _capture_lifetime(self):
        try:
            backend = await self.runtime.journal.live_capture_backend()
            self.mic = LiveMicCapture(
                self.runtime.journal,
                LiveCaptureRegistration(
                    schema_version="2.0",
                    protocol="native_live_capture_v2",
                    backend=backend,
                    owner_instance=self.scope.owner_instance,
                    capture_id=self.scope.capture_id,
                    session_id=self.scope.session_id,
                    thread_id=self.scope.thread_id,
                    epoch=self.scope.epoch,
                ),
                current=self.current,
            )
            if not self.current():
                self.mic.fence("cancelled")
                raise HTTPException(409, "Original live capture is fenced")
            await self.mic.register()
            async with stt_operation(self.runtime, self.scope.content_scope) as native:
                self.native = native
                self.capture_ready.set()
                await self.capture_release.wait()
                # A logical fence never donates the old decoder's physical slot.
                await asyncio.gather(
                    *(asyncio.shield(state.lifetime) for state in self.states),
                    return_exceptions=True,
                )
                if native is not None:
                    native.transcription_confirmed = all(
                        state.release.is_set()
                        and (
                            state.pending is None
                            or state.pending.phase == "begin"
                            or state.pending.never_dispatched
                        )
                        for state in self.states
                    )
        finally:
            self.capture_ready.set()

    async def _lifetime(self, state):
        try:
            await self.capture_ready.wait()
            if self.capture_task.done():
                await self.capture_task
                raise HTTPException(409, "Original live capture did not open")
            state.native, state.admission = self.native, self.admission
            state.client = CapturedWhistleStream(
                self.speech if self.native is None else self.native.provider
            )
            state.ready.set()
            await state.release.wait()
            await state.client.drain()
        finally:
            state.ready.set()

    def _new_stream(self, body, original):
        state = super()._new_stream(body, original)
        state.live_segment, state.live_offset, state.live_previous = self._next_segment
        return state

    def _admit_native_stream(self, state):
        # The one conservative crash admission belongs to this entire mic lease.
        if state.native.lease.admission_id is None:
            super()._admit_native_stream(state)

    async def _verify_current(self, state):
        if not self.current():
            raise HTTPException(409, "Original live capture dependency or owner changed")
        await super()._verify_current(state)
        permission = await self.mic.inspect()
        if not permission.capture_open:
            raise HTTPException(409, "Original native microphone permission is closed")
        if not self.current():
            raise HTTPException(409, "Original live capture owner changed during permission lookup")

    async def next_utterance(self, boundary, request):
        boundary = LiveStreamNextUtterance.model_validate(boundary, strict=True)
        return await self.begin_segment(
            boundary.segment_index,
            boundary.native_scope(),
            request,
            original_body=boundary.model_dump(mode="json"),
            _boundary=boundary,
        )

    async def begin_segment(
        self, segment_index, body, request, *, original_body=None, _boundary=None
    ):
        if type(segment_index) is not int or not 0 <= segment_index <= 1000000:
            raise HTTPException(400, "Invalid original decoder segment index")
        next_scope = None
        if _boundary is not None:
            next_scope = LiveStreamScope.model_validate(
                _boundary.model_dump(include=set(LiveStreamScope.model_fields)), strict=True
            )
            if (
                next_scope.model_dump(exclude={"utterance_id"})
                != self.scope.model_dump(exclude={"utterance_id"})
                or _boundary.previous_utterance_id != self.scope.utterance_id
                or next_scope.utterance_id == self.initial_utterance_id
            ):
                raise HTTPException(409, "Original live utterance boundary scope changed")
        if body != (self.scope if next_scope is None else next_scope).native_scope():
            raise HTTPException(409, "Original live capture scope changed")
        if not self.current():
            self.fence()
            raise HTTPException(409, "Original live capture is unavailable")
        previous = self.states[-1] if self.states else None
        if segment_index != self.segment_index + 1:
            raise HTTPException(409, "Original decoder segment is not the next contiguous segment")
        if _boundary is not None:
            known = self.originals.get(("finish", _boundary.previous_finish_sha256))
            if (
                previous is None
                or known is None
                or known[0] is not previous
                or known[1] is not previous.last
                or previous.last.phase != "finish"
                or previous.last.ack is None
            ):
                raise HTTPException(
                    409, "Utterance boundary requires its original positive final source"
                )
        if previous is not None:
            if (
                not previous.decoder_finished
                or previous.pending is not None
                or previous.last is None
                or previous.last.capture_ended
                or not previous.lifetime.done()
                or previous.lifetime.cancelled()
                or previous.lifetime.exception() is not None
                or previous.client.cleanup_failed
            ):
                raise HTTPException(409, "The previous decoder or publication remains unsettled")
            self.segment_sample_offset += previous.sample_offset
        if next_scope is not None:
            self.scope = next_scope
        self.segment_index = segment_index
        self._next_segment = (
            segment_index,
            self.segment_sample_offset,
            None
            if previous is None or next_scope is not None
            else previous.last.window.window_index,
        )
        if self.capture_task is None:
            self.runtime._incremental_stream_owners.add(self)
            self.capture_task = self._retain(asyncio.create_task(self._capture_lifetime()))
        original_body = original_body or {
            **self.scope.model_dump(mode="json"),
            "segment_index": segment_index,
        }
        return await super().begin(body, request, original_body=original_body)

    def _window_cursor(self, state, original, result, hypothesis):
        if state.last is None and state.live_previous is not None:
            return state.live_previous + 1, 1
        return super()._window_cursor(state, original, result, hypothesis)

    def _window_times(self, state, expected_samples):
        start = round(state.live_offset / 16)
        return start, start + min(60000, round(expected_samples / 16))

    async def _before_publication(self, state):
        try:
            await self._verify(state)
        except HTTPException:
            # Recognized input already exists. Exact native fencing permits
            # retention without reviving this capture's inference permission.
            await self.mic.wait_fenced()

    async def _publish_window(self, state, original):
        proof = await self.mic.publish(original.expected)
        original.live_publication = proof
        return proof.source

    async def _lookup_window(self, state, original):
        observed = await self.mic.inspect_publication(original.expected)
        if observed.receipt is not None:
            original.live_publication = observed.receipt
            return observed.receipt.source
        return None

    def _accept_entry(self, state, original, observed, *, settle=True):
        pair = LiveWindowPublicationProof.model_validate(
            original.live_publication.model_dump(mode="python"), strict=True
        )
        if (
            pair.source != observed
            or pair.original != self.mic._publication(original.expected).reference()
        ):
            raise ValueError("Live stream requires its exact original paired publication")
        super()._accept_entry(state, original, observed, settle=settle)

    def fence_state(self, state, *, retry=False):
        self.fence_capture()
        super().fence_state(state, retry=retry)

    def fence_capture(self, reason="cancelled"):
        self.capture_fenced = True
        if self.mic is not None:
            self.mic.fence(reason)
        self.capture_release.set()

    def fence(self):
        self.fence_capture()
        super().fence()

    async def close(self, reason="cancelled"):
        self.fence_capture(reason)
        super().fence()
        tasks = self.tasks()
        if tasks:
            await asyncio.wait(tasks, timeout=2)
        return bool(
            self.mic is not None
            and self.mic.fenced_state is not None
            and self.capture_task is not None
            and self.capture_task.done()
            and not self.capture_task.cancelled()
            and self.capture_task.exception() is None
            and not any(not task.done() for task in self.tasks())
            and (self.native is None or self.native.lease.settled)
        )
