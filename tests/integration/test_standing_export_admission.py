"""Independent native standing export oracles; no inference or worker launch.

Positive initial proof always comes from actual qualified SQLite admission.
Counterfeit/corrupt candidates occur only in explicit refusal tests.
"""

import importlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from test_agent_submit_admission import proposal as ordinary_proposal
from test_standing_execution_owner import (
    MARKER,
    SECRET,
    database_state,
    mode_request,
    uid,
)
from test_standing_task_admission import (
    assert_created,
    prepare,
)
from test_standing_task_admission import (
    build_native as build_native,
)
from test_standing_task_admission import (
    native as native,
)
from test_standing_task_admission import (
    prepared as prepared,
)

from packages.contracts.agent_control import MAX_AGENT_CONTROL_INTEGER
from packages.contracts.agent_export import AgentExportAdmission, AgentExportRequest
from packages.contracts.models import AppendRequest, SessionRequest
from packages.contracts.standing_admission import StandingInitialProof, standing_entry_digest
from packages.contracts.standing_execution import StandingExecutionIntent
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.session.agent_control import AgentControl
from services.session.store import JournalError


def contract():
    try:
        return importlib.import_module("packages.contracts.standing_export")
    except ModuleNotFoundError as error:
        if error.name != "packages.contracts.standing_export":
            raise
        pytest.fail("Frozen standing export contract capability is absent", pytrace=False)


def export_method(owner):
    method = getattr(owner, "export_admission", None)
    assert callable(method), "Frozen native standing export admission capability is absent"
    return method


@dataclass
class Accepted:
    prepared: object
    owner: object
    proof: StandingInitialProof

    @property
    def native(self):
        return self.prepared.native


def accept(value):
    owner = value.task_owner()
    proof = assert_created(value, owner.admit(value.request, capability=SECRET))
    return Accepted(value, owner, proof)


@pytest.fixture
def accepted(prepared):
    return accept(prepared)


def accept_variant(native, *, scope="local_only", local=True, provider="synthetic-no-inference"):
    if native.record.receipt.request.authorization.content_scope != scope:
        native.wall.value = native.wall.value.replace(hour=11, minute=59, second=55)
        native.save(content_scope=scope)
        native.wall.value = native.wall.value.replace(hour=12, minute=0, second=10)
    raw = native.request().model_dump(mode="json")
    raw["task"]["model_pin"].update(local=local, provider=provider)
    return accept(prepare(native, request=StandingExecutionIntent.model_validate(raw)))


def old_export(proof, **changes):
    task = proof.claim.request.task
    value = {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "objective_revision": 1,
        **task.task_session.model_dump(mode="json"),
        "source_event_ids": [],
        "source_revisions": {},
        "context_event_ids": [],
        "content_scope": task.content_scope,
        "provider": {
            "schema_version": "1.0",
            "provider": task.model_pin.provider,
            "model": task.model_pin.model,
            "local": task.model_pin.local,
            "streaming": False,
        },
        "request_sha256": "d" * 64,
        "purpose": "complete_count",
        "request_nonce": "synthetic-export-nonce",
        **changes,
    }
    return AgentExportRequest.model_validate(value)


def request(value, *, initial=None, **changes):
    initial = value.proof if initial is None else initial
    return contract().StandingExportRequest(
        schema_version="1.0", initial=initial, export=old_export(initial, **changes)
    )


def safe(error):
    assert MARKER not in str(error) and SECRET.hex() not in str(error)
    assert repr(SECRET) not in str(error) and len(str(error)) <= 256


def read_only(value, original):
    native = value.native
    connection = native.store.db
    before = database_state(connection)
    total = connection.total_changes
    calls = native.wall.calls, native.mono.calls
    native.wall.forbidden = native.mono.forbidden = True
    result = export_method(value.owner)(original)
    assert type(result) is contract().StandingExportAdmission
    checked = result.validate_request(original)
    assert checked == result
    assert checked.admission.validate_request(original.export) == checked.admission
    assert database_state(connection) == before and connection.total_changes == total
    assert (native.wall.calls, native.mono.calls) == calls
    assert not connection.in_transaction
    return result


