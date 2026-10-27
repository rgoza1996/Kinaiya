"""The sole SQLite journal owner. Transactions establish sequence and provenance."""

import json
import sqlite3
import threading
from pathlib import Path

from packages.contracts.models import AppendRequest, JournalEntry, Session, SessionRequest

ALLOWED_KINDS = {
    "owner": {"conversation.user", "conversation.revision", "delivery.updated"},
    "converse": {
        "conversation.user",
        "conversation.converse",
        "conversation.revision",
        "delivery.updated",
        "converse.safety",
    },
    "agent": {
        "agent.command",
        "agent.outcome",
        "agent.tool",
        "agent.state",
        "approval.requested",
        "approval.resolved",
        "artifact.updated",
    },
    "decisions": {"routing.decided"},
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
            if request.kind == "conversation.user":
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
        revisions = self.read(
            session_id=event.session_id,
            kinds=["conversation.revision"],
            after=event.sequence,
            limit=200,
        )
        for revision in revisions:
            if revision.payload.get("target_event_id") == event_id:
                event = event.model_copy(update={"payload": {**event.payload, **revision.payload}})
        return event

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

    async def session(self, session_id):
        return self.store.session(session_id)

    async def resolve(self, event_id):
        return self.store.resolve(event_id)

    async def entry(self, event_id):
        return self.store.entry(event_id)

    async def cursor(self, consumer):
        return self.store.cursor(self.role, consumer)

    async def acknowledge(self, consumer, sequence):
        return self.store.acknowledge(self.role, consumer, sequence)
