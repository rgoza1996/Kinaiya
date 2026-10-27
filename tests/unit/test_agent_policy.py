"""Unit tests for core.policy.ToolPolicy: scoping, grants, approvals."""

import time

import pytest

from core.policy import ApprovalError, PolicyDenied, ToolPolicy, action_digest


def make_policy(tmp_path, **kw):
    return ToolPolicy(workspace=tmp_path, **kw)


def test_read_inside_workspace_ok(tmp_path):
    policy = make_policy(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hi")
    assert policy.check_read("a.txt") == target.resolve()


def test_absolute_workspace_path_ok(tmp_path):
    policy = make_policy(tmp_path)
    target = tmp_path / "sub" / "b.txt"
    target.parent.mkdir()
    target.write_text("hi")
    assert policy.check_read(str(target)) == target.resolve()


def test_traversal_outside_workspace_denied(tmp_path):
    policy = make_policy(tmp_path, allow_write=True)
    with pytest.raises(PolicyDenied):
        policy.check_read("../escape.txt")
    with pytest.raises(PolicyDenied):
        policy.check_write("../escape.txt")


def test_symlink_escape_denied(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    policy = make_policy(tmp_path, allow_write=True)
    with pytest.raises(PolicyDenied):
        policy.check_read("link.txt")
    with pytest.raises(PolicyDenied):
        policy.check_write("link.txt", approval_id=None, digest=None)


def test_symlink_dir_escape_denied(tmp_path):
    outside_dir = tmp_path.parent / "outside-dir"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "x.txt").write_text("x")
    linkdir = tmp_path / "d"
    linkdir.symlink_to(outside_dir)
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyDenied):
        policy.check_search("d")


def test_protected_roots_never_writable(tmp_path):
    protected = tmp_path / "core"
    protected.mkdir()
    policy = make_policy(tmp_path, allow_write=True, yolo=True, protected_roots=(protected,))
    with pytest.raises(PolicyDenied):
        policy.check_write("core/policy.py")


def test_search_defaults_to_workspace(tmp_path):
    policy = make_policy(tmp_path)
    assert policy.check_search(None) == tmp_path.resolve()
    assert policy.check_search(".") == tmp_path.resolve()


def test_write_without_grant_needs_approval(tmp_path):
    policy = make_policy(tmp_path)
    assert policy.needs_approval("write_file", {"path": "a.txt"})
    assert policy.needs_approval("shell", {"command": "echo hi"})
    assert not policy.needs_approval("read_file", {"path": "a.txt"})
    with pytest.raises(ApprovalError):
        policy.check_write("a.txt")
    with pytest.raises(ApprovalError):
        policy.check_shell()


def test_grants_cover_workspace_actions(tmp_path):
    policy = make_policy(tmp_path, allow_write=True, allow_shell=True)
    assert not policy.needs_approval("write_file", {})
    assert not policy.needs_approval("shell", {})
    assert policy.check_write("a.txt") == (tmp_path / "a.txt").resolve()
    policy.check_shell()  # no raise


def test_yolo_grants_workspace_only(tmp_path):
    protected = tmp_path / "evaluator"
    protected.mkdir()
    policy = make_policy(tmp_path, yolo=True, protected_roots=(protected,))
    assert policy.effective_write and policy.effective_shell
    policy.check_write("work.txt")  # allowed
    with pytest.raises(PolicyDenied):
        policy.check_write("evaluator/scores.json")
    with pytest.raises(PolicyDenied):
        policy.check_write("../escape.txt")


def test_permission_changes_owner_only(tmp_path):
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyDenied):
        policy.set_permissions(actor="decisions", allow_write=True)
    with pytest.raises(PolicyDenied):
        policy.set_permissions(actor="intruder", yolo=True)
    policy.set_permissions(actor="owner", allow_write=True)
    assert policy.effective_write


def test_approval_single_use_digest_bound(tmp_path):
    policy = make_policy(tmp_path)
    digest = policy.digest_for("write_file", {"path": "a.txt", "content": "1"}, 1)
    approval = policy.create_approval(digest, 1)
    policy.consume_approval(approval.approval_id, digest, 1)
    with pytest.raises(ApprovalError):
        policy.consume_approval(approval.approval_id, digest, 1)


def test_approval_rejects_changed_action(tmp_path):
    policy = make_policy(tmp_path)
    digest = policy.digest_for("write_file", {"path": "a.txt", "content": "1"}, 1)
    approval = policy.create_approval(digest, 1)
    other = policy.digest_for("write_file", {"path": "a.txt", "content": "2"}, 1)
    with pytest.raises(ApprovalError):
        policy.consume_approval(approval.approval_id, other, 1)
    # Unspent approval still valid for the original action afterwards.
    policy.consume_approval(approval.approval_id, digest, 1)


def test_approval_rejects_stale_revision(tmp_path):
    policy = make_policy(tmp_path)
    digest = policy.digest_for("write_file", {"path": "a.txt", "content": "1"}, 1)
    approval = policy.create_approval(digest, 1)
    with pytest.raises(ApprovalError):
        policy.consume_approval(approval.approval_id, digest, 2)


def test_approval_expires(tmp_path):
    policy = make_policy(tmp_path)
    digest = policy.digest_for("shell", {"command": "echo hi"}, 1)
    approval = policy.create_approval(digest, 1, ttl_s=-1)
    assert approval.expires_at < time.time()
    with pytest.raises(ApprovalError):
        policy.consume_approval(approval.approval_id, digest, 1)


def test_approval_unknown_id(tmp_path):
    policy = make_policy(tmp_path)
    with pytest.raises(ApprovalError):
        policy.consume_approval("apr_nope", "sha256:x", 1)


def test_digest_stable_and_revision_scoped():
    args = {"path": "a.txt", "content": "1"}
    assert action_digest("write_file", args, 1) == action_digest("write_file", args, 1)
    assert action_digest("write_file", args, 1) != action_digest("write_file", args, 2)
    assert action_digest("write_file", args, 1) != action_digest("shell", args, 1)
