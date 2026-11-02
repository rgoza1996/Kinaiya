"""Finite prepared-run grants through genuine native protocols, without inference.

Oracle source: frozen 239-prepared-run-budget-contract-v1, SHA-256
ffbdf3b9906ac9f9f9ce49c985469edaa79a83f2dfa41e3ff84b9df79882840a.
Resources reports come only from the existing original-owner physical fixture.
These checks do not qualify a scheduler, model, or physical device.
"""

from datetime import UTC, datetime

import httpx
import pytest
from test_standing_agent_settlement import (
    commit,
    post,
    response,
)
from test_standing_agent_settlement import (
    factory as factory,
)
from test_standing_agent_settlement import (
    prepared_settlement as prepared_settlement,
)
from test_standing_dispatch_api import headers
from test_standing_dispatch_owner import database_state
from test_standing_execution_api import (
    ADMIT,
    CAP_HEADER,
    CLAIM_LOOKUP,
    PREPARE,
    SECRET,
    TASK_LOOKUP,
    UNRESOLVED,
    original,
)
from test_standing_physical_finalization import physical as physical

from packages.contracts.standing_admission import StandingInitialProof, StandingTaskAdmission
from packages.contracts.standing_dispatch import (
    StandingDispatchState,
    StandingDispatchStop,
    StandingDispatchStopProof,
)
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionClaimAdmission,
    StandingExecutionIntent,
    capability_digest,
)
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeReceipt,
    StandingResponsibilitySnapshot,
)

OTHER_RESPONSIBILITY = "23910000-0000-4000-8000-000000000099"
CLAIM_HEADER = {CAP_HEADER: SECRET.hex()}


def at(day, *, before=False):
    return datetime(
        2026, 11, day, 11 if before else 12, 59 if before else 0, 50 if before else 10, tzinfo=UTC
    )


def occurrence(packet, *, day, serial, record=None):
    raw = packet.model_dump(mode="json")
    key = f"prepared-budget:execution:{serial}"
    raw.update(
        idempotency_key=key,
        occurrence={
            "schema_version": "1.0",
            "local_date": f"2026-11-{day:02d}",
            "due_utc": f"2026-11-{day:02d}T12:00:00Z",
            "utc_offset_seconds": 0,
        },
        capability_sha256=capability_digest(
            packet.expected_backend, key, packet.executor_id, SECRET
        ),
    )
    raw["task"]["task_id"] = f"23910000-0000-4000-8000-{serial:012d}"
    if record is not None:
        raw.update(
            responsibility_id=record.receipt.request.responsibility_id,
            authorization_revision=record.receipt.revision,
            authorization_digest=record.receipt.authorization_digest,
            record_digest=record.record_digest,
        )
    return StandingExecutionIntent.model_validate(raw)


async def save_budget(env, packet, *, maximum, day, serial, responsibility=None):
    """Issue an explicit owner CAS; never change an existing claim or quota row."""
    env.wall.value = at(day, before=True)
    state = response(
        await env.client.get("/v1/standing/dispatch", headers=headers()),
        200,
        StandingDispatchState,
    )
    previous = response(
        await env.client.get(
            "/v1/standing/responsibilities/" + packet.responsibility_id, headers=headers()
        ),
        200,
        StandingResponsibilitySnapshot,
    ).record
    responsibility = responsibility or packet.responsibility_id
    target = response(
        await env.client.get("/v1/standing/responsibilities/" + responsibility, headers=headers()),
        200,
        StandingResponsibilitySnapshot,
    )
    authorization = previous.receipt.request.authorization.model_dump(mode="json")
    authorization["maximum_runs"] = maximum
    authorization["schedule"]["start_local_date"] = f"2026-11-{day:02d}"
    request = StandingChange.model_validate(
        {
            "schema_version": "1.0",
            "idempotency_key": f"prepared-budget:owner:{serial}",
            "expected_backend": state.backend_id,
            "expected_instance": state.instance_id,
            "expected_revision": 0 if target.record is None else target.record.receipt.revision,
            "responsibility_id": responsibility,
            "authorization": authorization,
        }
    )
    response(
        await post(env, "/v1/standing/changes", request, role="owner"),
        200,
        StandingChangeReceipt,
        request,
    )
    saved = response(
        await env.client.get("/v1/standing/responsibilities/" + responsibility, headers=headers()),
        200,
        StandingResponsibilitySnapshot,
    ).record
    assert saved.activation == "armed"
    assert saved.receipt.request.authorization.maximum_runs == maximum
    env.wall.value = at(day)
    return occurrence(packet, day=day, serial=serial, record=saved)


