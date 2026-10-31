"""Lead integration checks: actual files, shell/MCP isolation and native approvals.

Inference and speech text are explicitly synthetic; no paid model is used.
The Store, supervisor, policy and (where applicable) stdio MCP process are real.
"""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient
from test_mcp_agent import _calls, _cfg, _sandbox_ok, _write_server
from test_streaming_source_authority import (
    ORIGINAL,
    OVERWRITE,
    _approval_entry,
    _finish_action,
    _make_env,
    _publish_window,
    _submit_decisions,
    _wait_state,
    _wait_terminal,
    _write_action,
)

from core.policy import ApprovalError, ToolPolicy
from packages.contracts.models import (
    ApprovalResolution,
    InterruptRequest,
    SessionRequest,
    TaskRequest,
    TaskState,
)
from packages.ecosystem.mcp_host import MCPHost
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.agent.runtime import StaleRevision, TaskTerminal
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


def _resolution(entry, *, approved=True):
    return ApprovalResolution(
        approved=approved,
        action_digest=entry.payload["action_digest"],
        objective_revision=entry.payload["objective_revision"],
    )


@pytest.mark.parametrize("grant", ["none", "write", "yolo"])
async def test_speech_approval_is_one_use_for_exact_action(tmp_path, grant):
    store, session, workspace, runtime = _make_env(
        tmp_path,
        [_write_action(), _write_action("second.txt"), _finish_action()],
        allow_write=grant == "write",
        yolo=grant == "yolo",
    )
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "public synthetic: write two files"
        source = _publish_window(store, session, text)
        task = await _submit_decisions(runtime, session, text, source.event_id, key="two")
        assert (
            await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        ).state == TaskState.AWAITING_APPROVAL
        first = _approval_entry(store, task.task_id)
        assert first is not None and note.read_text() == ORIGINAL
        await runtime.resolve_approval(first.payload["approval_id"], _resolution(first))
        for _ in range(100):
            requests = store.read(kinds=["approval.requested"])
            if len(requests) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(requests) == 2
        second = requests[1]
        assert note.read_text() == OVERWRITE
        assert not (workspace / "second.txt").exists()
        assert second.payload["approval_id"] != first.payload["approval_id"]
        assert second.payload["action_digest"] != first.payload["action_digest"]
        assert runtime.policy.peek_approval(first.payload["approval_id"]).used
        with pytest.raises(ApprovalError, match="unknown or superseded"):
            await runtime.resolve_approval(first.payload["approval_id"], _resolution(first))
        await runtime.resolve_approval(
            second.payload["approval_id"], _resolution(second, approved=False)
        )
        assert (await _wait_terminal(runtime, task.task_id)).state == TaskState.FAILED
        assert not (workspace / "second.txt").exists()
    finally:
        await runtime.close()
        store.close()


@pytest.mark.parametrize("invalidator", ["expired", "corrected", "epoch", "cancelled"])
async def test_invalidated_partial_approval_cannot_write(tmp_path, invalidator):
    store, session, workspace, runtime = _make_env(tmp_path, [_write_action()], yolo=True)
    try:
        note = workspace / "note.txt"
        note.write_text(ORIGINAL)
        text = "public synthetic: overwrite note.txt"
        source = _publish_window(store, session, text)
        task = await _submit_decisions(runtime, session, text, source.event_id, key="invalidate")
        assert (
            await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        ).state == TaskState.AWAITING_APPROVAL
        entry = _approval_entry(store, task.task_id)
        assert entry is not None
        if invalidator == "expired":
            runtime.policy.peek_approval(entry.payload["approval_id"]).expires_at = time.time() - 1
        elif invalidator == "corrected":
            # Real same-slot STT revision, without relying on a later Decisions call.
            _publish_window(
                store,
                session,
                "public synthetic: do not overwrite",
                revision=2,
                ended=True,
                key="correction",
            )
        elif invalidator == "epoch":
            store.reset(session.session_id)
        else:
            await runtime.interrupt(
                task.task_id,
                InterruptRequest(expected_objective_revision=1, idempotency_key="cancel"),
            )
            await _wait_terminal(runtime, task.task_id)
        assert (await runtime.pending_approvals()).approvals == []
        if invalidator == "cancelled":
            with pytest.raises(TaskTerminal):
                await runtime.resolve_approval(entry.payload["approval_id"], _resolution(entry))
        elif invalidator in {"corrected", "epoch"}:
            with pytest.raises(StaleRevision):
                await runtime.resolve_approval(entry.payload["approval_id"], _resolution(entry))
            assert not runtime.policy.peek_approval(entry.payload["approval_id"]).used
        else:
            await runtime.resolve_approval(entry.payload["approval_id"], _resolution(entry))
        final = (
            await runtime.status(task.task_id)
            if invalidator in {"corrected", "epoch"}
            else await _wait_terminal(runtime, task.task_id)
        )
        assert final.state != TaskState.SUCCEEDED
        assert note.read_text() == ORIGINAL
        assert not any(e.kind == "file" for e in final.evidence)
    finally:
        await runtime.close()
        store.close()


