"""Latest-window scheduling; persistence precedes this independent classifier."""

import asyncio
import hashlib
import math
import time
from collections import OrderedDict
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from packages.contracts.models import AppendRequest, SpeechWindow, WindowDecision
from packages.providers.decision_capture import (
    CaptureNativeUnavailable,
    capture_sources,
    decision_attempt,
)
from packages.providers.disclosure import combine_scopes, source_scope
from packages.providers.text import ProviderError, ProviderRefusal
from services.decisions.window_routing import (
    admissible_window_projection,
    is_ambiguous_multi_task_target,
)

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
        self,
        journal,
        agent,
        classifier,
        *,
        agent_enabled=True,
        timeout=5,
        parallel=2,
        max_age=60,
        model_settings=None,
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
        self.output_models = {}
        self.model_settings = model_settings
        self.last_error = None
        self.agent_available = None
        self.running = False

    async def _native(self, operation, *args, **kwargs):
        try:
            return await getattr(self.journal, operation)(*args, **kwargs)
        except Exception as exc:
            raise JournalUnavailable("Native journal is unavailable") from exc

    async def status(self, source, phase, reason, *, attempt=None):
        payload = {
            "source_event_id": source.event_id,
            "source_sequence": source.sequence,
            "source_revision": source.payload.get("revision", 1),
            "phase": phase,
            "reason": reason,
        }
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
        with (
            self.model_settings.preparing("windows")
            if self.model_settings is not None
            else nullcontext()
        ):
            return await self._classify(source, attempt=attempt)

    async def _scoped_source(self, source, window):
        if window.revision == 1:
            return source, window
        if not (
            hasattr(self.journal, "entry")
            and hasattr(self.journal, "resolve")
            and hasattr(self.journal, "read")
        ):
            return source, window
        try:
            floor = await source_scope(self.journal, source.event_id, resolved=source)
        except Exception as exc:
            raise JournalUnavailable("Native journal is unavailable") from exc
        scope = combine_scopes(window.content_scope, floor)
        if scope == window.content_scope:
            return source, window
        updated = source.model_copy(update={"payload": {**source.payload, "content_scope": scope}})
        return updated, SpeechWindow.model_validate(updated.payload)

    async def _resolve_context(self, source, window, recent):
        has_resolver = (
            hasattr(self.journal, "entry")
            and hasattr(self.journal, "resolve")
            and hasattr(self.journal, "read")
        )
        revised_targets = {
            e.payload.get("target_event_id")
            for e in recent
            if e.epoch == source.epoch
            and e.thread_id == source.thread_id
            and e.kind == "conversation.revision"
            and isinstance(e.payload, dict)
        }
        window_slot_counts = {}
        for e in recent:
            if (
                e.epoch == source.epoch
                and e.thread_id == source.thread_id
                and e.kind == "conversation.window"
                and isinstance(e.payload, dict)
            ):
                slot = (e.payload.get("utterance_id"), e.payload.get("window_index"))
                window_slot_counts[slot] = window_slot_counts.get(slot, 0) + 1

        candidates = [
            e
            for e in recent
            if e.epoch == source.epoch
            and e.thread_id == source.thread_id
            and e.sequence < source.sequence
            and e.kind in {"conversation.window", "conversation.user", "conversation.converse"}
        ]
        seen_slots = set()
        resolved_entries = []
        full_page = len(recent) >= 100
        source_hyp = hypothesis(source)
        for e in candidates:
            entry = e
            if e.kind == "conversation.window":
                try:
                    w = SpeechWindow.model_validate(e.payload)
                except ValueError:
                    continue
                slot = (w.utterance_id, w.window_index)
                if slot in seen_slots:
                    continue
                needs_resolve = has_resolver and (
                    full_page or w.revision > 1 or window_slot_counts.get(slot, 0) > 1
                )
                if needs_resolve:
                    try:
                        latest = await self.journal.resolve(e.event_id)
                        floor = await source_scope(self.journal, latest.event_id, resolved=latest)
                    except Exception as exc:
                        raise JournalUnavailable("Native journal is unavailable") from exc
                    w_latest = SpeechWindow.model_validate(latest.payload)
                    scope = combine_scopes(w_latest.content_scope, floor)
                    entry = (
                        latest
                        if scope == w_latest.content_scope
                        else latest.model_copy(
                            update={"payload": {**latest.payload, "content_scope": scope}}
                        )
                    )
                seen_slots.add(slot)
                if w.utterance_id == window.utterance_id and hypothesis(entry) == source_hyp:
                    continue
            elif e.kind == "conversation.user":
                needs_resolve = has_resolver and (
                    full_page or e.event_id in revised_targets or e.payload.get("revision", 1) > 1
                )
                if needs_resolve:
                    try:
                        latest = await self.journal.resolve(e.event_id)
                        floor = await source_scope(self.journal, e.event_id, resolved=latest)
                    except Exception as exc:
                        raise JournalUnavailable("Native journal is unavailable") from exc
                    scope = combine_scopes(latest.payload.get("content_scope", "local_only"), floor)
                    entry = latest.model_copy(
                        update={"payload": {**latest.payload, "content_scope": scope}}
                    )
            resolved_entries.append(entry)
        return resolved_entries[-12:]

    def _active_provider_info(self):
        if self.model_settings is not None:
            state = self.model_settings._state()
            info, _ = self.model_settings._selection_info(state[2])
            return info
        provider = getattr(self.classifier, "provider", None)
        return getattr(provider, "info", None)

    async def _scoped_tasks(self, tasks):
        if len(tasks) <= 1 or not (
            hasattr(self.journal, "entry")
            and hasattr(self.journal, "resolve")
            and hasattr(self.journal, "read")
        ):
            return tasks
        scoped = []
        for task in tasks:
            scope = getattr(task, "content_scope", "local_only") or "local_only"
            source_ids = getattr(task, "source_event_ids", None) or []
            for source_id in source_ids:
                if not isinstance(source_id, str) or not source_id:
                    continue
                try:
                    floor = await source_scope(self.journal, source_id)
                    scope = combine_scopes(scope, floor)
                except Exception:
                    scope = "local_only"
            if scope != getattr(task, "content_scope", "local_only") and hasattr(
                task, "model_copy"
            ):
                task = task.model_copy(update={"content_scope": scope})
            scoped.append(task)
        return scoped

    async def _classify(self, source, *, attempt=None):
        if not await self._ready(source):
            return
        window = SpeechWindow.model_validate(source.payload)
        await self.status(source, "classifying", "content_window", attempt=attempt)
        source, window = await self._scoped_source(source, window)
        recent = await self._native("recent", session_id=source.session_id, limit=100)
        context = await self._resolve_context(source, window, recent)
        tasks, available = [], self.agent_enabled
        if available:
            try:
                tasks = await asyncio.wait_for(self.agent.status(), 0.25)
                if not isinstance(tasks, list):
                    raise ValueError("Agent status is unavailable")
                tasks = [
                    t
                    for t in tasks
                    if t.session_id == source.session_id
                    and t.epoch == source.epoch
                    and t.state not in {"succeeded", "failed", "cancelled"}
                ]
            except Exception:
                tasks, available = [], False
        tasks = await self._scoped_tasks(tasks)
        self.agent_available = available if self.agent_enabled else None
        if self.agent_enabled and not available:
            await self.status(source, "agent_unavailable", "agent_status_unavailable")
        started = time.monotonic()
        key = (source.session_id, source.epoch, window.utterance_id)
        model_stamp = self.model_settings.cache_stamp() if self.model_settings is not None else None
        provider_info = self._active_provider_info()
        window_chars = getattr(self.classifier, "window_chars", 36000)
        (
            source_admissible,
            admissible_context,
            _kept_context,
            _task_refs,
            input_digest,
        ) = admissible_window_projection(
            source,
            context,
            tasks,
            provider_info=provider_info,
            window_chars=window_chars,
            agent_enabled=self.agent_enabled,
            agent_available=available,
            model_stamp=model_stamp,
        )
        cached = self.outputs.get(key)
        reused = (
            source_admissible
            and cached is not None
            and cached[0] == input_digest
            and self.output_models.get(key) == model_stamp
        )
        if reused:
            labels = dict(cached[2])
            original_call = cached[3]
            if original_call is not None:
                async with decision_attempt(
                    self.journal,
                    source,
                    getattr(self.classifier, "provider", None),
                    attempt=attempt,
                    reused_from=original_call,
                ) as capture:
                    capture_sources(source, admissible_context)
                    if hasattr(self.journal, "entry") and hasattr(self.journal, "resolve"):
                        await capture.check_current(
                            SimpleNamespace(info=provider_info)
                            if provider_info is not None
                            else getattr(self.classifier, "provider", None)
                        )
                    capture.validated_output = labels
        elif window.text.strip():
            if self.model_settings is None:
                async with decision_attempt(
                    self.journal,
                    source,
                    getattr(self.classifier, "provider", None),
                    attempt=attempt,
                ) as capture:
                    labels = await asyncio.wait_for(
                        self.classifier.classify(source, admissible_context, tasks), self.timeout
                    )
                    capture.validated_output = labels
            else:
                from services.decisions.window_routing import WindowClassifier

                async with self.model_settings.operation() as lease:
                    classifier = WindowClassifier(
                        lease.provider, window_chars=self.classifier.window_chars
                    )
                    async with decision_attempt(
                        self.journal,
                        source,
                        lease.provider,
                        attempt=attempt,
                        model_pin=lease.pin,
                        current=lease.current,
                    ) as capture:
                        labels = await asyncio.wait_for(
                            classifier.classify(source, admissible_context, tasks), self.timeout
                        )
                        capture.validated_output = labels
            original_call = capture.call_id if capture.started else None
        else:
            labels = {
                "conversation": "IGNORE",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "reason_code": "no_transcript_text",
            }
            original_call = None
        if labels.get("agent_route") in {"STEER_CURRENT_TASK", "INTERRUPT_CURRENT_TASK"}:
            target = next((t for t in tasks if t.task_id == labels.get("target_task_id")), None)
            if is_ambiguous_multi_task_target(target, tasks, provider_info=provider_info):
                labels = {
                    **labels,
                    "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                    "target_task_id": None,
                    "expected_objective_revision": None,
                    "reason_code": "ambiguous_target",
                }
        self.outputs[key] = (input_digest, hypothesis(source), dict(labels), original_call)
        self.output_models[key] = model_stamp
        self.outputs.move_to_end(key)
        if len(self.outputs) > 128:
            evicted, _ = self.outputs.popitem(last=False)
            self.output_models.pop(evicted, None)
        if not self.agent_enabled:
            # Explicitly disabled work never replays later. A failed status
            # probe is different: preserve valid intent, then let the native
            # dispatcher wait for a confirmed boundary and recheck admission.
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
        except (JournalUnavailable, CaptureNativeUnavailable):
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
