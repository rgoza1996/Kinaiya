"""Resource policy constrains provider calls; it never chooses a fallback."""

from pathlib import Path

from packages.providers.text import ProviderError
from services.resources.state import read_record


class ResourceBoundProvider:
    def __init__(self, provider, state_path: Path, role: str):
        self.provider = provider
        self.state_path = state_path
        self.role = role

    @property
    def info(self):
        return self.provider.info

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
