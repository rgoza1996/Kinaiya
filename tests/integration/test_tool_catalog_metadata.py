"""Passive broker projections over synthetic state; no model or server calls.

Plugin approval reports below are explicit stored-state fixtures. They are
not evidence of evaluating the synthetic plugin or of sandbox readiness.
"""

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.plugins import PluginError
from core.plugins.manifest import PluginManifest
from core.plugins.suite import suite_hash_of
from core.policy import ToolPolicy
from packages.contracts.models import ProviderInfo
from packages.ecosystem import mcp_host as mcp
from packages.ecosystem.skills import SourceRef
from services.agent.mcp_tools import ModelScopedMCP
from services.agent.plugin_service import PluginService
from services.agent.skill_service import SkillService, SkillServiceError
from services.agent.tools import ToolFailure

LOCAL = ProviderInfo(provider="test", model="synthetic", local=True)
META = ProviderInfo(provider="meta", model="synthetic-alias", local=False)
CLOUD = ProviderInfo(provider="other", model="synthetic", local=False)


def forbidden(*args, **kwargs):
    raise AssertionError("execution or pin creation must not occur")


def skill_source(path, *, name="demo", version="1.0", description="Synthetic skill", extra=""):
    path.mkdir()
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: {json.dumps(version)}\n"
        f"description: {json.dumps(description)}\n{extra}---\n"
        "Synthetic private instruction body must not be returned.\n"
    )
    return path


