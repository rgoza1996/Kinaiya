"""Predeclared promotion policy (pinned; candidates cannot change it).

Gates (Roadmap 12.2): contract/integrity/sandbox checks pass (enforced by
the registry before scoring); no permission/objective regression is
observable in this narrow selector task because candidates receive only
bounded JSON and every output ID and byte is re-validated host-side; task
quality and non-inferiority are the numeric gates below; resource/failure
behavior is bounded per case; rollback is verified by the test suite.
"""

from __future__ import annotations

POLICY_VERSION = "harness-policy.v1"

DEFAULT_POLICY: dict = {
    "min_recall": 0.5,
    "non_inferiority_margin": 0.1,
    "max_failure_rate": 0.0,
    "require_no_invalid": True,
    "max_mean_wall_s": 4.0,
}

__all__ = ["DEFAULT_POLICY", "POLICY_VERSION"]
