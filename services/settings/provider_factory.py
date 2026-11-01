"""Native measured text recipes; inspection never constructs adapters or keys."""

import json
from dataclasses import dataclass

from packages.contracts.models import CatalogModelBinding, ProviderInfo

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


class UnavailableHostProvider:
    """Owner control plane survives a missing host adapter without inference.

    There is deliberately no synthetic provider identity or locality. Native
    model settings exclude this marker from selectable host profiles.
    """

    def __init__(self, role="Agent"):
        self.role = role

    @property
    def info(self):
        from packages.providers.text import ProviderError

        raise ProviderError(f"Host {self.role} profile is unavailable")

    async def prepare_complete(self, messages):
        from packages.providers.text import ProviderError

        raise ProviderError(f"Host {self.role} profile is unavailable")

    async def complete(self, messages):
        from packages.providers.text import ProviderError

        raise ProviderError(f"Host {self.role} profile is unavailable")

    def stream(self, messages):
        from packages.providers.text import ProviderError

        raise ProviderError(f"Host {self.role} profile is unavailable")

    async def close(self):
        pass


@dataclass(frozen=True)
class CatalogRecipe:
    binding: CatalogModelBinding
    catalog_revision: int
    configuration: str
    reasoning: str | None

    @property
    def config(self):
        return json.loads(self.configuration)

    @property
    def info(self):
        return declaration_info(self.config)


def declaration_info(config):
    from urllib.parse import urlsplit

    kind = config["kind"]
    return ProviderInfo(
        provider={"compatible": "openai-compatible"}.get(kind, kind),
        model=config["model"],
        local=kind != "meta"
        and urlsplit(config["base_url"]).hostname in {"localhost", "127.0.0.1", "::1"},
        streaming=kind != "laya",
    )


def _recipe(catalog, binding, reasoning, role="agent"):
    if role not in {"agent", "converse"}:
        raise ValueError("Unsupported measured text role")
    binding = CatalogModelBinding.model_validate(binding)
    profile = catalog.profile(binding.id, binding.generation, for_new=True)
    if profile["configuration_sha256"] != binding.configuration_sha256:
        raise ValueError("Catalog model configuration identity changed")
    config = profile["config"]
    kind = config["kind"]
    if kind == "lmstudio":
        if reasoning not in {None, "none"}:
            raise ValueError("LM Studio measured text supports reasoning none only")
        effort = "none"
    elif kind == "meta":
        if config["context_tokens"] is None or reasoning not in {None, *EFFORTS}:
            label = "Agent" if role == "agent" else "Converse"
            raise ValueError(f"Meta {label} requests require explicit measured context limits")
        effort = config["reasoning_effort"] if reasoning is None else reasoning
    else:
        raise ValueError(
            "This catalog adapter cannot measure Agent complete requests"
            if role == "agent"
            else "This catalog adapter cannot measure Converse streaming requests"
        )
    revision = catalog._revision()[0]
    return CatalogRecipe(
        binding,
        revision,
        json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        effort,
    )


def inspect_recipe(catalog, binding, reasoning=None, *, role="agent"):
    if catalog is None:
        raise ValueError("Provider catalog is unavailable")
    catalog.db.execute("BEGIN")
    try:
        recipe = _recipe(catalog, binding, reasoning, role)
        catalog.db.commit()
        return recipe
    except BaseException:
        catalog.db.rollback()
        raise


def open_recipe(
    catalog, binding, reasoning=None, *, auxiliary=False, resource_state=None, role="agent"
):
    """Capture one coherent current generation/key, then construct locally.

    The read snapshot is new-selection admission. A subsequent owner rotation
    cannot retarget this captured request. No connection or inference is opened.
    """
    if catalog is None:
        raise ValueError("Provider catalog is unavailable")
    catalog.db.execute("BEGIN")
    try:
        recipe = _recipe(catalog, binding, reasoning, role)
        config = recipe.config
        reference = config.get("credential")
        key = (
            None
            if reference is None
            else catalog.credential(reference["id"], reference["generation"], for_new=True)
        )
        catalog.db.commit()
    except BaseException:
        catalog.db.rollback()
        raise
    if config["kind"] == "lmstudio":
        from packages.providers.lmstudio import LMStudioProvider

        provider = LMStudioProvider(
            config["base_url"],
            config["model"],
            max_tokens=config["max_tokens"],
            timeout=config["timeout_s"],
            reasoning_effort="none",
            pin_complete_identity=True,
        )
    else:
        from packages.providers.text import MetaResponsesProvider

        provider = MetaResponsesProvider(
            base_url=config["base_url"],
            model=config["model"],
            api_key=key,
            allow_cloud=config["allow_cloud"],
            max_tokens=config["max_tokens"],
            context_tokens=config["context_tokens"],
            reasoning_effort=recipe.reasoning,
            structured_actions=role == "agent" and not auxiliary,
        )
    if resource_state is not None:
        from services.resources.providers import ResourceBoundProvider

        provider = ResourceBoundProvider(provider, resource_state, role)
    return provider, recipe
