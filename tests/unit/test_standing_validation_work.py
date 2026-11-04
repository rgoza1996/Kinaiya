"""Algorithmic validation-work bounds for standing resource finalization.

Guards the standing-finalization contract and owner validation call graph
against reintroducing redundant nested model reconstructions or repeated
canonical-byte revalidations (Issue #356 / PR #374), without wall-clock timing
thresholds, sleeps, or host-speed assumptions.

Supported scope and rationale:
- Python >= 3.11 and Pydantic >= 2.10, < 3 (configured with
  ``revalidate_instances="always"`` on ``_Closed`` and ``_Finalization``).
- In ``packages.contracts.standing_resource_finalization``, ``_Finalization``
  runs a ``mode="wrap"`` validator (``complete_graph``) that calls
  ``_fields(value, cls)``. Because child models in ``_PACKETS`` run their own
  ``complete_graph`` wrap validator when Pydantic validates the enclosing model,
  ``_fields`` leaves ``nested in _PACKETS`` children for their own validator
  frame and rebuilds non-``_PACKETS`` ``BaseModel`` children into plain field
  dictionaries once.
- Similarly, ``mode="before"`` validators in ``standing_resources``,
  ``standing_closure``, and ``standing_execution`` verify exact model/dict
  closure and return a field dictionary so Pydantic's field validation constructs
  each child model once rather than twice, and ``StandingInitialProof`` verifies
  its entry digest via ``_standing_entry_digest`` on its already-validated
  ``claim`` rather than re-running ``_checked(claim, StandingExecutionClaim)``.
- Every assertion below derives its expected count from the exact structure of
  the validated object graph or SQLite ledger (e.g., 1x per model node or 1x
  per model field slot, scaling linearly as O(N) across N=4, 16, 32 ledger rows),
  paired with negative controls that simulate the pre-#374 redundant passes.
"""

from __future__ import annotations

import copy
import os
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from test_standing_agent_client import RESPONSIBILITY, SECRET, make_intent, renewal, workspace
from test_standing_resource_finalization_contracts import (
    BACKEND,
    ERROR,
    GOLDEN_INTENT,
    INSTANCE,
    encoded,
    vectors,
)

import packages.contracts.standing_admission as standing_admission
import packages.contracts.standing_closure as standing_closure
import packages.contracts.standing_execution as standing_execution
import packages.contracts.standing_resource_finalization as standing_finalization_contract
import packages.contracts.standing_resources as standing_resources
from packages.contracts.agent_control import AgentModeChange
from packages.contracts.models import SessionRequest
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_closure import StandingAdmissionClosure
from packages.contracts.standing_dispatch import StandingDispatchActivation
from packages.contracts.standing_execution import StandingExecutionClaim, StandingExecutionIntent
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceBindingRequest,
    StandingResourceFinalization,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    checked_finalization,
    finalization_bytes,
    resource_finalization_digest,
)
from packages.contracts.standing_resources import (
    StandingResourceObservation,
    StandingResourceReservation,
    TaskResourceContext,
    TaskResourceQuery,
)
from packages.contracts.standing_work import StandingChange, StandingConflict, StandingUnavailable
from services.common.auth import Auth
from services.resources.standing_barrier import (
    _SQL as BARRIER_SQL,
)
from services.resources.standing_barrier import (
    _TABLE as BARRIER_TABLE,
)
from services.resources.standing_barrier import (
    StandingBarrier,
)
from services.resources.standing_finalization import (
    _SQL as FINAL_SQL,
)
from services.resources.standing_finalization import (
    _TABLE as FINAL_TABLE,
)
from services.resources.standing_finalization import (
    StandingFinalization,
)
from services.resources.standing_handoff import (
    _DEFINITIONS as HANDOFF_DEFINITIONS,
)
from services.resources.standing_handoff import (
    _INTENTS as HANDOFF_INTENTS,
)
from services.resources.standing_handoff import (
    _META as HANDOFF_META,
)
from services.resources.standing_handoff import (
    StandingHandoff,
)
from services.session.api import create_app
from services.session.store import Store

PRIVATE = "SYNTHETIC-PRIVATE-VALIDATION-WORK-MARKER"
LEASE_ID = "00000000-0000-4000-8000-000000000992"
LARGE_OBJECTIVE = "\0" * 10500

