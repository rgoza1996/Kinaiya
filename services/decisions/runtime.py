"""Durable asynchronous attention routing; never on Converse's response path."""

import asyncio
import json
import math
import re
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace

import httpx

from packages.contracts.decisions_attempts import (
    MAX_ATTEMPTS as MAX_NATIVE_ATTEMPTS,
)
from packages.contracts.decisions_attempts import DecisionsAttemptLookup, DecisionsSource
from packages.contracts.models import (
    AppendRequest,
    Decision,
    InterruptRequest,
    SteerRequest,
    TaskRequest,
)
from packages.providers.classification_errors import (
    classification_refusal_reason,
    retained_refusal_reason,
)
from packages.providers.decision_admission import NativeDecisionAttempt, observe_completion
from packages.providers.decision_capture import (
    CaptureNativeUnavailable,
    DecisionCapture,
    _capture,
    capture_prompt_scope,
    capture_sources,
    decision_attempt,
)
from packages.providers.disclosure import (
    combine_scopes,
    entry_allowed,
    scope_allowed,
    source_scope,
)
from packages.providers.text import ProviderError, ProviderRefusal, owned_http_stream_cleanup
from services.decisions.model_settings import DecisionsModelUnavailable
from services.decisions.prompts import DEFAULT_PROMPT, instructions, prompt_stamp
from services.decisions.window_routing import (
    _project_tasks_with_sources,
    acknowledged_task,
    dispatch_failure,
    is_ambiguous_multi_task_target,
    resolve_scoped_tasks,
)

DISPATCH_TIMEOUT = 15  # Match the existing Agent HTTP client's admission bound.
MAX_ATTEMPTS = 2
FINALIZED_ROUTES = frozenset({"ignore", "queue", "steer", "interrupt"})
ALLOWED_CLASSIFICATION_KEYS = frozenset(
    {
        "route",
        "target_task_id",
        "expected_objective_revision",
        "reason_code",
        "confidence",
    }
)


class InvalidClassificationError(ProviderRefusal):
    """Deterministic invalid model output; refused without fabricating Ignore."""

    def __init__(self, reason_code: str, message: str = "Invalid classification output") -> None:
        self.reason_code = reason_code
        super().__init__(message)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidClassificationError("malformed_json")
        result[key] = value
    return result


def _reject_constant(_token):
    raise InvalidClassificationError("malformed_json")


