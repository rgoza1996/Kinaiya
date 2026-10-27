"""Real isolated computation through owner controls and independent Agent."""

import asyncio
import json
import shlex
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from core.plugins import PluginError
from core.policy import ToolPolicy
from packages.commands import Registry
from packages.contracts.models import CommandRequest, ProviderInfo, SessionRequest, TaskRequest
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.agent.plugin_controls import PluginControl, apply_plugin_control
from services.agent.plugin_service import PluginService
from services.common.auth import Auth
from services.session.store import LocalJournal, Store

SOURCE = b"def run(a):\n    return {'sum': a['a'] + a['b']}\n"
MANIFEST = {
    "plugin_id": "adder",
    "name": "Synthetic addition",
    "description": "Compute a sum without language-model inference",
    "author": "synthetic-fixture",
    "license": "MIT",
    "input_schema": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {"sum": {"type": "integer"}},
        "required": ["sum"],
        "additionalProperties": False,
    },
}


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    suite = tmp_path / "owner-suite.json"
    suite.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "plugin_id": "adder",
                "cases": [{"input": {"a": 1, "b": 2}, "expected": {"sum": 3}}],
            }
        )
    )
    service = PluginService(tmp_path / "plugins", workspace, {"adder": suite})
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    provider = ScriptedProvider([])
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace, allow_write=True),
        plugin_service=service,
    )
    yield SimpleNamespace(
        service=service,
        runtime=runtime,
        provider=provider,
        store=store,
        session=session,
        workspace=workspace,
        suite=suite,
        root=tmp_path,
    )
    await runtime.close()
    store.close()


async def activate(env, source=SOURCE, suffix=""):
    version = env.service.registry.register_bytes(source, MANIFEST)
    report = await env.service.evaluate(version)
    assert report["passed"]
    await apply_plugin_control(
        env.runtime,
        PluginControl(
            operation="promote",
            version_id=version,
            report_id=report["report_id"],
            reviewed=True,
            idempotency_key="promote" + suffix,
        ),
    )
    return version


def action(tool, **arguments):
    return json.dumps({"tool": tool, "arguments": arguments})


async def submit(env, responses):
    env.provider.responses.extend(responses)
    s = env.session
    task = await env.runtime.submit(
        TaskRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            objective="Public synthetic computation; save the real result",
            idempotency_key="task",
        )
    )
    return await asyncio.wait_for(env.runtime.wait(task.task_id), 5)


async def test_unapproved_candidate_cannot_run_or_appear_to_agent(env):
    version = env.service.registry.register_bytes(SOURCE, MANIFEST)
    with pytest.raises(PluginError, match="approved|inactive"):
        await env.service.run(version, {"a": 1, "b": 2})
    assert (await env.service.catalog(env.provider.info))["plugins"] == []
    assert env.provider.calls == []


async def test_owner_direct_run_and_cli_use_no_model_and_replay_result(env):
    version = await activate(env)
    control = PluginControl(
        operation="run",
        version_id=version,
        arguments={"a": 7, "b": 8},
        idempotency_key="direct-run",
    )
    result = await apply_plugin_control(env.runtime, control)
    assert result["output"] == {"sum": 15}
    assert await apply_plugin_control(env.runtime, control) == result
    commands = Registry(env.runtime, LocalJournal(env.store, "owner"))
    try:
        payload = {"operation": "run", "version_id": version, "arguments": {"a": 9, "b": 8}}
        s = env.session
        reply = await commands.execute(
            CommandRequest(
                session_id=s.session_id,
                thread_id=s.thread_id,
                epoch=s.epoch,
                input="/plugins control " + shlex.quote(json.dumps(payload)),
                idempotency_key="cli",
            )
        )
        assert reply["status"] == "ok", reply
        assert reply["result"]["output"] == {"sum": 17}
        assert env.provider.calls == []
        definition = next(d for d in commands.definitions()["commands"] if d["name"] == "plugins")
        assert definition["surfaces"] == ["agent-chat", "agent-cli"]
    finally:
        commands.close()


async def test_owner_authentication_and_exact_control_fields(env):
    tokens = {
        role: role + "-synthetic-token-0123456789"
        for role in (
            "owner",
            "agent",
            "converse",
            "decisions",
            "resources",
        )
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env.runtime, Auth(tokens))),
        base_url="http://test",
    ) as client:
        for role in ("agent", "converse", "decisions", "resources"):
            reply = await client.get(
                "/v1/plugins", headers={"Authorization": "Bearer " + tokens[role]}
            )
            assert reply.status_code == 403
        headers = {"Authorization": "Bearer " + tokens["owner"]}
        assert (await client.get("/v1/plugins", headers=headers)).status_code == 200
        reply = await client.post(
            "/v1/plugins/control",
            headers=headers,
            json={
                "operation": "disable",
                "plugin_id": "adder",
                "arguments": {},
                "idempotency_key": "bad-extra",
            },
        )
        assert reply.status_code == 400


