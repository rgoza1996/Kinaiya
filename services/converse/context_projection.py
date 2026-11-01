"""Bounded context projection result, independent of a recall implementation."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packages.contracts.context_usage import ContextSelection
    from packages.providers.prompt_budget import PromptTokenCount


@dataclass(frozen=True)
class ContextProjection:
    messages: list[dict[str, str]]
    consumed_scopes: list[str]
    delivery: str | None = None
    consumed_memory: bool = False
    measurement: "PromptTokenCount | None" = None
    history_messages: int | None = None
    history_selection: "ContextSelection | None" = None
    history_watermark: int | None = None
