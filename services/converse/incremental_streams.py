"""Captured stream, ordered publication and exact original recovery (#654)."""

import asyncio
import base64
import hashlib
import secrets
from dataclasses import dataclass, field

from anyio import CancelScope
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.models import AppendRequest, JournalEntry, SpeechWindow
from packages.contracts.stt_settlement import digest, encode
from packages.contracts.whistle_stream_owner import (
    PUBLICATION_PROTOCOL,
    ConverseStreamCapability,
    ConverseStreamLookup,
    ConverseStreamProof,
    StreamPublication,
    StreamPublicationProof,
    pcm_request_digest,
)
from packages.providers.whistle_assets import PROFILE
from packages.providers.whistle_ipc import decode_json
from packages.providers.whistle_stream_owner import CapturedWhistleStream
from services.converse.stt_operations import stt_operation

BASE = "/v1/converse/audio/stream"
MAX_ACK_BYTES = 272 * 1024


@dataclass(eq=False)
class _Original:
    phase: str
    request_sha256: str
    sequence: int = 0
    sample_offset: int = 0
    samples: int = 0
    user_speaking: bool = False
    capture_ended: bool = False
    audio_sha256: str | None = None
    native_original: object = None
    native_dispatched: bool = False
    never_dispatched: bool = False
    result: str | None = None
    window: SpeechWindow | None = None
    expected: AppendRequest | None = None
    entry: JournalEntry | None = None
    ack: str | None = None
    task: asyncio.Task | None = None
    recovery: asyncio.Task | None = None
    recovery_action: str | None = None
    publication: asyncio.Task | None = None


@dataclass(eq=False)
class _Stream:
    scope: dict
    begin: _Original
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    lifetime: asyncio.Task | None = None
    client: object = None
    native: object = None
    admission: object = None
    stream_id: str | None = None
    worker_incarnation: str | None = None
    sequence: int = 0
    sample_offset: int = 0
    recognizer_updates: int = 0
    window_index: int | None = None
    revision: int = 0
    last_text: str | None = None
    last_speaking: bool | None = None
    last_audio_sha256: str | None = None
    last: _Original | None = None
    pending: _Original | None = None
    cleanup: asyncio.Task | None = None
    settlement: asyncio.Task | None = None
    fenced: bool = False
    decoder_finished: bool = False
    process_released: bool = False
    never_dispatched: bool = False


