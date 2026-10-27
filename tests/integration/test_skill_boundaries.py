"""Synthetic filesystem races and parser limits; no private files or services."""

import gzip
import http.server
import io
import os
import tarfile
import threading
from pathlib import Path

import pytest

from packages.ecosystem import skills
from packages.ecosystem.skills import SkillError, SkillManager, SourceRef


def source(root, extra=""):
    root.mkdir()
    (root / "SKILL.md").write_text(
        f"---\nname: boundary\ndescription: Public synthetic instructions\n{extra}---\nPublic body.\n"
    )
    (root / "resource.txt").write_text("public")
    return root


def test_import_file_swap_cannot_follow_outside_symlink(tmp_path, monkeypatch):
    src = source(tmp_path / "source")
    outside = tmp_path / "outside.txt"
    outside.write_text("SYNTHETIC_SENTINEL")
    manager = SkillManager(tmp_path / "state")
    original = os.open
    attacked = False

    def race(path, flags, *args, **kwargs):
        nonlocal attacked
        if not attacked and str(path) == "resource.txt" and kwargs.get("dir_fd") is not None:
            attacked = True
            (src / "resource.txt").unlink()
            (src / "resource.txt").symlink_to(outside)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    with pytest.raises(SkillError, match="symlink"):
        manager.install(src, authority="owner")
    assert attacked and manager.list() == []
    assert outside.read_text() == "SYNTHETIC_SENTINEL"


@pytest.mark.parametrize("control", [".versions", "installed", "registry.json"])
def test_control_symlinks_do_not_create_outside_state(tmp_path, control):
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("SYNTHETIC_SENTINEL")
    (root / control).symlink_to(marker if control == "registry.json" else outside)
    with pytest.raises(SkillError, match="symlink"):
        SkillManager(root)
    assert list(outside.iterdir()) == [marker]
    assert marker.read_text() == "SYNTHETIC_SENTINEL"


def test_control_root_ancestor_symlink_and_workspace_overlap_fail_before_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SkillError, match="symlink"):
        SkillManager(alias / "state")
    assert not list(outside.iterdir())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SkillError, match="disjoint"):
        SkillManager(workspace / "state", workspace=workspace)
    assert not (workspace / "state").exists()


def test_import_metadata_and_resources_come_from_one_captured_snapshot(tmp_path, monkeypatch):
    src = source(tmp_path / "source")
    original = skills._capture_pinned

    def capture(root, **kwargs):
        blobs = original(root, **kwargs)
        if Path(root) == src:
            (src / "SKILL.md").write_text("---\nname: changed\ndescription: Changed\n---\nchanged")
            (src / "resource.txt").write_text("changed")
        return blobs

    monkeypatch.setattr(skills, "_capture_pinned", capture)
    manager = SkillManager(tmp_path / "state")
    record = manager.install(src, authority="owner")
    manager.enable(record.id, authority="owner")
    assert record.name == "boundary"
    assert manager.load(record.id).body == "Public body.\n"
    assert manager.load_resource(record.id, "resource.txt") == b"public"


@pytest.mark.parametrize("flag", ["disable-model-invocation: true\n", "manual-only: true\n"])
def test_invocation_limits_apply_to_instructions_and_resources(tmp_path, flag):
    manager = SkillManager(tmp_path / "state")
    record = manager.install(source(tmp_path / "source", flag), authority="owner")
    manager.enable(record.id, authority="owner")
    with pytest.raises(SkillError, match="invocation"):
        manager.load(record.id, invocation="model")
    with pytest.raises(SkillError, match="invocation"):
        manager.load_resource(record.id, "resource.txt", invocation="model")
    assert manager.load(record.id, invocation="user").untrusted
    assert manager.load_resource(record.id, "resource.txt", invocation="user") == b"public"


def test_tampered_invocation_metadata_does_not_override_pinned_author_controls(tmp_path):
    manager = SkillManager(tmp_path / "state")
    record = manager.install(
        source(tmp_path / "source", "disable-model-invocation: true\n"), authority="owner"
    )
    manager.enable(record.id, authority="owner")
    manager._registry[record.id]["model_invocable"] = True
    with pytest.raises(SkillError, match="metadata"):
        manager.load(record.id, invocation="model")


def test_compressed_pax_metadata_is_capped_before_large_allocation(tmp_path):
    archive = tmp_path / "bomb.tar.gz"
    size = skills.MAX_TAR_STREAM_BYTES + 4096
    header = tarfile.TarInfo("synthetic-pax")
    header.type = tarfile.XHDTYPE
    header.size = size
    with gzip.open(archive, "wb") as output:
        output.write(header.tobuf())
        remaining = size
        while remaining:
            count = min(65536, remaining)
            output.write(b"0" * count)
            remaining -= count
        output.write(b"\0" * 1024)
    assert archive.stat().st_size < 65536
    manager = SkillManager(tmp_path / "state")
    with pytest.raises(SkillError, match="size limit"):
        manager.install_archive(archive, authority="owner")
    assert manager.list() == []


