"""Codex-authored native Agent mode authority in the protected Session database."""

import secrets
import time
import uuid
from contextlib import contextmanager
from functools import wraps

from pydantic import TypeAdapter

from packages.contracts.agent_control import (
    MAX_AGENT_CONTROL_INTEGER,
    AgentControlSnapshot,
    AgentModeChange,
    AgentModeLookupResult,
    AgentModeReceipt,
    AgentSteerAdmissionRequest,
    AgentSteerLookupResult,
    AgentSteerReceipt,
    AgentTaskIdentity,
    BackendId,
    ControlInstance,
)
from packages.contracts.context import MAX_CONTEXT_PAGE_BYTES, context_json
from packages.contracts.interfaces import (
    AgentControlConflict,
    AgentControlUnavailable,
    AgentModeOff,
)
from packages.contracts.models import AppendRequest, JournalEntry, SpeechWindow, TaskState
from packages.providers.disclosure import combine_scopes

_TABLES = {
    "agent_control_state": ["id", "backend_id", "instance_id", "revision", "mode", "initial_mode"],
    "agent_control_generations": ["instance_id", "retired"],
    "agent_control_changes": ["key", "digest", "request", "receipt"],
    "agent_steer_intents": [
        "submit_event_id",
        "base_revision",
        "key",
        "request",
        "receipt",
        "event_id",
    ],
}
_MAX_RECORDS = 10000


