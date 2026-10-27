"""Real isolated selectors behind authenticated owner controls and task pins."""

import json
import shutil

import httpx
import pytest

from core.extensions import Registry
from core.policy import ToolPolicy
from packages.contracts.models import SessionRequest, TaskRequest
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.agent.context import select_context
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


@pytest.fixture
async def environment(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("alpha")
    (workspace / "b.txt").write_text("beta")
    suite = tmp_path / "heldout.json"
    suite.write_text(
        json.dumps(
            {
                "suite_version": "fixture",
                "cases": [
                    {
                        "case_id": "alpha",
                        "payload": {
                            "query": "alpha",
                            "files": [{"id": "a", "path": "a", "text": "alpha"}],
                            "max_chars": 5,
                        },
                        "expected_ids": ["a"],
                    }
                ],
            }
        )
    )
    reg = Registry(tmp_path / "protected-registry", suite)
    provider = ScriptedProvider(
        [
            json.dumps({"tool": "select_context", "arguments": {"max_chars": 5}}),
            json.dumps(
                {
                    "tool": "finish",
                    "arguments": {"objective_revision": 1, "summary": "Located reference"},
                }
            ),
        ]
    )
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace),
        selector_registry=reg,
    )
    tokens = {
        r: r + "-fixture-token-0123456789abcdef"
        for r in ("owner", "agent", "converse", "decisions", "resources")
    }
    app = create_app(runtime, Auth(tokens))
    yield store, session, workspace, reg, runtime, app, tokens
    await runtime.close()
    store.close()


def need_isolation():
    if shutil.which("bwrap") is None:
        pytest.skip("Real Linux bubblewrap unavailable; no subprocess fallback")


async def test_task_uses_pinned_version_and_context_is_only_reference_data(environment):
    need_isolation()
    store, s, workspace, reg, runtime, app, tokens = environment
    task = await runtime.submit(
        TaskRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=1,
            objective="Locate alpha",
            idempotency_key="context",
        )
    )
    pin = task.harness_version
    final = await runtime.wait(task.task_id)
    assert final.state == "succeeded" and final.harness_version == pin == reg.active
    assert all(e.kind == "read" for e in final.evidence)
    assert not runtime.policy.yolo
    # The outer model receives bounded selected references; it is not sent the scanned file bodies.
    messages = runtime.provider.calls[-1]
    observations = "\n".join(
        m["content"] for m in messages if "untrusted_reference_context" in m["content"]
    )
    assert "a.txt" in observations and '"text":' not in observations
    assert "selector_version" in observations


async def test_harness_activation_requires_owner_and_bound_passing_report(environment):
    need_isolation()
    store, s, workspace, reg, runtime, app, tokens = environment
    source = workspace / "selector.py"
    source.write_text("def select_context(p):\n    return [p['files'][0]['id']]\n")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        path = "/v1/harness/register"
        body = {"path": "selector.py", "manifest": {"name": "owner-reviewed"}}
        denied = await client.post(
            path, json=body, headers={"Authorization": "Bearer " + tokens["decisions"]}
        )
        assert denied.status_code == 403
        client.headers["Authorization"] = "Bearer " + tokens["owner"]
        result = await client.post(path, json=body)
        assert result.status_code == 200
        version = result.json()["version_id"]
        old = reg.active
        report = await client.post("/v1/harness/evaluate", json={"version_id": version})
        assert report.json()["passed"] is True
        promotion = {"version_id": version, "report_id": report.json()["report_id"]}
        assert (await client.post("/v1/harness/promote", json=promotion)).status_code == 409
        promotion["reviewed"] = True
        assert (await client.post("/v1/harness/promote", json=promotion)).json()[
            "active"
        ] == version
        assert (await client.post("/v1/harness/rollback")).json()["active"] == old
        # Registration cannot use owner service identity to read beyond worker scope.
        source.unlink()
        source.symlink_to(reg.root / "active.json")
        assert (await client.post(path, json=body)).status_code == 400


async def test_context_scan_never_reads_a_symlink_outside_scope(environment, tmp_path):
    need_isolation()
    store, s, workspace, reg, runtime, app, tokens = environment
    (tmp_path / "private.txt").write_text("secret fixture")
    (workspace / "escape.txt").symlink_to(tmp_path / "private.txt")
    task = await runtime.submit(
        TaskRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=1,
            objective="Locate alpha",
            idempotency_key="scope",
        )
    )
    await runtime.wait(task.task_id)
    out, _ = await select_context(runtime, task, {"max_chars": 100})
    assert "escape" not in out and "secret fixture" not in out