def claim_count(env):
    return env.store.db.execute("SELECT COUNT(*) FROM standing_execution_claims").fetchone()[0]


async def retained_claim(env, packet, claim):
    before = database_state(env.store.db)
    replay = response(
        await post(env, PREPARE, packet), 200, StandingExecutionClaimAdmission, packet
    )
    assert replay.disposition == "retained" and replay.claim == claim
    looked = response(await post(env, CLAIM_LOOKUP, packet), 200, StandingExecutionClaim, packet)
    assert looked == claim
    assert database_state(env.store.db) == before


async def unchanged_error(env, path, packet, *, status=409, **kwargs):
    before = database_state(env.store.db)
    response(await post(env, path, packet, **kwargs), status)
    # Observe every user schema and table, including entries, origins and ledgers.
    assert database_state(env.store.db) == before


async def exhausted(env, packet):
    before = database_state(env.store.db)
    response(await post(env, CLAIM_LOOKUP, packet), 200)
    await unchanged_error(env, PREPARE, packet)
    response(await post(env, CLAIM_LOOKUP, packet), 200)
    assert database_state(env.store.db) == before


async def consume(
    env, packet, prepared_settlement, *, expected_count, kind="created-unlaunched", history=()
):
    prepared = response(
        await post(env, PREPARE, packet), 200, StandingExecutionClaimAdmission, packet
    )
    assert prepared.disposition == "created"
    assert claim_count(env) == expected_count
    await retained_claim(env, packet, prepared.claim)
    for historical_packet, historical_claim in history:
        await retained_claim(env, historical_packet, historical_claim)
    active = response(
        await env.client.get(UNRESOLVED, headers=headers("agent")),
        200,
        StandingExecutionClaim,
        packet,
    )
    assert active == prepared.claim
    data = await prepared_settlement(session=env, claim=prepared.claim, kind=kind)
    assert data.value.initial is not None
    assert claim_count(env) == expected_count  # First admission spends no second run.
    before = database_state(env.store.db)
    admitted = response(
        await post(env, ADMIT, packet, extra=CLAIM_HEADER), 200, StandingTaskAdmission, packet
    )
    assert admitted.disposition == "retained" and admitted.proof == data.value.initial
    assert database_state(env.store.db) == before
    settled = await commit(data)
    assert claim_count(env) == expected_count  # Genuine settlement is not a refund.
    await retained_claim(env, packet, prepared.claim)
    return prepared.claim, data, settled


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum", (1, 2))
async def test_finite_preparations_last_admission_and_settlement_do_not_refund(
    factory, prepared_settlement, maximum
):
    env = await factory()
    packet = await save_budget(env, await original(env), maximum=maximum, day=1, serial=1)
    originals = []
    for index in range(maximum):
        env.wall.value = at(index + 1)
        current = occurrence(packet, day=index + 1, serial=index + 10)
        claim, data, _ = await consume(
            env,
            current,
            prepared_settlement,
            expected_count=index + 1,
            kind="launched-outcome-unconfirmed"
            if maximum == 2 and index == 1
            else "created-unlaunched",
            history=[(p, c) for p, c, _ in originals],
        )
        originals.append((current, claim, data.value.initial))
        changed = current.model_dump(mode="json")
        changed["task"]["task_id"] = "23910000-0000-4000-8000-000000000098"
        await unchanged_error(env, PREPARE, StandingExecutionIntent.model_validate(changed))

    env.wall.value = at(maximum + 1)
    over_budget = occurrence(packet, day=maximum + 1, serial=30)
    await exhausted(env, over_budget)
    assert claim_count(env) == maximum
    # Role checks remain authoritative even when the real finite budget is spent.
    await unchanged_error(env, PREPARE, over_budget, status=403, role="owner")
    await unchanged_error(env, PREPARE, over_budget, status=403, role="resources")

    env.wall.value = at(6)  # The original genuine grants expired on November 5.
    for current, claim, initial in originals:
        await retained_claim(env, current, claim)
        before = database_state(env.store.db)
        replay = response(
            await post(env, ADMIT, current, extra=CLAIM_HEADER),
            200,
            StandingTaskAdmission,
            current,
        )
        looked = response(await post(env, TASK_LOOKUP, current), 200, StandingInitialProof, current)
        assert replay.disposition == "retained" and replay.proof == looked == initial
        assert database_state(env.store.db) == before


