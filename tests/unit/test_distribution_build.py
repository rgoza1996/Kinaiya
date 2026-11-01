"""Real Git fixture and deterministic archive assertions for #262."""

import json
import subprocess
import tarfile

import pytest

from packages.distribution.releases import ReleaseError, validate_manifest, verify_artifact


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Release fixture")
    for name, text in {
        "pyproject.toml": '[project]\nname="fixture"\nversion="0.1.0"\n',
        "requirements-runtime.lock": "# pinned fixture\n",
        "LICENSE": "Fixture license\n",
        "core/__init__.py": "VALUE = 'committed'\n",
        "packages/__init__.py": "",
        "services/__init__.py": "",
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    return repo


def test_source_bundle_is_reproducible_and_ignores_worktree_secrets(source_repo, tmp_path):
    from scripts.build_release import build_release

    sha = git(source_repo, "rev-parse", "HEAD")
    (source_repo / ".env").write_text("DO_NOT_SHIP=synthetic\n")
    (source_repo / "core/__init__.py").write_text("DO_NOT_SHIP = 'dirty'\n")
    first = build_release(
        source_repo,
        tmp_path / "first",
        channel="nightly",
        version="0.2.0",
        build_number=123,
        source_sha=sha,
    )
    second = build_release(
        source_repo,
        tmp_path / "second",
        channel="nightly",
        version="0.2.0",
        build_number=123,
        source_sha=sha,
    )
    assert validate_manifest(first) == second
    artifact = first["artifacts"][0]
    one, two = [tmp_path / directory / artifact["name"] for directory in ("first", "second")]
    assert one.read_bytes() == two.read_bytes()
    verify_artifact(one, artifact)
    with tarfile.open(one) as archive:
        assert ".env" not in archive.getnames()
        assert archive.extractfile("core/__init__.py").read() == b"VALUE = 'committed'\n"
        identity = json.load(archive.extractfile("release-identity.json"))
        assert identity["source_sha"] == sha
        assert identity["channel"] == "nightly"
        assert identity["build_number"] == 123
        assert all(member.uid == 0 and member.gid == 0 and member.mtime == 0 for member in archive)
    assert json.loads((tmp_path / "first/release-manifest.json").read_text()) == first


def test_bundle_source_is_explicit_allowlist_with_lock_inventory(source_repo, tmp_path):
    from scripts.build_release import build_release

    for name in (
        ".env",
        "data/config.json",
        "workspace/private.py",
        "core/.env",
        "core/credentials.json",
        "services/config.json",
        "packages/token.pem",
        "docs/private.txt",
    ):
        path = source_repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC_SECRET_DO_NOT_SHIP")
    git(source_repo, "add", ".")
    git(source_repo, "commit", "-qm", "synthetic private files")
    sha = git(source_repo, "rev-parse", "HEAD")
    data = build_release(
        source_repo,
        tmp_path / "out",
        channel="stable",
        version="0.2.0",
        build_number=124,
        source_sha=sha,
    )
    with tarfile.open(tmp_path / "out" / data["artifacts"][0]["name"]) as archive:
        assert set(archive.getnames()) == {
            "core/__init__.py",
            "packages/__init__.py",
            "services/__init__.py",
            "pyproject.toml",
            "requirements-runtime.lock",
            "LICENSE",
            "release-identity.json",
            "release-inventory.json",
        }
        assert b"SYNTHETIC_SECRET" not in b"".join(
            archive.extractfile(member).read() for member in archive
        )
        inventory = json.load(archive.extractfile("release-inventory.json"))
        assert inventory["source_sha"] == sha
        assert inventory["runtime_lock"]["name"] == "requirements-runtime.lock"
        assert len(inventory["runtime_lock"]["sha256"]) == 64


@pytest.mark.parametrize("source", ["main", "A" * 40, "a" * 39, "--help"])
def test_bundle_refuses_non_exact_source_before_git(source_repo, tmp_path, source):
    from scripts.build_release import build_release

    with pytest.raises(ReleaseError, match="source_sha"):
        build_release(
            source_repo,
            tmp_path / "out",
            channel="stable",
            version="0.2.0",
            build_number=124,
            source_sha=source,
        )
    assert not (tmp_path / "out").exists()


def test_bundle_requires_locked_runtime_and_refuses_git_symlinks(source_repo, tmp_path):
    from scripts.build_release import build_release

    git(source_repo, "rm", "requirements-runtime.lock")
    git(source_repo, "commit", "-qm", "missing runtime lock")
    with pytest.raises(ReleaseError, match="runtime lock"):
        build_release(
            source_repo,
            tmp_path / "out",
            channel="stable",
            version="0.2.0",
            build_number=124,
            source_sha=git(source_repo, "rev-parse", "HEAD"),
        )
    git(source_repo, "reset", "--hard", "HEAD~1")
    oid = git(source_repo, "hash-object", "-w", "--stdin")
    git(source_repo, "update-index", "--add", "--cacheinfo", f"120000,{oid},core/link.py")
    git(source_repo, "commit", "-qm", "unsafe git symlink")
    with pytest.raises(ReleaseError, match="symlink"):
        build_release(
            source_repo,
            tmp_path / "out2",
            channel="stable",
            version="0.2.0",
            build_number=124,
            source_sha=git(source_repo, "rev-parse", "HEAD"),
        )


def test_matching_web_bundle_is_embedded_and_has_same_identity(source_repo, tmp_path):
    from scripts.build_release import build_release

    sha = git(source_repo, "rev-parse", "HEAD")
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>fixture</html>")
    (web / "main.dart.js").write_text("// compiled fixture")
    data = build_release(
        source_repo,
        tmp_path / "out",
        channel="nightly",
        version="0.2.0",
        build_number=125,
        source_sha=sha,
        web_dir=web,
        web_source_sha=sha,
    )
    assert [a["platform"] for a in data["artifacts"]] == ["linux", "web"]
    for artifact in data["artifacts"]:
        verify_artifact(tmp_path / "out" / artifact["name"], artifact)
        with tarfile.open(tmp_path / "out" / artifact["name"]) as archive:
            assert json.load(archive.extractfile("release-identity.json"))["source_sha"] == sha
            index = (
                "apps/kinaiya_flutter/build/web/index.html"
                if artifact["platform"] == "linux"
                else "index.html"
            )
            assert archive.extractfile(index).read() == b"<html>fixture</html>"
    with pytest.raises(ReleaseError, match="web.*source"):
        build_release(
            source_repo,
            tmp_path / "mismatch",
            channel="stable",
            version="0.2.0",
            build_number=126,
            source_sha=sha,
            web_dir=web,
            web_source_sha="f" * 40,
        )


@pytest.mark.parametrize(
    "bad_name", ["core/bad:name.py", "core/bad\\\\name.py", "core/new\nline.py"]
)
def test_archive_rejects_nonportable_member_names(source_repo, tmp_path, bad_name):
    from scripts.build_release import validate_archive_name

    with pytest.raises(ReleaseError):
        validate_archive_name(bad_name)
    for name in ("../escape", "/absolute", "a/../b", "a//b", "a/./b", "C:/absolute"):
        with pytest.raises(ReleaseError):
            validate_archive_name(name)


def test_build_bounds_and_immutable_output(source_repo, tmp_path, monkeypatch):
    from scripts import build_release as builder

    sha = git(source_repo, "rev-parse", "HEAD")
    output = tmp_path / "out"
    data = builder.build_release(
        source_repo, output, channel="stable", version="0.2.0", build_number=124, source_sha=sha
    )
    before = (output / data["artifacts"][0]["name"]).read_bytes()
    with pytest.raises(ReleaseError, match="exist"):
        builder.build_release(
            source_repo, output, channel="stable", version="0.2.0", build_number=124, source_sha=sha
        )
    assert (output / data["artifacts"][0]["name"]).read_bytes() == before
    monkeypatch.setattr(builder, "MAX_FILE_BYTES", 4)
    with pytest.raises(ReleaseError, match="bound"):
        builder.build_release(
            source_repo,
            tmp_path / "bounded",
            channel="stable",
            version="0.2.0",
            build_number=125,
            source_sha=sha,
        )


def test_module_cli_builds_and_validates_offline(source_repo, tmp_path):
    import sys

    output = tmp_path / "cli-output"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.build_release",
            "--repo",
            str(source_repo),
            "--output",
            str(output),
            "--channel",
            "nightly",
            "--version",
            "0.2.0",
            "--build-number",
            "126",
            "--source-sha",
            git(source_repo, "rev-parse", "HEAD"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (output / "release-manifest.json").is_file()
    result = subprocess.run(
        [sys.executable, "-m", "scripts.build_release", "--validate", str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "validated" in result.stdout


@pytest.mark.parametrize("attack", ["traversal", "symlink", "missing", "oversized"])
def test_local_validation_rejects_unsafe_or_partial_archives(source_repo, tmp_path, attack):
    import hashlib
    import io

    from scripts.build_release import MAX_FILE_BYTES, build_release, validate_bundle

    output = tmp_path / "out"
    data = build_release(
        source_repo,
        output,
        channel="stable",
        version="0.2.0",
        build_number=124,
        source_sha=git(source_repo, "rev-parse", "HEAD"),
    )
    path = output / data["artifacts"][0]["name"]
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("../outside" if attack == "traversal" else "release-identity.json")
        content = b"{}"
        if attack == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "/outside"
            archive.addfile(member)
        elif attack == "oversized":
            member.size = MAX_FILE_BYTES + 1
            archive.addfile(member)
        else:
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    data["artifacts"][0]["size"] = path.stat().st_size
    data["artifacts"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (output / "release-manifest.json").write_text(json.dumps(data))
    with pytest.raises(ReleaseError):
        validate_bundle(output)