PACKET_KINDS = (
    ("StandingResourceBindingRequest", StandingResourceBindingRequest),
    ("StandingResourceBindingReceipt", StandingResourceBindingReceipt),
    ("StandingResourceFinalizeRequest", StandingResourceFinalizeRequest),
    ("StandingResourceFinalizeContext", StandingResourceFinalizeContext),
    ("StandingResourceFinalization", StandingResourceFinalization),
    ("StandingResourceFinalizationReceipt", StandingResourceFinalizationReceipt),
)

GRAPH_MODES = (
    ("admitted_reserved", {"admitted": True, "reserved": True}),
    (
        "admitted_registered_no_lease",
        {
            "admitted": True,
            "reserved": True,
            "resource_disposition": "registered-no-lease",
            "restoration": "not-required",
        },
    ),
    ("admitted_unreserved", {"admitted": True, "reserved": False}),
    ("unadmitted_unreserved", {"admitted": False, "reserved": False}),
)


def large_vectors(**options):
    original = copy.deepcopy(GOLDEN_INTENT)
    original["task"]["objective"] = LARGE_OBJECTIVE
    return vectors(original=original, **options)


def model_nodes(value):
    """Yield every BaseModel instance in the validated object tree."""
    if isinstance(value, BaseModel):
        yield value
        for child in value.__dict__.values():
            yield from model_nodes(child)
    elif type(value) in (list, tuple):
        for item in value:
            yield from model_nodes(item)
    elif type(value) is dict:
        for item in value.values():
            yield from model_nodes(item)


def expected_graph_work(root: BaseModel) -> dict[str, int]:
    """Derive exact 1x-per-node and 1x-per-field work bounds from a model graph."""
    nodes = list(model_nodes(root))
    packet_set = set(standing_finalization_contract._PACKETS)
    return {
        "packet_frames": sum(1 for node in nodes if type(node) in packet_set),
        "model_fields": sum(len(type(node).model_fields) for node in nodes),
        "claim_digests": sum(1 for node in nodes if type(node) is StandingExecutionClaim),
        "entry_digests": sum(1 for node in nodes if type(node) is StandingInitialProof),
        "closure_digests": sum(1 for node in nodes if type(node) is StandingAdmissionClosure),
        "observations": sum(1 for node in nodes if type(node) is StandingResourceObservation),
        "resource_contexts": sum(1 for node in nodes if type(node) is TaskResourceContext),
    }


def instrument_contract_boundaries(monkeypatch):
    """Wrap narrow contract validation boundaries and return live call counters."""
    counts = {
        "packet_frames": 0,
        "model_fields": 0,
        "claim_digests": 0,
        "entry_digests": 0,
        "closure_digests": 0,
        "observations": 0,
        "resource_contexts": 0,
    }
    orig_fields = standing_finalization_contract._fields
    orig_model_kind = standing_finalization_contract._model_kind
    orig_claim_digest = standing_execution._claim_digest
    orig_entry_digest = standing_admission._standing_entry_digest
    orig_closure_digest = standing_closure._digest
    orig_obs_body = standing_resources._observation_body_from_fields
    orig_ctx_body = standing_resources._context_body_from_fields

    def wrapped_fields(value, kind):
        counts["packet_frames"] += 1
        return orig_fields(value, kind)

    def wrapped_model_kind(annotation):
        counts["model_fields"] += 1
        return orig_model_kind(annotation)

    def wrapped_claim_digest(*args, **kwargs):
        counts["claim_digests"] += 1
        return orig_claim_digest(*args, **kwargs)

    def wrapped_entry_digest(*args, **kwargs):
        counts["entry_digests"] += 1
        return orig_entry_digest(*args, **kwargs)

    def wrapped_closure_digest(*args, **kwargs):
        counts["closure_digests"] += 1
        return orig_closure_digest(*args, **kwargs)

    def wrapped_obs_body(*args, **kwargs):
        counts["observations"] += 1
        return orig_obs_body(*args, **kwargs)

    def wrapped_ctx_body(*args, **kwargs):
        counts["resource_contexts"] += 1
        return orig_ctx_body(*args, **kwargs)

    monkeypatch.setattr(standing_finalization_contract, "_fields", wrapped_fields)
    monkeypatch.setattr(standing_finalization_contract, "_model_kind", wrapped_model_kind)
    monkeypatch.setattr(standing_execution, "_claim_digest", wrapped_claim_digest)
    monkeypatch.setattr(standing_admission, "_standing_entry_digest", wrapped_entry_digest)
    monkeypatch.setattr(standing_closure, "_digest", wrapped_closure_digest)
    monkeypatch.setattr(standing_resources, "_observation_body_from_fields", wrapped_obs_body)
    monkeypatch.setattr(standing_resources, "_context_body_from_fields", wrapped_ctx_body)
    return counts


