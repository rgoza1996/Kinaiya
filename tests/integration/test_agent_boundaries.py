"""Lead-authored regressions against real persistence and OS boundaries."""

import asyncio
import json
import os

import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    ProviderInfo,
    SessionRequest,
    TaskRequest,
)
from packages.providers.completion_budget import PreparedComplete
from services.agent import AgentRuntime
from services.agent.tools import ToolFailure, run_shell, run_write
from services.session.store import LocalJournal, Store


class Provider:
    info = ProviderInfo(provider="fixture", model="boundary-tests", local=True)

    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    async def prepare_complete(self, messages):
        """Explicit local test double; no tokenizer or inference claim."""
        return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

    async def complete(self, messages):
        self.calls += 1
        value = self.responses.pop(0)
        if callable(value):
            value = await value()
        return json.dumps(value)


def req(session, text, key="task", **kwargs):
    return TaskRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective=text,
        idempotency_key=key,
        **kwargs,
    )


def user(store, session, text="Write a file", key="source"):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={"text": text, "final": True, "revision": 1},
            idempotency_key=key,
        ),
        "owner",
    )


@pytest.mark.parametrize("attack", ["assistant", "invented", "cross_session", "stale_revision"])
async def test_router_cannot_manufacture_user_authority(tmp_path, attack):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    source = user(store, session)
    objective = source.payload["text"]
    if attack == "assistant":
        source = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                kind="conversation.converse",
                payload={"text": objective, "final": True},
                idempotency_key="assistant",
            ),
            "converse",
        )
    elif attack == "invented":
        objective = "Delete everything instead"
    elif attack == "cross_session":
        source = user(store, store.create_session(SessionRequest()))
    else:
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                kind="conversation.revision",
                payload={
                    "target_event_id": source.event_id,
                    "text": "Do not write anything",
                    "revision": 2,
                },
                idempotency_key="revision",
            ),
            "owner",
        )
    provider = Provider([])
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, tmp_path, ToolPolicy(tmp_path))
    try:
        with pytest.raises(PolicyDenied):
            await runtime.submit(
                req(session, objective, source_event_ids=[source.event_id]), actor="decisions"
            )
        assert provider.calls == 0
    finally:
        await runtime.close()
        store.close()


async def test_source_revision_before_effect_blocks_stale_write(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    source = user(store, session)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_write():
        entered.set()
        await release.wait()
        return {"tool": "write_file", "arguments": {"path": "stale.txt", "content": "obsolete"}}

    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        Provider([delayed_write]),
        tmp_path,
        ToolPolicy(tmp_path, allow_write=True),
    )
    try:
        task = await runtime.submit(
            req(session, source.payload["text"], source_event_ids=[source.event_id]),
            actor="decisions",
        )
        await entered.wait()
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                kind="conversation.revision",
                payload={
                    "target_event_id": source.event_id,
                    "text": "Do not write anything",
                    "revision": 2,
                },
                idempotency_key="revision",
            ),
            "owner",
        )
        release.set()
        final = await runtime.wait(task.task_id, timeout=1)
        assert final.state == "failed"
        assert "Source was revised" in final.error
        assert not (tmp_path / "stale.txt").exists()
    finally:
        await runtime.close()
        store.close()


