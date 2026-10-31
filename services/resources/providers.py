"""Resource policy constrains provider calls; it never chooses a fallback."""

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path

from packages.contracts.agent_tools import agent_export_scope
from packages.providers.completion_budget import PreparedComplete
from packages.providers.prompt_budget import PromptCountUnavailable, PromptTokenCount
from packages.providers.text import ProviderCleanupError, ProviderError, ScriptedProvider
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
        if value.get("provider") == "managed-gguf":
            if set(value) != {
                "provider",
                "model",
                "profile",
                "manifest_sha256",
                "resources_url",
                "context_tokens",
                "max_new_tokens",
            }:
                raise ValueError("Invalid managed GGUF binding shape")
            info = provider.info
            if (
                any(
                    type(value[key]) is not str
                    for key in ("provider", "model", "profile", "manifest_sha256", "resources_url")
                )
                or any(
                    not 0 < len(value[key].encode("utf-8")) <= 128
                    or any(char in value[key] for char in "\0\r\n")
                    for key in ("model", "profile")
                )
                or re.fullmatch(r"[0-9a-f]{64}", value["manifest_sha256"]) is None
                or local_base(value["resources_url"]) != value["resources_url"]
                or type(value["context_tokens"]) is not int
                or not 64 <= value["context_tokens"] <= 32768
                or type(value["max_new_tokens"]) is not int
                or not 16 <= value["max_new_tokens"] < value["context_tokens"]
                or value["max_new_tokens"] > 2048
                or info.provider != value["provider"]
                or info.model != value["model"]
                or info.local is not True
                or info.streaming is not True
            ):
                raise ValueError("Invalid managed GGUF binding identity")
            return value
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
        return state

    async def measure_prompt(self, messages):
        try:
            state = self._check()
            provider, role, state_path = self.provider, self.role, self.state_path
            measure = getattr(provider, "measure_prompt", None)
            cloud = getattr(provider, "cloud_prompt_measurement", False) is True
            if (
                role != "converse"
                or not (provider.info.local is True or cloud)
                or not callable(measure)
            ):
                raise PromptCountUnavailable("Prompt measurement is unavailable")
            result = await measure(messages)
            if (
                self.provider is not provider
                or self.role != role
                or self.state_path != state_path
                or not (
                    provider.info.local is True
                    or (cloud and getattr(provider, "cloud_prompt_measurement", False) is True)
                )
                or self._check().revision != state.revision
                or type(result) is not PromptTokenCount
            ):
                raise PromptCountUnavailable("Prompt measurement is unavailable")
            return result
        except ProviderCleanupError:
            raise
        except Exception as exc:
            raise PromptCountUnavailable("Prompt measurement is unavailable") from exc

    async def prepare_complete(self, messages):
        prepared = None
        try:
            state = self._check()
            provider, role, state_path = self.provider, self.role, self.state_path
            owner = asyncio.current_task()
            prepare = getattr(provider, "prepare_complete", None)
            if role != "agent" or not callable(prepare):
                raise PromptCountUnavailable("Complete prompt measurement is unavailable")

            def current():
                try:
                    return (
                        asyncio.current_task() is owner
                        and self.provider is provider
                        and self.role == role
                        and self.state_path == state_path
                        and self._check() == state
                    )
                except Exception:
                    return False

            with agent_export_scope(current):
                prepared = await prepare(messages)
            if (
                not current()
                or type(prepared) is not PreparedComplete
                or (prepared.fixture and not isinstance(provider, ScriptedProvider))
            ):
                raise PromptCountUnavailable("Complete prompt resource policy changed")

            async def generate():
                if not current():
                    prepared.discard()
                    raise PromptCountUnavailable("Complete prompt resource policy changed")
                with agent_export_scope(current):
                    return await prepared.complete()

            return PreparedComplete(prepared.measurement, generate, fixture=prepared.fixture)
        except (ProviderCleanupError, asyncio.CancelledError):
            if isinstance(prepared, PreparedComplete):
                prepared.discard()
            raise
        except Exception as exc:
            if isinstance(prepared, PreparedComplete):
                prepared.discard()
            raise PromptCountUnavailable("Complete prompt measurement is unavailable") from exc

    async def complete(self, messages):
        self._check()
        return await self.provider.complete(messages)

    async def health(self):
        self._check()
        probe = getattr(self.provider, "health", None)
        return await probe() if probe is not None else None

    async def stream(self, messages):
        self._check()
        source = self.provider.stream(messages)
        try:
            async for chunk in source:
                yield chunk
        finally:
            # Keep the selected provider's real cleanup in this caller's owner;
            # early wrapper close must not defer it to asyncgen finalization.
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