async def test_agent_calls_durable_plugin_and_finishes_with_native_file_evidence(env):
    version = await activate(env)
    task = await submit(
        env,
        [
            action("plugins_list", plugin_id="adder"),
            action("plugin_call", plugin_id="adder", arguments={"a": 40, "b": 2}),
            action("write_file", path="result.json", content='{"sum":42}'),
            action("finish", summary="Saved sum 42", objective_revision=1),
        ],
    )
    assert task.state == "succeeded", task.error
    assert env.runtime.plugin_pins(task.task_id) == {"adder": version}
    assert json.loads((env.workspace / "result.json").read_text()) == {"sum": 42}
    assert any(e.kind == "file" and e.sha256 for e in task.evidence)
    assert any(e.kind == "extension" and version in e.output for e in task.evidence)
    pinned = env.store.read(kinds=["plugin.pinned"])
    assert len(pinned) == 1 and pinned[0].origin == "agent-supervisor"
    assert pinned[0].payload["plugin_versions"] == {"adder": version}


async def test_failed_plugin_cannot_be_hidden_by_a_successful_write(env):
    await activate(env)
    task = await submit(
        env,
        [
            action("plugin_call", plugin_id="adder", arguments={"a": "bad", "b": 2}),
            action("write_file", path="unrelated", content="public synthetic"),
            action("finish", summary="False success", objective_revision=1),
        ],
    )
    assert task.state == "failed" and "plugin computation failed" in task.error


async def test_plugin_receipt_alone_is_not_task_completion_authority(env):
    await activate(env)
    task = await submit(
        env,
        [
            action("plugin_call", plugin_id="adder", arguments={"a": 1, "b": 2}),
            action("finish", summary="Unverified external effect", objective_revision=1),
        ],
    )
    assert task.state == "failed" and "native evidence" in task.error


async def test_recovery_retains_native_pins_and_never_reexecutes(env):
    version = await activate(env)
    waiting = asyncio.Event()
    original = env.provider.complete

    async def provider(messages):
        if env.provider.responses:
            return await original(messages)
        waiting.set()
        await asyncio.Event().wait()

    env.provider.complete = provider
    env.provider.responses.append(
        action("plugin_call", plugin_id="adder", arguments={"a": 1, "b": 2})
    )
    s = env.session
    task = await env.runtime.submit(
        TaskRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            objective="Public recovery fixture",
            idempotency_key="recovery",
        )
    )
    await asyncio.wait_for(waiting.wait(), 3)
    await env.runtime.close()
    env.service = PluginService(env.root / "plugins", env.workspace, {"adder": env.suite})
    env.provider = ScriptedProvider([])
    env.runtime = AgentRuntime(
        LocalJournal(env.store, "agent"),
        env.provider,
        env.workspace,
        ToolPolicy(env.workspace),
        plugin_service=env.service,
    )
    recovered = await env.runtime.recover()
    assert recovered[0].task_id == task.task_id and recovered[0].state == "paused"
    assert env.runtime.plugin_pins(task.task_id) == {"adder": version}
    assert env.runtime.plugin_dependents(version) == [task.task_id]
    env.service.registry.disable("adder")
    with pytest.raises(ValueError, match="recoverable task"):
        await apply_plugin_control(
            env.runtime,
            PluginControl(
                operation="remove",
                version_id=version,
                idempotency_key="remove-recovered",
            ),
        )
    assert env.provider.calls == []


async def test_startup_reconciles_orphan_pins_from_native_journal(env):
    version = await activate(env)
    env.service.registry.pin(version, task_id="crashed_before_pin_journal")
    await env.runtime.recover()
    env.service.registry.disable("adder")
    env.service.registry.remove(version)


async def test_task_pins_are_separate_and_removal_requires_all_tasks_release(env):
    version = await activate(env)
    one = SimpleNamespace(task_id="task_one", plugin_versions={})
    two = SimpleNamespace(task_id="task_two", plugin_versions={})
    for task in (one, two):
        assert (await env.service.pin_task(task, "adder", env.provider.info))[0] == version
    await env.service.release_task(one.task_id)
    env.service.registry.disable("adder")
    with pytest.raises(PluginError, match="pinned"):
        env.service.registry.remove(version)
    await env.service.release_task(two.task_id)
    env.service.registry.remove(version)


async def test_failed_durable_unpin_keeps_release_retryable(env, monkeypatch):
    version = await activate(env)
    task = SimpleNamespace(task_id="synthetic_cleanup_failure", plugin_versions={})
    assert (await env.service.pin_task(task, "adder", env.provider.info))[0] == version
    original = env.service.registry._write_pins
    calls = 0

    def fail_once(pins):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("Synthetic pin persistence failure")
        return original(pins)

    monkeypatch.setattr(env.service.registry, "_write_pins", fail_once)
    with pytest.raises(OSError, match="pin persistence"):
        await env.service.release_task(task.task_id)
    await env.service.release_task(task.task_id)
    assert not env.service.registry._read_pins()


