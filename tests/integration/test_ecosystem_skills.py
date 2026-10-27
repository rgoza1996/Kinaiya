"""Bounded Skills-compatible SKILL.md filesystem lifecycle.

Covers absolute roots, traversal, all-member archive validation, tamper,
owner authority, collisions, enable/setup gating, and byte-for-byte rollback.
"""

import http.server
import io
import os
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from packages.ecosystem.skills import (
    OWNER_AUTHORITY,
    SkillError,
    SkillManager,
    SourceRef,
    fetch_github_pinned,
)

PINNED_SHA = "a" * 40
SKILL_MD = b"---\nname: demo\ndescription: demo skill\nversion: 1.0.0\n---\nDo things.\n"


def make_skill(
    path, name="demo", version="1.0.0", body="Do the thing.\n", extra_front="", files=None
):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {name} skill\nversion: {version}\n{extra_front}---\n"
    (path / "SKILL.md").write_text(front + body, encoding="utf-8")
    for rel, data in (files or {}).items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def tree_bytes(root):
    root = Path(root)
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def live_dir(mgr, skill_id):
    found = [p for p in (mgr.root / "installed").rglob("SKILL.md")]
    assert found, f"no installed bytes for {skill_id}"
    assert len(found) == 1
    return found[0].parent


def github_ref(name="demo", ref=PINNED_SHA):
    return SourceRef(
        kind="github",
        namespace="github:acme",
        name=name,
        ref=ref,
        raw=f"github:acme/{name}@{ref}",
    )


# -- install / load -----------------------------------------------------------


def test_install_load_roundtrip_absolute_root(tmp_path):
    assert tmp_path.is_absolute()
    mgr = SkillManager(tmp_path / "registry")
    rec = mgr.install(make_skill(tmp_path / "src"), authority=OWNER_AUTHORITY)
    assert rec.id == "local/demo"
    assert rec.enabled is False
    assert rec.digest.startswith("sha256:")
    with pytest.raises(SkillError, match="not enabled"):
        mgr.load(rec.id)
    mgr.enable(rec.id, authority=OWNER_AUTHORITY)
    loaded = mgr.load(rec.id)
    assert loaded.untrusted is True
    assert "Do the thing." in loaded.body
    assert "Do the thing." not in str(mgr.list())
    assert mgr.verify(rec.id).digest == rec.digest


def test_dict_source_ref_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src")
    with pytest.raises(SkillError, match="typed SourceRef"):
        mgr.install(src, source_ref={"kind": "local"}, authority=OWNER_AUTHORITY)  # type: ignore[arg-type]