def refused(value, error, original, **options):
    connection = value.native.store.db
    before = database_state(connection)
    total = connection.total_changes
    calls = value.native.wall.calls, value.native.mono.calls
    value.native.wall.forbidden = value.native.mono.forbidden = True
    with pytest.raises(error) as caught:
        export_method(value.owner)(original, **options)
    safe(caught.value)
    assert database_state(connection) == before and connection.total_changes == total
    assert (value.native.wall.calls, value.native.mono.calls) == calls
    assert not connection.in_transaction


def test_existing_fixture_has_actual_accepted_link_and_no_export_capability_use(accepted):
    native = accepted.native
    assert accepted.owner.lookup(accepted.prepared.request) == accepted.proof
    assert accepted.proof.journal_entry() == native.store.entry(accepted.proof.event_id)
    assert (
        native.store.db.execute("SELECT count(*) FROM standing_task_admissions").fetchone()[0] == 1
    )
    assert old_export(accepted.proof).task_id == accepted.prepared.request.task.task_id
    assert SECRET.hex() not in accepted.proof.canonical_bytes().decode()


@pytest.mark.parametrize(
    "scope,provider",
    (("public_synthetic", "meta"), ("private_cloud_authorized", "synthetic-cloud")),
)
def test_existing_variant_fixture_control_has_genuine_captured_cloud_pin(
    build_native, scope, provider
):
    value = accept_variant(build_native(), scope=scope, local=False, provider=provider)
    assert value.owner.lookup(value.prepared.request) == value.proof
    assert value.proof.claim.request.task.model_pin.local is False
    assert value.proof.claim.request.task.model_pin.provider == provider
    assert value.proof.claim.request.task.content_scope == scope
    assert old_export(value.proof).provider.local is False


@pytest.mark.parametrize("purpose", ("complete_count", "complete_generation"))
def test_exact_metadata_export_uses_actual_global_watermark_without_dml_or_clocks(
    accepted, purpose
):
    native = accepted.native
    other = native.store.create_session(SessionRequest(channel="converse"))
    event = native.store.append(
        AppendRequest(
            session_id=other.session_id,
            thread_id=other.thread_id,
            epoch=other.epoch,
            kind="conversation.user",
            payload={"text": "Public synthetic watermark.", "final": True},
            idempotency_key="watermark",
        ),
        "owner",
    )
    original = request(accepted, purpose=purpose)
    result = read_only(accepted, original)
    assert result.admission.journal_sequence == event.sequence > accepted.proof.sequence
    assert result.admission.effective_scope == "local_only"
    assert result.request_digest == contract().standing_export_digest(original)


@pytest.mark.parametrize(
    "captured,proposed,local,provider,effective",
    (
        ("local_only", "public_synthetic", True, "synthetic-no-inference", "local_only"),
        (
            "private_cloud_authorized",
            "public_synthetic",
            True,
            "synthetic-no-inference",
            "private_cloud_authorized",
        ),
        ("public_synthetic", "local_only", True, "synthetic-no-inference", "local_only"),
        ("public_synthetic", "public_synthetic", False, "synthetic-cloud", "public_synthetic"),
        (
            "private_cloud_authorized",
            "public_synthetic",
            False,
            "synthetic-cloud",
            "private_cloud_authorized",
        ),
        ("local_only", "public_synthetic", False, "synthetic-cloud", None),
        ("private_cloud_authorized", "private_cloud_authorized", False, "meta", None),
        ("public_synthetic", "public_synthetic", False, "meta", "public_synthetic"),
    ),
)
def test_native_captured_floor_and_actual_captured_provider_limit_disclosure(
    build_native, captured, proposed, local, provider, effective
):
    value = accept_variant(build_native(), scope=captured, local=local, provider=provider)
    original = request(value, content_scope=proposed)
    if effective is None:
        refused(value, StandingConflict, original)
    else:
        result = read_only(value, original)
        assert result.admission.effective_scope == effective
        assert result.admission.provider.local is local


