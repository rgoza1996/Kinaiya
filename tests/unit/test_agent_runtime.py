"""Unit tests for the seed Agent runtime: loop, evidence, revisions, recovery."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from core.policy import ApprovalError, PolicyDenied, ToolPolicy
from packages.contracts.interfaces import Journal
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    InterruptRequest,
    JournalEntry,
    ProviderInfo,
    Session,
    SteerRequest,
    TaskRequest,
    TaskState,
)
from packages.providers.completion_budget import PreparedComplete
from services.agent.runtime import (
    AgentRuntime,
    IdemConflict,
    StaleRevision,
    TaskNotFound,
    TaskTerminal,
)

assert isinstance(Journal, object)  # interface imported for conformance


class InMemoryJournal:
    """Narrowly scoped test double implementing the contracts Journal."""

    def __init__(self, session: Session) -> None:
        self.session_obj = session
        self.entries: list[JournalEntry] = []
        self._seq = 0

    async def append(self, request: AppendRequest) -> JournalEntry:
        self._seq += 1
        entry = JournalEntry(
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            kind=request.kind,
            payload=request.payload,
            causes=list(request.causes),
            task_refs=list(request.task_refs),
            idempotency_key=request.idempotency_key,
            sequence=self._seq,
            origin="authenticated-user"
            if request.kind == "conversation.user"
            else "agent-supervisor",
            created_at=datetime.now(UTC),
        )
        self.entries.append(entry)
        return entry

    async def read(
        self,
        *,
        after: int = 0,
        limit: int = 100,
        session_id: str | None = None,
        thread_id: str | None = None,
        kinds: list[str] | None = None,
        task_id: str | None = None,
        search: str | None = None,
    ) -> list[JournalEntry]:
        out = [e for e in self.entries if e.sequence > after]
        if session_id is not None:
            out = [e for e in out if e.session_id == session_id]
        if thread_id is not None:
            out = [e for e in out if e.thread_id == thread_id]
        if kinds is not None:
            out = [e for e in out if e.kind in kinds]
        if task_id is not None:
            out = [e for e in out if task_id in e.task_refs]
        if search is not None:
            out = [e for e in out if search in json.dumps(e.payload)]
        return out[:limit]

    def _find(self, event_id: str) -> JournalEntry:
        for entry in self.entries:
            if entry.event_id == event_id:
                return entry
        raise KeyError(event_id)

    async def entry(self, event_id: str) -> JournalEntry:
        return self._find(event_id)

    async def resolve(self, event_id: str) -> JournalEntry:
        event = self._find(event_id)
        merged = dict(event.payload)
        for other in self.entries:
            if (
                other.kind == "conversation.revision"
                and other.sequence > event.sequence
                and other.payload.get("target_event_id") == event_id
            ):
                merged.update(other.payload)
        return event.model_copy(update={"payload": merged})

    async def session(self, session_id: str) -> Session:
        if session_id != self.session_obj.session_id:
            raise KeyError(session_id)
        return self.session_obj

    def kinds(self) -> list[str]:
        return [e.kind for e in self.entries]


class ScriptedProvider:
    """Deterministic fixture provider. Items: dict/str response, Exception to
    raise, or zero-arg callable (sync or async) returning the response."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, str]]] = []
        self.info = ProviderInfo(provider="fixture", model="scripted-seed", local=True)

    async def prepare_complete(self, messages):
        """Explicit local test double; no tokenizer or inference claim."""
        return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if not self.script:
            return json.dumps(
                {"tool": "finish", "arguments": {"summary": "script end", "objective_revision": 1}}
            )
        item = self.script.pop(0)
        if callable(item):
            result = item()
            if asyncio.iscoroutine(result):
                result = await result
            item = result
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            return json.dumps(item)
        return item

    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            yield "fixture"
            _ = messages

        return gen()


class HangProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.hung = asyncio.Event()

    async def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        self.hung.set()
        await asyncio.Event().wait()  # hangs until the task is cancelled
        raise AssertionError("unreachable")


SESSION = Session(session_id="s1", thread_id="t1", channel="agent", epoch=1)


def task_req(objective="write the file", key="k1", revision=1, sources=None):
    return TaskRequest(
        session_id="s1",
        thread_id="t1",
        epoch=1,
        objective=objective,
        idempotency_key=key,
        source_event_ids=sources or [],
        source_revision=revision,
    )


