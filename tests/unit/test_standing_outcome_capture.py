"""Independent frozen producer-capture checks, not settlement authority.

Full public synthetic intent/initial vectors come from the pre-existing
standing execution/admission oracles. Expected outcome/reference bytes use the
stdlib literal projection below, never a candidate encoder or shortened body.
The new capture implementation was not consulted while authoring these tests.
"""

import asyncio
import copy
import hashlib
import importlib
import json
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import datetime

import pytest
from test_standing_admission_contract import proof as literal_initial
from test_standing_execution_contract import GOLDEN_HASHES, GOLDEN_INTENT

from packages.contracts.models import AppendRequest, JournalEntry
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_execution import StandingExecutionIntent
from services.agent import completion

MARKER = "PUBLIC-SYNTHETIC-INVALID-OUTCOME-DETAIL"
EVENT = "00000000-0000-0000-0000-000000000888"
OTHER = "00000000-0000-0000-0000-000000000999"
SAFE = 9_007_199_254_740_991
CREATED = "2026-11-01T08:30:02.001000Z"


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def capture_module():
    try:
        return importlib.import_module("services.agent.standing_outcome")
    except ModuleNotFoundError as exc:
        if exc.name != "services.agent.standing_outcome":
            raise
        pytest.fail("Standing outcome capture capability is absent", pytrace=False)


def reference_type():
    try:
        module = importlib.import_module("packages.contracts.standing_agent_settlement")
    except ModuleNotFoundError as exc:
        if exc.name != "packages.contracts.standing_agent_settlement":
            raise
        pytest.fail("Standing outcome reference capability is absent", pytrace=False)
    return module.StandingAgentOutcomeReference


