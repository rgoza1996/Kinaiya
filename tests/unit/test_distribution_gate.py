"""Publication gates are tested without a GitHub write or signing credentials."""

import pytest

from packages.distribution.releases import ReleaseError

SHA = "a" * 40
REPO = "rgoza1996/Kinaiya"


def ci_run(**changes):
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


def event(kind="workflow_run", **changes):
    return {
        "repository": {"full_name": REPO},
        "workflow_run": ci_run(),
        "inputs": {},
        "kind": kind,
        **changes,
    }


def test_successful_official_main_ci_qualifies_nightly():
    from scripts.release_gate import qualify_candidate

    result = qualify_candidate(
        event(),
        main_sha=SHA,
        source_sha=SHA,
        ci_runs=[ci_run()],
        build_number=123,
        publish_enabled=True,
        environment={"deployment_branch_policy": {"protected_branches": True}},
    )
    assert result == {
        "channel": "nightly",
        "source_sha": SHA,
        "version": "0.1.0",
        "tag": f"nightly-123-{SHA[:12]}",
        "dry_run": False,
        "build_number": 123,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"conclusion": "failure"},
        {"status": "in_progress"},
        {"head_branch": "feature"},
        {"event": "pull_request"},
        {"head_repository": {"full_name": "fork/Kinaiya"}},
        {"path": ".github/workflows/other.yml"},
        {"name": "other"},
        {"head_sha": "b" * 40},
    ],
)
def test_workflow_run_refuses_failed_fork_wrong_or_stale_gates(changes):
    from scripts.release_gate import qualify_candidate

    with pytest.raises(ReleaseError):
        qualify_candidate(
            event(workflow_run=ci_run(**changes)),
            main_sha=SHA,
            source_sha=SHA,
            ci_runs=[ci_run()],
            build_number=123,
            publish_enabled=True,
            environment={"deployment_branch_policy": {"protected_branches": True}},
        )


def test_newer_failed_ci_attempt_cannot_reuse_old_success():
    from scripts.release_gate import qualify_candidate

    with pytest.raises(ReleaseError, match="CI"):
        qualify_candidate(
            event(),
            main_sha=SHA,
            source_sha=SHA,
            ci_runs=[ci_run(), ci_run(run_attempt=2, conclusion="failure")],
            build_number=123,
            publish_enabled=True,
            environment={"deployment_branch_policy": {"protected_branches": True}},
        )


def test_dry_run_manual_feature_build_needs_no_publish_environment():
    from scripts.release_gate import qualify_candidate

    result = qualify_candidate(
        event(
            "workflow_dispatch", inputs={"dry_run": True, "channel": "nightly", "version": "0.2.0"}
        ),
        main_sha="b" * 40,
        source_sha=SHA,
        ci_runs=[],
        build_number=123,
    )
    assert result["dry_run"] is True
    assert result["source_sha"] == SHA
    assert result["channel"] == "nightly"


@pytest.mark.parametrize(
    "inputs",
    [
        {"dry_run": False, "channel": "stable", "version": "0.2.0", "tag": "v0.2.0-alpha.1"},
        {"dry_run": False, "channel": "stable", "version": "0.2.0", "tag": "v0.3.0"},
        {"dry_run": False, "channel": "stable", "version": "0.2.0", "tag": "main"},
    ],
)
def test_stable_never_promotes_alpha_or_unqualified_tag(inputs):
    from scripts.release_gate import qualify_candidate

    with pytest.raises(ReleaseError):
        qualify_candidate(
            event("workflow_dispatch", inputs=inputs),
            main_sha=SHA,
            source_sha=SHA,
            ci_runs=[ci_run()],
            build_number=123,
            publish_enabled=True,
            source_on_main=True,
            environment={
                "deployment_branch_policy": {"protected_branches": True},
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "reviewers": [{"id": 1}],
                        "prevent_self_review": True,
                    }
                ],
            },
        )


def test_stable_requires_explicit_publish_enable_and_protected_review():
    from scripts.release_gate import qualify_candidate

    dispatch = event(
        "workflow_dispatch",
        inputs={"dry_run": False, "channel": "stable", "version": "0.2.0", "tag": "v0.2.0"},
    )
    protected = {
        "deployment_branch_policy": {"protected_branches": True},
        "protection_rules": [
            {"type": "required_reviewers", "reviewers": [{"id": 1}], "prevent_self_review": True}
        ],
    }
    for enabled, environment, on_main in [
        (False, protected, True),
        (True, {}, True),
        (True, protected, False),
        (True, {"deployment_branch_policy": {"protected_branches": True}}, True),
    ]:
        with pytest.raises(ReleaseError):
            qualify_candidate(
                dispatch,
                main_sha=SHA,
                source_sha=SHA,
                ci_runs=[ci_run()],
                build_number=123,
                publish_enabled=enabled,
                environment=environment,
                source_on_main=on_main,
            )
    result = qualify_candidate(
        dispatch,
        main_sha=SHA,
        source_sha=SHA,
        ci_runs=[ci_run()],
        build_number=123,
        publish_enabled=True,
        environment=protected,
        source_on_main=True,
    )
    assert result["tag"] == "v0.2.0"
    assert result["dry_run"] is False


def test_prepare_dry_run_has_no_network_or_publication_authority(monkeypatch):
    from scripts import release_gate

    monkeypatch.setattr(release_gate, "_git", lambda repo, *args: SHA, raising=False)
    monkeypatch.setattr(
        release_gate,
        "_api",
        lambda endpoint: pytest.fail("Dry run contacted publisher API"),
        raising=False,
    )
    result = release_gate.prepare(
        event("workflow_dispatch", inputs={"dry_run": "true", "channel": "nightly"}),
        repo=".",
        build_number=123,
        github_ref="refs/heads/feature",
        workflow_ref="feature",
    )
    assert result["dry_run"] is True
    assert result["source_sha"] == SHA


def test_prepare_publication_requires_trusted_workflow_ref(monkeypatch):
    from scripts import release_gate

    monkeypatch.setattr(release_gate, "_git", lambda repo, *args: SHA, raising=False)
    with pytest.raises(ReleaseError, match="main workflow"):
        release_gate.prepare(
            event(),
            repo=".",
            build_number=123,
            publish_enabled=True,
            github_ref="refs/heads/feature",
            workflow_ref="feature",
        )
