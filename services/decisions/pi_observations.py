"""Durably retain validated Pi observations outside the peer's mounts."""

import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import TypeAdapter

from packages.agent_adapters.pi_sdk import MAX_FRAME_BYTES, PiEventReplayExpired, PiReply
from packages.contracts.external_agent import (
    MAX_CURSOR,
    ExternalAgentEvent,
    ExternalAgentEventPage,
    ExternalAgentMessageEvent,
    ExternalAgentOutputUnavailable,
    ExternalAgentSource,
    ExternalAgentStateEvent,
    ExternalAgentTarget,
    ExternalAgentTask,
    ExternalAgentToolEvent,
)
from services.decisions.pi_process import PiLaunchUnavailable, _open_path, _require_owner_private

_MAX_EVENTS = 512
_MAX_BYTES = 8 * MAX_FRAME_BYTES
_MAX_BATCH_BYTES = MAX_FRAME_BYTES + 4096
_MAX_RECEIPTS = 512
_EVENT = TypeAdapter(ExternalAgentEvent)
_FINAL = {"settled", "failed", "interrupted"}
_IN_FLIGHT = ("starting", "running", "interrupt_requested")


class PiObservationUnavailable(ValueError):
    """The protected observation store cannot attest to this request."""


class PiObservations:
    def __init__(self, data_dir, target):
        self.target = ExternalAgentTarget.model_validate(target, strict=True)
        self.generation = secrets.token_hex(16)
        self.directory = Path(data_dir) / "external-agent"
        self.filename = self.target.attachment_id + ".observations.sqlite"

    @contextmanager
    def _database(self, *, write=False, create=False):
        flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC
        if create:
            flags |= os.O_CREAT
        try:
            parent = _open_path(self.directory, directory=True)
            try:
                _require_owner_private(parent, directory=True)
                descriptor = os.open(self.filename, flags | os.O_NONBLOCK, 0o600, dir_fd=parent)
                try:
                    _require_owner_private(descriptor, directory=False)
                    mode = "rw" if write else "ro"
                    uri = f"file:/proc/self/fd/{descriptor}?mode={mode}"
                    with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as database:
                        database.execute("PRAGMA trusted_schema=OFF")
                        if write:
                            database.execute("PRAGMA synchronous=FULL")
                            database.execute("PRAGMA secure_delete=ON")
                        with database:
                            database.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                            yield database
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent)
        except (OSError, sqlite3.Error, PiLaunchUnavailable) as error:
            raise PiObservationUnavailable from error

    def _head(self, db, *, current=False):
        if db.execute("PRAGMA user_version").fetchone() != (1,):
            raise PiObservationUnavailable
        row = db.execute(
            "SELECT attachment,backend,generation,cursor FROM stream WHERE id=1"
        ).fetchone()
        if (
            row is None
            or row[:2] != (self.target.attachment_id, self.target.backend_session_id)
            or (current and row[2] != self.generation)
            or type(row[3]) is not int
            or not 0 <= row[3] <= MAX_CURSOR
        ):
            raise PiObservationUnavailable
        return row[2], row[3]

    def start(self):
        with self._database(write=True, create=True) as db:
            if (
                db.execute("PRAGMA user_version").fetchone() == (0,)
                and db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is None
            ):
                db.execute(
                    "CREATE TABLE stream (id INTEGER PRIMARY KEY CHECK(id=1),"
                    "attachment TEXT NOT NULL,backend TEXT NOT NULL,generation TEXT,"
                    "cursor INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE tasks (run_id TEXT PRIMARY KEY,generation TEXT NOT NULL,"
                    "digest TEXT NOT NULL,source TEXT NOT NULL,state TEXT NOT NULL,"
                    "disposition TEXT,reason TEXT)"
                )
                db.execute(
                    "CREATE TABLE events (cursor INTEGER PRIMARY KEY,wire TEXT NOT NULL,"
                    "bytes INTEGER NOT NULL)"
                )
                db.execute(
                    "INSERT INTO stream VALUES(1,?,?,NULL,0)",
                    (self.target.attachment_id, self.target.backend_session_id),
                )
                db.execute("PRAGMA user_version=1")
            prior, _ = self._head(db)
            self._seal(db, prior)
            db.execute("UPDATE stream SET generation=? WHERE id=1", (self.generation,))

    def stop(self):
        with self._database(write=True) as db:
            self._head(db, current=True)
            self._seal(db, self.generation)
            db.execute("UPDATE stream SET generation=NULL WHERE id=1")

    def _seal(self, db, generation):
        if generation is None:
            return
        rows = db.execute(
            "SELECT run_id FROM tasks WHERE generation=? AND state IN (?,?,?) "
            "AND disposition IS NOT 'refused' ORDER BY rowid",
            (generation, *_IN_FLIGHT),
        ).fetchall()
        for (task_id,) in rows:
            self._transition(db, task_id, "uncertain")

    def prepare(self, claim, source):
        if claim.target != self.target or claim.disposition != "original":
            raise PiObservationUnavailable
        with self._database(write=True) as db:
            self._head(db, current=True)
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,'starting',NULL,NULL)",
                (
                    claim.admission_id,
                    self.generation,
                    claim.command_digest,
                    source.model_dump_json(),
                ),
            )
            db.execute(
                "DELETE FROM tasks WHERE rowid IN (SELECT rowid FROM tasks "
                "ORDER BY rowid DESC LIMIT -1 OFFSET ?) "
                "AND (disposition='refused' OR state NOT IN (?,?,?))",
                (_MAX_RECEIPTS, *_IN_FLIGHT),
            )

    def original_reply(self, claim):
        if claim.target != self.target:
            raise PiObservationUnavailable
        with self._database() as db:
            self._head(db)
            row = db.execute(
                "SELECT digest,disposition,reason FROM tasks WHERE run_id=?",
                (claim.admission_id,),
            ).fetchone()
        if row is None:
            return None
        digest, disposition, reason = row
        if digest != claim.command_digest:
            raise PiObservationUnavailable
        return PiReply(disposition, reason) if disposition is not None else None

    def record_reply(self, task_id, reply):
        with self._database(write=True) as db:
            self._head(db, current=True)
            changed = db.execute(
                "UPDATE tasks SET disposition=?,reason=? WHERE run_id=? AND generation=? "
                "AND disposition IS NULL",
                (reply.disposition, reply.reason, task_id, self.generation),
            )
            if changed.rowcount != 1:
                raise PiObservationUnavailable
            if reply.disposition == "uncertain":
                state = self._task_state(db, task_id)
                if state == "starting":
                    self._transition(db, task_id, "uncertain")

    def _task_state(self, db, task_id):
        row = db.execute(
            "SELECT state FROM tasks WHERE run_id=? AND generation=? "
            "AND disposition IS NOT 'refused'",
            (task_id, self.generation),
        ).fetchone()
        if row is None:
            raise PiObservationUnavailable
        return row[0]

    def record_event(self, event):
        with self._database(write=True) as db:
            _, cursor = self._head(db, current=True)
            if self._task_state(db, event.run_id) in _FINAL:
                raise PiObservationUnavailable
            identity = {"cursor": cursor + 1, "task_id": event.run_id}
            if event.kind == "assistant_message":
                observed = ExternalAgentMessageEvent(kind=event.kind, text=event.text, **identity)
            elif event.kind == "tool_activity":
                observed = ExternalAgentToolEvent(
                    kind=event.kind,
                    tool_call_id=event.tool_call_id,
                    tool_name=event.tool_name,
                    phase=event.phase,
                    **identity,
                )
            elif event.kind == "output_unavailable":
                observed = ExternalAgentOutputUnavailable(kind=event.kind, **identity)
            else:
                self._transition(db, event.run_id, event.kind)
                return
            self._append(db, observed)

    def _transition(self, db, task_id, state):
        _, cursor = self._head(db)
        db.execute("UPDATE tasks SET state=? WHERE run_id=?", (state, task_id))
        self._append(
            db,
            ExternalAgentStateEvent(
                kind="task_state", cursor=cursor + 1, state=state, revision=1, task_id=task_id
            ),
        )

    @staticmethod
    def _append(db, event):
        wire = event.model_dump_json()
        size = len(wire.encode("utf-8")) + 1
        if size > _MAX_BATCH_BYTES:
            raise PiObservationUnavailable
        db.execute("INSERT INTO events VALUES(?,?,?)", (event.cursor, wire, size))
        db.execute("UPDATE stream SET cursor=? WHERE id=1", (event.cursor,))
        db.execute(
            "DELETE FROM events WHERE cursor <= coalesce((SELECT max(cursor) FROM ("
            "SELECT cursor, sum(bytes) OVER (ORDER BY cursor DESC) AS total,"
            " row_number() OVER (ORDER BY cursor DESC) AS count FROM events"
            ") WHERE total>? OR count>?),0)",
            (_MAX_BYTES, _MAX_EVENTS),
        )

    def status(self, *, live):
        with self._database() as db:
            generation, _ = self._head(db)
            rows = db.execute(
                "SELECT run_id,source,state FROM tasks WHERE disposition IS NOT 'refused' "
                "ORDER BY rowid DESC LIMIT 128"
            ).fetchall()
        tasks = tuple(
            ExternalAgentTask(
                task_id=task_id,
                source=ExternalAgentSource.model_validate_json(source, strict=True),
                state="uncertain" if not live and state in _IN_FLIGHT else state,
                revision=1,
            )
            for task_id, source, state in reversed(rows)
        )
        return generation is None, tasks

    def events(self, request):
        if request.target != self.target:
            raise PiObservationUnavailable
        with self._database() as db:
            _, head = self._head(db)
            start = db.execute("SELECT min(cursor) FROM events").fetchone()[0]
            if request.after > head or (start is not None and request.after < start - 1):
                raise PiEventReplayExpired
            rows = db.execute(
                "SELECT cursor,wire,bytes FROM events WHERE cursor>? ORDER BY cursor LIMIT ?",
                (request.after, request.limit),
            )
            events, total = [], 0
            for cursor, wire, size in rows:
                total += size
                if total > _MAX_BATCH_BYTES:
                    break
                event = _EVENT.validate_json(wire)
                if event.cursor != cursor:
                    raise PiObservationUnavailable
                events.append(event)
            end = events[-1].cursor if events else request.after
            return ExternalAgentEventPage(
                contract_version=1,
                request_id=request.request_id,
                kind="events",
                target=self.target,
                after=request.after,
                next_after=end,
                has_more=end < head,
                events=tuple(events),
            )
