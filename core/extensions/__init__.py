"""Protected executable-harness framework: trusted core side.

Only the modules in this package may touch the registry, the sandbox, the
evaluation policy, and the activation pointer. Generated candidate source
never runs in this process; it runs under bubblewrap via
:mod:`core.extensions.sandbox`.
"""

from core.extensions.errors import (
    CandidateFailure,
    HarnessError,
    IntegrityError,
    PromotionError,
    RegistrationError,
    SandboxUnavailable,
)
from core.extensions.manifest import Manifest
from core.extensions.registry import EvaluationReport, Registry

__all__ = [
    "CandidateFailure",
    "EvaluationReport",
    "HarnessError",
    "IntegrityError",
    "Manifest",
    "PromotionError",
    "RegistrationError",
    "Registry",
    "SandboxUnavailable",
]
