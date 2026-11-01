"""Independent fresh-primary ownership oracles; synthetic factories, no inference."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.policy import PolicyDenied
from packages.contracts.models import CatalogModelBinding, ProviderInfo
from packages.providers.text import ProviderCleanupError
from services.agent import model_settings as models
from services.agent.model_settings import (
    AUXILIARY_TASKS,
    HOST_PROFILE,
    AgentModelSettings,
    BoundModelSettingsChange,
)
from services.agent.task_model_lease import TaskModelLease
from services.resources.providers import ResourceBoundProvider
from services.settings.provider_factory import CatalogRecipe

_DEFAULT = object()


class MeasuredAdapter:
    structured_actions = False

    def __init__(self, info, effort=None, *, gated=False, fail=False):
        self.info, self.reasoning_effort = info, effort
        self.close_entered, self.close_release = asyncio.Event(), asyncio.Event()
        if not gated:
            self.close_release.set()
        self.fail, self.closed, self.close_calls = fail, False, 0
        self.inference_calls = []

    async def prepare_complete(self, *args, **kwargs):
        self.inference_calls.append("prepare")
        raise AssertionError("Selection cannot make a provider request")

    async def complete(self, *args, **kwargs):
        self.inference_calls.append("complete")
        raise AssertionError("Selection cannot make a provider request")

    async def close(self):
        self.close_calls += 1
        self.close_entered.set()
        await self.close_release.wait()
        if self.fail:
            raise ProviderCleanupError("Synthetic task-owned close did not settle")
        self.closed = True


class Factory:
    def __init__(self, adapters, info):
        self.adapters, self.info = adapters, info
        self.calls, self.created = [], []
        self.override = None
        self.gated = self.fail = False
        self.wrong = None

    def __call__(self, effort):
        self.calls.append(effort)
        if self.override is not None:
            return self.override
        info = (
            self.info.model_copy(update={"model": "wrong-model"})
            if self.wrong == "identity"
            else self.info
        )
        candidate = MeasuredAdapter(
            info,
            "high" if self.wrong == "reasoning" and effort is None else effort,
            gated=self.gated,
            fail=self.fail,
        )
        self.created.append(candidate)
        self.adapters.append(candidate)
        return candidate


class RoutesWitness:
    def __init__(self, actual):
        self.actual, self.calls = actual, []

    def __getattr__(self, name):
        return getattr(self.actual, name)

    def configured(self, *args):
        self.calls.append(args)
        return self.actual.configured(*args)


@pytest.fixture
async def make_native(tmp_path):
    owned, adapters = [], []

    def make(*, host_factory=_DEFAULT, local=True):
        info = ProviderInfo(provider="synthetic-native", model="host", local=local)
        base = MeasuredAdapter(info)
        adapters.append(base)
        factory, auxiliary = Factory(adapters, info), Factory(adapters, info)
        secondary = Factory(
            adapters, ProviderInfo(provider="synthetic-native", model="secondary", local=True)
        )
        configured_factory = factory if host_factory is _DEFAULT else host_factory
        settings = AgentModelSettings(
            base,
            {"secondary": secondary},
            path=tmp_path / f"models-{len(owned)}.sqlite",
            host_factory=configured_factory,
            auxiliary_host_factory=auxiliary,
            reasoning_levels={
                HOST_PROFILE: ["low", "high"] if callable(configured_factory) else [],
                "secondary": ["high"],
            },
        )
        routes = RoutesWitness(settings.auxiliary_routes)
        settings.auxiliary_routes = routes
        value = SimpleNamespace(
            settings=settings,
            base=base,
            factory=factory,
            auxiliary=auxiliary,
            secondary=secondary,
            routes=routes,
            adapters=adapters,
            resource_path=tmp_path / "unused-resource-state",
        )
        owned.append(value)
        return value

    yield make
    for adapter in adapters:
        adapter.close_release.set()
    for value in reversed(owned):
        await asyncio.gather(
            *(lease.close() for lease in tuple(value.settings.catalog_leases)),
            return_exceptions=True,
        )
        await value.settings.close()
    assert all(adapter.inference_calls == [] for adapter in adapters)


def choose(value, *, profile=HOST_PROFILE, effort=None):
    settings = value.settings
    return settings.change(
        BoundModelSettingsChange(
            schema_version="1.1",
            expected_backend=settings.backend_id,
            expected_instance=settings.instance,
            expected_revision=settings.snapshot()["revision"],
            idempotency_key=f"selection-{settings.snapshot()['revision']}",
            profile_id=profile,
            reasoning=effort,
        )
    )


def owned(value):
    return value.settings.task_selection(
        "seed-v1", "local_only", owned_primary=True, include_auxiliary=False
    )


async def settle_started(settings):
    tasks = [lease.cleanup for lease in settings.catalog_leases if lease.cleanup is not None]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)


async def test_old_defaults_still_share_host_and_capture_the_same_auxiliary_bindings(make_native):
    value = make_native()
    primary, pin, auxiliary, pins, lease = value.settings.task_selection("seed-v1", "local_only")
    again = value.settings.task_selection(
        "seed-v1", "local_only", owned_primary=False, include_auxiliary=True
    )
    assert primary is value.base and again[0] is primary
    assert pin == again[1] and auxiliary == again[2] and pins == again[3]
    assert set(auxiliary) == set(pins) == set(AUXILIARY_TASKS)
    assert all(provider is value.auxiliary.created[0] for provider in auxiliary.values())
    assert lease is None and again[4] is None
    assert value.factory.calls == [] and value.auxiliary.calls == [None]
    assert len(value.routes.calls) == 2


@pytest.mark.parametrize("name", ("owned_primary", "include_auxiliary"))
@pytest.mark.parametrize("bad", (None, 1, "true"))
async def test_selection_flags_require_actual_booleans_before_factories(make_native, name, bad):
    value = make_native()
    flags = {"owned_primary": True, "include_auxiliary": False, name: bad}
    with pytest.raises((TypeError, ValueError)):
        value.settings.task_selection("seed-v1", "local_only", **flags)
    assert value.factory.calls == value.auxiliary.calls == value.routes.calls == []
    assert value.base.close_calls == 0 and not value.settings.catalog_leases


@pytest.mark.parametrize("effort", (None, "high"))
async def test_fresh_host_primary_is_the_exact_lease_owner_until_actual_close(make_native, effort):
    value = make_native()
    if effort is not None:
        choose(value, effort=effort)
    value.factory.calls.clear()
    value.factory.gated = True
    before = value.settings.db.total_changes
    primary, pin, auxiliary, pins, lease = owned(value)
    assert type(lease) is TaskModelLease and lease in value.settings.catalog_leases
    assert lease.adapters == [primary] and primary is value.factory.created[-1]
    assert primary is not value.base
    assert all(primary is not cached for cached in value.settings.instances.values())
    assert (pin.profile_id, pin.provider, pin.model, pin.local, pin.reasoning) == (
        HOST_PROFILE,
        value.base.info.provider,
        "host",
        True,
        effort,
    )
    assert pin.settings_revision == value.settings.snapshot()["revision"]
    assert auxiliary == pins == {} and lease.auxiliary_routes == {}
    assert value.auxiliary.calls == value.routes.calls == []
    assert value.factory.calls == [effort] and value.settings.db.total_changes == before
    closing = lease.start_close()
    await asyncio.wait_for(primary.close_entered.wait(), 1)
    assert not closing.done() and lease.settled is False and lease.failed is False
    assert lease in value.settings.catalog_leases and value.settings.catalog_cleanup_busy is True
    assert primary.close_calls == 1 and value.base.close_calls == 0
    primary.close_release.set()
    await asyncio.wait_for(asyncio.shield(closing), 1)
    assert primary.closed and primary.close_calls == 1
    assert lease.settled is True and lease.failed is False
    assert lease not in value.settings.catalog_leases and lease.adapters == []


async def test_selected_named_host_profile_gets_a_fresh_adapter_not_the_resolved_cache(make_native):
    value = make_native()
    choose(value, profile="secondary", effort="high")
    cached, cached_pin = value.settings.selection("seed-v1")
    value.secondary.calls.clear()
    primary, pin, auxiliary, pins, lease = owned(value)
    assert type(lease) is TaskModelLease and lease.adapters == [primary]
    assert primary is not cached and primary is not value.settings.base["secondary"]
    assert pin == cached_pin and pin.profile_id == "secondary" and pin.reasoning == "high"
    assert value.secondary.calls == ["high"] and value.factory.calls == []
    assert auxiliary == pins == {} and value.routes.calls == []


@pytest.mark.parametrize("factory", (None, 7), ids=("missing", "not-callable"))
async def test_missing_native_host_cloning_support_is_not_a_shared_primary(make_native, factory):
    value = make_native(host_factory=factory)
    with pytest.raises((TypeError, ValueError, RuntimeError)):
        owned(value)
    await settle_started(value.settings)
    assert value.base.close_calls == 0 and value.factory.calls == []
    assert value.auxiliary.calls == value.routes.calls == []
    assert not value.settings.catalog_leases


@pytest.mark.parametrize("alias", ("base", "cached-variant", "auxiliary", "wrapped-base"))
async def test_existing_adapter_aliases_are_refused_without_closing_their_owner(make_native, alias):
    value = make_native()
    target = value.base
    if alias == "cached-variant":
        choose(value, effort="high")
        target = value.settings.selection("seed-v1")[0]
    elif alias == "auxiliary":
        target = value.settings.resolve_auxiliary({"profile_id": HOST_PROFILE, "reasoning": None})
    candidate = (
        ResourceBoundProvider(target, value.resource_path, "agent")
        if alias == "wrapped-base"
        else target
    )
    value.factory.override = candidate
    with pytest.raises((ValueError, RuntimeError)):
        owned(value)
    await settle_started(value.settings)
    assert target.close_calls == 0 and value.base.close_calls == 0
    assert all(
        target is not adapter
        for lease in value.settings.catalog_leases
        for adapter in lease.adapters
    )
    assert value.routes.calls == []


@pytest.mark.parametrize("wrapped", (False, True), ids=("exact", "fresh-wrapper"))
async def test_active_task_primary_cannot_be_adopted_by_another_lease(make_native, wrapped):
    value = make_native()
    primary, _, _, _, original_lease = owned(value)
    value.factory.override = (
        ResourceBoundProvider(primary, value.resource_path, "agent") if wrapped else primary
    )
    with pytest.raises((ValueError, RuntimeError)):
        owned(value)
    await settle_started(value.settings)
    assert original_lease in value.settings.catalog_leases
    assert original_lease.adapters == [primary] and original_lease.cleanup is None
    assert primary.close_calls == 0 and original_lease.settled is False
    assert all(
        primary is not adapter
        for lease in value.settings.catalog_leases
        if lease is not original_lease
        for adapter in lease.adapters
    )


@pytest.mark.parametrize("wrong", ("identity", "reasoning"))
async def test_fresh_candidate_is_owned_before_later_identity_validation_fails(make_native, wrong):
    value = make_native()
    value.factory.wrong, value.factory.gated = wrong, True
    with pytest.raises((ValueError, RuntimeError)):
        owned(value)
    (candidate,) = value.factory.created
    await asyncio.wait_for(candidate.close_entered.wait(), 1)
    (lease,) = value.settings.catalog_leases
    assert type(lease) is TaskModelLease and lease.adapters == [candidate]
    assert lease.cleanup is not None and not lease.cleanup.done()
    assert lease.settled is False and candidate.close_calls == 1
    assert value.base.close_calls == 0
    candidate.close_release.set()
    await asyncio.wait_for(lease.close(), 1)
    assert candidate.closed and lease.settled is True and lease.failed is False
    assert lease not in value.settings.catalog_leases


async def test_owned_cloud_refusal_never_discloses_or_closes_the_shared_base(make_native):
    value = make_native(local=False)
    with pytest.raises(PolicyDenied):
        owned(value)
    await settle_started(value.settings)
    assert value.base.close_calls == 0
    assert all(
        candidate.closed and candidate.close_calls == 1 for candidate in value.factory.created
    )
    assert not value.settings.catalog_leases
    assert value.auxiliary.calls == value.routes.calls == []


async def test_catalog_primary_stays_fresh_and_rejects_a_wrapper_of_its_live_lease(
    make_native, monkeypatch
):
    value = make_native()
    binding = CatalogModelBinding(
        id="synthetic-catalog", generation=3, configuration_sha256="c" * 64
    )
    recipe = CatalogRecipe(
        binding,
        7,
        json.dumps(
            {"kind": "lmstudio", "model": "synthetic-catalog", "base_url": "http://127.0.0.1:1/v1"}
        ),
        "none",
    )
    factory = Factory(value.adapters, recipe.info)
    opens = []

    def inspect(catalog, reference, reasoning=None):
        assert CatalogModelBinding.model_validate(reference) == binding
        return recipe

    def open_synthetic(catalog, reference, reasoning=None, *, auxiliary=False, **kwargs):
        assert CatalogModelBinding.model_validate(reference) == binding
        opens.append(auxiliary)
        return factory(recipe.reasoning), recipe

    monkeypatch.setattr(models, "inspect_recipe", inspect)
    monkeypatch.setattr(models, "open_recipe", open_synthetic)
    value.settings.change(
        BoundModelSettingsChange(
            schema_version="1.1",
            expected_backend=value.settings.backend_id,
            expected_instance=value.settings.instance,
            expected_revision=0,
            idempotency_key="choose-catalog",
            catalog_binding=binding,
        )
    )
    primary, pin, auxiliary, pins, lease = owned(value)
    assert type(lease) is TaskModelLease and lease.adapters == [primary]
    assert primary is factory.created[0] and primary is not value.base
    assert pin.catalog_binding == binding and pin.catalog_revision == 7
    assert pin.profile_id == binding.id and pin.reasoning == "none"
    assert auxiliary == pins == {} and value.routes.calls == value.auxiliary.calls == []
    assert opens == [False]
    factory.override = ResourceBoundProvider(primary, value.resource_path, "agent")
    with pytest.raises((ValueError, RuntimeError)):
        owned(value)
    await settle_started(value.settings)
    assert primary.close_calls == 0 and lease.adapters == [primary] and lease.cleanup is None


async def test_real_sixty_four_active_leases_refuse_another_factory_capture(make_native):
    value = make_native()
    captures = [owned(value) for _ in range(64)]
    assert len(value.settings.catalog_leases) == 64
    assert len({id(capture[0]) for capture in captures}) == 64
    assert all(
        type(capture[4]) is TaskModelLease and capture[4].adapters == [capture[0]]
        for capture in captures
    )
    calls = len(value.factory.calls)
    with pytest.raises((ValueError, RuntimeError)):
        owned(value)
    assert len(value.factory.calls) == calls
    assert len(value.settings.catalog_leases) == 64
    assert all(capture[0].close_calls == 0 and capture[4].cleanup is None for capture in captures)


async def test_failed_primary_close_stays_owned_and_blocks_new_selection(make_native):
    value = make_native()
    value.factory.fail = True
    primary, _, _, _, lease = owned(value)
    with pytest.raises(ProviderCleanupError):
        await lease.close()
    assert lease.cleanup.done() and lease.failed is True and lease.settled is False
    assert lease in value.settings.catalog_leases and lease.adapters == [primary]
    assert primary.close_calls == 1 and primary.closed is False
    assert value.settings.catalog_cleanup_busy is True
    calls = len(value.factory.calls)
    with pytest.raises((ValueError, RuntimeError)):
        owned(value)
    assert len(value.factory.calls) == calls and primary.close_calls == 1
    assert value.base.close_calls == 0
