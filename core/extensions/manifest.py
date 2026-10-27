"""Manifest model for executable harness extensions.

A manifest declares what a candidate *is*; the sandbox and the registry
decide what it may *do*. Only ``context_selection`` under contract
``context_selector.v1`` is supported. Anything else is rejected at
registration (unsupported capability escalation).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT = "context_selector.v1"
ENTRYPOINT = "select_context"

ALLOWED_CAPABILITIES = frozenset({"context_selection"})


class Manifest(BaseModel):
    """Validated manifest for one immutable extension version."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    contract: Literal["context_selector.v1"] = CONTRACT
    entrypoint: Literal["select_context"] = ENTRYPOINT
    capabilities: list[str] = Field(default_factory=lambda: ["context_selection"])
    dependencies: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)

    def check_supported(self) -> None:
        """Reject capabilities/dependencies this framework cannot isolate."""
        from core.extensions.errors import RegistrationError

        unknown = set(self.capabilities) - ALLOWED_CAPABILITIES
        if unknown:
            raise RegistrationError(f"unsupported capabilities: {sorted(unknown)}")
        if self.dependencies:
            raise RegistrationError(
                f"dependencies must be empty (stdlib-only selectors); got {self.dependencies}"
            )

    def canonical(self) -> str:
        """Canonical JSON used in the content-addressed version ID."""
        return self.model_dump_json(exclude_none=False)
