"""Join the actual submit client, authenticated APIs, SQLite and Agent lifecycle.

Only the text provider is scripted. Tools and journal writes are real and local;
the transport faults interrupt the actual Session response boundary.
"""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_agent_submit_admission import AUTH, TOKENS, headers

from core.policy import ToolPolicy
from packages.contracts.client import JournalClient
from packages.contracts.models import SessionRequest, TaskState
from packages.providers.text import ScriptedProvider
from services.agent.app import create_app as agent_app
from services.agent.runtime import AgentRuntime
from services.session.api import create_app as session_app
from services.session.store import Store

ADMIT = "/v1/transcript/agent-submits"
UNCONFIRMED = {"detail": "Agent submission persistence is unconfirmed"}
FILE_TEXT = "Public synthetic native HTTP admission evidence.\n"


class SessionResponseFault(httpx.AsyncBaseTransport):
    """Real ASGI/SQLite admission, with one selected transport-boundary fault."""

    def __init__(self, app, mode):
        self.delegate = httpx.ASGITransport(app=app)
        self.mode = mode
        self.fired = False
        self.admissions = []
        self.generic_submits = []

    async def handle_async_request(self, request):
        raw = json.loads(await request.aread()) if request.method == "POST" else None
        if request.url.path == "/v1/transcript/entries" and raw["payload"].get("op") == "submit":
            self.generic_submits.append(raw)
        admission = request.url.path == ADMIT
        if admission:
            self.admissions.append(raw)
        fault = admission and self.mode is not None and not self.fired
        if fault and self.mode == "before_commit":
            self.fired = True
            raise httpx.ConnectError("Public synthetic connection failure", request=request)
        response = await self.delegate.handle_async_request(request)
        if fault:
            self.fired = True
            assert response.status_code == 200
            if self.mode == "lost_ack":
                await response.aclose()
                raise httpx.ReadError("Public synthetic lost acknowledgement", request=request)
            assert self.mode == "malformed_ack"
            body = json.loads(await response.aread())
            await response.aclose()
            body["entry"]["origin"] = "untrusted-response"
            return httpx.Response(200, json=body, request=request)
        return response

    async def aclose(self):
        await self.delegate.aclose()


@asynccontextmanager
async def stack(tmp_path, *, channel="agent", mode=None):
    assert callable(getattr(JournalClient, "admit_submit", None)), (
        "Native submit transport is absent (capability baseline)"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel=channel))
    transport = SessionResponseFault(session_app(store, AUTH), mode)
    journal = JournalClient("http://session.test", TOKENS["agent"], transport=transport)
    provider = ScriptedProvider(
        [
            json.dumps(
                {"tool": "write_file", "arguments": {"path": "proof.txt", "content": FILE_TEXT}}
            ),
            json.dumps(
                {"tool": "finish", "arguments": {"summary": "Written.", "objective_revision": 1}}
            ),
        ]
    )
    runtime = AgentRuntime(journal, provider, workspace, ToolPolicy(workspace, allow_write=True))
    body = {
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "epoch": session.epoch,
        "objective": "Write the public synthetic admission proof file.",
        "idempotency_key": "synthetic-native-http-stack",
        "content_scope": "local_only",
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent_app(runtime, AUTH)),
            base_url="http://agent.test",
        ) as client:
            yield SimpleNamespace(
                client=client,
                body=body,
                runtime=runtime,
                provider=provider,
                store=store,
                transport=transport,
                workspace=workspace,
            )
    finally:
        await asyncio.wait_for(runtime.close(), timeout=3)
        await asyncio.wait_for(journal.close(), timeout=3)
        store.close()


def submit_entries(store):
    return [
        entry
        for entry in store.read(kinds=["agent.command"])
        if entry.payload.get("op") == "submit"
    ]


@pytest.mark.parametrize("channel", ["agent", "converse"])
async def test_http_native_created_runs_once_with_real_file_and_outcome(tmp_path, channel):
    async with stack(tmp_path, channel=channel) as env:
        response = await env.client.post("/v1/agent/tasks", json=env.body, headers=headers("owner"))
        assert response.status_code == 201, response.text
        task_id = response.json()["task_id"]
        final = await asyncio.wait_for(env.runtime.wait(task_id, timeout=4), timeout=5)
        assert final.state == TaskState.SUCCEEDED, final.error
        assert (env.workspace / "proof.txt").read_text() == FILE_TEXT
        assert len(env.provider.calls) == 2
        assert len(final.evidence) == 1
        assert final.evidence[0].sha256 == hashlib.sha256(FILE_TEXT.encode()).hexdigest()
        native = submit_entries(env.store)
        assert len(native) == 1 and native[0].payload["task_id"] == task_id
        outcomes = env.store.read(kinds=["agent.outcome"], task_id=task_id)
        assert len(outcomes) == 1
        assert outcomes[0].payload["task"]["state"] == "succeeded"
        retry = await env.client.post("/v1/agent/tasks", json=env.body, headers=headers("owner"))
        assert retry.status_code == 201
        assert retry.json()["task_id"] == task_id and retry.json()["state"] == "succeeded"
        assert len(env.provider.calls) == 2 and len(env.transport.admissions) == 1
        assert env.transport.generic_submits == []


@pytest.mark.parametrize("mode", ["before_commit", "lost_ack", "malformed_ack"])
async def test_http_submit_uncertainty_never_resubmits_or_launches_work(tmp_path, mode):
    async with stack(tmp_path, mode=mode) as env:
        first = await env.client.post("/v1/agent/tasks", json=env.body, headers=headers("owner"))
        assert env.transport.fired, "The actual native admission did not reach its fault boundary"
        assert first.status_code == 503 and first.json() == UNCONFIRMED
        native = submit_entries(env.store)
        assert len(native) == (mode != "before_commit")
        retry = await env.client.post("/v1/agent/tasks", json=env.body, headers=headers("owner"))
        if mode == "before_commit":
            assert retry.status_code == 503 and retry.json() == UNCONFIRMED
        else:
            assert retry.status_code == 201, retry.text
            assert retry.json()["state"] == "paused"
            assert retry.json()["task_id"] == native[0].payload["task_id"]
        visible = await env.client.get("/v1/agent/tasks", headers=headers("owner"))
        assert visible.status_code == 200
        assert all(task["state"] == "paused" for task in visible.json())
        assert env.provider.calls == [] and list(env.workspace.iterdir()) == []
        assert env.transport.generic_submits == [] and len(env.transport.admissions) == 1
        assert submit_entries(env.store) == native
        assert env.store.read(kinds=["agent.outcome"]) == []
