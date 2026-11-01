"""Native accepted-task and captured-route bindings for auxiliary trace writes."""

from packages.contracts.agent_submit import MAX_AGENT_SUBMIT_RESPONSE_BYTES
from packages.contracts.auxiliary_attempts import AttemptModelPin, AuxiliaryAttempt
from packages.contracts.models import JournalEntry


def validate_attempt(store, request):
    """Called under the journal owner lock before append DML/idempotent return."""
    attempt = AuxiliaryAttempt.model_validate(request.payload)
    if (
        request.task_refs != [attempt.task_id]
        or request.causes
        or not request.idempotency_key.startswith(f"agent:{attempt.task_id}:agent.model_attempt:")
    ):
        raise ValueError("Auxiliary attempt does not bind its native task")
    if not store._agent_submit_ready or store._agent_submit_unavailable:
        raise ValueError("Native accepted task authority is unavailable")
    row = store.db.execute(
        "SELECT sequence,event_id,session_id,thread_id,epoch,kind,writer,idem,"
        "CASE WHEN length(CAST(request AS BLOB))<=? THEN request END AS request,"
        "CASE WHEN length(CAST(value AS BLOB))<=? THEN value END AS value "
        "FROM entries WHERE sequence=?",
        (MAX_AGENT_SUBMIT_RESPONSE_BYTES, MAX_AGENT_SUBMIT_RESPONSE_BYTES, attempt.submit_sequence),
    ).fetchone()
    if row is None:
        raise ValueError("Native accepted task receipt is unavailable")
    accepted = store._agent_submit_check_row(row)
    store._agent_submit_winner(
        accepted.event_id, accepted.session_id, accepted.payload["idempotency_key"]
    )
    if (accepted.session_id, accepted.thread_id, accepted.epoch, accepted.payload["task_id"]) != (
        request.session_id,
        request.thread_id,
        request.epoch,
        attempt.task_id,
    ) or accepted.payload["objective_revision"] > attempt.objective_revision:
        raise ValueError("Auxiliary attempt scope differs from native acceptance")
    initial = AttemptModelPin.model_validate(
        accepted.payload.get("auxiliary_models", {}).get(attempt.purpose)
    )
    if attempt.phase == "route_captured":
        route = attempt.route
        if (
            route.harness_version != accepted.payload["harness_version"]
            or route.candidates[0].model_pin != initial
        ):
            raise ValueError("Captured route differs from its accepted initial model pin")
        return attempt
    if attempt.route_event_id is None:
        return attempt
    row = store.db.execute(
        "SELECT writer, CASE WHEN length(CAST(value AS BLOB))<=65536 THEN value END AS value "
        "FROM entries WHERE event_id=?",
        (attempt.route_event_id,),
    ).fetchone()
    if row is None or row["writer"] != "agent" or type(row["value"]) is not str:
        raise ValueError("Native captured route receipt is unavailable")
    entry = JournalEntry.model_validate_json(row["value"])
    captured = AuxiliaryAttempt.model_validate(entry.payload)
    if (
        entry.origin != "agent-supervisor"
        or entry.kind != "agent.model_attempt"
        or captured.phase != "route_captured"
        or captured.route is None
        or (entry.session_id, entry.thread_id, entry.epoch, captured.task_id)
        != (request.session_id, request.thread_id, request.epoch, attempt.task_id)
        or entry.task_refs != [attempt.task_id]
        or entry.sequence <= accepted.sequence
        or captured.objective_revision > attempt.objective_revision
        or captured.submit_sequence != attempt.submit_sequence
        or captured.purpose != attempt.purpose
        or captured.route_sha256 != attempt.route_sha256
        or captured.route_revision != attempt.route_revision
        or captured.route.candidates[0].model_pin != initial
        or captured.route.harness_version != accepted.payload["harness_version"]
    ):
        raise ValueError("Auxiliary attempt does not bind its captured native route")
    if attempt.ordinal is not None and (
        attempt.ordinal > len(captured.route.candidates)
        or attempt.model_pin != captured.route.candidates[attempt.ordinal - 1].model_pin
    ):
        raise ValueError("Auxiliary attempt differs from its captured candidate")
    return attempt