def vector(state="succeeded", revision=1):
    original = StandingExecutionIntent.model_validate(copy.deepcopy(GOLDEN_INTENT))
    initial = StandingInitialProof.model_validate(literal_initial())
    pinned = copy.deepcopy(GOLDEN_INTENT["task"])
    session = pinned["task_session"]
    task = {
        "schema_version": "1.0",
        "task_id": pinned["task_id"],
        "session_id": session["session_id"],
        "thread_id": session["thread_id"],
        "epoch": session["epoch"],
        "objective": pinned["objective"],
        "content_scope": pinned["content_scope"],
        "objective_revision": revision,
        "state": state,
        "summary": "Synthetic café outcome 🧭." if state == "succeeded" else "",
        "evidence": [],
        "remaining_required_work": [],
        "source_event_ids": [],
        "idempotency_key": "standing:" + GOLDEN_HASHES["occurrence_id"],
        "harness_version": pinned["harness_version"],
        "model_pin": pinned["model_pin"],
        "auxiliary_models": {},
        "workspace_pin": pinned["workspace_pin"],
        "error": None
        if state == "succeeded"
        else "Synthetic failed work"
        if state == "failed"
        else "cancelled by owner",
    }
    payload = {
        **{
            name: copy.deepcopy(task[name])
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
        "task": task,
        "plugin_versions": {},
    }
    if state == "succeeded":
        payload["summary"] = task["summary"]
    elif state == "failed":
        payload["error"] = task["error"]
    request = AppendRequest(
        schema_version="1.0",
        session_id=session["session_id"],
        thread_id=session["thread_id"],
        epoch=session["epoch"],
        kind="agent.outcome",
        payload=payload,
        causes=[],
        task_refs=[pinned["task_id"]],
        idempotency_key=f"agent:{pinned['task_id']}:agent.outcome:2",
    )
    entry = native_entry(request)
    return original, initial, request, entry


def native_entry(request, **updates):
    raw = {
        **request.model_dump(mode="json"),
        "event_id": EVENT,
        "sequence": 18,
        "origin": "agent-supervisor",
        "created_at": CREATED,
    }
    return JournalEntry.model_validate(raw).model_copy(deep=True, update=updates)


def expected_reference(request, entry):
    return {
        "schema_version": "1.0",
        "task_id": request.task_refs[0],
        "event_id": entry.event_id,
        "sequence": entry.sequence,
        "objective_revision": request.payload["objective_revision"],
        "state": request.payload["state"],
        "request_sha256": hashlib.sha256(encoded(request.model_dump(mode="json"))).hexdigest(),
        "entry_sha256": hashlib.sha256(encoded(entry.model_dump(mode="json"))).hexdigest(),
    }


def refuses(*values):
    with pytest.raises(ValueError) as exc:
        capture_module().capture_outcome(*values)
    assert MARKER not in str(exc.value) and exc.value.__cause__ is None


def change_pair(values, path, value):
    """Mutate actual accepted-model storage, bypassing coercing constructors."""
    for root in values[2:]:
        target = root.__dict__
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = copy.deepcopy(value)


@pytest.mark.parametrize("state", ("succeeded", "failed", "cancelled"))
def test_complete_terminal_capture_matches_literal_full_bytes_and_reference(state):
    values = vector(state)
    module = capture_module()
    result = module.capture_outcome(*values)
    request, entry = values[2:]
    assert type(result) is module.RetainedOutcome and is_dataclass(result)
    assert type(result.request) is type(result.entry) is bytes
    assert result.request == encoded(request.model_dump(mode="json"))
    assert result.entry == encoded(entry.model_dump(mode="json"))
    assert result.reference.model_dump(mode="json") == expected_reference(request, entry)
    assert b"caf\xc3\xa9" in result.request and b"\\u00e9" not in result.request
    assert json.loads(result.entry)["payload"]["task"]["state"] == state
    assert (
        json.loads(result.request)["payload"]["task"]["model_pin"]
        == GOLDEN_INTENT["task"]["model_pin"]
    )


def test_retained_value_and_reference_are_frozen_and_detached_from_all_inputs():
    values = vector()
    result = capture_module().capture_outcome(*values)
    before = result.request, result.entry, result.reference.model_dump(mode="json")
    values[0].__dict__["task"].__dict__["objective"] = MARKER
    values[1].__dict__["event_id"] = OTHER
    values[2].payload["task"]["model_pin"]["model"] = MARKER
    values[3].payload["task"]["evidence"].append({"output": MARKER})
    assert (result.request, result.entry, result.reference.model_dump(mode="json")) == before
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        result.request = b"replacement"
    with pytest.raises((ValueError, AttributeError, TypeError)):
        result.reference.sequence = 19


def test_genuine_typed_native_defaults_need_not_be_explicitly_in_fields_set():
    original, initial, request, _ = vector()
    request = AppendRequest(**request.model_dump(exclude={"schema_version"}))
    entry = JournalEntry(**request.model_dump(), sequence=18, origin="agent-supervisor")
    assert "schema_version" not in request.model_fields_set
    assert {"event_id", "created_at"}.isdisjoint(entry.model_fields_set)
    result = capture_module().capture_outcome(original, initial, request, entry)
    assert result.request == encoded(request.model_dump(mode="json"))
    assert result.entry == encoded(entry.model_dump(mode="json"))
    assert result.reference.model_dump(mode="json") == expected_reference(request, entry)


@pytest.mark.parametrize("state", ("failed", "cancelled"))
def test_legitimate_changed_objective_revision_and_all_accepted_extras_are_retained(state):
    values = vector(state, revision=2)
    request = values[2]
    request.payload["objective"] = request.payload["task"]["objective"] = "A steered synthetic task"
    request.payload["plugin_versions"] = {"observed-package": "synthetic-version"}
    request.payload["native_extra"] = {
        "note": "Preserve café and e\u0301 literally.",
        "duration": 0.125,
        "large_ordinary_integer": SAFE + 1,
    }
    request.causes.append("synthetic-owner-control")
    values = (*values[:3], native_entry(request))
    result = capture_module().capture_outcome(*values)
    assert result.reference.objective_revision == 2 and result.reference.state == state
    assert result.reference.model_dump(mode="json") == expected_reference(*values[2:])
    assert json.loads(result.request) == request.model_dump(mode="json")
    assert json.loads(result.entry) == values[3].model_dump(mode="json")


@pytest.mark.parametrize("field", ("original", "initial", "request", "entry"))
def test_plain_dictionary_cannot_replace_typed_original_capture_input(field):
    values = list(vector())
    index = ("original", "initial", "request", "entry").index(field)
    values[index] = values[index].model_dump(mode="json")
    refuses(*values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "standing-supervisor"),
        ("origin", "Agent-Supervisor"),
        ("event_id", ""),
        ("event_id", "native\x00event"),
        ("event_id", "x" * 129),
        ("sequence", 17),
        ("sequence", True),
        ("sequence", SAFE + 1),
        ("created_at", CREATED),
        ("created_at", datetime(2026, 11, 1)),
    ],
)
def test_native_metadata_is_actual_bounded_and_after_its_initial(field, value):
    values = vector()
    values[3].__dict__[field] = value
    refuses(*values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "agent.command"),
        ("session_id", MARKER),
        ("thread_id", MARKER),
        ("epoch", True),
        ("epoch", 4),
        ("task_refs", []),
        ("task_refs", [OTHER]),
        ("task_refs", [GOLDEN_INTENT["task"]["task_id"], OTHER]),
    ],
)
def test_even_equal_envelopes_cannot_change_original_task_scope(field, value):
    values = vector()
    change_pair(values, (field,), value)
    refuses(*values)


