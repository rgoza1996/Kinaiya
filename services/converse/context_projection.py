"""Bounded context projection result, independent of a recall implementation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextProjection:
    messages: list[dict[str, str]]
    consumed_scopes: list[str]
    delivery: str | None = None
    consumed_memory: bool = False
