"""Real loopback HTTP recovery probe, synthetic providers, no Converse or paid inference.

Use a fresh owned --data-dir. The native journal is accessed through JournalClient.
An explicit fault fixture hides one committed retirement acknowledgment; this is
not a claim to have observed a spontaneous network outage.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import uvicorn

from core.policy import ToolPolicy
from packages.contracts.client import JournalClient
from packages.contracts.models import ProviderInfo, TaskRetirement
from packages.ecosystem.mcp_control import MCPController
from services.agent import AgentRuntime, create_app
from services.common.auth import Auth, load_or_create_tokens
from services.session.api import create_app as session_app
from services.session.store import Store


class SyntheticPendingProvider:
    info = ProviderInfo(provider="synthetic-fixture", model="pending-no-network", local=True)

    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.cancelled = False

    async def complete(self, messages):
        self.calls += 1
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class NoInferenceProvider:
    info = ProviderInfo(provider="synthetic-fixture", model="recovery-no-inference", local=True)

    def __init__(self):
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        raise AssertionError("Recovered work must never invoke inference")


class LostAckJournal(JournalClient):
    """Commit through real HTTP, then inject one missing acknowledgment."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.injected = False

    async def append(self, request):
        event = await super().append(request)
        if request.kind == "agent.retirement" and not self.injected:
            self.injected = True
            raise httpx.ReadError("Synthetic lost retirement acknowledgment")
        return event


