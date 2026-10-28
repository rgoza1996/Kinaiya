"""Tests for derivative runtime wheel builder (synthetic fixtures only)."""

from __future__ import annotations

import base64
import difflib
import errno
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


def load_repository_script(name):
    path = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tested_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


builder = load_repository_script("build_vibevoice_runtime")


SYNTHETIC_FILES = {
    "vibevoice/__init__.py": b"VERSION = 'synthetic'\n",
    "vibevoice/modular/__init__.py": b"",
    "vibevoice/modular/model.py": b"class Model:\n    pass\n",
}
LICENSE_BYTES = b"MIT synthetic license\n"
REVISION = "synthetic-revision-001"


def _synthetic_manifest(files, license_bytes):
    runtime_files = {}
    for name, data in files.items():
        digest = hashlib.sha256(data).hexdigest()
        runtime_files[name] = {"size_bytes": len(data), "sha256": digest}
    license_digest = hashlib.sha256(license_bytes).hexdigest()
    return {
        "runtime_revision": REVISION,
        "runtime_files": runtime_files,
        "files": {
            "licenses/runtime-MIT.txt": {
                "size_bytes": len(license_bytes),
                "sha256": license_digest,
            }
        },
    }


def _write_fixtures(root: Path, files, license_bytes):
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    license_path = root.parent / "LICENSE-synthetic"
    license_path.write_bytes(license_bytes)
    return license_path


def _build(tmp_path, monkeypatch, files=None, license_bytes=None):
    use_files = dict(SYNTHETIC_FILES) if files is None else files
    use_license = LICENSE_BYTES if license_bytes is None else license_bytes
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    license_path = _write_fixtures(src, use_files, use_license)
    manifest = _synthetic_manifest(use_files, use_license)
    monkeypatch.setattr(builder, "_load_checked_manifest", lambda: manifest)
    result = builder.build_runtime_wheel(src, license_path, out)
    return src, license_path, out, result


def test_deterministic_byte_equal_builds(tmp_path, monkeypatch):
    _, _, out_a, result_a = _build(tmp_path / "a", monkeypatch)
    _, _, out_b, result_b = _build(tmp_path / "b", monkeypatch)
    wheel_a = (out_a / builder.WHEEL_FILENAME).read_bytes()
    wheel_b = (out_b / builder.WHEEL_FILENAME).read_bytes()
    assert wheel_a == wheel_b
    digest_a = result_a["attestation"]["wheel_sha256"]
    digest_b = result_b["attestation"]["wheel_sha256"]
    assert digest_a == digest_b
    assert result_a["attestation"]["sources"] == result_b["attestation"]["sources"]


def test_wheel_preserves_bytes_metadata_and_record(tmp_path, monkeypatch):
    _, _, out, result = _build(tmp_path, monkeypatch)
    wheel_path = out / builder.WHEEL_FILENAME
    with zipfile.ZipFile(wheel_path) as handle:
        names = handle.namelist()
        assert names == sorted(names)
        for name, data in SYNTHETIC_FILES.items():
            assert handle.read(name) == data
        assert handle.read(builder.LICENSE_ARCNAME) == LICENSE_BYTES
        metadata = handle.read(f"{builder.DIST_INFO}/METADATA").decode()
        assert "kinaiya-vibevoice-runtime" in metadata
        assert "1.0.0+kinaiya.2" in metadata
        assert "microsoft/VibeVoice" in metadata
        assert REVISION in metadata
        assert "MIT" in metadata
        assert "explicit checked compatibility patch" in metadata
        assert "Requires-Python: >=3.11" in metadata
        for dep in builder.REQUIRES_DIST:
            assert f"Requires-Dist: {dep}" in metadata
        marker = handle.read(f"{builder.DIST_INFO}/WHEEL").decode()
        assert "Tag: py3-none-any" in marker
        record = handle.read(f"{builder.DIST_INFO}/RECORD").decode().splitlines()
        by_name = {}
        for line in record:
            parts = line.split(",")
            by_name[parts[0]] = parts
        record_key = f"{builder.DIST_INFO}/RECORD"
        assert by_name[record_key] == [record_key, "", ""]
        for name in names:
            if name.endswith("/RECORD"):
                continue
            algo, size = by_name[name][1], by_name[name][2]
            assert algo.startswith("sha256=")
            assert int(size) == len(handle.read(name))
            expected = (
                base64.urlsafe_b64encode(hashlib.sha256(handle.read(name)).digest())
                .decode()
                .rstrip("=")
            )
            assert algo == f"sha256={expected}"
        dates = {info.date_time for info in handle.infolist()}
        assert dates == {builder.ZIP_DATE_TIME}
    provenance = json.loads((out / builder.PROVENANCE_FILENAME).read_text())
    assert provenance["source_revision"] == REVISION
    wheel_digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    assert provenance["wheel_sha256"] == wheel_digest
    assert provenance["license_sha256"] == hashlib.sha256(LICENSE_BYTES).hexdigest()
    assert result["attestation"]["wheel_sha256"] == wheel_digest