def parse_finalized_classification(raw) -> dict:
    """Strictly parse a raw classifier string into a JSON object without repair."""
    if not isinstance(raw, str):
        raise InvalidClassificationError("malformed_json")
    text = raw.strip()
    if text.startswith("```"):
        if "\n" not in text:
            raise InvalidClassificationError("malformed_json")
        inner = text.split("\n", 1)[1]
        if "```" not in inner:
            raise InvalidClassificationError("malformed_json")
        text = inner.rsplit("```", 1)[0].strip()
    if not text:
        raise InvalidClassificationError("malformed_json")
    try:
        data = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except InvalidClassificationError:
        raise
    except (
        json.JSONDecodeError,
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        raise InvalidClassificationError("malformed_json") from None
    return validate_classification_dict(data)


def validate_classification_dict(data, *, source=None) -> dict:
    """Validate the complete finalized classification dictionary contract."""
    if not isinstance(data, dict):
        raise InvalidClassificationError("invalid_shape")
    if set(data) - ALLOWED_CLASSIFICATION_KEYS:
        raise InvalidClassificationError("invalid_shape")
    if "route" not in data:
        raise InvalidClassificationError("missing_route")
    route = data["route"]
    if type(route) is not str or route not in FINALIZED_ROUTES:
        raise InvalidClassificationError("invalid_route")
    target_task_id = data.get("target_task_id")
    if target_task_id is not None and (
        type(target_task_id) is not str
        or not target_task_id.strip()
        or len(target_task_id) > 128
        or target_task_id != target_task_id.strip()
    ):
        raise InvalidClassificationError("invalid_target_fields")
    expected_revision = data.get("expected_objective_revision")
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 1
    ):
        raise InvalidClassificationError("invalid_target_fields")
    if route in {"steer", "interrupt"} and (target_task_id is None or expected_revision is None):
        raise InvalidClassificationError("invalid_target_fields")
    if "reason_code" in data:
        reason_code = data["reason_code"]
        if type(reason_code) is not str or not reason_code.strip() or len(reason_code) > 120:
            raise InvalidClassificationError("invalid_reason_code")
    if "confidence" in data and data["confidence"] is not None:
        confidence = data["confidence"]
        if (
            type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise InvalidClassificationError("invalid_confidence")
    if source is not None and isinstance(getattr(source, "payload", None), dict):
        text = source.payload.get("text", "")
        if not isinstance(text, str):
            raise InvalidClassificationError("invalid_instruction")
        if (
            route in {"queue", "steer"}
            and not source.payload.get("outcome_event_id")
            and (not text.strip() or len(text) > 16000)
        ):
            raise InvalidClassificationError("invalid_instruction")
    result = {}
    for key in (
        "route",
        "target_task_id",
        "expected_objective_revision",
        "reason_code",
        "confidence",
    ):
        if key in data:
            result[key] = data[key].strip() if key == "reason_code" else data[key]
    return result


class ModelClassifier:
    def __init__(self, provider, window_chars=36000, *, prompt=None):
        self.provider = provider
        self.window_chars = window_chars
        self.prompt = DEFAULT_PROMPT if prompt is None else prompt

    async def classify(self, source, context, tasks):
        prompt = self.prompt
        prompt_stamp(prompt, "finalized", self.provider.info)
        capture_prompt_scope(prompt.content_scope)
        if not entry_allowed(self.provider.info, source):
            raise ProviderRefusal("Source disclosure does not permit this classifier")
        capture = _capture.get()
        if capture is not None and any(
            getattr(t, "source_event_ids", None) and getattr(t, "_disclosure_sources", None) is None
            for t in tasks
        ):
            tasks = await resolve_scoped_tasks(capture.journal, tasks)
        system_instructions = instructions(prompt, "finalized")
        included_context = [e for e in context[-12:] if entry_allowed(self.provider.info, e)]
        projected_tasks, task_sources = _project_tasks_with_sources(
            tasks, provider_info=self.provider.info, limit=8
        )
        body = json.dumps(
            {
                "context": [
                    {"kind": e.kind, "text": e.payload.get("text", "")[:2000]}
                    for e in included_context
                ],
                "latest": {"kind": source.kind, "text": source.payload.get("text", "")},
                "tasks": projected_tasks,
            }
        )
        if len(body) > self.window_chars:
            included_context = []
            projected_tasks, task_sources = _project_tasks_with_sources(
                tasks, provider_info=self.provider.info, limit=5
            )
            body = json.dumps(
                {
                    "context": [],
                    "latest": {"kind": source.kind, "text": source.payload.get("text", "")},
                    "tasks": projected_tasks,
                }
            )
        if len(body) > self.window_chars:
            raise ProviderRefusal("Current message exceeds classifier context bound")
        capture_sources(source, [*included_context, *task_sources])
        raw = await self.provider.complete(
            [{"role": "system", "content": system_instructions}, {"role": "user", "content": body}]
        )
        return validate_classification_dict(
            parse_finalized_classification(raw),
            source=source,
        )


def _is_budget_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 402:
        return True
    if isinstance(exc, ProviderError):
        lowered = str(exc).lower()
        if "token budget" in lowered or "exhausted budget" in lowered:
            return True
        if re.search(r"\bhttp\s+402\b", lowered) is not None:
            return True
    return False


def _extract_raw_classification_text(raw: str, request_format: str | None) -> str | None:
    if request_format in {"laya_systemone", "native_d1_systemone"}:
        return None
    try:
        envelope = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(envelope, dict):
        return raw
    if (
        isinstance(envelope.get("choices"), list)
        and envelope["choices"]
        and isinstance(envelope["choices"][0], dict)
        and isinstance(envelope["choices"][0].get("message"), dict)
        and isinstance(envelope["choices"][0]["message"].get("content"), str)
    ):
        return envelope["choices"][0]["message"]["content"]
    if request_format == "responses" and isinstance(envelope.get("output"), list):
        parts = []
        for item in envelope["output"]:
            if isinstance(item, dict) and item.get("type") == "message":
                for block in item.get("content") or []:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "output_text"
                        and isinstance(block.get("text"), str)
                    ):
                        parts.append(block["text"])
        return "".join(parts) if parts else None
    return raw


