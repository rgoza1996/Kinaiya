"""Actual native Resources registration; metadata does not settle a lease."""

import asyncio
import hashlib
import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_agent_submit_admission import proposal as ordinary_proposal
from test_standing_admission_closure_native import TransactionGate, peer
from test_standing_dispatch_api import headers, rows
from test_standing_execution_api import BodyWitness, checked, original
from test_standing_execution_api import env as env
from test_standing_execution_api import factory as factory
from test_standing_task_admission import SECRET, SqlBoundary, database_state
from test_standing_task_admission import build_native as build_native
from test_standing_task_admission import native as native
from test_standing_task_admission import prepared as prepared

from packages.contracts.agent_control import AgentTaskIdentity
from packages.contracts.client import JournalClient
from packages.contracts.models import SessionRequest
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.session.agent_control import AgentControl
from services.session.store import Store

META = "standing_resource_meta"
ROWS = "standing_resource_reservations"
CONTEXT = "/v1/standing/resources/context"
RESERVE = "/v1/standing/resources/reserve"
OBSERVE = "/v1/standing/resources/observe"
CAP_HEADER = "X-Kinaiya-Standing-Capability"
MARKER = "SYNTHETIC-RESOURCE-STORAGE-FAULT-DO-NOT-ECHO"
RESOURCE_INSTANCE = "00000000-0000-4000-8000-000000000991"
LEASE = "00000000-0000-4000-8000-000000000992"


def feature():
    try:
        return importlib.import_module("packages.contracts.standing_resources")
    except ModuleNotFoundError as error:
        if error.name != "packages.contracts.standing_resources":
            raise
        pytest.fail("Frozen standing Resources contract is absent", pytrace=False)


def owner_type():
    try:
        return importlib.import_module("services.session.standing_resources").StandingResourceOwner
    except ModuleNotFoundError as error:
        if error.name != "services.session.standing_resources":
            raise
        pytest.fail("Frozen native standing Resources owner is absent", pytrace=False)


def query(task_id):
    return feature().TaskResourceQuery(schema_version="1.0", task_id=task_id)


def resource_owner(prepared, tasks, *, register=True, store=None, control=None):
    native = prepared.native
    owner = owner_type()(store or native.store, control or native.control, tasks)
    native.ledgers.append(owner)
    if register:
        owner.register()
    return owner


@pytest.fixture
def registered(prepared):
    feature()
    tasks = prepared.task_owner()
    owner = resource_owner(prepared, tasks)
    return SimpleNamespace(prepared=prepared, native=prepared.native, tasks=tasks, owner=owner)


def accepted(reg):
    return reg.tasks.admit(reg.prepared.request, capability=SECRET).proof


