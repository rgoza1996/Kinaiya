"""Error types for the protected executable-harness framework."""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for all harness-framework errors."""


class SandboxUnavailable(HarnessError):
    """Isolation cannot be established; execution is refused (fail closed)."""


class RegistrationError(HarnessError):
    """Candidate source or manifest failed static validation."""


class IntegrityError(HarnessError):
    """Stored bytes, hashes, or bindings do not match; refuse to run/promote."""


class PromotionError(HarnessError):
    """Promotion gate failed (actor, review, report binding, or verdict)."""


class CandidateFailure(HarnessError):
    """Candidate executed but failed: timeout, crash, or contract violation."""
