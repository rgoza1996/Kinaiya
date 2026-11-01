"""Final native validation consumes the original standing launch deadline.

Real Session/SQLite admission and the real post-ACK Session read precede the
fault. Only standing_runtime's local monotonic view advances; asyncio and the
global clock remain untouched. No receipt or native validation is fabricated.
"""

import importlib
import time
from types import SimpleNamespace

import pytest
from test_standing_agent_runtime import REFUSALS, TEXT, entries, finish, full_model_pin, submit
from test_standing_agent_runtime import factory as factory
from test_standing_agent_runtime import stack as stack

from packages.contracts.standing_admission import StandingTaskAdmission

ADMIT = "/v1/standing/tasks/admit"


def final_validation_fault(value, monkeypatch, *, boundary, advance):
    module = importlib.import_module("services.agent.standing_runtime")
    system_clock = time.monotonic
    observation = SimpleNamespace(
        armed=False, fired=False, validated=False, session=None, advance=advance, offset=0.0
    )

    def monotonic():
        return system_clock() + observation.offset

    # Replace the module reference, never attributes on the shared time module.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic))
    assert time.monotonic is system_clock and module.time is not time
    actual_session = value.journal.session

    async def arm_after_actual_post_ack_session(session_id):
        session = await actual_session(session_id)
        if value.transport.paths(ADMIT) and not observation.fired:
            assert len(entries(value)) == 1
            assert (session.session_id, session.thread_id, session.epoch) == (
                value.original.task.task_session.session_id,
                value.original.task.task_session.thread_id,
                value.original.task.task_session.epoch,
            )
            observation.session = session
            observation.armed = True
        return session

    monkeypatch.setattr(value.journal, "session", arm_after_actual_post_ack_session)

    def consume_deadline():
        observation.validated = True
        observation.fired = True
        observation.armed = False
        observation.offset += advance

    if boundary == "model":
        owner = value.runtime.model_settings
        actual_selection = owner.selection_info

        def selection_info(*args, **kwargs):
            selected = actual_selection(*args, **kwargs)
            if observation.armed:
                assert full_model_pin(selected[1]) == (
                    value.original.task.model_pin.model_dump(mode="json")
                )
                consume_deadline()
            return selected

        monkeypatch.setattr(owner, "selection_info", selection_info)
    else:
        assert boundary == "workspace"
        context = value.runtime.workspace_settings.active_context()
        context_type, actual_current = type(context), type(context).current

        def current(actual_context):
            valid = actual_current(actual_context)
            if actual_context is context and observation.armed:
                assert valid is True and context.pin == value.original.task.workspace_pin
                consume_deadline()
            return valid

        monkeypatch.setattr(context_type, "current", current)
    return observation


@pytest.mark.parametrize("boundary", ["model", "workspace"])
async def test_final_native_validation_after_cutoff_cannot_launch_accepted_task(
    stack, monkeypatch, boundary
):
    value = await stack()
    assert callable(getattr(value.runtime, "submit_standing", None)), "Standing worker absent"
    observation = final_validation_fault(value, monkeypatch, boundary=boundary, advance=26.0)
    result, refusal = None, None
    try:
        result = await submit(value)
    except REFUSALS as error:
        refusal = error

    assert observation.session is not None and observation.fired and observation.validated
    assert observation.offset == 26.0
    assert len(value.transport.paths(ADMIT)) == len(entries(value)) == 1
    proof = await value.journal.standing_task_lookup(value.original)
    assert proof.validate_request(value.original).claim == value.claim
    assert proof.journal_entry() == value.env.store.entry(proof.event_id)
    assert value.base.calls == [] and all(provider.calls == [] for provider in value.made)
    assert not any(record.worker is not None for record in value.runtime._tasks.values())
    assert not (value.workspace / "result.txt").exists()
    assert result is None and refusal is not None
    assert await value.journal.standing_unresolved() == value.claim
    assert value.runtime._submit_idem == {}


async def test_timely_same_final_native_validation_runs_real_captured_file_workflow(
    stack, monkeypatch
):
    value = await stack()
    assert callable(getattr(value.runtime, "submit_standing", None)), "Standing worker absent"
    observation = final_validation_fault(value, monkeypatch, boundary="model", advance=0.0)
    result = await submit(value)
    assert observation.session is not None and observation.fired and observation.validated
    assert observation.offset == 0.0
    assert type(result) is StandingTaskAdmission
    assert result.validate_request(value.original).disposition == "created"
    task = await finish(value)
    assert task.state.value == "succeeded" and (value.workspace / "result.txt").read_text() == TEXT
    assert len(value.made) == 1 and len(value.made[0].calls) == 3
    assert value.made[0].closed and value.record.worker.done()
    assert value.record.standing_initial == result.proof
    assert len(entries(value)) == len(value.transport.paths(ADMIT)) == 1
    assert await value.journal.standing_unresolved() == value.claim