def test_many_directory_members_are_rejected_before_extracting(tmp_path):
    archive = tmp_path / "many.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for i in range(skills.MAX_ARCHIVE_MEMBERS + 1):
            info = tarfile.TarInfo(f"dir{i}")
            info.type = tarfile.DIRTYPE
            output.addfile(info)
    manager = SkillManager(tmp_path / "state")
    with pytest.raises(SkillError, match="count limit"):
        manager.install_archive(archive, authority="owner")
    assert manager.list() == []


def test_codeload_root_directory_header_is_supported_and_siblings_rejected():
    for outside in (False, True):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            root = tarfile.TarInfo("repository-sha")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            info = tarfile.TarInfo("repository-sha/SKILL.md")
            info.size = 6
            archive.addfile(info, io.BytesIO(b"public"))
            if outside:
                other = tarfile.TarInfo("other/outside")
                other.size = 1
                archive.addfile(other, io.BytesIO(b"x"))
        stream = skills._capped_tar_stream(
            io.BytesIO(gzip.compress(buffer.getvalue())), "synthetic", is_gzip=True
        )
        if outside:
            with pytest.raises(SkillError, match="outside top directory"):
                skills._collect_tar_blobs_streaming(stream, "synthetic", strip_top="auto")
        else:
            assert skills._collect_tar_blobs_streaming(stream, "synthetic", strip_top="auto") == [
                ("SKILL.md", b"public")
            ]


def test_unknown_adapter_and_deep_yaml_never_become_ready(tmp_path):
    manager = SkillManager(tmp_path / "state")
    record = manager.install(
        source(tmp_path / "source", "compatibility: hermes\n"), authority="owner"
    )
    assert record.status == "needs_adapter" and not record.enabled
    with pytest.raises(SkillError, match="adapter"):
        manager.enable(record.id, authority="owner")
    deep = source(tmp_path / "deep", "extra: " + "[" * 80 + "0" + "]" * 80 + "\n")
    with pytest.raises(SkillError, match="structural bounds"):
        manager.install(deep, authority="owner")


def test_canonical_github_host_and_typed_reference_reject_embedded_credentials():
    with pytest.raises(SkillError, match="canonical"):
        SourceRef.parse("https://example.invalid/github.com/owner/repo/tree/" + "a" * 40)
    with pytest.raises(SkillError, match="credentials"):
        SourceRef(
            kind="github",
            namespace="github:owner",
            name="repo",
            ref="a" * 40,
            raw="https://user:synthetic-password@github.com/owner/repo",
        ).validate()


def test_pinned_monorepo_subdirectory_installs_frontmatter_name(tmp_path):
    payload = io.BytesIO()
    sha = "a" * 40
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, data in [
            (
                "repo-sha/skills/example/SKILL.md",
                b"---\nname: boundary\ndescription: Public\n---\nBody",
            ),
            ("repo-sha/skills/example/LICENSE", b"Synthetic license fixture"),
            ("repo-sha/README.md", b"Repository root"),
        ]:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == f"/owner/monorepo/tar.gz/{sha}"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload.getvalue())

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ref = SourceRef.parse(f"https://github.com/owner/monorepo/tree/{sha}/skills/example")
        selected = skills.fetch_github_pinned(
            ref, tmp_path / "fetch", base_url=f"http://127.0.0.1:{server.server_port}"
        )
        assert selected == tmp_path / "fetch/skills/example"
        manager = SkillManager(tmp_path / "state")
        record = manager.install(selected, source_ref=ref, authority="owner")
        assert record.id == "github:owner/boundary"
        assert record.source["repository"] == "monorepo"
        assert record.source["raw"] == f"github:owner/monorepo/skills/example@{sha}"
        manager.enable(record.id, authority="owner")
        assert manager.load_resource(record.id, "LICENSE") == b"Synthetic license fixture"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_frontmatter_delimiter_is_a_whole_line(tmp_path):
    src = source(tmp_path / "source")
    (src / "SKILL.md").write_text(
        '---\nname: boundary\ndescription: "A --- quoted description"\n---\nBody with --- text.'
    )
    manager = SkillManager(tmp_path / "state")
    record = manager.install(src, authority="owner")
    manager.enable(record.id, authority="owner")
    assert manager.load(record.id).body == "Body with --- text.\n"


def test_rollback_uses_the_verified_snapshot_even_if_source_changes(tmp_path, monkeypatch):
    manager = SkillManager(tmp_path / "state")
    src = source(tmp_path / "source")
    record = manager.install(src, authority="owner")
    (src / "resource.txt").write_text("new version")
    manager.update(record.id, src, authority="owner")
    original = skills._capture_pinned

    def capture(root, **kwargs):
        blobs = original(root, **kwargs)
        if Path(root).name == "files":
            (Path(root) / "resource.txt").write_text("MUTATED AFTER VERIFICATION")
        return blobs

    monkeypatch.setattr(skills, "_capture_pinned", capture)
    manager.rollback(record.id, authority="owner")
    manager.enable(record.id, authority="owner")
    assert manager.load_resource(record.id, "resource.txt") == b"public"