@pytest.mark.parametrize("grant", [False, True])
async def test_partial_speech_shell_requires_and_consumes_owner_approval(tmp_path, grant):
    store, session, workspace, runtime = _make_env(
        tmp_path,
        [
            json.dumps({"tool": "shell", "arguments": {"command": "printf APPROVED > shell.txt"}}),
            _finish_action(),
        ],
        yolo=grant,
    )
    try:
        if not _sandbox_ok(workspace):
            pytest.skip("bubblewrap isolation unavailable")
        text = "public synthetic: write shell.txt"
        source = _publish_window(store, session, text)
        task = await _submit_decisions(runtime, session, text, source.event_id, key="shell")
        assert (
            await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        ).state == TaskState.AWAITING_APPROVAL
        assert not (workspace / "shell.txt").exists()
        entry = _approval_entry(store, task.task_id)
        await runtime.resolve_approval(entry.payload["approval_id"], _resolution(entry))
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.SUCCEEDED, final.error
        assert (workspace / "shell.txt").read_text() == "APPROVED"
        assert runtime.policy.peek_approval(entry.payload["approval_id"]).used
        assert any(e.kind == "command" and e.exit_code == 0 for e in final.evidence)
    finally:
        await runtime.close()
        store.close()


async def test_partial_speech_mcp_yolo_still_requires_exact_owner_action(tmp_path):
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable")
    _write_server(tmp_path)
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    policy = ToolPolicy(tmp_path, yolo=True)
    host = MCPHost([_cfg(tmp_path)], policy)
    args = {
        "server_id": "synth",
        "tool": "write_text",
        "arguments": {"path": "mcp.txt", "content": "MCP-APPROVED"},
    }
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        ScriptedProvider(
            [
                json.dumps({"tool": "mcp_call", "arguments": args}),
                json.dumps({"tool": "read_file", "arguments": {"path": "mcp.txt"}}),
                _finish_action(),
            ]
        ),
        tmp_path,
        policy,
        mcp_host=host,
    )
    try:
        text = "public synthetic: write mcp.txt"
        source = _publish_window(store, session, text)
        task = await _submit_decisions(runtime, session, text, source.event_id, key="mcp")
        assert (
            await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        ).state == TaskState.AWAITING_APPROVAL
        assert _calls(tmp_path) == [] and not (tmp_path / "mcp.txt").exists()
        entry = _approval_entry(store, task.task_id)
        assert entry.payload["reason"] == "uncommitted_speech"
        await runtime.resolve_approval(entry.payload["approval_id"], _resolution(entry))
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.SUCCEEDED, final.error
        assert _calls(tmp_path) == [{"event": "call", "tool": "write_text"}]
        assert (tmp_path / "mcp.txt").read_text() == "MCP-APPROVED"
        assert runtime.policy.peek_approval(entry.payload["approval_id"]).used
        assert [e.kind for e in final.evidence] == ["external", "read"]
    finally:
        await runtime.close()
        store.close()


