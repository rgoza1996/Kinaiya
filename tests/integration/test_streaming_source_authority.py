"""Streaming source authority (public issue #51, Roadmap AT11) — behavioral red tests.

Source authority is separate from tool grants: an authenticated
``conversation.window`` with ``capture_ended=False`` is uncommitted even when
``user_speaking=False``. A decisions-routed task carrying that source must use
the existing digest/revision-bound owner action approval for mutating actions,
including under ``allow_write`` and broad YOLO grants. Planning/read-only
operations remain allowed. ``capture_ended=True`` with
``user_speaking=False`` is an authenticated complete-capture signal. Direct
owner tasks without speech source references keep normal grant behavior.

These tests use only the real Store/LocalJournal, AgentRuntime, ToolPolicy,
actual temporary file tools, and explicit ScriptedProvider fixtures. They
reproduce the baseline bug (partial window -> write executes with zero
approvals) and must FAIL on the base implementation for the gating cases.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.policy import ApprovalError, ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    SessionRequest,
    SpeechWindow,
    SteerRequest,
    TaskRequest,
    TaskState,
)
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime
from services.session.store import LocalJournal, Store

TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}

ORIGINAL = "ORIGINAL-note-content"
OVERWRITE = "OVERWRITE-from-partial-window"


def _write_action(path="note.txt", content=OVERWRITE):
    return json.dumps({"tool": "write_file", "arguments": {"path": path, "content": content}})


def _read_action(path="note.txt"):
    return json.dumps({"tool": "read_file", "arguments": {"path": path}})


def _finish_action(summary="Verified public synthetic fixture"):
    return json.dumps(
        {"tool": "finish", "arguments": {"summary": summary, "objective_revision": 1}}
    )


def _make_env(tmp_path, responses, *, allow_write=False, yolo=False):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    policy = ToolPolicy(workspace, allow_write=allow_write, yolo=yolo)
    provider = ScriptedProvider(list(responses))
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, policy)
    return store, session, workspace, runtime


def _publish_window(
    store,
    session,
    text,
    *,
    utterance="u-note",
    window_index=0,
    revision=1,
    speaking=False,
    ended=False,
    key="at11-w",
):
    window = SpeechWindow(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        utterance_id=utterance,
        window_index=window_index,
        revision=revision,
        text=text,
        start_ms=0,
        end_ms=1000 + window_index * 500,
        user_speaking=speaking,
        capture_ended=ended,
        content_scope="public_synthetic",
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.window",
            payload=window.model_dump(mode="json"),
            idempotency_key=key,
        ),
        "owner",
    )


def _submit_decisions(runtime, session, text, source_event_id, *, key, revision=1):
    return runtime.submit(
        TaskRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective=text,
            idempotency_key=key,
            source_event_ids=[source_event_id],
            source_revision=revision,
        ),
        actor="decisions",
    )


async def _wait_for(runtime, task_id, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    current = await runtime.status(task_id)
    while not predicate(current):
        if current.state in TERMINAL:
            return current
        if time.monotonic() >= deadline:
            return current
        await asyncio.sleep(0.02)
        current = await runtime.status(task_id)
    return current


async def _wait_state(runtime, task_id, state, timeout=2.0):
    return await _wait_for(runtime, task_id, lambda t: t.state == state, timeout)


async def _wait_terminal(runtime, task_id, timeout=2.0):
    return await _wait_for(runtime, task_id, lambda t: t.state in TERMINAL, timeout)


def _approval_entry(store, task_id):
    entries = store.read(kinds=["approval.requested"])
    for entry in entries:
        if task_id in (entry.task_refs or []):
            return entry
    return None


async def test_partial_window_write_requires_owner_approval_allow_write(tmp_path):
    """Baseline repro: partial window + allow_write must not write without approval."""
    store, session, workspace, runtime = _make_env(
        tmp_path, [_write_action(), _finish_action()], allow_write=True
    )
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "overwrite note.txt with contender content"
        source = _publish_window(
            store, session, text, speaking=False, ended=False, key="at11-w-allow"
        )
        task = await _submit_decisions(
            runtime, session, text, source.event_id, key="at11-allow-submit"
        )
        waiting = await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        assert waiting.state == TaskState.AWAITING_APPROVAL, (
            f"partial window executed without approval, state={waiting.state.value}"
        )
        assert note.read_text() == ORIGINAL
        entry = _approval_entry(store, task.task_id)
        assert entry is not None, "expected a durable approval.requested entry"
        payload = entry.payload
        assert payload["approval_id"]
        assert payload["action_digest"]
        assert payload["reason"] == "uncommitted_speech"
        resolved = await runtime.resolve_approval(
            payload["approval_id"],
            ApprovalResolution(
                approved=True,
                action_digest=payload["action_digest"],
                objective_revision=payload["objective_revision"],
            ),
            actor="owner",
        )
        assert resolved["approved"] is True
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.SUCCEEDED
        assert note.read_text() == OVERWRITE
    finally:
        await runtime.close()
        store.close()


async def test_partial_window_write_requires_owner_approval_yolo(tmp_path):
    """YOLO broad grants must not bypass approval for uncommitted speech."""
    store, session, workspace, runtime = _make_env(
        tmp_path, [_write_action(), _finish_action()], yolo=True
    )
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "overwrite note.txt under yolo grant"
        source = _publish_window(
            store, session, text, speaking=False, ended=False, key="at11-w-yolo"
        )
        task = await _submit_decisions(
            runtime, session, text, source.event_id, key="at11-yolo-submit"
        )
        waiting = await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        assert waiting.state == TaskState.AWAITING_APPROVAL, (
            f"yolo grant bypassed approval, state={waiting.state.value}"
        )
        assert note.read_text() == ORIGINAL
        entry = _approval_entry(store, task.task_id)
        assert entry is not None
        assert entry.payload["reason"] == "uncommitted_speech"
    finally:
        await runtime.close()
        store.close()


async def test_speaking_window_still_uncommitted(tmp_path):
    """Actively-speaking partial window is uncommitted even under allow_write."""
    store, session, workspace, runtime = _make_env(tmp_path, [_write_action()], allow_write=True)
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "overwrite note.txt while user still speaking"
        source = _publish_window(
            store,
            session,
            text,
            speaking=True,
            ended=False,
            key="at11-w-speaking",
        )
        task = await _submit_decisions(
            runtime, session, text, source.event_id, key="at11-speaking-submit"
        )
        waiting = await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        assert waiting.state == TaskState.AWAITING_APPROVAL, (
            f"speaking window executed without approval, state={waiting.state.value}"
        )
        assert note.read_text() == ORIGINAL
        assert _approval_entry(store, task.task_id) is not None
    finally:
        await runtime.close()
        store.close()


async def test_uncommitted_write_denied_blocks_effect(tmp_path):
    """Denied approval must leave the file unchanged and fail the task."""
    store, session, workspace, runtime = _make_env(tmp_path, [_write_action()], allow_write=True)
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "overwrite note.txt then face denial"
        source = _publish_window(
            store, session, text, speaking=False, ended=False, key="at11-w-deny"
        )
        task = await _submit_decisions(
            runtime, session, text, source.event_id, key="at11-deny-submit"
        )
        waiting = await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        assert waiting.state == TaskState.AWAITING_APPROVAL
        assert note.read_text() == ORIGINAL
        entry = _approval_entry(store, task.task_id)
        assert entry is not None
        resolved = await runtime.resolve_approval(
            entry.payload["approval_id"],
            ApprovalResolution(
                approved=False,
                action_digest=entry.payload["action_digest"],
                objective_revision=entry.payload["objective_revision"],
            ),
            actor="owner",
        )
        assert resolved["approved"] is False
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.FAILED
        assert note.read_text() == ORIGINAL
    finally:
        await runtime.close()
        store.close()


async def test_owner_steer_supersedes_pending_speech_approval(tmp_path):
    """Steering while awaiting approval supersedes the stale narrow approval."""
    store, session, workspace, runtime = _make_env(
        tmp_path, [_write_action(), _finish_action()], allow_write=True
    )
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "overwrite note.txt with first hypothesis"
        source = _publish_window(
            store, session, text, speaking=False, ended=False, key="at11-w-correct"
        )
        task = await _submit_decisions(
            runtime, session, text, source.event_id, key="at11-correct-submit"
        )
        waiting = await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        assert waiting.state == TaskState.AWAITING_APPROVAL
        entry = _approval_entry(store, task.task_id)
        assert entry is not None
        stale_id = entry.payload["approval_id"]
        stale_digest = entry.payload["action_digest"]
        await runtime.steer(
            task.task_id,
            SteerRequest(
                instruction="corrected objective: do not overwrite",
                expected_objective_revision=waiting.objective_revision,
                idempotency_key="at11-correct-steer",
            ),
            actor="owner",
        )
        applied = await _wait_for(runtime, task.task_id, lambda t: t.objective_revision == 2)
        assert applied.objective_revision == 2
        assert note.read_text() == ORIGINAL
        current_pending = runtime.approval_for_task(task.task_id)
        assert current_pending is None or current_pending != stale_id
        with pytest.raises(ApprovalError, match="unknown or superseded"):
            await runtime.resolve_approval(
                stale_id,
                ApprovalResolution(
                    approved=True,
                    action_digest=stale_digest,
                    objective_revision=waiting.objective_revision,
                ),
                actor="owner",
            )
        assert note.read_text() == ORIGINAL
    finally:
        await runtime.close()
        store.close()


async def test_readonly_planning_allowed_without_approval(tmp_path):
    """Read-only planning on an uncommitted window needs no action approval."""
    store, session, workspace, runtime = _make_env(
        tmp_path, [_read_action(), _finish_action()], allow_write=True
    )
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "summarize note.txt without changing it"
        source = _publish_window(
            store, session, text, speaking=False, ended=False, key="at11-w-read"
        )
        task = await _submit_decisions(
            runtime, session, text, source.event_id, key="at11-read-submit"
        )
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.SUCCEEDED
        assert note.read_text() == ORIGINAL
        assert _approval_entry(store, task.task_id) is None
    finally:
        await runtime.close()
        store.close()


async def test_completed_capture_keeps_grant_behavior(tmp_path):
    """Ended capture with silence is committed: allow_write applies directly."""
    store, session, workspace, runtime = _make_env(
        tmp_path, [_write_action(), _finish_action()], allow_write=True
    )
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "overwrite note.txt after complete capture"
        source = _publish_window(
            store,
            session,
            text,
            speaking=False,
            ended=True,
            key="at11-w-complete",
        )
        task = await _submit_decisions(
            runtime, session, text, source.event_id, key="at11-complete-submit"
        )
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.SUCCEEDED
        assert note.read_text() == OVERWRITE
        assert _approval_entry(store, task.task_id) is None
    finally:
        await runtime.close()
        store.close()


async def test_direct_owner_task_keeps_grant_behavior(tmp_path):
    """Direct owner tasks without speech sources keep normal grant behavior."""
    store, session, workspace, runtime = _make_env(
        tmp_path, [_write_action(), _finish_action()], allow_write=True
    )
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="owner-directed write without speech source",
                idempotency_key="at11-owner-submit",
            ),
            actor="owner",
        )
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.SUCCEEDED
        assert note.read_text() == OVERWRITE
        assert _approval_entry(store, task.task_id) is None
    finally:
        await runtime.close()
        store.close()