def test_rejects_tampered_source_without_output(tmp_path, monkeypatch):
    src, _, _, _ = _build(tmp_path, monkeypatch)
    (src / "vibevoice/modular/model.py").write_bytes(b"tampered\n")
    manifest = _synthetic_manifest(SYNTHETIC_FILES, LICENSE_BYTES)
    monkeypatch.setattr(builder, "_load_checked_manifest", lambda: manifest)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    lic = src.parent / "LICENSE-synthetic"
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(src, lic, fresh)
    assert list(fresh.iterdir()) == []


def test_rejects_symlink_file_dir_and_license(tmp_path, monkeypatch):
    manifest = _synthetic_manifest(SYNTHETIC_FILES, LICENSE_BYTES)
    monkeypatch.setattr(builder, "_load_checked_manifest", lambda: manifest)
    src = tmp_path / "src"
    src.mkdir()
    _write_fixtures(src, SYNTHETIC_FILES, LICENSE_BYTES)
    lic = src.parent / "LICENSE-synthetic"
    target = src / "vibevoice/__init__.py"
    target.unlink()
    (tmp_path / "evil.py").write_bytes(b"evil\n")
    target.symlink_to(tmp_path / "evil.py")
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(src, lic, tmp_path / "out1")
    assert not (tmp_path / "out1" / builder.WHEEL_FILENAME).exists()
    src2 = tmp_path / "src2"
    src2.mkdir()
    _write_fixtures(src2, SYNTHETIC_FILES, LICENSE_BYTES)
    lic2 = tmp_path / "LICENSE-synthetic"
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    (real_dir / "model.py").write_bytes(b"evil\n")
    victim = src2 / "vibevoice/modular"
    for child in list(victim.iterdir()):
        child.unlink()
    victim.rmdir()
    victim.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(src2, lic2, tmp_path / "out2")
    assert not (tmp_path / "out2" / builder.WHEEL_FILENAME).exists()
    src3 = tmp_path / "src3"
    src3.mkdir()
    _write_fixtures(src3, SYNTHETIC_FILES, LICENSE_BYTES)
    lic3 = src3.parent / "LICENSE-synthetic"
    lic3.unlink()
    lic3.symlink_to(tmp_path / "evil.py")
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(src3, lic3, tmp_path / "out3")
    assert not (tmp_path / "out3" / builder.WHEEL_FILENAME).exists()


def test_refuses_existing_output_and_missing_source(tmp_path, monkeypatch):
    _, _, out, _ = _build(tmp_path, monkeypatch)
    before = (out / builder.WHEEL_FILENAME).read_bytes()
    src = tmp_path / "src"
    lic = tmp_path / "LICENSE-synthetic"
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(src, lic, out)
    assert (out / builder.WHEEL_FILENAME).read_bytes() == before
    (src / "vibevoice/__init__.py").unlink()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(src, lic, fresh)
    assert list(fresh.iterdir()) == []


def test_cli_writes_provenance_and_compact_result(tmp_path, monkeypatch, capsys):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    license_path = _write_fixtures(src, SYNTHETIC_FILES, LICENSE_BYTES)
    manifest = _synthetic_manifest(SYNTHETIC_FILES, LICENSE_BYTES)
    monkeypatch.setattr(builder, "_load_checked_manifest", lambda: manifest)
    args = ["--source-dir", str(src), "--license-file", str(license_path)]
    args += ["--output-dir", str(out)]
    assert builder.main(args) == 0
    printed = capsys.readouterr().out.strip()
    public = json.loads(printed)
    assert public["wheel"] == builder.WHEEL_FILENAME
    assert public["revision"] == REVISION
    assert str(tmp_path) not in printed
    assert (out / builder.PROVENANCE_FILENAME).exists()


def test_source_is_packaged_without_execution(tmp_path, monkeypatch):
    files = {
        **SYNTHETIC_FILES,
        "vibevoice/__init__.py": b"raise AssertionError('source executed')\n",
    }
    _, _, _, result = _build(tmp_path, monkeypatch, files=files)
    with zipfile.ZipFile(result["wheel_path"]) as archive:
        assert archive.read("vibevoice/__init__.py") == files["vibevoice/__init__.py"]