@pytest.fixture
def skills(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = SkillService(tmp_path / "skills", workspace)
    yield service
    service.close()


async def install_skill(service, source, *, scope="public_synthetic", ref=None):
    installed = await service.mutate(
        "install", authority="owner", source=source, source_ref=ref, content_scope=scope
    )
    await service.mutate("enable", authority="owner", skill_id=installed["id"])
    return installed["id"]


def trap_skill_effects(service, monkeypatch):
    monkeypatch.setattr(service, "load", forbidden)
    monkeypatch.setattr(service, "_capture_pin", forbidden)
    monkeypatch.setattr(service, "_store_pin", forbidden)


async def test_skill_list_is_registered_only_and_selected_verifies_without_pinning(
    skills, tmp_path, monkeypatch
):
    identity = await install_skill(skills, skill_source(tmp_path / "source", description="d" * 200))
    trap_skill_effects(skills, monkeypatch)
    original_verify = skills._manager.verify
    verified = []

    def verify(record):
        verified.append(record.id)
        return original_verify(record)

    monkeypatch.setattr(skills._manager, "verify", verify)
    listed = await skills.tool_metadata(META)
    assert verified == [] and len(listed) == 1
    assert listed[0]["integrity"] == "registered_only"
    assert listed[0]["description"] == "d" * 80
    selected = (await skills.tool_metadata(META, skill_id=identity, task_id="task"))[0]
    assert verified == [identity]
    assert selected["integrity"] == "verified_current"
    assert selected["task_pinned_version"] is None
    assert selected["active_version"] == {
        "version": selected["version"],
        "digest": selected["digest"],
    }
    assert selected["source_kind"] == "local" and selected["source_revision"] is None
    assert not skills._pins and skills._pinned_bytes == 0
    encoded = json.dumps(selected)
    for excluded in ("body", "resource", "source/", "dependent_task", str(tmp_path)):
        assert excluded not in encoded


@pytest.mark.parametrize(
    "kind,prefix,reference", [("github", "github:", "a" * 40), ("clawhub", "clawhub:", "1.0")]
)
async def test_skill_long_qualified_identity_and_safe_source_revision(
    skills, tmp_path, kind, prefix, reference
):
    name = "n" * 128
    owner = "o" * 128
    ref = SourceRef(kind=kind, namespace=prefix + owner, name=name, ref=reference)
    identity = await install_skill(skills, skill_source(tmp_path / "source", name=name), ref=ref)
    row = (await skills.tool_metadata(META, skill_id=identity))[0]
    assert len(identity) in {264, 265}
    assert row["id"] == identity
    assert row["source_kind"] == kind and row["source_revision"] == reference
    assert set(row) == {
        "id",
        "name",
        "description",
        "version",
        "digest",
        "source_kind",
        "source_revision",
        "active_version",
        "task_pinned_version",
        "integrity",
        "model_invocable",
        "user_invocable",
    }


@pytest.mark.parametrize(
    "extra,scope,disabled",
    [
        ("", "local_only", False),
        ("manual-only: true\n", "public_synthetic", False),
        ("disable-model-invocation: true\n", "public_synthetic", False),
        ("", "public_synthetic", True),
    ],
)
async def test_skill_hidden_selection_matches_unknown_without_verification(
    skills, tmp_path, monkeypatch, extra, scope, disabled
):
    identity = await install_skill(
        skills, skill_source(tmp_path / "source", extra=extra), scope=scope
    )
    if disabled:
        await skills.mutate("disable", authority="owner", skill_id=identity)
    monkeypatch.setattr(skills._manager, "verify", forbidden)
    trap_skill_effects(skills, monkeypatch)
    assert await skills.tool_metadata(META) == []
    assert await skills.tool_metadata(META, skill_id=identity) == []
    assert await skills.tool_metadata(META, skill_id="local/absent") == []


async def test_skill_trusted_user_invocation_retains_author_limits(skills, tmp_path):
    identity = await install_skill(
        skills, skill_source(tmp_path / "source", extra="manual-only: true\n")
    )
    assert await skills.tool_metadata(META, skill_id=identity) == []
    row = (await skills.tool_metadata(META, skill_id=identity, invocation="user"))[0]
    assert row["model_invocable"] is False and row["user_invocable"] is True


async def test_skill_pin_metadata_survives_update_and_current_tamper_but_not_revocation(
    skills, tmp_path, monkeypatch
):
    identity = await install_skill(skills, skill_source(tmp_path / "v1", description="old"))
    pin = await skills.load("task", identity, META)
    await skills.mutate(
        "update",
        authority="owner",
        skill_id=identity,
        source=skill_source(tmp_path / "v2", version="2.0", description="new"),
        content_scope="public_synthetic",
    )
    trap_skill_effects(skills, monkeypatch)
    record = skills._manager._record(identity)
    installed = skills._manager._install_dir(record) / "SKILL.md"
    installed.write_text(installed.read_text() + "tampered bytes\n")
    old = (await skills.tool_metadata(META, skill_id=identity, task_id="task"))[0]
    assert old["integrity"] == "pinned_snapshot" and old["description"] == "old"
    assert old["digest"] == pin["digest"]
    assert old["task_pinned_version"] == {"version": "1.0", "digest": pin["digest"]}
    assert old["active_version"] == {"version": "2.0", "digest": record.digest}
    with pytest.raises(SkillServiceError, match="^Skill metadata is unavailable$"):
        await skills.tool_metadata(META, skill_id=identity)
    assert (await skills.tool_metadata(META))[0]["integrity"] == "registered_only"
    # Re-reviewing current metadata must not resurrect an old version's grant.
    await skills.mutate("scope", authority="owner", skill_id=identity, content_scope="local_only")
    await skills.mutate(
        "scope", authority="owner", skill_id=identity, content_scope="public_synthetic"
    )
    assert await skills.tool_metadata(META, skill_id=identity, task_id="task") == []


async def test_skill_metadata_cancellation_propagates(skills):
    started = asyncio.Event()

    async def inspect():
        started.set()
        return await skills.tool_metadata(LOCAL)

    async with skills._lock:
        task = asyncio.create_task(inspect())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize(
    "arguments",
    [
        {"skill_id": []},
        {"skill_id": "local/private\x00name"},
        {"task_id": True},
        {"invocation": "owner"},
    ],
)
async def test_skill_bad_metadata_queries_have_fixed_errors(skills, arguments):
    with pytest.raises(SkillServiceError) as error:
        await skills.tool_metadata(LOCAL, **arguments)
    assert str(error.value) == "Skill metadata is unavailable"


@pytest.fixture
def plugins(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "plugin_id": "demo",
                "cases": [{"input": {}, "expected": {}}],
            }
        )
    )
    service = PluginService(tmp_path / "plugins", workspace, {"demo": suite})
    monkeypatch.setattr(service, "run", forbidden)
    monkeypatch.setattr(service, "evaluate", forbidden)
    monkeypatch.setattr(service.registry, "run", forbidden)
    monkeypatch.setattr(service.registry, "evaluate", forbidden)
    yield service
    service.registry.close()
    service.controls.close()


async def approve_fixture(service, *, field="old", scope="public_synthetic", input_schema=None):
    manifest = {
        "plugin_id": "demo",
        "name": "Synthetic fixture",
        "description": field * 80,
        "author": "fixture",
        "license": "MIT",
        "input_schema": input_schema
        if input_schema is not None
        else {"type": "object", "properties": {field: {"type": "string"}}},
        "output_schema": {"type": "object"},
    }
    version = service.registry.register_bytes(b"def run(a):\n    return a\n", manifest)
    stored = service.registry.inspect(version)
    report_id = "rep-" + field
    report = {
        "passed": True,
        "version_id": version,
        "plugin_id": "demo",
        "source_sha256": stored["source_sha256"],
        "manifest_sha256": hashlib.sha256(
            PluginManifest(**manifest).canonical().encode()
        ).hexdigest(),
        "suite_sha256": suite_hash_of(service.registry.suites["demo"]),
    }
    (service.registry._reports / (report_id + ".json")).write_text(json.dumps(report))
    service.registry.promote(version, report_id, actor="owner", reviewed=True)
    await service.set_scope(version, scope, reviewed=True)
    return version


