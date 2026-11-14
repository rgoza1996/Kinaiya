"""Native charged attempt CAS. No inspection record can grant provider entry."""

from packages.contracts.decisions import encoded
from packages.contracts.decisions_attempts import (
    MAX_ATTEMPTS,
    DecisionsAttempt,
    DecisionsAttemptAdmit,
    DecisionsAttemptFinish,
    DecisionsAttemptLookup,
    DecisionsAttemptState,
    DecisionsSource,
    digest,
)
from packages.providers.disclosure import combine_scopes, scope_allowed


class DecisionsAttemptConflict(ValueError):
    pass


class DecisionsAttemptStore:
    def __init__(self, store, max_attempts=2):
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ValueError("decisions_max_attempts must be an integer from 1 to 16")
        self.store, self.max_attempts = store, max_attempts
        with store.lock, store.db as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS decisions_attempt_cutover(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1), through_sequence INTEGER NOT NULL);
                INSERT OR IGNORE INTO decisions_attempt_cutover
                  SELECT 1,coalesce(max(sequence),0) FROM entries;
                CREATE TABLE IF NOT EXISTS decisions_attempt_sources(
                  source_event_id TEXT PRIMARY KEY REFERENCES entries(event_id) ON DELETE CASCADE,
                  max_attempts INTEGER NOT NULL, charged_attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions_attempts(
                  attempt_id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL
                    REFERENCES decisions_attempt_sources(source_event_id) ON DELETE CASCADE,
                  attempt INTEGER NOT NULL, admission_digest TEXT NOT NULL,
                  completion_digest TEXT, value TEXT NOT NULL,
                  UNIQUE(source_event_id,attempt));
                CREATE TABLE IF NOT EXISTS decisions_attempt_outcomes(
                  source_event_id TEXT PRIMARY KEY REFERENCES entries(event_id) ON DELETE CASCADE,
                  event_id TEXT NOT NULL REFERENCES entries(event_id) ON DELETE CASCADE);
            """)

    def _source(self, binding):
        source = self.store.entry(binding.source_event_id)
        if DecisionsSource.of(source) != binding:
            raise DecisionsAttemptConflict("Decisions attempt source mismatch")
        if (
            source.kind not in {"conversation.user", "conversation.revision"}
            or source.origin != "authenticated-user"
        ):
            raise DecisionsAttemptConflict("Only native finalized user sources can admit attempts")
        return source

    def _state(self, source):
        db = self.store.db
        row = db.execute(
            "SELECT * FROM decisions_attempt_sources WHERE source_event_id=?", (source.event_id,)
        ).fetchone()
        limit, charged, last, state = self.max_attempts, 0, None, "known_absent"
        if row:
            limit, charged = row["max_attempts"], row["charged_attempts"]
            raw = db.execute(
                "SELECT value FROM decisions_attempts WHERE source_event_id=? AND attempt=?",
                (source.event_id, charged),
            ).fetchone()
            last = DecisionsAttempt.model_validate_json(raw[0])
            completion = last.completion
            state = (
                "admitted"
                if completion is None
                else {
                    "completed": "completed",
                    "refused": "refused",
                    "uncertain": "uncertain",
                    "error": "exhausted" if charged >= limit else "retryable_error",
                }[completion.status]
            )
        elif (
            source.sequence
            <= db.execute(
                "SELECT through_sequence FROM decisions_attempt_cutover WHERE singleton=1"
            ).fetchone()[0]
        ):
            # Older best-effort inspection cannot prove absence or fund replay.
            # Preserve it unchanged; do not manufacture admissions from it.
            state = "legacy_uncertain"
        return DecisionsAttemptState(
            source=DecisionsSource.of(source),
            state=state,
            max_attempts=limit,
            charged_attempts=charged,
            last_attempt=last,
        )

    def lookup(self, request):
        request = DecisionsAttemptLookup.model_validate(request)
        with self.store.lock:
            return self._state(self._source(request.source))

    def _current(self, request, source):
        session = self.store.session(source.session_id)
        if (session.thread_id, session.epoch, session.channel) != (
            source.thread_id,
            source.epoch,
            "converse",
        ):
            raise DecisionsAttemptConflict("Obsolete Decisions source")
        for binding in request.disclosure_sources:
            entry = self.store.entry(binding.source_event_id)
            if DecisionsSource.of(entry) != binding:
                entry = self.store.resolve(binding.source_event_id)
            if DecisionsSource.of(entry) != binding:
                raise DecisionsAttemptConflict("Decisions disclosure binding mismatch")
            original = (
                entry.payload.get("target_event_id")
                if entry.kind == "conversation.revision"
                else entry.event_id
            )
            resolved = self.store.resolve(original)
            if (
                binding == request.source
                and resolved.payload.get("revision", 1) != binding.source_revision
            ):
                raise DecisionsAttemptConflict("Decisions source revision changed")
            floor = self.store._agent_source_scope(self.store.entry(original), resolved)
            if not scope_allowed(
                request.provider,
                combine_scopes(entry.payload.get("content_scope", "local_only"), floor),
            ):
                raise DecisionsAttemptConflict("Decisions disclosure refused")

    def admit(self, request):
        request = DecisionsAttemptAdmit.model_validate(request)
        db = self.store.db
        with self.store.lock, db:
            db.execute("BEGIN IMMEDIATE")
            source = self._source(request.source)
            prior = db.execute(
                "SELECT * FROM decisions_attempts WHERE attempt_id=?", (request.attempt_id,)
            ).fetchone()
            fingerprint = digest(request.model_dump(mode="json"))
            if prior:
                if prior["admission_digest"] != fingerprint:
                    raise DecisionsAttemptConflict("Conflicting Decisions admission replay")
                return DecisionsAttempt.model_validate_json(prior["value"])
            self._current(request, source)
            if db.execute(
                "SELECT 1 FROM decisions_attempt_outcomes WHERE source_event_id=?",
                (source.event_id,),
            ).fetchone():
                raise DecisionsAttemptConflict("Decisions source already has a terminal outcome")
            state = self._state(source)
            if (
                state.state not in {"known_absent", "retryable_error"}
                or state.charged_attempts != request.expected_attempts
            ):
                raise DecisionsAttemptConflict("Decisions attempt cannot be admitted")
            if state.charged_attempts >= state.max_attempts:
                raise DecisionsAttemptConflict("Decisions attempts exhausted")
            count = state.charged_attempts + 1
            result = DecisionsAttempt(
                admission=request, admission_sha256=fingerprint, attempt=count, completion=None
            )
            db.execute(
                "INSERT INTO decisions_attempt_sources VALUES(?,?,?) ON CONFLICT(source_event_id) DO UPDATE SET charged_attempts=excluded.charged_attempts",
                (source.event_id, state.max_attempts, count),
            )
            db.execute(
                "INSERT INTO decisions_attempts VALUES(?,?,?,?,NULL,?)",
                (
                    request.attempt_id,
                    source.event_id,
                    count,
                    fingerprint,
                    encoded(result.model_dump(mode="json")).decode(),
                ),
            )
            return result

    def publish(self, request, role):
        """Serialize late original results against a published uncertainty refusal."""
        db = self.store.db
        with self.store.lock, db:
            db.execute("BEGIN IMMEDIATE")
            source = self.store.entry(request.causes[0])
            if (source.session_id, source.thread_id, source.epoch) != (
                request.session_id,
                request.thread_id,
                request.epoch,
            ):
                raise DecisionsAttemptConflict("Decisions outcome source mismatch")
            prior = db.execute(
                "SELECT event_id FROM decisions_attempt_outcomes WHERE source_event_id=?",
                (source.event_id,),
            ).fetchone()
            if prior:
                return self.store.entry(prior[0])
            result = self.store._append_entry_locked(request, role)
            db.execute(
                "INSERT INTO decisions_attempt_outcomes VALUES(?,?)",
                (source.event_id, result.event_id),
            )
            return result

    def finish(self, request):
        request = DecisionsAttemptFinish.model_validate(request)
        db = self.store.db
        with self.store.lock, db:
            db.execute("BEGIN IMMEDIATE")
            self._source(request.source)
            row = db.execute(
                "SELECT * FROM decisions_attempts WHERE attempt_id=?", (request.attempt_id,)
            ).fetchone()
            if row is None:
                raise DecisionsAttemptConflict("Unknown original Decisions admission")
            original = DecisionsAttempt.model_validate_json(row["value"])
            if (
                original.admission.source,
                original.admission.owner_id,
                original.admission_sha256,
            ) != (request.source, request.owner_id, request.admission_sha256):
                raise DecisionsAttemptConflict("Decisions completion lost its original owner")
            fingerprint = digest(request.model_dump(mode="json"))
            if row["completion_digest"] is not None:
                if row["completion_digest"] != fingerprint:
                    raise DecisionsAttemptConflict("Conflicting Decisions completion replay")
                return original
            result = original.model_copy(update={"completion": request})
            db.execute(
                "UPDATE decisions_attempts SET completion_digest=?,value=? WHERE attempt_id=?",
                (fingerprint, encoded(result.model_dump(mode="json")).decode(), request.attempt_id),
            )
            return result