async def test_pending_approval_api_is_owner_only_paged_and_cross_session(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    sessions = [store.create_session(SessionRequest(channel=c)) for c in ("converse", "agent")]
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        ScriptedProvider([_write_action()] * 103),
        tmp_path,
        ToolPolicy(tmp_path),
    )
    auth = Auth(
        {"owner": "synthetic-owner-token-00000", "decisions": "synthetic-decisions-token-0"}
    )
    try:
        tasks = []
        for index in range(103):
            session = sessions[index % 2]
            tasks.append(
                await runtime.submit(
                    TaskRequest(
                        session_id=session.session_id,
                        thread_id=session.thread_id,
                        epoch=session.epoch,
                        objective="public synthetic pending write",
                        idempotency_key=f"page-{index}",
                    )
                )
            )
        await asyncio.gather(
            *[_wait_state(runtime, t.task_id, TaskState.AWAITING_APPROVAL) for t in tasks]
        )
        assert all(t.state == TaskState.AWAITING_APPROVAL for t in await runtime.status())
        async with AsyncClient(
            transport=ASGITransport(app=create_app(runtime, auth)), base_url="http://fixture"
        ) as http:
            assert (await http.get("/v1/approvals")).status_code == 401
            assert (
                await http.get(
                    "/v1/approvals", headers={"Authorization": "Bearer " + auth.tokens["decisions"]}
                )
            ).status_code == 403
            http.headers["Authorization"] = "Bearer " + auth.tokens["owner"]
            first = await http.get("/v1/approvals")
            assert first.status_code == 200
            data = first.json()
            assert data["schema_version"] == "1.0" and len(data["approvals"]) == 100
            assert {a["session_id"] for a in data["approvals"]} == {s.session_id for s in sessions}
            assert all(
                a["origin"] == "agent-supervisor" and a["reason"] == "tool_permission"
                for a in data["approvals"]
            )
            second = (await http.get("/v1/approvals", params={"after": data["next_after"]})).json()
            assert len(second["approvals"]) == 3 and second["next_after"] is None
            ids = [a["approval_id"] for a in data["approvals"] + second["approvals"]]
            assert len(set(ids)) == 103
            assert (await http.get("/v1/approvals", params={"limit": 0})).status_code == 422
            assert (await http.get("/v1/approvals", params={"limit": 101})).status_code == 422
            stale_session = sessions[0]
            store.reset(stale_session.session_id)
            current = (await http.get("/v1/approvals")).json()["approvals"]
            assert len(current) == 51 and all(
                a["session_id"] == sessions[1].session_id for a in current
            )
    finally:
        await runtime.close()
        store.close()


@pytest.mark.parametrize("boundary", ["before_request", "after_owner_approval"])
async def test_steer_during_async_source_validation_replans_without_old_effects(tmp_path, boundary):
    from packages.contracts.models import SteerRequest

    store, session, workspace, runtime = _make_env(
        tmp_path,
        [
            # Before-request validation now precedes inference. There is no
            # old model action to consume in that case; after owner approval
            # the original write proposal has already been generated.
            *([_write_action()] if boundary == "after_owner_approval" else []),
            json.dumps({"tool": "read_file", "arguments": {"path": "note.txt"}}),
            json.dumps(
                {
                    "tool": "finish",
                    "arguments": {"summary": "Kept original file", "objective_revision": 2},
                }
            ),
        ],
        yolo=True,
    )

    class DelayedJournal(LocalJournal):
        enabled = boundary == "before_request"
        entered = asyncio.Event()
        release = asyncio.Event()

        async def resolve(self, event_id):
            # Only delay the actual Agent worker, not submission/owner control.
            if self.enabled and asyncio.current_task() in {
                r.worker for r in runtime._tasks.values()
            }:
                self.enabled = False
                self.entered.set()
                await self.release.wait()
            return await super().resolve(event_id)

    journal = DelayedJournal(store, "agent")
    runtime.journal = journal
    try:
        (workspace / "note.txt").write_text(ORIGINAL)
        text = "public synthetic: overwrite note.txt"
        source = _publish_window(store, session, text)
        task = await _submit_decisions(runtime, session, text, source.event_id, key="delayed-steer")
        if boundary == "after_owner_approval":
            assert (
                await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
            ).state == TaskState.AWAITING_APPROVAL
            entry = _approval_entry(store, task.task_id)
            journal.enabled = True
            await runtime.resolve_approval(entry.payload["approval_id"], _resolution(entry))
        await asyncio.wait_for(journal.entered.wait(), 2)
        await runtime.steer(
            task.task_id,
            SteerRequest(
                instruction="Keep the original file; only inspect it",
                expected_objective_revision=1,
                idempotency_key="delayed-owner-steer",
            ),
        )
        journal.release.set()
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.SUCCEEDED, final.error
        assert final.objective_revision == 2
        assert (workspace / "note.txt").read_text() == ORIGINAL
        assert not any(e.kind in {"file", "command", "external"} for e in final.evidence)
        if boundary == "before_request":
            assert _approval_entry(store, task.task_id) is None
            assert len(runtime.provider.calls) == 2
            assert all(
                "Keep the original file; only inspect it" in json.dumps(call)
                for call in runtime.provider.calls
            )
        else:
            assert not runtime.policy.peek_approval(entry.payload["approval_id"]).used
    finally:
        journal.release.set()
        await runtime.close()
        store.close()