def test_native_export_does_not_call_the_bypass_blocked_ordinary_route(accepted, monkeypatch):
    original = request(accepted)

    def ordinary_forbidden(_):
        raise AssertionError("Standing export called ordinary export fallback")

    monkeypatch.setattr(accepted.native.store, "agent_export_admission", ordinary_forbidden)
    assert read_only(accepted, original).admission.effective_scope == "local_only"


@pytest.mark.parametrize("field", ("event_id", "created_at"))
def test_full_initial_metadata_must_equal_native_proof_not_only_its_claim(accepted, field):
    # A valid pure packet derived from a real accepted claim remains an
    # untrusted proposal. It is never an alternative native admission ACK.
    raw = accepted.proof.model_dump(mode="json")
    changed_time = datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00")) + timedelta(
        microseconds=1
    )
    raw[field] = (
        "counterfeit-event"
        if field == "event_id"
        else changed_time.isoformat().replace("+00:00", "Z")
    )
    raw["entry_digest"] = standing_entry_digest(
        accepted.proof.claim, raw["event_id"], raw["sequence"], raw["created_at"]
    )
    changed = StandingInitialProof.model_validate(raw)
    assert changed.claim == accepted.proof.claim and changed != accepted.proof
    refused(accepted, StandingConflict, request(accepted, initial=changed))


def test_a_genuine_foreign_accepted_proof_is_not_authority_for_an_empty_owner(build_native):
    actual = accept(prepare(build_native()))
    empty = prepare(build_native())
    owner = empty.task_owner()
    target = Accepted(empty, owner, actual.proof)
    refused(target, StandingConflict, request(actual))


@pytest.mark.parametrize("change", ("disable", "stop", "controller"))
def test_accepted_task_export_survives_current_fences_without_reauthorizing(accepted, change):
    native = accepted.native
    original = request(accepted)
    if change == "disable":
        native.save(enabled=False)
    elif change == "stop":
        native.dispatch.stop(native.dispatch.stop_context())
        native.control.change(mode_request(native.control, "off", "export-off"))
    else:
        peer = AgentControl(native.store)
        peer.register()
        assert peer.instance != accepted.prepared.request.expected_instance
    result = read_only(accepted, original)
    assert result.admission.task_id == accepted.prepared.request.task.task_id


@pytest.mark.parametrize("change", ("reset", "thread", "channel"))
def test_current_actual_session_is_still_required_for_new_export(accepted, change):
    native = accepted.native
    original = request(accepted)
    if change == "reset":
        native.store.reset(native.session.session_id)
    else:
        raw = native.store.session(native.session.session_id).model_dump(mode="json")
        raw["thread_id" if change == "thread" else "channel"] = (
            uid(999) if change == "thread" else "converse"
        )
        with native.store.db:
            native.store.db.execute(
                "UPDATE sessions SET value=? WHERE session_id=?",
                (json.dumps(raw), native.session.session_id),
            )
    assert accepted.owner.lookup(accepted.prepared.request) == accepted.proof
    refused(accepted, StandingConflict, original)


@pytest.mark.parametrize("corruption", ("request", "value", "missing_entry"))
def test_native_export_requires_actual_uncorrupted_accepted_entry_and_link(accepted, corruption):
    original = request(accepted)
    connection = accepted.native.store.db
    with connection:
        if corruption == "missing_entry":
            connection.execute("DELETE FROM entries WHERE event_id=?", (accepted.proof.event_id,))
        else:
            row = connection.execute(
                f"SELECT {corruption} FROM entries WHERE event_id=?", (accepted.proof.event_id,)
            ).fetchone()
            raw = json.loads(row[0])
            raw["payload"]["objective"] = "Tampered native objective"
            connection.execute(
                f"UPDATE entries SET {corruption}=? WHERE event_id=?",
                (json.dumps(raw), accepted.proof.event_id),
            )
    refused(accepted, StandingUnavailable, original)


def test_native_full_graph_check_refuses_copied_nested_old_export_extra(accepted):
    original = request(accepted)
    forged = original.model_copy(
        update={
            "export": original.export.model_copy(
                update={
                    "provider": original.export.provider.model_copy(update={"unexpected": MARKER})
                }
            )
        }
    )
    refused(accepted, StandingUnavailable, forged)


