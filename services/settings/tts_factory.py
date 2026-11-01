"""Exact speech recipes; metadata never constructs an adapter or resolves a key."""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from packages.contracts.models import CatalogModelBinding
from packages.providers.openrouter_budget import CharacterBudget
from services.settings.provider_catalog import OpenRouterTTSProfile
from services.settings.provider_factory import declaration_info


@dataclass(frozen=True, repr=False)
class TTSBudgetPolicy:
    path: Path
    limit: object
    workspace: Path

    def validate(self):
        path = Path(os.path.abspath(self.path))
        if path != path.resolve() or path.is_relative_to(self.workspace.resolve()):
            raise ValueError("Retained speech budget is unavailable")
        directory = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            parent = os.fstat(directory)
            if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                raise ValueError("Retained speech budget is unavailable")
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                ):
                    raise ValueError("Retained speech budget is unavailable")
            finally:
                os.close(fd)
            # This is a bounded existing-ledger read; no lock file, allowance,
            # reservation, adapter or provider connection is created here.
            budget = CharacterBudget(path, limit_usd=self.limit, require_existing=True)
            budget._read(directory)
        finally:
            os.close(directory)


def budget_available(policy):
    if policy is None:
        return False
    try:
        policy.validate()
        return True
    except Exception:
        return False


@dataclass(frozen=True)
class TTSRecipe:
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
        raise ValueError("TTS catalog configuration identity changed")
    config = OpenRouterTTSProfile.model_validate(profile["config"]).model_dump()
    return TTSRecipe(
        binding,
        catalog._revision()[0],
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def inspect_tts_recipe(catalog, binding):
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


def open_tts_recipe(catalog, binding, budget_policy):
    if not budget_available(budget_policy) or catalog is None:
        raise ValueError("A retained native speech budget is required")
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
        from packages.providers.openrouter_speech import OpenRouterTTSClient

        adapter = OpenRouterTTSClient(
            base_url=config["base_url"],
            model=config["model"],
            voice=config["voice"],
            allow_cloud=True,
            timeout_s=config["timeout_s"],
            api_key=key,
            budget_path=budget_policy.path,
            budget_limit_usd=budget_policy.limit,
            require_existing_budget=True,
        )
        return adapter, recipe
    finally:
        key = None
