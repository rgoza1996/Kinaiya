"""Trusted write_file argument validation before approval/effect (issue 105)."""

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

from core.policy import ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    JournalEntry,
    ProviderInfo,
    Session,
    SteerRequest,
    TaskRequest,
    TaskState,
)
from packages.providers.completion_budget import PreparedComplete
from services.agent.runtime import AgentRuntime
from tests.agent_submit_double import InMemorySubmitProtocol

SESSION = Session(session_id="s1", thread_id="t1", channel="agent", epoch=1)


class InMemoryJournal(InMemorySubmitProtocol):
    def __init__(self, session):
        super().__init__()
        self.session_obj = session
        self.entries = []
        self._seq = 0

    async def append(self, request: AppendRequest) -> JournalEntry:
        self._seq += 1
        e = JournalEntry(
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
        self.entries.append(e)
        return e

    async def read(
        self,
        *,
        after=0,
        limit=100,
        session_id=None,
        thread_id=None,
        kinds=None,
        task_id=None,
        search=None,
    ):
        out = [e for e in self.entries if e.sequence > after]
        if session_id is not None:
            out = [e for e in out if e.session_id == session_id]
        if thread_id is not None:
            out = [e for e in out if e.thread_id == thread_id]
        if kinds is not None:
            out = [e for e in out if e.kind in kinds]
        if task_id is not None:
            out = [e for e in out if task_id in e.task_refs]
        return out[:limit]

    async def entry(self, event_id):
        return self.resolve_sync(event_id)

    def resolve_sync(self, event_id):
        for e in self.entries:
            if e.event_id == event_id:
                return e
        raise KeyError(event_id)

    async def resolve(self, event_id):
        return self.resolve_sync(event_id)

    async def session(self, session_id):
        if session_id != self.session_obj.session_id:
            raise KeyError(session_id)
        return self.session_obj


class ScriptedProvider:
    def __init__(self, script):
        self.script = list(script)
        self.info = ProviderInfo(provider="fixture", model="seed", local=True)

    async def prepare_complete(self, messages):
        """Explicit local test double; no tokenizer or inference claim."""
        return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

    async def complete(self, messages):
        import json as _j

        if not self.script:
            return _j.dumps(
                {"tool": "finish", "arguments": {"summary": "end", "objective_revision": 1}}
            )
        item = self.script.pop(0)
        if callable(item):
            r = item()
            if asyncio.iscoroutine(r):
                r = await r
            item = r
        if isinstance(item, dict):
            return _j.dumps(item)
        return item

    def stream(self, messages):
        async def gen():
            yield "x"

        return gen()


def task_req(key="k", revision=1):
    return TaskRequest(
        session_id="s1",
        thread_id="t1",
        epoch=1,
        objective="write it",
        idempotency_key=key,
        source_event_ids=[],
        source_revision=revision,
    )


def finish_action(revision=1, summary="done"):
    return {"tool": "finish", "arguments": {"summary": summary, "objective_revision": revision}}


def pol(tmp_path, mode):
    if mode == "grant":
        return ToolPolicy(workspace=tmp_path, allow_write=True)
    if mode == "yolo":
        return ToolPolicy(workspace=tmp_path, yolo=True)
    return ToolPolicy(workspace=tmp_path)


MALFORMED = [
    ("int-path", {"path": 7, "content": "x"}, "note-int.txt"),
    ("int-content", {"path": "note.txt", "content": 8}, "note.txt"),
    ("missing-content", {"path": "note.txt"}, "note.txt"),
    ("missing-path", {"content": "x"}, "note-missing.txt"),
    ("empty-path", {"path": "", "content": "x"}, "empty-probe.txt"),
    ("nul-path", {"path": "a\x00b", "content": "x"}, "nul-probe.txt"),
    ("unknown-field", {"path": "note.txt", "content": "x", "unsupported_field": True}, "note.txt"),
    ("bool-revision", {"path": "note.txt", "content": "x", "objective_revision": True}, "note.txt"),
    (
        "mismatch-revision",
        {"path": "note.txt", "content": "x", "objective_revision": 2},
        "note.txt",
    ),
    ("zero-revision", {"path": "note.txt", "content": "x", "objective_revision": 0}, "note.txt"),
    ("oversize", {"path": "big.txt", "content": "y" * (65536 * 4 + 1)}, "big.txt"),
]


@pytest.mark.parametrize("mode", ["default", "grant", "yolo"])
@pytest.mark.parametrize("case", MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_malformed_write_no_approval_no_effect(tmp_path, mode, case):
    name, args, probe = case
    journal = InMemoryJournal(SESSION)
    provider = ScriptedProvider([{"tool": "write_file", "arguments": dict(args)}])
    runtime = AgentRuntime(journal, provider, tmp_path, pol(tmp_path, mode), max_steps=1)
    try:
        task = await runtime.submit(task_req(key=f"{mode}-{name}"))
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.FAILED
        assert [e for e in journal.entries if e.kind == "approval.requested"] == []
        assert runtime.approval_for_task(task.task_id) is None
        rec = runtime._tasks[task.task_id]
        assert rec.last_tool_ok is False
        assert not (tmp_path / probe).exists()
        assert not [e for e in journal.entries if e.kind == "agent.tool" and e.payload.get("ok")]
        assert not list(tmp_path.glob(".kinaiya-*"))
    finally:
        await runtime.close()


async def test_direct_dispatch_malformed_never_consumes_approval(tmp_path):
    journal = InMemoryJournal(SESSION)
    provider = ScriptedProvider([])
    policy = ToolPolicy(workspace=tmp_path, allow_write=True)
    runtime = AgentRuntime(journal, provider, tmp_path, policy, max_steps=2)
    try:
        task = await runtime.submit(task_req(key="direct"))
        rec = runtime._tasks[task.task_id]
        good = {"path": "ok.txt", "content": "hi"}
        digest = policy.digest_for("write_file", good, 1)
        ap = policy.create_approval(digest, 1)
        await runtime._dispatch(
            rec,
            "write_file",
            {"path": 7, "content": "x"},
            digest,
            "raw",
            approval_id=ap.approval_id,
        )
        assert ap.used is False
        assert rec.last_tool_ok is False
        assert not (tmp_path / "7").exists()
        assert not (tmp_path / "ok.txt").exists()
    finally:
        await runtime.close()


async def test_valid_no_metadata_digest_effect(tmp_path):
    args = {"path": "plain.txt", "content": "hello"}
    journal = InMemoryJournal(SESSION)
    provider = ScriptedProvider([{"tool": "write_file", "arguments": dict(args)}, finish_action()])
    policy = ToolPolicy(workspace=tmp_path)
    runtime = AgentRuntime(journal, provider, tmp_path, policy, max_steps=2)
    try:
        task = await runtime.submit(task_req(key="valid-plain"))
        async with asyncio.timeout(10):
            while True:
                if (await runtime.status(task.task_id)).state == TaskState.AWAITING_APPROVAL:
                    break
                await asyncio.sleep(0.02)
        req = [e for e in journal.entries if e.kind == "approval.requested"][0]
        assert req.payload["arguments"] == args
        assert req.payload["action_digest"] == policy.digest_for("write_file", args, 1)
        await runtime.resolve_approval(
            req.payload["approval_id"],
            ApprovalResolution(
                approved=True, action_digest=req.payload["action_digest"], objective_revision=1
            ),
        )
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        assert (tmp_path / "plain.txt").read_bytes() == b"hello"
        fe = [e for e in final.evidence if e.kind == "file"][0]
        assert fe.objective_revision == 1 and fe.sha256 == hashlib.sha256(b"hello").hexdigest()
        assert policy.peek_approval(req.payload["approval_id"]).used is True
    finally:
        await runtime.close()


async def test_valid_with_matching_metadata_digest_effect(tmp_path):
    args = {"path": "meta.txt", "content": "hi meta", "objective_revision": 1}
    journal = InMemoryJournal(SESSION)
    provider = ScriptedProvider([{"tool": "write_file", "arguments": dict(args)}, finish_action()])
    policy = ToolPolicy(workspace=tmp_path)
    runtime = AgentRuntime(journal, provider, tmp_path, policy, max_steps=2)
    try:
        task = await runtime.submit(task_req(key="valid-meta"))
        async with asyncio.timeout(10):
            while True:
                if (await runtime.status(task.task_id)).state == TaskState.AWAITING_APPROVAL:
                    break
                await asyncio.sleep(0.02)
        req = [e for e in journal.entries if e.kind == "approval.requested"][0]
        assert req.payload["action_digest"] == policy.digest_for("write_file", args, 1)
        await runtime.resolve_approval(
            req.payload["approval_id"],
            ApprovalResolution(
                approved=True, action_digest=req.payload["action_digest"], objective_revision=1
            ),
        )
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        assert (tmp_path / "meta.txt").read_text() == "hi meta"
    finally:
        await runtime.close()


async def test_empty_and_256k_boundary(tmp_path):
    policy = ToolPolicy(workspace=tmp_path, allow_write=True)
    # empty content valid
    j1 = InMemoryJournal(SESSION)
    r1 = AgentRuntime(
        j1,
        ScriptedProvider(
            [
                {"tool": "write_file", "arguments": {"path": "empty.txt", "content": ""}},
                finish_action(),
            ]
        ),
        tmp_path,
        policy,
        max_steps=2,
    )
    try:
        t = await r1.submit(task_req(key="empty"))
        f = await r1.wait(t.task_id, timeout=10)
        assert f.state == TaskState.SUCCEEDED and (tmp_path / "empty.txt").read_bytes() == b""
    finally:
        await r1.close()
    # exact 256KiB with multibyte boundary ('é' 2 bytes x 131072 = 262144)
    exact = "é" * 131072
    assert len(exact.encode("utf-8")) == 65536 * 4
    j2 = InMemoryJournal(SESSION)
    r2 = AgentRuntime(
        j2,
        ScriptedProvider(
            [
                {"tool": "write_file", "arguments": {"path": "exact.txt", "content": exact}},
                finish_action(),
            ]
        ),
        tmp_path,
        ToolPolicy(workspace=tmp_path, allow_write=True),
        max_steps=2,
    )
    try:
        t = await r2.submit(task_req(key="exact"))
        f = await r2.wait(t.task_id, timeout=10)
        assert f.state == TaskState.SUCCEEDED
        assert (tmp_path / "exact.txt").read_bytes() == exact.encode("utf-8")
    finally:
        await r2.close()
    # one byte over fails bounded with no file
    j3 = InMemoryJournal(SESSION)
    r3 = AgentRuntime(
        j3,
        ScriptedProvider(
            [{"tool": "write_file", "arguments": {"path": "over.txt", "content": exact + "x"}}]
        ),
        tmp_path,
        ToolPolicy(workspace=tmp_path, allow_write=True),
        max_steps=1,
    )
    try:
        t = await r3.submit(task_req(key="over"))
        f = await r3.wait(t.task_id, timeout=10)
        assert f.state == TaskState.FAILED and not (tmp_path / "over.txt").exists()
        assert r3._tasks[t.task_id].last_tool_ok is False
    finally:
        await r3.close()


async def test_invalid_utf8_surrogate_refused(tmp_path):
    bad = "\ud800"
    journal = InMemoryJournal(SESSION)
    runtime = AgentRuntime(
        journal,
        ScriptedProvider(
            [{"tool": "write_file", "arguments": {"path": "bad.txt", "content": bad}}]
        ),
        tmp_path,
        ToolPolicy(workspace=tmp_path, allow_write=True),
        max_steps=1,
    )
    try:
        task = await runtime.submit(task_req(key="surrogate"))
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.FAILED
        assert not (tmp_path / "bad.txt").exists()
        assert [e for e in journal.entries if e.kind == "approval.requested"] == []
    finally:
        await runtime.close()


async def test_recovery_after_malformed_then_correct(tmp_path):
    provider = ScriptedProvider(
        [
            {"tool": "write_file", "arguments": {"path": 7, "content": "x"}},
            {"tool": "write_file", "arguments": {"path": "fixed.txt", "content": "ok"}},
            finish_action(),
        ]
    )
    runtime = AgentRuntime(
        InMemoryJournal(SESSION),
        provider,
        tmp_path,
        ToolPolicy(workspace=tmp_path, allow_write=True),
        max_steps=3,
    )
    try:
        task = await runtime.submit(task_req(key="recover"))
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        assert (tmp_path / "fixed.txt").read_text() == "ok"
        assert not (tmp_path / "7").exists()
    finally:
        await runtime.close()


async def test_steer_revision_still_enforced(tmp_path):
    gate = asyncio.Event()

    async def stale():
        await gate.wait()
        return {
            "tool": "write_file",
            "arguments": {"path": "stale.txt", "content": "old", "objective_revision": 1},
        }

    provider = ScriptedProvider(
        [
            {"tool": "write_file", "arguments": {"path": "v1.txt", "content": "old"}},
            stale,
            {
                "tool": "write_file",
                "arguments": {"path": "v2.txt", "content": "new", "objective_revision": 2},
            },
            finish_action(revision=2, summary="new done"),
        ]
    )
    runtime = AgentRuntime(
        InMemoryJournal(SESSION),
        provider,
        tmp_path,
        ToolPolicy(workspace=tmp_path, allow_write=True),
        max_steps=4,
    )
    try:
        task = await runtime.submit(task_req(key="steer"))
        async with asyncio.timeout(10):
            while True:
                if len((await runtime.status(task.task_id)).evidence) >= 1:
                    break
                await asyncio.sleep(0.02)
        await runtime.steer(
            task.task_id,
            SteerRequest(instruction="v2", expected_objective_revision=1, idempotency_key="st1"),
        )
        gate.set()
        final = await runtime.wait(task.task_id, timeout=10)
        assert final.state == TaskState.SUCCEEDED
        assert (tmp_path / "v2.txt").read_text() == "new"
        assert final.objective_revision == 2
    finally:
        gate.set()
        await runtime.close()
