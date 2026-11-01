"""Combined mode/permission/approval behavior with real native effect owners.

Only model output and the first permit's TTL are fixtures. Session/Agent APIs,
SQLite, scoped permission storage, approval identities and file effects are real.
No Converse runtime or paid inference is constructed.
"""

import asyncio
import hashlib
import json

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.client import JournalClient
from packages.contracts.models import SessionRequest
from packages.providers.text import ScriptedProvider
from services.agent.app import create_app as agent_app
from services.agent.runtime import AgentRuntime
from services.agent.tool_permissions import AgentToolPermissions
from services.common.auth import Auth
from services.session.api import create_app as session_app
from services.session.store import Store

TOKENS = dict(zip(("owner", "agent", "converse", "decisions", "resources"), "oacdr", strict=True))
TOKENS = {role: value * 32 for role, value in TOKENS.items()}
AUTH = Auth(TOKENS)
PERMISSIONS = "/v1/agent/options/tool-permissions"
TEXT = "Public synthetic mode and permission integration.\n"


class ExpireFirstPermit(ToolPolicy):
    """Use the real broker's supported TTL input, then its normal renewal TTL."""

    def create_approval(self, *args, **kwargs):
        if not self._approvals:
            kwargs["ttl_s"] = -1
        return super().create_approval(*args, **kwargs)


async def wait_for(client, path, predicate):
    async with asyncio.timeout(5):
        while True:
            response = await client.get(path)
            assert response.status_code == 200, response.text
            value = response.json()
            if predicate(value):
                return value
            await asyncio.sleep(0.01)


async def save_permission(client, mode, key):
    before = await client.get(PERMISSIONS)
    assert before.status_code == 200, before.text
    state = before.json()
    response = await client.post(
        PERMISSIONS,
        json={
            "schema_version": "1.0",
            "expected_revision": state["revision"],
            "expected_state": state["state_token"],
            "idempotency_key": key,
            "scope": {"kind": "default"},
            "tool": "write_file",
            "mode": mode,
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("decision", ["approve", "expire_then_approve", "deny_at_next_action"])
async def test_off_keeps_native_permissions_renewal_and_approval_authoritative(tmp_path, decision):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "session.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    surface = session_app(store, AUTH)
    policy_type = ExpireFirstPermit if decision == "expire_then_approve" else ToolPolicy
    provider = ScriptedProvider(
        [
            json.dumps({"tool": "write_file", "arguments": {"path": "proof.txt", "content": TEXT}}),
            json.dumps(
                {"tool": "finish", "arguments": {"summary": "Verified.", "objective_revision": 1}}
            ),
        ]
    )
    owner = {"Authorization": f"Bearer {TOKENS['owner']}"}
    journal = JournalClient(
        "http://session.test", TOKENS["agent"], transport=httpx.ASGITransport(app=surface)
    )
    runtime = AgentRuntime(journal, provider, workspace, policy_type(workspace, allow_write=True))
    runtime.tool_permissions = AgentToolPermissions(runtime, path=tmp_path / "permissions.sqlite")
    try:
        async with (
            surface.router.lifespan_context(surface),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=surface),
                base_url="http://session.test",
                headers=owner,
            ) as session_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=agent_app(runtime, AUTH)),
                base_url="http://agent.test",
                headers=owner,
            ) as client,
        ):
            await save_permission(client, "ask", "mode-integration-ask")
            request = {
                "schema_version": "1.0",
                "session_id": session.session_id,
                "thread_id": session.thread_id,
                "epoch": session.epoch,
                "objective": "Write the public synthetic verification file.",
                "content_scope": "public_synthetic",
                "idempotency_key": "mode-permission-task",
            }
            accepted = await client.post("/v1/agent/tasks", json=request)
            assert accepted.status_code == 201, accepted.text
            task_id = accepted.json()["task_id"]
            page = await wait_for(client, "/v1/approvals", lambda value: bool(value["approvals"]))
            approval = page["approvals"][0]
            assert approval["task_id"] == task_id and approval["tool"] == "write_file"
            assert not (workspace / "proof.txt").exists() and len(provider.calls) == 1

            observed = await session_client.get("/v1/agent-control/state")
            assert observed.status_code == 200, observed.text
            state = observed.json()
            off = await session_client.post(
                "/v1/agent-control/changes",
                json={
                    "schema_version": "1.0",
                    "idempotency_key": "mode-permission-off",
                    "expected_backend": state["backend_id"],
                    "expected_instance": state["instance_id"],
                    "expected_revision": state["revision"],
                    "mode": "off",
                    "active_work_policy": "finish_already_accepted",
                },
            )
            assert off.status_code == 200, off.text
            new = await client.post(
                "/v1/agent/tasks", json={**request, "idempotency_key": "mode-refused-new-task"}
            )
            assert new.status_code == 403, new.text
            assert len(await runtime.status(None)) == 1 and not (workspace / "proof.txt").exists()
            identity = {
                "action_digest": approval["action_digest"],
                "objective_revision": approval["objective_revision"],
            }
            if decision == "expire_then_approve":
                assert approval["status"] == "expired"
                expired = await client.post(
                    f"/v1/approvals/{approval['approval_id']}/resolve",
                    json={**identity, "approved": True},
                )
                assert expired.status_code == 403 and not (workspace / "proof.txt").exists()
                renewed = await client.post(
                    f"/v1/approvals/{approval['approval_id']}/reissue", json=identity
                )
                assert renewed.status_code == 200, renewed.text
                replacement = renewed.json()
                assert replacement["approval_id"] != approval["approval_id"]
                assert replacement["replacement_status"] == "pending"
                assert not (workspace / "proof.txt").exists() and len(provider.calls) == 1
                approval = {**approval, "approval_id": replacement["approval_id"]}
            if decision == "deny_at_next_action":
                await save_permission(client, "deny", "mode-integration-deny")
            result = await client.post(
                f"/v1/approvals/{approval['approval_id']}/resolve",
                json={**identity, "approved": True},
            )
            if decision == "deny_at_next_action":
                assert result.status_code == 403 and not (workspace / "proof.txt").exists()
                stopped = await client.post(
                    f"/v1/agent/tasks/{task_id}/interrupt",
                    json={
                        "expected_objective_revision": 1,
                        "idempotency_key": "mode-permission-stop",
                        "reason": "Owner stops the still unauthorized action.",
                    },
                )
                assert stopped.status_code == 200, stopped.text
                terminal = await wait_for(
                    client,
                    f"/v1/agent/tasks/{task_id}",
                    lambda value: value["state"] == "cancelled",
                )
                assert not (workspace / "proof.txt").exists() and len(provider.calls) == 1
                assert terminal["objective_revision"] == 1
            else:
                assert result.status_code == 200, result.text
                terminal = await wait_for(
                    client,
                    f"/v1/agent/tasks/{task_id}",
                    lambda value: value["state"] == "succeeded",
                )
                assert (workspace / "proof.txt").read_text() == TEXT and len(provider.calls) == 2
                digest = hashlib.sha256(TEXT.encode()).hexdigest()
                assert any(
                    item["kind"] == "file" and item["sha256"] == digest
                    for item in terminal["evidence"]
                )
                assert runtime.policy.approval_state(approval["approval_id"]) == "used"
            after = await session_client.get("/v1/agent-control/state")
            assert after.status_code == 200 and after.json()["mode"] == "off"
    finally:
        await asyncio.wait_for(runtime.close(), 3)
        await asyncio.wait_for(journal.close(), 3)
        store.close()
