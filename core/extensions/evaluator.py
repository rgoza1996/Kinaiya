"""Independent evaluation of one candidate against the pinned baseline.

Identical inputs and identical char budgets for both versions. Expected
answers and evaluator code are never mounted into the candidate sandbox.
Metrics are computed host-side; candidate self-reported numbers are
ignored. No model calls occur during evaluation.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.extensions.payloads import check_output_ids
from core.extensions.sandbox import SandboxLimits, run_selector

BUDGET_UNIT = "chars: sum(len(text)) of selected files must be <= max_chars"


def _recall(selected: list[str], expected: list[str]) -> float:
    if not expected:
        return 1.0 if not selected else 0.0
    return len(set(selected) & set(expected)) / len(expected)


def run_case(
    source_file: Path,
    case_payload: dict,
    expected_ids: list[str],
    *,
    limits: SandboxLimits,
) -> dict[str, Any]:
    """Run one suite case; never raises — failures become measurements."""
    started = time.monotonic()
    try:
        ids = run_selector(source_file, case_payload, limits=limits)
        ids = check_output_ids(ids, case_payload)
        wall = time.monotonic() - started
        return {
            "ok": True,
            "ids": ids,
            "recall": _recall(ids, expected_ids),
            "wall_s": wall,
            "failure": None,
        }
    except Exception as exc:  # noqa: BLE001 - failures are data
        return {
            "ok": False,
            "ids": [],
            "recall": 0.0,
            "wall_s": time.monotonic() - started,
            "failure": f"{type(exc).__name__}: {exc}"[:300],
        }


def summarize(cases: list[dict[str, Any]], suite_size: int) -> dict[str, Any]:
    """Aggregate per-case results into host-computed metrics."""
    oks = [c for c in cases if c["ok"]]
    recalls = [c["recall"] for c in cases]
    walls = [c["wall_s"] for c in cases]
    n_fail = suite_size - len(oks)
    return {
        "n_cases": suite_size,
        "n_ok": len(oks),
        "n_fail": n_fail,
        "failure_rate": n_fail / suite_size if suite_size else 1.0,
        "validity_rate": len(oks) / suite_size if suite_size else 0.0,
        "mean_recall": sum(recalls) / len(recalls) if recalls else 0.0,
        "mean_wall_s": sum(walls) / len(walls) if walls else 0.0,
        "max_wall_s": max(walls) if walls else 0.0,
        "budget_unit": BUDGET_UNIT,
    }


def verdict(
    candidate: dict[str, Any], baseline: dict[str, Any], policy: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Apply the predeclared policy. No post-hoc thresholds are possible."""
    reasons: list[str] = []
    passed = True

    def gate(ok: bool, reason: str) -> None:
        nonlocal passed
        reasons.append(("PASS " if ok else "FAIL ") + reason)
        if not ok:
            passed = False

    gate(
        candidate["failure_rate"] <= policy["max_failure_rate"],
        f"failure_rate {candidate['failure_rate']:.3f} "
        f"<= {policy['max_failure_rate']:.3f}",
    )
    if policy.get("require_no_invalid", True):
        gate(
            candidate["validity_rate"] == 1.0,
            f"validity_rate {candidate['validity_rate']:.3f} == 1.0",
        )
    gate(
        candidate["mean_wall_s"] <= policy["max_mean_wall_s"],
        f"mean_wall_s {candidate['mean_wall_s']:.3f} "
        f"<= {policy['max_mean_wall_s']:.3f}",
    )
    floor = max(
        policy["min_recall"],
        baseline["mean_recall"] - policy["non_inferiority_margin"],
    )
    gate(
        candidate["mean_recall"] >= floor,
        f"mean_recall {candidate['mean_recall']:.3f} >= {floor:.3f} "
        f"(min {policy['min_recall']:.3f}, baseline "
        f"{baseline['mean_recall']:.3f} - margin "
        f"{policy['non_inferiority_margin']:.3f})",
    )
    return passed, reasons