@pytest.mark.parametrize(
    "path,value",
    [
        (("state",), "running"),
        (("objective_revision",), True),
        (("task_id",), OTHER),
        (("task", "state"), "failed"),
        (("task", "objective_revision"), 2),
        (("task", "task_id"), OTHER),
        (("task", "session_id"), MARKER),
        (("task", "thread_id"), MARKER),
        (("task", "epoch"), True),
        (("task", "idempotency_key"), "non-standing-key"),
        (("task", "harness_version"), "v-" + "b" * 24),
        (("task", "workspace_pin", "root_identity"), "b" * 64),
        (("task", "model_pin", "model"), MARKER),
        (("task", "model_pin", "local"), 1),
        (("task", "model_pin", "settings_revision"), True),
        (("task", "model_pin", "catalog_revision"), 12),
        (("task", "model_pin", "catalog_binding", "generation"), 4),
    ],
)
def test_full_task_payload_and_fixed_native_pins_cannot_be_reinterpreted(path, value):
    values = vector()
    change_pair(values, ("payload", *path), value)
    refuses(*values)


@pytest.mark.parametrize("which", ("request", "entry"))
def test_complete_append_envelope_mismatch_and_actual_extra_metadata_refuse(which):
    values = vector()
    index = 2 if which == "request" else 3
    values[index].__dict__["causes"] = [MARKER]
    refuses(*values)
    values = vector()
    values[index].__dict__[MARKER] = "extra actual field"
    refuses(*values)


def test_exact_initial_and_original_are_required_not_equal_looking_history():
    values = vector()
    values[1].__dict__["sequence"] = True
    refuses(*values)
    values = vector()
    values[0].__dict__["task"].__dict__["model_pin"].__dict__["local"] = 1
    refuses(*values)
    values = vector()
    values[1].__dict__["claim"].__dict__["request"].__dict__["executor_id"] = OTHER
    refuses(*values)
    values = list(vector())
    foreign = copy.deepcopy(GOLDEN_INTENT)
    foreign["executor_id"] = OTHER
    values[0] = StandingExecutionIntent.model_validate(foreign)
    refuses(*values)


@pytest.mark.parametrize("field", (MARKER, 1))
def test_actual_field_set_metadata_cannot_smuggle_ignored_names(field):
    values = vector()
    values[2].__pydantic_fields_set__.add(field)
    refuses(*values)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_nonfinite_extra_payload_data_cannot_enter_complete_canonical_hashes(value):
    values = vector()
    change_pair(values, ("payload", "extra_metric"), value)
    refuses(*values)


def test_native_append_boundary_accepts_full_65536_and_refuses_the_next_byte():
    values = vector("failed")
    request = values[2]
    request.payload["task"]["remaining_required_work"] = [""]
    padding = 65536 - len(request.model_dump_json().encode("utf-8"))
    assert padding > 0
    request.payload["task"]["remaining_required_work"][0] = "x" * padding
    assert len(request.model_dump_json().encode("utf-8")) == 65536
    values = (*values[:3], native_entry(request))
    result = capture_module().capture_outcome(*values)
    assert result.request == encoded(request.model_dump(mode="json"))
    assert len(values[3].model_dump_json().encode("utf-8")) <= 69632
    request.payload["task"]["remaining_required_work"][0] += "x"
    assert len(request.model_dump_json().encode("utf-8")) == 65537
    refuses(*values[:3], native_entry(request))


def test_oversized_native_entry_never_becomes_a_shortened_reference():
    values = vector()
    values[3].__dict__["event_id"] = MARKER * 3000
    assert len(values[3].model_dump_json().encode("utf-8")) > 69632
    refuses(*values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "1.1"),
        ("task_id", "not-a-uuid"),
        ("event_id", "bad\x00event"),
        ("sequence", True),
        ("objective_revision", True),
        ("state", "paused"),
        ("request_sha256", "A" * 64),
        ("entry_sha256", "0" * 63),
    ],
)
def test_reference_grammar_rejects_tampering_without_claiming_authority(field, value):
    values = vector()
    raw = expected_reference(*values[2:])
    raw[field] = value
    with pytest.raises(ValueError):
        reference_type().model_validate(raw)


def test_reference_requires_complete_closed_metadata_and_its_exact_raw_limit():
    raw = expected_reference(*vector()[2:])
    kind = reference_type()
    parsed = kind.model_validate(raw)
    assert parsed.model_dump(mode="json") == raw
    for field in raw:
        missing = copy.deepcopy(raw)
        missing.pop(field)
        with pytest.raises(ValueError):
            kind.model_validate(missing)
    with pytest.raises(ValueError):
        kind.model_validate({**raw, "extra": MARKER})
    compact = encoded(raw)
    exact = b" " * (2048 - len(compact)) + compact
    assert kind.from_json(exact).model_dump(mode="json") == raw
    with pytest.raises(ValueError):
        kind.from_json(b" " + exact)