def test_closed_task_capability_cannot_export_even_an_exact_readable_proof(accepted):
    original = request(accepted)
    accepted.owner.close()
    refused(accepted, StandingUnavailable, original)


@pytest.mark.parametrize(
    "projection", ("sql", "request", "value", "task_refs", "proof_only", "oversized_proof")
)
def test_ordinary_export_cannot_bypass_native_standing_identity_projections(accepted, projection):
    native = accepted.native
    ordinary = old_export(accepted.proof, content_scope="public_synthetic")
    connection = native.store.db
    with connection:
        if projection in {"proof_only", "oversized_proof"}:
            connection.execute("DELETE FROM entries WHERE event_id=?", (accepted.proof.event_id,))
            if projection == "oversized_proof":
                raw = connection.execute("SELECT proof FROM standing_task_admissions").fetchone()[0]
                connection.execute(
                    "UPDATE standing_task_admissions SET proof=?",
                    (raw + " " * (163841 - len(raw.encode())),),
                )
        else:
            # Removing links deliberately isolates each actual journal
            # projection. No such corrupted row is used as a positive ACK.
            connection.execute("DROP TABLE standing_task_admissions")
            if projection != "sql":
                row = connection.execute(
                    "SELECT request,value FROM entries WHERE event_id=?", (accepted.proof.event_id,)
                ).fetchone()
                proposed, value = json.loads(row[0]), json.loads(row[1])
                proposed["kind"] = (
                    "agent.standing-command" if projection == "request" else "agent.command"
                )
                value["kind"] = (
                    "agent.standing-command" if projection == "value" else "agent.command"
                )
                if projection == "task_refs":
                    proposed["kind"] = value["kind"] = "agent.standing-command"
                    proposed["payload"]["task_id"] = value["payload"]["task_id"] = "other-task"
                connection.execute(
                    "UPDATE entries SET kind='agent.command',writer='agent',request=?,value=? WHERE event_id=?",
                    (json.dumps(proposed), json.dumps(value), accepted.proof.event_id),
                )
    before = database_state(connection)
    with pytest.raises(JournalError) as caught:
        native.store.agent_export_admission(ordinary)
    safe(caught.value)
    assert database_state(connection) == before and not connection.in_transaction


def test_unrelated_actual_ordinary_task_still_exports_beside_accepted_standing(accepted):
    native = accepted.native
    session = native.store.create_session(SessionRequest(channel="agent"))
    task_id = uid(888)
    receipt = native.store.admit_submit(ordinary_proposal(session, task_id=task_id), "agent")
    assert receipt.entry.payload["task_id"] == task_id
    original = old_export(
        accepted.proof,
        task_id=task_id,
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
    )
    before = database_state(native.store.db)
    result = native.store.agent_export_admission(original)
    assert type(result) is AgentExportAdmission and result.validate_request(original) == result
    assert database_state(native.store.db) == before


class ReadTransactionBoundary:
    """Real SELECT/COMMIT faults; never replace a query result or fabricate ACK."""

    def __init__(self, connection, *, phase=None, rollback_failure=False, pulse=None):
        self.connection = connection
        self.phase, self.rollback_failure, self.pulse = phase, rollback_failure, pulse
        self.fired = self.committed = self.rollback_fired = self.closed = self.pulsed = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        cursor = self.connection.execute(statement, parameters)
        text = " ".join(statement.upper().split())
        if (
            self.pulse is not None
            and not self.pulsed
            and "FROM ENTRIES" in text
            and ("MAX(" in text or "ORDER BY SEQUENCE DESC" in text)
        ):
            assert self.connection.in_transaction
            self.pulsed = True
            self.pulse()
        return cursor

    def commit(self):
        assert self.connection.in_transaction
        if self.phase is not None:
            self.fired = True
            if self.phase == "after":
                self.connection.commit()
                self.committed = True
            raise RuntimeError(MARKER)
        return self.connection.commit()

    def rollback(self):
        self.rollback_fired = True
        if self.rollback_failure:
            raise RuntimeError(MARKER)
        return self.connection.rollback()

    def close(self):
        self.closed = True
        self.connection.close()