def write_action(path="note.txt", content="hello agent", revision=1):
    return {
        "tool": "write_file",
        "arguments": {"path": path, "content": content, "objective_revision": revision},
    }


def finish_action(revision=1, summary="done"):
    return {"tool": "finish", "arguments": {"summary": summary, "objective_revision": revision}}


def make_runtime(tmp_path, provider, **policy_kw):
    policy_kw.setdefault("allow_write", True)
    policy = ToolPolicy(workspace=tmp_path, **policy_kw)
    journal = InMemoryJournal(SESSION)
    runtime = AgentRuntime(journal, provider, tmp_path, policy)
    return runtime, journal, policy


async def poll_for(predicate, timeout=10.0):
    async with asyncio.timeout(timeout):
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return
            await asyncio.sleep(0.02)


async def _evdone(runtime, task_id) -> bool:
    return len((await runtime.status(task_id)).evidence) >= 1


async def _is_state(runtime, task_id, state) -> bool:
    return (await runtime.status(task_id)).state == state


async def _user_text(journal, text, key, session=SESSION):
    """Plant a finalized authenticated-user message in a test journal."""
    return await journal.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={"text": text, "final": True},
            idempotency_key=key,
        )
    )


def _sandbox_ok(workspace) -> bool:
    """True only when real bubblewrap isolation works in this environment."""
    import subprocess

    from services.agent import tools as agent_tools

    policy = ToolPolicy(workspace=workspace)
    try:
        argv, _ = agent_tools.build_bwrap_args(policy, "true", False)
        done = subprocess.run(argv, capture_output=True, timeout=30)
    except Exception:
        return False
    return done.returncode == 0


async def test_standalone_actual_file_task(tmp_path):
    provider = ScriptedProvider([write_action(), finish_action()])
    runtime, journal, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        content = (tmp_path / "note.txt").read_bytes()
        assert content == b"hello agent"
        file_ev = [e for e in final.evidence if e.kind == "file"]
        assert len(file_ev) == 1
        assert file_ev[0].sha256 == hashlib.sha256(b"hello agent").hexdigest()
        assert file_ev[0].objective_revision == 1
        assert file_ev[0].path == str((tmp_path / "note.txt").resolve())
        assert "agent.command" in journal.kinds()
        assert "agent.tool" in journal.kinds()
        assert "agent.outcome" in journal.kinds()
        outcome = [e for e in journal.entries if e.kind == "agent.outcome"][0]
        assert outcome.task_refs == [task.task_id]
    finally:
        await runtime.close()


async def test_finish_without_evidence_fails(tmp_path):
    provider = ScriptedProvider([finish_action(summary="trust me")])
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.FAILED
        assert "no current-revision evidence" in (final.error or "")
        assert final.evidence == []
    finally:
        await runtime.close()


async def test_failed_shell_must_not_report_success(tmp_path, monkeypatch):
    from core.supervision import command_evidence
    from services.agent import runtime as runtime_module
    from services.agent.tools import _FailedWithEvidence

    async def failed_tool(*args, **kwargs):
        raise _FailedWithEvidence(
            "shell exited 1", command_evidence("false", 1, "", 1, "shell exit=1")
        )

    # Supervisor regression is deterministic on machines without bubblewrap.
    # Real namespace execution and denied writes have separate integration tests.
    monkeypatch.setattr(runtime_module, "run_shell", failed_tool)
    provider = ScriptedProvider(
        [
            {"tool": "shell", "arguments": {"command": "false", "objective_revision": 1}},
            finish_action(summary="all good"),
        ]
    )
    runtime, _, _ = make_runtime(tmp_path, provider, allow_shell=True)
    try:
        task = await runtime.submit(task_req())
        final = await runtime.wait(task.task_id, timeout=30)
        assert final.state == TaskState.FAILED
        assert "failed" in (final.error or "").lower()
        cmd_ev = [e for e in final.evidence if e.kind == "command"]
        assert len(cmd_ev) == 1
        assert cmd_ev[0].exit_code != 0
    finally:
        await runtime.close()


async def test_recovery_after_failed_tool_can_succeed(tmp_path):
    provider = ScriptedProvider(
        [
            {"tool": "shell", "arguments": {"command": "false", "objective_revision": 1}},
            write_action(),
            finish_action(),
        ]
    )
    runtime, _, _ = make_runtime(tmp_path, provider, allow_shell=True)
    try:
        task = await runtime.submit(task_req())
        final = await runtime.wait(task.task_id, timeout=30)
        assert final.state == TaskState.SUCCEEDED
        assert (tmp_path / "note.txt").read_text() == "hello agent"
    finally:
        await runtime.close()


