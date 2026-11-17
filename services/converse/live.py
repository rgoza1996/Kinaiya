"""Native v2 Converse effects using the existing conversation/speech owner."""

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from packages.contracts.live_decisions_attempts import LiveDecisionsOutcome
from packages.contracts.live_effect_pending import LiveEffectPendingItem, LiveEffectPendingQuery
from packages.contracts.live_effects import (
    PROTOCOL,
    LiveEffectAdmit,
    LiveEffectFinish,
    LiveEffectRefused,
    LiveEffectStart,
    effect_key,
)
from packages.contracts.operation_ownership import ActualOperation, actual_owner, retain_task
from services.converse.windows import PreparedReply, WindowConverseController, WindowGenerationTask

_live_reply = ContextVar("kinaiya_live_converse_reply", default=None)


@dataclass
class _Route:
    item: LiveEffectPendingItem
    effect: str
    admission: LiveEffectAdmit
    task: object = None
    admission_task: object = None
    start_request: object = None
    start_task: object = None
    prepared: object = None
    append_tasks: dict = field(default_factory=dict)
    finish_request: object = None
    finish_task: object = None
    owners: list = field(default_factory=list)
    retired: bool = False


@dataclass
class LivePreparedReply(PreparedReply):
    route: object = None
    receipt: object = None
    output: list = field(default_factory=list)
    output_chars: int = 0
    generation_error: object = None
    observation_task: object = None


