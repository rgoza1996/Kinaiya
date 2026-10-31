"""Real controller, journal and filesystem behind offline scripted inference."""

import asyncio
import hashlib
import json

import httpx
import pytest

from core.policy import ToolPolicy
from packages.commands import Registry
from packages.contracts.models import (
    CommandRequest,
    InterruptRequest,
    ProviderInfo,
    SessionRequest,
    TaskRequest,
)
from packages.providers.completion_budget import PreparedComplete
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.agent.skill_service import SkillService, SkillServiceError
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


def package(
    path, *, name="recipe", extra="", body="Write proof.txt containing exactly public proof."
):
    path.mkdir()
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Synthetic public recipe\n{extra}---\n{body}\n"
    )
    (path / "LICENSE").write_text("Synthetic fixture license")
    return path


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    service = SkillService(tmp_path / "skills", workspace)
    provider = ScriptedProvider([])
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace, allow_write=True),
        skill_service=service,
    )
    commands = Registry(runtime, LocalJournal(store, "owner"), path=tmp_path / "commands.sqlite")
    yield tmp_path, workspace, store, session, service, provider, runtime, commands
    commands.close()
    await runtime.close()
    store.close()


def command(session, text, key="command"):
    return CommandRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        input=text,
        idempotency_key=key,
    )


def request(session, key="task"):
    return TaskRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Use the installed recipe to create the requested proof file.",
        idempotency_key=key,
    )


async def activate(service, source, scope="public_synthetic"):
    result = await service.mutate(
        "install", source=str(source), content_scope=scope, authority="owner"
    )
    await service.mutate("enable", skill_id=result["id"], authority="owner")
    return result["id"]


async def test_model_loads_selected_skill_and_resource_then_writes_real_evidence(env):
    root, workspace, store, session, service, provider, runtime, _ = env
    skill_id = await activate(service, package(root / "source"))
    provider.responses.extend(
        [
            json.dumps(action)
            for action in [
                {"tool": "skills_list", "arguments": {}},
                {"tool": "skill_load", "arguments": {"skill_id": skill_id}},
                {"tool": "skill_load", "arguments": {"skill_id": skill_id, "resource": "LICENSE"}},
                {
                    "tool": "write_file",
                    "arguments": {"path": "proof.txt", "content": "public proof"},
                },
                {
                    "tool": "finish",
                    "arguments": {"summary": "Created proof", "objective_revision": 1},
                },
            ]
        ]
    )
    task = await runtime.submit(request(session))
    result = await runtime.wait(task.task_id, timeout=3)
    assert result.state == "succeeded"
    assert (workspace / "proof.txt").read_text() == "public proof"
    assert any(
        e.kind == "file" and e.sha256 == hashlib.sha256(b"public proof").hexdigest()
        for e in result.evidence
    )
    assert len([e for e in result.evidence if e.kind == "external"]) == 3
    instruction = next(
        json.loads(e.output)
        for e in result.evidence
        if e.kind == "external" and "instruction_sha256" in e.output
    )
    assert instruction["instruction_path"] == "SKILL.md"
    assert (
        instruction["instruction_sha256"]
        == "sha256:" + hashlib.sha256((root / "source/SKILL.md").read_bytes()).hexdigest()
    )
    assert "source" in instruction and "digest" in instruction
    assert (await service.inspect_owner(skill_id))["dependent_task_ids"] == []
    assert "Synthetic fixture license" in json.dumps(provider.calls[-1])
    assert any(e.kind == "agent.tool" for e in store.read())


async def test_skill_content_alone_never_proves_task_success(env):
    root, _, _, session, service, provider, runtime, _ = env
    skill_id = await activate(service, package(root / "source"))
    provider.responses.extend(
        [
            json.dumps({"tool": "skill_load", "arguments": {"skill_id": skill_id}}),
            json.dumps({"tool": "finish", "arguments": {"objective_revision": 1}}),
        ]
    )
    task = await runtime.submit(request(session))
    result = await runtime.wait(task.task_id, timeout=3)
    assert result.state == "failed" and "independent native evidence" in result.error
    assert (await service.inspect_owner(skill_id))["dependent_task_ids"] == []