async def test_duplicate_submit_identical_returns_same_task(tmp_path):
    provider = ScriptedProvider([write_action(), finish_action()])
    runtime, journal, _ = make_runtime(tmp_path, provider)
    try:
        first = await runtime.submit(task_req(key="dup"))
        second = await runtime.submit(task_req(key="dup"))
        assert first.task_id == second.task_id
        submits = [
            e
            for e in journal.entries
            if e.kind == "agent.command" and e.payload.get("op") == "submit"
        ]
        assert len(submits) == 1
        final = await runtime.wait(first.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
    finally:
        await runtime.close()


async def test_duplicate_submit_conflicting_payload_rejected(tmp_path):
    provider = ScriptedProvider([])
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        await runtime.submit(task_req(key="dup", objective="first"))
        with pytest.raises(IdemConflict):
            await runtime.submit(task_req(key="dup", objective="second"))
    finally:
        await runtime.close()


async def test_actor_rules(tmp_path):
    provider = ScriptedProvider([])
    runtime, journal, _ = make_runtime(tmp_path, provider)
    try:
        with pytest.raises(PolicyDenied):
            await runtime.submit(task_req(key="a"), actor="intruder")
        with pytest.raises(PolicyDenied):
            await runtime.submit(task_req(key="b"), actor="decisions")
        with pytest.raises(PolicyDenied):
            await runtime.submit(task_req(key="b2", sources=["evt-missing"]), actor="decisions")
        entry = await _user_text(journal, "do the thing", "u1")
        with pytest.raises(PolicyDenied):
            await runtime.submit(
                task_req("invented by classifier", "b3", sources=[entry.event_id]),
                actor="decisions",
            )
        task = await runtime.submit(
            task_req("do the thing", "c", sources=[entry.event_id]), actor="decisions"
        )
        assert task.task_id
        assert task.objective == "do the thing"
        # decisions actor must carry sources for steer too
        with pytest.raises(PolicyDenied):
            await runtime.steer(
                task.task_id,
                SteerRequest(instruction="x", expected_objective_revision=1, idempotency_key="s1"),
                actor="decisions",
            )
    finally:
        await runtime.close()


async def test_decisions_submit_with_sources_ok(tmp_path):
    provider = ScriptedProvider([])
    runtime, journal, _ = make_runtime(tmp_path, provider)
    try:
        entry = await _user_text(journal, "do the thing", "u2")
        task = await runtime.submit(
            task_req("do the thing", "d", sources=[entry.event_id]), actor="decisions"
        )
        assert task.source_event_ids == [entry.event_id]
        assert task.objective == "do the thing"
    finally:
        await runtime.close()


async def test_traversal_and_symlink_cannot_succeed(tmp_path):
    outside = tmp_path.parent / "agent-test-outside.txt"
    outside.write_text("do not touch")
    link = tmp_path / "evil"
    link.symlink_to(outside)
    provider = ScriptedProvider(
        [
            {"tool": "read_file", "arguments": {"path": "../agent-test-outside.txt"}},
            {"tool": "read_file", "arguments": {"path": "evil"}},
            finish_action(summary="read it"),
        ]
    )
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.FAILED
        assert final.evidence == []
        assert outside.read_text() == "do not touch"
    finally:
        await runtime.close()
        outside.unlink(missing_ok=True)


async def test_cancellation_during_inference(tmp_path):
    provider = HangProvider()
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        await poll_for(lambda: provider.hung.is_set())
        # /status must not wait for the model (async, but never blocks on it).
        current = await runtime.status(task.task_id)
        assert current.state in (TaskState.RUNNING, TaskState.ACCEPTED)
        interrupted = await asyncio.wait_for(
            runtime.interrupt(
                task.task_id,
                InterruptRequest(expected_objective_revision=1, idempotency_key="stop1"),
            ),
            timeout=5,
        )
        assert interrupted.state == TaskState.CANCEL_REQUESTED
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.CANCELLED
    finally:
        await runtime.close()


async def test_interrupt_kills_running_shell(tmp_path):
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")

    async def slow_shell():
        return {"tool": "shell", "arguments": {"command": "sleep 60", "objective_revision": 1}}

    provider = ScriptedProvider([slow_shell])
    runtime, _, _ = make_runtime(tmp_path, provider, allow_shell=True)
    try:
        task = await runtime.submit(task_req())
        # Wait until the shell child is actually running.
        async with asyncio.timeout(15):
            while True:
                rec = runtime._tasks[task.task_id]
                if rec.proc_slot.get("proc") is not None:
                    break
                if rec.task.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED):
                    break
                await asyncio.sleep(0.02)
        before = datetime.now(UTC)
        child = rec.proc_slot["proc"]._proc
        interrupted = await asyncio.wait_for(
            runtime.interrupt(
                task.task_id,
                InterruptRequest(expected_objective_revision=1, idempotency_key="stop2"),
            ),
            timeout=5,
        )
        assert (datetime.now(UTC) - before).total_seconds() < 5
        # Killing the process group may finish before acknowledgment returns.
        assert interrupted.state in {TaskState.CANCEL_REQUESTED, TaskState.CANCELLED}
        final = await runtime.wait(task.task_id, timeout=15)
        assert final.state == TaskState.CANCELLED
        assert child.returncode is not None
    finally:
        await runtime.close()


