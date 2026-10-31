"""Synthetic project metadata and recorded output provenance; no model/device calls."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from core.policy import ToolPolicy
from core.supervision import file_evidence
from packages.commands import Registry
from packages.contracts.models import AppendRequest, JournalEntry, SessionRequest
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.agent.library_api import output_record
from services.agent.projects import ProjectChange, ProjectConflict, Projects
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentRuntime(
        LocalJournal(store, "agent"), ScriptedProvider([]), workspace, ToolPolicy(workspace)
    )
    runtime.projects = Projects(tmp_path / "projects.sqlite")
    commands = Registry(runtime, LocalJournal(store, "owner"))
    tokens = {
        role: role[0] * 32 for role in ["owner", "agent", "converse", "decisions", "resources"]
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, Auth(tokens), commands)),
        base_url="http://synthetic",
        headers={"Authorization": "Bearer " + tokens["owner"]},
    ) as client:
        yield SimpleNamespace(
            client=client, store=store, workspace=workspace, runtime=runtime, tokens=tokens
        )
    await runtime.close()
    commands.close()
    store.close()


def change(**kwargs):
    return ProjectChange(schema_version="1.0", **kwargs)


def test_project_receipt_survives_restart_and_conflicts_refuse(tmp_path):
    path = tmp_path / "projects.sqlite"
    projects = Projects(path)
    create = change(operation="create", name="Public spreadsheets", idempotency_key="create")
    receipt = projects.change(create)
    assert receipt["project"]["revision"] == 1
    project_id = receipt["project"]["project_id"]
    projects.close()
    projects = Projects(path)
    assert projects.change(create) == receipt
    assert len(projects.page()["items"]) == 1
    with pytest.raises(ProjectConflict):
        projects.change(change(operation="create", name="Other", idempotency_key="create"))
    attach = change(
        operation="attach",
        project_id=project_id,
        expected_revision=1,
        session_id="synthetic-agent-session",
        idempotency_key="attach",
    )
    assert projects.change(attach)["project"]["session_ids"] == ["synthetic-agent-session"]
    assert projects.change(attach)["project"]["revision"] == 2
    with pytest.raises(ProjectConflict):
        projects.change(attach.model_copy(update={"idempotency_key": "stale"}))
    assert projects.get(project_id)["revision"] == 2
    projects.close()
    assert path.stat().st_mode & 0o777 == 0o600


async def test_projects_owner_only_and_native_session_association(env):
    session = env.store.create_session(SessionRequest(channel="agent"))
    converse = env.store.create_session(SessionRequest(channel="converse"))
    body = {
        "schema_version": "1.0",
        "operation": "create",
        "name": "Public project",
        "idempotency_key": "new",
        "session_id": session.session_id,
    }
    assert (
        await env.client.post(
            "/v1/agent/projects",
            json=body,
            headers={"Authorization": "Bearer " + env.tokens["agent"]},
        )
    ).status_code == 403
    denied = await env.client.post(
        "/v1/agent/projects", json={**body, "session_id": converse.session_id}
    )
    assert denied.status_code == 400
    receipt = (await env.client.post("/v1/agent/projects", json=body)).json()
    assert receipt["project"]["session_ids"] == [session.session_id]
    assert (await env.client.get("/v1/agent/projects/changes/new")).json() == receipt
    assert (await env.client.post("/v1/agent/projects", json=body)).json() == receipt
    env.store.delete(session.session_id)
    assert (await env.client.post("/v1/agent/projects", json=body)).json() == receipt
    assert len((await env.client.get("/v1/agent/projects")).json()["items"]) == 1


async def test_library_only_lists_native_successful_writes_and_never_scans(env):
    session = env.store.create_session(SessionRequest(channel="agent"))
    (env.workspace / "unrecorded.md").write_text("not generated")
    for index, (name, ok, tool) in enumerate(
        [
            ("report.md", True, "write_file"),
            ("art.svg", True, "write_file"),
            ("failed.csv", False, "write_file"),
            ("read-only.pdf", True, "read_file"),
            ("source.py", True, "write_file"),
        ]
    ):
        evidence = file_evidence(env.workspace / name, b"synthetic", 1, "synthetic output")
        env.store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="agent.tool",
                task_refs=["synthetic-task"],
                idempotency_key=str(index),
                payload={"tool": tool, "ok": ok, "evidence": evidence.model_dump(mode="json")},
            ),
            "agent",
        )
    before = set(env.workspace.iterdir())
    documents = (await env.client.get("/v1/agent/library?kind=documents")).json()
    assert [row["name"] for row in documents["items"]] == ["report.md"]
    row = documents["items"][0]
    assert row["file_type"] == "MD" and row["relative_path"] == "report.md"
    assert row["created_at"] and row["task_id"] == "synthetic-task"
    assert "output" not in row and "summary" not in row
    media = (await env.client.get("/v1/agent/library?kind=media")).json()
    assert [row["name"] for row in media["items"]] == ["art.svg"]
    assert set(env.workspace.iterdir()) == before
    assert (
        await env.client.get(f"/v1/agent/library?kind=documents&after={documents['next_cursor']}")
    ).json()["items"] == []
    assert (
        await env.client.get(
            "/v1/agent/library?kind=documents",
            headers={"Authorization": "Bearer " + env.tokens["converse"]},
        )
    ).status_code == 403


def test_library_rejects_foreign_and_traversing_receipts(tmp_path):
    workspace = tmp_path / "workspace"
    for path in [tmp_path / "foreign.md", workspace / ".." / "escape.md"]:
        evidence = file_evidence(path, b"public", 1, "synthetic")
        entry = JournalEntry(
            session_id="s",
            thread_id="t",
            epoch=1,
            sequence=1,
            origin="agent-supervisor",
            kind="agent.tool",
            idempotency_key="x",
            task_refs=["task"],
            payload={
                "tool": "write_file",
                "ok": True,
                "evidence": evidence.model_dump(mode="json"),
            },
        )
        assert output_record(entry, workspace, "documents") is None
    assert not Path(workspace).exists()