def _native(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (AgentControlUnavailable, AgentControlConflict, AgentModeOff):
            raise
        except Exception:
            raise AgentControlUnavailable from None

    return call


class AgentControl:
    """A reader until register() is explicitly called by the Session API owner.

    Different apps have different controller objects even when they share Store.
    Registration history is durable; close never retires another controller.
    """

    def __init__(self, store, *, new_store=False):
        self.store = store
        self.new_store = new_store
        self.instance = None

    def _sql(self, statement, args=()):
        try:
            return self.store.db.execute(statement, args)
        except Exception:
            raise AgentControlUnavailable from None

    def _one(self, statement, args=()):
        try:
            return self._sql(statement, args).fetchone()
        except Exception:
            raise AgentControlUnavailable from None

    def _all(self, statement, args=()):
        try:
            return self._sql(statement, args).fetchall()
        except Exception:
            raise AgentControlUnavailable from None

    def _poison(self):
        self.store._agent_submit_unavailable = True
        try:
            self.store.db.close()
        except Exception:
            pass

    @contextmanager
    def transaction(self):
        with self.store.lock:
            connection = self.store.db
            try:
                if connection.in_transaction:
                    self._poison()
                    raise AgentControlUnavailable
                self._sql("BEGIN IMMEDIATE")
                yield
                try:
                    connection.commit()
                except Exception:
                    raise AgentControlUnavailable from None
            except BaseException:
                try:
                    connection.rollback()
                except BaseException:
                    self._poison()
                raise

    def _ensure(self, *, legacy_agent_enabled=None):
        columns = [row["name"] for row in self._all("PRAGMA table_info(schema_meta)")]
        tables = {
            row["name"]
            for row in self._all("SELECT name FROM sqlite_schema WHERE type='table'")
            if row["name"] in _TABLES
        }
        if columns == ["version"] and not tables:
            rows = self._all("SELECT version FROM schema_meta")
            if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] != 1:
                raise AgentControlUnavailable
            self._sql("ALTER TABLE schema_meta ADD COLUMN agent_control_version INTEGER")
            self._sql(
                "CREATE TABLE agent_control_state(id INTEGER PRIMARY KEY CHECK(id=1),"
                "backend_id TEXT NOT NULL,instance_id TEXT,revision INTEGER NOT NULL,"
                "mode TEXT NOT NULL,initial_mode TEXT NOT NULL)"
            )
            self._sql(
                "CREATE TABLE agent_control_generations(instance_id TEXT PRIMARY KEY,"
                "retired INTEGER NOT NULL CHECK(retired IN (0,1)))"
            )
            self._sql(
                "CREATE TABLE agent_control_changes(key TEXT PRIMARY KEY,digest TEXT NOT NULL,"
                "request TEXT NOT NULL,receipt TEXT NOT NULL)"
            )
            self._sql(
                "CREATE TABLE agent_steer_intents("
                "submit_event_id TEXT NOT NULL REFERENCES entries(event_id) ON DELETE CASCADE,"
                "base_revision INTEGER NOT NULL,key TEXT NOT NULL,request TEXT NOT NULL,"
                "receipt TEXT NOT NULL,event_id TEXT NOT NULL REFERENCES entries(event_id) ON DELETE CASCADE,"
                "PRIMARY KEY(submit_event_id,base_revision),UNIQUE(submit_event_id,key))"
            )
            mode = "off" if self.new_store and legacy_agent_enabled is False else "on_demand"
            self._sql(
                "INSERT INTO agent_control_state VALUES(1,?,NULL,0,?,?)",
                (secrets.token_hex(16), mode, mode),
            )
            self._sql("UPDATE schema_meta SET agent_control_version=1")
        elif columns != ["version", "agent_control_version"] or tables != set(_TABLES):
            raise AgentControlUnavailable
        marker = self._all("SELECT version,agent_control_version FROM schema_meta")
        if len(marker) != 1 or tuple(marker[0]) != (1, 1):
            raise AgentControlUnavailable
        for name, expected in _TABLES.items():
            if [row["name"] for row in self._all(f"PRAGMA table_info({name})")] != expected:
                raise AgentControlUnavailable

    def metadata(self):
        self._ensure()
        rows = self._all("SELECT * FROM agent_control_state")
        if len(rows) != 1:
            raise AgentControlUnavailable
        row = rows[0]
        try:
            TypeAdapter(BackendId).validate_python(row["backend_id"], strict=True)
            if row["instance_id"] is not None:
                TypeAdapter(ControlInstance).validate_python(row["instance_id"], strict=True)
            if (
                row["id"] != 1
                or type(row["revision"]) is not int
                or not 0 <= row["revision"] <= MAX_AGENT_CONTROL_INTEGER
                or row["mode"] not in {"off", "on_demand", "autonomous"}
                or row["initial_mode"] not in {"off", "on_demand"}
            ):
                raise ValueError
            active = self._all(
                "SELECT instance_id,retired FROM agent_control_generations WHERE retired=0"
            )
            if row["instance_id"] is None:
                if active:
                    raise ValueError
            elif len(active) != 1 or active[0]["instance_id"] != row["instance_id"]:
                raise ValueError
            if (
                self._one(
                    "SELECT 1 FROM agent_control_generations WHERE typeof(retired)!='integer' "
                    "OR retired NOT IN (0,1) OR typeof(instance_id)!='text' LIMIT 1"
                )
                is not None
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise AgentControlUnavailable from None
        return row

    def _known(self, instance):
        row = self._one(
            "SELECT instance_id,retired FROM agent_control_generations WHERE instance_id=?",
            (instance,),
        )
        if row is None:
            raise AgentControlConflict
        if type(row["retired"]) is not int or row["retired"] not in (0, 1):
            raise AgentControlUnavailable
        return bool(row["retired"])

    @_native
    def register(self, *, legacy_agent_enabled=None):
        if self.instance is not None:
            raise AgentControlConflict
        instance = str(uuid.uuid4())
        with self.transaction():
            self._ensure(legacy_agent_enabled=legacy_agent_enabled)
            self.metadata()
            if self._one("SELECT count(*) FROM agent_control_generations")[0] >= _MAX_RECORDS:
                raise AgentControlUnavailable
            self._sql("UPDATE agent_control_generations SET retired=1 WHERE retired=0")
            self._sql("INSERT INTO agent_control_generations VALUES(?,0)", (instance,))
            self._sql("UPDATE agent_control_state SET instance_id=? WHERE id=1", (instance,))
        self.instance = instance

    def _snapshot(self, row):
        if row["instance_id"] is None:
            raise AgentControlUnavailable
        return AgentControlSnapshot(
            schema_version="1.0",
            backend_id=row["backend_id"],
            instance_id=row["instance_id"],
            revision=row["revision"],
            mode=row["mode"],
            active_work_policy="finish_already_accepted",
            control_writable=self.instance is not None and self.instance == row["instance_id"],
            available_modes=("off", "on_demand"),
            standing_work_available=False,
        )

    @_native
    def snapshot(self):
        with self.transaction():
            return self._snapshot(self.metadata())

    @_native
    def backend(self):
        """Internal Agent discovery without registering any control writer."""
        with self.transaction():
            return self.metadata()["backend_id"]

    def _mode_proof(self, request, row):
        saved = self._one(
            "SELECT * FROM agent_control_changes WHERE key=?", (request.idempotency_key,)
        )
        if saved is None:
            return None
        try:
            original = AgentModeChange.from_json(saved["request"].encode("utf-8"))
            receipt = AgentModeReceipt.from_json(saved["receipt"].encode("utf-8")).validate_request(
                original
            )
            if (
                saved["key"] != original.idempotency_key
                or saved["digest"] != original.request_digest()
                or receipt.backend_id != row["backend_id"]
                or receipt.revision > row["revision"]
            ):
                raise ValueError
            self._known(original.expected_instance)
        except (ValueError, TypeError, AttributeError):
            raise AgentControlUnavailable from None
        if original.canonical_bytes() != request.canonical_bytes():
            raise AgentControlConflict
        return receipt

    @_native
    def change(self, request):
        request = AgentModeChange.model_validate(request)
        with self.transaction():
            row = self.metadata()
            previous = self._mode_proof(request, row)
            if previous is not None:
                return previous
            self._known(request.expected_instance)
            if (
                request.expected_backend != row["backend_id"]
                or request.expected_instance != row["instance_id"]
                or self.instance != row["instance_id"]
                or request.expected_revision != row["revision"]
                or request.mode == "autonomous"
            ):
                raise AgentControlConflict
            if self._one("SELECT count(*) FROM agent_control_changes")[0] >= _MAX_RECORDS:
                raise AgentControlUnavailable
            receipt = AgentModeReceipt(
                schema_version="1.0",
                status="mode_saved",
                backend_id=row["backend_id"],
                service_instance=self.instance,
                revision=request.expected_revision + 1,
                request=request,
            )
            self._sql(
                "UPDATE agent_control_state SET mode=?,revision=? WHERE id=1",
                (request.mode, receipt.revision),
            )
            self._sql(
                "INSERT INTO agent_control_changes VALUES(?,?,?,?)",
                (
                    request.idempotency_key,
                    request.request_digest(),
                    request.canonical_bytes().decode(),
                    receipt.canonical_bytes().decode(),
                ),
            )
            return receipt

    @_native
    def lookup(self, request):
        request = AgentModeChange.model_validate(request)
        with self.transaction():
            row = self.metadata()
            if request.expected_backend != row["backend_id"]:
                raise AgentControlConflict
            receipt = self._mode_proof(request, row)
            retired = self._known(request.expected_instance)
            return AgentModeLookupResult(
                schema_version="1.0",
                status="recorded" if receipt is not None else "unobserved",
                request=request,
                state=self._snapshot(row),
                receipt=receipt,
                mutation_fenced=receipt is not None
                or retired
                or row["revision"] > request.expected_revision,
            ).validate_request(request)

    def require_admission(self):
        """Called inside the existing native acceptance transaction, after proof."""
        row = self.metadata()
        if row["mode"] == "off":
            raise AgentModeOff
        return row

    @_native
    def close(self):
        instance, self.instance = self.instance, None
        if instance is None:
            return
        with self.transaction():
            row = self.metadata()
            self._sql(
                "UPDATE agent_control_generations SET retired=1 WHERE instance_id=?", (instance,)
            )
            if row["instance_id"] == instance:
                self._sql("UPDATE agent_control_state SET instance_id=NULL WHERE id=1")

    def _initial(self, request):
        task = request.task
        entry = self.store._agent_submit_winner(
            task.submit_event_id, task.session_id, task.submit_idempotency_key
        )
        identity = AgentTaskIdentity(
            schema_version="1.0",
            session_id=entry.session_id,
            thread_id=entry.thread_id,
            epoch=entry.epoch,
            task_id=entry.payload["task_id"],
            submit_event_id=entry.event_id,
            submit_sequence=entry.sequence,
            submit_idempotency_key=entry.payload["idempotency_key"],
            submit_payload_digest=entry.payload["payload_digest"],
        )
        if identity != task:
            raise AgentControlConflict
        return entry

    def _event(self, row):
        try:
            raw = context_json(row["value"].encode("utf-8"), maximum=MAX_CONTEXT_PAGE_BYTES)
            original = context_json(row["request"].encode("utf-8"), maximum=MAX_CONTEXT_PAGE_BYTES)
            if type(raw.get("sequence")) is not int or type(raw.get("epoch")) is not int:
                raise ValueError
            event = JournalEntry.model_validate(raw)
            if (
                any(
                    row[name] != getattr(event, name)
                    for name in ("sequence", "event_id", "session_id", "thread_id", "epoch", "kind")
                )
                or event.origin != "agent-supervisor"
                or row["writer"] != "agent"
                or row["idem"] != event.idempotency_key
                or original != {key: raw[key] for key in AppendRequest.model_fields}
            ):
                raise ValueError
            return event
        except (ValueError, TypeError, AttributeError, KeyError):
            raise AgentControlUnavailable from None

    def _steer_proof(self, request):
        saved = self._one(
            "SELECT * FROM agent_steer_intents WHERE submit_event_id=? AND key=?",
            (request.task.submit_event_id, request.request.idempotency_key),
        )
        if saved is None:
            return None
        try:
            original = AgentSteerAdmissionRequest.from_json(saved["request"].encode())
            receipt = AgentSteerReceipt.from_json(saved["receipt"].encode()).validate_request(
                original
            )
            event_row = self._one("SELECT * FROM entries WHERE event_id=?", (saved["event_id"],))
            event = self._event(event_row)
            if (
                saved["submit_event_id"] != original.task.submit_event_id
                or saved["base_revision"] != original.request.expected_objective_revision
                or saved["key"] != original.request.idempotency_key
                or receipt.disposition != "created"
                or event.event_id != receipt.event_id
                or event.sequence != receipt.sequence
                or event.kind != "agent.command"
                or (event.session_id, event.thread_id, event.epoch)
                != (original.task.session_id, original.task.thread_id, original.task.epoch)
                or event.task_refs != [original.task.task_id]
                or event.payload.get("intent_digest") != original.request_digest()
                or event.payload.get("submit_event_id") != original.task.submit_event_id
                or event.payload.get("op") != "steer-requested"
                or event.payload.get("actor") != original.actor
                or event.payload.get("task_id") != original.task.task_id
                or event.payload.get("expected_objective_revision")
                != original.request.expected_objective_revision
                or event.causes != list(original.request.source_event_ids)
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError, KeyError):
            raise AgentControlUnavailable from None
        if original.canonical_bytes() != request.canonical_bytes():
            raise AgentControlConflict
        return AgentSteerReceipt.model_validate(
            {**receipt.model_dump(mode="python"), "disposition": "retained"}
        )

    def _history(self, initial):
        revision = initial.payload["objective_revision"]
        scope = initial.payload.get("content_scope", "local_only")
        state = "accepted"
        sources = set(initial.payload["source_event_ids"])
        after = initial.sequence
        deadline = time.monotonic() + 4.0
        while True:
            if time.monotonic() >= deadline:
                raise AgentControlUnavailable
            rows = self._all(
                "SELECT * FROM entries WHERE session_id=? AND thread_id=? AND epoch=? "
                "AND writer='agent' AND sequence>? AND ("
                "substr(idem,1,length(?))=? OR "
                "CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.task_id') END=? OR "
                "CASE WHEN json_valid(request) THEN json_extract(request,'$.payload.task_id') END=? OR "
                "CASE WHEN json_valid(value) THEN EXISTS(SELECT 1 FROM json_each(entries.value,'$.task_refs') WHERE value=?) END OR "
                "CASE WHEN json_valid(request) THEN EXISTS(SELECT 1 FROM json_each(entries.request,'$.task_refs') WHERE value=?) END"
                ") ORDER BY sequence LIMIT 200",
                (
                    initial.session_id,
                    initial.thread_id,
                    initial.epoch,
                    after,
                    "agent:" + initial.payload["task_id"] + ":",
                    "agent:" + initial.payload["task_id"] + ":",
                    initial.payload["task_id"],
                    initial.payload["task_id"],
                    initial.payload["task_id"],
                    initial.payload["task_id"],
                ),
            )
            if not rows:
                break
            for row in rows:
                after = row["sequence"]
                event = self._event(row)
                if event.task_refs != [initial.payload["task_id"]]:
                    if (
                        initial.payload["task_id"] in event.task_refs
                        or event.payload.get("task_id") == initial.payload["task_id"]
                    ):
                        raise AgentControlUnavailable
                    continue
                if event.payload.get("task_id") != initial.payload["task_id"]:
                    raise AgentControlUnavailable
                if event.payload.get("op") == "submit":
                    raise AgentControlConflict
                scope = combine_scopes(scope, event.payload.get("content_scope", "local_only"))
                state = event.payload.get("state", state)
                if state not in {value.value for value in TaskState}:
                    raise AgentControlUnavailable
                if event.kind == "agent.command" and event.payload.get("op") == "steer-applied":
                    saved = self._one(
                        "SELECT request FROM agent_steer_intents WHERE event_id=?",
                        (event.payload.get("steer_event_id"),),
                    )
                    if saved is None:
                        raise AgentControlUnavailable
                    proposal = AgentSteerAdmissionRequest.from_json(saved[0].encode())
                    proof = self._steer_proof(proposal)
                    if (
                        proposal.task.submit_event_id != initial.event_id
                        or event.payload.get("intent_digest") != proposal.request_digest()
                        or proof.target_objective_revision != revision + 1
                        or event.payload.get("objective_revision") != revision + 1
                    ):
                        raise AgentControlUnavailable
                    revision += 1
                    sources.update(proposal.request.source_event_ids)
                if event.kind == "agent.outcome":
                    if event.payload.get("task", {}).get("state") in {
                        "succeeded",
                        "failed",
                        "cancelled",
                    }:
                        state = event.payload["task"]["state"]
        return revision, scope, state, sources

    def _source(self, source_id, task):
        original = self.store.entry(source_id)
        resolved = self.store.resolve(source_id)
        if (
            (original.session_id, original.thread_id, original.epoch)
            != (task.session_id, task.thread_id, task.epoch)
            or (resolved.session_id, resolved.thread_id, resolved.epoch)
            != (task.session_id, task.thread_id, task.epoch)
            or original.kind not in {"conversation.user", "conversation.window"}
            or resolved.kind != original.kind
            or original.origin != "authenticated-user"
            or resolved.origin != "authenticated-user"
            or not isinstance(resolved.payload.get("text"), str)
            or not resolved.payload["text"].strip()
            or type(resolved.payload.get("revision", 1)) is not int
        ):
            raise AgentControlConflict
        if original.kind == "conversation.window":
            window = SpeechWindow.model_validate(resolved.payload)
            latest = self.store.latest_window(task.session_id, task.epoch, window.utterance_id)
            control = self.store.window_control(task.session_id, task.epoch, agent_interrupt=True)
            if (
                latest is None
                or latest.payload.get("text") != window.text
                or (control is not None and control.payload["source_sequence"] > resolved.sequence)
            ):
                raise AgentControlConflict
        elif resolved.payload.get("final") is not True:
            raise AgentControlConflict
        return resolved, self.store._agent_source_scope(original, resolved)

    @_native
    def admit_steer(self, request):
        request = AgentSteerAdmissionRequest.model_validate(request)
        self.store._ensure_agent_submit_upgrade()
        with self.transaction():
            row = self.metadata()
            if request.expected_backend != row["backend_id"]:
                raise AgentControlConflict
            initial = self._initial(request)
            previous = self._steer_proof(request)
            if previous is not None:
                return previous
            if (
                self._one(
                    "SELECT 1 FROM agent_steer_intents WHERE submit_event_id=? AND base_revision=?",
                    (request.task.submit_event_id, request.request.expected_objective_revision),
                )
                is not None
            ):
                raise AgentControlConflict
            session = self.store.session(request.task.session_id)
            if (session.thread_id, session.epoch) != (request.task.thread_id, request.task.epoch):
                raise AgentControlConflict
            revision, scope, state, old_sources = self._history(initial)
            if (
                state in {"succeeded", "failed", "cancelled", "cancel_requested"}
                or revision != request.request.expected_objective_revision
            ):
                raise AgentControlConflict
            for source_id in old_sources:
                _, floor = self._source(source_id, request.task)
                scope = combine_scopes(scope, floor)
            texts = []
            for observed in request.source_revisions:
                resolved, floor = self._source(observed.event_id, request.task)
                if (
                    observed.revision != resolved.payload.get("revision", 1)
                    or observed.content_scope != floor
                ):
                    raise AgentControlConflict
                texts.append(resolved.payload["text"])
                scope = combine_scopes(scope, floor)
            if request.actor == "decisions" and request.request.instruction != "\n".join(texts):
                raise AgentControlConflict
            if (
                combine_scopes(
                    scope, request.request.content_scope, request.effective_content_scope
                )
                != request.effective_content_scope
            ):
                raise AgentControlConflict
            row = self.require_admission()
            if self._one("SELECT count(*) FROM agent_steer_intents")[0] >= _MAX_RECORDS:
                raise AgentControlUnavailable
            event = self.store._append_entry_locked(
                AppendRequest(
                    session_id=request.task.session_id,
                    thread_id=request.task.thread_id,
                    epoch=request.task.epoch,
                    kind="agent.command",
                    task_refs=[request.task.task_id],
                    causes=list(request.request.source_event_ids),
                    idempotency_key="agent:steer:" + request.request_digest(),
                    payload={
                        "op": "steer-requested",
                        "task_id": request.task.task_id,
                        "state": state,
                        "actor": request.actor,
                        "submit_event_id": initial.event_id,
                        "intent_digest": request.request_digest(),
                        "expected_objective_revision": revision,
                        "content_scope": request.effective_content_scope,
                    },
                ),
                "agent",
            )
            receipt = AgentSteerReceipt(
                schema_version="1.0",
                disposition="created",
                status="intent_accepted",
                request=request,
                event_id=event.event_id,
                sequence=event.sequence,
                control_revision=row["revision"],
                admission_mode=row["mode"],
                target_objective_revision=revision + 1,
            ).validate_request(request)
            self._sql(
                "INSERT INTO agent_steer_intents VALUES(?,?,?,?,?,?)",
                (
                    initial.event_id,
                    revision,
                    request.request.idempotency_key,
                    request.canonical_bytes().decode(),
                    receipt.canonical_bytes().decode(),
                    event.event_id,
                ),
            )
            return receipt

    @_native
    def lookup_steer(self, request):
        request = AgentSteerAdmissionRequest.model_validate(request)
        self.store._ensure_agent_submit_upgrade()
        with self.transaction():
            row = self.metadata()
            if request.expected_backend != row["backend_id"]:
                raise AgentControlConflict
            self._initial(request)
            receipt = self._steer_proof(request)
            return AgentSteerLookupResult(
                schema_version="1.0",
                status="recorded" if receipt else "unobserved",
                request=request,
                receipt=receipt,
            ).validate_request(request)