def trap_plugin_pins(service, monkeypatch):
    monkeypatch.setattr(service, "pin_task", forbidden)
    monkeypatch.setattr(service.registry, "pin", forbidden)


async def test_plugin_metadata_no_pin_or_execution_and_schemas_only_when_selected(
    plugins, monkeypatch
):
    version = await approve_fixture(plugins)
    trap_plugin_pins(plugins, monkeypatch)
    before = plugins.registry._pins_file.read_bytes()
    listed = (await plugins.tool_metadata(META))[0]
    assert listed["version_id"] == version and listed["task_pinned_version"] is None
    assert len(listed["description"]) == 80
    assert "input_schema" not in listed and "output_schema" not in listed
    selected = (await plugins.tool_metadata(META, plugin_id="demo"))[0]
    assert selected["input_schema"]["properties"] == {"old": {"type": "string"}}
    assert set(selected) == {
        "plugin_id",
        "description",
        "version_id",
        "source_sha256",
        "active_version",
        "task_pinned_version",
        "input_schema",
        "output_schema",
    }
    selected["input_schema"]["properties"].clear()
    assert (await plugins.tool_metadata(META, plugin_id="demo"))[0]["input_schema"]["properties"]
    assert plugins.registry._pins_file.read_bytes() == before and plugins._pins == {}


@pytest.mark.parametrize("pin_source", ["task", "service"])
async def test_plugin_selected_schema_is_existing_old_pin_not_active(
    plugins, monkeypatch, pin_source
):
    first = await approve_fixture(plugins)
    second = await approve_fixture(plugins, field="new")
    task = SimpleNamespace(task_id="task", plugin_versions={})
    if pin_source == "task":
        task.plugin_versions["demo"] = first
    else:
        plugins._pins["task"] = {"demo": first}
    trap_plugin_pins(plugins, monkeypatch)
    row = (await plugins.tool_metadata(META, plugin_id="demo", task=task))[0]
    assert row["version_id"] == row["task_pinned_version"] == first
    assert row["active_version"] == second
    assert set(row["input_schema"]["properties"]) == {"old"}
    await plugins.set_scope(second, "local_only", reviewed=True)
    old = (await plugins.tool_metadata(META, plugin_id="demo", task=task))[0]
    assert old["active_version"] is None and old["version_id"] == first
    assert await plugins.tool_metadata(META, plugin_id="demo") == []
    await plugins.set_scope(first, "local_only", reviewed=True)
    assert await plugins.tool_metadata(META, plugin_id="demo", task=task) == []
    await plugins.set_scope(first, "public_synthetic", reviewed=True)
    plugins.registry.disable("demo")
    assert await plugins.tool_metadata(META, plugin_id="demo", task=task) == []


@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized"])
async def test_plugin_hidden_selection_is_unknown_and_provider_scope_stays_exact(plugins, scope):
    await approve_fixture(plugins, scope=scope)
    assert await plugins.tool_metadata(META) == []
    assert await plugins.tool_metadata(META, plugin_id="demo") == []
    assert await plugins.tool_metadata(META, plugin_id="absent") == []
    assert bool(await plugins.tool_metadata(CLOUD)) == (scope == "private_cloud_authorized")


async def test_plugin_bad_pin_and_native_failure_have_constant_diagnostics(plugins, monkeypatch):
    first = await approve_fixture(plugins)
    second = await approve_fixture(plugins, field="new")
    task = SimpleNamespace(task_id="task", plugin_versions={"demo": first})
    plugins._pins["task"] = {"demo": second}
    with pytest.raises(PluginError, match="^Plugin metadata is unavailable$"):
        await plugins.tool_metadata(META, plugin_id="demo", task=task)

    def corrupt():
        raise ValueError("synthetic private exception detail")

    monkeypatch.setattr(plugins.registry, "versions", corrupt)
    with pytest.raises(PluginError) as error:
        await plugins.tool_metadata(LOCAL)
    assert str(error.value) == "Plugin metadata is unavailable"
    assert "private" not in repr(error.value)


async def test_plugin_inspection_cannot_requalify_unapproved_version_or_create_a_pin(
    plugins, monkeypatch
):
    first = await approve_fixture(plugins)
    manifest = plugins.registry.inspect(first)["manifest"]
    candidate = plugins.registry.register_bytes(b"def run(a):\n    return {}\n", manifest)
    task = SimpleNamespace(task_id="task", plugin_versions={"demo": candidate})
    trap_plugin_pins(plugins, monkeypatch)
    assert await plugins.tool_metadata(META, plugin_id="demo", task=task) == []
    assert (await plugins.tool_metadata(META, plugin_id="demo"))[0]["version_id"] == first
    assert plugins.registry._read_pins() == {}


