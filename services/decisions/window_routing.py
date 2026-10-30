"""Bounded Decisions window classifier and Agent dispatcher (issue #49, §10).

Owns ONLY the streaming-transcript path: classify one authenticated
``conversation.window`` into separate conversation/agent labels, then dispatch
agent-affecting decisions to the native Agent executor. No personality, no
conversation generation, no transcript persistence here.
"""

import asyncio
import hashlib
import json
import re
import time

import httpx
from pydantic import ValidationError

from core.policy import PolicyDenied
from packages.contracts.models import (
    AppendRequest,
    InterruptRequest,
    JournalEntry,
    SpeechWindow,
    SteerRequest,
    Task,
    TaskRequest,
    TaskState,
    WindowDecision,
    new_id,
)
from packages.providers.disclosure import combine_scopes, scope_allowed

CONVERSATION_LABELS = (
    "IGNORE",
    "WAIT_FOR_NEXT_WINDOW",
    "RESPOND_INTERRUPT_USER",
    "RESPOND_WITHOUT_INTERRUPTING_USER",
)

AGENT_LABELS = (
    "IGNORE_FOR_IMMEDIATE_ROUTING",
    "STEER_CURRENT_TASK",
    "INTERRUPT_CURRENT_TASK",
    "QUEUE_UNTIL_TASK_DONE",
)

MAX_CONTEXT_ENTRIES = 12
MAX_ENTRY_CHARS = 2000
MAX_TASKS = 8
READ_LIMIT = 100
MAX_QUEUED_DRAIN = 50


class UnconfirmedAgentAcknowledgement(Exception):
    """A successful transport did not confirm the requested native task."""


