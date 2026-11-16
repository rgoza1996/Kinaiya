"""Explicit native live source ledger and sole exact result publisher."""

from contextvars import ContextVar

from packages.contracts.live_decisions_attempts import (
    OUTCOME_KIND,
    PROTOCOL,
    LiveDecisionsAttempt,
    LiveDecisionsAttemptAdmit,
    LiveDecisionsAttemptFinish,
    LiveDecisionsAttemptLookup,
    LiveDecisionsAttemptState,
    LiveDecisionsOutcome,
    LiveDecisionsPublication,
    LiveDecisionsPublicationState,
    LiveDecisionsSource,
)
from packages.contracts.live_decisions_pending import (
    LiveDecisionsPendingPage,
    LiveDecisionsPendingQuery,
)
from packages.contracts.live_windows import LiveWindowCapture
from packages.contracts.models import AppendRequest
from packages.contracts.stt_settlement import digest
from services.session.decisions_attempts import DecisionsAttemptConflict, DecisionsAttemptStore
from services.session.live_windows import _capture, _pair, _read, latest

_PUBLISH = ContextVar("native_live_decision_publication", default=None)


class LiveDecisionsAttemptStore(DecisionsAttemptStore):
    CONTRACT_VERSION = 2
    LOOKUP_TYPE = LiveDecisionsAttemptLookup
    ADMIT_TYPE = LiveDecisionsAttemptAdmit
    FINISH_TYPE = LiveDecisionsAttemptFinish
    ATTEMPT_TYPE = LiveDecisionsAttempt
    STATE_TYPE = LiveDecisionsAttemptState

    def pending(self, request):
        request = LiveDecisionsPendingQuery.model_validate(request, strict=True)
        with self.store.lock:
            controller = self.store.live_captures.backend().controller_id
            # Use the current per-utterance marker and its canonical eligible
            # sequence. Raw duplicates cannot move a source across this cursor.
            rows = self.store.db.execute(
                """SELECT
                    CASE WHEN typeof(e.event_id)='text' AND length(CAST(e.event_id AS BLOB))<=128
                         THEN e.event_id END,
                    CASE WHEN json_type(e.value,'$.payload.eligible_source_sequence')='integer'
                         THEN json_extract(e.value,'$.payload.eligible_source_sequence') END AS eligible_sequence
                FROM entries e JOIN sessions s ON s.session_id=e.session_id
                JOIN live_captures c
                  ON c.owner_instance=json_extract(e.value,'$.payload.original.owner_instance')
                 AND c.capture_id=json_extract(e.value,'$.payload.original.capture_id')
                WHERE e.kind='conversation.window_capture' AND e.writer='converse'
                  AND c.controller_id=? AND c.fence IS NULL AND c.receipt IS NOT NULL
                  AND c.session_id=e.session_id AND c.thread_id=e.thread_id AND c.epoch=e.epoch
                  AND e.epoch=json_extract(s.value,'$.epoch')
                  AND e.thread_id=json_extract(s.value,'$.thread_id')
                  AND json_extract(s.value,'$.channel')='converse'
                  AND e.sequence=(
                    SELECT max(n.sequence) FROM entries n
                    WHERE n.kind='conversation.window_capture' AND n.writer='converse'
                      AND n.session_id=e.session_id AND n.epoch=e.epoch
                      AND json_extract(n.value,'$.payload.original.utterance_id')=
                          json_extract(e.value,'$.payload.original.utterance_id')
                  )
                  AND json_extract(e.value,'$.payload.eligible_source_sequence')>?
                  AND NOT EXISTS (
                    SELECT 1 FROM decisions_attempt_outcomes o
                    WHERE o.source_event_id=json_extract(e.value,'$.payload.eligible_source_event_id')
                  )
                ORDER BY eligible_sequence LIMIT ?""",
                (controller, request.after_sequence, request.limit + 1),
            ).fetchall()
            sources = []
            for event_id, sequence in rows[: request.limit]:
                if type(event_id) is not str or type(sequence) is not int:
                    raise DecisionsAttemptConflict(
                        "Original pending source metadata is unavailable"
                    )
                capture = _read(self.store, "event_id=?", (event_id,))
                if capture is None:
                    raise DecisionsAttemptConflict("Original pending capture is unavailable")
                marker = LiveWindowCapture.model_validate(capture.payload, strict=True)
                original_capture = _capture(self.store, marker.eligible_source_event_id)
                if original_capture is None:
                    raise DecisionsAttemptConflict("Original eligible source is unavailable")
                reference = LiveWindowCapture.model_validate(
                    original_capture.payload, strict=True
                ).original
                pair = _pair(self.store, reference)
                if pair is None:
                    raise DecisionsAttemptConflict("Original pending source is unavailable")
                binding = self._binding(pair.source)
                if binding.source_sequence != sequence:
                    raise DecisionsAttemptConflict("Original pending source ordering changed")
                self.check_current(binding)
                sources.append(binding)
            return LiveDecisionsPendingPage(
                schema_version="2.0",
                protocol=PROTOCOL,
                original=request,
                sources=sources,
                through_sequence=sources[-1].source_sequence if sources else request.after_sequence,
                has_more=len(rows) > request.limit,
            )

    def _binding(self, source):
        annotation = _capture(self.store, source.event_id)
        if annotation is None:
            raise DecisionsAttemptConflict("Live attempt requires its native capture annotation")
        marker = LiveWindowCapture.model_validate(annotation.payload, strict=True)
        pair = _pair(self.store, marker.original)
        if pair is None or pair.source != source:
            raise DecisionsAttemptConflict("Original live source is unavailable")
        capture = self.store.live_captures._row(
            marker.original.owner_instance, marker.original.capture_id
        )
        if capture is None or capture[1] is None:
            raise DecisionsAttemptConflict("Original live capture registration is unavailable")
        return LiveDecisionsSource.of(pair, capture[1])

    def _source(self, binding):
        pair = _pair(self.store, binding.publication)
        if pair is None:
            raise DecisionsAttemptConflict("Original live source is unavailable")
        binding.check_entry(pair.source)
        if self._binding(pair.source) != binding:
            raise DecisionsAttemptConflict("Live attempt changed its original native binding")
        return pair.source

    def check_current(self, binding):
        permission = self.store.live_captures.source_state(binding.publication)
        if not permission.capture_permits_source or permission.capture.receipt != binding.capture:
            raise DecisionsAttemptConflict("Original live capture permission is no longer current")
        reference = binding.publication
        candidate = latest(
            self.store,
            reference.session_id,
            reference.thread_id,
            reference.epoch,
            reference.utterance_id,
        )
        if (
            candidate is None
            or LiveDecisionsSource.of(candidate.eligible, permission.capture.receipt) != binding
        ):
            raise DecisionsAttemptConflict("Original live eligibility is no longer current")

    def _current(self, request, source):
        self.check_current(request.source)
        super()._current(request, source)

    @staticmethod
    def _primary_revision(request, binding, resolved):
        # Native live eligibility deliberately survives identical raw same-slot
        # revisions. The complete canonical eligible pair is checked above;
        # resolved scope restrictions still apply to every disclosed source.
        pass

    def _publication_state(self, request):
        source = self._source(request.source)
        row = self.store.db.execute(
            "SELECT event_id FROM decisions_attempt_outcomes WHERE source_event_id=?",
            (source.event_id,),
        ).fetchone()
        receipt = None if row is None else self.store.entry(row[0])
        return LiveDecisionsPublicationState(
            schema_version="2.0", protocol=PROTOCOL, original=request, receipt=receipt
        )

    def lookup_publication(self, request):
        request = LiveDecisionsPublication.model_validate(request, strict=True)
        with self.store.lock:
            return self._publication_state(request)

    def publish_result(self, request):
        request = LiveDecisionsPublication.model_validate(request, strict=True)
        with self._transaction() as db:
            prior = self._publication_state(request)
            if prior.receipt is not None:
                return prior
            state = self._state(self._source(request.source))
            attempt = state.last_attempt
            if (
                attempt is None
                or attempt.completion is None
                or attempt.completion.status != "completed"
                or (
                    attempt.admission.attempt_id,
                    attempt.admission.owner_id,
                    attempt.admission_sha256,
                    digest(attempt.completion.model_dump(mode="json")),
                )
                != (
                    request.attempt_id,
                    request.owner_id,
                    request.admission_sha256,
                    request.completion_sha256,
                )
            ):
                raise DecisionsAttemptConflict("Original completed live result is unavailable")
            completion = attempt.completion
            decision = completion.validated_output
            source = self._source(request.source)
            self._current(attempt.admission, source)
            if decision.window_index != source.payload.get("window_index"):
                raise DecisionsAttemptConflict("Live result changed its original window index")
            outcome = LiveDecisionsOutcome(**request.model_dump(mode="python"), decision=decision)
            append = AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind=OUTCOME_KIND,
                payload=outcome.model_dump(mode="json"),
                causes=[source.event_id],
                task_refs=[decision.target_task_id] if decision.target_task_id else [],
                idempotency_key="live-window-decision:" + source.event_id,
            )
            token = _PUBLISH.set(append.model_dump(mode="json"))
            try:
                event = self.store._append_entry_locked(append, "decisions")
            finally:
                _PUBLISH.reset(token)
            db.execute(
                "INSERT INTO decisions_attempt_outcomes VALUES(?,?)",
                (source.event_id, event.event_id),
            )
            return LiveDecisionsPublicationState(
                schema_version="2.0", protocol=PROTOCOL, original=request, receipt=event
            )


def validate_live_outcome(request, role):
    if role != "decisions" or _PUBLISH.get() != request.model_dump(mode="json"):
        raise DecisionsAttemptConflict("Live outcomes require their original native publisher")