async def test_recovery_across_byte_limited_pages_preserves_completed_idempotency(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    for index in range(12):
        user(store, session, "x" * 30000, str(index))
    provider = Provider(
        [
            {"tool": "write_file", "arguments": {"path": "result.txt", "content": "actual result"}},
            {
                "tool": "finish",
                "arguments": {"summary": "Evidence verified", "objective_revision": 1},
            },
        ]
    )
    journal = LocalJournal(store, "agent")
    runtime = AgentRuntime(journal, provider, tmp_path, ToolPolicy(tmp_path, allow_write=True))
    request = req(session, "Write result.txt")
    task = await runtime.submit(request)
    final = await runtime.wait(task.task_id, 2)
    assert final.state == "succeeded"
    await runtime.close()
    no_calls = Provider([])
    recovered = AgentRuntime(journal, no_calls, tmp_path, ToolPolicy(tmp_path, allow_write=True))
    try:
        await recovered.recover()
        replay = await recovered.submit(request)
        assert replay.task_id == task.task_id and replay.state == "succeeded"
        assert no_calls.calls == 0
        assert replay.evidence == final.evidence
    finally:
        await recovered.close()
        store.close()


@pytest.mark.sandbox
async def test_shell_cannot_write_protected_core_or_see_supervisor_proc(tmp_path):
    core = tmp_path / "protected"
    core.mkdir()
    guard = core / "guard.txt"
    guard.write_text("protected")
    policy = ToolPolicy(tmp_path, allow_shell=True, protected_roots=(core,))
    try:
        await run_shell(policy, "true", 5, 1)
    except ToolFailure as exc:
        pytest.skip(f"OS sandbox unavailable: {exc}")
    with pytest.raises(ToolFailure):
        await run_shell(policy, "printf compromised > protected/guard.txt", 5, 1)
    assert guard.read_text() == "protected"
    _, evidence = await run_shell(policy, f"test ! -e /proc/{os.getpid()}/environ", 5, 1)
    assert evidence.exit_code == 0


def test_atomic_workspace_write_does_not_mutate_external_hardlink(tmp_path):
    external = tmp_path.parent / (tmp_path.name + "-external")
    external.write_text("protected outside inode")
    alias = tmp_path / "alias.txt"
    alias.hardlink_to(external)
    try:
        run_write(ToolPolicy(tmp_path, allow_write=True), "alias.txt", "workspace edit", 1)
        assert alias.read_text() == "workspace edit"
        assert external.read_text() == "protected outside inode"
    finally:
        external.unlink()


def test_directory_replaced_by_symlink_after_policy_check_fails_closed(tmp_path, monkeypatch):
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    policy = ToolPolicy(tmp_path, allow_write=True)
    original = policy.resolve_path
    checked = 0

    def race(path):
        nonlocal checked
        resolved = original(path)
        checked += 1
        if checked == 2:
            inside.rmdir()
            inside.symlink_to(outside, target_is_directory=True)
        return resolved

    monkeypatch.setattr(policy, "resolve_path", race)
    try:
        with pytest.raises(OSError):
            run_write(policy, "inside/escape.txt", "must not escape", 1)
        assert not (outside / "escape.txt").exists()
    finally:
        outside.rmdir()


async def test_transcript_is_retrieved_only_on_demand_and_cannot_cross_sessions(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    selected = user(store, session, "Selected synthetic context", "selected")
    other = user(
        store, store.create_session(SessionRequest()), "Outside session context", "outside"
    )
    seen = []

    class Observed(Provider):
        async def complete(self, messages):
            seen.append(messages)
            return await super().complete(messages)

    provider = Observed(
        [
            {"tool": "read_transcript", "arguments": {"event_ids": [selected.event_id]}},
            {"tool": "read_transcript", "arguments": {"event_ids": [other.event_id]}},
            {
                "tool": "finish",
                "arguments": {
                    "summary": "must not bypass denied retrieval",
                    "objective_revision": 1,
                },
            },
        ]
    )
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, tmp_path, ToolPolicy(tmp_path))
    try:
        task = await runtime.submit(req(session, "Inspect selected journal context"))
        final = await runtime.wait(task.task_id, 2)
        assert final.state == "failed"
        assert "Selected synthetic context" not in json.dumps(seen[0])
        assert "Selected synthetic context" in json.dumps(seen[1])
        assert "Outside session context" not in json.dumps(seen)
    finally:
        await runtime.close()
        store.close()


async def test_approval_arriving_before_publish_ack_is_not_lost(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    published, release = asyncio.Event(), asyncio.Event()

    class DelayedAcknowledgment(LocalJournal):
        async def append(self, request):
            entry = await super().append(request)
            if request.kind == "approval.requested":
                published.set()
                await release.wait()
            return entry

    provider = Provider(
        [
            {"tool": "write_file", "arguments": {"path": "approved.txt", "content": "approved"}},
            {"tool": "finish", "arguments": {"summary": "Verified write", "objective_revision": 1}},
        ]
    )
    runtime = AgentRuntime(
        DelayedAcknowledgment(store, "agent"), provider, tmp_path, ToolPolicy(tmp_path)
    )
    try:
        task = await runtime.submit(req(session, "Write approved.txt"))
        await asyncio.wait_for(published.wait(), 1)
        event = store.read(kinds=["approval.requested"])[0]
        await runtime.resolve_approval(
            event.payload["approval_id"],
            ApprovalResolution(
                approved=True, action_digest=event.payload["action_digest"], objective_revision=1
            ),
        )
        release.set()
        final = await runtime.wait(task.task_id, 1)
        assert final.state == "succeeded" and (tmp_path / "approved.txt").read_text() == "approved"
    finally:
        release.set()
        await runtime.close()
        store.close()
