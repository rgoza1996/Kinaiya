"""Independent frozen-v1 Session finalization HTTP/native oracles.

Only disposable SQLite, actual Session owners and in-process ASGI are exercised.
Raw complete packets and hashes come from the frozen contracts and old native
fixtures, without consulting the new owner or transport implementation. These
tests establish no physical Resources cleanup, worker or provider completion.
"""

import asyncio
import copy
import hashlib
import importlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import test_standing_execution_api as old_api
from test_standing_dispatch_api import headers, rows
from test_standing_execution_api import SECRET, close_env, original, prepared
from test_standing_execution_api import factory as factory
from test_standing_execution_owner import mode_request
from test_standing_resources_registration import RESOURCE_INSTANCE, reservation

from packages.contracts.standing_admission import StandingTaskAdmission
from packages.contracts.standing_closure import StandingAdmissionClosure
from packages.contracts.standing_execution import StandingExecutionClaim, StandingExecutionIntent
from packages.contracts.standing_resources import StandingResourceObservation, TaskResourceContext
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeReceipt,
    StandingConflict,
    StandingUnavailable,
)
from services.session.store import Store

BIND = "/v1/standing/resources/binding"
BIND_LOOKUP = BIND + "/lookup"
CONTEXT = "/v1/standing/resources/finalization/context"
REPORT = "/v1/standing/resources/finalization/report"
LOOKUP = "/v1/standing/resources/finalization/lookup"
ROUTES = (BIND, BIND_LOOKUP, CONTEXT, REPORT, LOOKUP)
CAP_HEADER = "X-Kinaiya-Standing-Capability"
MARKER = "SYNTHETIC-PRIVATE-FINALIZATION-HTTP-DO-NOT-ECHO"
BACKEND = "b" * 32
OTHER_INSTANCE = "00000000-0000-4000-8000-000000000993"
CAPS = {BIND: 163840, BIND_LOOKUP: 163840, CONTEXT: 147456, REPORT: 802816, LOOKUP: 147456}


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(domain, body):
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded(body)).hexdigest()


def rehash(value, field, domain):
    body = {key: child for key, child in value.items() if key != field}
    value[field] = digest(domain, body)
    return value


def context_hash(value):
    return rehash(value, "context_digest", "kinaiya.standing.resource-finalize-context.v1")


def report_hash(value):
    return rehash(value, "finalization_digest", "kinaiya.standing.resource-finalization.v1")


def safe_response(reply, status):
    statuses = status if type(status) is tuple else (status,)
    assert reply.status_code in statuses, (reply.status_code, reply.text)
    assert reply.headers.get("cache-control") == "no-store"
    assert MARKER not in reply.text and SECRET.hex() not in reply.text
    if reply.status_code == 200:
        assert reply.headers["content-type"].split(";", 1)[0] == "application/json"
        return reply.json()
    assert len(reply.content) <= 256
    return None


async def post(env, path, body, *, role="resources", raw=None, extra=None):
    return await env.client.post(
        path,
        content=encoded(body) if raw is None else raw,
        headers=headers(role, **{"Content-Type": "application/json", **(extra or {})}),
    )


def owner(env):
    value = getattr(env.app.state, "standing_resource_finalization_owner", None)
    assert value is not None, "Frozen native Resources finalization owner is absent"
    assert getattr(env.app.state, "standing_resource_finalization_initialized", False) is True
    return value


def operations(env):
    value = getattr(env.app.state, "standing_resource_finalization_operations", None)
    assert value is not None, "Frozen finalization actual-operation hub is absent"
    return value


def packet(name, raw):
    # Runtime use after the independent pure-family freeze; no source inspection.
    module = importlib.import_module("packages.contracts.standing_resource_finalization")
    return getattr(module, name).model_validate(copy.deepcopy(raw))


