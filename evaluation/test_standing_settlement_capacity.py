"""Manual native lifetime-capacity qualification, excluded by pytest testpaths.

Run explicitly with ROOT, ROOT/tests/integration and ROOT/tests/unit on
PYTHONPATH. Screening2/4 retain the default64 policy and qualify no smaller
product limit. ALL THREE required64 cases must pass before capacity publication.
Each measured RPC retains its original Task, has the unchanged25-second limit,
and checks elapsed wall time after return. The work budget is four hours per
case; original cleanup follows separately and is never certified by a timeout.

No native candidate/client implementation was consulted. The frozen native39
fixture supplies genuine SQLite/ASGI registration, Resources physical reports,
and exact original closes. This uses no model, inference, live service/device
or fabricated positive report. Synthetic later-revision outcomes exercise
storage/verification, not actual Agent execution or steering authorization.
Logical SQL-body accounting excludes SQLite pages/indexes/WAL and unrelated
journal/control families; it is not a whole-database disk bound or benchmark.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as civil_time
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from test_agent_export_admission import export_request
from test_agent_submit_admission import proposal as ordinary_proposal
from test_standing_agent_settlement import (
    CLAIM_SECRET,
    LOOKUP,
    REGISTER,
    REGISTRATION,
    SETTLE,
    post,
    query,
    registration,
    response,
    settlement,
    wire,
)
from test_standing_agent_settlement import factory as factory
from test_standing_agent_settlement import physical as physical
from test_standing_dispatch_api import headers, rows
from test_standing_execution_api import original
from test_standing_execution_contract import claim as sizing_claim
from test_standing_execution_contract import initial_expected
from test_standing_physical_finalization import (
    begin,
    close_resources,
    handoff,
    ready,
    recorded_reservation,
)

from packages.contracts.client import JournalClient
from packages.contracts.models import AppendRequest, ProviderInfo, Task
from packages.contracts.standing_agent_settlement import (
    StandingAgentOutcomeReference,
    StandingAgentRetirementRegistrationReceipt,
    StandingAgentRetirementRegistrationRequest,
    StandingAgentSettlementReceipt,
    StandingAgentSettlementRequest,
)
from packages.contracts.standing_dispatch import (
    StandingDispatchState,
    StandingProducerRenewal,
)
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionIntent,
    capability_digest,
    standing_initial_request,
)
from packages.contracts.standing_resource_finalization import (
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeRequest,
)
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeReceipt,
    StandingResponsibilitySnapshot,
    latest_relevant,
)

HISTORY = 64
SLOT = 3_080_192
SHARED = 4096
BUDGET = 197_136_384
RPC_SECONDS = 25.0
WORK_SECONDS = 4 * 60 * 60
MAX_SAMPLES = 4096
START = date(2026, 11, 1)
# Root-frozen read-only inspection map, not discovered from implementation.
FAMILIES = {
    "standing_execution_claims": (
        "key",
        "claim_id",
        "occurrence_id",
        "responsibility_id",
        "authorization_revision",
        "claim",
    ),
    "standing_task_admissions": ("claim_id", "event_id", "sequence", "proof"),
    "standing_admission_closures": ("claim_id", "closure"),
    "standing_resource_reservations": ("claim_id", "request"),
    "standing_resource_bindings": ("claim_id", "request"),
    "standing_resource_finalizations": ("claim_id", "report"),
    "standing_claim_origins": ("claim_id", "origin", "reserved_bytes"),
    "standing_agent_retirement_registrations": ("claim_id", "owner_id", "key", "record"),
    "standing_agent_settlements": ("claim_id", "key", "outcome_event_id", "record"),
}
META = {
    "standing_settlement_meta": (
        "id",
        "version",
        "backend_id",
        "history_limit",
        "slot_bytes",
        "body_budget",
        "active_claim_id",
    ),
    "standing_execution_meta": ("id", "version", "backend_id"),
    "standing_admission_closure_meta": ("id", "version", "backend_id"),
    "standing_resource_meta": ("id", "version", "backend_id", "coverage_sequence"),
    "standing_resource_finalization_meta": ("id", "version", "backend_id"),
}
BODY_CAPS = {
    "standing_execution_claims": ("claim", 147456),
    "standing_task_admissions": ("proof", 163840),
    "standing_admission_closures": ("closure", 180224),
    "standing_resource_reservations": ("request", 196608),
    "standing_resource_bindings": ("request", 163840),
    "standing_resource_finalizations": ("report", 802816),
    "standing_agent_retirement_registrations": ("record", 149504),
    "standing_agent_settlements": ("record", 973824),
}


def key(kind, index):
    prefix = f"capacity:{kind}:{index:02d}:"
    return prefix + "x" * (128 - len(prefix))


def identifier(kind, index):
    return f"239{kind:05d}-0000-4000-8000-{index:012x}"


def utc(day, hour=12, minute=1, second=10):
    return datetime.combine(day, civil_time(hour, minute, second), tzinfo=UTC)


def secret(index):
    return hashlib.sha256(f"public-synthetic-capacity-secret:{index}".encode()).digest()


def largest_initial_objective(template):
    # Attributed old literal builders are used ONLY for size arithmetic. Their
    # synthetic claim is never submitted or used as native/physical testimony.
    raw = template.model_dump(mode="json")
    raw["task"]["objective"] = ""
    empty = AppendRequest.model_validate(initial_expected(sizing_claim(raw)))
    remaining = 65536 - len(empty.model_dump_json().encode())
    objective = "\0" * (remaining // 6) + "x" * (remaining % 6)
    assert 1 <= len(objective) <= 16000
    raw["task"]["objective"] = objective
    intent = StandingExecutionIntent.model_validate(raw)
    proof = StandingExecutionClaim.model_validate(sizing_claim(intent.model_dump(mode="json")))
    assert len(standing_initial_request(proof).model_dump_json().encode()) == 65536
    return objective


def sized_outcome(value, *, maximal):
    first = standing_initial_request(value.claim)
    task = Task(
        task_id=value.packet.task.task_id,
        session_id=value.packet.task.task_session.session_id,
        thread_id=value.packet.task.task_session.thread_id,
        epoch=value.packet.task.task_session.epoch,
        objective="Synthetic trusted-Agent later objective."
        if maximal
        else value.packet.task.objective,
        objective_revision=2 if maximal else 1,
        state="succeeded",
        content_scope=value.packet.task.content_scope,
        summary="Synthetic capacity outcome",
        evidence=[],
        remaining_required_work=[],
        source_event_ids=[],
        idempotency_key=first.payload["idempotency_key"],
        harness_version=value.packet.task.harness_version,
        model_pin=value.packet.task.model_pin.model_dump(mode="json"),
        auxiliary_models={},
        workspace_pin=value.packet.task.workspace_pin,
        error=None,
    )
    body = task.model_dump(mode="json")
    payload = {
        **{
            name: body[name]
            for name in (
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
        },
        "task": body,
        "plugin_versions": {},
        "accepted_extra": {"finite_float": 1.25, "large_integer": 10**20},
        "capacity_padding": "",
    }
    request = AppendRequest(
        session_id=task.session_id,
        thread_id=task.thread_id,
        epoch=task.epoch,
        kind="agent.outcome",
        payload=payload,
        causes=[],
        task_refs=[task.task_id],
        idempotency_key=key("outcome", value.index),
    )
    if maximal:
        payload["capacity_padding"] = "x" * (65536 - len(request.model_dump_json().encode()))
        request = AppendRequest.model_validate(
            {**request.model_dump(mode="json"), "payload": payload}
        )
        assert len(request.model_dump_json().encode()) == 65536
    return request


class Measure:
    def __init__(self):
        self.samples, self.pending = [], set()
        self.index = -1

    async def rpc(self, name, operation):
        assert len(self.samples) < MAX_SAMPLES and len(self.pending) < 64
        started, error = time.perf_counter(), None

        async def invoke():
            return await operation()

        task = asyncio.create_task(invoke())
        self.pending.add(task)
        try:
            async with asyncio.timeout(RPC_SECONDS):
                result = await asyncio.shield(task)
            assert task.done() and not task.cancelled() and task.exception() is None
            if time.perf_counter() - started >= RPC_SECONDS:
                raise TimeoutError("Capacity RPC returned after unchanged25-second limit")
            self.pending.remove(task)
            return result
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self.samples.append(
                {
                    "index": self.index,
                    "operation": name,
                    "seconds": time.perf_counter() - started,
                    "error": error,
                }
            )

    async def finish(self):
        # Keep exact originals even if a public timing facade already failed.
        for task in tuple(self.pending):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            assert task.done() and not task.cancelled()
            task.result()  # A failed original remains a visible cleanup error.
            self.pending.remove(task)


async def native(measure, env, path, request, kind=None, *, token=None, status=200):
    async def invoke():
        reply = await post(env, path, request, secret=token)
        return response(
            reply, status, kind, request if path in {REGISTER, SETTLE} and status == 200 else None
        )

    return await measure.rpc(path, invoke)


def logical_bytes(db, table, columns, claim_id=None):
    expression = "+".join(f'coalesce(length(CAST("{c}" AS BLOB)),0)' for c in columns)
    where, args = (" WHERE claim_id=?", (claim_id,)) if claim_id is not None else ("", ())
    row = db.execute(
        f'SELECT count(*),coalesce(sum({expression}),0) FROM "{table}"{where}', args
    ).fetchone()
    return row[0], row[1]


def policy(db, backend, expected_active=None):
    record = db.execute("SELECT * FROM standing_settlement_meta").fetchall()
    assert len(record) == 1
    row = record[0]
    assert (row["id"], row["version"], row["backend_id"]) == (1, 2, backend)
    assert (row["history_limit"], row["slot_bytes"], row["body_budget"]) == (HISTORY, SLOT, BUDGET)
    assert row["body_budget"] - row["history_limit"] * row["slot_bytes"] == SHARED
    assert row["active_claim_id"] == expected_active
    shared = 0
    for table, columns in META.items():
        count, size = logical_bytes(db, table, columns)
        assert count == 1
        metadata = db.execute(f'SELECT id,version,backend_id FROM "{table}"').fetchone()
        assert tuple(metadata) == (1, 2, backend)
        shared += size
    assert shared <= SHARED and not db.in_transaction
    return shared


def debit(db, count):
    row = db.execute(
        "SELECT count(*),coalesce(sum(reserved_bytes),0),min(reserved_bytes),max(reserved_bytes) FROM standing_claim_origins"
    ).fetchone()
    assert row[0] == count and row[1] == count * SLOT
    if count:
        assert row[2] == row[3] == SLOT
    assert row[1] + SHARED <= BUDGET


def stored_body(db, table, column, maximum, claim_id, expected):
    row = db.execute(
        f'SELECT typeof("{column}"),length(CAST("{column}" AS BLOB)),CASE WHEN typeof("{column}")=\'text\' AND length(CAST("{column}" AS BLOB)) BETWEEN 1 AND ? THEN "{column}" END FROM "{table}" WHERE claim_id=? LIMIT 2',
        (maximum, claim_id),
    ).fetchall()
    assert len(row) == 1 and row[0][0] == "text" and 1 <= row[0][1] <= maximum
    assert row[0][2].encode() == expected


def accounting(db, snapshot):
    total, families = 0, {}
    for table, columns in FAMILIES.items():
        count, size = logical_bytes(db, table, columns, snapshot.claim_id)
        if table == "standing_resource_reservations" and snapshot.reservation is not None:
            assert count == 1
        else:
            assert count == (0 if table == "standing_resource_reservations" else 1)
        families[table] = size
        total += size
        if table in snapshot.bodies:
            column, maximum = BODY_CAPS[table]
            stored_body(db, table, column, maximum, snapshot.claim_id, snapshot.bodies[table])
    links = (
        db.execute(
            "SELECT event_id FROM standing_task_admissions WHERE claim_id=?", (snapshot.claim_id,)
        ).fetchone()[0],
        db.execute(
            "SELECT outcome_event_id FROM standing_agent_settlements WHERE claim_id=?",
            (snapshot.claim_id,),
        ).fetchone()[0],
    )
    assert links == (snapshot.initial_event, snapshot.outcome_event)
    entries = []
    for event_id, expected_request, expected_entry in snapshot.entries:
        row = db.execute(
            "SELECT length(CAST(request AS BLOB)),length(CAST(value AS BLOB)),CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB)) BETWEEN 1 AND 65536 THEN request END,CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB)) BETWEEN 1 AND 69632 THEN value END FROM entries WHERE event_id=?",
            (event_id,),
        ).fetchone()
        assert row is not None and row[2] is not None and row[3] is not None
        assert wire(json.loads(row[2])) == expected_request
        assert wire(json.loads(row[3])) == expected_entry
        total += row[0] + row[1] + 4096
        entries.append({"request_bytes": row[0], "entry_bytes": row[1]})
    assert total <= SLOT and not db.in_transaction
    return {
        "index": snapshot.index,
        "logical_bytes": total,
        "families": families,
        "linked_entries": entries,
    }


async def grant_batch(measure, env, template, group, objective):
    start, count = START + timedelta(days=group * 30), 30 if group < 2 else 5
    env.wall.value = utc(start, 12, 0, 10)
    state = await measure.rpc("dispatch.current", lambda: dispatch(env))
    current = await measure.rpc(
        "authorization.template", lambda: responsibility(env, template.responsibility_id)
    )
    raw = current.receipt.request.model_dump(mode="json")
    raw.update(
        idempotency_key=key("grant", group),
        expected_backend=state.backend_id,
        expected_instance=state.instance_id,
        expected_revision=current.receipt.revision if group == 0 else 0,
        responsibility_id=template.responsibility_id if group == 0 else identifier(30, group),
    )
    raw["authorization"].update(objective=objective, maximum_runs=count)
    raw["authorization"]["schedule"].update(
        local_time="12:01",
        start_local_date=start.isoformat(),
        valid_until_utc=utc(start + timedelta(days=count - 1), 12, 3, 0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    )
    request = StandingChange.model_validate(raw)

    async def save():
        return response(
            await post(env, "/v1/standing/changes", request, role="owner"),
            200,
            StandingChangeReceipt,
            request,
        )

    await measure.rpc("authorization.save", save)
    record = await measure.rpc(
        "authorization.saved", lambda: responsibility(env, request.responsibility_id)
    )
    assert record.receipt.request.authorization.maximum_runs == count <= 32
    assert record.activation == "armed" and record.authorized_from_utc is not None
    saved = datetime.fromisoformat(record.authorized_from_utc.replace("Z", "+00:00"))
    expiry = datetime.fromisoformat(
        record.receipt.request.authorization.schedule.valid_until_utc.replace("Z", "+00:00")
    )
    assert timedelta(0) < expiry - saved <= timedelta(days=30)
    return record


async def dispatch(env):
    return response(
        await env.client.get("/v1/standing/dispatch", headers=headers()), 200, StandingDispatchState
    )


async def responsibility(env, identity):
    return response(
        await env.client.get("/v1/standing/responsibilities/" + identity, headers=headers()),
        200,
        StandingResponsibilitySnapshot,
    ).record


async def proposal(measure, env, template, record, index):
    day = START + timedelta(days=index)
    env.wall.value = utc(day)
    env.mono.value += 100
    renewal = StandingProducerRenewal(
        schema_version="1.0",
        expected_backend=template.expected_backend,
        expected_instance=template.expected_instance,
        producer_id=template.producer.request.producer_id,
        sequence=index + 2,
    )
    producer = await measure.rpc("producer.fresh", lambda: env.agent.standing_renew(renewal))
    assert producer.validate_request(renewal) == producer
    state = await measure.rpc("dispatch.current", lambda: dispatch(env))
    assert state.dispatch_ready and state.instance_id == template.expected_instance
    raw = template.model_dump(mode="json")
    raw.update(
        idempotency_key=key("prepare", index),
        responsibility_id=record.receipt.request.responsibility_id,
        authorization_revision=record.receipt.revision,
        authorization_digest=record.receipt.authorization_digest,
        record_digest=record.record_digest,
        producer=producer.model_dump(mode="json"),
        occurrence={
            "schema_version": "1.0",
            "local_date": day.isoformat(),
            "due_utc": utc(day, 12, 1, 0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "utc_offset_seconds": 0,
        },
        capability_sha256=capability_digest(
            template.expected_backend, key("prepare", index), template.executor_id, CLAIM_SECRET
        ),
    )
    raw["task"].update(
        task_id=identifier(10, index), objective=record.receipt.request.authorization.objective
    )
    value = StandingExecutionIntent.model_validate(raw)
    relevant = latest_relevant(
        record.receipt.request.authorization.schedule,
        record.authorized_from_utc,
        env.wall.value.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    assert (
        relevant is not None
        and relevant.local_date == value.occurrence.local_date
        and relevant.due_utc == value.occurrence.due_utc
    )
    return value


async def availability(measure, env):
    request = ordinary_proposal(env.session, task_id="capacity-ordinary", key="capacity-ordinary")
    admitted = await measure.rpc("ordinary.submit", lambda: env.agent.admit_submit(request))
    export = export_request(
        env.session,
        task_id="capacity-ordinary",
        content_scope="local_only",
        provider=ProviderInfo(provider="synthetic-local", model="no-inference", local=True),
    )
    result = await measure.rpc("ordinary.export", lambda: env.agent.agent_export_admission(export))
    assert result.validate_request(export).effective_scope == "local_only"
    page = await measure.rpc(
        "ordinary.history",
        lambda: env.agent.read(
            after=admitted.entry.sequence - 1,
            limit=1,
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            task_id="capacity-ordinary",
        ),
    )
    assert page == [admitted.entry]
    assert (
        await measure.rpc("ordinary.session", lambda: env.agent.session(env.session.session_id))
        == env.session
    )


async def run_capacity(factory, physical, record_property, *, count, maximal, acquired=False):
    measure, snapshots, sizes, accounts = Measure(), [], [], []
    started = time.perf_counter()
    env, observer = None, None
    finished, failure, cleanup_failure = False, None, None
    try:
        async with asyncio.timeout(WORK_SECONDS):
            env = await factory()
            template = await original(env)
            # This client is fixture-owned and closes after all actual RPCs.
            env.agent = JournalClient(
                "http://standing.test", "a" * 32, transport=httpx.ASGITransport(app=env.app)
            )
            observer = sqlite3.connect(f"file:{env.path}?mode=ro", uri=True)
            observer.row_factory = sqlite3.Row
            objective = largest_initial_objective(template) if maximal else template.task.objective
            group_record = None
            policy(observer, template.expected_backend)
            for index in range(count):
                measure.index = index
                if index % 30 == 0:
                    group_record = await grant_batch(measure, env, template, index // 30, objective)
                intent = await proposal(measure, env, template, group_record, index)
                admission = await measure.rpc(
                    "claim.prepare", partial(env.agent.standing_prepare, intent)
                )
                assert admission.disposition == "created"
                claim = admission.validate_request(intent).claim
                debit(observer, index + 1)
                policy(observer, template.expected_backend, claim.claim_id)
                assert observer.execute(
                    "SELECT origin,reserved_bytes FROM standing_claim_origins WHERE claim_id=?",
                    (claim.claim_id,),
                ).fetchone()[:] == ("fresh-v2-prepared", SLOT)
                for table in FAMILIES:
                    expected = (
                        1 if table in {"standing_execution_claims", "standing_claim_origins"} else 0
                    )
                    assert (
                        observer.execute(
                            f'SELECT count(*) FROM "{table}" WHERE claim_id=?', (claim.claim_id,)
                        ).fetchone()[0]
                        == expected
                    )
                if count == HISTORY and index == HISTORY - 1:
                    a = snapshots[0]
                    old_request = StandingAgentSettlementRequest.from_json(a.request)
                    before = rows(env.store)
                    replay = await native(
                        measure,
                        env,
                        SETTLE,
                        old_request,
                        StandingAgentSettlementReceipt,
                        token=secret(0),
                    )
                    assert (
                        replay.disposition == "retained"
                        and replay.settlement.canonical_bytes() == a.record
                    )
                    assert await measure.rpc("active.b", env.agent.standing_unresolved) == claim
                    assert rows(env.store) == before
                value = await physical(session=env, claim=claim)
                value.index = index
                reg_request = registration(
                    claim,
                    owner=identifier(20, index),
                    key=key("register", index),
                    secret=secret(index),
                )
                reg = await native(
                    measure, env, REGISTER, reg_request, StandingAgentRetirementRegistrationReceipt
                )
                assert reg.disposition == "created"
                value.binding = await measure.rpc(
                    "resources.bind", partial(value.coordinator.bind_standing_claim, claim)
                )
                initial_request = standing_initial_request(claim)
                if maximal:
                    assert len(initial_request.model_dump_json().encode()) == 65536
                value.initial = (
                    await measure.rpc(
                        "initial.admit",
                        partial(value.agent.standing_admit, intent, capability=CLAIM_SECRET),
                    )
                ).proof
                initial_entry = value.initial.journal_entry()
                assert env.store.entry(initial_entry.event_id) == initial_entry
                reservation, original_process, original_identity, restored_process = (
                    None,
                    None,
                    None,
                    None,
                )
                if acquired:
                    original_process = await measure.rpc("resources.ready", partial(ready, value))
                    original_identity = value.coordinator.manager.owned_identity("whistle")
                    assert original_identity is not None and original_process.returncode is None
                    acquired_result = await measure.rpc(
                        "resources.handoff", partial(handoff, value)
                    )
                    assert (
                        acquired_result["ok"] and acquired_result["policy"]["phase"] == "local_work"
                    )
                    reservation = recorded_reservation(value)
                    assert reservation.context.standing == value.initial
                    assert reservation.context.query.task_id == intent.task.task_id
                    assert value.coordinator.state.record.lease_id == reservation.lease_id
                    assert original_process.returncode is not None
                    stopped = value.coordinator.manager._workers["whistle"].stop_evidence
                    assert (
                        stopped["reaped"]
                        and stopped["group_empty"]
                        and stopped["rss_before_bytes"] > 0
                    )
                    assert value.effect_checks >= 2 and "stopped" in value.memories
                request = sized_outcome(value, maximal=maximal)
                if maximal:
                    bad = request.model_copy(deep=True)
                    bad.payload["capacity_padding"] += "x"
                    assert len(bad.model_dump_json().encode()) == 65537
                    before_entries = observer.execute("SELECT count(*) FROM entries").fetchone()[0]

                    async def refuse_oversize(bad=bad):
                        return await env.client.post(
                            "/v1/transcript/entries",
                            content=bad.model_dump_json().encode(),
                            headers=headers("agent", **{"Content-Type": "application/json"}),
                        )

                    refused = await measure.rpc("outcome65537.refused", refuse_oversize)
                    assert refused.status_code == 409 and refused.is_closed
                    assert (
                        observer.execute("SELECT count(*) FROM entries").fetchone()[0]
                        == before_entries
                    )
                entry = await measure.rpc("outcome.append", partial(value.agent.append, request))
                assert (
                    entry.origin == "agent-supervisor" and entry.sequence > value.initial.sequence
                )
                assert env.store.entry(entry.event_id) == entry
                request_bytes, entry_bytes = (
                    wire(request.model_dump(mode="json")),
                    wire(entry.model_dump(mode="json")),
                )
                assert len(request_bytes) <= 65536 and len(entry_bytes) <= 69632
                if maximal:
                    assert len(request_bytes) == 65536
                reference = StandingAgentOutcomeReference(
                    schema_version="1.0",
                    task_id=intent.task.task_id,
                    event_id=entry.event_id,
                    sequence=entry.sequence,
                    objective_revision=2 if maximal else 1,
                    state="succeeded",
                    request_sha256=hashlib.sha256(request_bytes).hexdigest(),
                    entry_sha256=hashlib.sha256(entry_bytes).hexdigest(),
                )
                closure = await measure.rpc(
                    "admission.close",
                    partial(value.agent.standing_close_admission, intent, capability=CLAIM_SECRET),
                )
                assert closure.initial == value.initial
                value.request = StandingResourceFinalizeRequest(
                    schema_version="1.0",
                    idempotency_key=key("resource-finalize", index),
                    original=intent,
                    expected_resource_backend=value.binding.request.resource_backend,
                    expected_resource_instance=value.binding.request.resource_instance,
                )
                report = await measure.rpc("resources.finalize", partial(begin, value))
                assert type(report) is StandingResourceFinalizationReceipt
                report = report.validate_request(value.request)
                if acquired:
                    assert report.finalization.resource_disposition == "matching-lease-finalized"
                    assert report.finalization.restoration == "restored"
                    assert report.finalization.context.observation.reservation == reservation
                    assert report.finalization.reserved_lease_id == reservation.lease_id
                    assert (
                        report.finalization.handoff_action_key == value.payload["idempotency_key"]
                    )
                    assert report.finalization.handoff_action_digest == reservation.action_digest
                    restored_identity = value.coordinator.manager.owned_identity("whistle")
                    restored_process = value.coordinator.manager._workers["whistle"].process
                    assert (
                        restored_identity is not None and restored_identity is not original_identity
                    )
                    assert value.coordinator.manager.is_current(restored_identity)
                    assert (
                        restored_process is not original_process
                        and restored_process is not None
                        and restored_process.returncode is None
                    )
                    before_replay = list(value.starts), list(value.stops)
                    replay = await measure.rpc("resources.finalize.replay", partial(begin, value))
                    assert (
                        replay.validate_request(value.request).finalization == report.finalization
                    )
                    assert (value.starts, value.stops) == before_replay
                else:
                    assert (
                        report.finalization.resource_disposition == "no-reservation"
                        and report.finalization.restoration == "not-required"
                    )
                known = await measure.rpc(
                    "resources.native-lookup",
                    partial(value.agent.standing_resource_finalization_lookup, value.request),
                )
                assert known is not None and known.finalization == report.finalization
                final_request = settlement(
                    reg.registration,
                    report,
                    kind="launched-outcome-known",
                    reference=reference,
                    key=key("settle", index),
                )
                receipt = await native(
                    measure,
                    env,
                    SETTLE,
                    final_request,
                    StandingAgentSettlementReceipt,
                    token=secret(index),
                )
                assert (
                    receipt.disposition == "created" and receipt.settlement.request == final_request
                )
                assert await measure.rpc("active.empty", env.agent.standing_unresolved) is None
                policy(observer, template.expected_backend)
                sizes.append(
                    {
                        "index": index,
                        "initial_request_bytes": len(initial_request.model_dump_json().encode()),
                        "initial_entry_bytes": len(wire(initial_entry.model_dump(mode="json"))),
                        "outcome_request_bytes": len(request_bytes),
                        "outcome_entry_bytes": len(entry_bytes),
                        "intent_bytes": len(intent.canonical_bytes()),
                        "claim_bytes": len(claim.canonical_bytes()),
                        "initial_proof_bytes": len(value.initial.canonical_bytes()),
                        "registration_request_bytes": len(reg_request.canonical_bytes()),
                        "registration_record_bytes": len(reg.registration.canonical_bytes()),
                        "binding_request_bytes": len(value.binding.request.canonical_bytes()),
                        "binding_receipt_bytes": len(value.binding.canonical_bytes()),
                        "resource_finalize_request_bytes": len(value.request.canonical_bytes()),
                        "resource_context_bytes": len(
                            report.finalization.context.canonical_bytes()
                        ),
                        "resource_finalization_bytes": len(report.finalization.canonical_bytes()),
                        "resource_receipt_bytes": len(report.canonical_bytes()),
                        "settlement_request_bytes": len(final_request.canonical_bytes()),
                        "settlement_record_bytes": len(receipt.settlement.canonical_bytes()),
                        "settlement_receipt_bytes": len(receipt.canonical_bytes()),
                    }
                )
                if acquired:
                    sizes[-1].update(
                        task_resource_context_bytes=len(reservation.context.canonical_bytes()),
                        reservation_bytes=len(reservation.canonical_bytes()),
                        resource_observation_bytes=len(
                            report.finalization.context.observation.canonical_bytes()
                        ),
                    )
                snapshot = SimpleNamespace(
                    index=index,
                    claim_id=claim.claim_id,
                    intent=intent.canonical_bytes(),
                    claim=claim.canonical_bytes(),
                    initial=value.initial.canonical_bytes(),
                    resource_request=value.request.canonical_bytes(),
                    resource_report=report.finalization.canonical_bytes(),
                    reservation=None if reservation is None else reservation.canonical_bytes(),
                    initial_event=initial_entry.event_id,
                    outcome_event=entry.event_id,
                    registration=reg_request.canonical_bytes(),
                    registration_record=reg.registration.canonical_bytes(),
                    request=final_request.canonical_bytes(),
                    record=receipt.settlement.canonical_bytes(),
                    bodies={
                        "standing_execution_claims": claim.canonical_bytes(),
                        "standing_task_admissions": value.initial.canonical_bytes(),
                        "standing_admission_closures": closure.canonical_bytes(),
                        "standing_resource_bindings": value.binding.request.canonical_bytes(),
                        "standing_resource_finalizations": report.finalization.canonical_bytes(),
                        "standing_agent_retirement_registrations": reg.registration.canonical_bytes(),
                        "standing_agent_settlements": receipt.settlement.canonical_bytes(),
                    },
                    entries=(
                        (
                            initial_entry.event_id,
                            wire(initial_request.model_dump(mode="json")),
                            wire(initial_entry.model_dump(mode="json")),
                        ),
                        (entry.event_id, request_bytes, entry_bytes),
                    ),
                )
                if acquired:
                    snapshot.bodies["standing_resource_reservations"] = (
                        reservation.canonical_bytes()
                    )
                snapshots.append(snapshot)
                accounts.append(accounting(observer, snapshot))
                await measure.rpc("resources.retire", partial(close_resources, value))
                assert value.cleanup_error is None and value.coordinator._close_owner.done()
                assert not value.coordinator._close_owner.cancelled()
                assert value.coordinator._close_owner.exception() is None
                assert value.gateway.http.is_closed and not value.gateway._submit_exchanges
                assert value.native.close_calls == value.control_transport.close_calls == 1
                if acquired:
                    assert value.provider.calls == []
                    assert restored_process.returncode is not None
                    managed = value.coordinator.manager._workers["whistle"]
                    assert managed.stop_evidence["reaped"] and managed.stop_evidence["group_empty"]
                    assert managed.pending_spawn is managed.pending_cleanup is None
                    assert (
                        managed.leader_fd is None and managed.pgid is None and managed.drains == []
                    )
                else:
                    assert value.provider.calls == [] and value.starts == []
            assert len(snapshots) == count
            assert len({snapshot.claim_id for snapshot in snapshots}) == count
            assert (
                len({event for snapshot in snapshots for event, _, _ in snapshot.entries})
                == count * 2
            )
            debit(observer, count)
            for snapshot in snapshots:
                measure.index = snapshot.index
                original_intent = StandingExecutionIntent.from_json(snapshot.intent)
                retained_claim = await measure.rpc(
                    "history.claim", partial(env.agent.standing_claim_lookup, original_intent)
                )
                assert (
                    retained_claim is not None
                    and retained_claim.canonical_bytes() == snapshot.claim
                )
                retained_initial = await measure.rpc(
                    "history.initial", partial(env.agent.standing_task_lookup, original_intent)
                )
                assert (
                    retained_initial is not None
                    and retained_initial.canonical_bytes() == snapshot.initial
                )
                resource_request = StandingResourceFinalizeRequest.from_json(
                    snapshot.resource_request
                )
                retained_report = await measure.rpc(
                    "history.resources",
                    partial(env.agent.standing_resource_finalization_lookup, resource_request),
                )
                assert (
                    retained_report is not None
                    and retained_report.finalization.canonical_bytes() == snapshot.resource_report
                )
                reg_request = StandingAgentRetirementRegistrationRequest.from_json(
                    snapshot.registration
                )
                reg = await native(
                    measure,
                    env,
                    REGISTRATION,
                    reg_request,
                    StandingAgentRetirementRegistrationReceipt,
                )
                assert reg.registration.canonical_bytes() == snapshot.registration_record
                request = StandingAgentSettlementRequest.from_json(snapshot.request)
                receipt = await native(
                    measure, env, LOOKUP, query(request), StandingAgentSettlementReceipt
                )
                assert (
                    receipt.validate_query(query(request)).settlement.canonical_bytes()
                    == snapshot.record
                )
                assert (
                    accounting(observer, snapshot)["logical_bytes"]
                    == accounts[snapshot.index]["logical_bytes"]
                )
            await availability(measure, env)
            if count == HISTORY:
                assert (
                    sum(account["logical_bytes"] for account in accounts)
                    + policy(observer, template.expected_backend)
                    <= BUDGET
                )
                measure.index = HISTORY
                intent = await proposal(measure, env, template, group_record, HISTORY)
                current = await measure.rpc(
                    "authorization.hplus1-current",
                    lambda: responsibility(env, intent.responsibility_id),
                )
                assert (
                    current == group_record
                    and current.receipt.request.authorization.maximum_runs == 5
                )
                assert (
                    observer.execute(
                        "SELECT count(*) FROM standing_execution_claims WHERE responsibility_id=? AND authorization_revision=?",
                        (intent.responsibility_id, intent.authorization_revision),
                    ).fetchone()[0]
                    == 4
                )
                assert (
                    await measure.rpc(
                        "hplus1.absent", lambda: env.agent.standing_claim_lookup(intent)
                    )
                    is None
                )
                before = rows(env.store)

                async def full():
                    return await post(env, "/v1/standing/executions/prepare", intent)

                response(await measure.rpc("hplus1.capacity503", full), 503)
                assert rows(env.store) == before
                debit(observer, HISTORY)
                assert (
                    await measure.rpc("active.still-empty", env.agent.standing_unresolved) is None
                )
                await availability(measure, env)
            finished = True
    except BaseException as exc:
        failure = type(exc).__name__
        raise
    finally:
        try:
            await measure.finish()
        except BaseException as exc:
            cleanup_failure = type(exc).__name__
            raise
        finally:
            try:
                if env is not None and hasattr(env, "agent"):
                    await env.agent.close()
                    assert env.agent.http.is_closed and not env.agent._submit_exchanges
                if observer is not None:
                    observer.close()
            except BaseException as exc:
                cleanup_failure = type(exc).__name__
                raise
            finally:
                record_property(
                    "capacity_measurement",
                    json.dumps(
                        {
                            "requested_histories": count,
                            "maximal_native_boundary_vector": maximal,
                            "genuine_resources_acquired_and_restored": acquired,
                            "completed_histories": len(snapshots),
                            "work_finished": finished,
                            "failure": failure,
                            "driver_cleanup_failure": cleanup_failure,
                            "whole_seconds_including_driver_cleanup": time.perf_counter() - started,
                            "rpc_limit_seconds": RPC_SECONDS,
                            "work_budget_seconds": WORK_SECONDS,
                            "timings": measure.samples,
                            "packet_sizes": sizes,
                            "actual_maxima_bytes": {
                                name: max(item[name] for item in sizes)
                                for name in sizes[0]
                                if name != "index"
                            }
                            if sizes
                            else {},
                            "declared_packet_ceilings_not_observed_maxima": {
                                "native_request": 65536,
                                "native_entry": 69632,
                                "registration_request": 148480,
                                "registration_record": 149504,
                                "task_resource_context": 180224,
                                "resource_reservation": 196608,
                                "resource_observation": 212992,
                                "resource_context": 786432,
                                "resource_finalization": 802816,
                                "resource_receipt": 819200,
                                "settlement_request": 972800,
                                "settlement_record": 973824,
                                "settlement_receipt": 973952,
                            },
                            "logical_accounting": accounts,
                        },
                        sort_keys=True,
                    ),
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", (2, 4), ids=("screen2-not-capacity", "screen4-not-capacity"))
async def test_screening_keeps_default64_policy(factory, physical, record_property, count):
    await run_capacity(factory, physical, record_property, count=count, maximal=False)


@pytest.mark.asyncio
async def test_required64_full_lifetime_and_hplus1(factory, physical, record_property):
    await run_capacity(factory, physical, record_property, count=HISTORY, maximal=False)


@pytest.mark.asyncio
async def test_required64_max_native_bodies_and_complete_graphs(factory, physical, record_property):
    await run_capacity(factory, physical, record_property, count=HISTORY, maximal=True)


@pytest.mark.asyncio
async def test_screen2_max_native_bodies_with_reservation(factory, physical, record_property):
    await run_capacity(factory, physical, record_property, count=2, maximal=True, acquired=True)


@pytest.mark.asyncio
async def test_required64_max_native_bodies_with_reservation(factory, physical, record_property):
    await run_capacity(
        factory, physical, record_property, count=HISTORY, maximal=True, acquired=True
    )
