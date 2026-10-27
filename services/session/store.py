"""The sole SQLite journal owner. Transactions establish sequence and provenance."""

import json
import sqlite3
import threading
from pathlib import Path

from packages.contracts.models import (
    AppendRequest,
    JournalEntry,
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
            CREATE INDEX IF NOT EXISTS entries_window_dispatch ON entries(
                json_extract(value,'$.payload.decision_id'), sequence)
                WHERE kind='routing.window_dispatch';
            CREATE INDEX IF NOT EXISTS entries_window_semantic ON entries(
                session_id, json_extract(value,'$.payload.semantic_key'), sequence)
                WHERE kind='routing.window_dispatch';
            CREATE TABLE IF NOT EXISTS cursors(
                writer TEXT NOT NULL, consumer TEXT NOT NULL, sequence INTEGER NOT NULL,
                PRIMARY KEY(writer,consumer));
        """)
        if self.db.execute("SELECT version FROM schema_meta").fetchone()[0] != 1:
            raise JournalError(
                "Unsupported database version; preserve the database before migration"
            )
        self.db.commit()

    def create_session(self, request: SessionRequest) -> Session:
        session = Session(channel=request.channel, name=request.name)
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
        encoded = request.model_dump_json()
        if len(encoded.encode()) > 65536:
            raise JournalError("Entry exceeds 64 KiB; store large artifacts by reference")
        with self.lock, self.db:
            session = self.session(request.session_id)
            if (request.epoch, request.thread_id) != (session.epoch, session.thread_id):
                raise JournalError("Stale epoch or wrong session thread")
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

    async def session(self, session_id):
        return self.store.session(session_id)

    async def sessions(self):
        if self.role not in {"owner", "resources"}:
            raise PermissionError("Listing sessions requires owner or resources")
        return self.store.sessions()

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