class ObservedConnection(sqlite3.Connection):
    """The real native connection, with dormant hooks installed at creation."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.boundary = None
        self.statement_count = 0

    def execute(self, sql, parameters=()):
        cursor = super().execute(sql, parameters)
        self.statement_count += 1
        if self.boundary is not None:
            self.boundary.after_execute(sql)
        return cursor

    def commit(self):
        if self.boundary is not None:
            return self.boundary.commit()
        return super().commit()

    def rollback(self):
        if self.boundary is not None:
            return self.boundary.rollback()
        return super().rollback()

    def close(self):
        if self.boundary is not None:
            return self.boundary.close()
        return super().close()


class ObservedLock:
    """Retain one actual RLock while arming retirement observation later."""

    def __init__(self, lock):
        self.lock = lock
        self.control = None
        self.retirement_thread = None
        self.retirement_entered = threading.Event()
        self.retired_before_lock = False

    def arm(self, control):
        self.control = control
        self.retirement_thread = None
        self.retirement_entered.clear()
        self.retired_before_lock = False

    def __getattr__(self, name):
        return getattr(self.lock, name)

    def __enter__(self):
        if threading.get_ident() == self.retirement_thread:
            self.retired_before_lock = self.control.instance is None
            self.retirement_entered.set()
        return self.lock.__enter__()

    def __exit__(self, *args):
        return self.lock.__exit__(*args)


class ObservedStore(Store):
    """Prepare instrumentation before create_app constructs native owners."""

    def __init__(self, path):
        connect = sqlite3.connect

        def observed_connect(*args, **kwargs):
            return connect(*args, **{**kwargs, "factory": ObservedConnection})

        # Store initialization is synchronous. No native owner exists yet, and
        # the actual SQLite connection is created with its final identity.
        with patch.object(sqlite3, "connect", observed_connect):
            super().__init__(path)
        self.lock = ObservedLock(self.lock)


@pytest.fixture
async def native_factory(factory, record_property):
    opened = []

    async def make(path=None):
        # Reuse the unchanged old ASGI setup while supplying its Store before
        # any owner can pin the native connection and lifecycle lock.
        with patch.object(old_api, "Store", ObservedStore):
            env = await factory(path)
        assert isinstance(env.store.db, ObservedConnection)
        assert isinstance(env.store.lock, ObservedLock)
        opened.append(env)
        return env

    try:
        yield make
    finally:
        joined = []
        for env in reversed(opened):
            connection = env.store.db
            await close_env(env)
            assert env.closed and env.client.is_closed
            assert env.operations.active_operations == 0
            newer = getattr(env.app.state, "standing_resource_finalization_operations", None)
            if newer is not None:
                assert newer.active_operations == 0
            with pytest.raises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            joined.append({"sqlite_closed": True, "http_closed": True, "actual_operations": 0})
        record_property("actual_native_teardown", json.dumps(joined, sort_keys=True))


@pytest.fixture
async def env(native_factory):
    return await native_factory()


async def large_original(env):
    value = await original(env)
    standing = env.app.state.standing_owner
    before = standing.snapshot(value.responsibility_id).record
    change_raw = before.receipt.request.model_dump(mode="json")
    change_raw.update(
        idempotency_key="finalization-http:large-grant", expected_revision=before.receipt.revision
    )
    change_raw["authorization"]["objective"] = "\0" * 10500
    change_raw["authorization"]["schedule"]["local_time"] = "12:01"
    change = StandingChange.model_validate(change_raw)
    reply = await post(env, "/v1/standing/changes", change_raw, role="owner")
    safe_response(reply, 200)
    StandingChangeReceipt.from_json(reply.content).validate_request(change)
    actual = standing.snapshot(value.responsibility_id).record
    raw = value.model_dump(mode="json")
    raw.update(
        authorization_revision=actual.receipt.revision,
        authorization_digest=actual.receipt.authorization_digest,
        record_digest=actual.record_digest,
    )
    raw["task"]["objective"] = actual.receipt.request.authorization.objective
    raw["occurrence"]["due_utc"] = "2026-11-01T12:01:00Z"
    env.wall.value = datetime(2026, 11, 1, 12, 1, 10, tzinfo=UTC)
    return StandingExecutionIntent.model_validate(raw)


async def seed(env, *, large=False):
    value = await (large_original(env) if large else original(env))
    admitted = await prepared(env, value)
    assert admitted.disposition == "created"
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    binding = {
        "schema_version": "1.0",
        "claim": admitted.claim.model_dump(mode="json"),
        "resource_backend": BACKEND,
        "resource_instance": RESOURCE_INSTANCE,
    }
    request = {
        "schema_version": "1.0",
        "idempotency_key": "finalization-http:one",
        "original": value.model_dump(mode="json"),
        "expected_resource_backend": BACKEND,
        "expected_resource_instance": RESOURCE_INSTANCE,
    }
    return SimpleNamespace(
        env=env,
        original=value,
        claim=admitted.claim,
        binding=binding,
        request=request,
        initial=None,
        reservation=None,
        closure=None,
        context=None,
        report=None,
    )


async def bind(data):
    reply = await post(data.env, BIND, data.binding)
    value = safe_response(reply, 200)
    assert value == {
        "schema_version": "1.0",
        "disposition": "created",
        "request": data.binding,
        "request_digest": digest("kinaiya.standing.resource-binding.v1", data.binding),
    }
    return value


async def admit(data):
    reply = await post(
        data.env,
        "/v1/standing/tasks/admit",
        data.original.model_dump(mode="json"),
        role="agent",
        extra={CAP_HEADER: SECRET.hex()},
    )
    safe_response(reply, 200)
    result = StandingTaskAdmission.from_json(reply.content).validate_request(data.original)
    assert result.disposition == "created"
    data.initial = result.proof
    return data.initial


async def reserve(data):
    assert data.initial is not None
    query = {"schema_version": "1.0", "task_id": data.original.task.task_id}
    reply = await post(data.env, "/v1/standing/resources/context", query)
    safe_response(reply, 200)
    context = TaskResourceContext.from_json(reply.content)
    assert context.standing == data.initial
    data.reservation = reservation(context).model_dump(mode="json")
    result = safe_response(
        await post(data.env, "/v1/standing/resources/reserve", data.reservation), 200
    )
    assert result["disposition"] == "created" and result["request"] == data.reservation
    return data.reservation


async def close_admission(data):
    reply = await post(
        data.env,
        "/v1/standing/tasks/close-admission",
        data.original.model_dump(mode="json"),
        role="agent",
        extra={CAP_HEADER: SECRET.hex()},
    )
    safe_response(reply, 200)
    data.closure = StandingAdmissionClosure.from_json(reply.content).validate_request(data.original)
    assert data.closure.initial == data.initial
    return data.closure


async def expected_context(data):
    assert data.closure is not None
    reply = await post(
        data.env, "/v1/standing/resources/observe", data.request["original"], role="agent"
    )
    safe_response(reply, 200)
    observation = StandingResourceObservation.from_json(reply.content).validate_request(
        data.original
    )
    assert observation.closure_digest == data.closure.closure_digest
    assert (
        observation.reservation is None
        if data.reservation is None
        else observation.reservation.model_dump(mode="json") == data.reservation
    )
    return context_hash(
        {
            "schema_version": "1.0",
            "request": copy.deepcopy(data.request),
            "binding": copy.deepcopy(data.binding),
            "closure": data.closure.model_dump(mode="json"),
            "observation": observation.model_dump(mode="json"),
        }
    )


def make_report(context, *, disposition=None, restoration=None):
    request = context["observation"]["reservation"]
    return report_hash(
        {
            "schema_version": "1.0",
            "context": copy.deepcopy(context),
            "resource_disposition": disposition
            or ("no-reservation" if request is None else "matching-lease-finalized"),
            "handoff_action_key": None if request is None else "native-reservation café 🧭",
            "handoff_action_digest": None if request is None else request["action_digest"],
            "reserved_lease_id": None if request is None else request["lease_id"],
            "policy_revision": 5,
            "owner_intent_revision": 8,
            "restoration": restoration or ("not-required" if request is None else "restored"),
        }
    )


async def setup(env, *, admitted=True, reserved=False, large=False):
    assert admitted or not reserved
    data = await seed(env, large=large)
    await bind(data)
    if admitted:
        await admit(data)
    if reserved:
        await reserve(data)
    await close_admission(data)
    data.context = await expected_context(data)
    reply = await post(env, CONTEXT, data.request)
    assert safe_response(reply, 200) == data.context
    assert reply.content == encoded(data.context)
    data.report = make_report(data.context)
    return data


def immutable_rows(env):
    retained = dict(rows(env.store))
    return {
        key: value
        for key, value in retained.items()
        if key
        in {
            "standing_execution_claims",
            "standing_task_admissions",
            "standing_admission_closures",
            "standing_resource_reservations",
            "entries",
        }
    }


def ledger_table(env, fields):
    candidates = []
    for (name,) in env.store.db.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
        escaped = name.replace('"', '""')
        columns = tuple(row[1] for row in env.store.db.execute(f'PRAGMA table_info("{escaped}")'))
        if columns == fields and name != "standing_resource_reservations":
            candidates.append(name)
    assert len(candidates) == 1, candidates
    return candidates[0]


class Witness(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0

    async def __aiter__(self):
        self.reads += 1
        yield MARKER.encode()


class SqlBoundary:
    def __init__(self, connection, table, *, phase=None):
        self.connection, self.table, self.phase = connection, table, phase
        self.wrote = self.fired = self.closed = False
        self.entered, self.release = threading.Event(), threading.Event()

    def arm(self):
        assert isinstance(self.connection, ObservedConnection)
        assert self.connection.boundary is None
        self.connection.boundary = self

    def disarm(self):
        assert self.connection.boundary is self
        self.connection.boundary = None

    def after_execute(self, sql):
        normalized = " ".join(sql.upper().split())
        if normalized.startswith("INSERT") and self.table.upper() in normalized:
            self.wrote = True
            if self.phase == "gate" and not self.fired:
                self.fired = True
                assert self.connection.in_transaction
                self.entered.set()
                assert self.release.wait(8), "Native SQL gate was not released"
            if self.phase == "rollback-poison":
                raise RuntimeError(MARKER)

    def commit(self):
        if self.wrote and not self.fired and self.phase in {"before", "after"}:
            self.fired = True
            if self.phase == "after":
                sqlite3.Connection.commit(self.connection)
            raise RuntimeError(MARKER)
        return sqlite3.Connection.commit(self.connection)

    def rollback(self):
        if self.wrote and self.phase == "rollback-poison":
            self.fired = True
            raise RuntimeError(MARKER)
        return sqlite3.Connection.rollback(self.connection)

    def close(self):
        self.closed = True
        return sqlite3.Connection.close(self.connection)


async def test_old_native_claim_admission_reservation_closure_routes_remain_compatible(env):
    data = await seed(env)
    await admit(data)
    await reserve(data)
    await close_admission(data)
    expected = await expected_context(data)
    assert expected["observation"]["reservation"] == data.reservation
    assert data.claim == env.app.state.standing_execution_owner.unresolved()
    assert not env.store.db.in_transaction and not env.store._agent_submit_unavailable


async def test_old_large_native_fixture_produces_valid_full_graph_above_old_transport_clamp(env):
    data = await seed(env, large=True)
    await admit(data)
    await reserve(data)
    await close_admission(data)
    context = await expected_context(data)
    assert 262144 < len(encoded(make_report(context))) <= 802816
    assert data.initial.claim == data.claim
    assert not env.store.db.in_transaction


async def test_registered_state_hooks_passive_constructor_and_exact_native_side_ledgers(env):
    current = owner(env)
    assert operations(env).active_operations == 0
    before, connection = rows(env.store), env.store.db

    class NoSql:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, *args, **kwargs):
            raise AssertionError("Passive finalization constructor touched SQLite")

        def commit(self):
            raise AssertionError("Passive finalization constructor committed")

        def rollback(self):
            raise AssertionError("Passive finalization constructor rolled back")

    env.store.db = NoSql()
    try:
        passive = type(current)(
            env.store,
            env.app.state.agent_control,
            env.app.state.standing_task_owner,
            env.app.state.standing_resource_owner,
        )
        passive.close()
    finally:
        env.store.db = connection
    assert rows(env.store) == before
    assert ledger_table(env, ("claim_id", "request"))
    assert ledger_table(env, ("claim_id", "report"))


@pytest.mark.parametrize("replacement", ("connection", "lock"))
async def test_later_store_identity_replacement_is_rejected_before_any_sql(env, replacement):
    data = await seed(env)
    owner(env)
    connection, lock = env.store.db, env.store.lock
    before, statements = rows(env.store), connection.statement_count

    class ForeignConnection:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("Replacement connection reached SQL")

    foreign = ForeignConnection()
    if replacement == "connection":
        env.store.db = foreign
    else:
        env.store.lock = threading.RLock()
    try:
        safe_response(await post(env, BIND, data.binding), 503)
        assert foreign.calls == 0
        assert connection.statement_count == statements
    finally:
        env.store.db, env.store.lock = connection, lock
    assert rows(env.store) == before and not connection.in_transaction


@pytest.mark.parametrize("admitted,reserved", ((False, False), (True, False), (True, True)))
async def test_actual_bind_close_full_context_report_retries_and_immutable_native_history(
    env, admitted, reserved
):
    data = await setup(env, admitted=admitted, reserved=reserved)
    before = immutable_rows(env)
    assert safe_response(await post(env, LOOKUP, data.request, role="agent"), 200) is None
    saved = safe_response(await post(env, REPORT, data.report), 200)
    assert saved == {"schema_version": "1.0", "disposition": "created", "finalization": data.report}
    after = rows(env.store)
    for role in ("agent", "resources"):
        found = safe_response(await post(env, LOOKUP, data.request, role=role), 200)
        assert found == {**saved, "disposition": "retained"}
    assert safe_response(await post(env, REPORT, data.report), 200) == {
        **saved,
        "disposition": "retained",
    }
    assert (
        safe_response(await post(env, BIND_LOOKUP, data.binding), 200)["disposition"] == "retained"
    )
    assert safe_response(await post(env, BIND, data.binding), 200)["disposition"] == "retained"
    assert immutable_rows(env) == before and rows(env.store) == after
    assert env.app.state.standing_execution_owner.unresolved() == data.claim
    assert not env.store.db.in_transaction and not env.store._agent_submit_unavailable
    table = ledger_table(env, ("claim_id", "report"))
    assert tuple(env.store.db.execute(f'SELECT claim_id,report FROM "{table}"').fetchone()) == (
        data.claim.claim_id,
        encoded(data.report).decode(),
    )
    if not admitted:
        assert data.context["closure"]["initial"] is None
        assert data.context["observation"]["accepted_sequence"] is None
        assert data.context["observation"]["coverage"] == "legacy-or-unadmitted"


@pytest.mark.parametrize(
    "disposition,restoration",
    (
        ("registered-no-lease", "not-required"),
        ("registered-no-lease", "restored"),
        ("registered-no-lease", "owner-off"),
        ("matching-lease-finalized", "owner-off"),
    ),
)
async def test_native_resources_testimony_preserves_distinct_registered_and_owner_off_dispositions(
    env, disposition, restoration
):
    data = await setup(env, reserved=True)
    report = make_report(data.context, disposition=disposition, restoration=restoration)
    result = safe_response(await post(env, REPORT, report), 200)
    assert result["finalization"] == report
    assert result["finalization"]["reserved_lease_id"] == data.reservation["lease_id"]
    assert immutable_rows(env)["standing_resource_reservations"]


async def test_nullable_lookup_before_binding_and_context_requires_committed_permanent_closure(env):
    data = await seed(env)
    assert safe_response(await post(env, BIND_LOOKUP, data.binding), 200) is None
    assert safe_response(await post(env, LOOKUP, data.request), 200) is None
    before = rows(env.store)
    safe_response(await post(env, CONTEXT, data.request), 409)
    assert rows(env.store) == before
    await bind(data)
    safe_response(await post(env, CONTEXT, data.request), 409)
    await close_admission(data)
    expected = await expected_context(data)
    assert safe_response(await post(env, CONTEXT, data.request), 200) == expected


@pytest.mark.parametrize("first", ("admit", "close"))
async def test_new_binding_after_actual_initial_or_permanent_closure_is_forbidden(env, first):
    data = await seed(env)
    if first == "admit":
        await admit(data)
    else:
        await close_admission(data)
    before = rows(env.store)
    safe_response(await post(env, BIND, data.binding), 409)
    safe_response(await post(env, CONTEXT, data.request), 409)
    assert rows(env.store) == before
    assert env.app.state.standing_execution_owner.unresolved() == data.claim


async def test_binding_matches_full_actual_prepared_claim_and_retained_resource_identity(env):
    data = await seed(env)
    bad = copy.deepcopy(data.binding)
    bad["claim"]["prepared_utc"] = "2026-11-01T12:00:11Z"
    claim_body = {
        key: value
        for key, value in bad["claim"].items()
        if key not in {"schema_version", "claim_digest"}
    }
    bad["claim"]["claim_digest"] = digest("kinaiya.standing.claim.record.v1", claim_body)
    assert StandingExecutionClaim.model_validate(bad["claim"]).validate_request(data.original)
    before = rows(env.store)
    safe_response(await post(env, BIND, bad), 409)
    assert rows(env.store) == before
    await bind(data)
    before = rows(env.store)
    for field, value in (("resource_backend", "c" * 32), ("resource_instance", OTHER_INSTANCE)):
        bad = {**data.binding, field: value}
        safe_response(await post(env, BIND, bad), 409)
        safe_response(await post(env, BIND_LOOKUP, bad), 409)
    assert rows(env.store) == before


async def test_bound_claim_reservation_cannot_switch_resource_backend_or_controller(env):
    data = await seed(env)
    await bind(data)
    await admit(data)
    reply = await post(
        env,
        "/v1/standing/resources/context",
        {"schema_version": "1.0", "task_id": data.original.task.task_id},
    )
    safe_response(reply, 200)
    raw = reservation(TaskResourceContext.from_json(reply.content)).model_dump(mode="json")
    before = rows(env.store)
    for field, value in (("resource_backend", "c" * 32), ("resource_instance", OTHER_INSTANCE)):
        safe_response(await post(env, "/v1/standing/resources/reserve", {**raw, field: value}), 409)
    assert rows(env.store) == before
    safe_response(await post(env, "/v1/standing/resources/reserve", raw), 200)


async def test_full_native_context_and_immutable_report_cannot_be_replaced_by_valid_foreign_packets(
    env, native_factory
):
    data = await setup(env, reserved=True)
    foreign = await setup(await native_factory(), reserved=True)
    before = rows(env.store)
    safe_response(await post(env, REPORT, foreign.report), 409)
    forged = copy.deepcopy(data.report)
    observed = forged["context"]["observation"]
    observed["coverage_sequence"] += 1
    observed["coverage"] = "legacy-or-unadmitted"
    rehash(observed, "observation_digest", "kinaiya.standing.resource-observation.v1")
    context_hash(forged["context"])
    report_hash(forged)
    assert StandingResourceObservation.model_validate(observed).validate_request(data.original)
    safe_response(await post(env, REPORT, forged), (409, 503))
    assert rows(env.store) == before
    saved = safe_response(await post(env, REPORT, data.report), 200)
    before = rows(env.store)
    for variation in ("policy", "idempotency"):
        changed = copy.deepcopy(data.report)
        if variation == "policy":
            changed["policy_revision"] += 1
        else:
            changed["context"]["request"]["idempotency_key"] = "finalization-http:different"
            context_hash(changed["context"])
        report_hash(changed)
        safe_response(await post(env, REPORT, changed), 409)
    assert rows(env.store) == before
    assert safe_response(await post(env, LOOKUP, data.request, role="agent"), 200) == {
        **saved,
        "disposition": "retained",
    }


async def test_history_survives_stop_mode_off_expired_grant_and_real_database_reopen(
    env, native_factory
):
    data = await setup(env, reserved=True)
    saved = safe_response(await post(env, REPORT, data.report), 200)
    dispatch = env.app.state.standing_dispatch_owner
    dispatch.stop(dispatch.stop_context())
    control = env.app.state.agent_control
    control.change(mode_request(control, "off", "finalization-http:owner-off"))
    env.wall.value = datetime(2030, 1, 1, tzinfo=UTC)
    env.mono.value += 1_000_000
    before = rows(env.store)
    assert safe_response(await post(env, BIND, data.binding), 200)["disposition"] == "retained"
    assert safe_response(await post(env, REPORT, data.report), 200) == {
        **saved,
        "disposition": "retained",
    }
    assert rows(env.store) == before
    old_instance = control.instance
    await close_env(env)
    reopened = await native_factory(env.path)
    assert reopened.app.state.agent_control.instance != old_instance
    before = rows(reopened.store)
    assert (
        safe_response(await post(reopened, BIND_LOOKUP, data.binding), 200)["disposition"]
        == "retained"
    )
    assert safe_response(await post(reopened, LOOKUP, data.request, role="agent"), 200) == {
        **saved,
        "disposition": "retained",
    }
    assert safe_response(await post(reopened, REPORT, data.report), 200) == {
        **saved,
        "disposition": "retained",
    }
    assert rows(reopened.store) == before


@pytest.mark.parametrize(
    "path,role",
    [
        (path, role)
        for path in ROUTES
        for role in (None, "owner", "agent", "converse", "decisions")
        if not (path == LOOKUP and role == "agent")
    ],
)
async def test_routes_authenticate_before_body_query_and_private_capability(env, path, role):
    body, before = Witness(), rows(env.store)
    reply = await env.client.post(
        path + "?bad=1&bad=2",
        content=body,
        headers=headers(role, **{"Content-Type": "not/json", CAP_HEADER: MARKER}),
    )
    safe_response(reply, 401 if role is None else 403)
    assert body.reads == 0 and rows(env.store) == before


@pytest.mark.parametrize("path", ROUTES)
async def test_raw_media_bounds_queries_duplicate_keys_and_all_private_headers_are_rejected(
    env, path
):
    data = await setup(env, reserved=True)
    body = (
        data.binding
        if path in (BIND, BIND_LOOKUP)
        else data.report
        if path == REPORT
        else data.request
    )
    raw, before = encoded(body), rows(env.store)
    for extra, malformed, status in (
        ({"Content-Type": "text/plain"}, raw, 415),
        ({"Content-Encoding": "gzip"}, raw, 415),
        ({CAP_HEADER: ""}, raw, 422),
        ({CAP_HEADER: SECRET.hex()}, raw, 422),
        ({}, b" " * (CAPS[path] + 1), 422),
        ({}, b'{"schema_version":"1.0",' + raw[1:], 422),
        ({}, b"\xff", 422),
        ({}, b'{"' + MARKER.encode() + b'":NaN}', 422),
    ):
        safe_response(await post(env, path, body, raw=malformed, extra=extra), status)
    for query in ("?extra=1", "?extra=1&extra=2", "?capability=" + SECRET.hex()):
        safe_response(await post(env, path + query, body), 422)
    duplicate = list(headers("resources", **{"Content-Type": "application/json"}).items()) + [
        (CAP_HEADER, SECRET.hex()),
        (CAP_HEADER, MARKER),
    ]
    safe_response(await env.client.post(path, content=raw, headers=duplicate), 422)
    assert rows(env.store) == before
    padded = b" " * (CAPS[path] - len(raw)) + raw
    reply = await post(env, path, body, raw=padded)
    safe_response(reply, 200)


async def test_large_actual_context_and_report_responses_exceed_old_clamp_without_truncation(env):
    data = await setup(env, reserved=True, large=True)
    assert 262144 < len(encoded(data.context)) <= 786432
    assert 262144 < len(encoded(data.report)) <= 802816
    reply = await post(env, REPORT, data.report)
    saved = safe_response(reply, 200)
    assert saved["finalization"] == data.report
    assert 262144 < len(reply.content) <= 819200
    lookup = await post(env, LOOKUP, data.request, role="agent")
    assert safe_response(lookup, 200) == {**saved, "disposition": "retained"}
    assert len(lookup.content) > 262144


@pytest.mark.parametrize("fault", ("json", "oversized", "trigger", "missing-table"))
async def test_corrupt_or_foreign_native_binding_schema_is_unavailable_without_repair(env, fault):
    data = await seed(env)
    await bind(data)
    table = ledger_table(env, ("claim_id", "request"))
    with env.store.lock, env.store.db:
        if fault in {"json", "oversized"}:
            value = MARKER if fault == "json" else "x" * 163841
            env.store.db.execute(f'UPDATE "{table}" SET request=?', (value,))
        elif fault == "trigger":
            env.store.db.execute(
                f'CREATE TRIGGER synthetic_foreign_binding BEFORE INSERT ON "{table}" BEGIN SELECT 1; END'
            )
        else:
            env.store.db.execute(f'DROP TABLE "{table}"')
    before = rows(env.store)
    safe_response(await post(env, BIND_LOOKUP, data.binding), 503)
    assert rows(env.store) == before and not env.store.db.in_transaction
    safe_response(await env.client.get("/v1/standing/dispatch", headers=headers()), 200)


async def test_closed_new_owner_leaves_ordinary_and_old_native_resource_routes_available(env):
    data = await setup(env, reserved=True)
    owner(env).close()
    before = rows(env.store)
    for path, raw in (
        (BIND, data.binding),
        (BIND_LOOKUP, data.binding),
        (CONTEXT, data.request),
        (REPORT, data.report),
        (LOOKUP, data.request),
    ):
        safe_response(await post(env, path, raw), 503)
    safe_response(await env.client.get("/v1/standing/dispatch", headers=headers()), 200)
    safe_response(
        await post(env, "/v1/standing/tasks/lookup", data.request["original"], role="agent"), 200
    )
    safe_response(
        await post(
            env,
            "/v1/standing/resources/context",
            {"schema_version": "1.0", "task_id": data.original.task.task_id},
        ),
        200,
    )
    assert rows(env.store) == before


@pytest.mark.parametrize("mutation", ("bind", "report"))
@pytest.mark.parametrize("phase", ("before", "after"))
async def test_real_commit_fault_before_or_after_commit_retains_only_actual_durable_history(
    env, mutation, phase
):
    data = await seed(env) if mutation == "bind" else await setup(env)
    current = owner(env)
    assert current is not None
    path, raw, lookup = (
        (BIND, data.binding, BIND_LOOKUP) if mutation == "bind" else (REPORT, data.report, LOOKUP)
    )
    table = ledger_table(
        env, ("claim_id", "request") if mutation == "bind" else ("claim_id", "report")
    )
    connection, before = env.store.db, immutable_rows(env)
    fault = SqlBoundary(connection, table, phase=phase)
    fault.arm()
    try:
        safe_response(await post(env, path, raw), 503)
        assert fault.fired and fault.wrote and not connection.in_transaction
    finally:
        fault.disarm()
    expected = int(phase == "after")
    assert connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == expected
    found = safe_response(
        await post(env, lookup, data.binding if mutation == "bind" else data.request), 200
    )
    assert (found is not None) is bool(expected)
    retry = safe_response(await post(env, path, raw), 200)
    assert retry["disposition"] == ("retained" if expected else "created")
    assert immutable_rows(env) == before


@pytest.mark.parametrize("mutation", ("bind", "report"))
@pytest.mark.parametrize("reason", ("deadline", "cancelled"))
async def test_actual_blocking_sql_counts_toward_native_deadline_and_cancellation_before_commit(
    env, mutation, reason
):
    data = await seed(env) if mutation == "bind" else await setup(env)
    current = owner(env)
    raw = data.binding if mutation == "bind" else data.report
    typed = packet(
        "StandingResourceBindingRequest" if mutation == "bind" else "StandingResourceFinalization",
        raw,
    )
    table = ledger_table(
        env, ("claim_id", "request") if mutation == "bind" else ("claim_id", "report")
    )
    connection, before = env.store.db, rows(env.store)
    gate = SqlBoundary(connection, table, phase="gate")
    cancelled = threading.Event()
    gate.arm()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                getattr(current, mutation),
                typed,
                deadline=time.monotonic() + (0.5 if reason == "deadline" else 7),
                cancelled=cancelled.is_set,
            )
            try:
                assert await asyncio.to_thread(gate.entered.wait, 3)
                if reason == "cancelled":
                    cancelled.set()
                else:
                    await asyncio.sleep(0.55)
            finally:
                gate.release.set()
            with pytest.raises(StandingUnavailable):
                await asyncio.to_thread(future.result, 5)
    finally:
        gate.release.set()
        gate.disarm()
    assert gate.fired and gate.wrote and not connection.in_transaction
    assert rows(env.store) == before


@pytest.mark.parametrize("mutation", ("bind", "report"))
async def test_actual_controller_retirement_during_sql_prevents_first_commit(env, mutation):
    data = await seed(env) if mutation == "bind" else await setup(env)
    current = owner(env)
    typed = packet(
        "StandingResourceBindingRequest" if mutation == "bind" else "StandingResourceFinalization",
        data.binding if mutation == "bind" else data.report,
    )
    table = ledger_table(
        env, ("claim_id", "request") if mutation == "bind" else ("claim_id", "report")
    )
    connection, lock = env.store.db, env.store.lock
    before = immutable_rows(env)
    gate = SqlBoundary(connection, table, phase="gate")
    control = env.app.state.agent_control
    observed = lock
    observed.arm(control)
    gate.arm()
    retirement = None

    def retire():
        observed.retirement_thread = threading.get_ident()
        return control.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(getattr(current, mutation), typed, deadline=time.monotonic() + 7)
            try:
                assert await asyncio.to_thread(gate.entered.wait, 3)
                retirement = pool.submit(retire)
                assert await asyncio.to_thread(observed.retirement_entered.wait, 3)
                assert observed.retired_before_lock and control.instance is None
                assert not retirement.done()
            finally:
                gate.release.set()
            with pytest.raises((StandingConflict, StandingUnavailable)):
                await asyncio.to_thread(future.result, 5)
            if retirement is not None:
                await asyncio.to_thread(retirement.result, 5)
    finally:
        gate.release.set()
        gate.disarm()
    assert gate.fired and gate.wrote
    assert not connection.in_transaction and not env.store._agent_submit_unavailable
    assert connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
    assert immutable_rows(env) == before


async def test_rollback_failure_poison_closes_actual_connection_without_releasing_claim(env):
    data = await setup(env)
    owner(env)
    table = ledger_table(env, ("claim_id", "report"))
    connection = env.store.db
    before = immutable_rows(env)
    fault = SqlBoundary(connection, table, phase="rollback-poison")
    fault.arm()
    try:
        safe_response(await post(env, REPORT, data.report), 503)
        assert fault.fired and fault.wrote and fault.closed
        assert env.store._agent_submit_unavailable
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    finally:
        fault.disarm()
    observer = sqlite3.connect(f"file:{env.path}?mode=ro", uri=True)
    try:
        observer.row_factory = sqlite3.Row
        snapshot = dict(rows(SimpleNamespace(db=observer)))
        assert {key: value for key, value in snapshot.items() if key in before} == before
        assert observer.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
    finally:
        observer.close()


async def test_lost_actual_report_http_ack_reconciles_exact_saved_record_without_rewrite(env):
    data = await setup(env, reserved=True)

    class LostAck(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner, self.dropped = httpx.ASGITransport(app=env.app), False

        async def handle_async_request(self, request):
            result = await self.inner.handle_async_request(request)
            if request.url.path == REPORT:
                assert result.status_code == 200
                self.dropped = True
                await result.aclose()
                raise httpx.ReadError(MARKER, request=request)
            return result

        async def aclose(self):
            await self.inner.aclose()

    transport = LostAck()
    async with httpx.AsyncClient(transport=transport, base_url="http://standing.test") as client:
        with pytest.raises(httpx.ReadError):
            await client.post(
                REPORT,
                content=encoded(data.report),
                headers=headers("resources", **{"Content-Type": "application/json"}),
            )
    assert transport.dropped
    before = rows(env.store)
    found = safe_response(await post(env, LOOKUP, data.request, role="agent"), 200)
    assert found == {
        "schema_version": "1.0",
        "disposition": "retained",
        "finalization": data.report,
    }
    assert safe_response(await post(env, REPORT, data.report), 200) == found
    assert rows(env.store) == before


async def test_adapter_rejects_full_returned_report_mismatch_after_actual_native_commit(
    env, monkeypatch
):
    data = await setup(env)
    current = owner(env)
    invoke = current.report

    def changed_reply(value, **options):
        actual = invoke(value, **options)
        raw = actual.model_dump(mode="json")
        raw["finalization"]["policy_revision"] += 1
        report_hash(raw["finalization"])
        return packet("StandingResourceFinalizationReceipt", raw)

    monkeypatch.setattr(current, "report", changed_reply)
    safe_response(await post(env, REPORT, data.report), 503)
    monkeypatch.setattr(current, "report", invoke)
    actual = safe_response(await post(env, LOOKUP, data.request, role="agent"), 200)
    assert actual["finalization"] == data.report


async def test_actual_thread_operation_survives_public_cancellation_until_explicit_close_join(
    env, monkeypatch
):
    data = await seed(env)
    current, hub = owner(env), operations(env)
    invoke = current.binding_lookup
    entered, release = threading.Event(), threading.Event()

    def held(value):
        entered.set()
        assert release.wait(8), "Actual native owner was abandoned"
        return invoke(value)

    monkeypatch.setattr(current, "binding_lookup", held)
    caller = asyncio.create_task(post(env, BIND_LOOKUP, data.binding))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert hub.active_operations == 1
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert hub.active_operations == 1
        closing = asyncio.create_task(hub.close())
        await asyncio.sleep(0)
        assert not closing.done() and hub.active_operations == 1
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        if closing is not None:
            await asyncio.wait_for(asyncio.shield(closing), 4)
        else:
            await hub.close()
    assert hub.active_operations == 0 and not env.store.db.in_transaction


async def test_shared_64_actual_owner_capacity_rejects_extra_ingress_before_read(env, monkeypatch):
    data = await seed(env)
    current, hub = owner(env), operations(env)
    invoke, release = current.binding_lookup, threading.Event()

    def held(value):
        assert release.wait(8), "Bounded actual owner did not receive teardown release"
        return invoke(value)

    monkeypatch.setattr(current, "binding_lookup", held)
    callers = [asyncio.create_task(post(env, BIND_LOOKUP, data.binding)) for _ in range(64)]
    try:
        async with asyncio.timeout(4):
            while hub.active_operations != 64:
                await asyncio.sleep(0)
        body = Witness()
        reply = await env.client.post(
            BIND_LOOKUP,
            content=body,
            headers=headers("resources", **{"Content-Type": "application/json"}),
        )
        safe_response(reply, 503)
        assert body.reads == 0 and hub.active_operations == 64
    finally:
        release.set()
        results = await asyncio.gather(*callers, return_exceptions=True)
    assert all(
        isinstance(result, httpx.Response) and result.status_code == 200 for result in results
    )
    assert all(result.content == b"null" for result in results)
    assert hub.active_operations == 0 and not env.store.db.in_transaction
