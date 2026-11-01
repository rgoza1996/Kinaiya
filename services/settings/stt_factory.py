"""Exact buffered STT recipes; inspect/save never import a credential or adapter."""

import json
from dataclasses import dataclass

from packages.contracts.models import CatalogModelBinding
from services.settings.provider_catalog import MetaSTTProfile
from services.settings.provider_factory import declaration_info


@dataclass(frozen=True)
class STTRecipe:
    binding: CatalogModelBinding
    catalog_revision: int
    configuration: str

    @property
    def config(self):
        return json.loads(self.configuration)

    @property
    def info(self):
        return declaration_info(self.config)


def _recipe(catalog, binding):
    binding = CatalogModelBinding.model_validate(binding)
    profile = catalog.profile(binding.id, binding.generation, for_new=True)
    if profile["configuration_sha256"] != binding.configuration_sha256:
        raise ValueError("STT catalog configuration identity changed")
    config = MetaSTTProfile.model_validate(profile["config"]).model_dump()
    return STTRecipe(
        binding,
        catalog._revision()[0],
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def inspect_stt_recipe(catalog, binding):
    if catalog is None:
        raise ValueError("Provider catalog is unavailable")
    catalog.db.execute("BEGIN")
    try:
        recipe = _recipe(catalog, binding)
        catalog.db.commit()
        return recipe
    except BaseException:
        catalog.db.rollback()
        raise


def open_stt_recipe(catalog, binding):
    """Capture one declaration/key generation together, then construct locally."""
    if catalog is None:
        raise ValueError("Provider catalog is unavailable")
    key = None
    catalog.db.execute("BEGIN")
    try:
        recipe = _recipe(catalog, binding)
        config = recipe.config
        reference = config["credential"]
        key = catalog.credential(reference["id"], reference["generation"], for_new=True)
        catalog.db.commit()
    except BaseException:
        catalog.db.rollback()
        raise
    try:
        from packages.providers.meta_speech import MetaSTTClient

        adapter = MetaSTTClient(
            base_url=config["base_url"],
            model=config["model"],
            allow_cloud=True,
            timeout_s=config["timeout_s"],
            api_key=key,
        )
        return adapter, recipe
    finally:
        key = None
