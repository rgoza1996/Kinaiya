"""One native consumption owner per live outcome/effect; unknown never regrants."""

import secrets
from contextlib import contextmanager

from packages.contracts.live_decisions_attempts import OUTCOME_KIND, LiveDecisionsOutcome
from packages.contracts.live_effects import (
    MAX_ACTIVE,
    MAX_RECORDS,
    MAX_RESPONSE_BYTES,
    PROTOCOL,
    LiveEffectAdmit,
    LiveEffectFinish,
    LiveEffectQuery,
    LiveEffectReceipt,
    LiveEffectRecord,
    LiveEffectStart,
    LiveEffectStarted,
    LiveEffectState,
    effect_json,
    effect_key,
)
from packages.contracts.stt_settlement import encode
from services.session.decisions_attempts import DecisionsAttemptConflict
from services.session.store import JournalError


class LiveEffectConflict(JournalError):
    pass


def role_for(effect):
    return "decisions" if effect == "agent_route" else "converse"


class LiveEffectStore:
    def __init__(self, store):
        self.store = store

    def _present(self):
        return (
            self.store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_effects'"
            ).fetchone()
            is not None
        )

    @contextmanager
    def _transaction(self):
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise LiveEffectConflict("Another native journal transaction remains owned")
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not self._present():
                    db.execute(
                        "CREATE TABLE live_effects(outcome_id TEXT NOT NULL,effect TEXT NOT NULL,session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,record TEXT NOT NULL,PRIMARY KEY(outcome_id,effect))"
                    )
                yield db

    def _outcome(self, query):
        state = self.store.live_decisions_attempts.lookup_publication(query.publication)
        if state.receipt is None:
            raise LiveEffectConflict("Original native live outcome remains unconfirmed")
        return state.receipt, LiveDecisionsOutcome.model_validate(
            state.receipt.payload, strict=True
        )

    def _record(self, event_id, query):
        if not self._present():
            return None
        row = self.store.db.execute(
            "SELECT CASE WHEN typeof(record)='text' AND length(CAST(record AS BLOB))<=32768 THEN record END FROM live_effects WHERE outcome_id=? AND effect=?",
            (event_id, query.effect),
        ).fetchone()
        if row is None:
            return None
        raw = row[0].encode() if type(row[0]) is str else b""
        record = LiveEffectRecord.model_validate(effect_json(raw, MAX_RESPONSE_BYTES), strict=True)
        original = record.receipt.original
        if original.publication != query.publication or original.effect != query.effect:
            raise LiveEffectConflict("Stored live consumption changed its original outcome")
        return record

    def _current(self, query):
        try:
            self.store.live_decisions_attempts.check_current(query.publication.source)
            return True
        except DecisionsAttemptConflict:
            return False

    def lookup(self, query):
        query = LiveEffectQuery.model_validate(query, strict=True)
        with self.store.lock:
            entry, _ = self._outcome(query)
            return LiveEffectState(
                schema_version="2.0",
                protocol=PROTOCOL,
                original=query,
                record=self._record(entry.event_id, query),
                source_current=self._current(query),
            )

    @staticmethod
    def _authorize(request, role):
        if role != role_for(request.effect):
            raise LiveEffectConflict("Native consumer role cannot own this effect")

    @staticmethod
    def _allowed(request, decision):
        allowed = (
            request.effect == "converse_backchannel"
            and decision.conversation == "WAIT_WITH_BACKCHANNEL"
            or request.effect == "converse_reply"
            and decision.conversation
            in {"RESPOND_INTERRUPT_USER", "RESPOND_WITHOUT_INTERRUPTING_USER"}
            or request.effect == "agent_route"
            and decision.agent_route != "IGNORE_FOR_IMMEDIATE_ROUTING"
        )
        if not allowed:
            raise LiveEffectConflict("Original live decision does not select this consumer effect")

    def admit(self, request, role):
        request = LiveEffectAdmit.model_validate(request, strict=True)
        self._authorize(request, role)
        with self._transaction() as db:
            entry, outcome = self._outcome(request)
            self._allowed(request, outcome.decision)
            record = self._record(entry.event_id, request)
            if record is not None:
                if record.receipt.original != request:
                    raise LiveEffectConflict("Another original consumer already owns this effect")
                return record
            self.store.live_decisions_attempts.check_current(request.publication.source)
            if (
                db.execute("SELECT count(*) FROM live_effects").fetchone()[0] >= MAX_RECORDS
                or db.execute(
                    "SELECT count(*) FROM live_effects WHERE json_type(record,'$.completion')='null' OR json_extract(record,'$.completion.status')='uncertain'"
                ).fetchone()[0]
                >= MAX_ACTIVE
            ):
                raise LiveEffectConflict("Native consumer history or active capacity is retained")
            through = db.execute("SELECT coalesce(max(sequence),0) FROM entries").fetchone()[0]
            receipt = LiveEffectReceipt(
                schema_version="2.0",
                protocol=PROTOCOL,
                original=request,
                receipt_id=secrets.token_hex(16),
                through_sequence=through,
            )
            record = LiveEffectRecord(
                schema_version="2.0",
                protocol=PROTOCOL,
                receipt=receipt,
                started=None,
                completion=None,
            )
            encoded = encode(record.model_dump(mode="json"))
            if len(encoded.encode()) > MAX_RESPONSE_BYTES:
                raise LiveEffectConflict("Native consumer record exceeds its bound")
            db.execute(
                "INSERT INTO live_effects VALUES(?,?,?,?)",
                (entry.event_id, request.effect, entry.session_id, encoded),
            )
            return record

    def start(self, request, role):
        request = LiveEffectStart.model_validate(request, strict=True)
        original = request.original.original
        self._authorize(original, role)
        with self._transaction() as db:
            entry, outcome = self._outcome(original)
            record = self._record(entry.event_id, original)
            if (
                record is None
                or record.receipt != request.original
                or record.completion is not None
            ):
                raise LiveEffectConflict("Original uncompleted reservation is unavailable")
            if record.started is not None:
                if record.started.original != request:
                    raise LiveEffectConflict(
                        "Another executor already started this original effect"
                    )
                return record
            deferred_queue = (
                original.effect == "agent_route"
                and outcome.decision.agent_route == "QUEUE_UNTIL_TASK_DONE"
            )
            if deferred_queue:
                # Routing was accepted while capture permission was positive.
                # An Agent queue outlives the mic, but not changed user authority.
                source = self.store.live_decisions_attempts._source(original.publication.source)
                session = self.store.session(source.session_id)
                resolved = self.store.resolve(source.event_id)
                latest = self.store.latest_window(
                    source.session_id, source.epoch, source.payload["utterance_id"]
                )
                if (
                    (session.thread_id, session.epoch) != (source.thread_id, source.epoch)
                    or latest is None
                    or any(
                        latest.payload.get(key) != source.payload.get(key)
                        or resolved.payload.get(key) != source.payload.get(key)
                        for key in ("text", "content_scope", "stt_model")
                    )
                ):
                    raise LiveEffectConflict("Queued user authority is no longer current")
                # Use the actual native publication/source rows, rather than
                # an unqualified transcript label or outcome arrival order.
                interrupted = db.execute(
                    """SELECT 1 FROM decisions_attempt_outcomes o
                    JOIN entries e ON e.event_id=o.event_id
                    JOIN entries s ON s.event_id=o.source_event_id
                    WHERE e.kind=? AND e.writer='decisions'
                      AND e.session_id=? AND e.thread_id=? AND e.epoch=?
                      AND s.session_id=e.session_id AND s.thread_id=e.thread_id AND s.epoch=e.epoch
                      AND s.sequence>?
                      AND json_extract(e.value,'$.payload.decision.agent_route')='INTERRUPT_CURRENT_TASK'
                    LIMIT 1""",
                    (
                        OUTCOME_KIND,
                        source.session_id,
                        source.thread_id,
                        source.epoch,
                        source.sequence,
                    ),
                ).fetchone()
                if interrupted is not None:
                    raise LiveEffectConflict("A later native interrupt fenced this queued route")
            else:
                self.store.live_decisions_attempts.check_current(original.publication.source)
            through = db.execute("SELECT coalesce(max(sequence),0) FROM entries").fetchone()[0]
            started = LiveEffectStarted(
                schema_version="2.0",
                protocol=PROTOCOL,
                original=request,
                started_id=secrets.token_hex(16),
                through_sequence=through,
            )
            result = record.model_copy(update={"started": started})
            encoded = encode(result.model_dump(mode="json"))
            if len(encoded.encode()) > MAX_RESPONSE_BYTES:
                raise LiveEffectConflict("Native consumer start exceeds its bound")
            db.execute(
                "UPDATE live_effects SET record=? WHERE outcome_id=? AND effect=?",
                (encoded, entry.event_id, original.effect),
            )
            return result

    def finish(self, request, role):
        request = LiveEffectFinish.model_validate(request, strict=True)
        original = request.original.original
        self._authorize(original, role)
        with self._transaction() as db:
            entry, outcome = self._outcome(original)
            record = self._record(entry.event_id, original)
            if record is None or record.receipt != request.original:
                raise LiveEffectConflict("Original native consumer receipt is unavailable")
            if request.status in {"recorded", "uncertain"} and record.started is None:
                raise LiveEffectConflict("Original effect has no native start marker")
            if record.completion is not None:
                if record.completion == request:
                    return record
                if record.completion.status != "uncertain" or request.status != "recorded":
                    raise LiveEffectConflict("Original consumer completion cannot be replaced")
            if request.evidence is not None:
                evidence = self.store.entry(request.evidence.event_id)
                source = original.publication.source.original
                if (
                    evidence.sequence != request.evidence.sequence
                    or evidence.sequence <= record.started.through_sequence
                    or (evidence.session_id, evidence.thread_id, evidence.epoch)
                    != (source.session_id, source.thread_id, source.epoch)
                ):
                    raise LiveEffectConflict(
                        "Consumer evidence changed its original scope or boundary"
                    )
                if original.effect == "agent_route":
                    expected_op = {
                        "STEER_CURRENT_TASK": "steer",
                        "INTERRUPT_CURRENT_TASK": "interrupt",
                        "QUEUE_UNTIL_TASK_DONE": "submit",
                    }.get(outcome.decision.agent_route)
                    valid = (
                        evidence.kind == "agent.command"
                        and evidence.origin == "agent"
                        and evidence.payload.get("actor") == "decisions"
                        and evidence.payload.get("source_event_ids") == [source.source_event_id]
                        and evidence.payload.get("op") == expected_op
                        and evidence.payload.get("idempotency_key") == effect_key(record.receipt)
                    )
                    if outcome.decision.target_task_id is not None and expected_op != "submit":
                        valid = (
                            valid
                            and evidence.payload.get("task_id") == outcome.decision.target_task_id
                            and evidence.payload.get("expected_objective_revision")
                            == outcome.decision.expected_objective_revision
                        )
                else:
                    valid = (
                        evidence.kind == "conversation.converse"
                        and evidence.origin == "converse"
                        and evidence.causes == [source.source_event_id]
                        and evidence.payload.get("window_source_id") == source.source_event_id
                        and evidence.payload.get("window_utterance_id")
                        == original.publication.source.publication.utterance_id
                        and evidence.payload.get("window_behavior") == outcome.decision.conversation
                        and evidence.payload.get("final") is True
                        and evidence.idempotency_key == effect_key(record.receipt) + ":0"
                    )
                if not valid:
                    raise LiveEffectConflict(
                        "Consumer evidence has no authenticated original action"
                    )
            result = record.model_copy(update={"completion": request})
            encoded = encode(result.model_dump(mode="json"))
            if len(encoded.encode()) > MAX_RESPONSE_BYTES:
                raise LiveEffectConflict("Native consumer completion exceeds its bound")
            db.execute(
                "UPDATE live_effects SET record=? WHERE outcome_id=? AND effect=?",
                (encoded, entry.event_id, original.effect),
            )
            return result