async def test_restart_does_not_restore_partial_speech_execution_approval(tmp_path):
    store, session, workspace, original_runtime = _make_env(tmp_path, [_write_action()], yolo=True)
    recovered_runtime = None
    try:
        (workspace / "note.txt").write_text(ORIGINAL)
        text = "public synthetic: overwrite note.txt"
        source = _publish_window(store, session, text)
        task = await _submit_decisions(
            original_runtime, session, text, source.event_id, key="restart"
        )
        assert (
            await _wait_state(original_runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        ).state == TaskState.AWAITING_APPROVAL
        entry = _approval_entry(store, task.task_id)
        await original_runtime.close()
        recovered_runtime = AgentRuntime(
            LocalJournal(store, "agent"),
            ScriptedProvider([]),
            workspace,
            ToolPolicy(workspace, yolo=True),
        )
        restored = await recovered_runtime.recover()
        assert len(restored) == 1 and restored[0].state == TaskState.PAUSED
        assert (await recovered_runtime.pending_approvals()).approvals == []
        with pytest.raises(ApprovalError, match="unknown or superseded"):
            await recovered_runtime.resolve_approval(
                entry.payload["approval_id"], _resolution(entry)
            )
        assert (workspace / "note.txt").read_text() == ORIGINAL
        assert recovered_runtime._tasks[task.task_id].worker is None
    finally:
        await original_runtime.close()
        if recovered_runtime is not None:
            await recovered_runtime.close()
        store.close()


async def test_correction_to_ended_capture_during_dispatch_does_not_upgrade_old_task(tmp_path):
    store, session, workspace, runtime = _make_env(
        tmp_path, [_write_action(), _finish_action()], allow_write=True
    )

    class DelayedJournal(LocalJournal):
        enabled = False
        reads = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def resolve(self, event_id):
            if self.enabled and asyncio.current_task() in {
                r.worker for r in runtime._tasks.values()
            }:
                self.reads += 1
                # Dispatch's epoch check saw revision 1; the separate source
                # authority read must not accept a revised completed capture.
                if self.reads == 2:
                    self.enabled = False
                    self.entered.set()
                    await self.release.wait()
            return await super().resolve(event_id)

    journal = DelayedJournal(store, "agent")
    runtime.journal = journal
    try:
        (workspace / "note.txt").write_text(ORIGINAL)
        text = "public synthetic: overwrite note.txt"
        source = _publish_window(store, session, text)
        task = await _submit_decisions(
            runtime, session, text, source.event_id, key="correct-during-dispatch"
        )
        assert (
            await _wait_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL)
        ).state == TaskState.AWAITING_APPROVAL
        entry = _approval_entry(store, task.task_id)
        journal.enabled = True
        await runtime.resolve_approval(entry.payload["approval_id"], _resolution(entry))
        await asyncio.wait_for(journal.entered.wait(), 2)
        _publish_window(
            store,
            session,
            "public synthetic: do not overwrite",
            revision=2,
            ended=True,
            key="late-final",
        )
        journal.release.set()
        final = await _wait_terminal(runtime, task.task_id)
        assert final.state == TaskState.FAILED
        assert (workspace / "note.txt").read_text() == ORIGINAL
        assert not any(e.kind == "file" for e in final.evidence)
    finally:
        journal.release.set()
        await runtime.close()
        store.close()