async def test_plugin_metadata_cancellation_propagates(plugins):
    started = asyncio.Event()

    async def inspect():
        started.set()
        return await plugins.tool_metadata(LOCAL)

    async with plugins._lock:
        task = asyncio.create_task(inspect())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def configured_host(tmp_path, **kwargs):
    return mcp.MCPHost(
        [
            mcp.MCPConfig(
                id="public",
                command="synthetic-secret-command",
                enabled=True,
                args=["synthetic-private-argument"],
                allowed_tools=["echo", "unseen"],
                content_scope="public_synthetic",
            )
        ],
        ToolPolicy(tmp_path, allow_shell=True),
        **kwargs,
    )


async def test_mcp_uncached_metadata_never_starts_actor_resolves_credentials_or_discovers(
    tmp_path, monkeypatch
):
    host = configured_host(tmp_path, secret_resolver=forbidden)
    monkeypatch.setattr(host, "_actor_for", forbidden)
    monkeypatch.setattr(host, "discover", forbidden)
    monkeypatch.setattr(mcp, "ClientSession", forbidden)
    try:
        scoped = ModelScopedMCP(host, META)
        row = scoped.cached_tool_metadata()[0]
        assert row["schema_state"] == "not_discovered" and row["tools"] == []
        assert row["allowed_tools"] == ["echo", "unseen"]
        assert row["server_version"] is None and row["definition_sha256"] is None
        assert len(row["config_sha256"]) == 64 and not host._actors
        assert row["configured_enabled"] and row["enabled"] and row["transport_grant"]
        selected = scoped.cached_tool_metadata("public", tool="echo")[0]["tools"][0]
        assert selected["schema_state"] == "not_discovered"
        assert "input_schema" not in selected
        assert scoped.cached_tool_metadata("public", tool="unknown") == []
        assert scoped.cached_tool_metadata("unknown") == []
        encoded = json.dumps(row)
        for private in (
            "secret-command",
            "private-argument",
            "endpoint",
            "secret_refs",
            "credentialed",
            "content_scope",
        ):
            assert private not in encoded
    finally:
        await host.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"server_id": []},
        {"server_id": "private\x00name"},
        {"server_id": "\ud800"},
        {"tool": "echo"},
    ],
)
async def test_mcp_bad_metadata_queries_have_fixed_errors(tmp_path, arguments):
    host = configured_host(tmp_path)
    try:
        with pytest.raises(mcp.MCPError) as error:
            host.cached_tool_metadata(**arguments)
        assert str(error.value) == "MCP metadata is unavailable" and not host._actors
    finally:
        await host.close()


@pytest.fixture
def synthetic_mcp_session(monkeypatch):
    calls = []

    @asynccontextmanager
    async def transport(*args, **kwargs):
        calls.append("transport")
        yield None, None, [b""]

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            calls.append("initialize")

        async def list_tools(self):
            calls.append("list_tools")
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="echo",
                        description="d" * 200,
                        inputSchema={
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    )
                ]
            )

        call_tool = forbidden

    monkeypatch.setattr(mcp, "_bounded_stdio_transport", transport)
    monkeypatch.setattr(
        mcp,
        "_bwrap_base_argv",
        lambda config, policy, *, readonly_bindings=(): ["/bin/bash", "-c", "synthetic"],
    )
    monkeypatch.setattr(mcp, "ClientSession", Session)
    return calls


async def test_mcp_observed_cache_selection_is_complete_copied_and_reset_on_retirement(
    tmp_path, monkeypatch, synthetic_mcp_session
):
    host = configured_host(tmp_path)
    try:
        await host.discover("public")  # In-process SDK fixture, no actual server.
        assert synthetic_mcp_session == ["transport", "initialize", "list_tools"]
        monkeypatch.setattr(host, "discover", forbidden)
        scoped = ModelScopedMCP(host, META)
        listed = scoped.cached_tool_metadata("public")[0]
        assert listed["schema_state"] == "observed"
        assert len(listed["definition_sha256"]) == 64
        assert listed["tools"][0]["description"] == "d" * 80
        assert "input_schema" not in listed["tools"][0]
        selected = scoped.cached_tool_metadata("public", tool="echo")[0]
        assert selected["tools"][0]["input_schema"]["required"] == ["text"]
        selected["tools"][0]["input_schema"].clear()
        assert scoped.cached_tool_metadata("public", tool="echo")[0]["tools"][0]["input_schema"]
        assert synthetic_mcp_session == ["transport", "initialize", "list_tools"]
        await host.reset_connections(actor="owner")
        assert scoped.cached_tool_metadata("public")[0]["schema_state"] == "not_discovered"
    finally:
        await host.close()


async def test_mcp_call_schema_refresh_is_observed_without_calling_invalid_arguments(
    tmp_path, synthetic_mcp_session
):
    host = configured_host(tmp_path)
    try:
        with pytest.raises(mcp.MCPToolError, match="failed schema"):
            await host.call("public", "echo", {})
        assert (
            host.cached_tool_metadata("public", tool="echo")[0]["tools"][0]["schema_state"]
            == "observed"
        )
        assert synthetic_mcp_session == ["transport", "initialize", "list_tools"]
    finally:
        await host.close()