async def test_owner_lifecycle_commands_use_no_model_and_preserve_builtin_stop(env):
    root, _, _, session, service, provider, _, commands = env
    source = package(root / "source", name="stop")
    installed = await commands.execute(
        command(session, f'/skills install "{source}" public_synthetic')
    )
    assert installed["status"] == "ok"
    assert installed["result"]["id"] == "local/stop"
    enabled = await commands.execute(command(session, "/skills enable local/stop", "enable"))
    assert enabled["status"] == "ok"
    assert len([x for x in commands.definitions()["commands"] if x["name"] == "stop"]) == 1
    stopped = await commands.execute(command(session, "/stop", "stop"))
    assert stopped["status"] == "error" and "No matching active task" in stopped["error"]
    assert provider.calls == []
    assert (await service.inspect_owner("local/stop"))["enabled"]


async def test_explicit_manual_skill_command_is_a_normal_idempotent_agent_task(env):
    root, workspace, _, session, service, provider, runtime, commands = env
    skill_id = await activate(service, package(root / "source", extra="manual-only: true\n"))
    await commands.refresh_skill_commands()
    definitions = {row["name"]: row for row in commands.definitions()["commands"]}
    assert definitions["skill"]["execution_type"] == "skill_invocation"
    assert definitions["recipe"]["execution_type"] == "skill_invocation"
    provider.responses.extend(
        [
            json.dumps(
                {
                    "tool": "write_file",
                    "arguments": {"path": "proof.txt", "content": "public proof"},
                }
            ),
            json.dumps({"tool": "finish", "arguments": {"objective_revision": 1}}),
        ]
    )
    body = command(session, "/recipe Write the proof file.")
    first = await commands.execute(body)
    assert first["status"] == "ok" and first["result"]["skill_id"] == skill_id
    assert await commands.execute(body) == first
    result = await runtime.wait(first["result"]["accepted_task_id"], timeout=3)
    assert result.state == "succeeded" and (workspace / "proof.txt").is_file()
    assert len(provider.calls) == 2


async def test_model_cannot_choose_user_invocation_or_dispatch_lifecycle(env):
    root, _, _, session, service, provider, runtime, _ = env
    skill_id = await activate(
        service, package(root / "source", extra="manual-only: true\n", body="PRIVATE MANUAL BODY")
    )
    provider.responses.extend(
        [
            json.dumps(
                {"tool": "skill_load", "arguments": {"skill_id": skill_id, "invocation": "user"}}
            ),
            json.dumps({"tool": "finish", "arguments": {"objective_revision": 1}}),
        ]
    )
    task = await runtime.submit(request(session))
    result = await runtime.wait(task.task_id, timeout=3)
    assert result.state == "failed"
    assert "PRIVATE MANUAL BODY" not in json.dumps(provider.calls)


async def test_contributor_cannot_explicitly_load_local_only_skill(env):
    root, _, _, session, service, provider, runtime, _ = env
    provider.info = ProviderInfo(provider="meta", model="any-alias", local=False)
    skill_id = await activate(
        service, package(root / "source", body="PRIVATE INSTRUCTIONS"), scope="local_only"
    )
    task = await runtime.submit(
        request(session).model_copy(update={"content_scope": "public_synthetic"}),
        explicit_skill=skill_id,
    )
    result = await runtime.wait(task.task_id, timeout=3)
    assert result.state == "failed" and provider.calls == []