def reservation(context, *, lease=LEASE, action_digest=None):
    payload = {
        "operation": "handoff",
        "idempotency_key": "native-reservation",
        "required_bytes": 134217728,
        "task_id": context.query.task_id,
    }
    return feature().StandingResourceReservation(
        schema_version="1.0",
        context=context,
        resource_backend="b" * 32,
        resource_instance=RESOURCE_INSTANCE,
        lease_id=lease,
        action_digest=action_digest
        or hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def ordinary(store, task_id="ordinary_task", key="ordinary-native"):
    session = store.create_session(SessionRequest(channel="agent"))
    entry = store.admit_submit(ordinary_proposal(session, task_id=task_id, key=key), "agent").entry
    identity = AgentTaskIdentity(
        schema_version="1.0",
        session_id=entry.session_id,
        thread_id=entry.thread_id,
        epoch=entry.epoch,
        task_id=task_id,
        submit_event_id=entry.event_id,
        submit_sequence=entry.sequence,
        submit_idempotency_key=entry.payload["idempotency_key"],
        submit_payload_digest=entry.payload["payload_digest"],
    )
    return identity


async def join_client(client):
    while client._submit_exchanges:
        tasks = {
            t for x in client._submit_exchanges for t in (x.task, x.close_task) if t is not None
        }
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


@asynccontextmanager
async def gateway(env, role="resources", transport=None):
    client = JournalClient(
        "http://standing.test",
        headers(role)["Authorization"][7:],
        transport=transport or httpx.ASGITransport(app=env.app),
    )
    try:
        yield client
    finally:
        await client.close()
        await asyncio.wait_for(join_client(client), 3)


def capability(client, name):
    method = getattr(client, name, None)
    assert callable(method), "Frozen Resources JournalClient method is absent"
    return method


async def http_accepted(env):
    feature()
    packet = await original(env)
    async with gateway(env, "agent") as agent:
        await agent.standing_prepare(packet)
        proof = (await agent.standing_admit(packet, capability=SECRET)).proof
    return packet, proof


def test_constructor_passive_and_registration_captures_coverage_once(prepared):
    feature()
    tasks = prepared.task_owner()
    connection = prepared.native.store.db
    before = database_state(connection)

    class NoSql(SqlBoundary):
        def execute(self, *args, **kwargs):
            raise AssertionError("Passive Resources constructor touched SQLite")

    prepared.native.store.db = NoSql(connection)
    try:
        owner = resource_owner(prepared, tasks, register=False)
    finally:
        prepared.native.store.db = connection
    assert database_state(connection) == before
    owner.register()
    assert set(database_state(connection)[1]) - set(before[1]) == {META, ROWS}
    assert tuple(connection.execute(f"SELECT * FROM {META}").fetchone()) == (
        1,
        1,
        prepared.request.expected_backend,
        0,
    )
    tasks.admit(prepared.request, capability=SECRET)
    other = resource_owner(prepared, tasks)
    assert connection.execute(f"SELECT coverage_sequence FROM {META}").fetchone()[0] == 0
    assert other.context(query(prepared.request.task.task_id)).standing == tasks.lookup(
        prepared.request
    )


def test_context_proves_actual_standing_and_unrelated_ordinary_identity(registered):
    reg = registered
    proof = accepted(reg)
    context = reg.owner.context(query(proof.claim.request.task.task_id))
    assert context.family == "standing" and context.standing == proof and context.ordinary is None
    identity = ordinary(reg.native.store)
    other = reg.owner.context(query(identity.task_id))
    assert other.family == "ordinary" and other.ordinary == identity and other.standing is None
    assert other.backend_id == context.backend_id
    assert other.validate_request(other.query) == other


def test_missing_prepared_and_ambiguous_task_ids_are_never_ordinary(registered):
    reg = registered
    for task_id in ("unknown_task", reg.prepared.request.task.task_id):
        before = database_state(reg.native.store.db)
        with pytest.raises((StandingConflict, StandingUnavailable)):
            reg.owner.context(query(task_id))
        assert database_state(reg.native.store.db) == before
    reg.tasks.close_admission(reg.prepared.request, capability=SECRET)
    observed = reg.owner.observe(reg.prepared.request).validate_request(reg.prepared.request)
    assert observed.accepted_sequence is None and observed.reservation is None
    assert observed.coverage == "legacy-or-unadmitted" and observed.coverage_sequence == 0
    ordinary(reg.native.store, "shared_task", "first")
    ordinary(reg.native.store, "shared_task", "second")
    before = database_state(reg.native.store.db)
    with pytest.raises((StandingConflict, StandingUnavailable)):
        reg.owner.context(query("shared_task"))
    assert database_state(reg.native.store.db) == before


def test_actual_reserve_then_close_returns_only_retained_history_and_covered_observation(
    registered,
):
    reg = registered
    proof = accepted(reg)
    request = reservation(reg.owner.context(query(proof.claim.request.task.task_id)))
    saved = reg.owner.reserve(request).validate_request(request)
    assert saved.disposition == "created"
    closure = reg.tasks.close_admission(reg.prepared.request, capability=SECRET)
    before = database_state(reg.native.store.db)
    replay = reg.owner.reserve(request).validate_request(request)
    assert replay.disposition == "retained" and replay.request == request
    observed = reg.owner.observe(reg.prepared.request).validate_request(reg.prepared.request)
    assert observed.reservation == request and observed.closure_digest == closure.closure_digest
    assert observed.coverage_sequence == 0 and observed.accepted_sequence == proof.sequence
    assert observed.coverage == "accepted-after-bootstrap"
    with pytest.raises(StandingConflict):
        reg.owner.reserve(request.model_copy(update={"lease_id": RESOURCE_INSTANCE}))
    assert database_state(reg.native.store.db) == before
    assert len(reg.native.store.db.execute(f"SELECT * FROM {ROWS}").fetchall()) == 1


def test_close_first_permanently_refuses_new_reservation_without_claim_release(registered):
    reg = registered
    proof = accepted(reg)
    request = reservation(reg.owner.context(query(proof.claim.request.task.task_id)))
    reg.tasks.close_admission(reg.prepared.request, capability=SECRET)
    before = database_state(reg.native.store.db)
    with pytest.raises(StandingConflict):
        reg.owner.reserve(request)
    observed = reg.owner.observe(reg.prepared.request)
    assert observed.reservation is None and observed.accepted_sequence == proof.sequence
    assert reg.prepared.claims.unresolved() == reg.prepared.claim
    assert database_state(reg.native.store.db) == before


def test_existing_initial_at_bootstrap_remains_legacy_metadata(prepared):
    feature()
    tasks = prepared.task_owner()
    proof = tasks.admit(prepared.request, capability=SECRET).proof
    owner = resource_owner(prepared, tasks)
    tasks.close_admission(prepared.request, capability=SECRET)
    observed = owner.observe(prepared.request).validate_request(prepared.request)
    assert observed.coverage_sequence == proof.sequence == observed.accepted_sequence
    assert observed.coverage == "legacy-or-unadmitted" and observed.reservation is None


def test_first_reserve_rechecks_current_native_session_epoch(registered):
    reg = registered
    proof = accepted(reg)
    request = reservation(reg.owner.context(query(proof.claim.request.task.task_id)))
    with reg.native.store.db:
        reg.native.store.db.execute(
            "UPDATE sessions SET epoch=epoch+1 WHERE session_id=?",
            (proof.claim.request.task.task_session.session_id,),
        )
    before = database_state(reg.native.store.db)
    with pytest.raises(StandingConflict):
        reg.owner.reserve(request)
    assert database_state(reg.native.store.db) == before


class ReserveCommit(SqlBoundary):
    def __init__(self, connection, phase):
        super().__init__(connection)
        self.phase, self.wrote, self.once = phase, False, False

    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        self.wrote |= "INSERT" in sql.upper() and ROWS.upper() in sql.upper()
        return result

    def commit(self):
        if self.wrote and not self.once:
            self.once = True
            if self.phase == "after":
                self.connection.commit()
            raise RuntimeError(MARKER)
        return self.connection.commit()


@pytest.mark.parametrize("phase", ("before", "after"))
def test_real_commit_failure_keeps_exact_uncertainty_or_rollback(registered, phase):
    reg = registered
    proof = accepted(reg)
    request = reservation(reg.owner.context(query(proof.claim.request.task.task_id)))
    connection = reg.native.store.db
    boundary = ReserveCommit(connection, phase)
    reg.native.store.db = boundary
    try:
        with pytest.raises(StandingUnavailable) as caught:
            reg.owner.reserve(request)
        assert MARKER not in str(caught.value)
        assert boundary.once and not connection.in_transaction
    finally:
        reg.native.store.db = connection
    assert connection.execute(f"SELECT count(*) FROM {ROWS}").fetchone()[0] == int(phase == "after")
    reg.tasks.close_admission(reg.prepared.request, capability=SECRET)
    observed = reg.owner.observe(reg.prepared.request)
    assert observed.reservation == (request if phase == "after" else None)
    if phase == "after":
        assert reg.owner.reserve(request).disposition == "retained"
    else:
        with pytest.raises(StandingConflict):
            reg.owner.reserve(request)


@pytest.mark.parametrize("corruption", ("partial", "oversized", "foreign-key"))
def test_partial_or_corrupt_registration_family_never_repairs_or_downgrades(registered, corruption):
    reg = registered
    proof = accepted(reg)
    context = reg.owner.context(query(proof.claim.request.task.task_id))
    request = reservation(context)
    connection = reg.native.store.db
    with connection:
        if corruption == "partial":
            connection.execute(f"DROP TABLE {META}")
        elif corruption == "oversized":
            connection.execute(
                f"INSERT INTO {ROWS} VALUES(?,?)", (proof.claim.claim_id, " " * 196609)
            )
        else:
            connection.execute(f"DROP TABLE {ROWS}")
            connection.execute(
                f"CREATE TABLE {ROWS}(claim_id TEXT PRIMARY KEY REFERENCES standing_execution_claims(claim_id) ON DELETE CASCADE,request TEXT NOT NULL)"
            )
    before = database_state(connection)
    with pytest.raises(StandingUnavailable):
        reg.owner.context(context.query)
    with pytest.raises(StandingUnavailable):
        reg.owner.reserve(request)
    assert database_state(connection) == before


@pytest.mark.parametrize("first", ("reserve", "close"))
def test_actual_sqlite_reserve_close_order_has_one_pre_effect_outcome(registered, first):
    reg = registered
    proof = accepted(reg)
    request = reservation(reg.owner.context(query(proof.claim.request.task.task_id)))
    connection = reg.native.store.db
    gate = TransactionGate(
        connection, ROWS if first == "reserve" else "standing_admission_closures"
    )
    reg.native.store.db = gate
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resource-register-order")
    functions = {
        "reserve": lambda: reg.owner.reserve(request),
        "close": lambda: reg.tasks.close_admission(reg.prepared.request, capability=SECRET),
    }
    started = threading.Event()
    try:
        winner = pool.submit(functions[first])
        assert gate.entered.wait(3)
        second = "close" if first == "reserve" else "reserve"

        def later():
            started.set()
            return functions[second]()

        loser = pool.submit(later)
        assert started.wait(1) and not loser.done()
        gate.release.set()
        winner.result(timeout=4)
        if first == "close":
            with pytest.raises(StandingConflict):
                loser.result(timeout=4)
        else:
            assert loser.result(timeout=4).initial == proof
        observed = reg.owner.observe(reg.prepared.request)
        assert observed.reservation == (request if first == "reserve" else None)
    finally:
        gate.release.set()
        pool.shutdown(wait=True)
        reg.native.store.db = connection


def test_same_database_restart_preserves_exact_registry_and_coverage(registered):
    reg = registered
    proof = accepted(reg)
    request = reservation(reg.owner.context(query(proof.claim.request.task.task_id)))
    reg.owner.reserve(request)
    reg.tasks.close_admission(reg.prepared.request, capability=SECRET)
    reg.native.control.close()
    store = Store(reg.native.store.path)
    control = AgentControl(store)
    control.register()
    try:
        tasks, _ = peer(reg.prepared, store, control)
        reg.native.wall.forbidden = reg.native.mono.forbidden = True
        owner = resource_owner(reg.prepared, tasks, store=store, control=control)
        before = database_state(store.db)
        assert owner.reserve(request).validate_request(request).disposition == "retained"
        observed = owner.observe(reg.prepared.request)
        assert observed.reservation == request and observed.coverage_sequence == 0
        assert database_state(store.db) == before
    finally:
        store.close()


@pytest.mark.parametrize(
    "path,allowed", ((CONTEXT, "resources"), (RESERVE, "resources"), (OBSERVE, "agent"))
)
async def test_new_metadata_routes_authenticate_before_body_and_never_broaden_agent_role(
    env, path, allowed
):
    before = rows(env.store)
    for role in (None, "owner", "agent", "resources", "converse"):
        if role == allowed:
            continue
        body = BodyWitness()
        result = await env.client.post(
            path + "?bad=1",
            content=body,
            headers=headers(role, **{"Content-Type": "not/json", CAP_HEADER: MARKER}),
        )
        checked(result, 401 if role is None else 403)
        assert body.reads == 0
    assert rows(env.store) == before


@pytest.mark.parametrize(
    "path,role,kind_name",
    (
        (CONTEXT, "resources", "TaskResourceQuery"),
        (RESERVE, "resources", "StandingResourceReservation"),
        (OBSERVE, "agent", "StandingExecutionIntent"),
    ),
)
async def test_individual_raw_bounds_media_query_private_header_and_full_response(
    env, path, role, kind_name
):
    module = feature()
    packet, proof = await http_accepted(env)
    async with gateway(env) as resources, gateway(env, "agent") as agent:
        context = await capability(resources, "resource_task_context")(query(packet.task.task_id))
        request = (
            query(packet.task.task_id)
            if path == CONTEXT
            else reservation(context)
            if path == RESERVE
            else packet
        )
        maximum = 131072 if path == OBSERVE else getattr(module, kind_name).wire_bytes
        raw = request.canonical_bytes()
        before = rows(env.store)
        for suffix, extra, body, status in (
            ("?bad=1&bad=2", {}, raw, 422),
            ("", {CAP_HEADER: SECRET.hex()}, raw, 422),
            ("", {"Content-Type": "text/plain"}, raw, 415),
            ("", {"Content-Encoding": "gzip"}, raw, 415),
            ("", {}, b" " * (maximum + 1), 422),
            ("", {}, b'{"schema_version":"1.0",' + raw[1:], 422),
        ):
            checked(
                await env.client.post(
                    path + suffix,
                    content=body,
                    headers=headers(role, **{"Content-Type": "application/json", **extra}),
                ),
                status,
            )
        assert rows(env.store) == before
        if path == OBSERVE:
            await agent.standing_close_admission(packet, capability=SECRET)
        padded = b" " * (maximum - len(raw)) + raw
        result = await env.client.post(
            path, content=padded, headers=headers(role, **{"Content-Type": "application/json"})
        )
        response_kind = getattr(
            module,
            "TaskResourceContext"
            if path == CONTEXT
            else "StandingResourceReservationReceipt"
            if path == RESERVE
            else "StandingResourceObservation",
        )
        value = checked(result, 200, response_kind).validate_request(request)
        assert value is not None
        assert proof.claim.request == packet


async def test_real_journal_methods_bind_full_native_context_reserve_and_observation(env):
    module = feature()
    packet, proof = await http_accepted(env)
    async with gateway(env) as resources, gateway(env, "agent") as agent:
        q = query(packet.task.task_id)
        context = await capability(resources, "resource_task_context")(q)
        assert type(context) is module.TaskResourceContext and context.standing == proof
        request = reservation(context)
        result = await capability(resources, "standing_resource_reserve")(request)
        assert result.validate_request(request).disposition == "created"
        closure = await agent.standing_close_admission(packet, capability=SECRET)
        observed = await capability(agent, "standing_resource_observe")(packet)
        assert observed.validate_request(packet).reservation == request
        assert observed.closure_digest == closure.closure_digest
        before = rows(env.store)
        for path in ("/v1/standing/tasks/admit", "/v1/standing/tasks/close-admission"):
            checked(
                await env.client.post(
                    path,
                    content=packet.canonical_bytes(),
                    headers=headers("resources", **{"Content-Type": "application/json"}),
                ),
                403,
            )
        assert rows(env.store) == before
