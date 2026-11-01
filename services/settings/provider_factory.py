"""Native measured text recipes; inspection never constructs adapters or keys."""

import json
from dataclasses import dataclass, field

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


@dataclass(repr=False)
class CapturedCatalogRecipe:
    """Native task-owned recipe/key; never a DTO, cache key or journal value."""

    recipe: CatalogRecipe
    key: str | None = field(repr=False)
    consumed: bool = False

    def release(self):
        self.key = None
        self.consumed = True


def declaration_info(config):
    from urllib.parse import urlsplit

    kind = config["kind"]
    return ProviderInfo(
        provider={
            "compatible": "openai-compatible",
            "meta_stt": "meta",
            "openrouter_tts": "openrouter",
        }.get(kind, kind),
        model=config["model"],
        local=kind not in {"meta", "meta_stt", "openrouter_tts"}
        and urlsplit(config["base_url"]).hostname in {"localhost", "127.0.0.1", "::1"},
        streaming=kind not in {"laya", "meta_stt"},
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
    captured = capture_recipes(catalog, [(binding, reasoning)], role=role)[0]
    return open_captured_recipe(
        captured, auxiliary=auxiliary, resource_state=resource_state, role=role
    ), captured.recipe


def capture_recipes(catalog, selections, *, role="agent"):
    """Capture a bounded ordered set in one native catalog read transaction."""
    if catalog is None or not 1 <= len(selections) <= 16:
        raise ValueError("Provider catalog capture is unavailable or exceeds its bound")
    captures = []
    catalog.db.execute("BEGIN")
    try:
        for binding, reasoning in selections:
            recipe = _recipe(catalog, binding, reasoning, role)
            reference = recipe.config.get("credential")
            key = (
                None
                if reference is None
                else catalog.credential(reference["id"], reference["generation"], for_new=True)
            )
            captures.append(CapturedCatalogRecipe(recipe, key))
        catalog.db.commit()
        return captures
    except BaseException:
        for capture in captures:
            capture.release()
        catalog.db.rollback()
        raise


def open_captured_recipe(
    captured, *, auxiliary=False, resource_state=None, role="agent", pre_generation_proofs=False
):
    """Consume captured authority once; never look up a current alias or key."""
    if type(captured) is not CapturedCatalogRecipe or captured.consumed:
        raise ValueError("Catalog recipe has already been consumed or retired")
    if type(pre_generation_proofs) is not bool:
        raise ValueError("Invalid native pre-generation proof configuration")
    if pre_generation_proofs and (role != "agent" or not auxiliary):
        raise ValueError("Pre-generation proofs require a task-owned auxiliary route")
    recipe, key = captured.recipe, captured.key
    config = recipe.config
    captured.release()
    if config["kind"] == "lmstudio":
        from packages.providers.lmstudio import LMStudioProvider

        provider = LMStudioProvider(
            config["base_url"],
            config["model"],
            max_tokens=config["max_tokens"],
            timeout=config["timeout_s"],
            reasoning_effort="none",
            pin_complete_identity=True,
            **({"pre_generation_proofs": True} if pre_generation_proofs else {}),
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
            **({"pre_generation_proofs": True} if pre_generation_proofs else {}),
        )
    if resource_state is not None:
        from services.resources.providers import ResourceBoundProvider

        provider = ResourceBoundProvider(provider, resource_state, role)
    return provider