async def test_revocation_blocks_next_model_request_and_cancellation_releases_pin(env):
    root, _, _, session, service, _, runtime, _ = env
    skill_id = await activate(service, package(root / "source"))
    entered, release = asyncio.Event(), asyncio.Event()

    class Hung:
        info = ProviderInfo(provider="meta", model="alias", local=False)
        calls = 0

        async def prepare_complete(self, messages):
            """Explicit local test double; no tokenizer or inference claim."""
            return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

        async def complete(self, messages):
            self.calls += 1
            entered.set()
            await release.wait()
            return json.dumps({"tool": "skills_list", "arguments": {}})

    runtime.provider = provider = Hung()
    task = await runtime.submit(
        request(session).model_copy(update={"content_scope": "public_synthetic"}),
        explicit_skill=skill_id,
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    await service.mutate("scope", authority="owner", skill_id=skill_id, content_scope="local_only")
    release.set()
    result = await runtime.wait(task.task_id, timeout=3)
    assert result.state == "failed" and provider.calls == 1
    assert (await service.inspect_owner(skill_id))["dependent_task_ids"] == []


async def test_cancel_hung_inference_releases_pinned_skill(env):
    root, _, _, session, service, _, runtime, _ = env
    skill_id = await activate(service, package(root / "source"))
    entered = asyncio.Event()

    class Hung:
        info = ProviderInfo(provider="fixture", model="local", local=True)

        async def prepare_complete(self, messages):
            """Explicit local test double; no tokenizer or inference claim."""
            return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

        async def complete(self, messages):
            entered.set()
            await asyncio.Event().wait()

    runtime.provider = Hung()
    task = await runtime.submit(request(session), explicit_skill=skill_id)
    await asyncio.wait_for(entered.wait(), timeout=2)
    await runtime.interrupt(
        task.task_id, InterruptRequest(expected_objective_revision=1, idempotency_key="cancel")
    )
    result = await runtime.wait(task.task_id, timeout=3)
    assert result.state == "cancelled"
    assert (await service.inspect_owner(skill_id))["dependent_task_ids"] == []


async def test_owner_api_is_authenticated_and_uses_same_controller(env):
    root, _, _, _, _, provider, runtime, commands = env
    auth = Auth({"owner": "o" * 32, "decisions": "d" * 32})
    app = create_app(runtime, auth, commands)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/skills")).status_code == 401
        assert (
            await client.get("/v1/skills", headers={"Authorization": "Bearer " + "d" * 32})
        ).status_code == 403
        client.headers["Authorization"] = "Bearer " + "o" * 32
        installed = await client.post(
            "/v1/skills/control",
            json={"operation": "install", "source": str(package(root / "source"))},
        )
        assert installed.status_code == 200
        listing = await client.get("/v1/skills")
        assert listing.status_code == 200 and len(listing.json()["skills"]) == 1
        assert (
            await client.post(
                "/v1/skills/control",
                json={"operation": "enable", "skill_id": "local/recipe", "authority": "owner"},
            )
        ).status_code == 422
        definitions = (await client.get("/v1/commands")).json()["commands"]
        assert next(row for row in definitions if row["name"] == "recipe")["available"] is False
        assert provider.calls == []


async def test_disclosure_review_is_bound_to_exact_bytes_not_skill_name(env):
    root, _, _, _, service, _, _, _ = env
    src = package(root / "source", body="PUBLIC FIRST VERSION")
    skill_id = await activate(service, src)
    (src / "SKILL.md").write_text(
        "---\nname: recipe\ndescription: Private replacement\n---\nSYNTHETIC PRIVATE SECOND VERSION"
    )
    await service.mutate("update", authority="owner", skill_id=skill_id, source=str(src))
    assert (await service.inspect_owner(skill_id))["scope"] == "local_only"
    cloud = ProviderInfo(provider="meta", model="alias", local=False)
    assert (await service.catalog(cloud))["skills"] == []
    with pytest.raises(SkillServiceError, match="disclosable"):
        await service.load("new-task", skill_id, cloud)


async def test_reviewing_new_public_version_does_not_disclose_old_private_pin(env):
    root, _, _, _, service, _, _, _ = env
    src = package(root / "source", body="SYNTHETIC PRIVATE OLD VERSION")
    skill_id = await activate(service, src, scope="local_only")
    local = ProviderInfo(provider="fixture", model="local", local=True)
    await service.load("old-task", skill_id, local)
    (src / "SKILL.md").write_text(
        "---\nname: recipe\ndescription: Public replacement\n---\nPUBLIC NEW VERSION"
    )
    await service.mutate(
        "update",
        authority="owner",
        skill_id=skill_id,
        source=str(src),
        content_scope="public_synthetic",
    )
    cloud = ProviderInfo(provider="meta", model="alias", local=False)
    assert "PUBLIC NEW VERSION" in (await service.load("new-task", skill_id, cloud))["body"]
    with pytest.raises(SkillServiceError, match="Pinned skill version"):
        await service.load("old-task", skill_id, cloud)


async def test_failed_revocation_cannot_resurrect_public_scope_after_restart(env, monkeypatch):
    root, workspace, _, _, service, _, _, _ = env
    skill_id = await activate(service, package(root / "source"))

    def fail():
        raise SkillServiceError("Synthetic disk failure")

    monkeypatch.setattr(service, "_write_scopes", fail)
    with pytest.raises(SkillServiceError, match="Synthetic disk failure"):
        await service.mutate(
            "scope", authority="owner", skill_id=skill_id, content_scope="local_only"
        )
    assert (await service.inspect_owner(skill_id))["scope"] == "local_only"
    service.close()
    with pytest.raises(SkillServiceError, match="Unfinished skill scope change"):
        SkillService(root / "skills", workspace)


def test_controller_overlap_and_hardlink_lock_fail_before_modifying_outside(tmp_path):
    import os

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SkillServiceError, match="disjoint"):
        SkillService(workspace / "state", workspace)
    assert list(workspace.iterdir()) == []
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("Synthetic sentinel")
    outside.chmod(0o644)
    os.link(outside, root / "controller.lock")
    with pytest.raises(SkillServiceError, match="regular file"):
        SkillService(root, workspace)
    assert outside.stat().st_mode & 0o777 == 0o644


