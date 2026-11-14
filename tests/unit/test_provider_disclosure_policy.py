"""Independent disclosure-ceiling oracles; no inference or external services.

Requirements frozen in provider-policy-v1 before the implementation was read.
The existing standing-export vectors are public synthetic packets, while the
passive plugin/MCP doubles supply metadata, never policy or execution authority.
"""

import copy
import hashlib
import json
from types import MappingProxyType, SimpleNamespace

import pytest
from pydantic import ValidationError
from test_standing_export_contract import (
    GOLDEN_EXPORT_DIGEST,
    admission_vector,
    request_vector,
)

from core.plugins import PluginError
from core.policy import PolicyDenied
from packages.commands import Registry
from packages.contracts.agent_export import AgentExportProviderInfo, AgentExportRequest
from packages.contracts.models import ProviderInfo
from packages.contracts.standing_export import (
    StandingExportAdmission,
    StandingExportRequest,
    standing_export_digest,
)
from packages.providers.disclosure import entry_allowed, public_only_provider, scope_allowed
from services.agent.mcp_tools import ModelScopedMCP
from services.agent.plugin_service import PluginService
from services.agent.skill_service import SkillService, SkillServiceError
from services.agent.tools import ToolFailure

MARKER = "SYNTHETIC-UNTRUSTED-POLICY-SENTINEL"
SCOPES = ("local_only", "public_synthetic", "private_cloud_authorized")
LEGACY_PROVIDER_KEYS = {"schema_version", "provider", "model", "local", "streaming"}


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def metadata(*, local=False, provider="synthetic-runtime", model="synthetic-model", **extra):
    return SimpleNamespace(local=local, provider=provider, model=model, **extra)


def provider(*, local=False, policy="public_synthetic_only", **changes):
    values = dict(provider="synthetic-runtime", model="synthetic-model", local=local)
    values.update(changes)
    return ProviderInfo(**values, disclosure_policy=policy)


@pytest.fixture(
    params=[
        ("synthetic-runtime", "synthetic-model", "public_synthetic_only"),
        ("meta", "synthetic-alias", "owner_authorized"),
        ("synthetic-runtime", "synthetic-CONTRIBUTOR", "owner_authorized"),
    ],
    ids=["explicit-ceiling", "meta-ceiling-not-relaxed", "contributor-ceiling-not-relaxed"],
)
def public_info(request):
    name, model, policy = request.param
    return provider(policy=policy, provider=name, model=model)


