"""Owner-defined midnight Pacific nightly cadence and immutable scheduled source."""

from pathlib import Path

import pytest
import yaml

from packages.distribution.releases import ReleaseError
from scripts.release_gate import prepare, qualify_candidate

SHA = "a" * 40
NEW_MAIN = "b" * 40
REPO = "rgoza1996/Kinaiya"
ENVIRONMENT = {"deployment_branch_policy": {"protected_branches": True}}


def ci(**changes):
    return {
        "name": "deterministic-ci",
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
        "head_repository": {"full_name": REPO},
        "run_number": 10,
        "run_attempt": 1,
        **changes,
    }


def scheduled():
    return {"kind": "schedule", "repository": {"full_name": REPO}, "schedule": "0 0 * * *"}


def test_midnight_pacific_replaces_automatic_per_commit_publication():
    path = Path(__file__).resolve().parents[2] / ".github/workflows/distribution.yml"
    config = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert config["on"]["schedule"] == [{"cron": "0 0 * * *", "timezone": "America/Los_Angeles"}]
    assert "workflow_run" not in config["on"]


def test_schedule_keeps_qualified_snapshot_when_main_advances_during_build():
    result = qualify_candidate(
        scheduled(),
        main_sha=NEW_MAIN,
        source_sha=SHA,
        ci_runs=[ci()],
        build_number=123,
        publish_enabled=True,
        environment=ENVIRONMENT,
        source_on_main=True,
    )
    assert result["channel"] == "nightly" and result["source_sha"] == SHA
    assert result["dry_run"] is False


@pytest.mark.parametrize(
    "changes", [{"conclusion": "failure"}, {"status": "in_progress"}, {"head_sha": NEW_MAIN}]
)
def test_schedule_never_substitutes_unqualified_or_other_source(changes):
    with pytest.raises(ReleaseError, match="CI"):
        qualify_candidate(
            scheduled(),
            main_sha=NEW_MAIN,
            source_sha=SHA,
            ci_runs=[ci(**changes)],
            build_number=123,
            publish_enabled=True,
            environment=ENVIRONMENT,
            source_on_main=True,
        )


def test_schedule_rejects_source_outside_main():
    with pytest.raises(ReleaseError):
        qualify_candidate(
            scheduled(),
            main_sha=NEW_MAIN,
            source_sha=SHA,
            ci_runs=[ci()],
            build_number=123,
            publish_enabled=True,
            environment=ENVIRONMENT,
            source_on_main=False,
        )


def test_prepare_binds_schedule_to_trusted_github_event_sha(tmp_path, monkeypatch):
    from scripts import release_gate as gate

    def git(repo, *args):
        if args == ("rev-parse", "HEAD"):
            return SHA
        if args == ("rev-parse", "refs/remotes/origin/main"):
            return NEW_MAIN
        if args == ("merge-base", SHA, NEW_MAIN):
            return SHA
        pytest.fail(f"unexpected git operation: {args}")

    def api(endpoint):
        if endpoint.endswith("/branches/main"):
            return {"protected": True}
        if endpoint.endswith("/environments/release-nightly"):
            return ENVIRONMENT
        if "/actions/workflows/ci.yml/runs?" in endpoint:
            assert "head_sha=" + SHA in endpoint
            return {"workflow_runs": [ci()]}
        pytest.fail(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(gate, "_git", git)
    monkeypatch.setattr(gate, "_api", api)
    kwargs = dict(
        repo=tmp_path,
        build_number=123,
        github_ref="refs/heads/main",
        workflow_ref=f"{REPO}/.github/workflows/distribution.yml@refs/heads/main",
        publish_enabled=True,
    )
    result = prepare(scheduled(), **kwargs, expected_source_sha=SHA)
    assert result["source_sha"] == SHA
    with pytest.raises(ReleaseError, match="snapshot"):
        prepare(scheduled(), **kwargs, expected_source_sha=NEW_MAIN)
