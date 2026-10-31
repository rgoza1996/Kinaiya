"""Retained native pins at actual Meta HTTP boundaries, with offline HTTP only.

Skill control, plugin approval/evaluation/pinning and isolated computation are
real. Synthetic plugin suites are outside the task workspace; no model or
external server is invoked by these tests.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from core.plugins import PluginError
from core.policy import ToolPolicy
from packages.contracts.models import ProviderInfo, SessionRequest, TaskRequest
from packages.providers.meta_action_wire import encode_action
from packages.providers.text import MetaResponsesProvider
from services.agent import AgentRuntime
from services.agent.plugin_service import PluginService
from services.agent.skill_service import SkillService, SkillServiceError
from services.session.store import LocalJournal, Store

MARKER = "PUBLIC_SYNTHETIC_RETAINED_PIN_BODY"
LOCAL = ProviderInfo(provider="fixture", model="local", local=True)
META = ProviderInfo(provider="meta", model="synthetic", local=False)


def skill_source(path, *, version="1.0"):
    path.mkdir()
    (path / "SKILL.md").write_text(
        "---\nname: probe\ndescription: Public synthetic skill\n"
        f'version: "{version}"\n---\n{MARKER}\n',
        encoding="utf-8",
    )
    return path


async def approve_plugin(service, *, suffix=""):
    manifest = {
        "plugin_id": "probe",
        "name": "Public synthetic plugin",
        "description": "Return a fixed public synthetic value",
        "author": "synthetic",
        "license": "MIT",
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    source = f"def run(a):\n    return {{'value': {MARKER!r}}}\n{suffix}".encode()
    version = service.registry.register_bytes(source, manifest)
    report = await service.evaluate(version)
    assert report["passed"]
    service.registry.promote(version, report["report_id"], actor="owner", reviewed=True)
    await service.set_scope(version, "public_synthetic", reviewed=True)
    return version


@pytest.fixture(params=["skill", "plugin"])
async def env(request, tmp_path):
    gateway = request.param
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "proof.txt").write_text("Public synthetic native file evidence")
    if gateway == "skill":
        service = SkillService(tmp_path / "skills", workspace)
        installed = await service.mutate(
            "install",
            source=skill_source(tmp_path / "source"),
            content_scope="public_synthetic",
            authority="owner",
        )
        identity = installed["id"]
        await service.mutate("enable", skill_id=identity, authority="owner")
        version = installed["version"]
    else:
        suite = tmp_path / "suite.json"
        suite.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "plugin_id": "probe",
                    "cases": [{"input": {}, "expected": {"value": MARKER}}],
                }
            )
        )
        service = PluginService(tmp_path / "plugins", workspace, {"probe": suite})
        identity, version = "probe", await approve_plugin(service)
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    state = SimpleNamespace(
        gateway=gateway,
        root=tmp_path,
        workspace=workspace,
        service=service,
        identity=identity,
        version=version,
        store=store,
        session=session,
        calls=[],
        changed=False,
        provider=None,
        runtime=None,
    )

    async def start(*, stage=None, change=None):
        actions = [
            {"tool": "read_file", "arguments": {"path": "proof.txt"}},
            {"tool": "finish", "arguments": {"summary": "Read proof", "objective_revision": 1}},
        ]
        if gateway == "plugin":
            actions.insert(
                0, {"tool": "plugin_call", "arguments": {"plugin_id": identity, "arguments": {}}}
            )

        async def send(req):
            body = json.loads(req.content)
            state.calls.append((req.url.path, MARKER in req.content.decode(), body))
            if req.url.path.endswith("/input_tokens"):
                return httpx.Response(
                    200, json={"object": "response.input_tokens", "input_tokens": 123}
                )
            action = actions.pop(0)
            if action["tool"] == "plugin_call":
                action = {
                    "tool": "plugin_call",
                    "arguments": {"plugin_id": identity, "plugin_arguments_json": "{}"},
                }
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": json.dumps(encode_action(action))}
                            ],
                        }
                    ],
                },
            )

        provider = MetaResponsesProvider(
            base_url="https://synthetic.invalid/v1",
            api_key="public-synthetic-fixture",
            allow_cloud=True,
            structured_actions=True,
            action_format="typed-v2",
            context_tokens=32768,
            max_tokens=8192,
            transport=httpx.MockTransport(send),
        )

        async def hook(req):
            selected = (
                req.url.path.endswith("/input_tokens")
                if stage == "count"
                else req.url.path == "/v1/responses"
            )
            if not state.changed and selected and MARKER in req.content.decode():
                state.changed = True
                await change(state)

        if change is not None:
            provider.http.event_hooks["request"].append(hook)
        runtime = AgentRuntime(
            LocalJournal(store, "agent"),
            provider,
            workspace,
            ToolPolicy(workspace),
            **{gateway + "_service": service},
        )
        state.provider, state.runtime = provider, runtime
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Inspect the public synthetic pin and read proof.txt.",
                content_scope="public_synthetic",
                idempotency_key="retained-disclosure",
            ),
            explicit_skill=identity if gateway == "skill" else None,
        )
        state.task_id = task.task_id
        return await runtime.wait(task.task_id, timeout=5)

    state.start = start
    yield state
    if service._lock.locked():
        service._lock.release()
    if state.runtime is not None:
        await state.runtime.close()
    if state.provider is not None:
        await state.provider.close()
    if gateway == "skill":
        service.close()
    else:
        await service.close()
    store.close()


async def mutate(state, operation):
    service = state.service
    if operation == "scope":
        if state.gateway == "skill":
            await service.mutate(
                "scope", skill_id=state.identity, content_scope="local_only", authority="owner"
            )
        else:
            await service.set_scope(state.version, "local_only", reviewed=True)
    elif operation == "disable":
        if state.gateway == "skill":
            await service.mutate("disable", skill_id=state.identity, authority="owner")
        else:
            service.registry.disable(state.identity)
    elif operation == "release":
        await service.release_task(state.task_id)
    else:
        assert operation == "held_lock"
        await service._lock.acquire()


@pytest.mark.parametrize("stage", ["count", "generation"])
@pytest.mark.parametrize("operation", ["scope", "disable", "held_lock", "release"])
async def test_changed_retained_disclosure_stops_before_protected_upload(env, stage, operation):
    async def change(state):
        await mutate(state, operation)

    final = await env.start(stage=stage, change=change)
    assert env.changed, final.error
    assert final.state == "failed", final.error
    protected = [path for path, contains, _ in env.calls if contains]
    assert protected == ([] if stage == "count" else ["/v1/responses/input_tokens"])
    assert not any(e.kind == "read" and e.path for e in final.evidence)


async def test_unchanged_retained_pins_continue_through_count_and_generation(env):
    final = await env.start()
    assert final.state == "succeeded", final.error
    assert any(e.kind == "read" and e.path for e in final.evidence)
    assert {path for path, contains, _ in env.calls if contains} == {
        "/v1/responses/input_tokens",
        "/v1/responses",
    }


@pytest.mark.parametrize("stage", ["count", "generation"])
async def test_rebound_pin_stops_before_protected_upload(env, stage):
    async def change(state):
        service = state.service
        if state.gateway == "skill":
            await service.mutate(
                "update",
                authority="owner",
                skill_id=state.identity,
                source=skill_source(state.root / "updated", version="2.0"),
                content_scope="public_synthetic",
            )
            await service.release_task(state.task_id)
            await service.load(state.task_id, state.identity, META, invocation="user")
        else:
            version = await approve_plugin(service, suffix="# second version\n")
            await service.release_task(state.task_id)
            # Exercise a native cache/task disagreement without rewriting the
            # runtime's trusted task map or replacing its dispatch function.
            await service.pin_task(
                SimpleNamespace(task_id=state.task_id, plugin_versions={state.identity: version}),
                state.identity,
                META,
            )

    final = await env.start(stage=stage, change=change)
    assert env.changed and final.state == "failed", final.error
    assert [path for path, contains, _ in env.calls if contains] == (
        [] if stage == "count" else ["/v1/responses/input_tokens"]
    )


@pytest.mark.parametrize("stage", ["count", "generation"])
async def test_changed_retained_source_stops_before_protected_upload(env, stage):
    async def change(state):
        if state.gateway == "skill":
            # Native cache corruption is distinct from an allowed update of
            # the installed package: the history still describes the old pin.
            pin = state.service._pins[(state.task_id, state.identity)]
            pin.source["ref"] = "changed-native-source-binding"
        else:
            source = state.service.registry._versions / state.version / "source.py"
            source.write_bytes(source.read_bytes() + b"# tampered stored source\n")

    final = await env.start(stage=stage, change=change)
    assert env.changed and final.state == "failed", final.error
    assert [path for path, contains, _ in env.calls if contains] == (
        [] if stage == "count" else ["/v1/responses/input_tokens"]
    )


async def test_legitimate_active_update_keeps_exact_old_pin_eligible(env):
    async def change(state):
        if state.gateway == "skill":
            await state.service.mutate(
                "update",
                authority="owner",
                skill_id=state.identity,
                source=skill_source(state.root / "updated", version="2.0"),
                content_scope="public_synthetic",
            )
        else:
            await approve_plugin(state.service, suffix="# eligible new active version\n")

    final = await env.start(stage="generation", change=change)
    assert env.changed and final.state == "succeeded", final.error
    if env.gateway == "plugin":
        assert env.runtime.plugin_pins(final.task_id) == {env.identity: env.version}
    assert any(e.kind == "read" and e.path for e in final.evidence)


async def test_local_only_pins_keep_local_eligibility_but_never_cloud_eligibility(env):
    service = env.service
    task = SimpleNamespace(task_id="native-local-task", plugin_versions={})
    if env.gateway == "skill":
        await service.mutate(
            "scope", skill_id=env.identity, content_scope="local_only", authority="owner"
        )
        await service.load(task.task_id, env.identity, LOCAL)
        guard = service.capture_task_disclosure(task.task_id, LOCAL)

        def cloud():
            return service.capture_task_disclosure(task.task_id, META)
    else:
        await service.set_scope(env.version, "local_only", reviewed=True)
        await service.pin_task(task, env.identity, LOCAL)
        guard = service.capture_task_disclosure(task, LOCAL)

        def cloud():
            return service.capture_task_disclosure(task, META)

    assert guard() is True
    with pytest.raises(
        (SkillServiceError, PluginError),
        match="^Retained (skill|plugin) disclosure is unavailable$",
    ):
        cloud()
    await service.release_task(task.task_id)
    assert guard() is False


async def test_capture_never_recreates_a_disposed_pin_or_executes(env, monkeypatch):
    service = env.service
    task = SimpleNamespace(task_id="native-guard-task", plugin_versions={})
    if env.gateway == "skill":
        await service.load(task.task_id, env.identity, META)

        def capture():
            return service.capture_task_disclosure(
                task.task_id, META, expected_skill_ids=frozenset({env.identity})
            )
    else:
        await service.pin_task(task, env.identity, META)

        def capture():
            return service.capture_task_disclosure(task, META)

    guard = capture()
    assert guard() is True

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A disclosure guard cannot load, pin, or execute")

    if env.gateway == "skill":
        monkeypatch.setattr(service, "load", forbidden)
        monkeypatch.setattr(service, "_capture_pin", forbidden)
    else:
        monkeypatch.setattr(service, "pin_task", forbidden)
        monkeypatch.setattr(service.registry, "pin", forbidden)
        monkeypatch.setattr(service.registry, "run", forbidden)
    assert guard() is True
    await service.release_task(task.task_id)
    assert guard() is False
    with pytest.raises(
        (SkillServiceError, PluginError),
        match="^Retained (skill|plugin) disclosure is unavailable$",
    ):
        capture()
    if env.gateway == "skill":
        monkeypatch.undo()
        await service.load(task.task_id, env.identity, META)
    else:
        monkeypatch.undo()
        await service.pin_task(task, env.identity, META)
    # Even recreating the same content/version cannot revive an old callback.
    assert guard() is False
    fresh = capture()
    assert fresh() is True
    if env.gateway == "skill":
        service.close()
    else:
        await service.close()
    assert fresh() is False
