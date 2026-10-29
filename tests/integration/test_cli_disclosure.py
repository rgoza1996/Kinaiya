"""The native direct-Agent CLI carries explicit grants without opening a socket."""

import json
from types import SimpleNamespace

import httpx
import pytest

from services import launcher


@pytest.mark.parametrize("objective", ["Synthetic objective", "/status"])
@pytest.mark.parametrize(
    "scope", [None, "local_only", "public_synthetic", "private_cloud_authorized"]
)
async def test_cli_preserves_input_scope_for_task_and_deterministic_commands(
    tmp_path, monkeypatch, objective, scope
):
    bodies = []

    def transport(request):
        body = json.loads(request.content)
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={
                    "session_id": "synthetic-session",
                    "thread_id": "synthetic-thread",
                    "epoch": 1,
                },
            )
        bodies.append((request.url.path, body))
        response = (
            {"status": "ok"}
            if objective.startswith("/")
            else {"task_id": "synthetic-task", "state": "succeeded"}
        )
        return httpx.Response(200, json=response)

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        launcher,
        "load_config",
        lambda _: {"session_url": "http://synthetic", "agent_url": "http://synthetic"},
    )
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: {"owner": "synthetic-owner"})
    monkeypatch.setattr(
        launcher.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(transport), **kwargs),
    )
    args = SimpleNamespace(config=None, data_dir=tmp_path, objective=objective)
    if scope is not None:
        args.content_scope = scope
    assert await launcher.direct_agent(args) == 0
    assert len(bodies) == 1
    endpoint, body = bodies[0]
    assert endpoint == ("/v1/commands/execute" if objective.startswith("/") else "/v1/agent/tasks")
    assert body["content_scope"] == (scope or "local_only")
