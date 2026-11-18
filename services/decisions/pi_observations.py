"""Durably retain validated Pi observations outside the peer's mounts."""

import os
import secrets
import sqlite3
import stat
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import TypeAdapter

from packages.agent_adapters.pi_sdk import MAX_FRAME_BYTES, PiEventReplayExpired, PiReply
from packages.contracts.external_agent import (
    MAX_CURSOR,
    MAX_EXTERNAL_COMMAND_BYTES,
    ExternalAgentClaim,
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
_MAX_QUEUED = 32
_EVENT = TypeAdapter(ExternalAgentEvent)
_FINAL = {"settled", "failed", "interrupted"}
_IN_FLIGHT = ("starting", "running", "interrupt_requested")
_DATABASE_ACCESS = threading.Lock()


class PiObservationUnavailable(ValueError):
    """The protected observation store cannot attest to this request."""


class PiQueueConflict(ValueError):
    """The original queued command has different content."""


class PiObservations:
    def __init__(self, data_dir, target):
        self.target = ExternalAgentTarget.model_validate(target, strict=True)
        self.generation = secrets.token_hex(16)
        self.directory = Path(data_dir) / "external-agent"
        self.filename = self.target.attachment_id + ".observations.sqlite"

    @contextmanager
    def _database(self, *, write=False, create=False):
        try:
            # One local observation transaction at a time prevents the pipe
            # reader's burst of writes from starving a snapshot in rollback mode.
            with _DATABASE_ACCESS:
                parent = _open_path(self.directory, directory=True)
                try:
                    _require_owner_private(parent, directory=True)
                    identity = self._file_identity(parent, create=create)
                    mode = "rw" if write else "ro"
                    uri = f"file:/proc/self/fd/{parent}/{self.filename}?mode={mode}"
                    with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as database:
                        if self._file_identity(parent) != identity:
                            raise PiObservationUnavailable
                        database.execute("PRAGMA trusted_schema=OFF")
                        if write:
                            database.execute("PRAGMA synchronous=FULL")
                            database.execute("PRAGMA secure_delete=ON")
                        with database:
                            database.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                            yield database
                finally:
                    os.close(parent)
        except (OSError, sqlite3.Error, PiLaunchUnavailable) as error:
            raise PiObservationUnavailable from error

    def _file_identity(self, parent, *, create=False):
        # Closing an independently opened database fd can clear SQLite's POSIX
        # locks. Use stat after the initial owner-only file creation.
        try:
            info = os.stat(self.filename, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(self.filename, flags, 0o600, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise PiObservationUnavailable
        return info.st_dev, info.st_ino

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
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending (idempotency_key TEXT PRIMARY KEY,"
                "run_id TEXT NOT NULL UNIQUE,wire TEXT NOT NULL)"
            )
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
            pending = db.execute(
                "SELECT run_id FROM pending WHERE idempotency_key=?", (claim.idempotency_key,)
            ).fetchone()
            if pending is None:
                db.execute(
                    "INSERT INTO tasks VALUES(?,?,?,?,'starting',NULL,NULL)",
                    (
                        claim.admission_id,
                        self.generation,
                        claim.command_digest,
                        source.model_dump_json(),
                    ),
                )
            else:
                if (
                    pending[0] != claim.admission_id
                    or db.execute(
                        "UPDATE tasks SET generation=? WHERE run_id=? AND digest=? AND state='queued'",
                        (self.generation, claim.admission_id, claim.command_digest),
                    ).rowcount
                    != 1
                ):
                    raise PiObservationUnavailable
                db.execute("DELETE FROM pending WHERE run_id=?", (claim.admission_id,))
                self._transition(db, claim.admission_id, "starting")
            self._prune_receipts(db)

    def _queued_task(self, db, request):
        if request.command.target != self.target:
            raise PiQueueConflict
        row = db.execute(
            "SELECT t.run_id,t.digest FROM pending p JOIN tasks t ON t.run_id=p.run_id "
            "WHERE p.idempotency_key=? AND t.state='queued'",
            (request.command.idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row[1] != request.command_digest():
            raise PiQueueConflict
        return ExternalAgentTask(
            task_id=row[0], source=request.command.source, revision=1, state="queued"
        )

    def queued_task(self, request):
        with self._database() as db:
            self._head(db, current=True)
            return self._queued_task(db, request)

    def enqueue(self, request, eligibility):
        wire = request.model_dump_json()
        digest = request.command_digest()
        if (
            len(wire.encode("utf-8")) > MAX_EXTERNAL_COMMAND_BYTES
            or eligibility.disposition != "eligible"
            or eligibility.target != self.target
            or eligibility.idempotency_key != request.command.idempotency_key
            or eligibility.decision_event_id != request.decision_event_id
            or eligibility.command_digest != digest
            or eligibility.admission_id != digest[:32]
        ):
            raise PiObservationUnavailable
        with self._database(write=True) as db:
            self._head(db, current=True)
            task = self._queued_task(db, request)
            if task is not None:
                return task
            if db.execute("SELECT count(*) FROM pending").fetchone()[0] >= _MAX_QUEUED:
                return None
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,'queued',NULL,NULL)",
                (
                    eligibility.admission_id,
                    self.generation,
                    digest,
                    request.command.source.model_dump_json(),
                ),
            )
            db.execute(
                "INSERT INTO pending VALUES(?,?,?)",
                (request.command.idempotency_key, eligibility.admission_id, wire),
            )
            self._transition(db, eligibility.admission_id, "queued")
            self._prune_receipts(db)
            return self._queued_task(db, request)

    def next_queued(self):
        with self._database() as db:
            self._head(db, current=True)
            row = db.execute("SELECT wire FROM pending ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            request = ExternalAgentClaim.model_validate_json(row[0], strict=True)
            if self._queued_task(db, request) is None:
                raise PiObservationUnavailable
            return request

    def retire_queued(self, request, state):
        with self._database(write=True) as db:
            self._head(db, current=True)
            task = self._queued_task(db, request)
            if task is not None:
                db.execute("DELETE FROM pending WHERE run_id=?", (task.task_id,))
                self._transition(db, task.task_id, state)
                self._prune_receipts(db)

    @staticmethod
    def _prune_receipts(db):
        db.execute(
            "DELETE FROM tasks WHERE rowid IN (SELECT rowid FROM tasks "
            "ORDER BY rowid DESC LIMIT -1 OFFSET ?) "
            "AND (disposition='refused' OR state NOT IN ('queued',?,?,?))",
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
            if reply.disposition == "refused":
                self._transition(db, task_id, "not_applied")
            elif reply.disposition == "uncertain":
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
                "SELECT run_id,source,state FROM tasks "
                "WHERE disposition IS NOT 'refused' OR state='not_applied' "
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