@pytest.mark.parametrize("mode,options", GRAPH_MODES, ids=[m[0] for m in GRAPH_MODES])
@pytest.mark.parametrize("name,kind", PACKET_KINDS, ids=[p[0] for p in PACKET_KINDS])
def test_finalization_packet_validation_visits_each_nested_record_and_field_once(
    mode, options, name, kind, monkeypatch
):
    """Each _Finalization parse/revalidation visits every model node and field slot 1x."""
    bundle = large_vectors(**options)
    raw = encoded(bundle[name])
    counts = instrument_contract_boundaries(monkeypatch)

    parsed = kind.from_json(raw)
    expected = expected_graph_work(parsed)
    assert counts == expected, (mode, name, counts, expected)

    for key in counts:
        counts[key] = 0
    rechecked = checked_finalization(parsed, kind)
    assert rechecked is not parsed
    assert counts == expected, (mode, name, counts, expected)

    for key in counts:
        counts[key] = 0
    wire = finalization_bytes(rechecked)
    assert wire == raw
    assert counts == expected, (mode, name, counts, expected)


def test_negative_control_detects_reintroduced_nested_revalidation_while_refusing_malformed_graphs(
    monkeypatch,
):
    """Simulating pre-#374 redundant nested reconstruction exceeds the 1x bound and fails."""
    bundle = large_vectors(admitted=True, reserved=True)
    raw = encoded(bundle["StandingResourceFinalizationReceipt"])
    baseline_model = StandingResourceFinalizationReceipt.from_json(raw)
    bound = expected_graph_work(baseline_model)
    assert bound == {
        "packet_frames": 5,
        "model_fields": 334,
        "claim_digests": 3,
        "entry_digests": 2,
        "closure_digests": 1,
        "observations": 1,
        "resource_contexts": 1,
    }

    counts = instrument_contract_boundaries(monkeypatch)
    instrumented_fields = standing_finalization_contract._fields
    instrumented_entry_digest = standing_admission._standing_entry_digest
    orig_field_dict = standing_resources._field_dict

    # Negative control 1: simulate pre-#374 _fields recursion into _PACKETS children.
    def pre374_recursive_fields(value, kind):
        result = instrumented_fields(value, kind)
        for field_name, field in kind.model_fields.items():
            child = result.get(field_name)
            nested = standing_finalization_contract._model_kind(field.annotation)
            if nested in standing_finalization_contract._PACKETS and child is not None:
                result[field_name] = standing_finalization_contract._fields(child, nested)
        return result

    # Negative control 2: simulate pre-#374 eager model_validate in _field_dict
    # and redundant StandingExecutionClaim recheck in _standing_entry_digest.
    def pre374_complete_in_before_validator(value, kind):
        return kind.model_validate(orig_field_dict(value, kind), strict=True)

    def pre374_entry_digest_with_claim_recheck(claim, event_id, sequence, created_at):
        standing_execution._checked(claim, StandingExecutionClaim)
        return instrumented_entry_digest(claim, event_id, sequence, created_at)

    with monkeypatch.context() as neg_ctx:
        neg_ctx.setattr(standing_finalization_contract, "_fields", pre374_recursive_fields)
        neg_ctx.setattr(standing_resources, "_field_dict", pre374_complete_in_before_validator)
        neg_ctx.setattr(
            standing_admission, "_standing_entry_digest", pre374_entry_digest_with_claim_recheck
        )
        for key in counts:
            counts[key] = 0
        parsed_neg = StandingResourceFinalizationReceipt.from_json(raw)
        assert finalization_bytes(parsed_neg) == raw
        # Pre-#374 redundant passes blow past the 1x algorithmic work bound on every axis:
        assert counts["packet_frames"] > bound["packet_frames"] * 2
        assert counts["model_fields"] > bound["model_fields"] * 3
        assert counts["claim_digests"] > bound["claim_digests"] * 5
        assert counts["entry_digests"] > bound["entry_digests"] * 4
        with pytest.raises(AssertionError):
            assert counts == bound

    # Restored production validators still refuse malformed raw and in-memory graphs.
    bad_raw = copy.deepcopy(bundle["StandingResourceFinalizationReceipt"])
    bad_raw["finalization"]["context"]["binding"]["claim"]["request"]["task"]["model_pin"][
        "reasoning"
    ] = PRIVATE
    with pytest.raises(ValueError) as caught_raw:
        StandingResourceFinalizationReceipt.from_json(encoded(bad_raw))
    assert type(caught_raw.value) is ValueError and str(caught_raw.value) == ERROR
    assert PRIVATE not in str(caught_raw.value) and caught_raw.value.__cause__ is None

    forged = StandingResourceFinalizationReceipt.from_json(raw)
    pin = forged.finalization.context.binding.claim.request.task.model_pin
    object.__setattr__(pin, "__pydantic_extra__", {PRIVATE: True})
    with pytest.raises(ValueError) as caught_forged:
        checked_finalization(forged, StandingResourceFinalizationReceipt)
    assert type(caught_forged.value) is ValueError and str(caught_forged.value) == ERROR
    assert PRIVATE not in str(caught_forged.value) and caught_forged.value.__cause__ is None