class LiveConverseController(WindowConverseController):
    """Explicit trusted embedding; construction starts no inference or capture."""

    def __init__(self, runtime):
        if runtime._window_text_ready is not None:
            raise ValueError("Converse already has its original window owner")
        super().__init__(runtime)
        runtime._live_speech_authority = self.validate_live_speech
        self.owner_instance = secrets.token_hex(16)
        self.routes = {}
        self.cursors = {"converse_reply": 0, "converse_backchannel": 0}
        self.closed = False
        self.running = False
        self.run_task = None
        self.run_owner = None
        self.native_wait = None
        self.local_wait = None
        self.changed = asyncio.Event()

    def _changed(self, task=None):
        self.changed.set()
        if task is not None and not task.cancelled():
            task.exception()

    @staticmethod
    def _consume(task):
        if not task.cancelled():
            task.exception()

    async def _native(self, operation, *args, **kwargs):
        prepared = _live_reply.get()
        if operation != "append" or prepared is None:
            return await super()._native(operation, *args, **kwargs)
        request = args[0]
        original = prepared.route.append_tasks.get(request.idempotency_key)
        if original is None:
            task = retain_task(asyncio.create_task(self.journal.append(request)))
            task.add_done_callback(self._changed)
            prepared.route.append_tasks[request.idempotency_key] = (request, task)
        else:
            saved, task = original
            if saved != request:
                raise ValueError("Live Converse metadata changed its original request")
        return await self._native_call(asyncio.shield, task)

    def _state_request(self, source, phase, **fields):
        prepared = _live_reply.get()
        if prepared is None:
            return super()._state_request(source, phase, **fields)
        if phase == "error":
            prepared.generation_error = fields.get("reason", "generation_error")
        request = super()._state_request(source, "live_" + phase, **fields)
        return request.model_copy(
            update={
                "idempotency_key": effect_key(prepared.receipt) + ":state:" + phase,
                "payload": request.payload
                | {
                    "live_effect": prepared.route.effect,
                    "live_effect_key": effect_key(prepared.receipt),
                },
            }
        )

    def _instructions(self, prepared, frame):
        instructions = super()._instructions(prepared, frame)
        if not isinstance(prepared, LivePreparedReply):
            return instructions
        intent = {
            "schema_version": "2.0",
            "conversation": prepared.behavior,
            "effect": prepared.route.effect,
            "source_event_id": prepared.source.event_id,
            "decision_event_id": prepared.decision.event_id,
            "user_speaking_at_decision": prepared.source.payload["user_speaking"],
            "capture_ended_at_decision": prepared.source.payload["capture_ended"],
        }
        return instructions + (
            "\nNative Converse intent v2\n"
            "These native facts describe the selected conversation action, not task success. "
            "WAIT_WITH_BACKCHANNEL permits an acknowledgment while listening continues; "
            "Converse chooses its wording or may choose no acknowledgment. "
            "This action is not evidence that the user finished speaking.\n"
            + json.dumps(intent, ensure_ascii=False, separators=(",", ":"))
        )

    async def _response_record(self, prepared, latest):
        if isinstance(prepared, LivePreparedReply):
            # V2 deduplicates the original source/action effect, not the entire
            # utterance. A backchannel must not consume its later full reply.
            return None
        return await super()._response_record(prepared, latest)

    def _bind_reply(self, prepared, request):
        if not isinstance(prepared, LivePreparedReply):
            return super()._bind_reply(prepared, request)
        return request.model_copy(
            update={
                "idempotency_key": effect_key(prepared.receipt) + ":" + str(prepared.published),
                "payload": request.payload
                | {
                    "live_effect": prepared.route.effect,
                    "live_effect_key": effect_key(prepared.receipt),
                },
            }
        )

    async def _source_current(self, prepared):
        if not isinstance(prepared, LivePreparedReply):
            return await super()._source_current(prepared)
        # Native v2 qualifies the whole captured history. A harmless speaking
        # hint may have a new raw event ID without replacing this producer.
        state = await self.journal.live_effect_lookup(
            prepared.route.item.query(prepared.route.effect)
        )
        record = state.record
        if (
            not state.source_current
            or record is None
            or record.receipt != prepared.receipt
            or record.started is None
            or record.started.original != prepared.route.start_request
            or record.completion is not None
            and record.completion.status in {"declined", "refused"}
        ):
            return False
        return True

    async def validate_live_speech(self, event):
        prepared = self.pending.get(event.session_id)
        epoch = self.runtime.playback.get(event.session_id)
        if (
            self.closed
            or not isinstance(prepared, LivePreparedReply)
            or epoch is None
            or epoch.chunks.get(event.event_id) != event
            or event.payload.get("window_source_id") != prepared.source.event_id
            or event.causes != [prepared.source.event_id]
            or event.payload.get("live_effect") != prepared.route.effect
            or event.payload.get("live_effect_key") != effect_key(prepared.receipt)
            or await self.current(prepared) is None
        ):
            raise ValueError("Original native live speech authority is unavailable")

    async def flush(self, prepared):
        if not isinstance(prepared, LivePreparedReply):
            return await super().flush(prepared)
        token = _live_reply.set(prepared)
        try:
            return await super().flush(prepared)
        finally:
            _live_reply.reset(token)

    async def generate(self, prepared):
        if not isinstance(prepared, LivePreparedReply):
            return await super().generate(prepared)
        token = _live_reply.set(prepared)
        try:
            await super().generate(prepared)
        finally:
            for task in (prepared.cleanup, prepared.model_cleanup):
                if task is not None:
                    retain_task(task, cleanup=True)
                    task.add_done_callback(self._changed)
            _live_reply.reset(token)

    @asynccontextmanager
    async def _owned_provider_stream(self, messages, prepared):
        # Keep the existing provider stream and sole-close owner. Capture raw
        # deltas independently from speech sentence boundaries below.
        async with super()._owned_provider_stream(messages, prepared) as stream:
            if not isinstance(prepared, LivePreparedReply):
                yield stream
                return

            async def original_deltas():
                async for delta in stream:
                    if type(delta) is not str or prepared.output_chars + len(delta) > 16000:
                        raise ValueError("Live Converse output exceeds its declared text bound")
                    prepared.output.append(delta)
                    prepared.output_chars += len(delta)
                    yield delta

            yield original_deltas()

    async def process_live(self, item, effect, *, timeout=2):
        item = LiveEffectPendingItem.model_validate(item, strict=True)
        if effect not in self.cursors:
            raise ValueError("Converse cannot own this native effect")
        if self.closed:
            return None
        key = (item.event_id, effect)
        route = self.routes.get(key)
        if route is None:
            if len(self.routes) >= 64:
                raise RuntimeError("Original live Converse effects retain their capacity")
            route = _Route(
                item,
                effect,
                LiveEffectAdmit(
                    **item.query(effect).model_dump(mode="json"),
                    owner_instance=self.owner_instance,
                    operation_id=secrets.token_hex(16),
                ),
            )
            self.routes[key] = route
        elif route.item != item:
            raise ValueError("Live Converse delivery changed its original publication")
        if route.task is None or route.task.done():
            route.owners[:] = [o for o in route.owners if not (o.finished and not o.failed)]
            owner = ActualOperation(lambda _: None)
            route.owners.append(owner)

            async def original():
                with actual_owner(owner), self.runtime.model_preparation():
                    return await self._advance(route)

            route.task = asyncio.create_task(original())
            owner.retain_task(route.task)
            route.task.add_done_callback(self._consume)
        try:
            return await asyncio.wait_for(asyncio.shield(route.task), timeout)
        except TimeoutError:
            return None

    async def _lookup(self, route):
        return (await self.journal.live_effect_lookup(route.item.query(route.effect))).record

    async def _advance(self, route):
        state = await self.journal.lookup_live_decision(route.item.publication)
        entry = state.receipt
        if entry is None or (entry.event_id, entry.sequence) != (
            route.item.event_id,
            route.item.sequence,
        ):
            raise ValueError("Live Converse effect has no original native publication")
        outcome = LiveDecisionsOutcome.model_validate(entry.payload, strict=True)
        record = await self._lookup(route)
        if record is None:
            if route.admission_task is None:
                route.admission_task = retain_task(
                    asyncio.create_task(self.journal.live_effect_admit(route.admission))
                )
            try:
                record = await asyncio.shield(route.admission_task)
            except LiveEffectRefused:
                route.retired = True
                return None
            except Exception:
                record = await self._lookup(route)
                if record is None:
                    return None
        if record.completion is not None and record.completion.status != "uncertain":
            route.retired = True
            return record
        if record.started is None:
            if self.closed or not self.runtime.enabled:
                return record
            old = self.pending.get(entry.session_id)
            if old is not None:
                await self.flush(old)
                if not self._reclaimable(old):
                    return record
            if not self.runtime.typed_provider_ready(entry.session_id):
                return record
            if entry.session_id not in self.pending and len(self.pending) >= 32:
                return record
            if route.start_request is None:
                route.start_request = LiveEffectStart(
                    schema_version="2.0",
                    protocol=PROTOCOL,
                    original=record.receipt,
                    owner_instance=self.owner_instance,
                    operation_id=secrets.token_hex(16),
                )
            if route.start_task is None:
                route.start_task = retain_task(
                    asyncio.create_task(self.journal.live_effect_start(route.start_request))
                )
            try:
                record = await asyncio.shield(route.start_task)
            except LiveEffectRefused:
                route.start_task = None
                return await self._lookup(route)
            except Exception:
                record = await self._lookup(route)
                if record is None or record.started is None:
                    return record
            if record.started is not None and record.started.original == route.start_request:
                source = await self.journal.entry(outcome.source.original.source_event_id)
                outcome.source.check_entry(source)
                await self.yield_speech(source.session_id)
                epoch = await self.runtime._epoch(source.session_id)
                prepared = LivePreparedReply(
                    source,
                    entry,
                    outcome.decision.conversation,
                    epoch.generation,
                    admission=self.runtime.admission(),
                    route=route,
                    receipt=record.receipt,
                )
                route.prepared = prepared
                self.pending[source.session_id] = prepared
                prepared.task = WindowGenerationTask(self.generate(prepared))
                retain_task(prepared.task)
                prepared.task.add_done_callback(self._changed)
        prepared = route.prepared
        if prepared is None:
            evidence = await self.journal.live_effect_evidence(record.receipt)
            if evidence.evidence is not None:
                return await self._finish(
                    route, record, "recorded", "native_converse_chunk_recorded", evidence.evidence
                )
            return await self._finish(
                route, record, "uncertain", "original_converse_producer_unconfirmed"
            )
        if prepared.task.done():
            if prepared.observation_task is None:
                prepared.observation_task = retain_task(
                    asyncio.create_task(self._observe(prepared))
                )
                prepared.observation_task.add_done_callback(self._changed)
            try:
                await asyncio.shield(prepared.observation_task)
            except Exception:
                return record
            evidence = await self.journal.live_effect_evidence(record.receipt)
            if evidence.evidence is not None:
                return await self._finish(
                    route, record, "recorded", "native_converse_chunk_recorded", evidence.evidence
                )
        if prepared.task.done() and self._reclaimable(prepared):
            successful_metadata = all(
                t.done() and not t.cancelled() and t.exception() is None
                for _, t in route.append_tasks.values()
            )
            if successful_metadata:
                return await self._finish(
                    route, record, "declined", "original_converse_returned_no_chunks"
                )
            return await self._finish(
                route, record, "uncertain", "original_converse_publication_unconfirmed"
            )
        return record

    async def _observe(self, prepared):
        token = _live_reply.set(prepared)
        try:
            text = (
                prepared.native_audio_reply.text
                if prepared.native_audio_reply is not None
                else "".join(prepared.output)
            )
            request = self._state_request(
                prepared.source,
                "output_observed",
                provider_output=text,
                provider_output_complete=(
                    prepared.provider_started
                    and not prepared.task.cancelled()
                    and prepared.generation_error is None
                ),
                generation_error=prepared.generation_error,
                generation_epoch=prepared.generation,
                published_chunks=prepared.published,
                content_scope=prepared.content_scope,
            )
            return await self._native("append", request)
        finally:
            _live_reply.reset(token)

    async def _finish(self, route, record, status, reason, evidence=None):
        request = LiveEffectFinish(
            schema_version="2.0",
            protocol=PROTOCOL,
            original=record.receipt,
            status=status,
            reason_code=reason,
            evidence=evidence,
        )
        if route.finish_request != request:
            if route.finish_request is not None and not (
                route.finish_request.status == "uncertain" and status == "recorded"
            ):
                return await self._lookup(route)
            route.finish_request = request
            route.finish_task = retain_task(
                asyncio.create_task(self.journal.live_effect_finish(request))
            )
        try:
            result = await asyncio.shield(route.finish_task)
        except Exception:
            result = await self._lookup(route)
        if (
            result is not None
            and result.completion is not None
            and result.completion.status != "uncertain"
        ):
            route.retired = True
        return result

    async def step(self):
        await self.tick()
        for key, route in tuple(self.routes.items()):
            if route.retired and all(o.finished and not o.failed for o in route.owners):
                self.routes.pop(key)
            elif route.task is None or route.task.done():
                await self.process_live(route.item, route.effect)
        backlog = False
        for effect, after in tuple(self.cursors.items()):
            query = LiveEffectPendingQuery(
                schema_version="2.0",
                protocol=PROTOCOL,
                effect=effect,
                after_sequence=after,
                limit=32,
            )
            page = await self.journal.pending_live_effects(query)
            for item in page.items:
                await self.process_live(item, effect)
            self.cursors[effect] = 0 if page.exhausted else page.through_sequence
            backlog |= not page.exhausted
        return backlog

    async def run(self):
        if self.run_task is not None:
            raise RuntimeError("The original live Converse consumer already owns its loop")
        self.run_task = asyncio.current_task()
        self.run_owner = ActualOperation(lambda _: None)
        self.run_owner.retain_task(self.run_task)
        self.running = True
        try:
            with actual_owner(self.run_owner):
                while not self.closed:
                    try:
                        if self.native_wait is not None and self.native_wait.done():
                            # A terminal error must not pin the loop to this
                            # observation. Its actual cleanup stays enrolled.
                            native_wait = self.native_wait
                            self.native_wait = None
                            native_wait.result()
                        self.changed.clear()
                        before = await self.journal.journal_wake_cursor()
                        backlog = await self.step()
                        after = await self.journal.journal_wake_cursor()
                        if self.closed:
                            break
                        if backlog or before != after or self.changed.is_set():
                            continue
                        if self.native_wait is None or self.native_wait.done():
                            self.native_wait = retain_task(
                                asyncio.create_task(self.journal.wait_for_journal_change(after))
                            )
                        if self.local_wait is None or self.local_wait.done():
                            self.local_wait = retain_task(asyncio.create_task(self.changed.wait()))
                        await asyncio.wait(
                            {self.native_wait, self.local_wait}, return_when=asyncio.FIRST_COMPLETED
                        )
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                        if not self.closed:
                            await asyncio.sleep(0.1)
        finally:
            self.running = False
            self.closed = True
            self.changed.set()
            await super().close()

    async def close(self):
        self.closed = True
        self.changed.set()
        await super().close()
        pending = [r.task for r in self.routes.values() if r.task is not None and not r.task.done()]
        pending.extend(
            t
            for t in (self.native_wait, self.local_wait, self.run_task)
            if t is not None and t is not asyncio.current_task() and not t.done()
        )
        if pending:
            await asyncio.wait(pending, timeout=2)
        return (
            not self.pending
            and (self.run_owner is None or self.run_owner.finished and not self.run_owner.failed)
            and all(o.finished and not o.failed for r in self.routes.values() for o in r.owners)
        )
