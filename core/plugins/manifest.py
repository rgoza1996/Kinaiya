"""Strict manifest model for native compute plugins.

A manifest declares what a candidate *is*; the sandbox and the registry
decide what it may *do*. Only ``python_json.v1`` with entrypoint ``run``
and capability ``compute`` is supported. Anything else is rejected at
registration as an explicit refusal, never fake compatibility.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RUNTIME = "python_json.v1"
ENTRYPOINT = "run"
SCHEMA_VERSION = "1.0"
ALLOWED_CAPABILITIES = frozenset({"compute"})


class PluginManifest(BaseModel):
    """Validated manifest for one immutable plugin version."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    runtime: Literal["python_json.v1"] = RUNTIME
    entrypoint: Literal["run"] = ENTRYPOINT
    capabilities: list[str] = Field(default_factory=lambda: ["compute"])
    dependencies: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    author: str = Field(min_length=1, max_length=200)
    license: str = Field(min_length=1, max_length=200)

    def check_supported(self) -> None:
        """Reject capabilities/dependencies this host cannot isolate."""
        from core.plugins.errors import PluginRegistrationError

        unknown = set(self.capabilities) - ALLOWED_CAPABILITIES
        if unknown:
            raise PluginRegistrationError(f"unsupported capabilities: {sorted(unknown)}")
        if self.capabilities != ["compute"]:
            raise PluginRegistrationError(
                f"capabilities must be exactly ['compute']; got {self.capabilities}"
            )
        if self.dependencies:
            raise PluginRegistrationError(
                f"dependencies must be empty (stdlib-only plugins); got {self.dependencies}"
            )

    def canonical(self) -> str:
        """Canonical JSON used in the content-addressed version ID.

        Keys are sorted at every level so logically identical manifests
        hash identically regardless of input key order.
        """
        import json as _json

        return _json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
