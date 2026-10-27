"""Offline independent selector evaluation; never calls a model or auto-promotes."""

import argparse
import json
from pathlib import Path

from core.extensions import Registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", default="candidate")
    args = parser.parse_args()
    registry = Registry(args.registry, args.suite)
    before = registry.active
    version = registry.register(args.source, {"name": args.name})
    report = registry.evaluate(version)
    result = {
        "report": report.model_dump(),
        "active_before": before,
        "active_after": registry.active,
        "promotion_attempted": False,
        "evaluation_model_calls": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "report_id": report.report_id,
                "passed": report.passed,
                "active_unchanged": before == registry.active,
            }
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