def test_partial_write_failure_removes_only_created_outputs(tmp_path, monkeypatch):
    src, lic, _, _ = _build(tmp_path, monkeypatch)
    out = tmp_path / "partial"
    write = builder.os.write
    calls = 0

    def failing_write(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "synthetic write failure")
        return write(descriptor, data[:17])

    monkeypatch.setattr(builder.os, "write", failing_write)
    with pytest.raises(OSError):
        builder.build_runtime_wheel(src, lic, out)
    assert list(out.iterdir()) == []


def test_existing_provenance_is_preserved_and_new_wheel_removed(tmp_path, monkeypatch):
    src, lic, _, _ = _build(tmp_path, monkeypatch)
    out = tmp_path / "previous"
    out.mkdir()
    previous = out / builder.PROVENANCE_FILENAME
    previous.write_bytes(b"owner previous provenance")
    with pytest.raises(builder.BuildError, match="output exists"):
        builder.build_runtime_wheel(src, lic, out)
    assert list(out.iterdir()) == [previous]
    assert previous.read_bytes() == b"owner previous provenance"


def test_parent_symlinks_are_refused(tmp_path, monkeypatch):
    src, lic, _, _ = _build(tmp_path, monkeypatch)
    link = tmp_path / "alias"
    link.symlink_to(src.parent, target_is_directory=True)
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(link / "src", lic, tmp_path / "fresh")
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(src, lic, link / "fresh")
    assert not (src.parent / "fresh").exists()


def test_nonregular_and_oversize_sources_are_refused_before_output(tmp_path, monkeypatch):
    src, lic, _, _ = _build(tmp_path, monkeypatch)
    target = src / "vibevoice/__init__.py"
    target.unlink()
    builder.os.mkfifo(target)
    with pytest.raises(builder.BuildError, match="nonregular"):
        builder.build_runtime_wheel(src, lic, tmp_path / "fifo-output")
    target.unlink()
    target.write_bytes(b"x" * 10000)
    with pytest.raises(builder.BuildError, match="size mismatch"):
        builder.build_runtime_wheel(src, lic, tmp_path / "oversize-output")
    assert not (tmp_path / "fifo-output").exists()
    assert not (tmp_path / "oversize-output").exists()


def test_checked_manifest_refuses_a_different_revision_or_incomplete_runtime(tmp_path, monkeypatch):
    manifest = builder._load_checked_manifest()
    path = tmp_path / builder.MANIFEST_RELATIVE
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(builder, "_repository_root", lambda: tmp_path)
    wrong = {**manifest, "runtime_revision": "0" * 40}
    path.write_text(json.dumps(wrong))
    with pytest.raises(builder.BuildError, match="checked manifest"):
        builder._load_checked_manifest()
    manifest["runtime_files"].pop(next(iter(manifest["runtime_files"])))
    path.write_text(json.dumps(manifest))
    with pytest.raises(builder.BuildError, match="checked manifest"):
        builder._load_checked_manifest()


def test_exact_patch_handles_multiple_hunks_files_and_zero_length_ranges():
    original = {
        "vibevoice/a.py": b"one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\n",
        "vibevoice/b.py": b"delete\nretained\n",
    }
    expected = {
        "vibevoice/a.py": b"added\none\nTWO\nthree\nfour\nfive\nsix\nSEVEN\neight\n",
        "vibevoice/b.py": b"retained\n",
    }
    patch = "".join(
        line
        for name in original
        for line in difflib.unified_diff(
            original[name].decode().splitlines(keepends=True),
            expected[name].decode().splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            n=0,
        )
    ).encode()
    assert builder._apply_unified_patch(original, patch) == expected
    assert original["vibevoice/a.py"].startswith(b"one\n")


@pytest.mark.parametrize(
    "patch",
    [
        b"",
        b"\xff\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n",
        b"--- a/../escape.py\n+++ b/../escape.py\n@@ -1 +1 @@\n-old\n+new\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/b.py\n@@ -1 +1 @@\n-old\n+new\n",
        b"--- a/vibevoice/missing.py\n+++ b/vibevoice/missing.py\n@@ -1 +1 @@\n-old\n+new\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -1 +1 @@\n-wrong\n+new\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -1,2 +1 @@\n-old\n+new\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -1 +2 @@\n-old\n+new\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -99 +99 @@\n-old\n+new\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -1 +1 @@\n-old\n+new",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -1 +1 @@\n-old\n!new\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -1 +1 @@\n-old\n+again\n",
        b"--- a/vibevoice/a.py\n+++ b/vibevoice/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        b"@@ -1 +1 @@\n-old\n+again\n",
    ],
)
def test_exact_patch_refuses_malformed_unmatched_fuzzy_and_unsafe_changes(patch):
    with pytest.raises(builder.BuildError):
        builder._apply_unified_patch({"vibevoice/a.py": b"old\n"}, patch)