class IncrementalStreams:
    active_limit = 32
    history_limit = 64

    def __init__(self, runtime, speech, controller):
        self.runtime, self.speech, self.controller = runtime, speech, controller
        self.instance = secrets.token_hex(16)
        self.states = []
        self.streams = {}
        self.originals = {}

    @property
    def busy_count(self):
        return sum(
            state.lifetime is not None
            and (
                not state.lifetime.done()
                or (state.client is not None and state.client.cleanup_failed)
            )
            for state in self.states
        )

    def tasks(self):
        tasks = set()
        for state in self.states:
            tasks.update(
                task
                for task in (state.lifetime, state.cleanup, state.settlement)
                if task is not None
            )
            for original in (state.begin, state.last, state.pending):
                if original is not None:
                    tasks.update(
                        task
                        for task in (original.task, original.recovery, original.publication)
                        if task is not None
                    )
        return tasks

    @staticmethod
    def _retain(task):
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return task

    def _work_finished(self, state, original, task):
        if not original.native_dispatched and original.native_original is None:
            # The sole effect-producing task has exited before dispatch. Its
            # separately retained handshakes carry metadata only; they cannot
            # send audio or revive this task. This is a positive local fence,
            # unlike inferring no effect from a missing remote receipt.
            original.never_dispatched = True
            if original.phase == "begin":
                state.never_dispatched = True
            if state.fenced:
                self._maybe_release(state)

    async def _verify(self, state):
        try:
            await self._verify_current(state)
        except Exception:
            self.fence_state(state)
            raise

    async def _verify_current(self, state):
        if state.fenced or not self.runtime.admission_current(state.admission):
            raise HTTPException(409, "Original stream availability changed")
        if state.native is not None and not await state.native.current():
            raise HTTPException(409, "Original stream STT selection changed")
        if not state.client.selected():
            raise HTTPException(409, "Original stream endpoint changed")
        try:
            async with asyncio.timeout(5):
                session = await self.runtime.journal.session(state.scope["session_id"])
        except (KeyError, ValueError):
            raise HTTPException(409, "Original stream session is unavailable") from None
        if (session.thread_id, session.epoch) != (state.scope["thread_id"], state.scope["epoch"]):
            raise HTTPException(409, "Original stream session or epoch changed")
        if state.fenced or not self.runtime.admission_current(state.admission):
            raise HTTPException(409, "Original stream availability changed")

    async def _lifetime(self, state):
        try:
            async with stt_operation(self.runtime, state.scope["content_scope"]) as native:
                state.native = native
                state.client = CapturedWhistleStream(
                    self.speech if native is None else native.provider
                )
                state.admission = self.runtime.admission()
                state.ready.set()
                await state.release.wait()
                await state.client.drain()
        finally:
            state.ready.set()

    def _trim(self):
        terminal = [
            state
            for state in self.states
            if state.lifetime is not None
            and state.lifetime.done()
            and (state.client is None or not state.client.cleanup_failed)
            and (
                state.never_dispatched
                or (
                    state.begin.ack is not None
                    and (state.last is None or state.last.ack is not None)
                )
            )
        ]
        for state in terminal[: max(0, len(terminal) - self.history_limit)]:
            self.states.remove(state)
            if state.stream_id is not None:
                self.streams.pop(state.stream_id, None)
            for original in (state.begin, state.last, state.pending):
                if original is not None:
                    self.originals.pop((original.phase, original.request_sha256), None)

    async def _wait(self, state, original, request):
        async def disconnected():
            while (await request.receive())["type"] != "http.disconnect":
                pass

        watcher = asyncio.create_task(disconnected())
        try:
            await asyncio.wait((original.task, watcher), return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(0)
            if watcher.done():
                self.fence_state(state)
                raise HTTPException(499, "Original stream request disconnected")
            return await asyncio.shield(original.task)
        except asyncio.CancelledError:
            self.fence_state(state)
            raise
        except HTTPException:
            if original.phase == "begin" and not original.native_dispatched:
                self.fence_state(state)
            raise
        except Exception:
            if original.phase == "begin" and not original.native_dispatched:
                self.fence_state(state)
                if state.client is None:
                    raise HTTPException(
                        503, "The selected STT adapter does not support owned streams"
                    ) from None
            # Keep the original exception with its owned task in memory.
            # Logging its native validation inputs could expose a transcript.
            raise RuntimeError("The original stream result remains unconfirmed") from None
        finally:
            watcher.cancel()
            with CancelScope(shield=True):
                await asyncio.wait((watcher,), timeout=0.2)

    def _new_stream(self, body, original):
        return _Stream(dict(body), original)

    async def _publish_window(self, state, original):
        return await self.controller.ingest(original.window)

    async def _lookup_window(self, state, original):
        descriptor = self._publication(original)
        observed = await self.runtime.journal.lookup_stream_publication(descriptor.model_dump())
        proof = StreamPublicationProof.model_validate(observed)
        if proof.original != descriptor:
            raise ValueError("Original stream publication lookup changed its source")
        return proof.entry

    async def _before_publication(self, state):
        await self._verify(state)

    def _window_cursor(self, state, original, result, hypothesis):
        index, revision = state.window_index, state.revision + 1
        if index is None:
            return 0, 1
        if (
            original.user_speaking != state.last_speaking
            or original.capture_ended
            or (bool(result["delta_text"].strip()) and hypothesis != state.last_text)
        ):
            return index + 1, 1
        return index, revision

    def _window_times(self, state, expected_samples):
        return 0, min(60000, round(expected_samples / 16))

    def _admit_native_stream(self, state):
        state.native.lease.owner.begin_native(state.native.lease)

    async def begin(self, body, request, *, original_body=None):
        if (
            self.controller is None
            or self.speech is None
            or getattr(self.runtime, "_stt_stopping", False)
        ):
            raise HTTPException(503, "Configure an available owned Whistle stream adapter")
        if not self.runtime.enabled:
            raise HTTPException(409, "Converse is unavailable")
        self._trim()
        sha = digest(body if original_body is None else original_body)
        if ("begin", sha) in self.originals:
            raise HTTPException(
                409, "This original begin already exists; inspect its exact outcome"
            )
        if (
            self.busy_count >= self.active_limit
            or len(self.states) >= self.active_limit + self.history_limit
        ):
            raise HTTPException(409, "Original stream work or recovery is still retained")
        original = _Original("begin", sha)
        state = self._new_stream(body, original)
        self.states.append(state)
        self.originals[("begin", sha)] = state, original
        self.runtime._incremental_stream_owners.add(self)
        state.lifetime = self._retain(asyncio.create_task(self._lifetime(state)))
        state.pending = original

        async def start():
            await state.ready.wait()
            if state.lifetime.done():
                await state.lifetime
                raise RuntimeError("The original stream model lease did not open")
            if state.native is not None:
                state.native.dependencies.add(asyncio.current_task())
            await self._verify(state)
            original.native_original = await state.client.reserve("begin", digest(body))
            await self._verify(state)
            if state.native is not None and state.native.lease.uncertain_native_worker:
                # A stream is not a unary STT publication. A crash preserves a
                # conservative NULL-binding admission; never overwrite v1's
                # one-publication descriptor with successive PCM receipts.
                self._admit_native_stream(state)
            original.native_dispatched = True
            result = await state.client.dispatch(original.native_original, body=body)
            self._accept_result(state, original, result)
            await self._verify(state)
            return decode_json(original.ack.encode("utf-8"), MAX_ACK_BYTES)

        original.task = self._retain(asyncio.create_task(start()))
        original.task.add_done_callback(lambda done: self._work_finished(state, original, done))
        return await self._wait(state, original, request)

    def _scope(self, body):
        state = self.streams.get(body["stream_id"])
        if state is None:
            raise HTTPException(409, "Unknown original Whistle stream")
        if body["worker_incarnation"] != state.worker_incarnation or any(
            body[key] != value for key, value in state.scope.items()
        ):
            raise HTTPException(409, "Original Whistle stream scope mismatch")
        return state

    async def _ack_previous(self, state):
        previous = state.last or state.begin
        if previous.ack is None or previous.result is None or previous.native_original is None:
            raise HTTPException(409, "The previous original is not confirmed")
        proof = await state.client.inspect(
            previous.native_original,
            operation="acknowledge",
            result_sha256=digest(decode_json(previous.result.encode("utf-8"), 128 * 1024)),
        )
        if proof.state != "acknowledged":
            raise RuntimeError("The previous broker receipt remains unconfirmed")
        if state.native is not None:
            if previous.task is not None and previous.task.done():
                state.native.dependencies.discard(previous.task)
            if previous.publication is not None and previous.entry is not None:
                state.native.publications.discard(previous.publication)
                state.native.dependencies.discard(previous.publication)

    async def append_or_finish(self, phase, body, request, *, original_body=None):
        state = self._scope(body)
        sha = digest(body if original_body is None else original_body)
        known = self.originals.get((phase, sha))
        if known is not None:
            _, original = known
            if original.ack is not None:
                return {**decode_json(original.ack.encode("utf-8"), MAX_ACK_BYTES), "replay": True}
            raise HTTPException(409, "The exact original is unresolved; inspect without replay")
        if state.pending is not None or state.decoder_finished or state.fenced:
            raise HTTPException(409, "Original stream progress or cleanup is unconfirmed")
        pcm16, pcm_sha256, samples = None, state.last_audio_sha256, 0
        if phase == "pcm":
            if body["sequence"] >= 100000:
                raise HTTPException(413, "Original PCM must reserve a sequence for decoder finish")
            try:
                pcm16 = base64.b64decode(body["pcm16_base64"], validate=True)
            except ValueError:
                raise HTTPException(400, "Invalid original PCM16 payload") from None
            if not 2 <= len(pcm16) <= 32000 or len(pcm16) % 2:
                raise HTTPException(400, "Original PCM requires1..16000 contiguous samples")
            pcm_sha256, samples = hashlib.sha256(pcm16).hexdigest(), len(pcm16) // 2
        if body["sequence"] != state.sequence + 1 or body["sample_offset"] != state.sample_offset:
            raise HTTPException(409, "Original stream sequence or sample offset is not contiguous")
        if state.sample_offset + samples > 960000:
            raise HTTPException(413, "Original stream exceeds960000 samples")
        original = _Original(
            phase,
            sha,
            body["sequence"],
            body["sample_offset"],
            samples,
            body["user_speaking"],
            body.get("capture_ended", False),
            pcm_sha256,
        )
        state.pending = original
        self.originals[(phase, sha)] = state, original

        async def advance():
            if state.native is not None:
                state.native.dependencies.add(asyncio.current_task())
            await self._ack_previous(state)
            await self._verify(state)
            if phase == "pcm":
                headers = {
                    "Content-Type": "application/octet-stream",
                    "x-kinaiya-stream-id": state.stream_id,
                    "x-kinaiya-worker-incarnation": state.worker_incarnation,
                    "x-kinaiya-session-id": state.scope["session_id"],
                    "x-kinaiya-thread-id": state.scope["thread_id"],
                    "x-kinaiya-epoch": str(state.scope["epoch"]),
                    "x-kinaiya-utterance-id": state.scope["utterance_id"],
                    "x-kinaiya-content-scope": state.scope["content_scope"],
                    "x-kinaiya-sequence": str(original.sequence),
                    "x-kinaiya-sample-offset": str(original.sample_offset),
                    "x-kinaiya-pcm-sha256": pcm_sha256,
                }
                upstream = pcm_request_digest(headers, pcm16)
                forward = None
            else:
                headers = None
                forward = {
                    **state.scope,
                    "stream_id": state.stream_id,
                    "worker_incarnation": state.worker_incarnation,
                    "sequence": original.sequence,
                    "sample_offset": original.sample_offset,
                }
                upstream = digest(forward)
            original.native_original = await state.client.reserve(phase, upstream)
            await self._verify(state)
            original.native_dispatched = True
            result = await state.client.dispatch(
                original.native_original, body=forward, pcm16=pcm16, headers=headers
            )
            self._accept_result(state, original, result)
            await self._before_publication(state)
            original.publication = self._retain(
                asyncio.create_task(self._publish_window(state, original))
            )
            if state.native is not None:
                state.native.dependencies.add(original.publication)
                state.native.publications.add(original.publication)
            entry = await asyncio.shield(original.publication)
            self._accept_entry(state, original, entry)
            return decode_json(original.ack.encode("utf-8"), MAX_ACK_BYTES)

        original.task = self._retain(asyncio.create_task(advance()))
        original.task.add_done_callback(lambda done: self._work_finished(state, original, done))
        return await self._wait(state, original, request)

    def _accept_result(self, state, original, result):
        # Validate transport identity and native bounds before using a result.
        if (
            type(result) is not dict
            or result.get("provider") != "whistle"
            or result.get("profile") != PROFILE
        ):
            raise ValueError("Invalid original Whistle stream result")
        if original.phase == "begin":
            from packages.providers.whistle_ipc import token

            if (
                not token(result.get("stream_id"))
                or not token(result.get("worker_incarnation"))
                or any(result.get(key) != value for key, value in state.scope.items())
                or type(result.get("sequence")) is not int
                or result["sequence"] != 0
                or type(result.get("sample_offset")) is not int
                or result["sample_offset"] != 0
            ):
                raise ValueError("Original Whistle startup result changed its source")
            state.stream_id, state.worker_incarnation = (
                result["stream_id"],
                result["worker_incarnation"],
            )
            self.streams[state.stream_id] = state
            original.result = encode(result)
            original.ack = encode(result)
            state.pending = None
            return
        from packages.providers.whistle_stream import validate_stream_update

        expected_samples = original.sample_offset + original.samples
        fields = {
            key: result.get(key)
            for key in (
                "delta_text",
                "committed_text",
                "pending_text",
                "received_samples",
                "final",
                "native_pass_ms",
                "language",
            )
        }
        validate_stream_update(
            fields, expected_samples=expected_samples, expect_final=original.phase == "finish"
        )
        if (
            result.get("stream_id") != state.stream_id
            or result.get("worker_incarnation") != state.worker_incarnation
            or type(result.get("sequence")) is not int
            or result["sequence"] != original.sequence
            or type(result.get("sample_offset")) is not int
            or result["sample_offset"] != original.sample_offset
            or (original.phase == "pcm" and result.get("pcm_sha256") != original.audio_sha256)
        ):
            raise ValueError("Original Whistle result changed its stream or PCM cursor")
        original.result = encode(result)
        if original.window is None:
            committed, pending = result["committed_text"].strip(), result["pending_text"].strip()
            hypothesis = f"{committed} {pending}" if committed and pending else committed or pending
            index, revision = self._window_cursor(state, original, result, hypothesis)
            start_ms, end_ms = self._window_times(state, expected_samples)
            original.window = SpeechWindow(
                **state.scope,
                window_index=index,
                revision=revision,
                text=hypothesis,
                start_ms=start_ms,
                end_ms=end_ms,
                user_speaking=False if original.capture_ended else original.user_speaking,
                capture_ended=original.capture_ended,
                audio_sha256=original.audio_sha256,
                stt_model=None if state.native is None else state.native.lease.pin,
            )
            original.expected = AppendRequest(
                session_id=state.scope["session_id"],
                thread_id=state.scope["thread_id"],
                epoch=state.scope["epoch"],
                kind="conversation.window",
                payload=original.window.model_dump(mode="json"),
                idempotency_key=f"speech-window:{original.window.epoch}:{original.window.utterance_id}:{index}:r{revision}",
            )
            if len(encode(original.expected.model_dump(mode="json")).encode("utf-8")) > 65536:
                raise ValueError("Original stream publication exceeds the native journal bound")
        if original.phase == "finish":
            state.decoder_finished = True

    @staticmethod
    def _publication(original):
        expected = original.expected
        return StreamPublication(
            schema_version="1.0",
            protocol=PUBLICATION_PROTOCOL,
            session_id=expected.session_id,
            thread_id=expected.thread_id,
            epoch=expected.epoch,
            idempotency_key=expected.idempotency_key,
            request_sha256=digest(expected.model_dump(mode="json")),
        )

    def _accept_entry(self, state, original, observed, *, settle=True):
        entry = JournalEntry.model_validate(observed.model_dump(), strict=True)
        proof = StreamPublicationProof(
            schema_version="1.0",
            protocol=PUBLICATION_PROTOCOL,
            original=self._publication(original),
            entry=entry,
        )
        original.entry = proof.entry
        result, window = decode_json(original.result.encode("utf-8"), 128 * 1024), original.window
        ack = {
            "stream_id": state.stream_id,
            "worker_incarnation": state.worker_incarnation,
            "pcm_sequence": original.sequence,
            "recognizer_updates": state.recognizer_updates + 1,
            "window_index": window.window_index,
            "window_revision": window.revision,
            "journal_sequence": entry.sequence,
            "received_samples": result["received_samples"],
            "delta_text": result["delta_text"],
            "committed_text": result["committed_text"],
            "pending_text": result["pending_text"],
            "final": original.phase == "finish",
            "capture_ended": original.capture_ended,
            "entry": entry.model_dump(mode="json"),
        }
        encoded = encode(ack)
        if len(encoded.encode("utf-8")) > MAX_ACK_BYTES:
            raise ValueError("Original stream acknowledgment exceeds its bound")
        previous = state.last
        if previous is not None:
            self.originals.pop((previous.phase, previous.request_sha256), None)
        original.ack, state.last, state.pending = encoded, original, None
        state.sequence, state.sample_offset = original.sequence, result["received_samples"]
        state.recognizer_updates += 1
        state.window_index, state.revision = window.window_index, window.revision
        state.last_text, state.last_speaking = window.text, window.user_speaking
        state.last_audio_sha256 = original.audio_sha256
        if state.native is not None and original.publication is not None:
            # The failed original stays owned until exact positive journal
            # proof is available. Keep its diagnostic task in this original,
            # while releasing the now-settled publication classification.
            state.native.publications.discard(original.publication)
        if settle:
            self._maybe_release(state)

    def _maybe_release(self, state):
        publication_settled = (
            state.pending is None
            or state.pending.phase == "begin"
            or state.pending.never_dispatched
        )
        if (
            (state.decoder_finished or state.process_released or state.never_dispatched)
            and publication_settled
            and state.settlement is None
        ):

            async def settle():
                if not state.never_dispatched and (
                    state.last is not None or state.begin.result is not None
                ):
                    await self._ack_previous(state)
                if state.native is not None:
                    state.native.transcription_confirmed = True
                state.release.set()

            state.settlement = self._retain(asyncio.create_task(settle()))

    def fence_state(self, state, *, retry=False):
        state.fenced = True
        if state.cleanup is None or (
            retry
            and state.cleanup.done()
            and not state.process_released
            and not state.never_dispatched
        ):
            state.cleanup = self._retain(asyncio.create_task(self._cancel(state)))

    def fence(self):
        for state in self.states:
            if state.lifetime is not None and not state.lifetime.done():
                self.fence_state(state)

    async def _cancel(self, state):
        original = state.begin
        if original.native_original is None and original.task is not None:
            await asyncio.gather(asyncio.shield(original.task), return_exceptions=True)
        pending = state.pending
        if pending is not None and pending is not original and pending.native_original is not None:
            # Fence even a late body that has not yet reached its reserved
            # broker entry. A missing response alone is never a no-effect proof.
            proof = await state.client.inspect(pending.native_original, operation="abort")
            if proof.state == "not_dispatched":
                pending.never_dispatched = True
        if original.native_original is not None:
            proof = await state.client.inspect(original.native_original, operation="abort")
            if original.result is None and proof.state == "completed":
                self._accept_result(state, original, proof.result)
            state.process_released = proof.process_released
            if proof.state == "not_dispatched":
                original.never_dispatched = True
                state.never_dispatched = True
        elif not original.native_dispatched:
            state.never_dispatched = True
        self._maybe_release(state)

    async def cancel(self, body):
        state = self.streams.get(body["stream_id"])
        if state is None or (
            body["worker_incarnation"] is not None
            and body["worker_incarnation"] != state.worker_incarnation
        ):
            raise HTTPException(409, "No exact owned stream cancellation identity is available")
        self.fence_state(state, retry=True)
        await asyncio.wait((state.cleanup,), timeout=1)
        return {
            "cancelled": True,
            "cleanup_confirmed": state.process_released or state.never_dispatched,
            "publication_confirmed": state.pending is None,
        }

    async def lookup(self, phase, request, *, reconcile=False):
        known = (
            self.originals.get((phase, request.request_sha256))
            if request.owner_instance == self.instance
            else None
        )
        if known is None:
            return self._proof(phase, request.request_sha256, None, None, "unknown")
        state, original = known
        if original.task is not None and not original.task.done():
            return self._proof(phase, request.request_sha256, state, original, "in_progress")

        async def recover():
            if original.result is None and original.native_original is not None:
                proof = await state.client.inspect(original.native_original)
                state.process_released = state.process_released or proof.process_released
                if proof.state == "completed":
                    self._accept_result(state, original, proof.result)
                elif proof.state == "not_dispatched":
                    original.never_dispatched = True
                    if original.phase == "begin":
                        state.never_dispatched = True
                    if reconcile:
                        self._maybe_release(state)
                    return self._proof(
                        phase, request.request_sha256, state, original, "not_dispatched"
                    )
            if original.ack is None and original.window is not None:
                entry = await self._lookup_window(state, original)
                if entry is not None:
                    self._accept_entry(state, original, entry, settle=False)
            if (
                state.fenced
                and not state.process_released
                and state.begin.native_original is not None
            ):
                proof = await state.client.inspect(state.begin.native_original)
                state.process_released = proof.process_released
            if reconcile and (
                state.decoder_finished
                and state.pending is None
                and state.settlement is not None
                and state.settlement.done()
                and not state.release.is_set()
            ):
                terminal = state.last
                proof = await state.client.inspect(terminal.native_original)
                if proof.state == "acknowledged" and proof.result_sha256 == digest(
                    decode_json(terminal.result.encode("utf-8"), 128 * 1024)
                ):
                    if state.native is not None:
                        state.native.transcription_confirmed = True
                    state.release.set()
                elif proof.state == "completed" and proof.result_sha256 == digest(
                    decode_json(terminal.result.encode("utf-8"), 128 * 1024)
                ):
                    state.settlement = None  # Explicit exact metadata ACK; never audio replay.
            if reconcile:
                self._maybe_release(state)
            outcome = (
                "confirmed"
                if original.ack is not None
                else "not_dispatched"
                if original.never_dispatched
                or (original.phase == "begin" and state.never_dispatched)
                else "publication_pending"
                if original.result is not None and original.phase != "begin"
                else "unknown"
            )
            return self._proof(phase, request.request_sha256, state, original, outcome)

        action = "reconcile" if reconcile else "lookup"
        if (
            original.recovery is not None
            and not original.recovery.done()
            and original.recovery_action != action
        ):
            raise HTTPException(409, "The exact original observation is still pending")
        if original.recovery is None or original.recovery.done():
            original.recovery_action = action
            original.recovery = self._retain(asyncio.create_task(recover()))
            if state.native is not None:
                state.native.dependencies.add(original.recovery)
                # Completed metadata observations are not an ever-growing
                # inference history. Actual HTTP/close tasks remain enrolled
                # in their captured adapter/journal owners until they finish.
                original.recovery.add_done_callback(state.native.dependencies.discard)
        return await asyncio.shield(original.recovery)

    def _proof(self, phase, sha, state, original, outcome):
        return ConverseStreamProof.model_validate(
            {
                "schema_version": "1.0",
                "protocol": "converse_stream_owner_v1",
                "owner_instance": self.instance,
                "phase": phase,
                "request_sha256": sha,
                "outcome": outcome,
                "result": None
                if original is None or original.ack is None
                else decode_json(original.ack.encode("utf-8"), MAX_ACK_BYTES),
                "native_original": None
                if original is None or original.native_original is None
                else original.native_original.model_dump(),
                "decoder_finished": state is not None and state.decoder_finished,
                "process_released": state is not None and state.process_released,
                "never_dispatched": state is not None and state.never_dispatched,
                "ownership_retired": bool(
                    state is not None
                    and state.lifetime is not None
                    and state.lifetime.done()
                    and not state.lifetime.cancelled()
                    and state.lifetime.exception() is None
                    and state.client is not None
                    and not state.client.cleanup_failed
                ),
            }
        ).model_dump(mode="json")


def install_incremental_streams(app, runtime, auth, speech, controller, reserve_api_model, models):
    owner = IncrementalStreams(runtime, speech, controller)
    authenticated = [Depends(auth.require("owner"))]

    async def parse(request, model, limit):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("content-type", "content-length", "content-encoding")
        ):
            raise HTTPException(400, "Invalid incremental request envelope")
        if request.headers.get("content-type", "").split(";")[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Incremental requests require identity JSON")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > limit:
                        raise HTTPException(413, "Incremental request exceeds its bound")
                    raw.extend(chunk)
            return model.model_validate(decode_json(bytes(raw), limit))
        except TimeoutError:
            raise HTTPException(408, "Incremental request body deadline expired") from None
        except (ValueError, RecursionError):
            raise HTTPException(400, "Invalid original incremental request") from None

    def respond(result):
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    def documented_body(model):
        # Manual authenticated/bounded acquisition must not remove the legacy
        # input contract from OpenAPI or re-enable framework body acquisition.
        return {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": model.model_json_schema()}},
            }
        }

    @app.get(BASE + "/ownership", dependencies=authenticated)
    async def capability():
        return respond(
            ConverseStreamCapability(
                schema_version="1.0",
                protocol="converse_stream_owner_v1",
                owner_instance=owner.instance,
            ).model_dump()
        )

    @app.post(
        BASE + "/begin", dependencies=authenticated, openapi_extra=documented_body(models["begin"])
    )
    async def begin(request: Request):
        body = await parse(request, models["begin"], 4096)
        reserve_api_model(runtime, request)
        return respond(await owner.begin(body.model_dump(mode="json"), request))

    @app.post(
        BASE + "/pcm", dependencies=authenticated, openapi_extra=documented_body(models["pcm"])
    )
    async def pcm(request: Request):
        body = await parse(request, models["pcm"], 96000)
        reserve_api_model(runtime, request)
        return respond(await owner.append_or_finish("pcm", body.model_dump(mode="json"), request))

    @app.post(
        BASE + "/finish",
        dependencies=authenticated,
        openapi_extra=documented_body(models["finish"]),
    )
    async def finish(request: Request):
        body = await parse(request, models["finish"], 4096)
        reserve_api_model(runtime, request)
        return respond(
            await owner.append_or_finish("finish", body.model_dump(mode="json"), request)
        )

    @app.post(
        BASE + "/cancel",
        dependencies=authenticated,
        openapi_extra=documented_body(models["cancel"]),
    )
    async def cancel(request: Request):
        body = await parse(request, models["cancel"], 4096)
        return respond(await owner.cancel(body.model_dump(mode="json")))

    @app.post(
        BASE + "/{phase}/{action}",
        dependencies=authenticated,
        openapi_extra=documented_body(ConverseStreamLookup),
    )
    async def lookup(phase: str, action: str, request: Request):
        if phase not in {"begin", "pcm", "finish"} or action not in {"lookup", "reconcile"}:
            raise HTTPException(404)
        body = await parse(request, ConverseStreamLookup, 4096)
        try:
            result = await owner.lookup(phase, body, reconcile=action == "reconcile")
        except (ValueError, RuntimeError, OSError, TimeoutError):
            raise HTTPException(503, "Exact original stream proof is unavailable") from None
        return respond(result)

    return owner
