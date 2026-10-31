"""Read-only references are a native scope, independent of owner grants."""

import os

import pytest

from core.policy import PolicyDenied, ToolPolicy
from services.agent.tools import (
    _readonly_bindings,
    build_bwrap_args,
    run_read,
    run_search,
    run_write,
)


def references(tmp_path, **kwargs):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/spec.txt").write_text("frozen public contract")
    (tmp_path / "README.md").write_text("readable public reference")
    return ToolPolicy(
        tmp_path, readonly_roots=[tmp_path / "tests", tmp_path / "README.md"], **kwargs
    )


def test_native_read_and_search_remain_available(tmp_path):
    policy = references(tmp_path)
    assert run_read(policy, "README.md", 65536, 1)[0] == "readable public reference"
    assert "tests/spec.txt:1:frozen" in run_search(policy, "frozen", None, 1)[0]
    assert "README.md:1:readable" in run_search(policy, "readable", "README.md", 1)[0]


@pytest.mark.parametrize("grants", [{}, {"allow_write": True}, {"yolo": True}])
@pytest.mark.parametrize("path", ["README.md", "tests/spec.txt", "tests/new.txt", "tests", "."])
def test_write_scope_survives_grants_and_owner_approval(tmp_path, grants, path):
    policy = references(tmp_path, **grants)
    digest = policy.digest_for("write_file", {"path": path, "content": "bad"}, 1)
    approval = policy.create_approval(digest, 1)
    with pytest.raises(PolicyDenied, match="read-only"):
        run_write(policy, path, "bad", 1, approval_id=approval.approval_id, digest=digest)
    assert not approval.used
    assert (tmp_path / "README.md").read_text() == "readable public reference"


def test_unrelated_source_write_and_canonical_alias_denial(tmp_path):
    policy = references(tmp_path, yolo=True)
    run_write(policy, "source.py", "permitted source", 1)
    (tmp_path / "alias").symlink_to(tmp_path / "tests", target_is_directory=True)
    with pytest.raises(PolicyDenied):
        run_write(policy, "alias/spec.txt", "bad", 1)
    assert (tmp_path / "source.py").read_text() == "permitted source"


def test_replaced_reference_symlink_does_not_move_write_boundary(tmp_path):
    policy = references(tmp_path, yolo=True)
    (tmp_path / "README.md").unlink()
    (tmp_path / "ordinary.txt").write_text("ordinary")
    (tmp_path / "README.md").symlink_to(tmp_path / "ordinary.txt")
    with pytest.raises(PolicyDenied):
        run_write(policy, "README.md", "bad", 1)
    assert (tmp_path / "ordinary.txt").read_text() == "ordinary"
    with pytest.raises(PolicyDenied):
        with _readonly_bindings(policy):
            pytest.fail("changed source must not be mounted")


def test_protected_roots_still_deny_reads_and_search(tmp_path):
    protected = tmp_path / "core"
    protected.mkdir()
    (protected / "secret").write_text("protected")
    policy = ToolPolicy(
        tmp_path, yolo=True, readonly_roots=[protected], protected_roots=[protected]
    )
    with pytest.raises(PolicyDenied):
        run_read(policy, "core/secret", 65536, 1)
    with pytest.raises(PolicyDenied):
        run_search(policy, "protected", "core", 1)
    assert run_search(policy, "protected", None, 1)[0] == "(no matches)"


@pytest.mark.parametrize("shape", ["missing", "hardlink", "symlink", "fifo"])
def test_shell_rejects_unpinnable_or_aliased_reference(tmp_path, shape):
    ref = tmp_path / "ref"
    ref.mkdir()
    item = ref / "item"
    if shape == "missing":
        ref.rmdir()
    elif shape == "fifo":
        os.mkfifo(item)
    elif shape == "symlink":
        item.symlink_to(tmp_path / "outside")
    else:
        item.write_text("reference")
        (tmp_path / "alias").hardlink_to(item)
    policy = ToolPolicy(tmp_path, readonly_roots=[ref])
    with pytest.raises(PolicyDenied):
        with _readonly_bindings(policy):
            pytest.fail("unsafe reference mounted")


