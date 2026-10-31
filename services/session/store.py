"""The sole SQLite journal owner. Transactions establish sequence and provenance."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from packages.contracts.context import (
    MAX_CONTEXT_PAGE_BYTES,
    ContextHistoryPage,
    ContextHistoryRequest,
    ContextQualificationPage,
    ContextQualificationRequest,
)
from packages.contracts.models import (
    MAX_PLAYBACK_GENERATION,
    AppendRequest,
    JournalEntry,
    PlaybackGeneration,
    Session,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)

ALLOWED_KINDS = {
    "owner": {
        "conversation.user",
        "conversation.window",
        "conversation.revision",
        "delivery.updated",
    },
    "converse": {
        "conversation.user",
        "conversation.window",
        "conversation.converse",
        "conversation.revision",
        "delivery.updated",
        "converse.safety",
        "converse.window_state",
    },
    "agent": {
        "agent.command",
        "agent.retirement",
        "agent.outcome",
        "agent.tool",
        "plugin.pinned",
        "agent.state",
        "approval.requested",
        "approval.resolved",
        "artifact.updated",
    },
    "decisions": {
        "routing.decided",
        "routing.classification_refused",
        "routing.dispatch",
        "routing.window_decided",
        "routing.window_dispatch",
        "routing.window_status",
    },
    "resources": {"resource.changed"},
}


class JournalError(ValueError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.lock = threading.RLock()
        self._converse_context_validators = {}
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
            INSERT INTO schema_meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_meta);
            CREATE TABLE IF NOT EXISTS sessions(
                session_id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                thread_id TEXT NOT NULL, epoch INTEGER NOT NULL, kind TEXT NOT NULL,
                writer TEXT NOT NULL, idem TEXT NOT NULL, request TEXT NOT NULL,
                value TEXT NOT NULL, UNIQUE(session_id,writer,idem));
            CREATE INDEX IF NOT EXISTS entries_thread ON entries(thread_id,sequence);
            CREATE INDEX IF NOT EXISTS entries_session ON entries(session_id,sequence);
            CREATE INDEX IF NOT EXISTS entries_context ON entries(
                session_id, thread_id, epoch, kind, sequence);
            CREATE INDEX IF NOT EXISTS entries_context_base ON entries(
                session_id, thread_id, epoch, sequence)
                WHERE kind IN ('conversation.user','conversation.window','conversation.converse');
            CREATE INDEX IF NOT EXISTS entries_context_window ON entries(
                session_id, thread_id, epoch,
                CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.utterance_id') END,
                CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.window_index') END,
                CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.revision') END, sequence)
                WHERE kind='conversation.window';
            CREATE INDEX IF NOT EXISTS entries_context_revision ON entries(
                session_id, thread_id, epoch,
                CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.target_event_id') END, sequence)
                WHERE kind='conversation.revision';
            CREATE INDEX IF NOT EXISTS entries_context_receipt ON entries(
                session_id, thread_id, epoch,
                CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.event_id') END, sequence)
                WHERE kind='delivery.updated';
            CREATE INDEX IF NOT EXISTS entries_context_stop ON entries(
                session_id, thread_id, epoch,
                CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.runtime_id') END,
                CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.next_generation_epoch') END, sequence)
                WHERE kind='delivery.updated'
                AND CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.control') END='generation_stop';
            CREATE INDEX IF NOT EXISTS entries_window_dispatch ON entries(
                json_extract(value,'$.payload.decision_id'), sequence)
                WHERE kind='routing.window_dispatch';
            CREATE INDEX IF NOT EXISTS entries_window_semantic ON entries(
                session_id, json_extract(value,'$.payload.semantic_key'), sequence)
                WHERE kind='routing.window_dispatch';
            CREATE INDEX IF NOT EXISTS entries_window_decision_source ON entries(
                json_extract(value,'$.payload.source_event_id'), sequence)
                WHERE kind='routing.window_decided';
            CREATE INDEX IF NOT EXISTS entries_window_status_source ON entries(
                json_extract(value,'$.payload.source_event_id'),
                json_extract(value,'$.payload.phase'), sequence)
                WHERE kind='routing.window_status';
            CREATE INDEX IF NOT EXISTS entries_window_backlog ON entries(session_id, sequence)
                WHERE kind='conversation.window';
            CREATE TABLE IF NOT EXISTS cursors(
                writer TEXT NOT NULL, consumer TEXT NOT NULL, sequence INTEGER NOT NULL,
                PRIMARY KEY(writer,consumer));
        """)
        if self.db.execute("SELECT version FROM schema_meta").fetchone()[0] != 1:
            raise JournalError(
                "Unsupported database version; preserve the database before migration"
            )
        self.db.commit()

    def register_converse_context_validator(self, implementation_id, callback):
        """Trusted startup only; registration never imports a memory backend."""
        import inspect

        from pydantic import TypeAdapter

        from packages.contracts.memory import Identifier

        try:
            name = TypeAdapter(Identifier).validate_python(implementation_id)
        except ValueError:
            raise ValueError("Invalid Converse context validator name") from None
        if (
            not callable(callback)
            or inspect.iscoroutinefunction(callback)
            or inspect.iscoroutinefunction(type(callback).__call__)
        ):
            raise ValueError("Converse context validator must be synchronous")
        with self.lock:
            if (
                name in self._converse_context_validators
                or len(self._converse_context_validators) >= 4
            ):
                raise ValueError("Converse context validator registration is unavailable")
            self._converse_context_validators[name] = callback

    @staticmethod
    def _memory_context_pin(request, role):
        if "memory_context" not in request.payload:
            return None
        from pydantic import TypeAdapter

        from packages.contracts.memory import Identifier
        from packages.contracts.memory_context import MemoryContextPin

        try:
            raw = json.dumps(
                request.payload["memory_context"],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if (
                len(raw) > 2048
                or role != "converse"
                or request.kind != "conversation.converse"
                or request.payload.get("content_scope") != "local_only"
                or request.payload.get("final") is not True
                or not isinstance(request.payload.get("text"), str)
            ):
                raise ValueError
            TypeAdapter(Identifier).validate_python(request.payload.get("runtime_id"))
            return MemoryContextPin.model_validate(request.payload["memory_context"])
        except (ValueError, TypeError, RecursionError):
            raise JournalError("Invalid Converse memory context provenance") from None

    def create_session(self, request: SessionRequest) -> Session:
        session = Session(
            channel=request.channel, name=request.name, harness_version=request.harness_version
        )
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?)", (session.session_id, session.model_dump_json())
            )
        return session

    def session(self, session_id: str) -> Session:
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if not row:
            raise KeyError(session_id)
        return Session.model_validate_json(row[0])

    def sessions(self) -> list[Session]:
        with self.lock:
            return [
                Session.model_validate_json(row[0])
                for row in self.db.execute(
                    "SELECT value FROM sessions ORDER BY rowid DESC LIMIT 200"
                )
            ]

    def sessions_page(self, **kwargs):
        from packages.contracts.agent_workspace import AgentSessionPage, AgentSessionPageRequest

        request = AgentSessionPageRequest(**kwargs)
        clauses = ["json_extract(value, '$.channel') = ?"]
        args = ["agent"]
        if request.harness_version == "unassociated":
            clauses.append("json_extract(value, '$.harness_version') IS NULL")
        elif request.harness_version is not None:
            clauses.append("json_extract(value, '$.harness_version') = ?")
            args.append(request.harness_version)
        if request.after is not None:
            cursor = int(request.after)
            if cursor > 9_223_372_036_854_775_807:
                raise ValueError("Invalid session cursor")
            clauses.append("rowid < ?")
            args.append(cursor)
        with self.lock:
            rows = self.db.execute(
                "SELECT rowid, value FROM sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY rowid DESC LIMIT ?",
                (*args, request.limit + 1),
            ).fetchall()
        return AgentSessionPage(
            items=[Session.model_validate_json(row[1]) for row in rows[: request.limit]],
            next_cursor=str(rows[request.limit - 1][0]) if len(rows) > request.limit else None,
        ).validate_request(request)

    def playback_generation(self, session_id, thread_id, epoch) -> PlaybackGeneration:
        """Recover native allocation metadata, independent of conversational context.

        The existing session index bounds the scope, not the amount of retained
        metadata work. Stream the selected rows; never return transcript bodies.
        """
        if (
            not isinstance(session_id, str)
            or not 1 <= len(session_id) <= 128
            or not isinstance(thread_id, str)
            or not 1 <= len(thread_id) <= 128
            or type(epoch) is not int
            or epoch < 1
        ):
            raise JournalError("Invalid playback-generation scope")
        with self.lock:
            session = self.session(session_id)
            if session.channel != "converse" or (session.thread_id, session.epoch) != (
                thread_id,
                epoch,
            ):
                raise JournalError("Stale or wrong playback-generation scope")
            maximum = 0
            rows = None
            try:
                rows = self.db.execute(
                    """SELECT kind,
                        CASE WHEN
                            json_type(value,'$.payload')='object'
                            AND json_type(value,'$.event_id')='text'
                            AND json_extract(value,'$.event_id')=event_id
                            AND json_type(value,'$.sequence')='integer'
                            AND json_extract(value,'$.sequence')=sequence
                            AND json_type(value,'$.session_id')='text'
                            AND json_extract(value,'$.session_id')=session_id
                            AND json_type(value,'$.thread_id')='text'
                            AND json_extract(value,'$.thread_id')=thread_id
                            AND json_type(value,'$.epoch')='integer'
                            AND json_extract(value,'$.epoch')=epoch
                            AND json_extract(value,'$.kind')=kind
                            AND json_extract(value,'$.origin')=CASE
                                WHEN kind='conversation.user' THEN 'authenticated-user'
                                ELSE 'converse' END
                        THEN 1 ELSE 0 END AS identity_matches,
                        json_type(value,'$.payload.generation_epoch') AS generation_type,
                        json_extract(value,'$.payload.generation_epoch') AS generation,
                        CASE WHEN kind='delivery.updated' THEN
                            json_type(value,'$.payload.next_generation_epoch') END AS next_type,
                        CASE WHEN kind='delivery.updated' THEN
                            json_extract(value,'$.payload.next_generation_epoch') END AS next_generation
                        FROM entries WHERE session_id=? AND thread_id=? AND epoch=?
                        AND writer='converse' AND kind IN (
                            'conversation.user','conversation.converse','converse.safety',
                            'converse.window_state','delivery.updated')""",
                    (session_id, thread_id, epoch),
                )
                for row in rows:
                    if not row["identity_matches"]:
                        raise JournalError("Native playback metadata identity is corrupt")
                    for type_key, value_key in (
                        ("generation_type", "generation"),
                        ("next_type", "next_generation"),
                    ):
                        field_type, counter = row[type_key], row[value_key]
                        if field_type in {None, "null"}:
                            continue
                        if (
                            field_type != "integer"
                            or type(counter) is not int
                            or not 0 <= counter <= MAX_PLAYBACK_GENERATION
                        ):
                            raise JournalError(
                                "Native playback counter is corrupt or unrepresentable"
                            )
                        maximum = max(maximum, counter)
            except sqlite3.Error as exc:
                raise JournalError("Native playback metadata read failed") from exc
            finally:
                if rows is not None:
                    rows.close()
            current = self.session(session_id)
            if current.channel != "converse" or (current.thread_id, current.epoch) != (
                thread_id,
                epoch,
            ):
                raise JournalError("Session changed during playback-generation read")
            return PlaybackGeneration(
                session_id=session_id,
                thread_id=thread_id,
                epoch=epoch,
                maximum_generation=maximum,
            )

    def reset(self, session_id: str) -> Session:
        with self.lock, self.db:
            session = self.session(session_id)
            session.epoch += 1
            self.db.execute(
                "UPDATE sessions SET value=? WHERE session_id=?",
                (session.model_dump_json(), session_id),
            )
        return session

    def delete(self, session_id: str):
        with self.lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    def entry(self, event_id: str) -> JournalEntry:
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM entries WHERE event_id=?", (event_id,)
            ).fetchone()
        if not row:
            raise KeyError(event_id)
        return JournalEntry.model_validate_json(row[0])

    def append(self, request: AppendRequest, role: str) -> JournalEntry:
        if request.kind not in ALLOWED_KINDS.get(role, set()):
            raise PermissionError("Writer cannot publish this event kind")
        pin = self._memory_context_pin(request, role)
        encoded = request.model_dump_json()
        if len(encoded.encode()) > 65536:
            raise JournalError("Entry exceeds 64 KiB; store large artifacts by reference")
        with self.lock, self.db:
            session = self.session(request.session_id)
            if (request.epoch, request.thread_id) != (session.epoch, session.thread_id):
                raise JournalError("Stale epoch or wrong session thread")
            if pin is not None and session.channel != "converse":
                raise JournalError("Memory context requires a Converse session")
            if request.kind == "conversation.window":
                try:
                    window = SpeechWindow.model_validate(request.payload)
                except ValueError as exc:
                    raise JournalError("Invalid transcript-window payload") from exc
                if session.channel != "converse" or (
                    window.session_id,
                    window.thread_id,
                    window.epoch,
                ) != (request.session_id, request.thread_id, request.epoch):
                    raise JournalError("Wrong transcript-window scope")
                previous = self._window_row(
                    window.session_id, window.epoch, window.utterance_id, window.window_index
                )
                if previous is not None:
                    prior = SpeechWindow.model_validate(previous.payload)
                    if window.revision == prior.revision and window == prior:
                        return previous
                    if window.revision != prior.revision + 1:
                        raise JournalError("Window revision must increase by one")
                elif window.revision != 1:
                    raise JournalError("The first window revision must be one")
            elif request.kind == "routing.window_decided":
                try:
                    decision = WindowDecision.model_validate(request.payload)
                except ValueError as exc:
                    raise JournalError("Invalid window decision payload") from exc
                source = self.resolve(decision.source_event_id)
                if (
                    source.kind != "conversation.window"
                    or source.origin != "authenticated-user"
                    or (
                        source.session_id,
                        source.thread_id,
                        source.epoch,
                        source.sequence,
                        source.payload.get("revision"),
                        request.causes,
                    )
                    != (
                        request.session_id,
                        request.thread_id,
                        request.epoch,
                        decision.source_sequence,
                        decision.source_revision,
                        [decision.source_event_id],
                    )
                ):
                    raise JournalError("Window decision does not bind an authenticated source")
            elif request.kind == "routing.window_status":
                payload = request.payload
                source_id = payload.get("source_event_id")
                if (
                    not isinstance(source_id, str)
                    or not 1 <= len(source_id) <= 128
                    or payload.get("phase")
                    not in {
                        "classifying",
                        "retrying",
                        "classified",
                        "busy",
                        "error",
                        "superseded",
                        "expired",
                        "obsolete",
                        "agent_unavailable",
                    }
                    or not isinstance(payload.get("reason"), str)
                    or not 1 <= len(payload["reason"]) <= 120
                    or (
                        "attempt" in payload
                        and (
                            type(payload["attempt"]) is not int or payload["attempt"] not in (1, 2)
                        )
                    )
                ):
                    raise JournalError("Invalid window status payload")
                source = self.entry(source_id)
                if (
                    source.kind != "conversation.window"
                    or source.origin != "authenticated-user"
                    or (source.session_id, source.thread_id, source.epoch, [source.event_id])
                    != (request.session_id, request.thread_id, request.epoch, request.causes)
                    or (
                        "source_sequence" in payload
                        and (
                            type(payload["source_sequence"]) is not int
                            or payload["source_sequence"] != source.sequence
                        )
                    )
                    or (
                        "source_revision" in payload
                        and (
                            type(payload["source_revision"]) is not int
                            or payload["source_revision"] != source.payload.get("revision", 1)
                        )
                    )
                ):
                    raise JournalError("Window status does not bind an authenticated source")
            row = self.db.execute(
                "SELECT request,value FROM entries WHERE session_id=? AND writer=? AND idem=?",
                (request.session_id, role, request.idempotency_key),
            ).fetchone()
            if row:
                if json.loads(row[0]) != json.loads(encoded):
                    raise JournalError("Idempotency key reused with different content")
                return JournalEntry.model_validate_json(row[1])
            for event_id in request.causes:
                source = self.entry(event_id)
                if source.session_id != request.session_id or source.epoch != request.epoch:
                    raise JournalError("Causal reference outside current session epoch")
            if request.kind in {"conversation.user", "conversation.converse"}:
                if not isinstance(request.payload.get("text"), str):
                    raise JournalError("Conversation requires text")
                if request.payload.get("final") is not True:
                    raise JournalError("Only finalized conversation enters the routing journal")
            if request.kind == "conversation.revision":
                target = self.entry(request.payload.get("target_event_id", ""))
                if target.kind != "conversation.user" or target.session_id != request.session_id:
                    raise JournalError("Revision must reference a user message in this session")
                if target.epoch != request.epoch or not isinstance(
                    request.payload.get("text"), str
                ):
                    raise JournalError("Invalid revision")
                current = self.resolve(target.event_id)
                if request.payload.get("revision") != current.payload.get("revision", 1) + 1:
                    raise JournalError("Revision must increase by one")
            origin = {"owner": "authenticated-user", "agent": "agent-supervisor"}.get(role, role)
            if request.kind in {"conversation.user", "conversation.window"}:
                origin = "authenticated-user"
            event = JournalEntry(**request.model_dump(), sequence=1, origin=origin)
            if pin is not None:
                validator = self._converse_context_validators.get(pin.implementation_id)
                try:
                    admitted = (
                        validator is not None
                        and validator(request.session_id, request.thread_id, request.epoch, pin)
                        is True
                    )
                except Exception:
                    raise JournalError("Converse memory context is unavailable") from None
                if not admitted:
                    raise JournalError("Converse memory context is unavailable")
                # No append DML precedes this callback. Reconciliation may
                # commit source invalidation/quarantine even when it refuses
                # publication, without committing a partial journal entry.
            cursor = self.db.execute(
                "INSERT INTO entries(event_id,session_id,thread_id,epoch,kind,writer,idem,request,value) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.session_id,
                    event.thread_id,
                    event.epoch,
                    event.kind,
                    role,
                    event.idempotency_key,
                    encoded,
                    "{}",
                ),
            )
            event.sequence = cursor.lastrowid
            self.db.execute(
                "UPDATE entries SET value=? WHERE event_id=?",
                (event.model_dump_json(), event.event_id),
            )
        return event

    def resolve(self, event_id: str) -> JournalEntry:
        event = self.entry(event_id)
        if event.kind == "conversation.window":
            window = SpeechWindow.model_validate(event.payload)
            with self.lock:
                latest = self._window_row(
                    window.session_id, window.epoch, window.utterance_id, window.window_index
                )
            return latest or event
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM entries WHERE session_id=? AND kind='conversation.revision' "
                "AND json_extract(value,'$.payload.target_event_id')=? ORDER BY sequence DESC LIMIT 1",
                (event.session_id, event_id),
            ).fetchone()
        if row:
            revision = JournalEntry.model_validate_json(row[0])
            event = event.model_copy(update={"payload": {**event.payload, **revision.payload}})
        return event

    def _window_row(self, session_id, epoch, utterance_id, window_index=None):
        query = "SELECT value FROM entries WHERE session_id=? AND epoch=? AND kind='conversation.window' AND json_extract(value,'$.payload.utterance_id')=?"
        args = [session_id, epoch, utterance_id]
        if window_index is not None:
            query += " AND json_extract(value,'$.payload.window_index')=?"
            args.append(window_index)
        query += " ORDER BY json_extract(value,'$.payload.window_index') DESC, json_extract(value,'$.payload.revision') DESC, sequence DESC LIMIT 1"
        row = self.db.execute(query, args).fetchone()
        return JournalEntry.model_validate_json(row[0]) if row else None

    def latest_window(self, session_id, epoch, utterance_id, window_index=None):
        with self.lock:
            session = self.session(session_id)
            if session.channel != "converse" or session.epoch != epoch:
                raise JournalError("Stale transcript-window scope")
            return self._window_row(session_id, epoch, utterance_id, window_index)

    def window_decision(self, source_event_id):
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM entries WHERE kind='routing.window_decided' AND json_extract(value,'$.payload.source_event_id')=? ORDER BY sequence LIMIT 1",
                (source_event_id,),
            ).fetchone()
        return JournalEntry.model_validate_json(row[0]) if row else None

    def pending_windows(self, *, limit=100):
        """Bounded fair admission from durable, undisposed transcript entries.

        Busy/retrying/classifying receipts do not retire work. Epoch reset is
        already a durable native fence; old epochs cannot accept new receipts.
        Rank within sessions so one session's backlog cannot fill every page.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise JournalError("Invalid pending-window query bound")
        with self.lock:
            # Unary + removes the column's TEXT affinity without changing the
            # string value. SQLite can then SEARCH the JSON expression indexes
            # instead of scanning every receipt for each candidate source.
            rows = self.db.execute(
                """WITH pending AS (
                    SELECT e.sequence,
                        ROW_NUMBER() OVER (
                            PARTITION BY e.session_id ORDER BY e.sequence
                        ) AS session_position
                    FROM entries e JOIN sessions s ON s.session_id=e.session_id
                    WHERE e.kind='conversation.window'
                      AND e.epoch=json_extract(s.value,'$.epoch')
                      AND json_extract(s.value,'$.channel')='converse'
                      AND NOT EXISTS (
                          SELECT 1 FROM entries d WHERE d.kind='routing.window_decided'
                          AND json_extract(d.value,'$.payload.source_event_id')=+e.event_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM entries d WHERE d.kind='routing.window_status'
                          AND json_extract(d.value,'$.payload.source_event_id')=+e.event_id
                          AND json_extract(d.value,'$.payload.phase')
                              IN ('superseded','expired','obsolete','error')
                      )
                ), admitted AS (
                    SELECT sequence, session_position FROM pending
                    ORDER BY session_position, sequence LIMIT ?
                ) SELECT e.value FROM admitted a JOIN entries e ON e.sequence=a.sequence
                  ORDER BY a.session_position, a.sequence""",
                (limit,),
            ).fetchall()
        return [JournalEntry.model_validate_json(row[0]) for row in rows]

    def window_classification_attempts(self, source_event_id):
        if not isinstance(source_event_id, str) or not 1 <= len(source_event_id) <= 128:
            raise JournalError("Invalid window source reference")
        with self.lock:
            rows = self.db.execute(
                """SELECT json_extract(value,'$.payload.attempt') AS attempt,
                          json_type(value,'$.payload.attempt') AS attempt_type
                   FROM entries WHERE kind='routing.window_status'
                   AND json_extract(value,'$.payload.source_event_id')=?
                   AND json_extract(value,'$.payload.phase')='classifying' LIMIT 2""",
                (source_event_id,),
            ).fetchall()
        attempts = [row["attempt"] for row in rows]
        # Legacy code reused one unnumbered receipt across both retries. It
        # cannot prove how many calls started, so do not admit a third call.
        if any(row["attempt_type"] != "integer" for row in rows) or any(
            type(attempt) is not int or attempt not in (1, 2) for attempt in attempts
        ):
            return 2
        return max(attempts, default=0)

    def window_response(self, session_id, epoch, utterance_id):
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM entries WHERE session_id=? AND epoch=? AND kind='converse.window_state' AND json_extract(value,'$.payload.utterance_id')=? AND json_extract(value,'$.payload.phase')='started' ORDER BY sequence LIMIT 1",
                (session_id, epoch, utterance_id),
            ).fetchone()
        return JournalEntry.model_validate_json(row[0]) if row else None

    def window_control(self, session_id, epoch, agent_interrupt=False):
        condition = (
            "json_extract(value,'$.payload.agent_route')='INTERRUPT_CURRENT_TASK'"
            if agent_interrupt
            else "json_extract(value,'$.payload.conversation')!='IGNORE'"
        )
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM entries WHERE session_id=? AND epoch=? AND kind='routing.window_decided' AND "
                + condition
                + " ORDER BY json_extract(value,'$.payload.source_sequence') DESC,sequence DESC LIMIT 1",
                (session_id, epoch),
            ).fetchone()
        return JournalEntry.model_validate_json(row[0]) if row else None

    def window_dispatches(
        self,
        *,
        session_id=None,
        decision_id=None,
        semantic_key=None,
        pending_only=False,
        interrupt_only=False,
        after=0,
        limit=100,
    ):
        """Indexed durable receipt lookup; historical prefixes never hide terminal receipts."""
        if not 1 <= limit <= 200 or after < 0:
            raise JournalError("Invalid window-dispatch query bound")
        where = ["e.kind='routing.window_dispatch'", "e.sequence>?"]
        args = [after]
        for field, value in (
            ("session_id", session_id),
            ("decision_id", decision_id),
            ("semantic_key", semantic_key),
        ):
            if value is not None:
                where.append(
                    "e.session_id=?"
                    if field == "session_id"
                    else f"json_extract(e.value,'$.payload.{field}')=?"
                )
                args.append(value)
        if interrupt_only:
            where.extend(
                [
                    "json_extract(e.value,'$.payload.agent_route')='INTERRUPT_CURRENT_TASK'",
                    "json_extract(e.value,'$.payload.state') IN ('applied','unknown')",
                ]
            )
        if pending_only:
            where.append("json_extract(e.value,'$.payload.state')='queued'")
            where.append("""NOT EXISTS (SELECT 1 FROM entries t
                WHERE t.kind='routing.window_dispatch' AND t.session_id=e.session_id
                AND json_extract(t.value,'$.payload.decision_id')=json_extract(e.value,'$.payload.decision_id')
                AND json_extract(t.value,'$.payload.state') IN ('applied','rejected','unknown','not_applied'))""")
        with self.lock:
            rows = self.db.execute(
                "SELECT e.value FROM entries e WHERE "
                + " AND ".join(where)
                + " ORDER BY e.sequence "
                + ("ASC" if pending_only else "DESC")
                + " LIMIT ?",
                [*args, limit],
            ).fetchall()
        return [JournalEntry.model_validate_json(r[0]) for r in rows]

    def recent(self, *, session_id: str, limit=100):
        if not 1 <= limit <= 200:
            raise JournalError("limit must be 1..200")
        with self.lock:
            rows = self.db.execute(
                "SELECT value FROM entries WHERE session_id=? ORDER BY sequence DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        output, size = [], 0
        for row in rows:
            size += len(row[0].encode())
            if size > 262144:
                break
            output.append(JournalEntry.model_validate_json(row[0]))
        return list(reversed(output))

    @contextmanager
    def _context_read(self):
        with self.lock:
            try:
                yield
            except sqlite3.Error as exc:
                # Malformed retained records refuse context, including when an
                # indexed payload expression cannot be evaluated. Never omit
                # their correction/qualification and restore an older draft.
                raise JournalError("Native context records are unavailable") from exc

    def _context_scope(self, source_event_id, through_sequence):
        source = self.entry(source_event_id)
        session = self.session(source.session_id)
        if (
            source.kind not in {"conversation.user", "conversation.window"}
            or source.origin != "authenticated-user"
            or session.channel != "converse"
            or (source.thread_id, source.epoch) != (session.thread_id, session.epoch)
        ):
            raise JournalError("Native context source is not a current Converse user source")
        if source.kind == "conversation.window":
            try:
                window = SpeechWindow.model_validate(source.payload)
            except ValueError as exc:
                raise JournalError("Invalid native context window source") from exc
            if (window.session_id, window.thread_id, window.epoch) != (
                source.session_id,
                source.thread_id,
                source.epoch,
            ):
                raise JournalError("Wrong native context window scope")
        elif source.payload.get("final") is not True or not isinstance(
            source.payload.get("text"), str
        ):
            raise JournalError("Invalid native context user source")
        latest = self.db.execute("SELECT coalesce(max(sequence),0) FROM entries").fetchone()[0]
        through = through_sequence if through_sequence is not None else latest
        if not source.sequence <= through <= latest:
            raise JournalError("Native context watermark does not cover the source")
        return source, through

    @staticmethod
    def _context_base_query(source, through):
        current_utterance = (
            source.payload["utterance_id"] if source.kind == "conversation.window" else None
        )
        # Choose window heads over the complete frozen scope before the page
        # cursor or provider disclosure. Later denied corrections never expose
        # an older public hypothesis. A prior utterance may have a late head.
        sql = """
            SELECT e.value FROM entries e
            WHERE e.session_id=? AND e.thread_id=? AND e.epoch=? AND e.sequence<=?
            AND e.kind IN ('conversation.user','conversation.window','conversation.converse')
            AND (
                (e.kind IN ('conversation.user','conversation.converse') AND e.sequence<?)
                OR (
                    e.kind='conversation.window'
                    AND (? IS NULL OR json_extract(e.value,'$.payload.utterance_id')<>?)
                    AND EXISTS (
                        SELECT 1 FROM entries p
                        WHERE p.session_id=e.session_id AND p.thread_id=e.thread_id
                        AND p.epoch=e.epoch AND p.kind='conversation.window'
                        AND p.sequence<?
                        AND json_extract(p.value,'$.payload.utterance_id')=
                            json_extract(e.value,'$.payload.utterance_id')
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM entries n
                        WHERE n.session_id=e.session_id AND n.thread_id=e.thread_id
                        AND n.epoch=e.epoch AND n.kind='conversation.window'
                        AND n.sequence<=?
                        AND json_extract(n.value,'$.payload.utterance_id')=
                            json_extract(e.value,'$.payload.utterance_id')
                        AND (
                            json_extract(n.value,'$.payload.window_index'),
                            json_extract(n.value,'$.payload.revision'), n.sequence
                        ) > (
                            json_extract(e.value,'$.payload.window_index'),
                            json_extract(e.value,'$.payload.revision'), e.sequence
                        )
                    )
                )
            )
        """
        return sql, [
            source.session_id,
            source.thread_id,
            source.epoch,
            through,
            source.sequence,
            current_utterance,
            current_utterance,
            source.sequence,
            through,
        ]

    def _context_page(self, request, source, through, sql, parameters, page_type, **extra):
        if request.before_sequence is not None:
            sql += " AND e.sequence<?"
            parameters.append(request.before_sequence)
        sql += " ORDER BY e.sequence DESC LIMIT ?"
        parameters.append(request.limit + 1)
        rows = self.db.execute(sql, parameters)
        entries, size, exhausted = [], 0, True
        try:
            for row in rows:
                if len(entries) == request.limit:
                    exhausted = False
                    break
                row_size = len(row[0].encode())
                if row_size > MAX_CONTEXT_PAGE_BYTES:
                    raise JournalError("Native context entry exceeds the page byte limit")
                if size + row_size > MAX_CONTEXT_PAGE_BYTES:
                    exhausted = False
                    break
                try:
                    entry = JournalEntry.model_validate_json(row[0], strict=True)
                except ValueError as exc:
                    raise JournalError("Invalid native context entry") from exc
                entries.append(entry)
                size += row_size
        finally:
            rows.close()
        try:
            page = page_type(
                schema_version="1.0",
                source_event_id=source.event_id,
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                through_sequence=through,
                entries=entries,
                next_before_sequence=entries[-1].sequence if not exhausted else None,
                exhausted=exhausted,
                **extra,
            )
            return page.validate_request(request)
        except ValueError as exc:
            raise JournalError("Invalid native context page") from exc

    def context_history(
        self, *, source_event_id, through_sequence=None, before_sequence=None, limit=100
    ) -> ContextHistoryPage:
        """Reverse semantic bases, independent of operational traffic and token capacity."""
        try:
            request = ContextHistoryRequest(
                source_event_id=source_event_id,
                through_sequence=through_sequence,
                before_sequence=before_sequence,
                limit=limit,
            )
        except ValueError as exc:
            raise JournalError("Invalid native context history request") from exc
        with self._context_read():
            source, through = self._context_scope(request.source_event_id, request.through_sequence)
            sql, parameters = self._context_base_query(source, through)
            return self._context_page(request, source, through, sql, parameters, ContextHistoryPage)

    def context_qualifications(
        self, *, source_event_id, through_sequence, base_event_ids, before_sequence=None, limit=100
    ) -> ContextQualificationPage:
        """All matching native corrections and playback evidence at the same watermark.

        Qualification paging must reach explicit exhaustion before the requested
        bases are projected. Generation stops can precede late obsolete chunks;
        the qualification cursor is therefore independent of the base cursor.
        """
        try:
            request = ContextQualificationRequest(
                source_event_id=source_event_id,
                through_sequence=through_sequence,
                base_event_ids=base_event_ids,
                before_sequence=before_sequence,
                limit=limit,
            )
        except ValueError as exc:
            raise JournalError("Invalid native context qualification request") from exc
        with self._context_read():
            source, through = self._context_scope(request.source_event_id, request.through_sequence)
            base_sql, base_parameters = self._context_base_query(source, through)
            placeholders = ",".join("?" for _ in request.base_event_ids)
            bases = [
                JournalEntry.model_validate_json(row[0], strict=True)
                for row in self.db.execute(
                    base_sql + f" AND e.event_id IN ({placeholders})",
                    [*base_parameters, *request.base_event_ids],
                )
            ]
            if {base.event_id for base in bases} != set(request.base_event_ids):
                raise JournalError("Qualification bases are not frozen native context records")
            user_ids = [base.event_id for base in bases if base.kind == "conversation.user"]
            assistant_ids = [
                base.event_id for base in bases if base.kind == "conversation.converse"
            ]
            clauses, parameters = [], [source.session_id, source.thread_id, source.epoch, through]
            if user_ids:
                user_slots = ",".join("?" for _ in user_ids)
                clauses.append(
                    "(e.kind='conversation.revision' AND "
                    f"json_extract(e.value,'$.payload.target_event_id') IN ({user_slots}))"
                )
                parameters.extend(user_ids)
            if assistant_ids:
                assistant_slots = ",".join("?" for _ in assistant_ids)
                clauses.append(f"""(
                    e.kind='delivery.updated' AND (
                        json_extract(e.value,'$.payload.event_id') IN ({assistant_slots})
                        OR (
                            json_extract(e.value,'$.payload.control')='generation_stop'
                            AND EXISTS (
                                SELECT 1 FROM entries b
                                WHERE b.event_id IN ({assistant_slots})
                                AND json_extract(b.value,'$.payload.runtime_id')=
                                    json_extract(e.value,'$.payload.runtime_id')
                                AND json_type(b.value,'$.payload.generation_epoch')='integer'
                                AND json_type(e.value,'$.payload.next_generation_epoch')='integer'
                                AND json_extract(e.value,'$.payload.next_generation_epoch')>
                                    json_extract(b.value,'$.payload.generation_epoch')
                            )
                        )
                    )
                )""")
                parameters.extend([*assistant_ids, *assistant_ids])
            # Window heads need no extra chain: head selection already happened
            # before disclosure. An all-window request has an empty qualification set.
            sql = (
                """
                SELECT e.value FROM entries e
                WHERE e.session_id=? AND e.thread_id=? AND e.epoch=? AND e.sequence<=?
                AND e.kind IN ('conversation.revision','delivery.updated')
                AND (
            """
                + (" OR ".join(clauses) if clauses else "0")
                + ")"
            )
            return self._context_page(
                request,
                source,
                through,
                sql,
                parameters,
                ContextQualificationPage,
                base_event_ids=request.base_event_ids,
            )

    def read(
        self,
        *,
        after=0,
        limit=100,
        session_id=None,
        thread_id=None,
        kinds=None,
        task_id=None,
        search=None,
    ) -> list[JournalEntry]:
        if after < 0 or not 1 <= limit <= 200:
            raise JournalError("after must be nonnegative; limit must be 1..200")
        clauses = ["sequence>?"]
        parameters = [after]
        for name, value in [("session_id", session_id), ("thread_id", thread_id)]:
            if value is not None:
                clauses.append(f"{name}=?")
                parameters.append(value)
        if kinds:
            if len(kinds) > 20:
                raise JournalError("Too many kind filters")
            clauses.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            parameters.extend(kinds)
        if task_id:
            clauses.append(
                "EXISTS(SELECT 1 FROM json_each(entries.value,'$.task_refs') WHERE value=?)"
            )
            parameters.append(task_id)
        if search:
            clauses.append("instr(json_extract(value,'$.payload.text'),?)>0")
            parameters.append(search[:1000])
        sql = (
            "SELECT value FROM entries WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence LIMIT ?"
        )
        with self.lock:
            rows = self.db.execute(sql, [*parameters, limit]).fetchall()
        output, size = [], 0
        for row in rows:
            size += len(row[0].encode())
            if size > 262144:
                break
            output.append(JournalEntry.model_validate_json(row[0]))
        return output

    def cursor(self, role: str, consumer: str) -> int:
        with self.lock:
            row = self.db.execute(
                "SELECT sequence FROM cursors WHERE writer=? AND consumer=?", (role, consumer)
            ).fetchone()
        return row[0] if row else 0

    def acknowledge(self, role: str, consumer: str, sequence: int):
        with self.lock, self.db:
            if sequence < self.cursor(role, consumer):
                raise JournalError("Cursor cannot move backwards")
            latest = self.db.execute("SELECT coalesce(max(sequence),0) FROM entries").fetchone()[0]
            if sequence > latest:
                raise JournalError("Cursor exceeds latest committed event")
            self.db.execute(
                "INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(writer,consumer) "
                "DO UPDATE SET sequence=excluded.sequence",
                (role, consumer, sequence),
            )

    def close(self):
        self.db.close()


class LocalJournal:
    """In-process adapter used by tests and single-service development, same authority checks."""

    def __init__(self, store: Store, role: str):
        self.store, self.role = store, role

    async def append(self, request):
        return self.store.append(request, self.role)

    async def read(self, **kwargs):
        return self.store.read(**kwargs)

    async def recent(self, **kwargs):
        return self.store.recent(**kwargs)

    async def context_history(self, **kwargs) -> ContextHistoryPage:
        if self.role not in {"owner", "converse"}:
            raise PermissionError("Context history requires owner or Converse")
        return self.store.context_history(**kwargs)

    async def context_qualifications(self, **kwargs) -> ContextQualificationPage:
        if self.role not in {"owner", "converse"}:
            raise PermissionError("Context qualifications require owner or Converse")
        return self.store.context_qualifications(**kwargs)

    async def session(self, session_id):
        return self.store.session(session_id)

    async def playback_generation(self, session_id, thread_id, epoch) -> PlaybackGeneration:
        if self.role not in {"owner", "converse"}:
            raise PermissionError("Playback-generation recovery requires owner or Converse")
        return self.store.playback_generation(session_id, thread_id, epoch)

    async def sessions(self):
        if self.role not in {"owner", "resources"}:
            raise PermissionError("Listing sessions requires owner or resources")
        return self.store.sessions()

    async def sessions_page(self, **kwargs):
        if self.role != "owner":
            raise PermissionError("Agent session inspection requires owner")
        return self.store.sessions_page(**kwargs)

    async def create_session(self, request):
        if self.role != "owner":
            raise PermissionError("Creating sessions requires owner")
        return self.store.create_session(request)

    async def resolve(self, event_id):
        return self.store.resolve(event_id)

    async def latest_window(self, session_id, epoch, utterance_id, window_index=None):
        return self.store.latest_window(session_id, epoch, utterance_id, window_index)

    async def window_decision(self, source_event_id):
        return self.store.window_decision(source_event_id)

    async def pending_windows(self, *, limit=100):
        if self.role not in {"owner", "decisions"}:
            raise PermissionError("Window admission requires owner or Decisions")
        return self.store.pending_windows(limit=limit)

    async def window_classification_attempts(self, source_event_id):
        if self.role not in {"owner", "decisions"}:
            raise PermissionError("Window admission requires owner or Decisions")
        return self.store.window_classification_attempts(source_event_id)

    async def window_response(self, session_id, epoch, utterance_id):
        return self.store.window_response(session_id, epoch, utterance_id)

    async def window_dispatches(self, **query):
        return self.store.window_dispatches(**query)

    async def window_control(self, session_id, epoch, agent_interrupt=False):
        return self.store.window_control(session_id, epoch, agent_interrupt)

    async def entry(self, event_id):
        return self.store.entry(event_id)

    async def cursor(self, consumer):
        return self.store.cursor(self.role, consumer)

    async def acknowledge(self, consumer, sequence):
        return self.store.acknowledge(self.role, consumer, sequence)
