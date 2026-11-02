"""Explicit native v2 claim history and original-Agent settlement authority.

Checked history is local to one synchronous transaction phase. It is never an
authority cache, and a post-DML commit guard checks the complete history anew.
"""

import hashlib
import hmac
import json
import math
import re
import time
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from packages.contracts.agent_control import MAX_AGENT_CONTROL_INTEGER
from packages.contracts.agent_export import AgentExportAdmission
from packages.contracts.models import AppendRequest, JournalEntry, Task
from packages.contracts.standing_admission import (
    StandingInitialProof,
    StandingTaskAdmission,
    standing_entry,
    standing_initial_proof,
)
from packages.contracts.standing_agent_settlement import (
    StandingAgentRetirementRegistrationReceipt,
    StandingAgentRetirementRegistrationRecord,
    StandingAgentRetirementRegistrationRequest,
    StandingAgentSettlementQuery,
    StandingAgentSettlementReceipt,
    StandingAgentSettlementRecord,
    StandingAgentSettlementRequest,
    agent_retirement_capability_digest,
    agent_retirement_registration_record_digest,
    agent_settlement_record_digest,
)
from packages.contracts.standing_closure import StandingAdmissionClosure, standing_closure_digest
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionClaimAdmission,
    StandingExecutionIntent,
    capability_digest,
    claim_digest,
    claim_id,
    intent_digest,
    occurrence_id,
    standing_initial_request,
)
from packages.contracts.standing_export import (
    StandingExportAdmission,
    StandingExportRequest,
    standing_export_digest,
)
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingRequest,
    StandingResourceFinalization,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    checked_finalization,
    finalization_bytes,
    resource_finalize_context_digest,
)
from packages.contracts.standing_resources import (
    StandingResourceObservation,
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    TaskResourceQuery,
    checked_resource,
    standing_resource_digest,
    standing_resource_observation_digest,
)
from packages.contracts.standing_work import (
    StandingConflict,
    StandingUnavailable,
    _checked,
    _utc,
    canonical_bytes,
)
from packages.providers.disclosure import combine_scopes, scope_allowed
from services.session.standing_admission import _STANDING_ROWS
from services.session.standing_closure import _TABLES as _CLOSURE_TABLES
from services.session.standing_closure import _sql_identity
from services.session.standing_execution import _native
from services.session.standing_resource_finalization import _TABLES as _FINAL_TABLES
from services.session.standing_resources import _TABLES as _RESOURCE_TABLES

HISTORY_LIMIT = 64
SLOT_BYTES = 3080192
SHARED_BYTES = 4096
BODY_BUDGET = SHARED_BYTES + HISTORY_LIMIT * SLOT_BYTES
_META = "standing_settlement_meta"
_ORIGINS = "standing_claim_origins"
_REGISTRATIONS = "standing_agent_retirement_registrations"
_SETTLEMENTS = "standing_agent_settlements"
_CLAIMS = "standing_execution_claims"
_INITIALS = "standing_task_admissions"
_CLOSURES = "standing_admission_closures"
_RESERVATIONS = "standing_resource_reservations"
_BINDINGS = "standing_resource_bindings"
_REPORTS = "standing_resource_finalizations"
_FRESH = "fresh-v2-prepared"
_LEGACY = "migrated-v1-uncovered"

_SQL = {
    "standing_execution_meta": "CREATE TABLE standing_execution_meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,backend_id TEXT NOT NULL)",
    _CLAIMS: "CREATE TABLE standing_execution_claims(key TEXT PRIMARY KEY,claim_id TEXT NOT NULL UNIQUE,occurrence_id TEXT NOT NULL UNIQUE,responsibility_id TEXT NOT NULL,authorization_revision INTEGER NOT NULL,claim TEXT NOT NULL)",
    _INITIALS: "CREATE TABLE standing_task_admissions(claim_id TEXT PRIMARY KEY,event_id TEXT NOT NULL UNIQUE,sequence INTEGER NOT NULL UNIQUE,proof TEXT NOT NULL)",
    **{name: value[0] for name, value in _CLOSURE_TABLES.items()},
    **{name: value[0] for name, value in _RESOURCE_TABLES.items()},
    **{name: value[0] for name, value in _FINAL_TABLES.items()},
    _META: "CREATE TABLE standing_settlement_meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,backend_id TEXT NOT NULL,history_limit INTEGER NOT NULL,slot_bytes INTEGER NOT NULL,body_budget INTEGER NOT NULL,active_claim_id TEXT)",
    _ORIGINS: "CREATE TABLE standing_claim_origins(claim_id TEXT PRIMARY KEY,origin TEXT NOT NULL,reserved_bytes INTEGER NOT NULL)",
    _REGISTRATIONS: "CREATE TABLE standing_agent_retirement_registrations(claim_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,key TEXT NOT NULL,record TEXT NOT NULL)",
    _SETTLEMENTS: "CREATE TABLE standing_agent_settlements(claim_id TEXT PRIMARY KEY,key TEXT NOT NULL,outcome_event_id TEXT,record TEXT NOT NULL)",
}
_NEW_TABLES = {_META, _ORIGINS, _REGISTRATIONS, _SETTLEMENTS}
_V1_TABLES = set(_SQL) - _NEW_TABLES
_VERSIONS = (
    "standing_execution_meta",
    "standing_admission_closure_meta",
    "standing_resource_meta",
    "standing_resource_finalization_meta",
)
_BODIES = {
    _CLAIMS: ("claim", 147456, StandingExecutionClaim),
    _INITIALS: ("proof", 163840, StandingInitialProof),
    _CLOSURES: ("closure", 180224, StandingAdmissionClosure),
    _RESERVATIONS: ("request", 196608, StandingResourceReservation),
    _BINDINGS: ("request", 163840, StandingResourceBindingRequest),
    _REPORTS: ("report", 802816, StandingResourceFinalization),
    _REGISTRATIONS: ("record", 149504, StandingAgentRetirementRegistrationRecord),
    _SETTLEMENTS: ("record", 973824, StandingAgentSettlementRecord),
}


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _fresh(value, kind):
    if type(value) is not kind:
        raise StandingUnavailable
    return kind.from_json(value.canonical_bytes())


def _native_json(raw, maximum):
    """Bound native outcome JSON before recursive decode; preserve old extras."""
    if type(raw) is not str:
        raise StandingUnavailable
    encoded = raw.encode("utf-8")
    if not 1 <= len(encoded) <= maximum:
        raise StandingUnavailable
    depth = nodes = 0
    quoted = escaped = token = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            if depth + 1 > 20:
                raise StandingUnavailable
            nodes += 1
            quoted, token = True, False
        elif char in "{[":
            nodes += 1
            depth += 1
            token = False
        elif char in "}]":
            depth -= 1
            token = False
        elif char in ",:" or char.isspace():
            token = False
        elif not token:
            if depth + 1 > 20:
                raise StandingUnavailable
            nodes += 1
            token = True
        if depth > 20 or nodes > 33792:
            raise StandingUnavailable

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise StandingUnavailable
            result[key] = value
        return result

    def constant(_):
        raise StandingUnavailable

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    pending = [(result, 1)]
    visited = 0
    while pending:
        value, level = pending.pop()
        visited += 1
        if level > 20 or visited > 33792:
            raise StandingUnavailable
        kind = type(value)
        if kind is dict:
            pending.extend((item, level + 1) for pair in value.items() for item in pair)
        elif kind is list:
            pending.extend((item, level + 1) for item in value)
        elif kind is str:
            value.encode("utf-8")
        elif kind is float and not math.isfinite(value):
            raise StandingUnavailable
        elif kind not in {str, int, float, bool, type(None)}:
            raise StandingUnavailable
    if type(result) is not dict:
        raise StandingUnavailable
    return result