def test_descriptor_sources_are_closed_and_ancestors_pinned(tmp_path):
    nested = tmp_path / "parent/deeper"
    nested.mkdir(parents=True)
    root = nested / "reference.txt"
    root.write_text("frozen")
    policy = ToolPolicy(tmp_path, readonly_roots=[root])
    with pytest.raises(PolicyDenied, match="pinned"):
        build_bwrap_args(policy, "true", False)
    with _readonly_bindings(policy) as (bindings, descriptors):
        argv, isolated = build_bwrap_args(policy, "true", False, readonly_bindings=bindings)
        assert isolated and "--ro-bind-fd" in argv
        assert [str(row[2]) for row in bindings if row[0] == "--bind-fd"] == [
            "/workspace",
            "/workspace/parent",
            "/workspace/parent/deeper",
        ]
        assert all(os.fstat(fd) for fd in descriptors)
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_pinned_mount_source_survives_path_replacement(tmp_path):
    root = tmp_path / "reference.txt"
    root.write_text("original reference")
    policy = ToolPolicy(tmp_path, readonly_roots=[root])
    with _readonly_bindings(policy) as (bindings, _):
        root.rename(tmp_path / "old-reference.txt")
        root.symlink_to("/not-mounted/private")
        descriptor = next(row[1] for row in bindings if row[0] == "--ro-bind-fd")
        assert os.read(descriptor, 100) == b"original reference"


def test_relative_references_are_workspace_relative(tmp_path):
    (tmp_path / "README.md").write_text("public")
    policy = ToolPolicy(tmp_path, yolo=True, readonly_roots=["README.md"])
    assert policy.readonly_roots == (tmp_path / "README.md",)
    assert run_read(policy, "README.md", 65536, 1)[0] == "public"
    with pytest.raises(PolicyDenied):
        run_write(policy, "README.md", "bad", 1)


def test_directory_swap_after_write_check_cannot_enter_readonly_root(tmp_path, monkeypatch):
    policy = references(tmp_path, yolo=True)
    writable = tmp_path / "writable"
    writable.mkdir()
    original = policy.check_write

    def race(path, **kwargs):
        resolved = original(path, **kwargs)
        writable.rmdir()
        writable.symlink_to(tmp_path / "tests", target_is_directory=True)
        return resolved

    monkeypatch.setattr(policy, "check_write", race)
    with pytest.raises(PolicyDenied):
        run_write(policy, "writable/spec.txt", "bad", 1)
    assert (tmp_path / "tests/spec.txt").read_text() == "frozen public contract"


def test_deep_readonly_subtree_fails_closed_without_fd_leak(tmp_path):
    root = tmp_path / "reference"
    root.mkdir()
    current = root
    for _ in range(33):
        current = current / "nested"
        current.mkdir()
    (current / "leaf").write_text("public")
    policy = ToolPolicy(tmp_path, readonly_roots=[root])
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(PolicyDenied, match="structural bound"):
        with _readonly_bindings(policy):
            pytest.fail("overlarge reference mounted")
    assert set(os.listdir("/proc/self/fd")) == before


@pytest.mark.parametrize("parent_alias", [False, True])
def test_configured_symlink_reference_is_refused(tmp_path, parent_alias):
    target = tmp_path / "target"
    target.mkdir()
    (target / "reference").write_text("public")
    alias = tmp_path / "alias"
    alias.symlink_to(
        target if parent_alias else target / "reference", target_is_directory=parent_alias
    )
    root = alias / "reference" if parent_alias else alias
    with pytest.raises(PolicyDenied, match="symlink aliases"):
        ToolPolicy(tmp_path, readonly_roots=[root])


def test_reference_ancestor_covers_whole_workspace_mount(tmp_path):
    policy = ToolPolicy(tmp_path, yolo=True, readonly_roots=[tmp_path.parent])
    with pytest.raises(PolicyDenied):
        run_write(policy, "new.txt", "bad", 1)
    with _readonly_bindings(policy) as (bindings, _):
        assert [str(row[2]) for row in bindings if row[0] == "--ro-bind-fd"] == ["/workspace"]


def test_unrelated_external_reference_never_becomes_a_mount(tmp_path):
    policy = ToolPolicy(tmp_path, readonly_roots=[tmp_path.parent / "not-in-workspace"])
    with _readonly_bindings(policy) as (bindings, descriptors):
        assert not bindings and not descriptors
