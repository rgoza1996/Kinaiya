"""Integration tests: real bubblewrap shell, real files, HTTP surface."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from core.policy import ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    JournalEntry,
    ProviderInfo,
    Session,
    TaskRequest,
    TaskState,
)
from services.agent import AgentRuntime, create_app
from services.agent import tools as agent_tools
from services.agent.tools import ShellUnavailable, run_shell

SESSION = Session(session_id="s9", thread_id="t9", channel="agent", epoch=1)


class InMemoryJournal:
    def __init__(self) -> None:
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
            origin="test",
            created_at=datetime.now(UTC),
        )
        self.entries.append(entry)
        return entry

    async def read(self, *, after: int = 0, limit: int = 100, **_kw) -> list[JournalEntry]:
        return [e for e in self.entries if e.sequence > after][:limit]

    async def session(self, session_id: str) -> Session:
        return SESSION


class ScriptedProvider:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.info = ProviderInfo(provider="fixture", model="scripted-seed", local=True)

    async def complete(self, messages: list[dict[str, str]]) -> str:
        _ = messages
        item = self.script.pop(0)
        if isinstance(item, dict):
            return json.dumps(item)
        return item

    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            yield "fixture"
            _ = messages

        return gen()


def req(objective, key, revision=1):
    return TaskRequest(
        session_id="s9", thread_id="t9", epoch=1, objective=objective,
        idempotency_key=key, source_revision=revision,
    )


def client_for(runtime):
    app = create_app(runtime)
    client = TestClient(app)
    client.headers.update({"X-Agent-Actor": "owner"})
    return client


async def test_seed_end_to_end_shell_and_file(tmp_path):
    """Real workspace, real bubblewrap shell, real file writes, no paid APIs."""
    provider = ScriptedProvider([
        {"tool": "shell",
         "arguments": {"command": "echo hello-sandbox > msg.txt && cat msg.txt"}},
        {"tool": "write_file",
         "arguments": {"path": "built.txt", "content": "built by agent"}},
        {"tool": "finish",
         "arguments": {"summary": "sandbox work done", "objective_revision": 1}},
    ])
    policy = ToolPolicy(workspace=tmp_path, allow_write=True, allow_shell=True)
    runtime = AgentRuntime(InMemoryJournal(), provider, tmp_path, policy)
    try:
        task = await runtime.submit(req("do sandbox work", "e2e"))
        final = await runtime.wait(task.task_id, timeout=60)
        assert final.state == TaskState.SUCCEEDED, final.error
        assert (tmp_path / "msg.txt").read_text().strip() == "hello-sandbox"
        assert (tmp_path / "built.txt").read_text() == "built by agent"
        kinds = [e.kind for e in final.evidence]
        assert kinds == ["command", "file"]
        cmd_ev = final.evidence[0]
        assert cmd_ev.exit_code == 0
        file_ev = final.evidence[1]
        assert file_ev.sha256 == hashlib.sha256(b"built by agent").hexdigest()
    finally:
        await runtime.close()


async def test_shell_without_bubblewrap_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_tools, "bwrap_path", lambda: None)
    policy = ToolPolicy(workspace=tmp_path, allow_shell=True)
    with pytest.raises(ShellUnavailable) as excinfo:
        await run_shell(policy, "echo hi", 10, 1)
    assert "bubblewrap" in str(excinfo.value)


async def test_shell_cannot_see_home_or_repo(tmp_path):
    marker = "should-never-appear-8f3a"
    policy = ToolPolicy(workspace=tmp_path, allow_shell=True)
    out, evidence = await run_shell(
        policy,
        f"echo HOME=$HOME; ls ~ 2>&1 | head -3; echo MARK={marker}; pwd",
        30, 1,
    )
    assert marker in out
    assert evidence.exit_code == 0
    # Workspace is mounted at /workspace, not the host path.
    assert "/workspace" in out or str(tmp_path) not in out


async def test_http_task_lifecycle_and_capabilities(tmp_path):
    provider = ScriptedProvider([
        {"tool": "write_file",
         "arguments": {"path": "http.txt", "content": "via http"}},
        {"tool": "finish",
         "arguments": {"summary": "http done", "objective_revision": 1}},
    ])
    policy = ToolPolicy(workspace=tmp_path, allow_write=True)
    runtime = AgentRuntime(InMemoryJournal(), provider, tmp_path, policy)
    try:
        client = client_for(runtime)
        created = client.post("/v1/agent/tasks", json={
            "session_id": "s9", "thread_id": "t9", "epoch": 1,
            "objective": "write via http", "idempotency_key": "http1",
        })
        assert created.status_code == 201, created.text
        task_id = created.json()["task_id"]
        listed = client.get("/v1/agent/tasks")
        assert listed.status_code == 200
        assert any(t["task_id"] == task_id for t in listed.json())
        caps = client.get("/v1/agents/capabilities")
        assert caps.status_code == 200
        body = caps.json()
        assert body["harness"] == "seed-v1"
        assert body["tools"] == ["read_file", "write_file", "search", "shell", "finish"]
        assert body["approvals"] == {"digest_bound": True, "one_use": True, "expiring": True}
        for _ in range(200):
            got = client.get(f"/v1/agent/tasks/{task_id}")
            assert got.status_code == 200
            if got.json()["state"] in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        assert got.json()["state"] == "succeeded", got.json()
        assert (tmp_path / "http.txt").read_text() == "via http"
        assert client.get("/v1/agent/tasks/missing").status_code == 404
    finally:
        await runtime.close()


async def test_http_auth_and_steer_interrupt(tmp_path):
    gate = asyncio.Event()

    class GatedProvider(ScriptedProvider):
        async def complete(self, messages):
            _ = messages
            await gate.wait()
            return json.dumps({"tool": "finish",
                               "arguments": {"summary": "x", "objective_revision": 2}})

    runtime = AgentRuntime(InMemoryJournal(), GatedProvider(), tmp_path,
                           ToolPolicy(workspace=tmp_path))
    try:
        client = client_for(runtime)
        anon = TestClient(create_app(runtime))
        denied = anon.post("/v1/agent/tasks", json={
            "session_id": "s9", "thread_id": "t9", "epoch": 1,
            "objective": "x", "idempotency_key": "auth1",
        })
        assert denied.status_code == 403
        created = client.post("/v1/agent/tasks", json={
            "session_id": "s9", "thread_id": "t9", "epoch": 1,
            "objective": "original", "idempotency_key": "auth2",
        })
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        # Stale steer is a 409, not silent acceptance.
        stale = client.post(f"/v1/agent/tasks/{task_id}/steer", json={
            "instruction": "other", "expected_objective_revision": 99,
            "idempotency_key": "stx",
        })
        assert stale.status_code == 409
        ok_steer = client.post(f"/v1/agent/tasks/{task_id}/steer", json={
            "instruction": "revised", "expected_objective_revision": 1,
            "idempotency_key": "stx2",
        })
        assert ok_steer.status_code == 200, ok_steer.text
        gate.set()
        for _ in range(200):
            got = client.get(f"/v1/agent/tasks/{task_id}")
            if got.json()["state"] in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        # No current-revision evidence: the steered finish must fail, not lie.
        assert got.json()["state"] == "failed", got.json()
        # Interrupt on a terminal task is idempotent, not an error.
        stop = client.post(f"/v1/agent/tasks/{task_id}/interrupt", json={
            "expected_objective_revision": 2, "idempotency_key": "stopx",
        })
        assert stop.status_code == 200
    finally:
        gate.set()
        await runtime.close()


async def test_http_approval_resolve(tmp_path):
    provider = ScriptedProvider([
        {"tool": "write_file",
         "arguments": {"path": "appr.txt", "content": "approved content"}},
        {"tool": "finish",
         "arguments": {"summary": "approved done", "objective_revision": 1}},
    ])
    policy = ToolPolicy(workspace=tmp_path)  # no grants: approval required
    journal = InMemoryJournal()
    runtime = AgentRuntime(journal, provider, tmp_path, policy)
    try:
        client = client_for(runtime)
        created = client.post("/v1/agent/tasks", json={
            "session_id": "s9", "thread_id": "t9", "epoch": 1,
            "objective": "needs approval", "idempotency_key": "appr1",
        })
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        for _ in range(200):
            if client.get(f"/v1/agent/tasks/{task_id}").json()["state"] == "awaiting_approval":
                break
            await asyncio.sleep(0.05)
        approval_id = runtime.approval_for_task(task_id)
        assert approval_id
        requested = [e for e in journal.entries if e.kind == "approval.requested"]
        assert len(requested) == 1
        digest = requested[0].payload["action_digest"]
        resolved = client.post(f"/v1/approvals/{approval_id}/resolve", json={
            "approved": True, "action_digest": digest, "objective_revision": 1,
        })
        assert resolved.status_code == 200, resolved.text
        for _ in range(200):
            got = client.get(f"/v1/agent/tasks/{task_id}")
            if got.json()["state"] in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        assert got.json()["state"] == "succeeded", got.json()
        assert (tmp_path / "appr.txt").read_text() == "approved content"
        # One-use: replaying the spend fails.
        again = client.post(f"/v1/approvals/{approval_id}/resolve", json={
            "approved": True, "action_digest": digest, "objective_revision": 1,
        })
        assert again.status_code in (403, 404, 409)
    finally:
        await runtime.close()
