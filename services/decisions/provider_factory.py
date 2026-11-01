"""Exact typed Laya recipes; inspection never constructs an adapter or key."""

import json
from dataclasses import dataclass

from packages.contracts.decisions_models import DecisionsModelSelection
from packages.contracts.models import ProviderInfo
from services.settings.provider_factory import declaration_info


@dataclass(frozen=True)
class DecisionsRecipe:
    selection: DecisionsModelSelection
    catalog_revision: int
    configuration: str

    @property
    def config(self):
        return json.loads(self.configuration)

    @property
    def info(self):
        return declaration_info(self.config)


def _recipe(catalog, selection):
    selection = DecisionsModelSelection.model_validate(selection)
    if selection.source != "catalog" or catalog is None:
        raise ValueError("Classifier catalog is unavailable")
    binding = selection.catalog_binding
    profile = catalog.profile(binding.id, binding.generation, for_new=True)
    if profile["configuration_sha256"] != binding.configuration_sha256:
        raise ValueError("Classifier catalog configuration changed")
    if profile["config"]["kind"] != "laya":
        raise ValueError("Only typed Laya catalog classifiers are supported")
    return DecisionsRecipe(
        selection,
        catalog._revision()[0],
        json.dumps(profile["config"], sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    )


def inspect_recipe(catalog, selection):
    if catalog is None:
        raise ValueError("Classifier catalog is unavailable")
    catalog.db.execute("BEGIN")
    try:
        recipe = _recipe(catalog, selection)
        catalog.db.commit()
        return recipe
    except BaseException:
        catalog.db.rollback()
        raise


def open_recipe(catalog, selection, *, resource_state=None):
    """Capture declaration and credential generation together at real admission."""
    from packages.providers.laya import LayaRoutingProvider

    if catalog is None:
        raise ValueError("Classifier catalog is unavailable")
    catalog.db.execute("BEGIN")
    try:
        recipe = _recipe(catalog, selection)
        config = recipe.config
        credential = config.get("credential")
        key = (
            ""
            if credential is None
            else catalog.credential(credential["id"], credential["generation"], for_new=True)
        )
        catalog.db.commit()
    except BaseException:
        catalog.db.rollback()
        raise
    adapter = LayaRoutingProvider(
        config["base_url"],
        config["model"],
        api_key=key,
        allow_cloud=config["allow_cloud"],
        timeout=config["timeout_s"],
        max_len=config["max_len"],
        head_max_len=config["head_max_len"],
        expected_artifact_sha256=config["expected_artifact_sha256"],
    )
    key = None
    provider = adapter
    if resource_state is not None:
        from services.resources.providers import ResourceBoundProvider

        provider = ResourceBoundProvider(adapter, resource_state, "decisions")
    return provider, adapter, recipe


def provider_info(provider):
    info = ProviderInfo.model_validate(provider.info.model_dump(), strict=True)
    for value in (info.provider, info.model):
        if not 1 <= len(value.encode("utf-8")) <= 256 or any(
            ord(c) < 32 or ord(c) == 127 for c in value
        ):
            raise ValueError("Classifier declaration is unavailable")
    return info