async def test_steer_applies_and_stale_finish_fails(tmp_path):
    gate = asyncio.Event()

    async def gated_stale_finish():
        await gate.wait()
        return finish_action(revision=1, summary="old plan done")

    provider = ScriptedProvider([write_action(), gated_stale_finish])
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        await poll_for(lambda: _evdone(runtime, task.task_id))
        steered = await runtime.steer(
            task.task_id,
            SteerRequest(
                instruction="do something else",
                expected_objective_revision=1,
                idempotency_key="st1",
            ),
        )
        assert steered.objective_revision == 1  # applied at the safe boundary
        gate.set()
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.FAILED
        assert "stale" in (final.error or "").lower()
        assert (await runtime.status(task.task_id)).objective_revision == 2
    finally:
        gate.set()
        await runtime.close()


async def test_steer_requires_reverification_then_succeeds(tmp_path):
    gate = asyncio.Event()

    async def gated_stale_write():
        await gate.wait()
        return write_action(path="stale.txt", content="stale plan", revision=1)

    provider = ScriptedProvider(
        [
            write_action(path="v1.txt", content="old"),
            gated_stale_write,
            write_action(path="v2.txt", content="reverified", revision=2),
            finish_action(revision=2, summary="new plan done"),
        ]
    )
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        await poll_for(lambda: _evdone(runtime, task.task_id))
        await runtime.steer(
            task.task_id,
            SteerRequest(
                instruction="write v2 instead", expected_objective_revision=1, idempotency_key="st2"
            ),
        )
        gate.set()
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        assert final.objective_revision == 2
        assert (tmp_path / "v2.txt").read_text() == "reverified"
        rev1 = [e for e in final.evidence if e.objective_revision == 1]
        rev2 = [e for e in final.evidence if e.objective_revision == 2]
        assert len(rev1) == 1 and len(rev2) >= 1
    finally:
        gate.set()
        await runtime.close()


async def test_steer_rejects_stale_revision(tmp_path):
    provider = ScriptedProvider([])
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        with pytest.raises(StaleRevision):
            await runtime.steer(
                task.task_id,
                SteerRequest(
                    instruction="x", expected_objective_revision=99, idempotency_key="stale"
                ),
            )
    finally:
        await runtime.close()


async def test_approval_flow_write_without_grant(tmp_path):
    provider = ScriptedProvider([write_action(), finish_action()])
    policy2 = ToolPolicy(workspace=tmp_path)
    journal2 = InMemoryJournal(SESSION)
    runtime2 = AgentRuntime(journal2, provider, tmp_path, policy2)
    try:
        task = await runtime2.submit(task_req())
        await poll_for(lambda: _is_state(runtime2, task.task_id, TaskState.AWAITING_APPROVAL))
        approval_id = runtime2.approval_for_task(task.task_id)
        assert approval_id
        requested = [e for e in journal2.entries if e.kind == "approval.requested"]
        assert len(requested) == 1
        args = {"path": "note.txt", "content": "hello agent", "objective_revision": 1}
        good_digest = policy2.digest_for("write_file", args, 1)
        assert requested[0].payload["action_digest"] == good_digest
        # Wrong digest is rejected.
        with pytest.raises(ApprovalError):
            await runtime2.resolve_approval(
                approval_id,
                ApprovalResolution(
                    approved=True, action_digest="sha256:wrong", objective_revision=1
                ),
            )
        # Non-owner cannot resolve.
        with pytest.raises(PolicyDenied):
            await runtime2.resolve_approval(
                approval_id,
                ApprovalResolution(approved=True, action_digest=good_digest, objective_revision=1),
                actor="intruder",
            )
        result = await runtime2.resolve_approval(
            approval_id,
            ApprovalResolution(approved=True, action_digest=good_digest, objective_revision=1),
        )
        assert result["approved"] is True
        final = await runtime2.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        assert (tmp_path / "note.txt").read_text() == "hello agent"
        assert policy2.peek_approval(approval_id).used is True
    finally:
        await runtime2.close()


