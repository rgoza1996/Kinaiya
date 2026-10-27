"""Latest-window scheduling; persistence precedes this independent classifier."""

import asyncio
import hashlib
import time
from collections import OrderedDict
from datetime import UTC, datetime

import httpx

from packages.contracts.models import AppendRequest, SpeechWindow, WindowDecision
from packages.providers.text import ProviderError


def hypothesis(entry):
    window = SpeechWindow.model_validate(entry.payload)
    return (
        window.text,
        window.user_speaking,
        window.capture_ended,
        window.revision,
        window.content_scope,
    )


class WindowDecisionsWorker:
    def __init__(
        self, journal, agent, classifier, *, agent_enabled=True, timeout=5, parallel=2, max_age=60
    ):
        self.journal, self.agent, self.classifier = journal, agent, classifier
        if type(timeout) not in (int, float) or not 0.1 <= timeout <= 30:
            raise ValueError("Window classification timeout must be .1..30 seconds")
        self.agent_enabled, self.timeout = agent_enabled, timeout
        self.parallel, self.max_age = max(1, min(parallel, 4)), max_age
        self.after = 0
        self.jobs = {}
        self.pending = OrderedDict()
        self.outputs = OrderedDict()
        self.last_error = None
        self.running = False

    async def status(self, source, phase, reason):
        await self.journal.append(
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="routing.window_status",
                causes=[source.event_id],
                payload={"source_event_id": source.event_id, "phase": phase, "reason": reason},
                idempotency_key=f"window-status:{source.event_id}:{phase}",
            )
        )

    async def fresh(self, source):
        if source.kind != "conversation.window" or source.origin != "authenticated-user":
            return False
        window = SpeechWindow.model_validate(source.payload)
        session = await self.journal.session(source.session_id)
        if session.epoch != source.epoch or session.channel != "converse":
            return False
        current = await self.journal.latest_window(
            source.session_id, source.epoch, window.utterance_id
        )
        return current is not None and hypothesis(current) == hypothesis(source)

    async def classify(self, source):
        if not await self.fresh(source):
            return
        if await self.journal.window_decision(source.event_id) is not None:
            return
        window = SpeechWindow.model_validate(source.payload)
        await self.status(source, "classifying", "content_window")
        recent = await self.journal.recent(session_id=source.session_id, limit=100)
        context = [
            e
            for e in recent
            if e.epoch == source.epoch
            and e.sequence < source.sequence
            and e.kind in {"conversation.window", "conversation.user", "conversation.converse"}
        ][-12:]
        tasks, available = [], self.agent_enabled
        if available:
            try:
                tasks = await asyncio.wait_for(self.agent.status(), 0.25)
                tasks = [
                    t
                    for t in tasks
                    if t.session_id == source.session_id
                    and t.epoch == source.epoch
                    and t.state not in {"succeeded", "failed", "cancelled"}
                ]
            except Exception:
                available = False
        started = time.monotonic()
        key = (source.session_id, source.epoch, window.utterance_id)
        task_binding = (
            available,
            tuple(sorted((t.task_id, t.objective_revision, str(t.state)) for t in tasks)),
        )
        cached = self.outputs.get(key)
        reused = cached is not None and cached[:2] == (hypothesis(source), task_binding)
        if reused:
            labels = dict(cached[2])
        elif window.text.strip():
            labels = await asyncio.wait_for(
                self.classifier.classify(source, context, tasks), self.timeout
            )
        else:
            labels = {
                "conversation": "IGNORE",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "reason_code": "no_transcript_text",
            }
        self.outputs[key] = (hypothesis(source), task_binding, dict(labels))
        self.outputs.move_to_end(key)
        if len(self.outputs) > 128:
            self.outputs.popitem(last=False)
        if not available:
            labels = {
                **labels,
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "target_task_id": None,
                "expected_objective_revision": None,
            }
        if not await self.fresh(source):
            await self.status(source, "superseded", "newer_hypothesis")
            return
        decision = WindowDecision(
            **labels,
            classification_reused=reused,
            source_event_id=source.event_id,
            source_sequence=source.sequence,
            source_revision=window.revision,
            decision_id="window-" + hashlib.sha256(source.event_id.encode()).hexdigest()[:32],
            classifier_ms=(time.monotonic() - started) * 1000,
        )
        await self.journal.append(
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="routing.window_decided",
                payload=decision.model_dump(mode="json"),
                causes=[source.event_id],
                task_refs=[decision.target_task_id] if decision.target_task_id else [],
                idempotency_key=f"window-decision:{source.event_id}",
            )
        )
        await self.status(source, "classified", decision.reason_code)

    async def _job(self, source):
        try:
            for attempt in range(2):
                try:
                    await self.classify(source)
                    return
                except (TimeoutError, httpx.TransportError) as exc:
                    if attempt:
                        raise
                    await self.status(source, "retrying", type(exc).__name__)
                    await asyncio.sleep(0.35)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = type(exc).__name__
            if isinstance(exc, ProviderError):
                if "HTTP 402;" in str(exc):
                    reason = "provider_budget_refused"
                elif "token budget" in str(exc):
                    reason = "provider_output_limit"
                else:
                    reason = "provider_unavailable"
            self.last_error = reason
            try:
                await self.status(source, "error", reason)
            except Exception:
                pass

    async def tick(self):
        # Re-scan on process startup. A cursor advance is not proof that an
        # in-flight classification committed; old windows expire, not replay.
        entries = await self.journal.read(
            after=self.after, limit=100, kinds=["conversation.window"]
        )
        for source in entries:
            self.after = source.sequence
            age = (datetime.now(UTC) - source.created_at).total_seconds()
            if age > self.max_age:
                continue
            try:
                if not await self.fresh(source):
                    continue
                w = SpeechWindow.model_validate(source.payload)
                key = (source.session_id, source.epoch, w.utterance_id)
                old = self.jobs.get(key)
                if old is not None and not old[1].done():
                    if hypothesis(old[0]) == hypothesis(source):
                        continue
                    # Finish the admitted request and discard a stale result.
                    # Cancelling HTTP cannot recall remote inference and would
                    # pile up uncertain reservations on a slow provider.
                self.pending[key] = source
                self.pending.move_to_end(key)
                if len(self.pending) > 32:
                    _, dropped = self.pending.popitem(last=False)
                    await self.status(dropped, "busy", "classification_capacity")
            except Exception as exc:
                self.last_error = type(exc).__name__
        for key, (_, task) in list(self.jobs.items()):
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                self.jobs.pop(key, None)
        for _ in range(len(self.pending)):
            if len(self.jobs) >= self.parallel:
                break
            key, source = self.pending.popitem(last=False)
            if key in self.jobs:
                self.pending[key] = source
                continue
            self.jobs[key] = (source, asyncio.create_task(self._job(source)))
        return len(entries)

    async def close(self):
        for _, task in self.jobs.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in self.jobs.values()), return_exceptions=True)
        self.jobs.clear()

    async def run(self):
        self.running = True
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
            self.running = False
            await self.close()