class StandingSettlementOwner:
    def __init__(self, store, control, tasks, resources, finalizations):
        self.store, self.control = store, control
        self.tasks, self.resources, self.finalizations = tasks, resources, finalizations
        self.claims = tasks.claims
        self._standing, self._dispatch = self.claims.owner, self.claims.dispatch
        self._owners = (
            store,
            control,
            tasks,
            resources,
            finalizations,
            self.claims,
            self._standing,
            self._dispatch,
        )
        self._connection, self._lock = store.db, store.lock
        self._registered = self._closed = self._failed = False
        self._backend = self._instance = None

    def close(self) -> None:
        self._closed = True

    def _sql(self, query, args=()):
        if self.store.db is not self._connection or self.store.lock is not self._lock:
            raise StandingUnavailable
        return self._connection.execute(query, args)

    def _one(self, query, args=()):
        return self._sql(query, args).fetchone()

    def _all(self, query, args=()):
        return self._sql(query, args).fetchall()

    def _collaborators(self):
        if (
            self._closed
            or self._failed
            or any(
                current is not original
                for current, original in zip(
                    (
                        self.store,
                        self.control,
                        self.tasks,
                        self.resources,
                        self.finalizations,
                        self.claims,
                        self.claims.owner,
                        self.claims.dispatch,
                    ),
                    self._owners,
                    strict=True,
                )
            )
            or self.store.db is not self._connection
            or self.store.lock is not self._lock
            or self.control.store is not self.store
            or self.tasks.store is not self.store
            or self.tasks.control is not self.control
            or self.tasks.claims is not self.claims
            or self.tasks.owner is not self._standing
            or self.tasks.dispatch is not self._dispatch
            or self.resources.store is not self.store
            or self.resources.control is not self.control
            or self.resources.tasks is not self.tasks
            or self.finalizations.store is not self.store
            or self.finalizations.control is not self.control
            or self.finalizations.tasks is not self.tasks
            or self.finalizations.resources is not self.resources
            or self.resources._finalization_owner is not self.finalizations
            and (self._registered or self.resources._finalization_owner is not None)
            or self.store._agent_submit_unavailable
            or any(part._closed or part._failed for part in self._owners[2:])
        ):
            raise StandingUnavailable
        self.claims._collaborators()
        if self._registered and any(
            getattr(part, "_settlement_owner", None) is not self
            or not part._registered
            or part._backend != self._backend
            or part._instance != self._instance
            for part in (self.claims, self.tasks, self.resources, self.finalizations)
        ):
            raise StandingUnavailable
        if self._registered and getattr(self.store, "_standing_settlement_owner", None) is not self:
            raise StandingUnavailable

    def _available(self):
        if not self._registered or self._closed or self._failed:
            raise StandingUnavailable
        self._collaborators()

    def _poison(self):
        self._failed = True
        self.store._agent_submit_unavailable = True
        try:
            self._connection.close()
        except BaseException:
            pass

    @contextmanager
    def _transaction(self, final=None):
        self._collaborators()
        with self._lock:
            self._collaborators()
            attempted = False
            try:
                if self._connection.in_transaction:
                    self._poison()
                    raise StandingUnavailable
                attempted = True
                self._sql("BEGIN IMMEDIATE")
                yield
                if final is not None:
                    try:
                        final()
                    except StandingConflict:
                        raise StandingUnavailable from None
                self._connection.commit()
            except BaseException:
                if attempted:
                    try:
                        self._connection.rollback()
                    except BaseException:
                        self._poison()
                        raise StandingUnavailable from None
                raise

    def _guard(self, deadline, cancelled):
        if cancelled is not None:
            status = cancelled()
            if type(status) is not bool or status:
                raise StandingUnavailable
        self._available()
        if time.monotonic() >= deadline:
            raise StandingUnavailable

    def _writer(self, meta, *, registering=False):
        self.claims._writer(meta, registering=True)
        if not registering and self._instance != self.control.instance:
            raise StandingConflict

    def _tables(self):
        for name, sql in _SQL.items():
            row = self._one("SELECT type,sql FROM sqlite_schema WHERE name=?", (name,))
            if (
                row is None
                or row["type"] != "table"
                or type(row["sql"]) is not str
                or _sql_identity(row["sql"]) != _sql_identity(sql)
                or self._all(f"PRAGMA foreign_key_list({name})")
                or self._one(
                    "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (name,)
                )
            ):
                raise StandingUnavailable
            expected_columns = []
            for declaration in sql.split("(", 1)[1].rsplit(")", 1)[0].split(","):
                words = declaration.split()
                expected_columns.append(
                    (
                        words[0],
                        words[1],
                        int("NOT NULL" in declaration),
                        None,
                        int("PRIMARY KEY" in declaration),
                        0,
                    )
                )
            actual_columns = [
                tuple(
                    item[key] for key in ("name", "type", "notnull", "dflt_value", "pk", "hidden")
                )
                for item in self._all(f"PRAGMA table_xinfo({name})")
            ]
            if actual_columns != expected_columns:
                raise StandingUnavailable
            expected = (
                {("key",), ("claim_id",), ("occurrence_id",)}
                if name == _CLAIMS
                else {("claim_id",), ("event_id",), ("sequence",)}
                if name == _INITIALS
                else {("claim_id",)}
                if name not in {*_VERSIONS, _META}
                else set()
            )
            found = set()
            indexes = self._all(f"PRAGMA index_list({name})")
            if len(indexes) != len(expected):
                raise StandingUnavailable
            for index in indexes:
                if (
                    index["unique"] != 1
                    or index["partial"] != 0
                    or index["origin"] != "pk"
                    and index["origin"] != "u"
                ):
                    raise StandingUnavailable
                escaped = index["name"].replace('"', '""')
                keys = tuple(
                    (item["name"], item["desc"], item["coll"])
                    for item in self._all(f'PRAGMA index_xinfo("{escaped}")')
                    if item["key"] == 1
                )
                if any(desc != 0 or coll != "BINARY" for _, desc, coll in keys):
                    raise StandingUnavailable
                found.add(tuple(key for key, _, _ in keys))
            if found != expected:
                raise StandingUnavailable

    def _legacy(self, meta, generations):
        claim = self.claims._schema(meta, generations)
        initial = self.tasks._schema(claim)
        closure = self.tasks._closures.observe(meta, claim, initial)
        coverage, reservation = self.resources._schema(meta, claim, initial)
        state = meta, generations, claim, initial, coverage, reservation, closure
        self.finalizations._schema(state)
        return claim, coverage

    @_native
    def register(self) -> None:
        if self._registered or self._closed or self._failed:
            raise StandingUnavailable

        changed = False

        def final():
            current = self._checked_history()
            if changed:
                self._writer(current.meta, registering=True)
            self._collaborators()

        try:
            with self._transaction(final):
                meta, generations, *_ = self.claims._joins()
                present = {
                    row["name"]
                    for row in self._all(
                        "SELECT name FROM sqlite_schema WHERE lower(name) IN ("
                        + ",".join("?" for _ in _SQL)
                        + ")",
                        tuple(_SQL),
                    )
                }
                if not present:
                    changed = True
                    self._writer(meta, registering=True)
                    if self._one(f"SELECT 1 FROM entries WHERE {_STANDING_ROWS} LIMIT 1"):
                        raise StandingUnavailable
                    maximum = self._one("SELECT coalesce(max(sequence),0) FROM entries")[0]
                    if type(maximum) is not int or not 0 <= maximum <= MAX_AGENT_CONTROL_INTEGER:
                        raise StandingUnavailable
                    for sql in _SQL.values():
                        self._sql(sql)
                    for name in _VERSIONS:
                        if name == "standing_resource_meta":
                            self._sql(
                                f"INSERT INTO {name} VALUES(1,2,?,?)", (meta["backend_id"], maximum)
                            )
                        else:
                            self._sql(f"INSERT INTO {name} VALUES(1,2,?)", (meta["backend_id"],))
                    self._sql(
                        f"INSERT INTO {_META} VALUES(1,2,?,?,?,?,NULL)",
                        (meta["backend_id"], HISTORY_LIMIT, SLOT_BYTES, BODY_BUDGET),
                    )
                elif present == _V1_TABLES:
                    changed = True
                    self._writer(meta, registering=True)
                    claim, _ = self._legacy(meta, generations)
                    for name in _NEW_TABLES:
                        self._sql(_SQL[name])
                    for name in _VERSIONS:
                        self._sql(f"UPDATE {name} SET version=2 WHERE id=1")
                    active = None if claim is None else claim.claim_id
                    self._sql(
                        f"INSERT INTO {_META} VALUES(1,2,?,?,?,?,?)",
                        (meta["backend_id"], HISTORY_LIMIT, SLOT_BYTES, BODY_BUDGET, active),
                    )
                    if claim is not None:
                        self._sql(
                            f"INSERT INTO {_ORIGINS} VALUES(?,?,?)", (active, _LEGACY, SLOT_BYTES)
                        )
                elif present != set(_SQL):
                    raise StandingUnavailable
                self._checked_history()
                instance = self.control.instance
            self._collaborators()
            self._backend, self._instance = meta["backend_id"], instance
            self.resources.register_finalization_owner(self.finalizations)
            for part in (self.claims, self.tasks, self.resources, self.finalizations):
                part._settlement_owner = self
                part._backend, part._instance, part._registered = self._backend, instance, True
            self._registered = True
            self.store._standing_settlement_owner = self
        except BaseException:
            self._failed = True
            raise

    def _checked_history(self):
        try:
            return self._history()
        except Exception:
            raise StandingUnavailable from None

    def _initial_history(self):
        self._collaborators()
        meta, generations, gate, activation, producer = self.claims._joins()
        if self._backend is not None and self._backend != meta["backend_id"]:
            raise StandingUnavailable
        self._tables()
        # Check every scalar's SQL storage and byte length before fetching rows.
        for name, sql in _SQL.items():
            conditions = []
            for declaration in sql.split("(", 1)[1].rsplit(")", 1)[0].split(","):
                column, kind = declaration.split()[:2]
                nullable = column in {"active_claim_id", "outcome_event_id"}
                if kind == "INTEGER":
                    check = f"typeof({column})!='integer' OR {column}<0 OR {column}>{MAX_AGENT_CONTROL_INTEGER}"
                else:
                    maximum = (
                        _BODIES[name][1] if name in _BODIES and column == _BODIES[name][0] else 512
                    )
                    check = f"typeof({column})!='text' OR length(CAST({column} AS BLOB)) NOT BETWEEN 1 AND {maximum}"
                conditions.append(
                    f"({column} IS NOT NULL AND ({check}))" if nullable else f"({check})"
                )
            if self._one(f"SELECT 1 FROM {name} WHERE " + " OR ".join(conditions) + " LIMIT 1"):
                raise StandingUnavailable
        shared = 0
        coverage = None
        for name in (*_VERSIONS, _META):
            if self._one(f"SELECT count(*) FROM {name}")[0] != 1:
                raise StandingUnavailable
            row = self._one(f"SELECT * FROM {name}")
            if (
                type(row["id"]) is not int
                or row["id"] != 1
                or type(row["version"]) is not int
                or row["version"] != 2
                or row["backend_id"] != meta["backend_id"]
            ):
                raise StandingUnavailable
            shared += sum(len(str(value).encode("utf-8")) for value in row if value is not None)
            if name == "standing_resource_meta":
                coverage = row["coverage_sequence"]
                if type(coverage) is not int or not 0 <= coverage <= MAX_AGENT_CONTROL_INTEGER:
                    raise StandingUnavailable
        marker = self._one(f"SELECT * FROM {_META}")
        if (
            any(
                type(marker[key]) is not int or marker[key] != value
                for key, value in (
                    ("history_limit", HISTORY_LIMIT),
                    ("slot_bytes", SLOT_BYTES),
                    ("body_budget", BODY_BUDGET),
                )
            )
            or shared > SHARED_BYTES
        ):
            raise StandingUnavailable
        active = marker["active_claim_id"]
        if active is not None and (
            type(active) is not str or re.fullmatch(r"[0-9a-f]{64}", active) is None
        ):
            raise StandingUnavailable
        costs = {}
        for name in (*_BODIES, _ORIGINS):
            count = self._one(f"SELECT count(*) FROM {name}")[0]
            if type(count) is not int or not 0 <= count <= HISTORY_LIMIT:
                raise StandingUnavailable
            if self._one(
                f"SELECT 1 FROM {name} WHERE typeof(claim_id)!='text' OR length(CAST(claim_id AS BLOB))!=64 LIMIT 1"
            ):
                raise StandingUnavailable
            if name in _BODIES:
                field, maximum, _ = _BODIES[name]
                if self._one(
                    f"SELECT 1 FROM {name} WHERE typeof({field})!='text' OR length(CAST({field} AS BLOB)) NOT BETWEEN 1 AND ? LIMIT 1",
                    (maximum,),
                ):
                    raise StandingUnavailable
            columns = [row["name"] for row in self._all(f"PRAGMA table_xinfo({name})")]
            lengths = "+".join(f"coalesce(length(CAST({column} AS BLOB)),0)" for column in columns)
            for row in self._all(f"SELECT claim_id,{lengths} AS bytes FROM {name}"):
                costs[row["claim_id"]] = costs.get(row["claim_id"], 0) + row["bytes"]
        # Bound every native entry body/column before fetching full JSON.
        for table, field in ((_INITIALS, "event_id"), (_SETTLEMENTS, "outcome_event_id")):
            if self._one(
                f"SELECT 1 FROM {table} WHERE {field} IS NOT NULL AND (typeof({field})!='text' OR length(CAST({field} AS BLOB)) NOT BETWEEN 1 AND 512) LIMIT 1"
            ):
                raise StandingUnavailable
            for row in self._all(
                f"SELECT claim_id,{field} AS event_id FROM {table} WHERE {field} IS NOT NULL"
            ):
                native = self._one(
                    "SELECT typeof(request) AS rt,typeof(value) AS vt,length(CAST(request AS BLOB)) AS r,length(CAST(value AS BLOB)) AS v FROM entries WHERE event_id=?",
                    (row["event_id"],),
                )
                if (
                    native is None
                    or native["rt"] != "text"
                    or native["vt"] != "text"
                    or not 1 <= native["r"] <= 65536
                    or not 1 <= native["v"] <= 69632
                ):
                    raise StandingUnavailable
                costs[row["claim_id"]] += native["r"] + native["v"] + 4096
        if (
            any(value > SLOT_BYTES for value in costs.values())
            or sum(costs.values()) + shared > BODY_BUDGET
        ):
            raise StandingUnavailable
        members = {}
        keys = set()
        occurrences = set()
        task_ids = set()
        for row in self._all(f"SELECT * FROM {_CLAIMS} ORDER BY claim_id"):
            claim = StandingExecutionClaim.from_json(row["claim"].encode("utf-8"))
            request = claim.request
            if (
                canonical_bytes(claim).decode("utf-8") != row["claim"]
                or row["claim_id"] != claim.claim_id
                or row["key"] != request.idempotency_key
                or row["occurrence_id"] != claim.occurrence_id
                or row["responsibility_id"] != request.responsibility_id
                or type(row["authorization_revision"]) is not int
                or row["authorization_revision"] != request.authorization_revision
                or request.expected_backend != meta["backend_id"]
                or row["key"] in keys
                or row["occurrence_id"] in occurrences
                or request.task.task_id in task_ids
            ):
                raise StandingUnavailable
            keys.add(row["key"])
            occurrences.add(row["occurrence_id"])
            task_ids.add(request.task.task_id)
            self.claims._historical(claim, meta, generations)
            members[claim.claim_id] = SimpleNamespace(
                claim=claim,
                initial=None,
                closure=None,
                reservation=None,
                binding=None,
                report=None,
                registration=None,
                settlement=None,
                origin=None,
            )
        for row in self._all(f"SELECT * FROM {_ORIGINS} ORDER BY claim_id"):
            item = self._member(members, row["claim_id"])
            if (
                row["origin"] not in {_FRESH, _LEGACY}
                or type(row["reserved_bytes"]) is not int
                or row["reserved_bytes"] != SLOT_BYTES
            ):
                raise StandingUnavailable
            item.origin = row["origin"]
        if any(item.origin is None for item in members.values()) or len(costs) != len(members):
            raise StandingUnavailable
        for item in members.values():
            if item.origin == _FRESH:
                self.tasks._task_available(item.claim.request.task.task_id)
        if (
            self._one(f"SELECT count(*) FROM entries WHERE {_STANDING_ROWS}")[0]
            != self._one(f"SELECT count(*) FROM {_INITIALS}")[0]
        ):
            raise StandingUnavailable
        for row in self._all(f"SELECT * FROM {_INITIALS} ORDER BY claim_id"):
            item = self._member(members, row["claim_id"])
            item.initial = self.tasks._proof(row, item.claim)
        settled = {row["claim_id"] for row in self._all(f"SELECT claim_id FROM {_SETTLEMENTS}")}
        if (
            not settled <= members.keys()
            or active is not None
            and active not in members
            or set(members) - settled != (set() if active is None else {active})
        ):
            raise StandingUnavailable
        return SimpleNamespace(
            meta=meta,
            generations=generations,
            gate=gate,
            activation=activation,
            producer=producer,
            members=members,
            active=active,
            coverage=coverage,
        )

    def _history(self):
        from packages.contracts import standing_agent_settlement as agent_codec
        from packages.contracts import standing_resource_finalization as resource_codec

        # Only immediately parsed new-family roots use these encoders. Each
        # call still walks the complete actual graph; old bridges stay below.
        def fresh_agent_bytes(value):
            try:
                return agent_codec._encode(value)
            except agent_codec._FAILURES:
                raise ValueError(agent_codec._ERROR) from None

        def fresh_resource_bytes(value):
            try:
                return resource_codec._encode(value)
            except resource_codec._FAILURES:
                raise ValueError(resource_codec._ERROR) from None

        history = self._initial_history()
        meta, generations, coverage = history.meta, history.generations, history.coverage
        members, active = history.members, history.active
        fields = (
            (_CLOSURES, "closure", "closure"),
            (_RESERVATIONS, "request", "reservation"),
            (_BINDINGS, "request", "binding"),
            (_REPORTS, "report", "report"),
            (_REGISTRATIONS, "record", "registration"),
            (_SETTLEMENTS, "record", "settlement"),
        )
        for table, field, attribute in fields:
            for row in self._all(f"SELECT * FROM {table} ORDER BY claim_id"):
                item = self._member(members, row["claim_id"])
                kind = _BODIES[table][2]
                value = kind.from_json(row[field].encode("utf-8"))
                wire = (
                    fresh_agent_bytes(value)
                    if table in {_REGISTRATIONS, _SETTLEMENTS}
                    else fresh_resource_bytes(value)
                    if table in {_BINDINGS, _REPORTS}
                    else canonical_bytes(value)
                )
                if wire.decode("utf-8") != row[field]:
                    raise StandingUnavailable
                setattr(item, attribute, value)
                self._join_member(item, meta, generations, coverage, attribute)
                if table == _REGISTRATIONS and (
                    row["owner_id"] != value.request.owner_id
                    or row["key"] != value.request.idempotency_key
                ):
                    raise StandingUnavailable
                if table == _SETTLEMENTS:
                    expected = value.request.work.outcome
                    if row["key"] != value.request.idempotency_key or row["outcome_event_id"] != (
                        None if expected is None else expected.event_id
                    ):
                        raise StandingUnavailable
        unsettled = [key for key, item in members.items() if item.settlement is None]
        for item in members.values():
            if (
                item.origin == _FRESH
                and item.registration is None
                and any(
                    value is not None
                    for value in (
                        item.initial,
                        item.binding,
                        item.reservation,
                        item.report,
                        item.settlement,
                    )
                )
            ):
                raise StandingUnavailable
            if item.origin == _LEGACY and (
                item.registration is not None or item.settlement is not None
            ):
                raise StandingUnavailable
            if item.origin == _FRESH and item.reservation is not None and item.binding is None:
                raise StandingUnavailable
        if unsettled != ([] if active is None else [active]):
            raise StandingUnavailable
        return history

    @staticmethod
    def _member(members, key):
        if key not in members:
            raise StandingUnavailable
        return members[key]

    def _join_member(self, item, meta, generations, coverage, field):
        original = item.claim.request
        if field == "closure":
            closure = item.closure.validate_request(original)
            if (closure.initial is None) != (item.initial is None) or (
                item.initial is not None
                and canonical_bytes(closure.initial) != canonical_bytes(item.initial)
            ):
                raise StandingUnavailable
        elif field == "reservation":
            value = item.reservation
            if (
                item.initial is None
                or canonical_bytes(value.context.standing) != canonical_bytes(item.initial)
                or value.context.backend_id != meta["backend_id"]
            ):
                raise StandingUnavailable
        elif field == "binding":
            value = item.binding
            if finalization_bytes(value.claim) != finalization_bytes(item.claim):
                raise StandingUnavailable
            if item.reservation is not None and (
                item.reservation.resource_backend != value.resource_backend
                or item.reservation.resource_instance != value.resource_instance
            ):
                raise StandingUnavailable
        elif field == "report":
            expected = self._context(item.report.context.request, item, meta, generations, coverage)
            if finalization_bytes(expected) != finalization_bytes(item.report.context):
                raise StandingUnavailable
        elif field == "registration":
            value = item.registration
            if (
                item.origin != _FRESH
                or value.claim_origin != _FRESH
                or value.backend_id != meta["backend_id"]
                or value.registered_instance != original.expected_instance
                or finalization_bytes(value.request.claim) != finalization_bytes(item.claim)
            ):
                raise StandingUnavailable
            self.tasks.dispatch._known(value.registered_instance, generations, stored=True)
        elif field == "settlement":
            value = item.settlement
            if value.backend_id != meta["backend_id"]:
                raise StandingUnavailable
            self.tasks.dispatch._known(value.settled_instance, generations, stored=True)
            self._settlement_joins(item, value.request)

    def _observation(self, item, coverage):
        original = item.claim.request
        if item.closure is None:
            raise StandingConflict
        sequence = None if item.initial is None else item.initial.sequence
        body = dict(
            schema_version="1.0",
            intent_digest=intent_digest(original),
            claim_id=claim_id(original),
            closure_digest=item.closure.closure_digest,
            coverage_sequence=coverage,
            accepted_sequence=sequence,
            coverage="accepted-after-bootstrap"
            if sequence is not None and sequence > coverage
            else "legacy-or-unadmitted",
            reservation=item.reservation,
        )
        return StandingResourceObservation(
            **body, observation_digest=standing_resource_observation_digest(body)
        ).validate_request(original)

    def _context(self, request, item, meta, generations, coverage):
        if request.original.expected_backend != meta["backend_id"]:
            raise StandingConflict
        self.tasks.dispatch._known(request.original.expected_instance, generations)
        if (
            finalization_bytes(request.original) != finalization_bytes(item.claim.request)
            or item.binding is None
            or item.closure is None
            or request.expected_resource_backend != item.binding.resource_backend
            or request.expected_resource_instance != item.binding.resource_instance
        ):
            raise StandingConflict
        observation = self._observation(item, coverage)
        # The complete constructor rebuilds every child and verifies the full
        # request/claim/initial/reservation joins on these fresh locals.
        return StandingResourceFinalizeContext(
            schema_version="1.0",
            request=request,
            binding=item.binding,
            closure=item.closure,
            observation=observation,
            context_digest=resource_finalize_context_digest(
                request, item.binding, item.closure, observation
            ),
        )

    def _settlement_joins(self, item, request):
        if (
            item.registration is None
            or item.report is None
            or item.closure is None
            or request.registration.canonical_bytes() != item.registration.canonical_bytes()
            or finalization_bytes(request.resources.finalization) != finalization_bytes(item.report)
        ):
            raise StandingConflict
        if request.work.kind == "bound-never-admitted":
            if item.initial is not None:
                raise StandingConflict
        elif item.initial is None:
            raise StandingConflict
        if request.work.kind == "launched-outcome-known":
            self._outcome(item, request.work.outcome)

    def _outcome(self, item, reference):
        if reference is None or item.initial is None:
            raise StandingUnavailable
        row = self.tasks._entry(reference.event_id)
        request = _native_json(row["request"], 65536)
        value = _native_json(row["value"], 69632)
        if set(request) != set(AppendRequest.model_fields) or set(value) != set(
            JournalEntry.model_fields
        ):
            raise StandingUnavailable
        request_wire, entry_wire = _json_bytes(request), _json_bytes(value)
        if len(request_wire) > 65536 or len(entry_wire) > 69632:
            raise StandingUnavailable
        entry = JournalEntry.model_validate_json(entry_wire, strict=True)
        AppendRequest.model_validate_json(request_wire, strict=True)
        body = value["payload"]
        snapshot = body.get("task")
        if type(snapshot) is not dict or set(snapshot) != set(Task.model_fields):
            raise StandingUnavailable
        task = Task.model_validate_json(_json_bytes(snapshot), strict=True)
        if task.model_dump(mode="json") != snapshot:
            raise StandingUnavailable
        original = item.claim.request.task
        session = original.task_session
        if (
            _json_bytes({key: value[key] for key in request}) != request_wire
            or row["event_id"] != entry.event_id
            or row["sequence"] != entry.sequence
            or row["session_id"] != entry.session_id
            or row["thread_id"] != entry.thread_id
            or row["epoch"] != entry.epoch
            or row["kind"] != entry.kind
            or row["idem"] != entry.idempotency_key
            or entry.created_at.tzinfo is None
            or entry.created_at.utcoffset() != timedelta(0)
        ):
            raise StandingUnavailable
        if (
            type(task.epoch) is not int
            or type(task.objective_revision) is not int
            or not 1 <= task.objective_revision <= MAX_AGENT_CONTROL_INTEGER
        ):
            raise StandingUnavailable
        # A complete native row can be valid but inapplicable to this claim.
        # Retained history still normalizes any such join failure to unavailable.
        if (
            row["writer"] != "agent"
            or entry.origin != "agent-supervisor"
            or entry.kind != "agent.outcome"
            or (entry.session_id, entry.thread_id, entry.epoch)
            != (session.session_id, session.thread_id, session.epoch)
            or entry.task_refs != [original.task_id]
            or entry.sequence <= item.initial.sequence
        ):
            raise StandingConflict
        if (
            reference.event_id != entry.event_id
            or reference.sequence != entry.sequence
            or reference.task_id != task.task_id
            or reference.objective_revision != task.objective_revision
            or reference.state != task.state.value
            or reference.request_sha256 != hashlib.sha256(request_wire).hexdigest()
            or reference.entry_sha256 != hashlib.sha256(entry_wire).hexdigest()
        ):
            raise StandingConflict
        if (
            task.task_id != original.task_id
            or (task.session_id, task.thread_id, task.epoch)
            != (session.session_id, session.thread_id, session.epoch)
            or task.harness_version != original.harness_version
            or task.workspace_pin != original.workspace_pin
            or task.idempotency_key != "standing:" + item.claim.occurrence_id
            or task.auxiliary_models != {}
        ):
            raise StandingConflict
        # Compare the complete validated declaration, preserving old nullable
        # catalog-pin wire omissions without changing their stored bytes.
        if task.model_pin is None or task.model_pin.__dict__ != original.model_pin.__dict__:
            raise StandingConflict
        mirrored = (
            "task_id",
            "session_id",
            "thread_id",
            "epoch",
            "objective",
            "objective_revision",
            "idempotency_key",
            "source_event_ids",
            "content_scope",
            "evidence",
            "state",
        )
        if any(key not in body for key in mirrored) or _json_bytes(
            {key: body[key] for key in mirrored}
        ) != _json_bytes({key: snapshot[key] for key in mirrored}):
            raise StandingConflict
        optional = tuple(
            key for key in ("summary", "error", "remaining_required_work") if key in body
        )
        if _json_bytes({key: body[key] for key in optional}) != _json_bytes(
            {key: snapshot[key] for key in optional}
        ):
            raise StandingConflict
        return entry

    def _select(self, history, original, *, missing=False):
        if original.expected_backend != history.meta["backend_id"]:
            raise StandingConflict
        self.tasks.dispatch._known(original.expected_instance, history.generations)
        item = next(
            (
                item
                for item in history.members.values()
                if item.claim.request.idempotency_key == original.idempotency_key
            ),
            None,
        )
        if item is None:
            if missing:
                return None
            raise StandingConflict
        if canonical_bytes(item.claim.request) != canonical_bytes(original):
            raise StandingConflict
        return item

    def _active(self, history, item):
        if history.active != item.claim.claim_id or item.settlement is not None:
            raise StandingConflict

    def _launch(self, history, request, deadline, cancelled, *, claim=None):
        self._guard(deadline, cancelled)
        self._writer(history.meta)
        self.tasks.dispatch._known(request.expected_instance, history.generations)
        if (
            request.expected_backend != history.meta["backend_id"]
            or request.expected_instance != history.meta["instance_id"]
        ):
            raise StandingConflict
        record = self.claims.owner._current_record(request.responsibility_id, history.meta)
        if record is None or record.receipt.revision != request.authorization_revision:
            raise StandingConflict
        latest = self.tasks.dispatch._producer_row(
            history.producer, history.meta, history.generations
        )
        if (
            history.meta["mode"] != "autonomous"
            or history.gate["epoch"] != request.dispatch_epoch
            or history.gate["epoch"] <= history.gate["stopped_through_epoch"]
            or history.activation is None
            or history.activation.activation_digest != request.activation_digest
            or history.activation.mode_receipt.revision != request.control_revision
            or history.meta["revision"] != request.control_revision
            or latest is None
        ):
            raise StandingConflict
        if claim is None:
            if canonical_bytes(latest) != canonical_bytes(request.producer):
                raise StandingConflict
        elif (
            latest.request.producer_id != request.producer.request.producer_id
            or latest.request.expected_instance != request.expected_instance
            or latest.request.sequence < request.producer.request.sequence
            or (
                latest.request.sequence == request.producer.request.sequence
                and canonical_bytes(latest) != canonical_bytes(request.producer)
            )
        ):
            raise StandingConflict
        self.claims.owner._agent_session(record.receipt.request)
        now = self.claims._clock()
        if claim is not None and _utc(now) < _utc(claim.prepared_utc):
            raise StandingUnavailable
        self.claims._grant(request, record, now)
        if not self.tasks.dispatch._live(
            history.meta, history.producer, self.tasks.dispatch._sample()
        ):
            raise StandingConflict
        self._writer(history.meta)
        return now

    def _capability(self, original, capability):
        if (
            type(capability) is not bytes
            or len(capability) != 32
            or not hmac.compare_digest(
                capability_digest(
                    original.expected_backend,
                    original.idempotency_key,
                    original.executor_id,
                    capability,
                ),
                original.capability_sha256,
            )
        ):
            raise StandingConflict

    @_native
    def prepare(self, request, *, deadline=None, cancelled=None):
        request = _checked(request, StandingExecutionIntent)
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)
        created = False

        def final():
            history = self._checked_history()
            item = self._select(history, request)
            if created:
                self._active(history, item)
                self.tasks._task_available(request.task.task_id)
                if _utc(self._launch(history, request, deadline, cancelled)) < _utc(
                    item.claim.prepared_utc
                ):
                    raise StandingUnavailable
            self._guard(deadline, cancelled)

        with self._transaction(final):
            history = self._checked_history()
            item = self._select(history, request, missing=True)
            if item is not None:
                result = StandingExecutionClaimAdmission(
                    schema_version="1.0", disposition="retained", claim=item.claim
                ).validate_request(request)
            else:
                if history.active is not None or any(
                    item.claim.occurrence_id == occurrence_id(request)
                    or item.claim.request.task.task_id == request.task.task_id
                    for item in history.members.values()
                ):
                    raise StandingConflict
                prepared = self._launch(history, request, deadline, cancelled)
                self.tasks._task_available(request.task.task_id)
                if len(history.members) >= HISTORY_LIMIT:
                    raise StandingUnavailable
                claim = StandingExecutionClaim(
                    schema_version="1.0",
                    request=request,
                    claim_id=claim_id(request),
                    occurrence_id=occurrence_id(request),
                    intent_digest=intent_digest(request),
                    prepared_utc=prepared,
                    claim_digest=claim_digest(request, prepared),
                )
                standing_initial_request(claim)
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_CLAIMS} VALUES(?,?,?,?,?,?)",
                    (
                        request.idempotency_key,
                        claim.claim_id,
                        claim.occurrence_id,
                        request.responsibility_id,
                        request.authorization_revision,
                        canonical_bytes(claim).decode("utf-8"),
                    ),
                )
                self._sql(
                    f"INSERT INTO {_ORIGINS} VALUES(?,?,?)", (claim.claim_id, _FRESH, SLOT_BYTES)
                )
                if (
                    self._sql(
                        f"UPDATE {_META} SET active_claim_id=? WHERE id=1 AND active_claim_id IS NULL",
                        (claim.claim_id,),
                    ).rowcount
                    != 1
                ):
                    raise StandingUnavailable
                created = True
                result = StandingExecutionClaimAdmission(
                    schema_version="1.0", disposition="created", claim=claim
                ).validate_request(request)
        self._guard(deadline, cancelled)
        return result

    @_native
    def claim_lookup(self, request):
        request = _checked(request, StandingExecutionIntent)
        self._available()
        with self._transaction(self._available):
            item = self._select(self._checked_history(), request, missing=True)
            return None if item is None else item.claim.validate_request(request)

    @_native
    def unresolved(self):
        self._available()
        with self._transaction(self._available):
            history = self._checked_history()
            return None if history.active is None else history.members[history.active].claim

    @_native
    def admit(self, request, *, capability, deadline=None, cancelled=None):
        request = _checked(request, StandingExecutionIntent)
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)
        created = False

        def admission(history, item):
            self._active(history, item)
            if item.registration is None or item.closure is not None:
                raise StandingConflict
            self._launch(history, request, deadline, cancelled, claim=item.claim)

        def final():
            history = self._checked_history()
            item = self._select(history, request)
            if item.initial is None:
                raise StandingUnavailable
            if created:
                admission(history, item)
            self._guard(deadline, cancelled)

        with self._transaction(final):
            history = self._checked_history()
            item = self._select(history, request)
            if item.initial is None:
                self._capability(request, capability)
                admission(history, item)
                self.tasks._task_available(request.task.task_id)
                initial = standing_initial_request(item.claim)
                self._guard(deadline, cancelled)
                entry = self.store._append_entry_locked(initial, "standing-supervisor")
                proof = standing_initial_proof(item.claim, standing_entry(entry, item.claim))
                self._sql(
                    f"INSERT INTO {_INITIALS} VALUES(?,?,?,?)",
                    (
                        item.claim.claim_id,
                        proof.event_id,
                        proof.sequence,
                        canonical_bytes(proof).decode("utf-8"),
                    ),
                )
                created = True
            else:
                proof = item.initial
            result = StandingTaskAdmission(
                schema_version="1.0", disposition="created" if created else "retained", proof=proof
            ).validate_request(request)
        self._guard(deadline, cancelled)
        return result

    @_native
    def initial_lookup(self, request):
        request = _checked(request, StandingExecutionIntent)
        self._available()
        with self._transaction(self._available):
            item = self._select(self._checked_history(), request, missing=True)
            return (
                None
                if item is None or item.initial is None
                else item.initial.validate_request(request)
            )

    @_native
    def close_admission(self, original, *, capability, deadline=None, cancelled=None):
        original = _checked(original, StandingExecutionIntent)
        self._capability(original, capability)
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)
        created = False

        def final():
            history = self._checked_history()
            item = self._select(history, original)
            if item.closure is None:
                raise StandingUnavailable
            if created:
                self._writer(history.meta)
            self._guard(deadline, cancelled)

        with self._transaction(final):
            history = self._checked_history()
            item = self._select(history, original)
            closure = item.closure
            if closure is None:
                self._writer(history.meta)
                closure = StandingAdmissionClosure(
                    schema_version="1.0",
                    intent_digest=intent_digest(original),
                    claim_id=item.claim.claim_id,
                    initial=item.initial,
                    closure_digest=standing_closure_digest(original, item.initial),
                ).validate_request(original)
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_CLOSURES} VALUES(?,?)",
                    (item.claim.claim_id, canonical_bytes(closure).decode("utf-8")),
                )
                created = True
        self._guard(deadline, cancelled)
        return closure

    @_native
    def closure_lookup(self, original):
        original = _checked(original, StandingExecutionIntent)
        self._available()
        with self._transaction(self._available):
            item = self._select(self._checked_history(), original, missing=True)
            return (
                None
                if item is None or item.closure is None
                else item.closure.validate_request(original)
            )

    @_native
    def export_admission(self, original, *, deadline=None, cancelled=None):
        if type(original) is not StandingExportRequest:
            raise StandingUnavailable
        original = _checked(original, StandingExportRequest)
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)

        def observe(history):
            item = self._select(history, original.initial.claim.request)
            self._active(history, item)
            if item.closure is not None or canonical_bytes(item.initial) != canonical_bytes(
                original.initial
            ):
                raise StandingConflict
            record = self.claims.owner._record(
                item.claim.request.responsibility_id,
                item.claim.request.authorization_revision,
                history.meta,
            )
            self.claims.owner._agent_session(record.receipt.request)
            return item.initial

        def final():
            observe(self._checked_history())
            self._guard(deadline, cancelled)

        with self._transaction(final):
            proof = observe(self._checked_history())
            effective = combine_scopes(
                proof.claim.request.task.content_scope, original.export.content_scope
            )
            if not scope_allowed(original.export.provider, effective):
                raise StandingConflict
            sequence = self._one("SELECT coalesce(max(sequence),0) FROM entries")[0]
            if type(sequence) is not int or not 0 <= sequence <= MAX_AGENT_CONTROL_INTEGER:
                raise StandingUnavailable
            admitted = AgentExportAdmission(
                **original.export.model_dump(mode="python"),
                binding_sha256=original.export.binding_digest(),
                effective_scope=effective,
                journal_sequence=sequence,
            )
            result = StandingExportAdmission(
                schema_version="1.0",
                request_digest=standing_export_digest(original),
                admission=admitted,
            ).validate_request(original)
            self._guard(deadline, cancelled)
        self._guard(deadline, cancelled)
        return result

    def ordinary_export_link(self, task_id):
        """Called under Store's same native lock; complete history precedes grant."""
        self._available()
        try:
            # Ordinary disclosure needs every claim/initial/native-entry link,
            # not unrelated physical reports or native outcome bodies.
            history = self._initial_history()
        except Exception:
            raise StandingUnavailable from None
        return any(
            (item.origin == _FRESH or item.initial is not None)
            and item.claim.request.task.task_id == task_id
            for item in history.members.values()
        )

    def blocks_admission(self):
        self._available()
        history = self._checked_history()
        return history.active is not None and history.members[history.active].closure is not None

    def closure_observe(self, meta, claim, initial):
        self._available()
        history = self._checked_history()
        if dict(meta) != dict(history.meta):
            raise StandingUnavailable
        if claim is None:
            if initial is not None:
                raise StandingUnavailable
            return None
        claim = _checked(claim, StandingExecutionClaim)
        item = self._select(history, claim.request)
        if (
            canonical_bytes(item.claim) != canonical_bytes(claim)
            or (item.initial is None) != (initial is None)
            or (
                item.initial is not None
                and canonical_bytes(item.initial) != canonical_bytes(initial)
            )
        ):
            raise StandingUnavailable
        return item.closure

    @_native
    def resource_context(self, query):
        query = checked_resource(query, TaskResourceQuery)
        self._available()
        with self._transaction(self._available):
            history = self._checked_history()
            item = next(
                (
                    item
                    for item in history.members.values()
                    if item.claim.request.task.task_id == query.task_id
                ),
                None,
            )
            return self.resources._context(
                query,
                history.meta,
                None if item is None else item.claim,
                None if item is None else item.initial,
            )

    def _reservation_authority(self, history, item, original):
        self._active(history, item)
        if (
            item.initial is None
            or item.registration is None
            or item.binding is None
            or item.closure is not None
        ):
            raise StandingConflict
        expected = self.resources._context(
            original.context.query, history.meta, item.claim, item.initial
        )
        if (
            canonical_bytes(expected) != canonical_bytes(original.context)
            or original.resource_backend != item.binding.resource_backend
            or original.resource_instance != item.binding.resource_instance
        ):
            raise StandingConflict
        task = item.claim.request.task.task_session
        session = self.store.session(task.session_id)
        if session.channel != "agent" or (session.thread_id, session.epoch) != (
            task.thread_id,
            task.epoch,
        ):
            raise StandingConflict
        self._writer(history.meta)

    @_native
    def reserve(self, original, *, deadline=None, cancelled=None):
        original = checked_resource(original, StandingResourceReservation)
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)
        if original.context.standing is None:
            raise StandingConflict
        request = original.context.standing.claim.request
        created = False

        def final():
            history = self._checked_history()
            item = self._select(history, request)
            if item.reservation is None or canonical_bytes(item.reservation) != canonical_bytes(
                original
            ):
                raise StandingUnavailable
            if created:
                self._reservation_authority(history, item, original)
            self._guard(deadline, cancelled)

        with self._transaction(final):
            history = self._checked_history()
            item = self._select(history, request)
            if item.reservation is None:
                self._reservation_authority(history, item, original)
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_RESERVATIONS} VALUES(?,?)",
                    (item.claim.claim_id, canonical_bytes(original).decode("utf-8")),
                )
                created = True
            elif canonical_bytes(item.reservation) != canonical_bytes(original):
                raise StandingConflict
            result = StandingResourceReservationReceipt(
                schema_version="1.0",
                disposition="created" if created else "retained",
                request=original,
                request_digest=standing_resource_digest(original),
            ).validate_request(original)
        self._guard(deadline, cancelled)
        return result

    @_native
    def resource_observe(self, original):
        original = checked_resource(original, StandingExecutionIntent)
        self._available()
        with self._transaction(self._available):
            history = self._checked_history()
            return self._observation(self._select(history, original), history.coverage)

    def check_reservation(self, original):
        self._available()
        if not self._connection.in_transaction:
            raise StandingUnavailable
        history = self._checked_history()
        if original.context.standing is None:
            raise StandingConflict
        item = self._select(history, original.context.standing.claim.request)
        self._reservation_authority(history, item, original)

    def _binding_authority(self, history, item, original):
        self._active(history, item)
        if (
            item.registration is None
            or item.initial is not None
            or item.closure is not None
            or item.origin != _FRESH
        ):
            raise StandingConflict
        if finalization_bytes(item.claim) != finalization_bytes(original.claim):
            raise StandingConflict
        self._writer(history.meta)

    @_native
    def bind(self, request, *, deadline=None, cancelled=None):
        original = checked_finalization(request, StandingResourceBindingRequest)
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)
        created = False

        def final():
            history = self._checked_history()
            item = self._select(history, original.claim.request)
            if item.binding is None or finalization_bytes(item.binding) != finalization_bytes(
                original
            ):
                raise StandingUnavailable
            if created:
                self._binding_authority(history, item, original)
            self._guard(deadline, cancelled)

        with self._transaction(final):
            history = self._checked_history()
            item = self._select(history, original.claim.request)
            if finalization_bytes(item.claim) != finalization_bytes(original.claim):
                raise StandingConflict
            if item.binding is None:
                self._binding_authority(history, item, original)
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_BINDINGS} VALUES(?,?)",
                    (item.claim.claim_id, finalization_bytes(original).decode("utf-8")),
                )
                created = True
            elif finalization_bytes(item.binding) != finalization_bytes(original):
                raise StandingConflict
            result = self.finalizations._binding_receipt(
                original, "created" if created else "retained"
            )
        self._guard(deadline, cancelled)
        return result

    @_native
    def binding_lookup(self, request):
        original = checked_finalization(request, StandingResourceBindingRequest)
        self._available()
        with self._transaction(self._available):
            history = self._checked_history()
            item = self._select(history, original.claim.request)
            if finalization_bytes(item.claim) != finalization_bytes(original.claim):
                raise StandingConflict
            if item.binding is None:
                return None
            if finalization_bytes(item.binding) != finalization_bytes(original):
                raise StandingConflict
            return self.finalizations._binding_receipt(item.binding, "retained")

    @_native
    def finalization_context(self, request):
        original = checked_finalization(request, StandingResourceFinalizeRequest)
        self._available()
        with self._transaction(self._available):
            history = self._checked_history()
            return self._context(
                original,
                self._select(history, original.original),
                history.meta,
                history.generations,
                history.coverage,
            )

    @_native
    def report(self, finalization, *, deadline=None, cancelled=None):
        original = checked_finalization(finalization, StandingResourceFinalization)
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)
        created = False

        def expected(history):
            item = self._select(history, original.context.request.original)
            context = self._context(
                original.context.request, item, history.meta, history.generations, history.coverage
            )
            if finalization_bytes(context) != finalization_bytes(original.context):
                raise StandingConflict
            return item

        def final():
            history = self._checked_history()
            # Fresh history already joins its retained report to native state.
            # Full report equality below binds that context to this request.
            item = self._select(history, original.context.request.original)
            if item.report is None or finalization_bytes(item.report) != finalization_bytes(
                original
            ):
                raise StandingUnavailable
            if created:
                self._writer(history.meta)
            self._guard(deadline, cancelled)

        with self._transaction(final):
            history = self._checked_history()
            item = expected(history)
            if item.report is None:
                self._writer(history.meta)
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_REPORTS} VALUES(?,?)",
                    (item.claim.claim_id, finalization_bytes(original).decode("utf-8")),
                )
                created = True
            elif finalization_bytes(item.report) != finalization_bytes(original):
                raise StandingConflict
            result = self.finalizations._receipt(original, "created" if created else "retained")
        self._guard(deadline, cancelled)
        return result

    @_native
    def finalization_lookup(self, request):
        original = checked_finalization(request, StandingResourceFinalizeRequest)
        self._available()
        with self._transaction(self._available):
            history = self._checked_history()
            item = self._select(history, original.original)
            if item.binding is not None and (
                item.binding.resource_backend != original.expected_resource_backend
                or item.binding.resource_instance != original.expected_resource_instance
            ):
                raise StandingConflict
            if item.report is None:
                return None
            if finalization_bytes(item.report.context.request) != finalization_bytes(original):
                raise StandingConflict
            return self.finalizations._receipt(item.report, "retained")

    def _registration_authority(self, history, item):
        self._active(history, item)
        if (
            item.origin != _FRESH
            or item.claim.request.expected_instance != history.meta["instance_id"]
            or any(
                value is not None
                for value in (
                    item.initial,
                    item.closure,
                    item.binding,
                    item.reservation,
                    item.report,
                    item.settlement,
                )
            )
        ):
            raise StandingConflict
        self._writer(history.meta)

    @_native
    def register_agent(
        self, request: StandingAgentRetirementRegistrationRequest, *, deadline=None, cancelled=None
    ) -> StandingAgentRetirementRegistrationReceipt:
        original = _fresh(request, StandingAgentRetirementRegistrationRequest)
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)
        created = False

        def final():
            history = self._checked_history()
            item = self._select(history, original.claim.request)
            if (
                item.registration is None
                or item.registration.canonical_bytes() != record.canonical_bytes()
            ):
                raise StandingUnavailable
            if created:
                self._registration_authority(history, item)
            self._guard(deadline, cancelled)

        with self._transaction(final):
            history = self._checked_history()
            item = self._select(history, original.claim.request)
            if finalization_bytes(item.claim) != finalization_bytes(original.claim):
                raise StandingConflict
            record = item.registration
            if record is None:
                self._registration_authority(history, item)
                instant = self.claims._clock()
                record = StandingAgentRetirementRegistrationRecord(
                    schema_version="1.0",
                    request=original,
                    backend_id=history.meta["backend_id"],
                    registered_instance=history.meta["instance_id"],
                    registered_utc=instant,
                    claim_origin=_FRESH,
                    record_digest=agent_retirement_registration_record_digest(
                        original,
                        history.meta["backend_id"],
                        history.meta["instance_id"],
                        instant,
                        _FRESH,
                    ),
                )
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_REGISTRATIONS} VALUES(?,?,?,?)",
                    (
                        item.claim.claim_id,
                        original.owner_id,
                        original.idempotency_key,
                        record.canonical_bytes().decode("utf-8"),
                    ),
                )
                created = True
            elif record.request.canonical_bytes() != original.canonical_bytes():
                raise StandingConflict
            result = StandingAgentRetirementRegistrationReceipt(
                schema_version="1.0",
                disposition="created" if created else "retained",
                registration=record,
            ).validate_request(original)
        self._guard(deadline, cancelled)
        return result

    @_native
    def registration_lookup(
        self, request: StandingAgentRetirementRegistrationRequest
    ) -> StandingAgentRetirementRegistrationReceipt | None:
        original = _fresh(request, StandingAgentRetirementRegistrationRequest)
        self._available()
        with self._transaction(self._available):
            item = self._select(self._checked_history(), original.claim.request)
            if finalization_bytes(item.claim) != finalization_bytes(original.claim):
                raise StandingConflict
            if item.registration is None:
                return None
            if item.registration.request.canonical_bytes() != original.canonical_bytes():
                raise StandingConflict
            return StandingAgentRetirementRegistrationReceipt(
                schema_version="1.0", disposition="retained", registration=item.registration
            ).validate_request(original)

    @_native
    def settle(
        self,
        request: StandingAgentSettlementRequest,
        secret32: bytes,
        *,
        deadline=None,
        cancelled=None,
    ) -> StandingAgentSettlementReceipt:
        original = _fresh(request, StandingAgentSettlementRequest)
        registration = original.registration
        claim = registration.request.claim
        if (
            type(secret32) is not bytes
            or len(secret32) != 32
            or not hmac.compare_digest(
                agent_retirement_capability_digest(
                    claim,
                    registration.request.owner_id,
                    registration.request.idempotency_key,
                    secret32,
                ),
                registration.request.capability_sha256,
            )
        ):
            raise StandingConflict
        deadline = self.claims._options(deadline, cancelled)
        self._guard(deadline, cancelled)
        created = False

        def final():
            history = self._checked_history()
            item = self._select(history, claim.request)
            if (
                item.settlement is None
                or item.settlement.canonical_bytes() != record.canonical_bytes()
            ):
                raise StandingUnavailable
            if created:
                if history.active is not None:
                    raise StandingUnavailable
                self._writer(history.meta)
            self._guard(deadline, cancelled)

        with self._transaction(final):
            history = self._checked_history()
            item = self._select(history, claim.request)
            record = item.settlement
            if record is None:
                self._settlement_joins(item, original)
                self._active(history, item)
                self._writer(history.meta)
                instant = self.claims._clock()
                record = StandingAgentSettlementRecord(
                    schema_version="1.0",
                    request=original,
                    backend_id=history.meta["backend_id"],
                    settled_instance=history.meta["instance_id"],
                    settled_utc=instant,
                    record_digest=agent_settlement_record_digest(
                        original, history.meta["backend_id"], history.meta["instance_id"], instant
                    ),
                )
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_SETTLEMENTS} VALUES(?,?,?,?)",
                    (
                        item.claim.claim_id,
                        original.idempotency_key,
                        None if original.work.outcome is None else original.work.outcome.event_id,
                        record.canonical_bytes().decode("utf-8"),
                    ),
                )
                if (
                    self._sql(
                        f"UPDATE {_META} SET active_claim_id=NULL WHERE id=1 AND active_claim_id=?",
                        (item.claim.claim_id,),
                    ).rowcount
                    != 1
                ):
                    raise StandingUnavailable
                created = True
            elif record.request.canonical_bytes() != original.canonical_bytes():
                raise StandingConflict
            result = StandingAgentSettlementReceipt(
                schema_version="1.0",
                disposition="created" if created else "retained",
                settlement=record,
            ).validate_request(original)
        self._guard(deadline, cancelled)
        return result

    @_native
    def lookup(self, query: StandingAgentSettlementQuery) -> StandingAgentSettlementReceipt | None:
        query = _fresh(query, StandingAgentSettlementQuery)
        self._available()
        with self._transaction(self._available):
            item = self._select(self._checked_history(), query.registration.request.claim.request)
            if (
                item.registration is None
                or item.registration.canonical_bytes() != query.registration.canonical_bytes()
            ):
                raise StandingConflict
            if item.settlement is None:
                return None
            if item.settlement.request.idempotency_key != query.settlement_key:
                raise StandingConflict
            return StandingAgentSettlementReceipt(
                schema_version="1.0", disposition="retained", settlement=item.settlement
            ).validate_query(query)
