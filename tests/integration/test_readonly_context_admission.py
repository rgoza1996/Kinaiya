"""Owner reference scopes are part of the exact Agent inference snapshot."""

import json

import httpx
import pytest
from test_agent_context_admission import generated

from core.policy import ToolPolicy
from packages.contracts.models import SessionRequest, TaskRequest
from packages.providers.text import MetaResponsesProvider
from services.agent import AgentRuntime
from services.session.store import LocalJournal, Store


@pytest.mark.parametrize("boundary", ["count", "generation"])
async def test_reference_scope_change_in_hook_refuses_old_request(tmp_path, boundary):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reference = workspace / "proof.txt"
    reference.write_text("Public synthetic proof")
    policy = ToolPolicy(workspace)
    calls, changed = [], False

    async def hook(request):
        nonlocal changed
        is_count = request.url.path.endswith("/input_tokens")
        if (boundary == "count" and is_count) or (boundary == "generation" and not is_count):
            policy.readonly_roots = (reference,)
            changed = True

    def send(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 123}
            )
        return generated(
            json.loads(request.content),
            {"tool": "read_file", "arguments": {"path": "proof.txt"}},
        )

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-fixture",
        allow_cloud=True,
        structured_actions=True,
        context_tokens=32768,
        transport=httpx.MockTransport(send),
    )
    provider.http.event_hooks["request"].append(hook)
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, policy)
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Inspect proof.txt",
                content_scope="public_synthetic",
                idempotency_key="readonly-scope-change",
            )
        )
        final = await runtime.wait(task.task_id, timeout=3)
        assert changed and final.state == "failed"
        assert calls == ([] if boundary == "count" else ["/v1/responses/input_tokens"])
        assert not final.evidence
        assert reference.read_text() == "Public synthetic proof"
    finally:
        await runtime.close()
        await provider.close()
        store.close()