@pytest.mark.parametrize(
    "scope,credentialed",
    [("local_only", False), ("private_cloud_authorized", False), ("public_synthetic", True)],
)
async def test_mcp_hidden_metadata_is_not_queried_or_disclosed(
    tmp_path, monkeypatch, scope, credentialed
):
    config = mcp.MCPConfig(
        id="hidden",
        command="synthetic",
        enabled=True,
        allowed_tools=["echo"],
        content_scope=scope,
        env_secret_refs={"TOKEN": "synthetic-secret-ref"} if credentialed else {},
    )
    host = mcp.MCPHost([config], ToolPolicy(tmp_path), secret_resolver=forbidden)
    try:
        monkeypatch.setattr(host, "_actor_for", forbidden)
        scoped = ModelScopedMCP(host, META)
        assert scoped.cached_tool_metadata() == []
        monkeypatch.setattr(host, "cached_tool_metadata", forbidden)
        assert scoped.cached_tool_metadata("hidden", tool="echo") == []
        assert scoped.cached_tool_metadata("unknown", tool="echo") == []
    finally:
        await host.close()


def test_mcp_scope_is_rechecked_after_passive_projection():
    class Host:
        reads = 0

        def list_servers(self):
            self.reads += 1
            return [
                {
                    "id": "public",
                    "content_scope": "public_synthetic" if self.reads == 1 else "local_only",
                    "credentialed": False,
                }
            ]

        def cached_tool_metadata(self, *args, **kwargs):
            return [{"id": "public", "content_scope": "public_synthetic", "credentialed": False}]

    assert ModelScopedMCP(Host(), META).cached_tool_metadata("public") == []


async def test_mcp_management_block_is_reported_without_schema_or_raw_diagnostic(
    tmp_path, synthetic_mcp_session
):
    blocked = False

    def guard():
        if blocked:
            raise ValueError("synthetic private management detail")

    host = configured_host(tmp_path, access_guard=guard)
    try:
        await host.discover("public")
        blocked = True
        row = ModelScopedMCP(host, META).cached_tool_metadata("public")[0]
        assert row["configured_enabled"] is True and row["enabled"] is False
        assert row["access_state"] == "unavailable" and row["schema_state"] == "not_discovered"
        assert row["tools"] == [] and "private management" not in json.dumps(row)
    finally:
        await host.close()


def test_mcp_projection_failure_is_constant_and_cancellation_propagates():
    class Host:
        def list_servers(self):
            return [{"id": "public", "content_scope": "public_synthetic", "credentialed": False}]

        def cached_tool_metadata(self, *args, **kwargs):
            raise ValueError("synthetic private detail")

    host = Host()
    with pytest.raises(ToolFailure) as error:
        ModelScopedMCP(host, META).cached_tool_metadata("public")
    assert str(error.value) == "MCP metadata is unavailable"

    def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    host.cached_tool_metadata = cancelled
    with pytest.raises(asyncio.CancelledError):
        ModelScopedMCP(host, META).cached_tool_metadata("public")


async def test_skill_current_guard_no_loading_pinning_and_scope_revocation(
    skills, tmp_path, monkeypatch
):
    identity = await install_skill(skills, skill_source(tmp_path / "source"))
    listed = await skills.tool_metadata(META)
    selected = await skills.tool_metadata(META, skill_id=identity)
    trap_skill_effects(skills, monkeypatch)
    assert skills.tool_metadata_current(META, listed)
    assert skills.tool_metadata_current(META, selected)
    assert skills.tool_metadata_current(META, [])
    async with skills._lock:
        assert not skills.tool_metadata_current(META, selected)
        assert not skills.tool_metadata_current(META, [])
    await skills.mutate("scope", authority="owner", skill_id=identity, content_scope="local_only")
    assert not skills.tool_metadata_current(META, listed)
    assert not skills.tool_metadata_current(META, selected)
    local = await skills.tool_metadata(LOCAL, skill_id=identity)
    assert skills.tool_metadata_current(LOCAL, local)
    assert not skills.tool_metadata_current(META, local)
    assert not skills._pins and skills._pinned_bytes == 0


async def test_skill_current_guard_distinguishes_active_and_old_pinned_metadata(
    skills, tmp_path, monkeypatch
):
    identity = await install_skill(skills, skill_source(tmp_path / "old"))
    await skills.load("task", identity, META)
    original = await skills.tool_metadata(META, skill_id=identity, task_id="task")
    await skills.mutate(
        "update",
        authority="owner",
        skill_id=identity,
        source=skill_source(tmp_path / "new", version="2.0"),
        content_scope="public_synthetic",
    )
    assert not skills.tool_metadata_current(META, original, task_id="task")
    current = await skills.tool_metadata(META, skill_id=identity, task_id="task")
    trap_skill_effects(skills, monkeypatch)
    assert current[0]["version"] == "1.0" and current[0]["active_version"]["version"] == "2.0"
    assert skills.tool_metadata_current(META, current, task_id="task")
    assert not skills.tool_metadata_current(META, current)
    # A current grant cannot authorize an old pin after its separate grant is removed.
    await skills.mutate("scope", authority="owner", skill_id=identity, content_scope="local_only")
    await skills.mutate(
        "scope", authority="owner", skill_id=identity, content_scope="public_synthetic"
    )
    assert not skills.tool_metadata_current(META, current, task_id="task")


