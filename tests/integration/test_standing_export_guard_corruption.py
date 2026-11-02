"""Independent ordinary-export refusals for corrupted standing native storage.

Every positive initial comes from real SQLite admission. Counterfeit pure
packets below are negative storage corruption, never native admission ACKs.
"""

import json

import pytest
from test_standing_execution_owner import database_state, make_claim, uid
from test_standing_export_admission import accepted as accepted
from test_standing_export_admission import build_native as build_native
from test_standing_export_admission import native as native
from test_standing_export_admission import old_export, safe
from test_standing_export_admission import prepared as prepared

from packages.contracts.standing_admission import StandingInitialProof, standing_initial_proof
from packages.contracts.standing_execution import (
    StandingExecutionIntent,
    standing_initial_request,
)
from services.session.store import JournalError


def missing_initial(accepted, *, replacement=None):
    connection = accepted.native.store.db
    before = dict(connection.execute("SELECT * FROM standing_task_admissions").fetchone())
    with connection:
        deleted = connection.execute(
            "DELETE FROM entries WHERE event_id=?", (accepted.proof.event_id,)
        )
        assert deleted.rowcount == 1
        if replacement is not None:
            connection.execute(
                "UPDATE standing_task_admissions SET proof=? WHERE claim_id=?",
                (replacement, before["claim_id"]),
            )
    after = dict(connection.execute("SELECT * FROM standing_task_admissions").fetchone())
    assert {key: after[key] for key in ("claim_id", "event_id", "sequence")} == {
        key: before[key] for key in ("claim_id", "event_id", "sequence")
    }
    assert connection.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    assert after["proof"] == (before["proof"] if replacement is None else replacement)


def ordinary_refused_without_mutation(accepted):
    ordinary = old_export(accepted.proof, content_scope="public_synthetic")
    assert ordinary.task_id == accepted.proof.claim.request.task.task_id
    assert ordinary.content_scope == "public_synthetic"
    assert accepted.proof.claim.request.task.content_scope == "local_only"
    connection = accepted.native.store.db
    before, changes = database_state(connection), connection.total_changes
    with pytest.raises(JournalError) as refused:
        accepted.native.store.agent_export_admission(ordinary)
    safe(refused.value)
    assert database_state(connection) == before
    assert connection.total_changes == changes
    assert not connection.in_transaction


def test_missing_initial_and_unrehashed_retained_task_id_cannot_become_ordinary(accepted):
    raw = accepted.proof.model_dump(mode="json")
    original_claim_digest = raw["claim"]["claim_digest"]
    original_entry_digest = raw["entry_digest"]
    raw["claim"]["request"]["task"]["task_id"] = uid(9201)
    assert raw["claim"]["request"]["task"]["task_id"] != accepted.proof.claim.request.task.task_id
    assert raw["claim"]["claim_digest"] == original_claim_digest
    assert raw["entry_digest"] == original_entry_digest
    # The pure parser confirms this is malformed proof, not another ACK.
    with pytest.raises(ValueError):
        StandingInitialProof.model_validate(raw)
    missing_initial(accepted, replacement=json.dumps(raw, sort_keys=True, separators=(",", ":")))
    ordinary_refused_without_mutation(accepted)


def test_missing_initial_and_valid_foreign_packet_cannot_replace_native_link(accepted):
    raw = accepted.proof.claim.request.model_dump(mode="json")
    raw["task"]["task_id"] = uid(9202)
    candidate = StandingExecutionIntent.model_validate(raw)
    claim = make_claim(candidate, prepared=accepted.proof.claim.prepared_utc)
    # Pure builders make a self-consistent counterfeit candidate. No native
    # owner accepts it and no SQL claim/link identity is replaced with it.
    counterfeit = standing_initial_proof(
        claim,
        {
            **standing_initial_request(claim).model_dump(mode="json"),
            "event_id": "counterfeit-initial-event",
            "sequence": accepted.proof.sequence + 1,
            "origin": "standing-supervisor",
            "created_at": accepted.proof.created_at,
        },
    )
    assert counterfeit.validate_request(candidate) == counterfeit
    assert counterfeit.claim.claim_id != accepted.proof.claim.claim_id
    assert counterfeit.event_id != accepted.proof.event_id
    assert counterfeit.sequence != accepted.proof.sequence
    missing_initial(accepted, replacement=counterfeit.canonical_bytes().decode("utf-8"))
    ordinary_refused_without_mutation(accepted)


def test_genuine_retained_proof_still_fences_ordinary_export_without_initial(accepted):
    missing_initial(accepted)
    ordinary_refused_without_mutation(accepted)
