"""Fail-closed qualification for the distribution workflow (no publishing)."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from packages.distribution.releases import VERSION, ReleaseError

REPOSITORY = "rgoza1996/Kinaiya"


def _require(condition, message):
    if not condition:
        raise ReleaseError(message)


def _success(run, source_sha):
    return all(
        (
            run.get("name") == "deterministic-ci",
            run.get("path") == ".github/workflows/ci.yml",
            run.get("head_repository", {}).get("full_name") == REPOSITORY,
            run.get("head_branch") == "main",
            run.get("event") == "push",
            run.get("head_sha") == source_sha,
            run.get("status") == "completed",
            run.get("conclusion") == "success",
        )
    )


def qualify_candidate(
    event,
    *,
    main_sha,
    source_sha,
    ci_runs,
    build_number,
    publish_enabled=False,
    environment=None,
    source_on_main=False,
):
    """Qualify the exact source, not arbitrary main or an existing alpha release.

    A manual dry run can test a feature branch, but cannot enter the publisher.
    All non-dry runs require current CI evidence and preconfigured protections.
    """
    _require(event.get("repository", {}).get("full_name") == REPOSITORY, "Unofficial repository")
    _require(
        isinstance(source_sha, str) and re.fullmatch(r"[0-9a-f]{40}", source_sha),
        "Invalid source SHA",
    )
    _require(type(build_number) is int and 1 <= build_number < 2**63, "Invalid build number")
    inputs = event.get("inputs") or {}
    kind = event.get("kind")
    _require(kind in ("workflow_run", "workflow_dispatch"), "Unsupported release trigger")
    raw_dry = inputs.get("dry_run", True)
    _require(raw_dry in (True, False, "true", "false"), "Invalid dry-run flag")
    dry_run = kind == "workflow_dispatch" and raw_dry in (True, "true")
    channel = inputs.get("channel", "nightly") if kind == "workflow_dispatch" else "nightly"
    version = inputs.get("version", "0.1.0") or "0.1.0"
    _require(channel in ("stable", "nightly"), "Unsupported release channel")
    _require(
        isinstance(version, str) and len(version) <= 64 and re.fullmatch(VERSION, version),
        "Invalid base version",
    )
    tag = (
        inputs.get("tag", "")
        if channel == "stable"
        else f"nightly-{build_number}-{source_sha[:12]}"
    )
    if channel == "stable":
        _require(
            kind == "workflow_dispatch" and tag == f"v{version}",
            "Stable requires an explicit matching final version tag",
        )
    if kind == "workflow_run":
        _require(
            _success(event.get("workflow_run", {}), source_sha),
            "Triggering CI did not qualify official main",
        )
    if not dry_run:
        _require(
            publish_enabled is True, "Publication disabled; configure RELEASE_PUBLISH_ENABLED first"
        )
        _require(
            source_sha == main_sha if channel == "nightly" else source_on_main,
            "Source is stale or not qualified on main",
        )
        _require(
            isinstance(ci_runs, list) and 1 <= len(ci_runs) <= 100, "Missing bounded CI evidence"
        )
        latest = max(ci_runs, key=lambda run: (run.get("run_number", 0), run.get("run_attempt", 0)))
        _require(_success(latest, source_sha), "Latest exact-source CI has not succeeded")
        environment = environment or {}
        _require(
            environment.get("deployment_branch_policy", {}).get("protected_branches") is True,
            "Publishing environment must allow protected branches only",
        )
        if channel == "stable":
            _require(
                any(
                    rule.get("type") == "required_reviewers"
                    and rule.get("reviewers")
                    and rule.get("prevent_self_review") is True
                    for rule in environment.get("protection_rules", [])
                ),
                "Stable requires an independent protected-environment reviewer",
            )
    return {
        "channel": channel,
        "source_sha": source_sha,
        "version": version,
        "tag": tag,
        "dry_run": dry_run,
        "build_number": build_number,
    }


def _git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stdin=subprocess.DEVNULL, timeout=30
    ).strip()


def _api(endpoint):
    raw = subprocess.check_output(
        ["gh", "api", "--method", "GET", endpoint], stdin=subprocess.DEVNULL, timeout=30
    )
    _require(len(raw) <= 4 * 1024 * 1024, "GitHub gate response exceeds bound")
    return json.loads(raw)


def prepare(event, *, repo, build_number, github_ref, workflow_ref, publish_enabled=False):
    """Read authoritative exact-source CI and environment settings before building."""
    kind = event.get("kind")
    inputs = event.get("inputs") or {}
    dry_run = kind == "workflow_dispatch" and inputs.get("dry_run", True) in (True, "true")
    if not dry_run:
        _require(
            github_ref == "refs/heads/main"
            and workflow_ref == f"{REPOSITORY}/.github/workflows/distribution.yml@refs/heads/main",
            "Publishing requires the official main workflow",
        )
        _require(
            publish_enabled is True, "Publication disabled; configure RELEASE_PUBLISH_ENABLED first"
        )
    channel = inputs.get("channel", "nightly") if kind == "workflow_dispatch" else "nightly"
    if channel == "stable":
        tag = inputs.get("tag", "")
        _require(
            isinstance(tag, str) and re.fullmatch(rf"v{VERSION}", tag),
            "Stable requires a final version tag",
        )
        source = _git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    elif kind == "workflow_run":
        source = event.get("workflow_run", {}).get("head_sha", "")
    else:
        source = _git(repo, "rev-parse", "HEAD")
    _require(
        isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source), "Invalid source SHA"
    )
    main_sha = _git(repo, "rev-parse", "refs/remotes/origin/main")
    ci_runs, environment, source_on_main = [], None, False
    if not dry_run:
        branch = _api(f"repos/{REPOSITORY}/branches/main")
        _require(branch.get("protected") is True, "Main branch protection is required")
        # Exact-source lookup is bounded; never accept a green check on a different commit.
        ci_runs = _api(
            f"repos/{REPOSITORY}/actions/workflows/ci.yml/runs?head_sha={source}&branch=main&event=push&per_page=100"
        )["workflow_runs"]
        environment = _api(f"repos/{REPOSITORY}/environments/release-{channel}")
        if channel == "stable":
            source_on_main = _git(repo, "merge-base", source, main_sha) == source
    return qualify_candidate(
        event,
        main_sha=main_sha,
        source_sha=source,
        ci_runs=ci_runs,
        build_number=build_number,
        publish_enabled=publish_enabled,
        environment=environment,
        source_on_main=source_on_main,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = json.loads(args.event.read_bytes())
        event["kind"] = os.environ["GITHUB_EVENT_NAME"]
        result = prepare(
            event,
            repo=Path.cwd(),
            build_number=int(os.environ["GITHUB_RUN_NUMBER"]),
            github_ref=os.environ["GITHUB_REF"],
            workflow_ref=os.environ["GITHUB_WORKFLOW_REF"],
            publish_enabled=os.environ.get("RELEASE_PUBLISH_ENABLED") == "true",
        )
        with args.output.open("a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(f"{key}={str(value).lower() if type(value) is bool else value}\n")
        print(json.dumps(result, sort_keys=True))
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"release gate refused: {exc}\n")


if __name__ == "__main__":
    main()