async def test_skill_current_guard_rejects_version_author_and_verified_byte_changes(
    skills, tmp_path
):
    identity = await install_skill(skills, skill_source(tmp_path / "source"))
    listed = await skills.tool_metadata(META)
    selected = await skills.tool_metadata(META, skill_id=identity)
    record = skills._manager._record(identity)
    installed = skills._manager._install_dir(record) / "SKILL.md"
    original = installed.read_text()
    installed.write_text(original + "changed bytes\n")
    assert skills.tool_metadata_current(META, listed)  # Still registered-only metadata.
    assert not skills.tool_metadata_current(META, selected)
    installed.write_text(original)
    await skills.mutate(
        "update",
        authority="owner",
        skill_id=identity,
        source=skill_source(
            tmp_path / "version2", version="2.0", extra="disable-model-invocation: true\n"
        ),
        content_scope="public_synthetic",
    )
    assert not skills.tool_metadata_current(META, listed)


async def test_skill_current_guard_checks_pinned_author_flags_and_trusted_invocation(
    skills, tmp_path
):
    identity = await install_skill(
        skills, skill_source(tmp_path / "source", extra="manual-only: true\n")
    )
    await skills.load("task", identity, META, invocation="user")
    rows = await skills.tool_metadata(META, skill_id=identity, task_id="task", invocation="user")
    assert skills.tool_metadata_current(META, rows, task_id="task", invocation="user")
    assert not skills.tool_metadata_current(META, rows, task_id="task")
    skills._pins[("task", identity)].user_invocable = False
    assert not skills.tool_metadata_current(META, rows, task_id="task", invocation="user")


@pytest.mark.parametrize("rows", [None, (), {}, [None], [{}], [{"id": "local/demo"}]])
@pytest.mark.parametrize("retained", [False, True])
async def test_skill_current_guard_rejects_malformed_rows(skills, rows, retained):
    assert not skills.tool_metadata_current(META, rows, retained=retained)


async def test_skill_current_guard_rejects_row_tampering_duplicates_and_invalid_provider(
    skills, tmp_path
):
    identity = await install_skill(skills, skill_source(tmp_path / "source"))
    rows = await skills.tool_metadata(META, skill_id=identity)
    assert not skills.tool_metadata_current(META, rows + rows)
    for key, value in (
        ("digest", "sha256:" + "0" * 64),
        ("source_revision", "invented"),
        ("model_invocable", 1),
        ("description", "\ud800"),
        ("active_version", None),
    ):
        changed = copy.deepcopy(rows)
        changed[0][key] = value
        assert not skills.tool_metadata_current(META, changed)
    assert not skills.tool_metadata_current(
        {"local": 1, "provider": "meta", "model": "fixture"}, rows
    )


async def test_plugin_current_guard_scope_disable_and_held_lock(plugins, monkeypatch):
    version = await approve_fixture(plugins)
    listed = await plugins.tool_metadata(META)
    selected = await plugins.tool_metadata(META, plugin_id="demo")
    trap_plugin_pins(plugins, monkeypatch)
    assert plugins.tool_metadata_current(META, listed)
    assert plugins.tool_metadata_current(META, selected)
    assert plugins.tool_metadata_current(META, [])
    async with plugins._lock:
        assert not plugins.tool_metadata_current(META, selected)
        assert not plugins.tool_metadata_current(META, [])
    await plugins.set_scope(version, "local_only", reviewed=True)
    assert not plugins.tool_metadata_current(META, selected)
    local = await plugins.tool_metadata(LOCAL, plugin_id="demo")
    assert plugins.tool_metadata_current(LOCAL, local)
    assert not plugins.tool_metadata_current(META, local)
    await plugins.set_scope(version, "public_synthetic", reviewed=True)
    assert plugins.tool_metadata_current(META, selected)
    plugins.registry.disable("demo")
    assert not plugins.tool_metadata_current(META, selected)
    assert plugins.registry._read_pins() == {}


