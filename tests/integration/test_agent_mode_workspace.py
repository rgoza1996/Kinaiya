"""Native steering must preserve captured workspace authority at each await."""

import asyncio

import httpx
import pytest
from test_agent_mode_permissions import AUTH, TOKENS, bind_workspace

from core.policy import ToolPolicy
from packages.contracts.client import JournalClient
from packages.contracts.models import SessionRequest
from packages.providers.text import ScriptedProvider
from services.agent.app import create_app as agent_app
from services.agent.runtime import AgentRuntime
from services.agent.workspace_settings import WorkspaceUnavailable
from services.session.api import create_app as session_app
from services.session.store import Store


class HeldModel(ScriptedProvider):
    def __init__(self):
        super().__init__([])
        self.entered = asyncio.Event()

    async def complete(self, messages):
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("The held synthetic model must never produce a tool proposal")


class SteerBoundary(httpx.AsyncBaseTransport):
    def __init__(self, app, phase):
        self.delegate = httpx.ASGITransport(app=app)
        self.phase = phase
        self.armed = self.fired = False
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def handle_async_request(self, request):
        selected = self.armed and not self.fired
        if (
            selected
            and self.phase == "before_backend"
            and request.url.path == "/v1/agent-control/state"
        ):
            self.fired = True
            self.entered.set()
            await self.release.wait()
        response = await self.delegate.handle_async_request(request)
        if (
            selected
            and self.phase == "after_admit"
            and request.url.path == "/v1/transcript/agent-steers"
        ):
            assert response.status_code == 200
            self.fired = True
            self.entered.set()
            await self.release.wait()
        return response

    async def aclose(self):
        await self.delegate.aclose()


@pytest.mark.parametrize("phase", ["unchanged", "before_backend", "after_admit"])
async def test_workspace_displacement_never_installs_a_new_steering_goal(tmp_path, phase):
    workspace, displaced = tmp_path / "workspace", tmp_path / "displaced"
    workspace.mkdir()
    store = Store(tmp_path / "session.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    surface = session_app(store, AUTH)
    boundary = SteerBoundary(surface, phase)
    journal = JournalClient("http://session.test", TOKENS["agent"], transport=boundary)
    provider = HeldModel()
    runtime = AgentRuntime(journal, provider, workspace, ToolPolicy(workspace, allow_write=True))
    owner = bind_workspace(runtime, tmp_path / "control")
    operation = None
    try:
        async with (
            surface.router.lifespan_context(surface),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=agent_app(runtime, AUTH)),
                base_url="http://agent.test",
                headers={"Authorization": f"Bearer {TOKENS['owner']}"},
            ) as client,
        ):
            created = await client.post(
                "/v1/agent/tasks",
                json={
                    "session_id": session.session_id,
                    "thread_id": session.thread_id,
                    "epoch": session.epoch,
                    "objective": "Wait for the public synthetic test to finish.",
                    "content_scope": "public_synthetic",
                    "idempotency_key": "workspace-original-task",
                },
            )
            assert created.status_code == 201, created.text
            task_id = created.json()["task_id"]
            await asyncio.wait_for(provider.entered.wait(), 3)
            record = runtime._tasks[task_id]
            original_objective = record.task.objective
            assert record.workspace_context is owner.active_context()
            assert record.task.workspace_pin == record.workspace_context.pin
            assert record.pending_steer is None and not record.resume.is_set()
            boundary.armed = True
            operation = asyncio.create_task(
                client.post(
                    f"/v1/agent/tasks/{task_id}/steer",
                    json={
                        "instruction": "New public synthetic instruction.",
                        "expected_objective_revision": 1,
                        "idempotency_key": "workspace-new-steer",
                        "content_scope": "public_synthetic",
                        "source_event_ids": [],
                    },
                )
            )
            if phase != "unchanged":
                await asyncio.wait_for(boundary.entered.wait(), 3)
                workspace.rename(displaced)
                workspace.mkdir()
                assert workspace.stat().st_ino != displaced.stat().st_ino
                with pytest.raises(WorkspaceUnavailable):
                    owner.active_context()
                boundary.release.set()
            response = await asyncio.wait_for(operation, 3)
            if phase == "unchanged":
                assert response.status_code == 200, response.text
                assert record.pending_steer["instruction"] == "New public synthetic instruction."
                assert record.resume.is_set()
            else:
                assert response.status_code == 403, (response.text, record.pending_steer)
                assert record.pending_steer is None and not record.resume.is_set()
                assert record.task.objective == original_objective
                assert record.task.objective_revision == 1
            intents = store.db.execute("SELECT count(*) FROM agent_steer_intents").fetchone()[0]
            assert intents == (0 if phase == "before_backend" else 1)
            assert not list(workspace.iterdir())
    finally:
        boundary.release.set()
        if operation is not None and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        if displaced.exists():
            workspace.rmdir()
            displaced.rename(workspace)
        await asyncio.wait_for(runtime.close(), 3)
        await asyncio.wait_for(journal.close(), 3)
        store.close()