def _recover_validated_output(call: dict, source) -> dict:
    raw = call.get("raw_response")
    validated = call.get("validated_output")
    request_format = call.get("request_format")
    raw_parsed = None
    if isinstance(raw, str):
        text = _extract_raw_classification_text(raw, request_format)
        if text is not None:
            raw_parsed = validate_classification_dict(
                parse_finalized_classification(text), source=source
            )
    validated_parsed = None
    if validated is not None:
        validated_parsed = validate_classification_dict(validated, source=source)
    if raw_parsed is not None and validated_parsed is not None:
        if raw_parsed != validated_parsed:
            raise InvalidClassificationError("invalid_shape")
        return raw_parsed
    if raw_parsed is not None:
        return raw_parsed
    if validated_parsed is not None:
        return validated_parsed
    raise InvalidClassificationError("invalid_shape")


class PersistedCallState:
    """Durable call-history recovery state for a finalized Decisions source."""

    __slots__ = ("state", "prior_attempts", "output", "refusal", "limit")

    def __init__(
        self,
        state: str,
        prior_attempts: int | None,
        output: dict | None = None,
        refusal: tuple[str, str] | None = None,
        limit: int = MAX_ATTEMPTS,
    ) -> None:
        self.state = state
        self.prior_attempts = prior_attempts
        self.output = output
        self.refusal = refusal
        self.limit = limit

    def __iter__(self):
        yield self.prior_attempts
        yield self.output
        yield self.refusal

    def __repr__(self) -> str:
        return (
            f"PersistedCallState(state={self.state!r}, "
            f"prior_attempts={self.prior_attempts!r}, "
            f"output={self.output!r}, refusal={self.refusal!r})"
        )


def validated_classification(source, result):
    # Inspection is observational: native stale-target/feedback overrides must
    # still run even when the original labels cannot form a valid Decision.
    try:
        Decision(
            **result,
            source_event_ids=[source.event_id],
            source_revision=source.payload.get("revision", 1),
            instruction=source.payload.get("text", "")
            if result.get("route") in {"queue", "steer"}
            else "",
            idempotency_key=f"validate:{source.event_id}",
        )
    except (TypeError, ValueError):
        return None
    return result