class Journal:
    """Only completion-hook dispatch is simulated, not native authority/cleanup."""

    def __init__(self, *, ack=None, pages=(), append_error=None, read_error=None):
        self.ack, self.pages = ack, list(pages)
        self.append_error, self.read_error = append_error, read_error
        self.appends, self.reads = [], []

    async def append(self, request):
        self.appends.append(request)
        if self.append_error is not None:
            raise self.append_error
        return self.ack

    async def read(self, **query):
        self.reads.append(query)
        if self.read_error is not None:
            raise self.read_error
        return self.pages.pop(0) if self.pages else []


async def test_exact_ack_hook_receives_complete_outcome_once_without_readback():
    values = vector()
    journal, captured = Journal(ack=values[3]), []

    def confirmed(request, entry):
        captured.append(capture_module().capture_outcome(*values[:2], request, entry))

    assert (
        await completion.append_completion(journal, values[2], after=17, on_confirmed=confirmed)
        is True
    )
    assert len(captured) == len(journal.appends) == 1 and journal.reads == []
    assert captured[0].reference.model_dump(mode="json") == expected_reference(*values[2:])


@pytest.mark.parametrize("return_value", (False, True, 1, "not-a-capture"))
async def test_non_none_hook_result_is_unconfirmed_without_another_remote_call(return_value):
    values = vector()
    journal, calls = Journal(ack=values[3]), []

    def confirmed(*pair):
        calls.append(pair)
        return return_value

    assert (
        await completion.append_completion(journal, values[2], after=17, on_confirmed=confirmed)
        is False
    )
    assert len(calls) == len(journal.appends) == 1 and journal.reads == []


async def test_lost_ack_hook_uses_one_exact_full_native_readback_and_no_second_post():
    values = vector()
    other = native_entry(values[2], sequence=19, idempotency_key="other-completion")
    journal = Journal(append_error=RuntimeError(MARKER), pages=[[values[3], other]])
    captured = []
    assert (
        await completion.append_completion(
            journal,
            values[2],
            after=17,
            on_confirmed=lambda request, entry: captured.append(
                capture_module().capture_outcome(*values[:2], request, entry)
            ),
        )
        is True
    )
    assert len(journal.appends) == len(journal.reads) == len(captured) == 1
    assert captured[0].entry == encoded(values[3].model_dump(mode="json"))


@pytest.mark.parametrize("tail", ("dictionary", "decreasing", "bool-sequence"))
async def test_matching_readback_followed_by_malformed_tail_never_calls_hook(tail):
    values = vector()
    bad = {
        "dictionary": {"sequence": 19},
        "decreasing": native_entry(values[2], sequence=17),
        "bool-sequence": native_entry(values[2], sequence=True),
    }[tail]
    journal = Journal(append_error=RuntimeError(MARKER), pages=[[values[3], bad]])
    calls = []
    assert (
        await completion.append_completion(
            journal, values[2], after=17, on_confirmed=lambda *pair: calls.append(pair)
        )
        is False
    )
    assert calls == [] and len(journal.appends) == len(journal.reads) == 1


async def test_hook_ordinary_failure_is_unconfirmed_without_repost_or_readback():
    values = vector()
    journal, calls = Journal(ack=values[3]), []

    def broken(request, entry):
        calls.append((request, entry))
        raise ValueError(MARKER)

    assert (
        await completion.append_completion(journal, values[2], after=17, on_confirmed=broken)
        is False
    )
    assert len(calls) == len(journal.appends) == 1 and journal.reads == []


@pytest.mark.parametrize("phase", ("append", "read", "hook"))
async def test_cancellation_propagates_and_never_captures_an_unconfirmed_outcome(phase):
    values = vector()
    journal = Journal(
        ack=values[3],
        append_error=asyncio.CancelledError()
        if phase == "append"
        else RuntimeError()
        if phase == "read"
        else None,
        read_error=asyncio.CancelledError(),
    )
    calls = []

    def confirmed(*pair):
        calls.append(pair)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await completion.append_completion(journal, values[2], after=17, on_confirmed=confirmed)
    assert len(journal.appends) == 1 and len(journal.reads) == int(phase == "read")
    assert len(calls) == int(phase == "hook")


@pytest.mark.parametrize("read_error", (None, RuntimeError(MARKER)))
async def test_failed_or_absent_readback_never_calls_capture_hook(read_error):
    values = vector()
    journal = Journal(append_error=RuntimeError(MARKER), read_error=read_error)
    calls = []
    assert (
        await completion.append_completion(
            journal, values[2], after=17, on_confirmed=lambda *pair: calls.append(pair)
        )
        is False
    )
    assert calls == [] and len(journal.appends) == len(journal.reads) == 1


async def test_no_hook_preserves_existing_boolean_completion_behavior():
    values = vector()
    journal = Journal(ack=values[3])
    assert await completion.append_completion(journal, values[2], after=17) is True
    assert journal.appends == [values[2]] and journal.reads == []
