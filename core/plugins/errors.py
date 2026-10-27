"""Error types for the native executable compute-plugin host.

All errors subclass :class:`PluginError` so owner tooling can fail closed
on any plugin-layer failure with a single except clause.
"""

from __future__ import annotations


class PluginError(Exception):
    """Base class for all compute-plugin host errors."""


class PluginRegistrationError(PluginError):
    """Candidate source or manifest failed static validation."""


class PluginIntegrityError(PluginError):
    """Stored bytes, hashes, or bindings do not match; refuse to run/promote."""


class PluginPromotionError(PluginError):
    """Promotion/rollback gate failed (actor, review, report binding, verdict)."""


class PluginCandidateFailure(PluginError):
    """Candidate executed but failed: timeout, crash, or contract violation."""


class PluginSandboxUnavailable(PluginError):
    """Isolation cannot be established; execution is refused (fail closed)."""


class PluginStateError(PluginError):
    """Registry state violation: bounds, pins, lifecycle, or lock contention."""
