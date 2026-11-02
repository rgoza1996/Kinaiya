"""Independent additive Stop-template tests; the sealed native50 is unchanged."""

import json

import pytest
from test_standing_dispatch_owner import (
    TABLES,
    active,
    database_state,
    gate,
    ledger_counts,
    mode_request,
    seed_generations,
    uid,
)
from test_standing_dispatch_owner import (
    native as native,
)

from packages.contracts.agent_control import AgentModeReceipt
from packages.contracts.interfaces import AgentControlUnavailable
from packages.contracts.standing_dispatch import (
    StandingDispatchActivation,
    StandingDispatchActivationReceipt,
    StandingDispatchStop,
    activation_digest,
)
from packages.contracts.standing_work import StandingUnavailable
from services.session.agent_control import AgentControl


def template_without_writes(native, owner):
    before = database_state(native.store.db)
    changes = native.store.db.total_changes
    clock_calls = native.clock.calls
    native.clock.forbidden = True
    template = owner.stop_context()
    assert isinstance(template, StandingDispatchStop)
    assert StandingDispatchStop.from_json(template.canonical_bytes()) == template
    assert database_state(native.store.db) == before
    assert native.store.db.total_changes == changes
    assert native.clock.calls == clock_calls
    assert not native.store.db.in_transaction
    return template


def assert_current_template(native, template, expected_instance):
    current = gate(native.store.db)
    assert template.expected_backend == native.backend
    assert template.expected_instance == expected_instance
    assert template.issued_epoch == current["epoch"]
    assert template.expected_fence_token == current["fence_token"]
    known = native.store.db.execute(
        "SELECT instance_id,retired FROM agent_control_generations WHERE instance_id=?",
        (template.expected_instance,),
    ).fetchone()
    assert known is not None
    assert known["instance_id"] == expected_instance
    assert type(known["retired"]) is int
    return current


def test_current_epoch_zero_context_is_only_a_read_only_template(native):
    owner = native.owner()
    current = owner.snapshot()
    template = template_without_writes(native, owner)
    assert_current_template(native, template, native.control.instance)
    assert template.issued_epoch == 0
    assert template.activation_digest is None
    assert template.expected_fence_token == current.fence_token
    assert template_without_writes(native, owner).canonical_bytes() == template.canonical_bytes()
    # Merely obtaining the template has not carried out the matching Stop0.
    assert gate(native.store.db)["fence_token"] == current.fence_token
    proof = owner.stop(template).validate_request(template)
    assert proof.state.fence_token != current.fence_token
    assert proof.state.epoch == proof.state.stopped_through_epoch == 0
    assert ledger_counts(native.store.db) == (1, 0, 0, 0)
    # A delayed sender holding the old context targets only epoch zero even
    # after a later explicit activation becomes the current ready epoch.
    native.clock.forbidden = False
    owner.renew(native.renewal())
    activation = native.activation(owner, key="after:captured:context")
    receipt = owner.activate(activation)
    before = database_state(native.store.db)
    stale = owner.stop(template).validate_request(template)
    assert stale.state.epoch == receipt.epoch == 1
    assert stale.state.stopped_through_epoch == 0
    assert stale.state.fence_token == proof.state.fence_token
    assert stale.state.dispatch_ready is True
    assert database_state(native.store.db) == before


def test_current_controller_is_preferred_over_the_retired_activation_issuer(native):
    _, _, _, _, receipt = active(native)
    former_instance = receipt.mode_receipt.service_instance
    replacement = AgentControl(native.store)
    replacement.register()
    owner = native.owner(control=replacement)
    template = template_without_writes(native, owner)
    assert replacement.instance != former_instance
    assert_current_template(native, template, replacement.instance)
    assert template.issued_epoch == receipt.epoch
    assert template.activation_digest == receipt.activation_digest
    assert (
        native.store.db.execute(
            "SELECT retired FROM agent_control_generations WHERE instance_id=?", (former_instance,)
        ).fetchone()[0]
        == 1
    )
    assert replacement.snapshot().control_writable is True
    off = mode_request(native.backend, replacement.instance, 1, key="context:owner:off", mode="off")
    replacement.change(off)
    while_off = template_without_writes(native, owner)
    assert while_off.expected_instance == replacement.instance
    assert while_off.activation_digest == receipt.activation_digest
    assert while_off.issued_epoch == receipt.epoch
    assert replacement.snapshot().mode == "off"