async def start(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    job = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            return server, job
        if job.done():
            await job
            raise RuntimeError("Probe service exited before readiness")
        await asyncio.sleep(0.05)
    raise RuntimeError("Probe service did not become ready")


async def stop(server, job):
    server.should_exit = True
    await asyncio.wait_for(job, 10)


async def run(args):
    repository = Path(__file__).resolve().parents[1]
    root = args.data_dir.absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    tokens = load_or_create_tokens(root)
    auth = Auth(tokens)
    store = Store(root / "journal.sqlite")
    policy = ToolPolicy(workspace)
    session_url = f"http://127.0.0.1:{args.session_port}"
    agent_url = f"http://127.0.0.1:{args.agent_port}"
    session_server = await start(session_app(store, auth), args.session_port)
    agent_server = None
    runtime = None
    journals = []
    providers = []
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + tokens["owner"]}, timeout=20
        ) as owner:
            response = await owner.post(session_url + "/v1/sessions", json={"channel": "agent"})
            response.raise_for_status()
            session = response.json()
            pending = SyntheticPendingProvider()
            journal = JournalClient(session_url, tokens["agent"])
            journals.append(journal)
            runtime = AgentRuntime(journal, pending, workspace, policy)
            agent_server = await start(create_app(runtime, auth), args.agent_port)
            objective = "SYNTHETIC_OLD_CONTEXT_MUST_NOT_ENTER_NEW_EPOCH"
            response = await owner.post(
                agent_url + "/v1/agent/tasks",
                json={
                    "session_id": session["session_id"],
                    "thread_id": session["thread_id"],
                    "epoch": session["epoch"],
                    "objective": objective,
                    "idempotency_key": "synthetic-task",
                },
            )
            response.raise_for_status()
            task = response.json()
            await asyncio.wait_for(pending.entered.wait(), 2)
            await stop(*agent_server)
            agent_server = None
            await runtime.close()
            runtime = None
            assert pending.calls == 1 and pending.cancelled
            response = await owner.post(session_url + f"/v1/sessions/{session['session_id']}/reset")
            response.raise_for_status()
            assert response.json()["epoch"] == 2

            async def reopen(with_lost_ack=False):
                cls = LostAckJournal if with_lost_ack else JournalClient
                journal = cls(session_url, tokens["agent"])
                journals.append(journal)
                provider = NoInferenceProvider()
                providers.append(provider)
                controller = MCPController(root / "mcp", workspace, policy, initial_configs=[])
                current = AgentRuntime(
                    journal, provider, workspace, policy, mcp_controller=controller
                )
                await current.recover()
                return current, await start(create_app(current, auth), args.agent_port), journal

            runtime, agent_server, lost_ack = await reopen(with_lost_ack=True)
            stop_path = agent_url + f"/v1/agent/tasks/{task['task_id']}/interrupt"
            stop_body = {
                "expected_objective_revision": 1,
                "idempotency_key": "synthetic-owner-stop",
            }
            management_body = {
                "operation": "configure",
                "idempotency_key": "synthetic-configure",
                "expected_revision": 0,
                "reviewed": True,
                "config": {
                    "id": "disabled-synthetic",
                    "transport": "http",
                    "url": "http://127.0.0.1:1/mcp",
                    "allow_remote": True,
                    "allowed_tools": [],
                    "enabled": False,
                    "content_scope": "public_synthetic",
                },
            }
            before = await owner.post(agent_url + "/v1/mcp/control", json=management_body)
            assert before.status_code == 409
            first = await owner.post(stop_path, json=stop_body)
            assert first.status_code == 500 and lost_ack.injected
            paused = (await owner.get(agent_url + f"/v1/agent/tasks/{task['task_id']}")).json()
            assert paused["state"] == "paused"
            still_busy = await owner.post(agent_url + "/v1/mcp/control", json=management_body)
            assert still_busy.status_code == 409
            # A second reset cannot turn the lost acknowledgment into a duplicate receipt.
            response = await owner.post(session_url + f"/v1/sessions/{session['session_id']}/reset")
            response.raise_for_status()
            assert response.json()["epoch"] == 3
            retried = await owner.post(stop_path, json=stop_body)
            retried.raise_for_status()
            retired = retried.json()
            assert retired["state"] == "cancelled" and retired["epoch"] == 1
            assert "uncertain" in retired["summary"]
            configured = await owner.post(agent_url + "/v1/mcp/control", json=management_body)
            configured.raise_for_status()
            assert configured.json()["status"] == "configured"
            await stop(*agent_server)
            agent_server = None
            await runtime.close()
            runtime = None
            runtime, agent_server, _journal = await reopen()
            recovered = (await owner.get(agent_url + f"/v1/agent/tasks/{task['task_id']}")).json()
            assert recovered["state"] == "cancelled" and recovered["epoch"] == 1
            assert recovered["session_id"] == session["session_id"]
            duplicate = await owner.post(stop_path, json=stop_body)
            duplicate.raise_for_status()
            conflict = await owner.post(stop_path, json={**stop_body, "reason": "changed"})
            assert conflict.status_code == 409
            events = store.read(task_id=task["task_id"], kinds=["agent.retirement"], limit=200)
            assert len(events) == 1 and events[0].epoch == 2
            metadata = TaskRetirement.model_validate(events[0].payload)
            assert metadata.task_epoch == 1 and metadata.effect_status == "unreconciled"
            assert events[0].origin == "agent-supervisor" and not events[0].causes
            assert objective not in events[0].model_dump_json()
            assert not [
                event
                for event in store.read(task_id=task["task_id"], limit=200)
                if event.epoch > 1 and event.kind == "agent.outcome"
            ]
            assert all(provider.calls == 0 for provider in providers)
            snapshot = (await owner.get(agent_url + "/v1/mcp/management")).json()
            assert not snapshot["blocked"] and len(snapshot["servers"]) == 1
            assert snapshot["servers"][0]["enabled"] is False
            report = {
                "synthetic_only": True,
                "converse_constructed": False,
                "external_model_calls": 0,
                "initial_pending_provider_fixture_calls": pending.calls,
                "initial_worker_cancelled": pending.cancelled,
                "recovered_provider_fixture_calls": [provider.calls for provider in providers],
                "journal_transport": "real authenticated loopback HTTP JournalClient",
                "task_original_epoch": task["epoch"],
                "current_session_epoch": store.session(session["session_id"]).epoch,
                "lost_ack_fixture_first_http_status": first.status_code,
                "management_before_retirement_http_status": before.status_code,
                "management_during_unknown_ack_http_status": still_busy.status_code,
                "management_after_retirement": configured.json(),
                "retirement_receipt_count": len(events),
                "retirement_receipt_epoch": events[0].epoch,
                "retirement_payload": metadata.model_dump(mode="json"),
                "recovered_state": recovered["state"],
                "recovered_task_epoch": recovered["epoch"],
                "conflicting_key_after_reopen_http_status": conflict.status_code,
                "new_epoch_execution_outcome_count": 0,
                "repository_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repository, text=True
                ).strip(),
                "working_tree_clean": not bool(
                    subprocess.check_output(
                        ["git", "status", "--porcelain"], cwd=repository, text=True
                    ).strip()
                ),
                "source_sha256": {
                    name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
                    for name in [
                        "services/agent/runtime.py",
                        "packages/contracts/models.py",
                        "services/session/store.py",
                        "scripts/verify_task_retirement.py",
                    ]
                },
            }
            path = root / "evidence.json"
            path.write_text(json.dumps(report, indent=2) + "\n")
            print(
                json.dumps(
                    {"evidence": str(path), "state": recovered["state"], "receipts": len(events)}
                )
            )
    finally:
        if agent_server is not None:
            await stop(*agent_server)
        if runtime is not None:
            await runtime.close()
        for journal in journals:
            await journal.close()
        await stop(*session_server)
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--session-port", type=int, default=8880)
    parser.add_argument("--agent-port", type=int, default=8881)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
