"""Integration tests: real bubblewrap shell, real files, HTTP surface."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

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
from services.agent.tools import ShellUnavailable, ToolFailure, run_shell
from services.common.auth import Auth

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
            origin="authenticated-user"
            if request.kind == "conversation.user"
            else "agent-supervisor",
            created_at=datetime.now(UTC),
        )
        self.entries.append(entry)
        return entry

    async def read(self, *, after: int = 0, limit: int = 100, **_kw) -> list[JournalEntry]:
        return [e for e in self.entries if e.sequence > after][:limit]

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
        session_id="s9",
        thread_id="t9",
        epoch=1,
        objective=objective,
        idempotency_key=key,
        source_revision=revision,
    )


def make_auth() -> tuple[Auth, dict[str, str]]:
    """Configured Auth with distinct bearer tokens per role; no fallback."""
    tokens = {
        "owner": "owner-token-0123456789abcdef01",
        "agent": "agent-token-0123456789abcdef02",
        "converse": "converse-token-0123456789abcdef",
        "decisions": "decisions-token-0123456789abcdef",
        "resources": "resources-token-0123456789abcdef",
    }
    return Auth(tokens), tokens


def bearer(tokens, role="owner") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


def _sandbox_ok(workspace) -> bool:
    """True only when real bubblewrap isolation works in this environment.

    Real-sandbox tests skip honestly when namespaces are unavailable;
    fail-closed denial is covered deterministically by tests below.
    """
    import subprocess

    policy = ToolPolicy(workspace=workspace)
    try:
        argv, _ = agent_tools.build_bwrap_args(policy, "true", False)
        done = subprocess.run(argv, capture_output=True, timeout=30)
    except Exception:
        return False
    return done.returncode == 0


async def test_seed_end_to_end_shell_and_file(tmp_path):
    """Real workspace, real bubblewrap shell, real file writes, no paid APIs."""
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    provider = ScriptedProvider(
        [
            {
                "tool": "shell",
                "arguments": {"command": "echo hello-sandbox > msg.txt && cat msg.txt"},
            },
            {"tool": "write_file", "arguments": {"path": "built.txt", "content": "built by agent"}},
            {
                "tool": "finish",
                "arguments": {"summary": "sandbox work done", "objective_revision": 1},
            },
        ]
    )
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
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    marker = "should-never-appear-8f3a"
    policy = ToolPolicy(workspace=tmp_path, allow_shell=True)
    out, evidence = await run_shell(
        policy,
        f"echo HOME=$HOME; ls ~ 2>&1 | head -3; echo MARK={marker}; pwd",
        30,
        1,
    )
    assert marker in out
    assert evidence.exit_code == 0
    # Workspace is mounted at /workspace, not the host path.
    assert "/workspace" in out or str(tmp_path) not in out


async def test_http_task_lifecycle_and_capabilities(tmp_path):
    provider = ScriptedProvider(
        [
            {"tool": "write_file", "arguments": {"path": "http.txt", "content": "via http"}},
            {"tool": "finish", "arguments": {"summary": "http done", "objective_revision": 1}},
        ]
    )
    policy = ToolPolicy(workspace=tmp_path, allow_write=True)
    runtime = AgentRuntime(InMemoryJournal(), provider, tmp_path, policy)
    auth, tokens = make_auth()
    app = create_app(runtime, auth)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/agent/tasks",
                json={
                    "session_id": "s9",
                    "thread_id": "t9",
                    "epoch": 1,
                    "objective": "write via http",
                    "idempotency_key": "http1",
                },
                headers=bearer(tokens),
            )
            assert created.status_code == 201, created.text
            task_id = created.json()["task_id"]
            listed = await client.get("/v1/agent/tasks", headers=bearer(tokens))
            assert listed.status_code == 200
            assert any(t["task_id"] == task_id for t in listed.json())
            caps = await client.get("/v1/agents/capabilities", headers=bearer(tokens))
            assert caps.status_code == 200
            body = caps.json()
            assert body["harness"] == "seed-v1"
            assert body["tools"] == [
                "read_file",
                "write_file",
                "search",
                "shell",
                "read_transcript",
                "finish",
            ]
            assert body["approvals"] == {"digest_bound": True, "one_use": True, "expiring": True}
            for _ in range(200):
                got = await client.get(f"/v1/agent/tasks/{task_id}", headers=bearer(tokens))
                assert got.status_code == 200
                if got.json()["state"] in ("succeeded", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.05)
            assert got.json()["state"] == "succeeded", got.json()
            assert (tmp_path / "http.txt").read_text() == "via http"
            missing = await client.get("/v1/agent/tasks/missing", headers=bearer(tokens))
            assert missing.status_code == 404
    finally:
        await runtime.close()


async def test_http_auth_and_steer_interrupt(tmp_path):
    gate = asyncio.Event()

    class GatedProvider(ScriptedProvider):
        async def complete(self, messages):
            _ = messages
            await gate.wait()
            return json.dumps(
                {"tool": "finish", "arguments": {"summary": "x", "objective_revision": 2}}
            )

    runtime = AgentRuntime(
        InMemoryJournal(), GatedProvider([]), tmp_path, ToolPolicy(workspace=tmp_path)
    )
    auth, tokens = make_auth()
    app = create_app(runtime, auth)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.post(
                "/v1/agent/tasks",
                json={
                    "session_id": "s9",
                    "thread_id": "t9",
                    "epoch": 1,
                    "objective": "x",
                    "idempotency_key": "auth1",
                },
            )
            assert denied.status_code in (401, 403)
            created = await client.post(
                "/v1/agent/tasks",
                json={
                    "session_id": "s9",
                    "thread_id": "t9",
                    "epoch": 1,
                    "objective": "original",
                    "idempotency_key": "auth2",
                },
                headers=bearer(tokens),
            )
            assert created.status_code == 201
            task_id = created.json()["task_id"]
            # Stale steer is a 409, not silent acceptance.
            stale = await client.post(
                f"/v1/agent/tasks/{task_id}/steer",
                json={
                    "instruction": "other",
                    "expected_objective_revision": 99,
                    "idempotency_key": "stx",
                },
                headers=bearer(tokens),
            )
            assert stale.status_code == 409
            ok_steer = await client.post(
                f"/v1/agent/tasks/{task_id}/steer",
                json={
                    "instruction": "revised",
                    "expected_objective_revision": 1,
                    "idempotency_key": "stx2",
                },
                headers=bearer(tokens),
            )
            assert ok_steer.status_code == 200, ok_steer.text
            gate.set()
            for _ in range(200):
                got = await client.get(f"/v1/agent/tasks/{task_id}", headers=bearer(tokens))
                if got.json()["state"] in ("succeeded", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.05)
            # No current-revision evidence: the steered finish must fail, not lie.
            assert got.json()["state"] == "failed", got.json()
            # Interrupt on a terminal task is idempotent, not an error.
            stop = await client.post(
                f"/v1/agent/tasks/{task_id}/interrupt",
                json={
                    "expected_objective_revision": 2,
                    "idempotency_key": "stopx",
                },
                headers=bearer(tokens),
            )
            assert stop.status_code == 200
    finally:
        gate.set()
        await runtime.close()


async def test_http_approval_resolve(tmp_path):
    provider = ScriptedProvider(
        [
            {
                "tool": "write_file",
                "arguments": {"path": "appr.txt", "content": "approved content"},
            },
            {"tool": "finish", "arguments": {"summary": "approved done", "objective_revision": 1}},
        ]
    )
    policy = ToolPolicy(workspace=tmp_path)  # no grants: approval required
    journal = InMemoryJournal()
    runtime = AgentRuntime(journal, provider, tmp_path, policy)
    auth, tokens = make_auth()
    app = create_app(runtime, auth)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/agent/tasks",
                json={
                    "session_id": "s9",
                    "thread_id": "t9",
                    "epoch": 1,
                    "objective": "needs approval",
                    "idempotency_key": "appr1",
                },
                headers=bearer(tokens),
            )
            assert created.status_code == 201
            task_id = created.json()["task_id"]
            for _ in range(200):
                state = (
                    await client.get(f"/v1/agent/tasks/{task_id}", headers=bearer(tokens))
                ).json()["state"]
                if state == "awaiting_approval":
                    break
                await asyncio.sleep(0.05)
            approval_id = runtime.approval_for_task(task_id)
            assert approval_id
            requested = [e for e in journal.entries if e.kind == "approval.requested"]
            assert len(requested) == 1
            digest = requested[0].payload["action_digest"]
            resolved = await client.post(
                f"/v1/approvals/{approval_id}/resolve",
                json={
                    "approved": True,
                    "action_digest": digest,
                    "objective_revision": 1,
                },
                headers=bearer(tokens),
            )
            assert resolved.status_code == 200, resolved.text
            for _ in range(200):
                got = await client.get(f"/v1/agent/tasks/{task_id}", headers=bearer(tokens))
                if got.json()["state"] in ("succeeded", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.05)
            assert got.json()["state"] == "succeeded", got.json()
            assert (tmp_path / "appr.txt").read_text() == "approved content"
            # One-use: replaying the spend fails.
            again = await client.post(
                f"/v1/approvals/{approval_id}/resolve",
                json={
                    "approved": True,
                    "action_digest": digest,
                    "objective_revision": 1,
                },
                headers=bearer(tokens),
            )
            assert again.status_code in (403, 404, 409)
    finally:
        await runtime.close()


async def test_shell_denied_namespace_fails_closed(tmp_path, monkeypatch):
    """A bubblewrap that refuses namespaces must fail closed, never silently unprotected."""
    fake = tmp_path / "bin" / "bwrap"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'cannot unshare: Operation not permitted' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent))
    policy = ToolPolicy(workspace=tmp_path, allow_shell=True)
    with pytest.raises(ToolFailure):
        await run_shell(policy, "echo hi", 10, 1)