@pytest.mark.parametrize("phase", ("before", "after"))
def test_read_transaction_commit_failure_never_returns_export_success(accepted, phase):
    original = request(accepted)
    native = accepted.native
    connection = native.store.db
    before, total = database_state(connection), connection.total_changes
    proxy = ReadTransactionBoundary(connection, phase=phase)
    native.store.db = proxy
    try:
        with pytest.raises(StandingUnavailable) as caught:
            export_method(accepted.owner)(original)
        safe(caught.value)
        assert proxy.fired and proxy.rollback_fired
        assert proxy.committed is (phase == "after")
        assert database_state(connection) == before and connection.total_changes == total
        assert not connection.in_transaction
    finally:
        native.store.db = connection


def test_failed_real_read_rollback_poison_preserves_all_accepted_disk_history(accepted):
    original = request(accepted)
    native = accepted.native
    connection = native.store.db
    before = database_state(connection)
    proxy = ReadTransactionBoundary(connection, phase="before", rollback_failure=True)
    native.store.db = proxy
    try:
        with pytest.raises(StandingUnavailable):
            export_method(accepted.owner)(original)
        assert proxy.fired and proxy.rollback_fired and proxy.closed
        assert accepted.owner._failed and native.store._agent_submit_unavailable
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with pytest.raises(StandingUnavailable):
            export_method(accepted.owner)(original)
    finally:
        native.store.db = connection
    with sqlite3.connect(native.store.path) as disk:
        assert database_state(disk) == before


@pytest.mark.parametrize("signal", ("cancel", "deadline", "session"))
def test_final_guard_and_session_check_follow_actual_watermark_sql(accepted, signal):
    original = request(accepted)
    native = accepted.native
    connection = native.store.db
    before = database_state(connection)
    cancelled = threading.Event()
    deadline = time.monotonic() + 2

    def pulse():
        if signal == "cancel":
            cancelled.set()
        elif signal == "deadline":
            threading.Event().wait(max(0, deadline - time.monotonic()))
            assert time.monotonic() >= deadline
        else:
            raw = native.store.session(native.session.session_id).model_dump(mode="json")
            raw["epoch"] += 1
            connection.execute(
                "UPDATE sessions SET value=? WHERE session_id=?",
                (json.dumps(raw), native.session.session_id),
            )

    proxy = ReadTransactionBoundary(connection, pulse=pulse)
    native.store.db = proxy
    try:
        with pytest.raises(
            (StandingConflict, StandingUnavailable) if signal == "session" else StandingUnavailable
        ):
            export_method(accepted.owner)(original, deadline=deadline, cancelled=cancelled.is_set)
        assert proxy.pulsed and proxy.rollback_fired
        assert database_state(connection) == before and not connection.in_transaction
    finally:
        native.store.db = connection


@pytest.mark.parametrize("option", ("expired", "cancelled", "bool_deadline"))
def test_native_export_entry_guard_has_no_disclosure_or_durable_side_effect(accepted, option):
    original = request(accepted)
    options = (
        {"deadline": time.monotonic() - 1}
        if option == "expired"
        else {"deadline": True}
        if option == "bool_deadline"
        else {"cancelled": lambda: True}
    )
    refused(accepted, StandingUnavailable, original, **options)


def test_actual_watermark_above_safe_native_integer_is_not_coerced_or_defaulted(accepted):
    native = accepted.native
    session = native.store.create_session(SessionRequest(channel="converse"))
    with native.store.db:
        native.store.db.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name='entries'", (MAX_AGENT_CONTROL_INTEGER,)
        )
    event = native.store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={"text": "Synthetic actual sequence bound.", "final": True},
            idempotency_key="beyond-safe-watermark",
        ),
        "owner",
    )
    assert event.sequence == MAX_AGENT_CONTROL_INTEGER + 1
    refused(accepted, StandingUnavailable, request(accepted))
