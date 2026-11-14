"""Host-installed admission policy; run before any repository-supplied job steps.

Install under root-owned /etc/kinaiya-ci and invoke with Python -I. A repository
copy is reviewable source, never the copy used to authorize its own job.
"""

import json
import os
import re
import signal
import socket
import stat
import sys
from pathlib import Path


def authorize(context, event, policy):
    """Return whether GitHub's runner context and webhook identify trusted work."""
    if not isinstance(event, dict) or not isinstance(policy, dict):
        return False
    repository = event.get("repository")
    sender = event.get("sender")
    if not isinstance(repository, dict) or not isinstance(sender, dict):
        return False
    if event.get("pull_request") is not None:
        return False
    if (
        context.get("GITHUB_REPOSITORY") != policy.get("repository")
        or str(repository.get("id")) != str(policy.get("repository_id"))
        or repository.get("full_name") != policy.get("repository")
        or context.get("GITHUB_REPOSITORY_ID") != str(policy.get("repository_id"))
        or context.get("GITHUB_ACTOR_ID") not in policy.get("actor_ids", [])
        or str(sender.get("id")) != context.get("GITHUB_ACTOR_ID")
    ):
        return False
    event_name = context.get("GITHUB_EVENT_NAME")
    ref = context.get("GITHUB_REF")
    sha = context.get("GITHUB_SHA", "")
    if event_name not in {"push", "workflow_dispatch"} or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False
    if event_name == "push" and (event.get("ref") != ref or event.get("after") != sha):
        return False
    if event_name == "workflow_dispatch" and event.get("ref") not in {
        ref,
        ref.removeprefix("refs/heads/") if isinstance(ref, str) else None,
    }:
        return False
    if ref != "refs/heads/main":
        if event_name != "workflow_dispatch" or policy.get("reviewed_refs", {}).get(ref) != sha:
            return False
    workflow = context.get("GITHUB_WORKFLOW_REF")
    allowed = {
        f"{policy['repository']}/.github/workflows/{name}@{ref}"
        for name in policy.get("workflows", [])
    }
    return workflow in allowed and context.get("GITHUB_WORKFLOW_SHA") == sha


def _root_owned(path):
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022


def _audit(accepted):
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not run_id.isascii() or not run_id.isdecimal():
        run_id = "unknown"
    message = f"Host policy: {'admitted' if accepted else 'refused'} run={run_id}"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as journal:
            journal.settimeout(1)
            journal.sendto(
                f"MESSAGE={message}\nSYSLOG_IDENTIFIER=kinaiya-ci-policy".encode(),
                "/run/systemd/journal/socket",
            )
    except OSError:
        pass  # A logging failure must never bypass refusal.


def _terminate_job():
    parent = os.getppid()
    try:
        if Path(os.readlink(f"/proc/{parent}/exe")).name == "Runner.Worker":
            os.kill(parent, signal.SIGKILL)
            return
    except OSError:
        pass
    # An unexpected worker layout must also fail closed. Only this exact service
    # can be stopped; the dedicated UID has no authority over other services.
    try:
        if (
            "0::/system.slice/kinaiya-ci.service"
            not in Path("/proc/self/cgroup").read_text().splitlines()
        ):
            return
        pids = Path("/sys/fs/cgroup/system.slice/kinaiya-ci.service/cgroup.procs").read_text()
        for value in pids.split():
            pid = int(value)
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except OSError:
        pass


def main():
    try:
        installed = Path(__file__)
        policy_path = installed.with_name("policy.json")
        if not all(_root_owned(path) for path in (installed, policy_path)):
            raise ValueError("policy must be root-owned and immutable to jobs")
        event_path = Path(os.environ["GITHUB_EVENT_PATH"])
        if event_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("oversized webhook")
        policy = json.loads(policy_path.read_text())
        event = json.loads(event_path.read_text())
        accepted = authorize(os.environ, event, policy)
    except Exception:
        # Includes malformed root configuration and recursive/invalid JSON.
        accepted = False
    _audit(accepted)
    print(
        "Host policy: trusted job admitted" if accepted else "Host policy: job refused", flush=True
    )
    if not accepted:
        # Stop the worker itself, so neither pre/post actions nor always() steps
        # can run after refusal. Limit the signal to our original runner parent.
        _terminate_job()
    return 0 if accepted else 1


if __name__ == "__main__":
    sys.exit(main())