async def test_public_task_pin_survives_explicitly_reviewed_public_update(env):
    root, _, _, _, service, _, _, _ = env
    src = package(root / "source", body="PUBLIC OLD VERSION")
    skill_id = await activate(service, src)
    cloud = ProviderInfo(provider="meta", model="alias", local=False)
    old = await service.load("old-task", skill_id, cloud)
    (src / "SKILL.md").write_text(
        "---\nname: recipe\ndescription: Public replacement\n---\nPUBLIC NEW VERSION"
    )
    await service.mutate(
        "update",
        authority="owner",
        skill_id=skill_id,
        source=str(src),
        content_scope="public_synthetic",
    )
    assert (await service.load("old-task", skill_id, cloud))["digest"] == old["digest"]
    assert "PUBLIC NEW VERSION" in (await service.load("new-task", skill_id, cloud))["body"]


async def test_large_instruction_package_can_load_small_resource_and_recheck_scope(env):
    root, _, _, _, service, _, _, _ = env
    skill_id = await activate(service, package(root / "source", body="Long instructions " * 1200))
    cloud = ProviderInfo(provider="meta", model="alias", local=False)
    result = await service.load("resource-task", skill_id, cloud, resource="LICENSE")
    assert result["resource_text"] == "Synthetic fixture license"
    await service.validate_task("resource-task", cloud)
    await service.mutate("disable", authority="owner", skill_id=skill_id)
    with pytest.raises(SkillServiceError, match="not enabled"):
        await service.validate_task("resource-task", cloud)


async def test_bare_name_collision_never_selects_a_publisher_by_install_order(env):
    from packages.ecosystem.skills import SourceRef

    root, _, _, session, service, provider, _, commands = env
    src = package(root / "source")
    await activate(service, src)
    remote = await service.mutate(
        "install",
        authority="owner",
        source=str(src),
        source_ref=SourceRef.parse("owner/repo@" + "a" * 40),
    )
    await service.mutate("enable", authority="owner", skill_id=remote["id"])
    await commands.refresh_skill_commands()
    assert not any(row["name"] == "recipe" for row in commands.definitions()["commands"])
    result = await commands.execute(command(session, "/recipe do work"))
    assert result["status"] == "error" and provider.calls == []


async def test_large_command_catalog_uses_bounded_pages_and_qualified_ids(env):
    root, _, _, _, service, _, _, commands = env
    for i in range(65):
        src = package(root / f"source-{i}", name=f"fixture-{i}")
        await service.mutate("install", authority="owner", source=str(src))
    await commands.refresh_skill_commands()
    definitions = commands.definitions()
    assert definitions["skill_commands_truncated"] is True
    assert not any(
        row["execution_type"] == "skill_invocation" and row["name"] != "skill"
        for row in definitions["commands"]
    )
    assert len((await service.list_owner(after=64))["skills"]) == 1