def populate_resources_ledger(conn: sqlite3.Connection, count: int) -> dict[str, int]:
    """Populate N standing claims across the 4 finalization modes and return exact record counts."""
    expected = {
        "barrier_rows": count,
        "witness_rows": count,
        "packet_frames": 0,
        "claim_digests": 0,
        "entry_digests": 0,
    }
    with conn:
        conn.execute(f"DELETE FROM {HANDOFF_INTENTS}")
        conn.execute(f"DELETE FROM {BARRIER_TABLE}")
        conn.execute(f"DELETE FROM {FINAL_TABLE}")
        for index in range(count):
            original = copy.deepcopy(GOLDEN_INTENT)
            original["idempotency_key"] = f"standing-work-scale-{index}"
            original["task"]["task_id"] = f"00000000-0000-4000-8000-{index + 1:012d}"
            slot = index % 4
            if slot == 0:
                original["task"]["objective"] = LARGE_OBJECTIVE
                bundle = vectors(admitted=True, reserved=True, original=original)
                phase_f, acquired, effects, restoration = "complete", 1, 1, "restored"
            elif slot == 1:
                bundle = vectors(
                    admitted=True,
                    reserved=True,
                    resource_disposition="registered-no-lease",
                    restoration="not-required",
                    original=original,
                )
                phase_f, acquired, effects, restoration = "never", 0, 0, "not-required"
            elif slot == 2:
                bundle = vectors(admitted=True, reserved=False, original=original)
                phase_f, acquired, effects, restoration = "covered", 0, 0, "not-required"
            else:
                bundle = vectors(admitted=False, reserved=False, original=original)
                phase_f, acquired, effects, restoration = "covered", 0, 0, "not-required"

            reservation_dict = bundle["StandingResourceFinalizeContext"]["observation"][
                "reservation"
            ]
            if reservation_dict is not None:
                action_key = f"handoff-scale-{index}"
                bundle["StandingResourceFinalization"]["handoff_action_key"] = action_key
                bundle["StandingResourceFinalization"]["finalization_digest"] = (
                    standing_finalization_contract._hash(
                        b"kinaiya.standing.resource-finalization.v1",
                        {
                            k: v
                            for k, v in bundle["StandingResourceFinalization"].items()
                            if k != "finalization_digest"
                        },
                    )
                )
                reservation_raw = encoded(reservation_dict).decode("utf-8")
                lease_id = reservation_dict["lease_id"]
                reservation_receipt = {
                    "schema_version": "1.0",
                    "disposition": "created",
                    "request": reservation_dict,
                    "request_digest": standing_finalization_contract._hash(
                        b"kinaiya.standing.resource-reservation.v1", reservation_dict
                    ),
                }
                conn.execute(
                    f"INSERT INTO {HANDOFF_INTENTS} VALUES(?,?,?,?)",
                    (
                        action_key,
                        reservation_dict["action_digest"],
                        reservation_raw,
                        encoded(reservation_receipt).decode("utf-8"),
                    ),
                )
                # Handoff _schema parses request (1 claim, 1 proof) + receipt (1 claim, 1 proof).
                expected["claim_digests"] += 2
                expected["entry_digests"] += 2
            else:
                reservation_raw = action_key = lease_id = None

            claim_raw = encoded(bundle["StandingResourceBindingRequest"]["claim"]).decode("utf-8")
            bind_raw = encoded(bundle["StandingResourceBindingRequest"]).decode("utf-8")
            rcpt_raw = encoded(bundle["StandingResourceBindingReceipt"]).decode("utf-8")
            seal_raw = encoded(bundle["StandingResourceFinalizeRequest"]).decode("utf-8")
            ctx_raw = encoded(bundle["StandingResourceFinalizeContext"]).decode("utf-8")
            rep_raw = encoded(bundle["StandingResourceFinalization"]).decode("utf-8")
            cid = bundle["StandingResourceBindingRequest"]["claim"]["claim_id"]
            tid = original["task"]["task_id"]

            conn.execute(
                f"INSERT INTO {BARRIER_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    tid,
                    original["expected_backend"],
                    BACKEND,
                    INSTANCE,
                    claim_raw,
                    bind_raw,
                    rcpt_raw,
                    seal_raw,
                    ctx_raw,
                    "drained",
                ),
            )
            conn.execute(
                f"INSERT INTO {FINAL_TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    bind_raw,
                    action_key,
                    reservation_raw,
                    lease_id,
                    phase_f,
                    acquired,
                    effects,
                    restoration,
                    5,
                    8,
                    rep_raw,
                ),
            )
            # Barrier row has 7 _PACKETS frames (binding=1, receipt=2, seal=1, context=3);
            # Witness row has 5 _PACKETS frames (binding=1, report=4).
            expected["packet_frames"] += 12
            claims_in_context = 3 if slot in (0, 1) else (2 if slot == 2 else 1)
            proofs_in_context = 2 if slot in (0, 1) else (1 if slot == 2 else 0)
            # Barrier row parses: claim (1), binding (1), receipt (1), context (claims_in_context).
            # Witness row parses: binding (1), reservation (1 if reserved else 0), report (claims_in_context).
            expected["claim_digests"] += (3 + claims_in_context) + (
                1 + (1 if reservation_dict is not None else 0) + claims_in_context
            )
            expected["entry_digests"] += proofs_in_context + (
                (1 if reservation_dict is not None else 0) + proofs_in_context
            )
    return expected