async def test_plugin_current_guard_preserves_public_old_pin_and_active_version_parity(
    plugins, monkeypatch
):
    first = await approve_fixture(plugins)
    task = SimpleNamespace(task_id="task", plugin_versions={"demo": first})
    original = await plugins.tool_metadata(META, plugin_id="demo", task=task)
    second = await approve_fixture(plugins, field="new", scope="local_only")
    assert not plugins.tool_metadata_current(META, original, task=task)
    current = await plugins.tool_metadata(META, plugin_id="demo", task=task)
    trap_plugin_pins(plugins, monkeypatch)
    assert current[0]["version_id"] == first and current[0]["active_version"] is None
    assert plugins.tool_metadata_current(META, current, task=task)
    assert not plugins.tool_metadata_current(META, current)
    await plugins.set_scope(second, "public_synthetic", reviewed=True)
    assert not plugins.tool_metadata_current(META, current, task=task)
    public = await plugins.tool_metadata(META, plugin_id="demo", task=task)
    assert plugins.tool_metadata_current(META, public, task=task)
    await plugins.set_scope(first, "local_only", reviewed=True)
    assert not plugins.tool_metadata_current(META, public, task=task)


async def test_plugin_current_guard_revalidates_approval_and_integrity(plugins):
    version = await approve_fixture(plugins)
    rows = await plugins.tool_metadata(META, plugin_id="demo")
    assert plugins.tool_metadata_current(META, rows)
    report = plugins.registry._reports / "rep-old.json"
    original = report.read_text()
    data = json.loads(original)
    data["passed"] = False
    report.write_text(json.dumps(data))
    assert not plugins.tool_metadata_current(META, rows)
    report.write_text(original)
    source = plugins.registry._versions / version / "source.py"
    source.write_bytes(source.read_bytes() + b"# tampered\n")
    assert not plugins.tool_metadata_current(META, rows)


@pytest.mark.parametrize("rows", [None, (), {}, [None], [{}], [{"plugin_id": "demo"}]])
@pytest.mark.parametrize("retained", [False, True])
async def test_plugin_current_guard_rejects_malformed_rows(plugins, rows, retained):
    assert not plugins.tool_metadata_current(META, rows, retained=retained)


async def test_plugin_current_guard_strict_schema_kinds_and_bad_provider(plugins):
    await approve_fixture(plugins, input_schema={"type": "object", "maxProperties": 1})
    rows = await plugins.tool_metadata(META, plugin_id="demo")
    assert plugins.tool_metadata_current(META, rows)
    assert not plugins.tool_metadata_current(META, rows + rows)
    modified = copy.deepcopy(rows)
    modified[0]["input_schema"]["maxProperties"] = True
    assert not plugins.tool_metadata_current(META, modified)
    for value in (float("nan"), object(), "\ud800"):
        modified = copy.deepcopy(rows)
        modified[0]["input_schema"]["enum"] = [value]
        assert not plugins.tool_metadata_current(META, modified)
    bad_provider = SimpleNamespace(local=1, provider="meta", model="fixture")
    assert not plugins.tool_metadata_current(bad_provider, rows)


@pytest.mark.parametrize("selected", [False, True])
async def test_skill_retained_guard_allows_actual_same_content_load_but_not_revocation(
    skills, tmp_path, monkeypatch, selected
):
    identity = await install_skill(skills, skill_source(tmp_path / "source"))
    rows = await skills.tool_metadata(META, skill_id=identity if selected else None, task_id="task")
    assert skills.tool_metadata_current(META, rows, task_id="task")
    loaded = await skills.load("task", identity, META)
    assert loaded["digest"] == rows[0]["digest"]
    trap_skill_effects(skills, monkeypatch)
    assert not skills.tool_metadata_current(META, rows, task_id="task")
    assert skills.tool_metadata_current(META, rows, task_id="task", retained=True)
    for field, value in (
        ("description", "different"),
        ("version", "9.0"),
        ("source_revision", "new"),
    ):
        changed = copy.deepcopy(rows)
        changed[0][field] = value
        assert not skills.tool_metadata_current(META, changed, task_id="task", retained=True)
    await skills.mutate("scope", authority="owner", skill_id=identity, content_scope="local_only")
    assert not skills.tool_metadata_current(META, rows, task_id="task", retained=True)


async def test_skill_retained_guard_keeps_disclosed_historical_active_reference_and_exact_content(
    skills, tmp_path
):
    identity = await install_skill(skills, skill_source(tmp_path / "v1"))
    rows = await skills.tool_metadata(META, skill_id=identity, task_id="task")
    await skills.load("task", identity, META)
    await skills.mutate(
        "update",
        authority="owner",
        skill_id=identity,
        source=skill_source(tmp_path / "v2", version="2.0"),
        content_scope="public_synthetic",
    )
    assert not skills.tool_metadata_current(META, rows, task_id="task")
    assert skills.tool_metadata_current(META, rows, task_id="task", retained=True)
    # Replacing the task's chosen bytes invalidates the original observation.
    await skills.release_task("task")
    await skills.load("task", identity, META)
    assert not skills.tool_metadata_current(META, rows, task_id="task", retained=True)