async def test_approved_pin_stays_on_old_version_and_disable_revokes_it(env):
    first = await activate(env)
    task = SimpleNamespace(task_id="pinned_task", plugin_versions={})
    await env.service.pin_task(task, "adder", env.provider.info)
    second = await activate(env, SOURCE + b"# independently reviewed revision\n", "-second")
    assert first != second and env.service.registry.active("adder") == second
    result = await env.service.call_task(task, "adder", {"a": 2, "b": 4}, env.provider.info)
    assert result["receipt"]["version_id"] == first
    env.service.registry.disable("adder")
    with pytest.raises(PluginError):
        await env.service.call_task(task, "adder", {"a": 2, "b": 4}, env.provider.info)


async def test_contributor_never_receives_local_or_private_authorized_metadata(env):
    version = await activate(env)
    contributor = ProviderInfo(provider="meta", model="muse-spark-1.3-contributor", local=False)
    for scope in ("local_only", "private_cloud_authorized"):
        await env.service.set_scope(version, scope, reviewed=True)
        assert (await env.service.catalog(contributor))["plugins"] == []
        with pytest.raises(PluginError, match="disclosure"):
            await env.service.catalog(contributor, plugin_id="adder")
    await env.service.set_scope(version, "public_synthetic", reviewed=True)
    assert (await env.service.catalog(contributor))["plugins"][0]["plugin_id"] == "adder"
    env.service.scope_pending.write_text("uncertain update")
    assert (await env.service.catalog(contributor))["plugins"] == []


async def test_suite_change_revokes_approval_and_blocks_rollback(env):
    first = await activate(env)
    await activate(env, SOURCE + b"# next revision\n", "-second")
    env.suite.write_text(env.suite.read_text() + " ")  # canonical-equivalent remains valid
    assert (await env.service.run(first, {"a": 1, "b": 3}))["output"] == {"sum": 4}
    suite = json.loads(env.suite.read_text())
    suite["cases"][0]["expected"] = {"sum": 99}
    env.suite.write_text(json.dumps(suite))
    with pytest.raises(PluginError, match="acceptance report"):
        await env.service.run(first, {"a": 1, "b": 3})
    with pytest.raises(PluginError, match="acceptance report"):
        env.service.registry.rollback("adder", reviewed=True)


async def test_lifecycle_replay_and_conflicts_survive_reopen(env):
    first = await activate(env)
    await activate(env, SOURCE + b"# next\n", "-second")
    rollback = PluginControl(
        operation="rollback", plugin_id="adder", reviewed=True, idempotency_key="rb"
    )
    saved = await apply_plugin_control(env.runtime, rollback)
    assert saved == {"active": first}
    await env.service.close()
    env.service = PluginService(env.root / "plugins", env.workspace, {"adder": env.suite})
    env.runtime.plugin_service = env.service
    assert await apply_plugin_control(env.runtime, rollback) == saved
    changed = rollback.model_copy(update={"operation": "disable", "reviewed": False})
    with pytest.raises(ValueError, match="conflicts"):
        await apply_plugin_control(env.runtime, changed)

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await env.service.owner_control("uncertain", {}, cancelled)
    with pytest.raises(ValueError, match="uncertain"):
        await env.service.owner_control("uncertain", {}, lambda: None)


async def test_service_cancellation_waits_for_real_sandbox_reap(env):
    source = b"def run(a):\n    if a['a'] == 99:\n        while True:\n            pass\n    return {'sum': a['a'] + a['b']}\n"
    version = await activate(env, source)
    worker = asyncio.create_task(env.service.run(version, {"a": 99, "b": 0}))
    await asyncio.sleep(0.15)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2)
    # The single execution slot is available only after the isolated worker exits.
    result = await asyncio.wait_for(env.service.run(version, {"a": 2, "b": 3}), 2)
    assert result["output"] == {"sum": 5}


async def test_repeated_cancellation_cannot_abandon_worker_thread(env, monkeypatch):
    version = await activate(env)
    entered, release = threading.Event(), threading.Event()

    def blocked(*args, cancel_event, **kwargs):
        entered.set()
        while not release.wait(0.01):
            pass
        raise PluginError("cancelled and reaped")

    monkeypatch.setattr(env.service.registry, "run", blocked)
    worker = asyncio.create_task(env.service.run(version, {}))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.005)
        worker.cancel()
        await asyncio.sleep(0.01)
        worker.cancel()
        await asyncio.sleep(0.01)
        assert not worker.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2)


async def test_disable_during_execution_is_responsive_and_revokes_result(env, monkeypatch):
    import core.plugins.registry as module

    version = await activate(env)
    entered, release = threading.Event(), threading.Event()
    original = module.run_plugin

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "run_plugin", blocked)
    worker = asyncio.create_task(env.service.run(version, {"a": 1, "b": 2}))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.005)
        start = time.monotonic()
        env.service.registry.disable("adder")
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
    with pytest.raises(PluginError, match="disabled"):
        await asyncio.wait_for(worker, 2)


async def test_control_roots_and_owner_suites_are_protected_from_agent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="disjoint"):
        PluginService(workspace / "plugins", workspace, {})
    suite = workspace / "suite.json"
    suite.write_text("{}")
    with pytest.raises(ValueError, match="outside"):
        PluginService(tmp_path / "plugins", workspace, {"adder": suite})