@pytest.mark.parametrize(
    "name,model,local",
    [
        ("synthetic-runtime", "synthetic-local", True),
        ("synthetic-runtime", "synthetic-cloud", False),
        ("meta", "synthetic-alias", False),
        ("synthetic-runtime", "SyNtHeTiC-CoNtRiBuToR", False),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
def test_legacy_provider_wire_bytes_and_digest_survive_absent_or_explicit_null(
    name, model, local, streaming
):
    old = {
        "schema_version": "1.0",
        "provider": name,
        "model": model,
        "local": local,
        "streaming": streaming,
    }
    old_json = json.dumps(old, ensure_ascii=False, separators=(",", ":"))
    old_canonical = canonical(old)
    for supplied in (old, {**old, "disclosure_policy": None}):
        parsed = ProviderInfo.model_validate(supplied)
        for reconstructed in (
            parsed,
            ProviderInfo.model_validate(parsed.model_dump()),
            ProviderInfo.model_validate_json(parsed.model_dump_json()),
        ):
            assert reconstructed.disclosure_policy is None
            assert reconstructed.model_dump() == old
            assert reconstructed.model_dump(mode="json") == old
            assert reconstructed.model_dump_json() == old_json
            assert canonical(reconstructed.model_dump()) == old_canonical
            assert (
                hashlib.sha256(canonical(reconstructed.model_dump())).digest()
                == hashlib.sha256(old_canonical).digest()
            )
            assert set(reconstructed.model_dump()) == LEGACY_PROVIDER_KEYS


@pytest.mark.parametrize("policy", ["public_synthetic_only", "owner_authorized"])
def test_explicit_policy_survives_provider_and_export_binding_reconstruction(policy):
    original = provider(policy=policy)
    wire = original.model_dump()
    assert wire["disclosure_policy"] == policy
    for rebuilt in (
        ProviderInfo.model_validate(wire),
        ProviderInfo.model_validate_json(original.model_dump_json()),
        AgentExportProviderInfo.model_validate(wire),
    ):
        assert rebuilt.disclosure_policy == policy
        assert rebuilt.model_dump() == wire
    fields = request_vector()["export"]
    fields["provider"] = original
    request = AgentExportRequest.model_validate(fields)
    assert request.model_dump()["provider"]["disclosure_policy"] == policy
    assert request.binding_digest() == hashlib.sha256(canonical(request.model_dump())).hexdigest()
    old = copy.deepcopy(fields)
    old["provider"] = original.model_dump(exclude={"disclosure_policy"})
    assert AgentExportRequest.model_validate(old).binding_digest() != request.binding_digest()


@pytest.mark.parametrize(
    "invalid", [MARKER, "PUBLIC_SYNTHETIC_ONLY", "", True, 1, [], {}, ["owner_authorized"]]
)
def test_policy_contract_rejects_malformed_literals_and_types(invalid):
    for kind in (ProviderInfo, AgentExportProviderInfo):
        with pytest.raises(ValidationError) as error:
            kind(
                provider="synthetic-runtime",
                model="synthetic-model",
                local=False,
                disclosure_policy=invalid,
            )
        # Pydantic's caller-owned diagnostic envelope is unchanged. Non-reflection
        # is required of helper/component boundaries below, not global errors.
        diagnostics = error.value.errors(include_input=False)
        assert any(item["loc"] == ("disclosure_policy",) for item in diagnostics)
        assert MARKER not in str(diagnostics)


@pytest.mark.parametrize(
    "name,model,policy,expected",
    [
        ("synthetic-runtime", "synthetic-model", None, False),
        ("synthetic-runtime", "synthetic-model", "owner_authorized", False),
        ("synthetic-runtime", "synthetic-model", "public_synthetic_only", True),
        ("meta", "synthetic-alias", None, True),
        ("meta", "synthetic-alias", "owner_authorized", True),
        ("synthetic-runtime", "ALIAS-ConTRiBuToR", None, True),
        ("synthetic-runtime", "ALIAS-ConTRiBuToR", "owner_authorized", True),
    ],
)
def test_shared_predicate_uses_native_ceiling_for_attributes_and_mapping(
    name, model, policy, expected
):
    raw = dict(provider=name, model=model, local=False)
    for values in (raw, {**raw, "disclosure_policy": policy}):
        if values is raw and policy is not None:
            continue
        assert public_only_provider(values) is expected
        assert public_only_provider(MappingProxyType(values)) is expected
        assert public_only_provider(SimpleNamespace(**values)) is expected


@pytest.mark.parametrize(
    "change",
    [
        {"provider": None},
        {"provider": 1},
        {"provider": ""},
        {"model": None},
        {"model": []},
        {"model": ""},
        {"disclosure_policy": MARKER},
        {"disclosure_policy": True},
        {"disclosure_policy": []},
        {"disclosure_policy": {}},
    ],
)
def test_invalid_shared_metadata_has_constant_non_reflecting_refusal(change):
    valid = dict(provider="synthetic-runtime", model="synthetic-model", local=False)
    errors = []
    for values in ({**valid, **change}, {**valid, "disclosure_policy": MARKER}):
        for supplied in (values, MappingProxyType(values), SimpleNamespace(**values)):
            with pytest.raises(ValueError) as error:
                public_only_provider(supplied)
            errors.append(str(error.value))
            assert MARKER not in str(error.value)
    assert len(set(errors)) == 1


def test_shared_metadata_requires_exact_strings_not_string_subclasses():
    class ForeignString(str):
        pass

    for field, value in (
        ("provider", ForeignString("synthetic-runtime")),
        ("model", ForeignString("synthetic-model")),
        ("disclosure_policy", ForeignString("public_synthetic_only")),
    ):
        raw = dict(provider="synthetic-runtime", model="synthetic-model", local=False)
        raw[field] = value
        with pytest.raises(ValueError):
            public_only_provider(raw)


def test_missing_shared_identity_is_refused_without_foreign_object_stringification():
    class Foreign:
        def __str__(self):
            pytest.fail("Untrusted metadata must not be formatted")

        def __repr__(self):
            pytest.fail("Untrusted metadata must not be formatted")

    for supplied in ({}, {"provider": "synthetic-runtime"}, Foreign()):
        with pytest.raises(ValueError) as error:
            public_only_provider(supplied)
        assert MARKER not in str(error.value)


@pytest.mark.parametrize("policy", [None, "public_synthetic_only", "owner_authorized"])
@pytest.mark.parametrize("local", [False, True])
def test_provider_ceiling_never_creates_consent_and_legacy_local_scope_is_preserved(policy, local):
    info = provider(local=local, policy=policy)
    for scope in SCOPES:
        expected = (
            local
            or scope == "public_synthetic"
            or (policy != "public_synthetic_only" and scope == "private_cloud_authorized")
        )
        assert scope_allowed(info, scope) is expected
        assert entry_allowed(info, SimpleNamespace(payload={"content_scope": scope})) is expected
    for malformed in (None, "cloud", "", True, 0, [], {}):
        assert not scope_allowed(info, malformed)
    assert entry_allowed(info, SimpleNamespace(payload={})) is local


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("scope", SCOPES)
def test_malformed_policy_denies_even_when_input_has_positive_scope(local, scope):
    info = metadata(local=local, disclosure_policy=MARKER)
    assert not scope_allowed(info, scope)
    assert not entry_allowed(info, SimpleNamespace(payload={"content_scope": scope}))


@pytest.mark.parametrize(
    "name,model", [("meta", "synthetic-alias"), ("synthetic-runtime", "CONTRIBUTOR-alias")]
)
def test_owner_authorized_cannot_relax_existing_contributor_scope_floor(name, model):
    info = provider(policy="owner_authorized", provider=name, model=model)
    assert scope_allowed(info, "public_synthetic")
    assert not scope_allowed(info, "private_cloud_authorized")
    assert not scope_allowed(info, "local_only")


@pytest.mark.parametrize(
    "kind,factory,nested",
    [
        (StandingExportRequest, request_vector, "export"),
        (StandingExportAdmission, admission_vector, "admission"),
    ],
)
def test_nested_legacy_packets_retain_exact_raw_dump_json_canonical_bytes(kind, factory, nested):
    raw = factory()
    expected = canonical(raw)
    assert set(raw[nested]["provider"]) == LEGACY_PROVIDER_KEYS
    explicit_null = copy.deepcopy(raw)
    explicit_null[nested]["provider"]["disclosure_policy"] = None
    for supplied in (raw, explicit_null):
        for parsed in (
            kind.model_validate(supplied),
            kind.from_json(canonical(supplied)),
            kind.model_validate_json(canonical(supplied)),
        ):
            assert parsed.canonical_bytes() == expected
            assert parsed.model_dump(mode="json") == raw
            assert canonical(json.loads(parsed.model_dump_json())) == expected
            assert getattr(parsed, nested).provider.disclosure_policy is None
            assert kind.model_validate(parsed.model_dump()).canonical_bytes() == expected
            assert kind.model_validate(parsed).canonical_bytes() == expected
    request = StandingExportRequest.model_validate(request_vector())
    assert standing_export_digest(request) == GOLDEN_EXPORT_DIGEST
    assert StandingExportAdmission.model_validate(admission_vector()).validate_request(
        request
    ).canonical_bytes() == canonical(admission_vector())


@pytest.mark.parametrize("policy", ["public_synthetic_only", "owner_authorized"])
def test_nested_explicit_policy_changes_and_survives_both_canonical_bindings(policy):
    raw = request_vector()
    raw["export"]["provider"]["disclosure_policy"] = policy
    original = StandingExportRequest.model_validate(raw)
    expected_digest = hashlib.sha256(b"kinaiya.standing.export.v1\0" + canonical(raw)).hexdigest()
    assert standing_export_digest(original) == expected_digest != GOLDEN_EXPORT_DIGEST
    admitted_raw = admission_vector(raw)
    admitted = StandingExportAdmission.model_validate(admitted_raw)
    assert admitted.validate_request(original).canonical_bytes() == canonical(admitted_raw)
    for kind, packet, nested in (
        (StandingExportRequest, raw, "export"),
        (StandingExportAdmission, admitted_raw, "admission"),
    ):
        for parsed in (kind.model_validate(packet), kind.from_json(canonical(packet))):
            assert parsed.canonical_bytes() == canonical(packet)
            assert getattr(parsed, nested).provider.disclosure_policy == policy
            assert kind.model_validate(parsed.model_dump()).canonical_bytes() == canonical(packet)
    # A receipt binding the old five-key provider cannot admit the new policy.
    with pytest.raises(ValueError):
        StandingExportAdmission.model_validate(admission_vector()).validate_request(original)


@pytest.mark.parametrize(
    "kind,factory,nested",
    [
        (StandingExportRequest, request_vector, "export"),
        (StandingExportAdmission, admission_vector, "admission"),
    ],
)
def test_legacy_exception_never_fills_any_old_provider_or_enclosing_field(kind, factory, nested):
    original = factory()
    for path in ((nested,), (nested, "provider")):
        container = original
        for key in path:
            container = container[key]
        for missing in container:
            raw = copy.deepcopy(original)
            target = raw
            for key in path:
                target = target[key]
            target.pop(missing)
            with pytest.raises(ValueError):
                kind.model_validate(raw)
            with pytest.raises(ValueError):
                kind.from_json(canonical(raw))
    bad = copy.deepcopy(original)
    bad[nested]["provider"]["unknown"] = MARKER
    with pytest.raises(ValueError) as error:
        kind.model_validate(bad)
    assert MARKER not in str(error.value)


def test_typed_graphs_do_not_receive_raw_legacy_normalization():
    request = StandingExportRequest.model_validate(request_vector())
    admitted = StandingExportAdmission.model_validate(admission_vector())

    class ProviderSubclass(AgentExportProviderInfo):
        pass

    original = request.export.provider
    missing = original.model_copy(deep=True)
    missing.__dict__.pop("disclosure_policy")
    wrongs = [
        missing,
        original.model_dump(),
        ProviderInfo.model_validate(original.model_dump()),
        ProviderSubclass.model_validate(original.model_dump()),
        original.model_copy(update={"unknown": MARKER}),
        original.model_copy(update={"disclosure_policy": MARKER}),
    ]
    for wrong in wrongs:
        bad = request.model_copy(
            update={"export": request.export.model_copy(update={"provider": wrong})}
        )
        for invoke in (
            lambda bad=bad: bad.canonical_bytes(),
            lambda bad=bad: standing_export_digest(bad),
            lambda bad=bad: admitted.validate_request(bad),
        ):
            with pytest.raises(ValueError) as error:
                invoke()
            assert MARKER not in str(error.value)
    assert request.canonical_bytes() == canonical(request_vector())


async def install_skill(service, directory, name, scope):
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Synthetic {name} metadata\nversion: 1.0.0\n---\n"
        f"Public synthetic test instruction for {name}.\n",
        encoding="utf-8",
    )
    result = await service.mutate(
        "install", authority="owner", source=directory, content_scope=scope
    )
    await service.mutate("enable", authority="owner", skill_id=result["id"])
    return result["id"]


@pytest.fixture
async def skills(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = SkillService(tmp_path / "skills", workspace)
    ids = {}
    try:
        for name, scope in zip(
            ("localfixture", "publicfixture", "privatefixture"), SCOPES, strict=True
        ):
            ids[scope] = await install_skill(service, tmp_path / name, name, scope)
        yield service, ids
    finally:
        service.close()


@pytest.mark.parametrize("representation", ["attributes", "dict", "readonly_mapping"])
async def test_skill_metadata_and_instruction_exports_obey_unfamiliar_public_only_ceiling(
    skills, representation, public_info
):
    service, ids = skills
    info = public_info
    if representation == "dict":
        info = info.model_dump()
    elif representation == "readonly_mapping":
        info = MappingProxyType(info.model_dump())
    page = await service.catalog(info)
    assert [row["id"] for row in page["skills"]] == [ids["public_synthetic"]]
    loaded = await service.load("synthetic-task", ids["public_synthetic"], info)
    assert "Public synthetic test instruction" in loaded["body"]
    for scope in ("local_only", "private_cloud_authorized"):
        with pytest.raises(SkillServiceError):
            await service.load("synthetic-task", ids[scope], info)
    ordinary = provider(policy="owner_authorized")
    assert {row["id"] for row in (await service.catalog(ordinary))["skills"]} == {
        ids["public_synthetic"],
        ids["private_cloud_authorized"],
    }
    assert {row["id"] for row in (await service.catalog(provider(local=True)))["skills"]} == set(
        ids.values()
    )


@pytest.mark.parametrize("local", [False, True])
async def test_bad_policy_is_a_typed_skill_refusal_without_exported_marker(skills, local):
    service, ids = skills
    info = metadata(local=local, disclosure_policy=MARKER)
    for invoke in (
        lambda: service.catalog(info),
        lambda: service.load("synthetic-task", ids["public_synthetic"], info),
    ):
        with pytest.raises(SkillServiceError) as error:
            await invoke()
        assert MARKER not in str(error.value)


async def test_failed_skill_scope_write_stays_local_and_pending_restart_fence_survives(
    skills, monkeypatch
):
    service, ids = skills

    def refuse_write():
        raise SkillServiceError("Synthetic persistence failure")

    monkeypatch.setattr(service, "_write_scopes", refuse_write)
    with pytest.raises(SkillServiceError, match="Synthetic persistence failure"):
        await service.mutate(
            "scope",
            authority="owner",
            skill_id=ids["public_synthetic"],
            content_scope="public_synthetic",
        )
    assert (await service.catalog(provider()))["skills"] == []
    with pytest.raises(SkillServiceError):
        await service.load("synthetic-task", ids["public_synthetic"], provider())
    root, workspace = service._root, service._workspace
    service.close()
    with pytest.raises(SkillServiceError, match="Unfinished skill scope change"):
        SkillService(root, workspace)


async def test_bare_command_visibility_uses_same_ceiling_without_dispatch(skills, public_info):
    service, ids = skills
    runtime = SimpleNamespace(
        skill_service=service,
        provider=SimpleNamespace(info=public_info),
        journal=object(),
        model_settings=None,
    )
    registry = Registry(runtime)
    try:
        await registry.refresh_skill_commands()
        rows = {
            row["origin"]: row
            for row in registry.definitions()["commands"]
            if row["execution_type"] == "skill_invocation" and row["origin"] != "kinaiya-core"
        }
        assert set(rows) == set(ids.values())
        assert {name for name, row in rows.items() if row["available"]} == {ids["public_synthetic"]}
        runtime.provider.info = provider(policy="owner_authorized")
        await registry.refresh_skill_commands()
        assert set(registry._skill_targets.values()) == {
            ids["public_synthetic"],
            ids["private_cloud_authorized"],
        }
        for local in (False, True):
            runtime.provider.info = metadata(local=local, disclosure_policy=MARKER)
            await registry.refresh_skill_commands()
            assert registry._skill_targets == {}
            assert all(
                not row["available"]
                for row in registry.definitions()["commands"]
                if row["origin"] in ids.values()
            )
    finally:
        registry.close()


@pytest.mark.parametrize("mapping_type", [dict, MappingProxyType], ids=["dict", "readonly_mapping"])
async def test_configured_command_policy_refusal_keeps_rows_unavailable_and_hides_input(
    skills, public_info, mapping_type
):
    service, ids = skills
    configured = public_info.model_dump()
    runtime = SimpleNamespace(
        skill_service=service,
        provider=SimpleNamespace(info=provider(local=True)),
        journal=object(),
        model_settings=SimpleNamespace(default_info=lambda: mapping_type(configured)),
    )
    registry = Registry(runtime)
    try:
        # The configured role wins over the fallback local provider. A valid
        # public-only configuration still exposes its public skill command.
        await registry.refresh_skill_commands()
        before = {
            row["origin"]: row
            for row in registry.definitions()["commands"]
            if row["origin"] in ids.values()
        }
        assert set(before) == set(ids.values())
        assert {identity for identity, row in before.items() if row["available"]} == {
            ids["public_synthetic"]
        }
        assert set(registry._skill_targets.values()) == {ids["public_synthetic"]}

        configured["disclosure_policy"] = MARKER
        await registry.refresh_skill_commands()
        after = {
            row["origin"]: row
            for row in registry.definitions()["commands"]
            if row["origin"] in ids.values()
        }
        assert set(after) == set(before)
        assert all(not row["available"] for row in after.values())
        assert registry._skill_targets == {}
        assert MARKER not in json.dumps(registry.definitions())
    finally:
        registry.close()


class PassiveMCP:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def list_servers(self):
        return copy.deepcopy(self.rows)

    def cached_tool_metadata(self, server_id=None, *, tool=None):
        return [
            {**row, "name": "synthetic_tool"}
            for row in self.list_servers()
            if server_id is None or row["id"] == server_id
        ]

    async def discover(self, server):
        self.calls.append(("discover", server))
        return [{"name": "synthetic_tool"}]

    async def call(self, server, tool, arguments):
        self.calls.append(("call", server))
        return {"content": [{"text": "Public synthetic result"}]}


def mcp_rows():
    return [
        {"id": "synthetic-public", "content_scope": "public_synthetic", "credentialed": False},
        {
            "id": "synthetic-private",
            "content_scope": "private_cloud_authorized",
            "credentialed": False,
        },
        {"id": "synthetic-local", "content_scope": "local_only", "credentialed": False},
        {"id": "synthetic-credentialed", "content_scope": "public_synthetic", "credentialed": True},
        {"id": "synthetic-unknown-credential", "content_scope": "public_synthetic"},
        {"id": "synthetic-unknown-scope", "credentialed": False},
    ]


@pytest.mark.parametrize("readonly_mapping", [False, True])
async def test_unfamiliar_public_only_mcp_filters_before_transport_and_passive_projection(
    public_info, readonly_mapping
):
    host = PassiveMCP(mcp_rows())
    info = MappingProxyType(public_info.model_dump()) if readonly_mapping else public_info
    view = ModelScopedMCP(host, info)
    assert [row["id"] for row in view.list_servers()] == ["synthetic-public"]
    cached = view.cached_tool_metadata()
    assert [row["id"] for row in cached] == ["synthetic-public"]
    assert all("credentialed" not in row and "content_scope" not in row for row in cached)
    for row in mcp_rows()[1:]:
        with pytest.raises(PolicyDenied):
            await view.discover(row["id"])
        with pytest.raises(PolicyDenied):
            await view.call(row["id"], "synthetic_tool", {})
    assert host.calls == []
    await view.discover("synthetic-public")
    await view.call("synthetic-public", "synthetic_tool", {})
    assert host.calls == [("discover", "synthetic-public"), ("call", "synthetic-public")]
    assert len(ModelScopedMCP(host, provider(local=True)).list_servers()) == len(mcp_rows())
    assert {
        row["id"]
        for row in ModelScopedMCP(host, provider(policy="owner_authorized")).list_servers()
    } == {
        "synthetic-public",
        "synthetic-private",
        "synthetic-credentialed",
        "synthetic-unknown-credential",
    }


@pytest.mark.parametrize("local", [False, True])
async def test_invalid_mcp_policy_never_contacts_host_or_exports_metadata(local):
    host = PassiveMCP(mcp_rows())
    try:
        view = ModelScopedMCP(host, metadata(local=local, disclosure_policy=MARKER))
    except (PolicyDenied, ToolFailure) as error:
        assert MARKER not in str(error)
    else:
        assert view.list_servers() == []
        assert view.cached_tool_metadata() == []
        with pytest.raises((PolicyDenied, ToolFailure)) as error:
            await view.call("synthetic-public", "synthetic_tool", {})
        assert MARKER not in str(error.value)
    assert host.calls == []


async def test_mcp_scope_revocation_during_response_prevents_model_delivery():
    class RevokingMCP(PassiveMCP):
        async def call(self, server, tool, arguments):
            result = await super().call(server, tool, arguments)
            self.rows[0]["content_scope"] = "private_cloud_authorized"
            return result

    host = RevokingMCP(mcp_rows())
    view = ModelScopedMCP(host, provider())
    with pytest.raises(PolicyDenied):
        await view.call("synthetic-public", "synthetic_tool", {})
    assert host.calls == [("call", "synthetic-public")]


@pytest.fixture
async def plugins(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = PluginService(tmp_path / "plugins", workspace, {})
    version = "v-" + "a" * 24
    # No plugin source, evaluator, promotion or executable candidate is created.
    # This passive native inventory lets the real catalog apply policy/scopes.
    row = {
        "version_id": version,
        "source_sha256": "ab" * 32,
        "manifest": {"plugin_id": "synthetic", "description": "Public synthetic schema"},
    }
    monkeypatch.setattr(service.registry, "versions", lambda: [row])
    monkeypatch.setattr(service.registry, "inspect", lambda requested: copy.deepcopy(row))
    monkeypatch.setattr(service.registry, "active", lambda identity: version)
    try:
        yield service, version
    finally:
        await service.close()


@pytest.mark.parametrize("readonly_mapping", [False, True])
async def test_plugin_catalog_keeps_policy_scope_pending_and_local_boundaries(
    plugins, public_info, readonly_mapping
):
    service, version = plugins
    info = MappingProxyType(public_info.model_dump()) if readonly_mapping else public_info
    for scope in ("local_only", "private_cloud_authorized"):
        await service.set_scope(version, scope, reviewed=True)
        assert (await service.catalog(info))["plugins"] == []
        with pytest.raises(PluginError):
            await service.catalog(info, plugin_id="synthetic")
    assert (await service.catalog(provider(policy="owner_authorized")))["plugins"][0][
        "plugin_id"
    ] == "synthetic"
    await service.set_scope(version, "public_synthetic", reviewed=True)
    assert (await service.catalog(info))["plugins"][0]["plugin_id"] == "synthetic"
    service.scope_pending.write_text("Public synthetic unresolved scope write")
    assert (await service.catalog(info))["plugins"] == []
    with pytest.raises(PluginError):
        await service.catalog(info, plugin_id="synthetic")
    assert (await service.catalog(provider(local=True)))["plugins"][0]["plugin_id"] == "synthetic"


@pytest.mark.parametrize("local", [False, True])
async def test_malformed_plugin_policy_is_unavailable_not_an_unhandled_value_error(plugins, local):
    service, version = plugins
    await service.set_scope(version, "public_synthetic", reviewed=True)
    info = metadata(local=local, disclosure_policy=MARKER)
    assert (await service.catalog(info))["plugins"] == []
    with pytest.raises(PluginError) as error:
        await service.catalog(info, plugin_id="synthetic")
    assert MARKER not in str(error.value)