async def test_skill_retained_guard_cannot_ignore_withdrawn_unchosen_active_reference(
    skills, tmp_path
):
    identity = await install_skill(skills, skill_source(tmp_path / "v1"))
    await skills.load("task", identity, META)
    await skills.mutate(
        "update",
        authority="owner",
        skill_id=identity,
        source=skill_source(tmp_path / "v2", version="2.0"),
        content_scope="public_synthetic",
    )
    rows = await skills.tool_metadata(META, skill_id=identity, task_id="task")
    # Owner update discards the unpinned active version's disclosure grant,
    # while preserving the public pin used by this task.
    await skills.mutate(
        "update",
        authority="owner",
        skill_id=identity,
        source=skill_source(tmp_path / "v3", version="3.0"),
        content_scope="public_synthetic",
    )
    assert rows[0]["version"] == "1.0" and rows[0]["active_version"]["version"] == "2.0"
    fresh = await skills.tool_metadata(META, skill_id=identity, task_id="task")
    assert fresh[0]["version"] == "1.0" and fresh[0]["active_version"]["version"] == "3.0"
    assert skills.tool_metadata_current(META, fresh, task_id="task", retained=True)
    assert not skills.tool_metadata_current(META, rows, task_id="task", retained=True)


@pytest.mark.parametrize("retained", [0, 1, None, "true"])
async def test_skill_retained_flag_is_trusted_strict_boolean(skills, retained):
    assert not skills.tool_metadata_current(META, [], retained=retained)


@pytest.mark.parametrize("selected", [False, True])
async def test_plugin_retained_guard_allows_actual_same_content_pin_but_not_revocation(
    plugins, monkeypatch, selected
):
    version = await approve_fixture(plugins)
    task = SimpleNamespace(task_id="task", plugin_versions={})
    rows = await plugins.tool_metadata(META, plugin_id="demo" if selected else None, task=task)
    pinned, changed = await plugins.pin_task(task, "demo", META)
    assert pinned == version and changed
    durable_pins = plugins.registry._pins_file.read_bytes()
    trap_plugin_pins(plugins, monkeypatch)
    assert not plugins.tool_metadata_current(META, rows, task=task)
    assert plugins.tool_metadata_current(META, rows, task=task, retained=True)
    assert plugins.registry._pins_file.read_bytes() == durable_pins
    for field, value in (("description", "different"), ("version_id", "v-" + "0" * 24)):
        modified = copy.deepcopy(rows)
        modified[0][field] = value
        assert not plugins.tool_metadata_current(META, modified, task=task, retained=True)
    if selected:
        modified = copy.deepcopy(rows)
        modified[0]["input_schema"]["properties"].clear()
        assert not plugins.tool_metadata_current(META, modified, task=task, retained=True)
    await plugins.set_scope(version, "local_only", reviewed=True)
    assert not plugins.tool_metadata_current(META, rows, task=task, retained=True)


async def test_plugin_retained_guard_checks_historical_active_disclosure_separately(plugins):
    first = await approve_fixture(plugins)
    task = SimpleNamespace(task_id="task", plugin_versions={})
    await plugins.pin_task(task, "demo", META)
    initial = await plugins.tool_metadata(META, plugin_id="demo", task=task)
    second = await approve_fixture(plugins, field="new")
    assert plugins.tool_metadata_current(META, initial, task=task, retained=True)
    rows = await plugins.tool_metadata(META, plugin_id="demo", task=task)
    third = await approve_fixture(plugins, field="third")
    assert first != second != third
    assert rows[0]["version_id"] == first and rows[0]["active_version"] == second
    assert not plugins.tool_metadata_current(META, rows, task=task)
    assert plugins.tool_metadata_current(META, rows, task=task, retained=True)
    await plugins.set_scope(second, "local_only", reviewed=True)
    assert not plugins.tool_metadata_current(META, rows, task=task, retained=True)
    assert plugins.tool_metadata_current(META, initial, task=task, retained=True)
    # The same-content allowance never substitutes a different selected version.
    await plugins.release_task(task.task_id)
    task.plugin_versions.clear()
    await plugins.pin_task(task, "demo", META)
    assert task.plugin_versions["demo"] == third
    assert not plugins.tool_metadata_current(META, initial, task=task, retained=True)


async def test_plugin_retained_guard_can_keep_previously_hidden_active_reference_unknown(plugins):
    await approve_fixture(plugins)
    task = SimpleNamespace(task_id="task", plugin_versions={})
    await plugins.pin_task(task, "demo", META)
    await approve_fixture(plugins, field="new", scope="local_only")
    rows = await plugins.tool_metadata(META, plugin_id="demo", task=task)
    assert rows[0]["active_version"] is None
    await approve_fixture(plugins, field="third")
    assert not plugins.tool_metadata_current(META, rows, task=task)
    assert plugins.tool_metadata_current(META, rows, task=task, retained=True)


@pytest.mark.parametrize("retained", [0, 1, None, "true"])
async def test_plugin_retained_flag_is_trusted_strict_boolean(plugins, retained):
    assert not plugins.tool_metadata_current(META, [], retained=retained)
