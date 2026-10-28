"""Latest-window scheduling; persistence precedes this independent classifier."""

import asyncio
import hashlib
import math
import time
from collections import OrderedDict
from datetime import UTC, datetime

import httpx

from packages.contracts.models import AppendRequest, SpeechWindow, WindowDecision
from packages.providers.text import ProviderError, ProviderRefusal

MAX_PENDING = 32
MAX_ATTEMPTS = 2
RETRY_DELAY = 0.35


class JournalUnavailable(Exception):
    """Native journal failures are not terminal classifier dispositions."""


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
        if (
            type(max_age) not in (int, float)
            or not math.isfinite(max_age)
            or not 0 < max_age <= 3600
        ):
            raise ValueError("Window maximum age must be positive and at most 3600 seconds")
        self.agent_enabled, self.timeout = agent_enabled, timeout
        self.parallel, self.max_age = max(1, min(parallel, 4)), max_age
        self.jobs = {}
        self.pending = OrderedDict()
        self.outputs = OrderedDict()
        self.last_error = None
        self.running = False

    async def _native(self, operation, *args, **kwargs):
        try:
            return await getattr(self.journal, operation)(*args, **kwargs)
        except Exception as exc:
            raise JournalUnavailable("Native journal is unavailable") from exc

    async def status(self, source, phase, reason, *, attempt=None):
        payload = {"source_event_id": source.event_id, "phase": phase, "reason": reason}
        suffix = ""
        if attempt is not None:
            payload["attempt"] = attempt
            suffix = f":attempt-{attempt}"
        await self._native(
            "append",
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="routing.window_status",
                causes=[source.event_id],
                payload=payload,
                idempotency_key=f"window-status:{source.event_id}:{phase}{suffix}",
            ),
        )

    async def fresh(self, source):
        if source.kind != "conversation.window" or source.origin != "authenticated-user":
            return False
        window = SpeechWindow.model_validate(source.payload)
        session = await self._native("session", source.session_id)
        if session.epoch != source.epoch or session.channel != "converse":
            return False
        current = await self._native(
            "latest_window", source.session_id, source.epoch, window.utterance_id
        )
        return current is not None and current.event_id == source.event_id

    async def _ready(self, source):
        if not await self.fresh(source):
            session = await self._native("session", source.session_id)
            # Reset/deletion is itself a durable fence. An old epoch cannot
            # append a status into the new one, or become admissible later.
            if session.epoch == source.epoch and source.kind == "conversation.window":
                await self.status(source, "superseded", "newer_hypothesis")
            return False
        age = (datetime.now(UTC) - source.created_at).total_seconds()
        if age > self.max_age:
            await self.status(source, "expired", "classification_age_limit")
            return False
        return await self._native("window_decision", source.event_id) is None

    async def classify(self, source, *, attempt=None):
        if not await self._ready(source):
            return
        window = SpeechWindow.model_validate(source.payload)
        await self.status(source, "classifying", "content_window", attempt=attempt)
        recent = await self._native("recent", session_id=source.session_id, limit=100)
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
        if not await self._ready(source):
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
        await self._native(
            "append",
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="routing.window_decided",
                payload=decision.model_dump(mode="json"),
                causes=[source.event_id],
                task_refs=[decision.target_task_id] if decision.target_task_id else [],
                idempotency_key=f"window-decision:{source.event_id}",
            ),
        )
        await self.status(source, "classified", decision.reason_code)

    async def _job(self, source):
        try:
            if not await self._ready(source):
                return
            attempts = await self._native("window_classification_attempts", source.event_id)
            if attempts >= MAX_ATTEMPTS:
                await self.status(source, "error", "classification_attempts_exhausted")
                return
            if attempts:
                # A restart may leave an uncertain admitted request. Count it
                # conservatively, and retain the same bounded retry delay.
                await asyncio.sleep(RETRY_DELAY)
            for attempt in range(attempts + 1, MAX_ATTEMPTS + 1):
                try:
                    await self.classify(source, attempt=attempt)
                    return
                except (TimeoutError, httpx.TransportError, ProviderError) as exc:
                    if (
                        attempt == MAX_ATTEMPTS
                        or isinstance(exc, ProviderRefusal)
                        or "HTTP 402;" in str(exc)
                        or "token budget" in str(exc)
                    ):
                        raise
                    await self.status(source, "retrying", type(exc).__name__, attempt=attempt)
                    await asyncio.sleep(RETRY_DELAY)
        except asyncio.CancelledError:
            raise
        except JournalUnavailable:
            # A failed read/append cannot prove classification failed or that a
            # result did not commit. Leave native backlog/receipts unchanged;
            # the next bounded query reconciles them before another admission.
            self.last_error = "journal_unavailable"
            await asyncio.sleep(RETRY_DELAY)
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
        for key, (_, task) in list(self.jobs.items()):
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                self.jobs.pop(key, None)
        # The journal owns the backlog. This bounded cache may be rebuilt or
        # interrupted without forgetting unadmitted work. Completed/error
        # receipts are excluded before capacity is allocated; busy is transient.
        entries = await self._native("pending_windows", limit=100)
        self.pending.clear()
        for source in entries:
            try:
                if not await self._ready(source):
                    continue
                w = SpeechWindow.model_validate(source.payload)
                key = (source.session_id, source.epoch, w.utterance_id)
                old = self.jobs.get(key)
                if old is not None and not old[1].done():
                    if old[0].event_id == source.event_id:
                        continue
                    # Finish the admitted request and discard a stale result.
                    # Cancelling HTTP cannot recall remote inference and would
                    # pile up uncertain reservations on a slow provider.
                if len(self.pending) >= MAX_PENDING and key not in self.pending:
                    await self.status(source, "busy", "classification_capacity")
                    continue
                self.pending[key] = source
            except Exception as exc:
                self.last_error = type(exc).__name__
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
