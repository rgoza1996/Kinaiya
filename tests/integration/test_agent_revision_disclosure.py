"""Independent issue95 review: only synthetic journals and a recording provider."""

import json

import pytest

from core.policy import ToolPolicy
from packages.contracts.models import AppendRequest, ProviderInfo, SessionRequest, TaskRequest
from services.agent import AgentRuntime
from services.session.store import LocalJournal, Store


class RecordingProvider:
    info = ProviderInfo(provider="meta", model="muse-spark-1.3-contributor", local=False)

    def __init__(self, arguments):
        self.calls = []
        self.responses = [
            {"tool": "read_transcript", "arguments": arguments},
            {"tool": "read_file", "arguments": {"path": "public.txt"}},
            {"tool": "finish", "arguments": {"objective_revision": 1}},
        ]

    async def complete(self, messages):
        self.calls.append(messages)
        return json.dumps(self.responses.pop(0))


@pytest.mark.parametrize("retrieval", ["event_id", "scan"])
@pytest.mark.parametrize("restricted_at", ["original", "middle", "public_control"])
async def test_revision_rows_keep_target_scope_floor(tmp_path, retrieval, restricted_at):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "public.txt").write_text("SYNTHETIC PUBLIC FILE")
    store = Store(tmp_path / "synthetic.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))

    def append(kind, payload, key):
        return store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind=kind,
                payload=payload,
                idempotency_key=key,
            ),
            "owner",
        )

    append(
        "conversation.user",
        {
            "text": "SYNTHETIC UNRELATED LOCAL",
            "final": True,
            "revision": 1,
            "content_scope": "local_only",
        },
        "unrelated",
    )
    original = append(
        "conversation.user",
        {
            "text": "SYNTHETIC ORIGINAL",
            "final": True,
            "revision": 1,
            "content_scope": "local_only" if restricted_at == "original" else "public_synthetic",
        },
        "original",
    )
    append(
        "conversation.revision",
        {
            "target_event_id": original.event_id,
            "text": "SYNTHETIC MIDDLE",
            "revision": 2,
            "content_scope": "local_only" if restricted_at == "middle" else "public_synthetic",
        },
        "middle",
    )
    latest = append(
        "conversation.revision",
        {
            "target_event_id": original.event_id,
            "text": "SYNTHETIC REVISION MARKER",
            "revision": 3,
            "content_scope": "public_synthetic",
        },
        "latest",
    )
    arguments = {"event_ids": [latest.event_id]} if retrieval == "event_id" else {"limit": 20}
    provider = RecordingProvider(arguments)
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace))
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Read public.txt",
                content_scope="public_synthetic",
                idempotency_key="task",
            )
        )
        await runtime.wait(task.task_id, timeout=2)
        compiled = json.dumps(provider.calls)
        assert "SYNTHETIC UNRELATED LOCAL" not in compiled
        if restricted_at == "public_control":
            assert "SYNTHETIC REVISION MARKER" in compiled
        else:
            assert "SYNTHETIC REVISION MARKER" not in compiled
    finally:
        await runtime.close()
        store.close()
