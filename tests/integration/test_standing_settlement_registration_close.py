"""Source-aware N2 regression: real SQLite registration must honor owner close.

These tests were written after the independent native source finding. They use
the existing native grant/control fixture and SQLite's actual trace boundary;
they are not independent pre-implementation oracles or Task-settlement proof.
"""

import pytest
from test_standing_dispatch_owner import database_state
from test_standing_execution_owner import build_native as build_native

from packages.contracts.standing_work import StandingUnavailable
from services.session.standing_admission import StandingTaskOwner
from services.session.standing_resource_finalization import StandingResourceFinalizationOwner
from services.session.standing_resources import StandingResourceOwner
from services.session.standing_settlement import StandingSettlementOwner


def composition(native, legacy):
    claims = native.ledger(register=False)
    tasks = StandingTaskOwner(native.store, native.control, native.owner, native.dispatch, claims)
    resources = StandingResourceOwner(native.store, native.control, tasks)
    finalizations = StandingResourceFinalizationOwner(
        native.store, native.control, tasks, resources
    )
    resources.register_finalization_owner(finalizations)
    if legacy:
        for owner in (claims, tasks, resources, finalizations):
            owner.register()
    settlement = StandingSettlementOwner(
        native.store, native.control, tasks, resources, finalizations
    )
    native.ledgers.extend((tasks, resources, finalizations, settlement))
    return settlement


@pytest.mark.parametrize("legacy", (False, True), ids=("fresh", "complete-v1-upgrade"))
def test_real_sql_close_rolls_back_atomic_registration(build_native, legacy):
    native = build_native()
    owner = composition(native, legacy)
    connection = native.store.db
    before = database_state(connection)
    observed = []

    def trace(sql):
        if sql == "SELECT * FROM standing_settlement_meta" and not observed:
            observed.append(sql)
            owner.close()

    connection.set_trace_callback(trace)
    try:
        with pytest.raises(StandingUnavailable):
            owner.register()
    finally:
        connection.set_trace_callback(None)
    assert observed == ["SELECT * FROM standing_settlement_meta"]
    assert database_state(connection) == before
    assert not connection.in_transaction
    assert not owner._registered
    assert getattr(native.store, "_standing_settlement_owner", None) is None
    assert all(
        getattr(part, "_settlement_owner", None) is None
        for part in (owner.claims, owner.tasks, owner.resources, owner.finalizations)
    )


@pytest.mark.parametrize("legacy", (False, True), ids=("fresh", "complete-v1-upgrade"))
def test_same_open_native_composition_registers_complete_v2(build_native, legacy):
    native = build_native()
    owner = composition(native, legacy)
    owner.register()
    assert owner._registered
    assert native.store._standing_settlement_owner is owner
    assert owner.unresolved() is None
    assert not native.store.db.in_transaction
    marker = native.store.db.execute("SELECT * FROM standing_settlement_meta").fetchone()
    assert marker["version"] == 2
    assert marker["history_limit"] == 64
    assert marker["slot_bytes"] == 3_080_192
    assert marker["body_budget"] == 197_136_384
    assert marker["active_claim_id"] is None