class DecisionsWorker:
    def __init__(
        self,
        journal,
        agent,
        classifier,
        *,
        window_turns=12,
        enabled=True,
        model_settings=None,
        max_attempts=None,
        native_media_owner=None,
        external_agent=None,
    ):
        self.journal, self.agent, self.classifier = journal, agent, classifier
        self.window_turns, self.enabled = window_turns, enabled
        self.consumer = "decisions-v1"
        self.model_settings = model_settings
        self.native_media_owner = native_media_owner
        if enabled and external_agent is not None:
            raise ValueError("Decisions requires one work executor")
        self.external_agent = external_agent
        if max_attempts is not None and (
            type(max_attempts) is not int or not 1 <= max_attempts <= MAX_NATIVE_ATTEMPTS
        ):
            raise ValueError("Worker max_attempts must be an integer from 1 to 16 or None")
        self.max_attempts = max_attempts
        self.last_error = None
        self.running = False
        self.ready = False
        self._pending_refusals = {}
        self._pending_decisions = {}
        self._transport_attempts = {}
        self._lookup_failures = {}
        self._preparation_failures = {}

    async def complete_native_media(self, source, fence, principal, *, key, timeout=30.0):
        """Imported native evidence can publish routing, never admit commands."""
        from services.session.media_clients import refuse
        from services.session.native_media import NativeBufferedMediaOwner

        if (
            type(self.native_media_owner) is not NativeBufferedMediaOwner
            or self.model_settings is None
            or not self.enabled
        ):
            refuse()
        lease = self.model_settings._capture()
        return await self.native_media_owner.run(
            source, fence, principal, lease, key=key, timeout=timeout, caller=self
        )

    def _active_provider_info(self):
        if self.model_settings is not None:
            state = self.model_settings._state()
            info, _ = self.model_settings._selection_info(state[2])
            return info
        provider = getattr(self.classifier, "provider", None)
        return getattr(provider, "info", None)

    def _call_binding_refusal(
        self, call: dict, source, scoped_source, info
    ) -> tuple[str, str] | None:
        expected_revision = source.payload.get("revision", 1)
        if (
            call.get("source_event_id") != source.event_id
            or call.get("source_sequence") not in (None, source.sequence)
            or call.get("source_revision") != expected_revision
            or call.get("session_id") != source.session_id
            or call.get("thread_id") != source.thread_id
            or call.get("epoch") != source.epoch
        ):
            return ("invalid_call_binding", "InvalidClassificationError")
        scope = scoped_source.payload.get("content_scope", "local_only")
        if info is not None and not entry_allowed(info, scoped_source):
            return ("provider_refused", "ProviderRefusal")
        model_pin = call.get("model_pin")
        if isinstance(model_pin, dict):
            pin_local = model_pin.get("local")
            if type(pin_local) is not bool or not scope_allowed(
                SimpleNamespace(local=pin_local), scope
            ):
                return ("provider_refused", "ProviderRefusal")
        if info is not None:
            if call.get("model") != info.model or call.get("provider") != info.provider:
                return ("invalid_call_binding", "InvalidClassificationError")
            if isinstance(model_pin, dict) and (
                model_pin.get("model") != info.model or model_pin.get("provider") != info.provider
            ):
                return ("invalid_call_binding", "InvalidClassificationError")
        else:
            if call.get("model") is not None or call.get("provider") is not None:
                return ("invalid_call_binding", "InvalidClassificationError")
        return None

    async def _scoped_source(self, entry):
        if entry.kind not in {"conversation.user", "conversation.revision"}:
            return entry
        source_id = (
            entry.payload.get("target_event_id")
            if entry.kind == "conversation.revision"
            else entry.event_id
        )
        floor = (
            await source_scope(self.journal, source_id)
            if isinstance(source_id, str)
            else "local_only"
        )
        return entry.model_copy(
            update={
                "payload": {
                    **entry.payload,
                    "content_scope": combine_scopes(
                        entry.payload.get("content_scope", "local_only"), floor
                    ),
                }
            }
        )

    async def _dispatch_recorded(self, source, decision):
        # The native cursor can lag a completed/uncertain dispatch after a crash.
        # Search durable receipts before another executor call, including legacy
        # rejections which predate explicit source identity fields.
        after = source.sequence
        while True:
            receipts = await self.journal.read(
                after=after,
                session_id=source.session_id,
                kinds=["routing.dispatch"],
                limit=200,
            )
            if any(
                e.origin == "decisions"
                and e.thread_id == source.thread_id
                and e.epoch == source.epoch
                and source.event_id in e.causes
                and e.payload.get("decision_id") == decision.decision_id
                and (
                    e.payload.get("state") in {"accepted", "rejected", "unknown", "not_applied"}
                    or (e.payload.get("state") == "queued" and e.payload.get("executor") == "pi")
                )
                for e in receipts
            ):
                return True
            if len(receipts) < 200:
                return False
            after = receipts[-1].sequence

    async def _record_dispatch(self, source, decision, original, **receipt):
        await self.journal.append(
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="routing.dispatch",
                payload={
                    "decision_id": decision.decision_id,
                    "source_event_id": original.event_id,
                    "source_sequence": original.sequence,
                    "source_revision": decision.source_revision,
                    "route": decision.route,
                    "target_task_id": decision.target_task_id,
                    **receipt,
                },
                causes=[source.event_id],
                task_refs=[decision.target_task_id] if decision.target_task_id else [],
                idempotency_key=f"dispatch:{decision.decision_id}",
            )
        )

    async def process(self, source):
        with (
            self.model_settings.preparing("finalized")
            if self.model_settings is not None
            else nullcontext()
        ):
            try:
                result = await self._process(source)
            except (httpx.HTTPError, OSError, sqlite3.Error):
                # Repeated source-specific preparation/read failures must not
                # hold the ordered consumer forever. They are never charges.
                failures = self._preparation_failures.get(source.event_id, 0) + 1
                self._preparation_failures[source.event_id] = failures
                if failures < 3:
                    raise
                return await self._append_refusal(
                    source, "call_history_unavailable", "CaptureNativeUnavailable"
                )
            self._preparation_failures.pop(source.event_id, None)
            return result

    async def _persisted_call_state(self, source, *, scoped_source=None) -> PersistedCallState:
        try:
            state = await self.journal.decisions_attempt_lookup(
                DecisionsAttemptLookup(source=DecisionsSource.of(source))
            )
            observe_completion(state)
        except Exception:
            return PersistedCallState("unavailable", None)
        count, limit = state.charged_attempts, state.max_attempts
        if state.state == "legacy_uncertain":
            return PersistedCallState(
                "legacy_uncertain",
                count,
                refusal=("legacy_call_history_uncertain", "UncertainCallAttempt"),
                limit=limit,
            )
        if state.state in {"admitted", "uncertain"}:
            return PersistedCallState(
                "pending_uncertain",
                count,
                refusal=("call_outcome_uncertain", "UncertainCallAttempt"),
                limit=limit,
            )
        if state.state == "known_absent":
            return PersistedCallState("known_absent", 0, limit=limit)
        last = state.last_attempt
        if last is None or last.completion is None:
            return PersistedCallState("unavailable", count, limit=limit)
        completion = last.completion
        if state.state == "completed":
            admission = last.admission
            call = {
                **admission.source.model_dump(),
                "provider": admission.provider.provider,
                "model": admission.provider.model,
                "model_pin": admission.model_pin.model_dump(mode="json")
                if admission.model_pin
                else None,
                "request_format": admission.request_format,
                "raw_response": completion.raw_response,
                "validated_output": completion.validated_output,
            }
            scoped = (
                scoped_source if scoped_source is not None else await self._scoped_source(source)
            )
            refusal = self._call_binding_refusal(call, source, scoped, self._active_provider_info())
            if refusal:
                return PersistedCallState("refused", count, refusal=refusal, limit=limit)
            # Recovery retains the complete original disclosure set, including
            # context and task-source floors changed since provider entry.
            try:
                capture = DecisionCapture(self.journal, scoped, None, count)
                capture.disclosure_sources = []
                for binding in admission.disclosure_sources:
                    entry = await self.journal.entry(binding.source_event_id)
                    if DecisionsSource.of(entry) != binding:
                        entry = await self.journal.resolve(binding.source_event_id)
                    if DecisionsSource.of(entry) != binding:
                        raise ProviderRefusal("Recovered Decisions disclosure binding changed")
                    capture.disclosure_sources.append(entry)
                async with asyncio.timeout(0.5):
                    await capture.check_current(SimpleNamespace(info=admission.provider))
            except ProviderRefusal:
                return PersistedCallState(
                    "refused", count, refusal=("provider_refused", "ProviderRefusal"), limit=limit
                )
            except Exception:
                return PersistedCallState("unavailable", count, limit=limit)
            try:
                output = _recover_validated_output(call, source)
            except InvalidClassificationError as exc:
                return PersistedCallState(
                    "refused", count, refusal=(exc.reason_code, type(exc).__name__), limit=limit
                )
            return PersistedCallState("completed", count, output=output, limit=limit)
        if state.state == "refused":
            return PersistedCallState(
                "refused",
                count,
                refusal=(
                    completion.reason_code
                    or retained_refusal_reason(
                        completion.error_type,
                        provider=last.admission.provider.provider,
                        request_format=last.admission.request_format,
                    ),
                    completion.error_type or "ProviderRefusal",
                ),
                limit=limit,
            )
        if state.state == "exhausted":
            return PersistedCallState(
                "refused",
                count,
                refusal=("provider_failed", completion.error_type or "ProviderError"),
                limit=limit,
            )
        return PersistedCallState("retryable_error", count, limit=limit)

    async def _native_unavailable(self, source):
        failures = self._lookup_failures.get(source.event_id, 0) + 1
        self._lookup_failures[source.event_id] = failures
        if failures >= 2:
            return await self._append_refusal(
                source, "call_history_unavailable", "CaptureNativeUnavailable"
            )
        raise CaptureNativeUnavailable("Decisions native attempt history is unavailable")

    async def _append_refusal(self, source, reason: str, error_type: str) -> Decision | None:
        current = await self.journal.session(source.session_id)
        if (current.thread_id, current.epoch) != (source.thread_id, source.epoch):
            self._pending_refusals.pop(source.event_id, None)
            self._transport_attempts.pop(source.event_id, None)
            self._lookup_failures.pop(source.event_id, None)
            return
        self._pending_refusals[source.event_id] = (reason, error_type)
        # A truncated or invalid result is not an ignore decision.
        # Persist the refusal before advancing this consumer so an
        # unroutable entry cannot starve later messages/sessions.
        source_event_id = (
            source.payload.get("target_event_id")
            if source.kind == "conversation.revision"
            and isinstance(source.payload.get("target_event_id"), str)
            and source.payload.get("target_event_id")
            else source.event_id
        )
        published = await self.journal.append(
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="routing.classification_refused",
                payload={
                    "source_event_id": source_event_id,
                    "source_revision": source.payload.get("revision", 1),
                    "reason": reason,
                    "error_type": error_type,
                },
                causes=[source.event_id],
                idempotency_key=f"classification-refused:{source.event_id}",
            )
        )
        self._pending_refusals.pop(source.event_id, None)
        self._transport_attempts.pop(source.event_id, None)
        self._lookup_failures.pop(source.event_id, None)

        if published.kind == "routing.decided":
            # The original may have committed its decision before this refusal.
            # Reconcile that exact winner before allowing cursor advancement.
            target = self.external_agent.routing_target(source) if self.external_agent else None
            return await self._dispatch_decision(
                source, Decision.model_validate(published.payload), published, target
            )
        return None

    async def _run_classifier(
        self,
        classifier,
        provider,
        source,
        context,
        tasks,
        *,
        attempt=1,
        model_pin=None,
        current=None,
    ):
        admission = NativeDecisionAttempt(
            self.journal, source, expected_attempts=attempt - 1, max_attempts=self.max_attempts
        )
        with owned_http_stream_cleanup():
            async with decision_attempt(
                self.journal,
                source,
                provider,
                attempt=attempt,
                model_pin=model_pin,
                current=current,
                admission=admission,
            ) as capture:
                result = await classifier.classify(source, context, tasks)
                result = validate_classification_dict(result, source=source)
                capture.validated_output = validated_classification(source, result)
                return result

    async def _classify(self, source, context, tasks, *, attempt=1):
        if self.model_settings is None:
            return await self._run_classifier(
                self.classifier,
                getattr(self.classifier, "provider", None),
                source,
                context,
                tasks,
                attempt=attempt,
            )
        async with self.model_settings.operation() as lease:
            classifier = ModelClassifier(
                lease.provider,
                window_chars=self.classifier.window_chars,
                prompt=getattr(self.classifier, "prompt", None),
            )
            return await self._run_classifier(
                classifier,
                lease.provider,
                source,
                context,
                tasks,
                attempt=attempt,
                model_pin=lease.pin,
                current=lease.current,
            )

    async def _scoped_tasks(self, tasks):
        return await resolve_scoped_tasks(self.journal, tasks)

    async def _process(self, source):
        session = await self.journal.session(source.session_id)
        if source.epoch != session.epoch or session.channel != "converse":
            return None
        if source.kind not in {
            "conversation.user",
            "conversation.converse",
            "conversation.revision",
        }:
            return None
        if source.payload.get("window_source_id"):
            # Window replies carry no new owner authority; their paired native
            # window decision already owns routing, independently of this path.
            return None
        external_target = (
            self.external_agent.routing_target(source) if self.external_agent else None
        )
        after, recorded = source.sequence, None
        while True:
            previous = await self.journal.read(
                after=after,
                session_id=source.session_id,
                kinds=["routing.decided", "routing.classification_refused"],
                limit=200,
            )
            recorded = next((e for e in previous if source.event_id in e.causes), None)
            if recorded is not None or len(previous) < 200:
                break
            after = previous[-1].sequence
        if recorded and recorded.kind == "routing.classification_refused":
            self._pending_refusals.pop(source.event_id, None)
            self._pending_decisions.pop(source.event_id, None)
            self._transport_attempts.pop(source.event_id, None)
            self._lookup_failures.pop(source.event_id, None)
            return None
        if recorded:
            self._pending_refusals.pop(source.event_id, None)
            self._pending_decisions.pop(source.event_id, None)
            self._transport_attempts.pop(source.event_id, None)
            self._lookup_failures.pop(source.event_id, None)
            decision = Decision.model_validate(recorded.payload)
        else:
            if source.event_id in self._pending_refusals:
                reason, error_type = self._pending_refusals[source.event_id]
                return await self._append_refusal(source, reason, error_type)
            if source.event_id in self._pending_decisions:
                decision = self._pending_decisions[source.event_id]
            else:
                recent = (
                    await self.journal.recent(session_id=source.session_id, limit=100)
                    if hasattr(self.journal, "recent")
                    else await self.journal.read(session_id=source.session_id, limit=100)
                )
                context = [
                    e
                    for e in recent
                    if e.sequence < source.sequence
                    and e.epoch == source.epoch
                    and e.thread_id == source.thread_id
                    and e.kind in {"conversation.user", "conversation.converse"}
                ]
                context = context[-self.window_turns :]
                tasks = await self.agent.status() if self.enabled else []
                tasks = [
                    t
                    for t in tasks
                    if t.session_id == source.session_id
                    and t.epoch == source.epoch
                    and t.state not in {"succeeded", "failed", "cancelled"}
                ]
                tasks = await self._scoped_tasks(tasks)
                if not self.enabled and external_target is None:
                    result = {"route": "ignore", "reason_code": "agent_disabled_no_replay"}
                elif source.kind == "conversation.converse":
                    result = {"route": "ignore", "reason_code": "assistant_has_no_user_authority"}
                else:
                    scoped_source = await self._scoped_source(source)
                    persisted = await self._persisted_call_state(
                        source, scoped_source=scoped_source
                    )
                    if persisted.state == "unavailable":
                        return await self._native_unavailable(source)
                    if persisted.refusal is not None:
                        reason, error_type = persisted.refusal
                        return await self._append_refusal(source, reason, error_type)
                    if persisted.output is not None:
                        try:
                            result = validate_classification_dict(persisted.output, source=source)
                        except InvalidClassificationError as exc:
                            return await self._append_refusal(
                                source, exc.reason_code, type(exc).__name__
                            )
                    else:
                        prior_attempts = persisted.prior_attempts
                        limit = min(persisted.limit, self.max_attempts or persisted.limit)
                        if prior_attempts >= limit:
                            return await self._append_refusal(
                                source, "provider_failed", "ProviderError"
                            )
                        scoped_context = [await self._scoped_source(entry) for entry in context]
                        attempt = prior_attempts + 1
                        try:
                            result = await self._classify(
                                scoped_source,
                                scoped_context,
                                tasks,
                                attempt=attempt,
                            )
                            try:
                                result = validate_classification_dict(result, source=source)
                            except InvalidClassificationError as exc:
                                return await self._append_refusal(
                                    source, exc.reason_code, type(exc).__name__
                                )
                        except DecisionsModelUnavailable:
                            raise
                        except CaptureNativeUnavailable:
                            return await self._native_unavailable(source)
                        except InvalidClassificationError as exc:
                            return await self._append_refusal(
                                source, exc.reason_code, type(exc).__name__
                            )
                        except ProviderRefusal as exc:
                            return await self._append_refusal(
                                source, classification_refusal_reason(exc), type(exc).__name__
                            )
                        except Exception as exc:
                            is_budget_error = _is_budget_error(exc)
                            if is_budget_error or attempt >= limit:
                                return await self._append_refusal(
                                    source,
                                    "token_budget_exceeded"
                                    if is_budget_error
                                    else "provider_failed",
                                    type(exc).__name__,
                                )
                            raise
                        self._lookup_failures.pop(source.event_id, None)
                if source.payload.get("outcome_event_id"):
                    result = {"route": "ignore", "reason_code": "outcome_feedback_loop_prevented"}
                target = next((t for t in tasks if t.task_id == result.get("target_task_id")), None)
                if result["route"] in {"steer", "interrupt"} and (
                    not target
                    or target.objective_revision != result.get("expected_objective_revision")
                ):
                    result = {"route": "ignore", "reason_code": "unknown_or_stale_target"}
                elif result["route"] in {"steer", "interrupt"} and is_ambiguous_multi_task_target(
                    target, tasks, provider_info=self._active_provider_info()
                ):
                    result = {"route": "ignore", "reason_code": "ambiguous_target"}
                decision = Decision(
                    **result,
                    source_event_ids=[source.payload["target_event_id"]]
                    if source.kind == "conversation.revision"
                    else [source.event_id],
                    source_revision=source.payload.get("revision", 1),
                    instruction=source.payload.get("text", "")
                    if result["route"] in {"queue", "steer"}
                    else "",
                    idempotency_key=f"route:{source.event_id}:r{source.payload.get('revision', 1)}",
                )
                self._pending_decisions[source.event_id] = decision
            published = await self.journal.append(
                AppendRequest(
                    session_id=source.session_id,
                    thread_id=source.thread_id,
                    epoch=source.epoch,
                    kind="routing.decided",
                    payload=decision.model_dump(mode="json"),
                    causes=[source.event_id],
                    task_refs=[decision.target_task_id] if decision.target_task_id else [],
                    idempotency_key=decision.idempotency_key,
                )
            )
            self._pending_decisions.pop(source.event_id, None)
            if published.kind == "routing.classification_refused":
                return None
            decision = Decision.model_validate(published.payload)
            recorded = published
        return await self._dispatch_decision(source, decision, recorded, external_target)

    async def _dispatch_decision(self, source, decision, recorded, external_target):
        if decision.route == "ignore" or (not self.enabled and external_target is None):
            return decision
        if await self._dispatch_recorded(source, decision):
            return decision
        original = (
            source
            if source.event_id == decision.source_event_ids[0]
            else await self.journal.entry(decision.source_event_ids[0])
        )
        if not self.enabled:
            if decision.route == "queue":
                try:
                    receipt = await asyncio.wait_for(
                        self.external_agent.dispatch(
                            external_target,
                            source,
                            recorded,
                            key=decision.idempotency_key,
                            event_ids=decision.source_event_ids,
                            instruction=decision.instruction,
                            accepted_state="accepted",
                        ),
                        timeout=DISPATCH_TIMEOUT,
                    )
                except TimeoutError:
                    receipt = {
                        "state": "unknown",
                        "reason": "external_agent_dispatch_unconfirmed",
                        "executor": "pi",
                    }
                await self._record_dispatch(source, decision, original, **receipt)
            return decision
        try:
            task = await asyncio.wait_for(self.dispatch(source, decision), timeout=DISPATCH_TIMEOUT)
            acknowledgement = acknowledged_task(
                task,
                source,
                decision.target_task_id if decision.route != "queue" else None,
                submission_key=decision.idempotency_key if decision.route == "queue" else None,
            )
        except Exception as exc:
            # Unconfirmed effects are durable terminal receipts. Neither a
            # transport failure nor an invalid ACK proves non-application.
            await self._record_dispatch(source, decision, original, **dispatch_failure(exc))
        else:
            await self._record_dispatch(
                source,
                decision,
                original,
                state="accepted",
                reason="executor_accepted",
                **acknowledgement,
            )
        return decision

    async def dispatch(self, source, decision):
        if decision.route == "queue":
            return await self.agent.submit(
                TaskRequest(
                    session_id=source.session_id,
                    thread_id=source.thread_id,
                    epoch=source.epoch,
                    objective=decision.instruction,
                    content_scope=combine_scopes(source.payload.get("content_scope", "local_only")),
                    idempotency_key=decision.idempotency_key,
                    source_event_ids=decision.source_event_ids,
                    source_revision=decision.source_revision,
                ),
                actor="decisions",
            )
        elif decision.route == "steer":
            return await self.agent.steer(
                decision.target_task_id,
                SteerRequest(
                    instruction=decision.instruction,
                    content_scope=combine_scopes(source.payload.get("content_scope", "local_only")),
                    expected_objective_revision=decision.expected_objective_revision,
                    idempotency_key=decision.idempotency_key,
                    source_event_ids=decision.source_event_ids,
                ),
                actor="decisions",
            )
        else:
            return await self.agent.interrupt(
                decision.target_task_id,
                InterruptRequest(
                    expected_objective_revision=decision.expected_objective_revision,
                    idempotency_key=decision.idempotency_key,
                    reason=source.payload.get("text", ""),
                    source_event_ids=decision.source_event_ids,
                    source_revision=decision.source_revision,
                ),
                actor="decisions",
            )

    async def tick(self):
        cursor = await self.journal.cursor(self.consumer)
        entries = await self.journal.read(after=cursor, limit=100)
        for entry in entries:
            await self.process(entry)
            await self.journal.acknowledge(self.consumer, entry.sequence)
        return len(entries)

    async def run(self):
        self.running = True
        try:
            while self.running:
                try:
                    count = await self.tick()
                    self.ready = True
                    self.last_error = None
                    if not count:
                        await asyncio.sleep(0.05)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    self.ready = False
                    self.last_error = type(exc).__name__
                    await asyncio.sleep(1)
        finally:
            self.running = False
            self.ready = False


class OutcomeWorker:
    def __init__(self, journal, converse):
        self.journal, self.converse = journal, converse

    async def tick(self):
        cursor = await self.journal.cursor("converse-outcomes-v1")
        entries = await self.journal.read(after=cursor, limit=100)
        for entry in entries:
            if entry.kind == "agent.outcome" and entry.payload.get("state") == "succeeded":
                try:
                    await self.converse.outcome_notice(entry)
                except ValueError:
                    pass  # Stale/unevidenced outcomes never become speech.
            await self.journal.acknowledge("converse-outcomes-v1", entry.sequence)
        return len(entries)

    async def run(self):
        from packages.contracts.agent_outcome import OutcomeEvidenceUnavailable

        while True:
            try:
                if not await self.tick():
                    await asyncio.sleep(0.1)
            except OutcomeEvidenceUnavailable:
                # Cursor stays before the unavailable outcome. Retry only its
                # authenticated evidence reads; no announcement was attempted.
                await asyncio.sleep(1)