def test_fresh_owner_discovers_current_target_after_null_controller_at_seeded_generation_cap(
    native,
):
    _, _, _, _, receipt = active(native)
    # 9,999 known retired UUID rows are a labelled SQLite capacity fixture,
    # not a claim that 10,000 actual Session controllers were started.
    seed_generations(native)
    native.control.close()
    failed_control = AgentControl(native.store)
    with pytest.raises(AgentControlUnavailable):
        failed_control.register()
    assert (
        native.store.db.execute("SELECT instance_id FROM agent_control_state").fetchone()[0] is None
    )
    owner = native.owner(control=failed_control)
    before_counts = ledger_counts(native.store.db)
    # The caller passes no saved activation or historical UUID to discovery.
    template = template_without_writes(native, owner)
    assert_current_template(native, template, receipt.mode_receipt.service_instance)
    assert template.activation_digest == receipt.activation_digest
    assert template.issued_epoch == receipt.epoch == 1
    assert (
        native.store.db.execute("SELECT instance_id FROM agent_control_state").fetchone()[0] is None
    )
    proof = owner.stop(template).validate_request(template)
    assert proof.state.instance_id is None
    assert proof.state.control_writable is False
    assert proof.state.producer is None
    assert proof.state.stopped_through_epoch == receipt.epoch
    assert proof.state.mode == "autonomous"
    assert ledger_counts(native.store.db) == before_counts == (10000, 1, 1, 1)
    fresh_template = template_without_writes(native, owner)
    assert fresh_template.expected_fence_token == proof.state.fence_token
    assert fresh_template.expected_instance == template.expected_instance


def test_null_controller_epoch_zero_uses_a_genuinely_issued_retired_uuid_without_assigning_writer(
    native,
):
    owner = native.owner()
    first = native.control.instance
    replacement = AgentControl(native.store)
    replacement.register()
    second = replacement.instance
    replacement.close()
    assert first != second
    read_control = AgentControl(native.store)
    owner = native.owner(control=read_control)
    template = template_without_writes(native, owner)
    assert template.expected_instance in {first, second}
    assert_current_template(native, template, template.expected_instance)
    assert template.issued_epoch == 0
    assert template.activation_digest is None
    for _ in range(5):
        assert (
            template_without_writes(native, owner).canonical_bytes() == template.canonical_bytes()
        )
    assert (
        native.store.db.execute(
            "SELECT retired FROM agent_control_generations WHERE instance_id=?",
            (template.expected_instance,),
        ).fetchone()[0]
        == 1
    )
    state = owner.snapshot()
    assert state.instance_id is None
    assert state.control_writable is False
    assert state.producer is None
    proof = owner.stop(template)
    assert proof.state.fence_token != template.expected_fence_token
    assert proof.state.instance_id is None
    assert ledger_counts(native.store.db) == (2, 0, 0, 0)


@pytest.mark.parametrize("damaged_proof", ["activation", "ordinary_mode"])
def test_stop_context_refuses_corrupt_selected_full_history_instead_of_guessing(
    native, damaged_proof
):
    owner, _, _, _, receipt = active(native)
    connection = native.store.db
    if damaged_proof == "activation":
        # This is a valid standalone full receipt for a different original
        # fence. Its stored original request and paired mode row stay intact.
        damaged = receipt.model_dump(mode="python")
        damaged["request"]["expected_fence_token"] = uid(999)
        foreign_original = StandingDispatchActivation.model_validate(damaged["request"])
        damaged["activation_digest"] = activation_digest(foreign_original, receipt.epoch)
        packet = StandingDispatchActivationReceipt.model_validate(damaged)
        packet.validate_request(foreign_original)
        connection.execute(
            "UPDATE standing_dispatch_activations SET receipt=?",
            (packet.canonical_bytes().decode(),),
        )
    else:
        damaged = json.loads(receipt.mode_receipt.canonical_bytes())
        damaged["request"]["idempotency_key"] = "other:original:key"
        packet = AgentModeReceipt.model_validate(damaged)
        packet.validate_request(packet.request)
        connection.execute(
            "UPDATE agent_control_changes SET receipt=?", (packet.canonical_bytes().decode(),)
        )
    connection.commit()
    before = database_state(connection)
    changes = connection.total_changes
    with pytest.raises(StandingUnavailable):
        owner.stop_context()
    assert database_state(connection) == before
    assert connection.total_changes == changes
    assert not connection.in_transaction


def test_no_issued_generation_at_epoch_zero_cannot_be_replaced_by_a_minted_identity(native):
    native.owner()
    native.control.close()
    connection = native.store.db
    connection.execute("DELETE FROM agent_control_generations")
    connection.commit()
    assert connection.execute("SELECT instance_id FROM agent_control_state").fetchone()[0] is None
    assert connection.execute("SELECT count(*) FROM agent_control_generations").fetchone()[0] == 0
    before = database_state(connection)
    read_control = AgentControl(native.store)
    with pytest.raises(StandingUnavailable):
        owner = native.owner(control=read_control)
        owner.stop_context()
    assert database_state(connection) == before
    assert read_control.instance is None


def test_unregistered_or_closed_local_capability_cannot_discover_or_initialize_context(native):
    owner = native.owner(register=False)
    before = database_state(native.store.db)
    with pytest.raises(StandingUnavailable):
        owner.stop_context()
    assert database_state(native.store.db) == before
    assert not TABLES & set(before[1])
    owner.register()
    owner.close()
    before = database_state(native.store.db)
    with pytest.raises(StandingUnavailable):
        owner.stop_context()
    assert database_state(native.store.db) == before
    assert native.control.snapshot().control_writable is True
