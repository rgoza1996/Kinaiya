"""Native executable compute-plugin host: trusted owner side.

Only the modules in this package may touch the plugin registry, the
sandbox, the evaluation suites, and the activation pointers. Generated
candidate source never runs in this process; it runs under bubblewrap via
:mod:`core.plugins.sandbox`. This is a NEW callable operation, separate
from context-selector harness optimization.
"""

from core.plugins.errors import (
    PluginCandidateFailure,
    PluginError,
    PluginIntegrityError,
    PluginPromotionError,
    PluginRegistrationError,
    PluginSandboxUnavailable,
    PluginStateError,
)
from core.plugins.manifest import PluginManifest
from core.plugins.registry import MAX_REPORTS, MAX_VERSIONS, PluginRegistry
from core.plugins.sandbox import EVAL_LIMITS, RUN_LIMITS, PluginLimits
from core.plugins.suite import MAX_SUITE_BYTES, MAX_SUITE_CASES

__all__ = [
    "EVAL_LIMITS",
    "MAX_REPORTS",
    "MAX_SUITE_BYTES",
    "MAX_SUITE_CASES",
    "MAX_VERSIONS",
    "RUN_LIMITS",
    "PluginCandidateFailure",
    "PluginError",
    "PluginIntegrityError",
    "PluginLimits",
    "PluginManifest",
    "PluginPromotionError",
    "PluginRegistrationError",
    "PluginRegistry",
    "PluginSandboxUnavailable",
    "PluginStateError",
]