def dispatch_failure(exc: Exception) -> dict:
    """Normalize executor admission without interpreting an untrusted error body."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {403, 404, 409}:
            return {"state": "rejected", "reason": "executor_rejected", "http_status": status}
    elif isinstance(exc, (PolicyDenied, ValueError, KeyError)) and not isinstance(
        exc, (ValidationError, json.JSONDecodeError)
    ):
        return {"state": "rejected", "reason": "executor_rejected"}
    return {"state": "unknown", "reason": "executor_ack_unknown"}


def acknowledged_task(
    task,
    source: JournalEntry,
    target_task_id: str | None = None,
    *,
    submission_key: str | None = None,
) -> dict:
    """Bind positive receipts to the real executor response, never a request fallback."""
    if (
        not isinstance(task, Task)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", task.task_id) is None
        or (task.session_id, task.thread_id, task.epoch)
        != (source.session_id, source.thread_id, source.epoch)
        or (target_task_id is not None and task.task_id != target_task_id)
        or (submission_key is not None and task.idempotency_key != submission_key)
    ):
        raise UnconfirmedAgentAcknowledgement("Executor task identity is unconfirmed")
    # Revalidate model_copy/native adapters too; an arbitrary state must never
    # become an affirmative UI receipt. No objective, summary or response text is copied.
    try:
        state = TaskState(task.state).value
    except (TypeError, ValueError) as exc:
        raise UnconfirmedAgentAcknowledgement("Executor task state is unconfirmed") from exc
    return {"native_task_id": task.task_id, "native_task_state": state}


_ALLOWED_SCOPES_LOCAL = {"local_only", "public_synthetic", "private_cloud_authorized"}
_ALLOWED_SCOPES_META = {"public_synthetic"}
_ALLOWED_SCOPES_REMOTE = {"public_synthetic", "private_cloud_authorized"}


def _provider_scopes(provider) -> set[str]:
    info = getattr(provider, "info", None)
    if info is not None and getattr(info, "local", False):
        return _ALLOWED_SCOPES_LOCAL
    if info is not None and (
        getattr(info, "provider", "") == "meta"
        or "contributor" in getattr(info, "model", "").lower()
    ):
        return _ALLOWED_SCOPES_META
    return _ALLOWED_SCOPES_REMOTE


def _entry_scope(entry: JournalEntry) -> str:
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    if entry.kind == "conversation.window":
        try:
            return SpeechWindow.model_validate(payload).content_scope
        except ValueError:
            return "local_only"
    scope = payload.get("content_scope", "local_only")
    return scope if isinstance(scope, str) else "local_only"


def _entry_text(entry: JournalEntry) -> str:
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    text = payload.get("text", "")
    return text if isinstance(text, str) else ""


class WindowClassifier:
    """Classify one streaming window with the configured provider.

    Model output is ONLY the five routing labels; every binding
    (source identity, sequence, revision) comes from the native source
    entry, never from the model. Missing/invalid output raises, it is
    never silently replaced with a fabricated classification.
    """

    def __init__(self, provider, window_chars: int = 36000):
        self.provider = provider
        self.window_chars = window_chars

    def _filter_context(self, context: list[JournalEntry]) -> tuple[list[dict], int]:
        allowed = _provider_scopes(self.provider)
        kept: list[dict] = []
        withheld = 0
        for entry in context[-MAX_CONTEXT_ENTRIES:]:
            if _entry_scope(entry) not in allowed:
                withheld += 1
                continue
            kept.append(
                {
                    "kind": entry.kind,
                    "text": _entry_text(entry)[:MAX_ENTRY_CHARS],
                }
            )
        return kept, withheld

    def _request_body(self, source: JournalEntry, context: list[JournalEntry], tasks: list) -> str:
        window = SpeechWindow.model_validate(source.payload)
        if not scope_allowed(self.provider.info, window.content_scope):
            raise ValueError("Window disclosure does not permit this provider")
        kept, _ = self._filter_context(context)
        task_refs = [
            {
                "task_id": t.task_id,
                "objective_revision": t.objective_revision,
                "state": str(t.state.value if hasattr(t.state, "value") else t.state),
            }
            for t in list(tasks)[:MAX_TASKS]
        ]
        body = json.dumps(
            {
                "latest": {
                    "text": window.text,
                    "user_speaking": window.user_speaking,
                    "capture_ended": window.capture_ended,
                    "utterance_id": window.utterance_id,
                    "window_index": window.window_index,
                    "revision": window.revision,
                },
                "context": kept,
                "tasks": task_refs,
            }
        )
        if len(body) > self.window_chars:
            body = json.dumps(
                {
                    "latest": {
                        "text": window.text,
                        "user_speaking": window.user_speaking,
                        "capture_ended": window.capture_ended,
                    },
                    "context": [],
                    "tasks": task_refs[:5],
                }
            )
        if len(body) > self.window_chars:
            raise ValueError("Current window exceeds classifier context bound")
        return body

    async def classify(
        self,
        source: JournalEntry,
        context: list[JournalEntry],
        tasks: list,
    ) -> dict:
        if source.kind != "conversation.window":
            raise ValueError(f"WindowClassifier needs a conversation.window, got {source.kind!r}")
        window = SpeechWindow.model_validate(source.payload)
        instructions = (
            "Decide how a live transcript window affects conversation timing and, "
            "separately, Agent work. Return ONLY JSON with exactly these keys: "
            '{"conversation":"IGNORE|WAIT_FOR_NEXT_WINDOW|RESPOND_INTERRUPT_USER|'
            'RESPOND_WITHOUT_INTERRUPTING_USER",'
            '"agent_route":"IGNORE_FOR_IMMEDIATE_ROUTING|STEER_CURRENT_TASK|'
            'INTERRUPT_CURRENT_TASK|QUEUE_UNTIL_TASK_DONE",'
            '"target_task_id":null,"expected_objective_revision":null,'
            '"reason_code":"short_label"}. '
            "Judge from successive window contents and context, not fixed silence: "
            "user_speaking/capture_ended are hints only. A trailing fragment such as "
            "'Korina, could you... actually...' waits; a backchannel 'Mm-hmm' during "
            "an explanation is ignored on both channels; 'Also include the documentation.' "
            "during a task responds without interrupting and steers the current task; "
            "'Stop, don't delete that!' during execution responds by interrupting and "
            "interrupts the current task; 'Once that finishes, check the changelog.' "
            "responds and queues until the task is done. target_task_id and "
            "expected_objective_revision must copy the supplied task list entries; "
            "invent nothing. No personality, no instructions, no commentary."
            " A request to stop only the companion's speech uses WAIT_FOR_NEXT_WINDOW and ignores Agent routing."
        )
        started = time.monotonic()
        body = self._request_body(source, context, tasks)
        context_used = json.loads(body)["context"]
        raw = await self.provider.complete(
            [
                {"role": "system", "content": instructions},
                {"role": "user", "content": body},
            ]
        )
        classifier_ms = (time.monotonic() - started) * 1000.0
        text = raw.strip() if isinstance(raw, str) else ""
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Classifier returned non-JSON output") from exc
        if not isinstance(data, dict):
            raise ValueError("Classifier output must be a JSON object")
        if set(data) - {
            "conversation",
            "agent_route",
            "target_task_id",
            "expected_objective_revision",
            "reason_code",
        }:
            raise ValueError("Classifier returned unsupported fields")
        for key in ("conversation", "agent_route", "reason_code"):
            if key not in data:
                raise ValueError(f"Classifier output is missing {key!r}")
        if data["conversation"] not in CONVERSATION_LABELS:
            raise ValueError(f"Unknown conversation label {data['conversation']!r}")
        if data["agent_route"] not in AGENT_LABELS:
            raise ValueError(f"Unknown agent route {data['agent_route']!r}")
        reason = data["reason_code"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
            raise ValueError("Classifier reason_code must be a short non-empty label")
        target = data.get("target_task_id")
        revision = data.get("expected_objective_revision")
        if target is not None and not isinstance(target, str):
            raise ValueError("target_task_id must be a string or null")
        # Bindings come from the native source, never from the model.
        decision = WindowDecision(
            decision_id=new_id(),
            source_event_id=source.event_id,
            source_sequence=source.sequence,
            source_revision=window.revision,
            conversation=data["conversation"],
            agent_route=data["agent_route"],
            target_task_id=target,
            expected_objective_revision=revision,
            reason_code=reason.strip(),
            classifier_ms=classifier_ms,
        )
        return {
            "prompt_chars": len(instructions) + len(body),
            "history_chars": sum(len(e["text"]) for e in context_used),
            "context_entries": len(context_used),
            "conversation": decision.conversation,
            "agent_route": decision.agent_route,
            "target_task_id": decision.target_task_id,
            "expected_objective_revision": decision.expected_objective_revision,
            "reason_code": decision.reason_code,
        }


def _stable_key(decision_id: str, route: str) -> str:
    digest = hashlib.sha256(f"{decision_id}:{route}".encode()).hexdigest()[:32]
    return f"window:{route.lower()}:{digest}"


class WindowAgentDispatcher:
    """Dispatch native ``routing.window_decided`` events to the Agent executor.

    Conversation timing labels are preserved untouched; only the independent
    ``agent_route`` drives executor calls. Queued work persists as
    ``routing.window_dispatch`` receipts so a reopened dispatcher resumes in
    order. No model calls happen here.
    """

    def __init__(self, journal, agent, *, enabled: bool = True):
        self.journal = journal
        self.agent = agent
        self.enabled = enabled
        self.consumer = "window-dispatch-v1"
        self.last_error: str | None = None
        self.running = False
        self.queue_after = 0

    async def _receipts(self, session_id: str, decision_id: str) -> list[JournalEntry]:
        return await self.journal.window_dispatches(session_id=session_id, decision_id=decision_id)

    @staticmethod
    def _semantic_key(source):
        # One action for unchanged speech, even if task state changes the model's label.
        return hashlib.sha256(
            json.dumps(
                [source.epoch, source.payload.get("utterance_id"), source.payload.get("text")],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    async def _record(
        self,
        decision: WindowDecision,
        decision_seq: JournalEntry,
        *,
        state: str,
        reason: str,
        native_task_id: str | None = None,
        native_task_state: str | None = None,
        http_status: int | None = None,
        suffix: str = "",
    ) -> None:
        payload: dict = {
            "decision_id": decision.decision_id,
            "source_event_id": decision.source_event_id,
            "target_task_id": decision.target_task_id,
            "agent_route": decision.agent_route,
            "state": state,
            "reason": reason,
            "source_sequence": decision.source_sequence,
            "source_revision": decision.source_revision,
        }
        source = await self.journal.entry(decision.source_event_id)
        payload["semantic_key"] = self._semantic_key(source)
        if native_task_id is not None:
            payload["native_task_id"] = native_task_id
        if native_task_state is not None:
            payload["native_task_state"] = native_task_state
        if http_status is not None:
            payload["http_status"] = http_status
        key = f"window-dispatch:{decision.decision_id}:{state}{suffix}"
        try:
            await self.journal.append(
                AppendRequest(
                    session_id=decision_seq.session_id,
                    thread_id=decision_seq.thread_id,
                    epoch=decision_seq.epoch,
                    kind="routing.window_dispatch",
                    payload=payload,
                    causes=[decision_seq.event_id],
                    task_refs=[decision.target_task_id] if decision.target_task_id else [],
                    idempotency_key=key,
                )
            )
        except ValueError:
            # An identical receipt already exists; replays stay idempotent.
            pass

    def _receipt_for(self, receipts: list[JournalEntry], decision_id: str, state: str):
        for entry in receipts:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if payload.get("decision_id") == decision_id and payload.get("state") == state:
                return entry
        return None

    def _terminal_for(self, receipts: list[JournalEntry], decision_id: str):
        for entry in receipts:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if payload.get("decision_id") != decision_id:
                continue
            if payload.get("state") in {"applied", "rejected", "unknown", "not_applied"}:
                return entry
        return None

    async def _active_tasks(self, session_id: str, epoch: int) -> list:
        try:
            tasks = await asyncio.wait_for(self.agent.status(), 0.3)
            if not isinstance(tasks, list):
                raise ValueError("Agent status is unavailable")
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise
        self.last_error = None
        return [
            t
            for t in tasks
            if getattr(t, "session_id", None) == session_id
            and getattr(t, "epoch", None) == epoch
            and str(getattr(t, "state", ""))
            not in {"TaskState.SUCCEEDED", "succeeded", "failed", "cancelled"}
            and getattr(getattr(t, "state", ""), "value", getattr(t, "state", ""))
            not in {"succeeded", "failed", "cancelled"}
        ]

    async def _task_by_id(self, task_id: str):
        try:
            tasks = await asyncio.wait_for(self.agent.status(), 0.3)
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise
        if isinstance(tasks, list):
            self.last_error = None
            return next((t for t in tasks if getattr(t, "task_id", None) == task_id), None)
        self.last_error = "invalid_agent_status"
        raise ValueError("Agent status is unavailable")

    @staticmethod
    def _task_state_value(task) -> str:
        state = getattr(task, "state", "")
        return getattr(state, "value", state)

    async def _resolve_source(self, decision: WindowDecision):
        try:
            return await self.journal.resolve(decision.source_event_id)
        except Exception:
            return None

    def _source_matches(
        self, decision: WindowDecision, source: JournalEntry, entry: JournalEntry
    ) -> bool:
        if source.kind != "conversation.window":
            return False
        if source.origin != "authenticated-user":
            return False
        if (source.session_id, source.thread_id, source.epoch) != (
            entry.session_id,
            entry.thread_id,
            entry.epoch,
        ):
            return False
        if source.sequence != decision.source_sequence:
            return False
        payload = source.payload if isinstance(source.payload, dict) else {}
        return payload.get("revision", 1) == decision.source_revision

    async def _superseded_by_newer_text(self, source: JournalEntry) -> bool:
        payload = source.payload if isinstance(source.payload, dict) else {}
        utterance = payload.get("utterance_id")
        if not isinstance(utterance, str):
            return False
        try:
            latest = await self.journal.latest_window(source.session_id, source.epoch, utterance)
        except Exception:
            return False
        if latest is None or latest.event_id == source.event_id:
            newest = latest
        else:
            newest = latest
        if newest is None:
            return False
        newest_payload = newest.payload if isinstance(newest.payload, dict) else {}
        current_text = payload.get("text", "")
        # Compare against the newest snapshot text; flags alone may change.
        return newest_payload.get("text", "") != current_text and (
            newest.event_id != source.event_id
            or newest_payload.get("revision", 1) != payload.get("revision", 1)
        )

    async def _duplicate_applied(
        self, receipts: list[JournalEntry], decision: WindowDecision, source: JournalEntry
    ) -> bool:
        matching = await self.journal.window_dispatches(
            session_id=source.session_id, semantic_key=self._semantic_key(source)
        )
        for entry in matching:
            prior = entry.payload if isinstance(entry.payload, dict) else {}
            if prior.get("state") not in {"applied", "queued", "unknown"}:
                continue
            if prior.get("decision_id") == decision.decision_id:
                continue
            return True
        return False

    async def _interrupt_fenced(self, source):
        control = await self.journal.window_control(
            source.session_id, source.epoch, agent_interrupt=True
        )
        return control is not None and control.payload["source_sequence"] > source.sequence

    async def _target_ready(self, decision: WindowDecision):
        """Return (task, error_reason). None task with None reason means still waiting."""
        if decision.target_task_id is None:
            return None, "untargeted"
        try:
            task = await self._task_by_id(decision.target_task_id)
        except Exception:
            # No mutation was attempted. An unavailable status cannot establish
            # that the target vanished or permit a control with a guessed binding.
            return None, "agent_status_unavailable"
        if task is None:
            return None, "unknown_target"
        if self._task_state_value(task) in {"succeeded", "failed", "cancelled"}:
            return None, "target_terminal"
        if decision.expected_objective_revision is None:
            return None, "missing_revision"
        if getattr(task, "objective_revision", None) != decision.expected_objective_revision:
            return None, "stale_revision"
        return task, None

    async def _dispatch_steer(self, decision, source, entry) -> None:
        task, error = await self._target_ready(decision)
        if error is not None or task is None:
            await self._record(
                decision,
                entry,
                state="not_applied" if error == "agent_status_unavailable" else "rejected",
                reason=error or "unknown_target",
            )
            return
        if (
            getattr(task, "session_id", None) != entry.session_id
            or getattr(task, "epoch", None) != entry.epoch
        ):
            await self._record(decision, entry, state="rejected", reason="target_session_changed")
            return
        payload = source.payload if isinstance(source.payload, dict) else {}
        text = payload.get("text", "")
        request = SteerRequest(
            instruction=text,
            content_scope=combine_scopes(payload.get("content_scope", "local_only")),
            expected_objective_revision=decision.expected_objective_revision,
            idempotency_key=_stable_key(decision.decision_id, decision.agent_route),
            source_event_ids=[source.event_id],
        )
        try:
            updated = await asyncio.wait_for(
                self.agent.steer(decision.target_task_id, request, actor="decisions"), 1
            )
            acknowledgement = acknowledged_task(updated, source, decision.target_task_id)
        except Exception as exc:
            failure = dispatch_failure(exc)
            if failure["state"] == "unknown":
                self.last_error = type(exc).__name__
            await self._record(decision, entry, **failure)
            return
        await self._record(
            decision,
            entry,
            state="applied",
            reason="steered",
            **acknowledgement,
        )

    async def _dispatch_interrupt(self, decision, source, entry) -> None:
        task, error = await self._target_ready(decision)
        if error is not None or task is None:
            await self._record(
                decision,
                entry,
                state="not_applied" if error == "agent_status_unavailable" else "rejected",
                reason=error or "unknown_target",
            )
            return
        if (
            getattr(task, "session_id", None) != entry.session_id
            or getattr(task, "epoch", None) != entry.epoch
        ):
            await self._record(decision, entry, state="rejected", reason="target_session_changed")
            return
        payload = source.payload if isinstance(source.payload, dict) else {}
        request = InterruptRequest(
            expected_objective_revision=decision.expected_objective_revision,
            idempotency_key=_stable_key(decision.decision_id, decision.agent_route),
            reason=payload.get("text", "")[:2000] or "User requested cancellation",
            source_event_ids=[source.event_id],
            source_revision=payload.get("revision", 1),
        )
        try:
            updated = await asyncio.wait_for(
                self.agent.interrupt(decision.target_task_id, request, actor="decisions"), 1
            )
            acknowledgement = acknowledged_task(updated, source, decision.target_task_id)
        except Exception as exc:
            failure = dispatch_failure(exc)
            if failure["state"] == "unknown":
                self.last_error = type(exc).__name__
            await self._record(decision, entry, **failure)
            return
        # An acknowledgment is never proof the work stopped; record only the
        # executor-reported state.
        await self._record(
            decision,
            entry,
            state="applied",
            reason="interrupt_requested",
            **acknowledgement,
        )

    async def _submit_for_queue(self, decision, source, entry) -> None:
        payload = source.payload if isinstance(source.payload, dict) else {}
        request = TaskRequest(
            session_id=entry.session_id,
            thread_id=entry.thread_id,
            epoch=entry.epoch,
            objective=payload.get("text", ""),
            content_scope=combine_scopes(payload.get("content_scope", "local_only")),
            idempotency_key=_stable_key(decision.decision_id, decision.agent_route),
            source_event_ids=[source.event_id],
            source_revision=payload.get("revision", 1),
        )
        try:
            created = await asyncio.wait_for(self.agent.submit(request, actor="decisions"), 1)
            acknowledgement = acknowledged_task(
                created, source, submission_key=request.idempotency_key
            )
        except Exception as exc:
            failure = dispatch_failure(exc)
            if failure["state"] == "unknown":
                self.last_error = type(exc).__name__
            await self._record(decision, entry, **failure)
            return
        await self._record(
            decision,
            entry,
            state="applied",
            reason="queued_submitted",
            **acknowledgement,
            suffix=":submit",
        )

    async def _maybe_drain_queue(self, decision, source, entry, receipts) -> None:
        session = await self.journal.session(entry.session_id)
        if (
            session.epoch != entry.epoch
            or await self._superseded_by_newer_text(source)
            or await self._interrupt_fenced(source)
        ):
            await self._record(decision, entry, state="rejected", reason="queue_source_obsolete")
            return
        if self._receipt_for(receipts, decision.decision_id, "queued") is None:
            await self._record(decision, entry, state="queued", reason="waiting_for_boundary")
        # Status transport failure leaves the durable queue waiting; it never means idle.
        active = await self._active_tasks(entry.session_id, entry.epoch)
        if decision.target_task_id is not None:
            task = await self._task_by_id(decision.target_task_id)
            if task is None:
                await self._record(decision, entry, state="rejected", reason="unknown_target")
                return
            if (getattr(task, "session_id", None), getattr(task, "epoch", None)) != (
                entry.session_id,
                entry.epoch,
            ):
                await self._record(
                    decision, entry, state="rejected", reason="target_session_changed"
                )
                return
            if self._task_state_value(task) == "cancelled":
                await self._record(decision, entry, state="rejected", reason="target_cancelled")
                return
            if self._task_state_value(task) not in {"succeeded", "failed", "cancelled"}:
                if (
                    decision.expected_objective_revision is not None
                    and getattr(task, "objective_revision", None)
                    != decision.expected_objective_revision
                ):
                    await self._record(decision, entry, state="rejected", reason="stale_revision")
                    return
                if self._receipt_for(receipts, decision.decision_id, "queued") is None:
                    await self._record(decision, entry, state="queued", reason="waiting_for_target")
                return
            # Target is terminal: revalidate the revision binding before submitting.
            if (
                decision.expected_objective_revision is not None
                and getattr(task, "objective_revision", None)
                != decision.expected_objective_revision
            ):
                await self._record(decision, entry, state="rejected", reason="stale_revision")
                return
            if active:
                return
            fresh = await self._resolve_source(decision)
            if fresh is None or not self._source_matches(decision, fresh, entry):
                await self._record(
                    decision, entry, state="rejected", reason="source_changed_while_queued"
                )
                return
            if self._receipt_for(receipts, decision.decision_id, "queued") is None:
                await self._record(decision, entry, state="queued", reason="target_done_submit")
            await self._submit_for_queue(decision, fresh, entry)
            return
        if active:
            if self._receipt_for(receipts, decision.decision_id, "queued") is None:
                await self._record(decision, entry, state="queued", reason="waiting_for_idle")
            return
        fresh = await self._resolve_source(decision)
        if fresh is None or not self._source_matches(decision, fresh, entry):
            await self._record(
                decision, entry, state="rejected", reason="source_changed_while_queued"
            )
            return
        if self._receipt_for(receipts, decision.decision_id, "queued") is None:
            await self._record(decision, entry, state="queued", reason="idle_submit")
        await self._submit_for_queue(decision, fresh, entry)

    async def _drain_queues(self, session_id: str, queued=None) -> None:
        if queued is None:
            queued = await self.journal.window_dispatches(
                session_id=session_id, pending_only=True, limit=MAX_QUEUED_DRAIN
            )
        for receipt in queued:
            try:
                decided = await self.journal.entry(receipt.causes[0] if receipt.causes else "")
            except Exception:
                continue
            if decided.kind != "routing.window_decided":
                continue
            try:
                decision = WindowDecision.model_validate(decided.payload)
            except ValueError:
                continue
            if not self.enabled:
                await self._record(decision, decided, state="not_applied", reason="agent_disabled")
                continue
            source = await self._resolve_source(decision)
            if source is None or not self._source_matches(decision, source, decided):
                await self._record(
                    decision, decided, state="rejected", reason="source_changed_while_queued"
                )
                continue
            fresh_receipts = await self._receipts(session_id, decision.decision_id)
            if self._terminal_for(fresh_receipts, decision.decision_id) is not None:
                continue
            await self._maybe_drain_queue(decision, source, decided, fresh_receipts)

    async def _drain_all(self) -> None:
        try:
            receipts = await self.journal.window_dispatches(
                after=self.queue_after, limit=MAX_QUEUED_DRAIN, pending_only=True
            )
            if not receipts and self.queue_after:
                receipts = await self.journal.window_dispatches(
                    after=0, limit=MAX_QUEUED_DRAIN, pending_only=True
                )
        except Exception:
            return
        sessions: list[str] = []
        for entry in receipts:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if payload.get("state") != "queued":
                continue
            if entry.session_id not in sessions:
                sessions.append(entry.session_id)
        selected = sessions[:8]
        # Advance only through the processed prefix. A ninth waiting session
        # must not be skipped forever behind eight busy sessions.
        prefix = []
        for receipt in receipts:
            if receipt.session_id not in selected:
                break
            prefix.append(receipt)
        self.queue_after = prefix[-1].sequence if prefix else 0
        for session_id in selected:
            try:
                await self._drain_queues(
                    session_id, [e for e in prefix if e.session_id == session_id]
                )
            except Exception as exc:
                self.last_error = type(exc).__name__

    async def process(self, entry: JournalEntry):
        if entry.kind != "routing.window_decided":
            return None
        if entry.origin != "decisions":
            return None
        try:
            decision = WindowDecision.model_validate(entry.payload)
        except ValueError as exc:
            self.last_error = type(exc).__name__
            return None
        if not self.enabled:
            await self._record(decision, entry, state="not_applied", reason="agent_disabled")
            return None
        try:
            session = await self.journal.session(entry.session_id)
        except Exception as exc:
            self.last_error = type(exc).__name__
            return None
        if session.epoch != entry.epoch:
            await self._record(decision, entry, state="rejected", reason="stale_epoch")
            return None
        source = await self._resolve_source(decision)
        if source is None or not self._source_matches(decision, source, entry):
            await self._record(decision, entry, state="rejected", reason="stale_or_unknown_source")
            return None
        if await self._superseded_by_newer_text(source):
            await self._record(
                decision, entry, state="rejected", reason="superseded_by_newer_window"
            )
            return None
        receipts = await self._receipts(entry.session_id, decision.decision_id)
        if self._terminal_for(receipts, decision.decision_id) is not None:
            return None
        if decision.agent_route == "IGNORE_FOR_IMMEDIATE_ROUTING":
            return None
        if await self._interrupt_fenced(source):
            await self._record(decision, entry, state="rejected", reason="later_interrupt")
            return None
        if await self._duplicate_applied(receipts, decision, source):
            await self._record(decision, entry, state="rejected", reason="duplicate_suppressed")
            return None
        try:
            if decision.agent_route == "STEER_CURRENT_TASK":
                await self._dispatch_steer(decision, source, entry)
            elif decision.agent_route == "INTERRUPT_CURRENT_TASK":
                await self._dispatch_interrupt(decision, source, entry)
            else:
                await self._maybe_drain_queue(decision, source, entry, receipts)
        except Exception as exc:
            self.last_error = type(exc).__name__
        try:
            await self._drain_queues(entry.session_id)
        except Exception as exc:
            self.last_error = type(exc).__name__
        return decision

    async def tick(self) -> int:
        cursor = await self.journal.cursor(self.consumer)
        entries = await self.journal.read(after=cursor, limit=READ_LIMIT)
        for entry in entries:
            try:
                await self.process(entry)
            except Exception as exc:
                self.last_error = type(exc).__name__
            try:
                await self.journal.acknowledge(self.consumer, entry.sequence)
            except ValueError as exc:
                self.last_error = type(exc).__name__
                break
        else:
            if entries:
                pass
        # Drain persisted queues even when no new decisions arrived, so a
        # reopened dispatcher resumes waiting work in order.
        try:
            await self._drain_all()
        except Exception as exc:
            self.last_error = type(exc).__name__
        return len(entries)

    async def run(self) -> None:
        self.running = True
        try:
            while self.running:
                try:
                    count = await self.tick()
                    if not self.enabled:
                        self.last_error = None
                    if not count:
                        await asyncio.sleep(0.05)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    await asyncio.sleep(1)
        finally:
            self.running = False