@pytest.mark.parametrize("claim_count", (4, 16, 32))
def test_resources_finalization_schema_validation_scales_linearly_with_claim_count(
    tmp_path, claim_count, monkeypatch
):
    """StandingFinalization._schema() validates each stored row and packet 1x (O(N) linear work)."""
    db_path = tmp_path / "resource-actions.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    os.chmod(db_path, 0o600)
    stat_info = db_path.stat()

    owner = SimpleNamespace(
        controller_id=INSTANCE,
        standing_handoff=None,
        standing_barrier=None,
        standing_finalization=None,
        state=SimpleNamespace(path=tmp_path / "state.json"),
    )
    handoff = StandingHandoff(owner)
    handoff.db = conn
    handoff.backend = BACKEND
    handoff.ready = True
    handoff.file_identity = (stat_info.st_dev, stat_info.st_ino)
    owner.standing_handoff = handoff
    barrier = StandingBarrier(owner)
    barrier.gate = handoff
    owner.standing_barrier = barrier
    finalization = StandingFinalization(owner)
    finalization.gate = handoff
    finalization.barrier = barrier
    owner.standing_finalization = finalization

    try:
        with conn:
            for sql in HANDOFF_DEFINITIONS.values():
                conn.execute(sql)
            conn.execute(f"INSERT INTO {HANDOFF_META} VALUES(1,1,?,?)", (BACKEND, INSTANCE))
            conn.execute(BARRIER_SQL)
            conn.execute(FINAL_SQL)

        expected = populate_resources_ledger(conn, claim_count)
        contract_counts = instrument_contract_boundaries(monkeypatch)
        row_counts = {"barrier_rows": 0, "witness_rows": 0}
        orig_barrier_row = barrier._checked_row
        orig_witness_row = finalization._checked

        def wrapped_barrier_row(row):
            row_counts["barrier_rows"] += 1
            return orig_barrier_row(row)

        def wrapped_witness_row(row, *, barrier_checked=False):
            row_counts["witness_rows"] += 1
            return orig_witness_row(row, barrier_checked=barrier_checked)

        monkeypatch.setattr(barrier, "_checked_row", wrapped_barrier_row)
        monkeypatch.setattr(finalization, "_checked", wrapped_witness_row)

        finalization._schema()

        observed = {
            "barrier_rows": row_counts["barrier_rows"],
            "witness_rows": row_counts["witness_rows"],
            "packet_frames": contract_counts["packet_frames"],
            "claim_digests": contract_counts["claim_digests"],
            "entry_digests": contract_counts["entry_digests"],
        }
        assert observed == expected
        # Exact linear O(N) constants per claim across N=4, 16, 32:
        assert observed["barrier_rows"] == claim_count
        assert observed["witness_rows"] == claim_count
        assert observed["packet_frames"] == 12 * claim_count
        assert observed["claim_digests"] == 10 * claim_count
        assert observed["entry_digests"] == 4 * claim_count

        if claim_count == 16:
            # Negative control: reintroducing pre-#374 unguarded barrier_checked=False
            # and redundant checked_finalization on parsed rows exceeds the linear bound.
            def pre374_witness_row(row, *, barrier_checked=False):
                row_counts["witness_rows"] += 1
                binding, reservation, report = orig_witness_row(row, barrier_checked=False)
                if reservation is not None:
                    checked_finalization(reservation, StandingResourceReservation)
                return binding, reservation, report

            with monkeypatch.context() as neg_ctx:
                neg_ctx.setattr(finalization, "_checked", pre374_witness_row)
                row_counts["barrier_rows"] = 0
                row_counts["witness_rows"] = 0
                for key in contract_counts:
                    contract_counts[key] = 0
                finalization._schema()
                assert row_counts["barrier_rows"] == 2 * claim_count > expected["barrier_rows"]
                assert contract_counts["packet_frames"] > expected["packet_frames"]
                assert contract_counts["claim_digests"] > expected["claim_digests"]
                with pytest.raises(AssertionError):
                    assert row_counts["barrier_rows"] == expected["barrier_rows"]

            # Malformed stored row in SQLite is still refused by _schema().
            with conn:
                conn.execute(f"UPDATE {FINAL_TABLE} SET report = report || ' '")
            with pytest.raises(StandingUnavailable):
                finalization._schema()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "admitted,reserved,expected_claims,expected_entries",
    (
        (
            True,
            True,
            {"context": 14, "report_created": 37, "report_retained": 44},
            {"context": 9, "report_created": 24, "report_retained": 29},
        ),
        (
            True,
            False,
            {"context": 11, "report_created": 28, "report_retained": 32},
            {"context": 6, "report_created": 15, "report_retained": 17},
        ),
        (
            False,
            False,
            {"context": 3, "report_created": 9, "report_retained": 11},
            {"context": 0, "report_created": 0, "report_retained": 0},
        ),
    ),
    ids=("admitted_reserved", "admitted_unreserved", "unadmitted_unreserved"),
)
async def test_session_finalization_owner_reuses_validated_wire_bytes_on_large_graphs(
    tmp_path, admitted, reserved, expected_claims, expected_entries, monkeypatch
):
    """StandingResourceFinalizationOwner avoids redundant finalization_bytes revalidation."""
    now = [datetime(2024, 1, 1, 8, 59, 0, tzinfo=UTC)]
    store = Store(tmp_path / "session.sqlite")
    tokens = {
        role: f"{role}-synthetic-" + ("x" * 32)
        for role in ("owner", "agent", "converse", "decisions", "resources")
    }
    app = create_app(
        store, Auth(tokens), standing_clock=lambda: now[0], standing_monotonic_ms=lambda: 1000
    )
    try:
        async with app.router.lifespan_context(app):
            control = app.state.agent_control.snapshot()
            session = store.create_session(SessionRequest(channel="agent"))
            scope = {
                field: getattr(session, field) for field in ("session_id", "thread_id", "epoch")
            }
            change = StandingChange.model_validate(
                {
                    "schema_version": "1.0",
                    "idempotency_key": "native-owner-large",
                    "expected_backend": control.backend_id,
                    "expected_instance": control.instance_id,
                    "expected_revision": 0,
                    "responsibility_id": RESPONSIBILITY,
                    "authorization": {
                        "label": "Large standing validation work fixture",
                        "objective": LARGE_OBJECTIVE,
                        "content_scope": "public_synthetic",
                        "enabled": True,
                        "task_session": scope,
                        "workspace_pin": workspace(),
                        "schedule": {
                            "kind": "daily_local",
                            "timezone": "UTC",
                            "local_time": "09:00",
                            "fold_policy": "earlier",
                            "gap_policy": "skip",
                            "grace_seconds": 600,
                            "missed_run_policy": "latest_within_grace",
                            "start_local_date": "2024-01-01",
                            "valid_until_utc": "2024-01-02T10:00:00Z",
                        },
                        "delivery": {
                            "kind": "in_app",
                            "attention": "routine",
                            "preview": "status_only",
                            "quiet_hours": None,
                        },
                        "overlap_policy": "one_unresolved_per_authorization",
                        "maximum_runs": 1,
                        "inference_authorization": "configured_agent_profile",
                    },
                }
            )
            app.state.standing_owner.change(change)
            producer = app.state.standing_dispatch_owner.renew(
                renewal(control.backend_id, control.instance_id)
            )
            dispatch = app.state.standing_dispatch_owner.snapshot()
            activation = app.state.standing_dispatch_owner.activate(
                StandingDispatchActivation(
                    schema_version="1.0",
                    expected_epoch=dispatch.epoch,
                    expected_fence_token=dispatch.fence_token,
                    mode_change=AgentModeChange(
                        schema_version="1.0",
                        idempotency_key="native-activation-large",
                        expected_backend=control.backend_id,
                        expected_instance=control.instance_id,
                        expected_revision=control.revision,
                        mode="autonomous",
                        active_work_policy="finish_already_accepted",
                    ),
                )
            )
            record = app.state.standing_owner.snapshot(RESPONSIBILITY).record
            base_intent = make_intent(
                backend=control.backend_id,
                instance=control.instance_id,
                task_session=scope,
                producer=producer,
                authorization_digest=record.receipt.authorization_digest,
                record_digest=record.record_digest,
                activation_digest=activation.activation_digest,
            )
            raw_intent = base_intent.model_dump(mode="json")
            raw_intent["task"]["objective"] = LARGE_OBJECTIVE
            original = StandingExecutionIntent.model_validate(raw_intent)
            now[0] = datetime(2024, 1, 1, 9, 0, 1, tzinfo=UTC)

            claim_adm = app.state.standing_execution_owner.prepare(original)
            final_owner = app.state.standing_resource_finalization_owner
            res_owner = app.state.standing_resource_owner

            bind_req = StandingResourceBindingRequest(
                schema_version="1.0",
                claim=claim_adm.claim,
                resource_backend=BACKEND,
                resource_instance=INSTANCE,
            )
            final_owner.bind(bind_req)
            if admitted:
                app.state.standing_task_owner.admit(original, capability=SECRET)
            res_req = None
            if reserved:
                task_ctx = res_owner.context(
                    TaskResourceQuery(schema_version="1.0", task_id=original.task.task_id)
                )
                res_req = StandingResourceReservation(
                    schema_version="1.0",
                    context=task_ctx,
                    resource_backend=BACKEND,
                    resource_instance=INSTANCE,
                    action_digest="e" * 64,
                    lease_id=LEASE_ID,
                )
                res_owner.reserve(res_req)
            app.state.standing_task_owner.close_admission(original, capability=SECRET)

            fin_req = StandingResourceFinalizeRequest(
                schema_version="1.0",
                idempotency_key="finalize:large-session",
                original=original,
                expected_resource_backend=BACKEND,
                expected_resource_instance=INSTANCE,
            )

            counts = instrument_contract_boundaries(monkeypatch)
            fb_calls = [0]
            orig_fb = standing_finalization_contract.finalization_bytes

            def wrapped_fb(value):
                fb_calls[0] += 1
                return orig_fb(value)

            monkeypatch.setattr(standing_finalization_contract, "finalization_bytes", wrapped_fb)
            import services.session.standing_resource_finalization as session_srf

            monkeypatch.setattr(session_srf, "finalization_bytes", wrapped_fb)

            ctx = final_owner.context(fin_req)
            # context() validates fin_req (1), stored binding (1), and builds context (3) = 5 packet frames,
            # with 0 calls to finalization_bytes.
            assert fb_calls[0] == 0
            assert counts["packet_frames"] == 5
            assert counts["claim_digests"] == expected_claims["context"]
            assert counts["entry_digests"] == expected_entries["context"]

            disposition = "matching-lease-finalized" if reserved else "no-reservation"
            action_key = "action:large-session" if reserved else None
            action_digest = res_req.action_digest if reserved else None
            lease_id = LEASE_ID if reserved else None
            restoration = "restored" if reserved else "not-required"
            report_packet = StandingResourceFinalization(
                schema_version="1.0",
                context=ctx,
                resource_disposition=disposition,
                handoff_action_key=action_key,
                handoff_action_digest=action_digest,
                reserved_lease_id=lease_id,
                policy_revision=5,
                owner_intent_revision=8,
                restoration=restoration,
                finalization_digest=resource_finalization_digest(
                    ctx, disposition, action_key, action_digest, lease_id, 5, 8, restoration
                ),
            )

            for key in counts:
                counts[key] = 0
            fb_calls[0] = 0
            created_receipt = final_owner.report(report_packet)
            assert created_receipt.disposition == "created"
            assert fb_calls[0] == 0
            assert counts["packet_frames"] == 21
            assert counts["claim_digests"] == expected_claims["report_created"]
            assert counts["entry_digests"] == expected_entries["report_created"]

            for key in counts:
                counts[key] = 0
            fb_calls[0] = 0
            retained_receipt = final_owner.report(report_packet)
            assert retained_receipt.disposition == "retained"
            assert fb_calls[0] == 0
            assert counts["packet_frames"] == 28
            assert counts["claim_digests"] == expected_claims["report_retained"]
            assert counts["entry_digests"] == expected_entries["report_retained"]

            if admitted and reserved:
                # Negative control: if top-level _encode wire comparisons in report()
                # ran checked_finalization first (matching pre-#374 finalization_bytes),
                # packet_frames and claim_digests exceed the bounded call-graph work.
                orig_encode = standing_finalization_contract._encode
                in_recheck = False

                def pre374_revalidating_encode(value):
                    nonlocal in_recheck
                    if not in_recheck and type(value) in standing_finalization_contract._PACKETS:
                        in_recheck = True
                        try:
                            checked_finalization(value, type(value))
                        finally:
                            in_recheck = False
                    return orig_encode(value)

                with monkeypatch.context() as neg_ctx:
                    neg_ctx.setattr(
                        standing_finalization_contract, "_encode", pre374_revalidating_encode
                    )
                    for key in counts:
                        counts[key] = 0
                    final_owner.report(report_packet)
                    assert counts["packet_frames"] > 28
                    assert counts["claim_digests"] > expected_claims["report_retained"]
                    with pytest.raises(AssertionError):
                        assert counts["packet_frames"] == 28

                # Foreign valid report is still refused with StandingConflict.
                foreign_ctx_raw = ctx.model_dump(mode="json")
                foreign_ctx_raw["request"]["idempotency_key"] = "finalize:foreign-key"
                foreign_ctx_raw["context_digest"] = standing_finalization_contract._hash(
                    b"kinaiya.standing.resource-finalize-context.v1",
                    {k: v for k, v in foreign_ctx_raw.items() if k != "context_digest"},
                )
                foreign_ctx = StandingResourceFinalizeContext.model_validate(foreign_ctx_raw)
                foreign_report = StandingResourceFinalization(
                    schema_version="1.0",
                    context=foreign_ctx,
                    resource_disposition=disposition,
                    handoff_action_key=action_key,
                    handoff_action_digest=action_digest,
                    reserved_lease_id=lease_id,
                    policy_revision=5,
                    owner_intent_revision=8,
                    restoration=restoration,
                    finalization_digest=resource_finalization_digest(
                        foreign_ctx,
                        disposition,
                        action_key,
                        action_digest,
                        lease_id,
                        5,
                        8,
                        restoration,
                    ),
                )
                with pytest.raises(StandingConflict):
                    final_owner.report(foreign_report)
    finally:
        store.close()