class LosePrepareAcknowledgement(httpx.AsyncBaseTransport):
    """Lose one actual successful response, without replacing the native operation."""

    def __init__(self, app):
        self.delegate = httpx.ASGITransport(app=app)
        self.lost = 0

    async def handle_async_request(self, request):
        reply = await self.delegate.handle_async_request(request)
        if request.url.path == PREPARE and not self.lost:
            await reply.aread()
            assert reply.status_code == 200
            await reply.aclose()
            self.lost += 1
            raise httpx.ReadError(
                "Synthetic lost original prepare acknowledgement", request=request
            )
        return reply

    async def aclose(self):
        await self.delegate.aclose()


@pytest.mark.asyncio
async def test_uncertain_prepare_keeps_global_active_claim_across_revision_stop_and_expiry(factory):
    env = await factory()
    packet = await save_budget(env, await original(env), maximum=1, day=1, serial=40)
    boundary = LosePrepareAcknowledgement(env.app)
    async with httpx.AsyncClient(transport=boundary, base_url="http://standing.test") as client:
        with pytest.raises(httpx.ReadError):
            await post(env, PREPARE, packet, client=client)
    assert boundary.lost == 1 and claim_count(env) == 1
    claim = response(await post(env, CLAIM_LOOKUP, packet), 200, StandingExecutionClaim, packet)
    await retained_claim(env, packet, claim)

    renewed = await save_budget(env, packet, maximum=2, day=2, serial=41)
    assert renewed.authorization_revision == packet.authorization_revision + 1
    await unchanged_error(env, PREPARE, renewed)
    await unchanged_error(env, ADMIT, packet, extra=CLAIM_HEADER)
    active = response(
        await env.client.get(UNRESOLVED, headers=headers("agent")),
        200,
        StandingExecutionClaim,
        packet,
    )
    assert active == claim and claim_count(env) == 1

    stop = response(
        await env.client.get("/v1/standing/dispatch/stop-context", headers=headers()),
        200,
        StandingDispatchStop,
    )
    response(
        await post(env, "/v1/standing/dispatch/stop", stop, role="owner"),
        200,
        StandingDispatchStopProof,
        stop,
    )
    env.wall.value = at(6)
    before = database_state(env.store.db)
    await retained_claim(env, packet, claim)
    assert (
        response(
            await env.client.get(UNRESOLVED, headers=headers("agent")),
            200,
            StandingExecutionClaim,
            packet,
        )
        == claim
    )
    assert claim_count(env) == 1 and database_state(env.store.db) == before
    # No Resources binding, worker, fabricated report, or cleanup success is asserted.


@pytest.mark.asyncio
async def test_explicit_revision_and_other_responsibility_have_separate_retained_budgets(
    factory, prepared_settlement
):
    env = await factory()
    packet = await save_budget(env, await original(env), maximum=1, day=1, serial=50)
    first, _, _ = await consume(env, packet, prepared_settlement, expected_count=1)
    env.wall.value = at(2)
    await exhausted(env, occurrence(packet, day=2, serial=51))

    renewed = await save_budget(env, packet, maximum=1, day=2, serial=52)
    assert renewed.responsibility_id == packet.responsibility_id
    assert renewed.authorization_revision == packet.authorization_revision + 1
    second, _, _ = await consume(
        env, renewed, prepared_settlement, expected_count=2, history=[(packet, first)]
    )
    env.wall.value = at(3)
    await exhausted(env, occurrence(renewed, day=3, serial=53))

    other = await save_budget(
        env, renewed, maximum=1, day=3, serial=54, responsibility=OTHER_RESPONSIBILITY
    )
    assert other.responsibility_id != packet.responsibility_id
    assert other.authorization_revision == 1
    third, _, _ = await consume(
        env,
        other,
        prepared_settlement,
        expected_count=3,
        history=[(packet, first), (renewed, second)],
    )
    env.wall.value = at(4)
    await exhausted(env, occurrence(other, day=4, serial=55))
    for original_packet, claim in ((packet, first), (renewed, second), (other, third)):
        await retained_claim(env, original_packet, claim)
    assert claim_count(env) == 3