def test_ref_name_must_match_skill_md(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src", name="demo")
    with pytest.raises(SkillError, match="does not match"):
        mgr.install(src, source_ref=github_ref(name="other"), authority=OWNER_AUTHORITY)


def test_unpinned_github_source_refused_at_install(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src", name="demo")
    moving = SourceRef.parse("acme/demo@main")
    assert not moving.is_pinned
    with pytest.raises(SkillError, match="pinned"):
        mgr.install(src, source_ref=moving, authority=OWNER_AUTHORITY)


def test_source_ref_parse_rejects_credentials_schemes_queries(tmp_path):
    with pytest.raises(SkillError, match="credentials"):
        SourceRef.parse("https://user:pass@github.com/acme/demo")
    with pytest.raises(SkillError, match="scheme"):
        SourceRef.parse("ftp://github.com/acme/demo")
    with pytest.raises(SkillError, match="query or fragment"):
        SourceRef.parse("https://github.com/acme/demo/tree/abc1234?tab=readme")
    with pytest.raises(SkillError, match="unsupported registry host"):
        SourceRef.parse("https://example.com/acme/demo")
    with pytest.raises(SkillError):
        SourceRef.parse("github:../../evil/repo")


# -- frontmatter / identity validation ----------------------------------------


def test_version_traversal_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    with pytest.raises(SkillError, match="version"):
        mgr.install(make_skill(tmp_path / "bad1", version="../../evil"), authority="owner")
    with pytest.raises(SkillError, match="version"):
        mgr.install(make_skill(tmp_path / "bad2", version="1.0/2"), authority="owner")


def test_namespace_traversal_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src", name="demo")
    evil = SourceRef(
        kind="github", namespace="github:../evil", name="demo", ref=PINNED_SHA, raw="evil"
    )
    with pytest.raises(SkillError):
        mgr.install(src, source_ref=evil, authority="owner")


def test_frontmatter_name_traversal_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    with pytest.raises(SkillError, match="skill name"):
        mgr.install(make_skill(tmp_path / "src", name="../evil"), authority="owner")


def test_frontmatter_alias_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src", extra_front="extra: &anchor\n  x: 1\nother: *anchor\n")
    with pytest.raises(SkillError, match="aliases"):
        mgr.install(src, authority="owner")


def test_frontmatter_duplicate_key_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: demo\ndescription: first\ndescription: second\n---\nBody.\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillError, match="duplicate key"):
        mgr.install(src, authority="owner")


def test_frontmatter_missing_and_nonmapping_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    noname = tmp_path / "noname"
    noname.mkdir()
    (noname / "SKILL.md").write_text("---\ndescription: no name\n---\nB\n", encoding="utf-8")
    with pytest.raises(SkillError, match="name"):
        mgr.install(noname, authority="owner")
    lst = tmp_path / "list"
    lst.mkdir()
    (lst / "SKILL.md").write_text("---\n- just\n- a\n- list\n---\nB\n", encoding="utf-8")
    with pytest.raises(SkillError, match="mapping"):
        mgr.install(lst, authority="owner")


# -- filesystem entrances ------------------------------------------------------


def test_symlinks_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src")
    (src / "link.md").symlink_to(src / "SKILL.md")
    with pytest.raises(SkillError, match="symlink"):
        mgr.install(src, authority="owner")
    (src / "link.md").unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (src / "escape").symlink_to(outside)
    with pytest.raises(SkillError, match="symlink"):
        mgr.install(src, authority="owner")
    (src / "escape").unlink()
    (src / "SKILL.md").rename(src / "SKILL.md.real")
    (src / "SKILL.md").symlink_to(src / "SKILL.md.real")
    with pytest.raises(SkillError, match="symlink"):
        mgr.install(src, authority="owner")
    linked = tmp_path / "linked"
    linked.symlink_to(src)
    with pytest.raises(SkillError, match="not a symlink"):
        mgr.install(linked, authority="owner")


def test_fifo_rejected(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no fifo support")
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src")
    os.mkfifo(src / "pipe")
    with pytest.raises(SkillError, match="special"):
        mgr.install(src, authority="owner")


# -- archives: every member validated, manual extraction -----------------------


def write_zip(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return path


def write_tar(path, members, mode="w:gz"):
    with tarfile.open(path, mode) as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_zip_traversal_all_members_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    evil = tmp_path / "evil.txt"
    archive = write_zip(tmp_path / "s.zip", [("SKILL.md", SKILL_MD), ("../evil.txt", b"x")])
    with pytest.raises(SkillError, match="unsafe name"):
        mgr.install_archive(archive, authority="owner")
    assert not evil.exists()
    archive2 = write_zip(tmp_path / "s2.zip", [("SKILL.md", SKILL_MD), ("/abs.txt", b"x")])
    with pytest.raises(SkillError, match="unsafe name"):
        mgr.install_archive(archive2, authority="owner")


def test_zip_symlink_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120000 | 0o777) << 16
        zf.writestr(info, "SKILL.md")
    with pytest.raises(SkillError, match="link rejected"):
        mgr.install_archive(archive, authority="owner")


def test_zip_oversize_member_rejected(tmp_path):
    from packages.ecosystem.skills import SINGLE_FILE_MAX_BYTES

    mgr = SkillManager(tmp_path / "registry")
    archive = write_zip(
        tmp_path / "big.zip",
        [("SKILL.md", SKILL_MD), ("blob.bin", b"x" * (SINGLE_FILE_MAX_BYTES + 1))],
    )
    with pytest.raises(SkillError, match="too large"):
        mgr.install_archive(archive, authority="owner")


def test_zip_single_top_dir_installs(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    archive = write_zip(
        tmp_path / "pkg.zip", [("pkg/SKILL.md", SKILL_MD), ("pkg/refs/a.txt", b"a")]
    )
    rec = mgr.install_archive(archive, authority="owner")
    assert rec.id == "local/demo"
    assert sorted(rec.files) == ["SKILL.md", "refs/a.txt"]


def test_tar_traversal_all_members_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    evil = tmp_path / "evil.txt"
    archive = write_tar(tmp_path / "s.tar.gz", [("SKILL.md", SKILL_MD), ("../evil.txt", b"x")])
    with pytest.raises(SkillError, match="unsafe name"):
        mgr.install_archive(archive, authority="owner")
    assert not evil.exists()


def test_tar_symlink_hardlink_device_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    for kind, extra in ((tarfile.SYMTYPE, "SKILL.md"), (tarfile.LNKTYPE, "SKILL.md")):
        archive = tmp_path / f"{kind}.tar"
        with tarfile.open(archive, "w") as tf:
            info = tarfile.TarInfo("SKILL.md")
            info.size = len(SKILL_MD)
            tf.addfile(info, io.BytesIO(SKILL_MD))
            link = tarfile.TarInfo("link")
            link.type = kind
            link.linkname = extra
            tf.addfile(link)
        with pytest.raises(SkillError, match="non-regular"):
            mgr.install_archive(archive, authority="owner")
    archive = tmp_path / "dev.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("SKILL.md")
        info.size = len(SKILL_MD)
        tf.addfile(info, io.BytesIO(SKILL_MD))
        dev = tarfile.TarInfo("dev")
        dev.type = tarfile.CHRTYPE
        tf.addfile(dev)
    with pytest.raises(SkillError, match="non-regular"):
        mgr.install_archive(archive, authority="owner")


def test_tar_count_and_total_limits(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    from packages.ecosystem.skills import MAX_FILES_PER_SKILL

    many = [(f"f{i}.txt", b"x") for i in range(MAX_FILES_PER_SKILL + 2)]
    archive = write_tar(tmp_path / "many.tar", [("SKILL.md", SKILL_MD)] + many, mode="w")
    with pytest.raises(SkillError, match="count limit"):
        mgr.install_archive(archive, authority="owner")
    big = [(f"b{i}.bin", b"x" * 1_048_576) for i in range(8)]
    archive = write_tar(tmp_path / "big.tar", [("SKILL.md", SKILL_MD)] + big, mode="w")
    with pytest.raises(SkillError, match="size limit"):
        mgr.install_archive(archive, authority="owner")


# -- tamper --------------------------------------------------------------------


def test_tamper_detected_and_blocks_mutation(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    rec = mgr.install(make_skill(tmp_path / "src"), authority="owner")
    mgr.enable(rec.id, authority="owner")
    target = live_dir(mgr, rec.id)
    (target / "SKILL.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(SkillError, match="changed since install"):
        mgr.verify(rec.id)
    with pytest.raises(SkillError, match="changed since install"):
        mgr.load(rec.id)
    with pytest.raises(SkillError, match="changed since install"):
        mgr.update(rec.id, make_skill(tmp_path / "src2"), authority="owner")


def test_added_symlink_is_tamper(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    rec = mgr.install(make_skill(tmp_path / "src"), authority="owner")
    (live_dir(mgr, rec.id) / "link").symlink_to(tmp_path / "registry.json")
    with pytest.raises(SkillError, match="symlink"):
        mgr.verify(rec.id)


# -- authority -----------------------------------------------------------------


def test_owner_authority_required_for_mutations(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src")
    with pytest.raises(SkillError, match="owner authority"):
        mgr.install(src, authority="agent")
    with pytest.raises(SkillError, match="owner authority"):
        mgr.install(src, authority=None)  # type: ignore[arg-type]
    rec = mgr.install(src, authority="owner")
    for call in (
        lambda: mgr.enable(rec.id, authority="model"),
        lambda: mgr.disable(rec.id, authority="decisions"),
        lambda: mgr.update(rec.id, src, authority="agent"),
        lambda: mgr.rollback(rec.id, authority="agent"),
        lambda: mgr.uninstall(rec.id, authority="agent"),
    ):
        with pytest.raises(SkillError, match="owner authority"):
            call()
    # Discovery needs no authority and cannot mutate.
    assert mgr.list() and mgr.inspect(rec.id)["id"] == rec.id
    assert mgr.versions(rec.id) == []


# -- collisions ----------------------------------------------------------------


def test_namespace_collisions_coexist_deterministically(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src", name="demo")
    first = mgr.install(src, authority="owner")
    second = mgr.install(src, source_ref=github_ref(), authority="owner")
    assert first.id == "local/demo"
    assert second.id == "github:acme/demo"
    assert [m["id"] for m in mgr.list()] == ["github:acme/demo", "local/demo"]


def test_duplicate_install_rejected_and_inspectable(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    src = make_skill(tmp_path / "src", name="demo")
    rec = mgr.install(src, authority="owner")
    with pytest.raises(SkillError, match="already installed.*use update"):
        mgr.install(src, authority="owner")
    assert mgr.inspect(rec.id)["digest"] == rec.digest


# -- update / rollback ----------------------------------------------------------


def test_update_and_byte_for_byte_rollback(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    v1 = make_skill(
        tmp_path / "v1",
        version="1.0.0",
        body="v1 body\n",
        files={"refs/a.txt": b"v1", "bin/run.sh": b"#!/bin/sh\necho v1\n"},
    )
    rec1 = mgr.install(v1, authority="owner")
    mgr.enable(rec1.id, authority="owner")
    v1_bytes = tree_bytes(live_dir(mgr, rec1.id))
    v2 = make_skill(
        tmp_path / "v2",
        version="2.0.0",
        body="v2 body\n",
        files={"refs/a.txt": b"v2", "extra.txt": b"new"},
    )
    rec2 = mgr.update(rec1.id, v2, authority="owner")
    assert rec2.digest != rec1.digest
    assert rec2.previous_digest == rec1.digest
    assert rec2.enabled is True
    assert tree_bytes(live_dir(mgr, rec2.id)) != v1_bytes
    v3 = make_skill(tmp_path / "v3", version="3.0.0", body="v3 body\n", files={"refs/a.txt": b"v3"})
    mgr.update(rec1.id, v3, authority="owner")
    snaps = mgr.versions(rec1.id)
    assert [s["digest"] for s in snaps] == [rec1.digest, rec2.digest]
    back2 = mgr.rollback(rec1.id, authority="owner")
    assert back2.digest == rec2.digest
    assert b"v2" in (live_dir(mgr, rec1.id) / "refs" / "a.txt").read_bytes()
    back1 = mgr.rollback(rec1.id, authority="owner")
    assert back1.digest == rec1.digest
    assert back1.previous_digest == ""
    assert tree_bytes(live_dir(mgr, rec1.id)) == v1_bytes
    assert mgr.verify(rec1.id).digest == rec1.digest
    assert "v1 body" in mgr.load(rec1.id).body


def test_rollback_without_history_rejected(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    rec = mgr.install(make_skill(tmp_path / "src"), authority="owner")
    with pytest.raises(SkillError, match="no previous version"):
        mgr.rollback(rec.id, authority="owner")


# -- enable / setup gating -------------------------------------------------------


def test_missing_setup_and_unsupported_gate_enable(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    needy = make_skill(
        tmp_path / "needy",
        extra_front="requires:\n  exec: [definitely-not-a-real-binary-xyz]\n  pip: [some-pkg]\n",
    )
    rec = mgr.install(needy, authority="owner")
    assert rec.status == "needs_setup"
    assert rec.enabled is False
    with pytest.raises(SkillError, match="needing setup"):
        mgr.enable(rec.id, authority="owner")
    with pytest.raises(SkillError, match="not enabled"):
        mgr.load(rec.id)
    foreign = make_skill(tmp_path / "foreign", name="foreign", extra_front="os: win32\n")
    frec = mgr.install(foreign, authority="owner")
    assert frec.status == "unsupported"
    with pytest.raises(SkillError, match="unsupported"):
        mgr.enable(frec.id, authority="owner")


def test_disable_blocks_load_and_reenable_works(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    rec = mgr.install(make_skill(tmp_path / "src"), authority="owner")
    mgr.enable(rec.id, authority="owner")
    mgr.disable(rec.id, authority="owner")
    assert mgr.inspect(rec.id)["status"] == "disabled"
    with pytest.raises(SkillError, match="not enabled"):
        mgr.load(rec.id)
    mgr.enable(rec.id, authority="owner")
    assert "Do the thing." in mgr.load(rec.id).body


def test_reserved_name_is_qualified_only(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    rec = mgr.install(make_skill(tmp_path / "src", name="stop"), authority="owner")
    assert rec.bare_invocable is False
    mgr.enable(rec.id, authority="owner")
    assert mgr.load("local/stop").name == "stop"


# -- uninstall containment --------------------------------------------------------


def test_uninstall_removes_only_owned_state(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    precious = tmp_path / "precious.txt"
    precious.write_text("keep", encoding="utf-8")
    rec = mgr.install(make_skill(tmp_path / "src"), authority="owner")
    target = live_dir(mgr, rec.id)
    assert target.is_dir()
    mgr.uninstall(rec.id, authority="owner")
    assert not target.exists()
    assert precious.read_text(encoding="utf-8") == "keep"
    with pytest.raises(SkillError, match="unknown skill"):
        mgr.inspect(rec.id)
    with pytest.raises(SkillError, match="unknown skill"):
        mgr.uninstall(rec.id, authority="owner")


# -- resources ---------------------------------------------------------------------


def test_load_resource_bounded_and_contained(tmp_path):
    mgr = SkillManager(tmp_path / "registry")
    rec = mgr.install(
        make_skill(tmp_path / "src", files={"refs/a.txt": b"hello"}), authority="owner"
    )
    mgr.enable(rec.id, authority="owner")
    assert mgr.load_resource(rec.id, "refs/a.txt") == b"hello"
    with pytest.raises(SkillError, match="escapes"):
        mgr.load_resource(rec.id, "../../precious")
    with pytest.raises(SkillError, match="relative"):
        mgr.load_resource(rec.id, "/etc/hostname")
    with pytest.raises(SkillError, match="not a regular file"):
        mgr.load_resource(rec.id, "refs/missing.txt")


# -- loopback fetch ------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""
    mode = "tarball"

    def do_GET(self):
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/other.tar.gz")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):
        pass


def serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def codeload_tarball(top, members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members:
            info = tarfile.TarInfo(top + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_fetch_rejects_redirect_without_following(tmp_path):
    _Handler.mode = "redirect"
    server = serve(_Handler)
    try:
        ref = github_ref()
        with pytest.raises(SkillError, match="[Rr]edirect"):
            fetch_github_pinned(
                ref,
                tmp_path / "dest",
                base_url=f"http://127.0.0.1:{server.server_port}",
            )
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_loopback_tarball_validates_all_members(tmp_path):
    evil = codeload_tarball("acme-demo-aaa/", [("SKILL.md", SKILL_MD), ("../evil.txt", b"x")])
    _Handler.payload = evil
    _Handler.mode = "tarball"
    server = serve(_Handler)
    try:
        with pytest.raises(SkillError, match="outside top directory|unsafe name"):
            fetch_github_pinned(
                github_ref(),
                tmp_path / "dest",
                base_url=f"http://127.0.0.1:{server.server_port}",
            )
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_loopback_tarball_success_installs(tmp_path):
    good = codeload_tarball("acme-demo-aaa/", [("SKILL.md", SKILL_MD), ("refs/a.txt", b"a")])
    _Handler.payload = good
    _Handler.mode = "tarball"
    server = serve(_Handler)
    try:
        dest = fetch_github_pinned(
            github_ref(),
            tmp_path / "fetched",
            base_url=f"http://127.0.0.1:{server.server_port}",
        )
        assert (dest / "SKILL.md").is_file()
        mgr = SkillManager(tmp_path / "registry")
        rec = mgr.install(dest, source_ref=github_ref(), authority="owner")
        assert rec.id == "github:acme/demo"
        assert rec.version == "1.0.0"
    finally:
        server.shutdown()
        server.server_close()


def test_remote_modes_fail_explicitly_without_network(tmp_path):
    with pytest.raises(SkillError, match="ClawHub"):
        fetch_github_pinned(SourceRef.parse("clawhub:scope/demo@1.0.0"), tmp_path / "d1")
    with pytest.raises(SkillError, match="pinned"):
        fetch_github_pinned(SourceRef.parse("acme/demo@main"), tmp_path / "d2")
    with pytest.raises(SkillError, match="typed SourceRef"):
        fetch_github_pinned({"kind": "github"}, tmp_path / "d3")  # type: ignore[arg-type]