def _patched_fixture(tmp_path, monkeypatch):
    src = tmp_path / "source"
    src.mkdir()
    license_path = _write_fixtures(src, SYNTHETIC_FILES, LICENSE_BYTES)
    manifest = _synthetic_manifest(SYNTHETIC_FILES, LICENSE_BYTES)
    manifest["upstream_runtime_files"] = dict(manifest["runtime_files"])
    patched = b"class DerivedModel:\n    pass\n"
    name = "vibevoice/modular/model.py"
    manifest["runtime_files"][name] = {
        "size_bytes": len(patched),
        "sha256": hashlib.sha256(patched).hexdigest(),
    }
    patch = "".join(
        difflib.unified_diff(
            SYNTHETIC_FILES[name].decode().splitlines(keepends=True),
            patched.decode().splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        )
    ).encode()
    patch_path = tmp_path / "packages/providers/vibevoice_transformers5.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_bytes(patch)
    manifest["runtime_patch"] = {
        "path": "packages/providers/vibevoice_transformers5.patch",
        "size_bytes": len(patch),
        "sha256": hashlib.sha256(patch).hexdigest(),
    }
    monkeypatch.setattr(builder, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(builder, "_load_checked_manifest", lambda: manifest)
    return src, license_path, patch_path, manifest, patched


def test_derived_wheel_has_exact_upstream_patch_and_final_source_provenance(tmp_path, monkeypatch):
    src, license_path, patch_path, manifest, patched = _patched_fixture(tmp_path, monkeypatch)
    result = builder.build_runtime_wheel(src, license_path, tmp_path / "out")
    proof = result["attestation"]
    name = "vibevoice/modular/model.py"
    assert proof["source_patch_sha256"] == hashlib.sha256(patch_path.read_bytes()).hexdigest()
    assert proof["upstream_sources"][name] == manifest["upstream_runtime_files"][name]["sha256"]
    assert proof["sources"][name] == manifest["runtime_files"][name]["sha256"]
    assert proof["sources"][name] != proof["upstream_sources"][name]
    with zipfile.ZipFile(result["wheel_path"]) as wheel:
        assert wheel.read(name) == patched
    assert (src / name).read_bytes() == SYNTHETIC_FILES[name]


@pytest.mark.parametrize("kind", ["input", "patch", "output", "symlink", "path", "file_set"])
def test_derived_build_refuses_tampering_before_creating_outputs(tmp_path, monkeypatch, kind):
    src, license_path, patch_path, manifest, _ = _patched_fixture(tmp_path, monkeypatch)
    name = "vibevoice/modular/model.py"
    if kind == "input":
        (src / name).write_bytes(b"class UntestedModel:\n    pass\n")
    elif kind == "patch":
        patch_path.write_bytes(patch_path.read_bytes().replace(b"DerivedModel", b"ChangedModel"))
    elif kind == "output":
        manifest["runtime_files"][name]["sha256"] = "0" * 64
    elif kind == "symlink":
        target = tmp_path / "elsewhere.patch"
        patch_path.rename(target)
        patch_path.symlink_to(target)
    elif kind == "path":
        manifest["runtime_patch"]["path"] = "elsewhere.patch"
    else:
        del manifest["upstream_runtime_files"][name]
    output = tmp_path / "out"
    with pytest.raises(builder.BuildError):
        builder.build_runtime_wheel(src, license_path, output)
    assert not output.exists()


def test_checked_transformers5_manifest_keeps_all_upstream_hashes_and_explicit_changes():
    manifest = builder._load_checked_manifest()
    assert manifest["runtime_versions"] == {
        "kinaiya-vibevoice-runtime": builder.VERSION,
        "transformers": "5.21.0",
        "torch": "2.14.1+cpu",
    }
    upstream = manifest["upstream_runtime_files"]
    derived = manifest["runtime_files"]
    assert len(upstream) == len(derived) == 22
    assert set(upstream) == set(derived)
    assert {name for name in upstream if upstream[name]["sha256"] != derived[name]["sha256"]} == {
        "vibevoice/modular/modeling_vibevoice_streaming.py",
        "vibevoice/modular/modeling_vibevoice_streaming_inference.py",
        "vibevoice/modular/modular_vibevoice_text_tokenizer.py",
        "vibevoice/modular/modular_vibevoice_tokenizer.py",
    }
    patch = builder._repository_root() / manifest["runtime_patch"]["path"]
    builder._verify_bytes(patch.read_bytes(), manifest["runtime_patch"])
    assert manifest["runtime_packaging"]["upstream_python_files_unchanged"] is False