async def test_denied_approval_fails_task(tmp_path):
    provider = ScriptedProvider([write_action(), finish_action()])
    policy = ToolPolicy(workspace=tmp_path)
    runtime = AgentRuntime(InMemoryJournal(SESSION), provider, tmp_path, policy)
    try:
        task = await runtime.submit(task_req())
        await poll_for(lambda: _is_state(runtime, task.task_id, TaskState.AWAITING_APPROVAL))
        approval_id = runtime.approval_for_task(task.task_id)
        args = {"path": "note.txt", "content": "hello agent", "objective_revision": 1}
        digest = policy.digest_for("write_file", args, 1)
        result = await runtime.resolve_approval(
            approval_id,
            ApprovalResolution(approved=False, action_digest=digest, objective_revision=1),
        )
        assert result["approved"] is False
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.FAILED
        assert not (tmp_path / "note.txt").exists()
    finally:
        await runtime.close()


async def test_recover_marks_uncertain_work_paused_without_rerun(tmp_path):
    journal = InMemoryJournal(SESSION)
    provider = ScriptedProvider([write_action(), finish_action()])
    runtime = AgentRuntime(
        journal, provider, tmp_path, ToolPolicy(workspace=tmp_path, allow_write=True)
    )
    try:
        task = await runtime.submit(task_req(key="rec"))
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        # A fresh runtime over a journal whose outcome entry was lost (crash
        # before outcome commit) must pause, never re-execute the write.
        journal2 = InMemoryJournal(SESSION)
        for entry in journal.entries:
            if entry.kind == "agent.outcome":
                continue
            journal2.entries.append(entry)
            journal2._seq = max(journal2._seq, entry.sequence)
        (tmp_path / "note.txt").unlink()
        runtime2 = AgentRuntime(
            journal2,
            ScriptedProvider([]),
            tmp_path,
            ToolPolicy(workspace=tmp_path, allow_write=True),
        )
        try:
            recovered = await runtime2.recover()
            assert len(recovered) == 1
            assert recovered[0].state == TaskState.PAUSED
            assert "not retried" in recovered[0].summary
            assert not (tmp_path / "note.txt").exists()
        finally:
            await runtime2.close()
    finally:
        await runtime.close()


async def test_wait_timeout_and_unknown_task(tmp_path):
    provider = HangProvider()
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        await poll_for(lambda: provider.hung.is_set())
        current = await runtime.wait(task.task_id, timeout=0.2)
        assert current.state not in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED)
        with pytest.raises(TaskNotFound):
            await runtime.wait("nope", timeout=0.1)
        with pytest.raises(TaskNotFound):
            await runtime.status("nope")
    finally:
        await runtime.close()


async def test_steer_terminal_task_rejected(tmp_path):
    provider = ScriptedProvider([write_action(), finish_action()])
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        with pytest.raises(TaskTerminal):
            await runtime.steer(
                task.task_id,
                SteerRequest(
                    instruction="more", expected_objective_revision=1, idempotency_key="late"
                ),
            )
    finally:
        await runtime.close()


async def test_model_cannot_supply_evidence(tmp_path):
    # Even if the model claims evidence paths/hashes in prose, the task can
    # only succeed on supervisor-recorded evidence.
    provider = ScriptedProvider(
        [
            {"tool": "search", "arguments": {"pattern": "nothing-matches-this-xyz"}},
            finish_action(summary="evidence: /etc/passwd sha256=deadbeef"),
        ]
    )
    runtime, _, _ = make_runtime(tmp_path, provider)
    try:
        task = await runtime.submit(task_req())
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED  # search evidence is real
        assert all(e.sha256 != "deadbeef" for e in final.evidence if e.sha256)
        assert all(
            (e.path or "").startswith(str(tmp_path.resolve()))
            or e.path is None
            or e.kind == "command"
            for e in final.evidence
        )
    finally:
        await runtime.close()
