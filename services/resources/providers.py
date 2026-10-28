"""Resource policy constrains provider calls; it never chooses a fallback."""

from collections.abc import Mapping
from pathlib import Path

from packages.providers.text import ProviderError
from services.resources.state import read_record


def managed_text_binding(provider):
    """Read only the static configured identity, never readiness or policy state."""
    try:
        value = getattr(provider, "managed_binding", None)
        if value is None:
            return None
        from packages.providers.qwen3_ipc import MANIFEST_SHA256, MODEL, PROFILE, validate_limits
        from services.resources.client import local_base

        if not isinstance(value, Mapping):
            raise ValueError("Invalid managed text binding shape")
        value = dict(value)
        if set(value) != {
            "provider",
            "model",
            "profile",
            "manifest_sha256",
            "resources_url",
            "max_input_tokens",
            "max_new_tokens",
        }:
            raise ValueError("Invalid managed text binding shape")
        limits = validate_limits(value["max_input_tokens"], value["max_new_tokens"])
        info = provider.info
        if (
            any(
                type(value[key]) is not str
                for key in ("provider", "model", "profile", "manifest_sha256", "resources_url")
            )
            or value["provider"] != "managed-qwen3"
            or value["model"] != MODEL
            or value["profile"] != PROFILE
            or value["manifest_sha256"] != MANIFEST_SHA256
            or not isinstance(value["resources_url"], str)
            or local_base(value["resources_url"]) != value["resources_url"]
            or info.provider != value["provider"]
            or info.model != value["model"]
            or info.local is not True
            or info.streaming is not True
        ):
            raise ValueError("Invalid managed text binding identity")
        return {**value, **limits}
    except Exception as exc:
        raise ProviderError("Managed text configuration identity is unavailable") from exc


class ResourceBoundProvider:
    def __init__(self, provider, state_path: Path, role: str):
        self.provider = provider
        self.state_path = state_path
        self.role = role

    @property
    def info(self):
        return self.provider.info

    @property
    def managed_binding(self):
        return managed_text_binding(self.provider)

    def _check(self):
        try:
            state = read_record(self.state_path)
        except Exception as exc:
            raise ProviderError("Resource policy is unavailable; inference is blocked") from exc
        if state.profile == "local_only" and not self.info.local:
            raise ProviderError("Local-only policy requires a configured local provider")
        if self.role == "converse" and (not state.desired_converse or state.phase != "ready"):
            raise ProviderError("Conversation inference is paused for resource coordination")
        if (
            self.role == "decisions"
            and self.info.local
            and (not state.desired_converse or state.phase != "ready")
        ):
            raise ProviderError("Local Decisions inference is paused for resource coordination")

    async def complete(self, messages):
        self._check()
        return await self.provider.complete(messages)

    async def health(self):
        self._check()
        probe = getattr(self.provider, "health", None)
        return await probe() if probe is not None else None

    async def stream(self, messages):
        self._check()
        async for chunk in self.provider.stream(messages):
            yield chunk
