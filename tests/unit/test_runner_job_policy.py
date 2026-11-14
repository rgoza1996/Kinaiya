"""Reject attacker-controlled PR contexts independently of repository YAML."""

from copy import deepcopy
from pathlib import Path

import pytest

from scripts.ci import runner_job_policy
from scripts.ci.runner_job_policy import authorize

_SHA = "a" * 40
_POLICY = {
    "repository": "rgoza1996/Kinaiya",
    "repository_id": "1435085891",
    "actor_ids": ["97354241"],
    "workflows": ["ci.yml", "self-hosted.yml"],
    "reviewed_refs": {"refs/heads/codex/self-hosted-ci-379": _SHA},
}


def _case(event_name="push", ref="refs/heads/main"):
    context = {
        "GITHUB_REPOSITORY": "rgoza1996/Kinaiya",
        "GITHUB_REPOSITORY_ID": "1435085891",
        "GITHUB_ACTOR_ID": "97354241",
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_REF": ref,
        "GITHUB_SHA": _SHA,
        "GITHUB_WORKFLOW_REF": f"rgoza1996/Kinaiya/.github/workflows/ci.yml@{ref}",
        "GITHUB_WORKFLOW_SHA": _SHA,
    }
    event = {
        "repository": {"id": 1435085891, "full_name": "rgoza1996/Kinaiya"},
        "sender": {"id": 97354241},
        "ref": ref,
        "after": _SHA,
    }
    return context, event


@pytest.mark.parametrize(
    ("event_name", "ref"),
    [
        ("push", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/codex/self-hosted-ci-379"),
    ],
)
def test_trusted_main_or_exact_reviewed_dispatch_is_admitted(event_name, ref):
    context, event = _case(event_name, ref)
    assert authorize(context, event, _POLICY)


@pytest.mark.parametrize(
    "event_name",
    ["pull_request", "pull_request_target", "workflow_run", "issue_comment", "schedule"],
)
def test_pr_and_indirect_events_are_refused_even_with_main_sha_and_owner_actor(event_name):
    context, event = _case(event_name)
    assert not authorize(context, event, _POLICY)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_REPOSITORY", "attacker/Kinaiya"),
        ("GITHUB_REPOSITORY_ID", "999"),
        ("GITHUB_ACTOR_ID", "999"),
        ("GITHUB_REF", "refs/pull/1/merge"),
        ("GITHUB_SHA", "invalid"),
        ("GITHUB_WORKFLOW_SHA", "b" * 40),
        ("GITHUB_WORKFLOW_REF", "rgoza1996/Kinaiya/.github/workflows/evil.yml@refs/heads/main"),
    ],
)
def test_wrong_identity_ref_or_workflow_is_refused(key, value):
    context, event = _case()
    context[key] = value
    assert not authorize(context, event, _POLICY)


@pytest.mark.parametrize("change", ["repository", "sender", "ref", "after", "pull_request"])
def test_inconsistent_or_malformed_webhook_is_refused(change):
    context, event = _case()
    event[change] = {} if change == "pull_request" else None
    assert not authorize(context, event, _POLICY)


def test_reviewed_branch_cannot_advance_or_receive_push_jobs():
    context, event = _case("workflow_dispatch", "refs/heads/codex/self-hosted-ci-379")
    context["GITHUB_SHA"] = context["GITHUB_WORKFLOW_SHA"] = "b" * 40
    assert not authorize(context, event, _POLICY)
    context, event = _case("push", "refs/heads/codex/self-hosted-ci-379")
    assert not authorize(context, event, _POLICY)
    policy = deepcopy(_POLICY)
    policy["reviewed_refs"] = {}
    context["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    assert not authorize(context, event, policy)


def test_refusal_terminates_original_worker_before_any_remaining_actions(monkeypatch):
    signals = []
    monkeypatch.setattr(runner_job_policy.os, "getppid", lambda: 101)
    monkeypatch.setattr(runner_job_policy.os, "readlink", lambda path: "/runner/bin/Runner.Worker")
    monkeypatch.setattr(runner_job_policy.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    runner_job_policy._terminate_job()
    assert signals == [(101, runner_job_policy.signal.SIGKILL)]


@pytest.mark.parametrize("in_service", [False, True])
def test_unknown_worker_layout_can_only_stop_own_service_and_never_itself(monkeypatch, in_service):
    signals = []
    monkeypatch.setattr(runner_job_policy.os, "getppid", lambda: 101)
    monkeypatch.setattr(runner_job_policy.os, "getpid", lambda: 102)
    monkeypatch.setattr(runner_job_policy.os, "readlink", lambda path: "/usr/bin/bash")
    monkeypatch.setattr(runner_job_policy.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    def read_text(path):
        if str(path) == "/proc/self/cgroup":
            return "0::/system.slice/kinaiya-ci.service" if in_service else "0::/other.service"
        assert str(path) == "/sys/fs/cgroup/system.slice/kinaiya-ci.service/cgroup.procs"
        return "100\n101\n102\n"

    monkeypatch.setattr(Path, "read_text", read_text)
    runner_job_policy._terminate_job()
    assert signals == (
        [(100, runner_job_policy.signal.SIGKILL), (101, runner_job_policy.signal.SIGKILL)]
        if in_service
        else []
    )
