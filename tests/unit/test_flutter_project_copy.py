"""Real Git/filesystem oracles for SDK fixture input ownership (#637)."""

import subprocess
from pathlib import Path

import pytest
from flutter_project_helpers import isolated_flutter_project


@pytest.fixture
def declared_project(tmp_path):
    root = tmp_path / "source"
    app = root / "apps/kinaiya_flutter"
    (app / "test").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
    (root / ".gitignore").write_text(".dart_tool/\nbuild/\n")
    (app / "pubspec.yaml").write_text("name: public_fixture\n")
    (app / "pubspec.lock").write_text("packages: {}\n")
    (app / "analysis_options.yaml").write_text("analyzer:\n  exclude: [build/**]\n")
    (app / "test/public_test.dart").write_text("// Public synthetic fixture input.\n")
    subprocess.run(
        [
            "git",
            "add",
            ".gitignore",
            "apps/kinaiya_flutter/pubspec.yaml",
            "apps/kinaiya_flutter/pubspec.lock",
            "apps/kinaiya_flutter/analysis_options.yaml",
            "apps/kinaiya_flutter/test/public_test.dart",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (app / ".dart_tool").mkdir()
    (app / ".dart_tool/package_config.json").write_text("PRIVATE-CACHE-MARKER")
    (app / "untracked-owner-note.txt").write_text("PRIVATE-UNDECLARED-MARKER")
    return root, app


def test_copy_preserves_current_declared_bytes_and_isolates_sdk_mutations(
    declared_project, tmp_path
):
    root, source = declared_project
    # A tracked working edit is an input; do not substitute committed bytes.
    (source / "test/public_test.dart").write_text("// Public current edit.\n")
    before = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }
    target = isolated_flutter_project(root, tmp_path / "sdk-project")
    assert target == tmp_path / "sdk-project"
    assert {path.relative_to(target) for path in target.rglob("*") if path.is_file()} == {
        Path("pubspec.yaml"),
        Path("pubspec.lock"),
        Path("analysis_options.yaml"),
        Path("test/public_test.dart"),
    }
    for relative in (Path("pubspec.yaml"), Path("pubspec.lock"), Path("test/public_test.dart")):
        assert (target / relative).read_bytes() == before[relative]
    (target / "analysis_options.yaml").write_text("analyzer:\n  exclude: [android/**, ios/**]\n")
    (target / ".dart_tool").mkdir()
    (target / ".dart_tool/package_config.json").write_text("new isolated SDK configuration")
    assert {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    } == before


def test_copy_refuses_destination_inside_original_checkout(declared_project):
    root, source = declared_project
    destination = source / "sdk-project"
    with pytest.raises(ValueError):
        isolated_flutter_project(root, destination)
    assert not destination.exists()


def test_copy_refuses_declared_symlink_to_private_input(declared_project, tmp_path):
    root, source = declared_project
    private = tmp_path / "private-marker"
    private.write_text("PUBLIC-SYNTHETIC-UNDECLARED-INPUT")
    (source / "test/escape.dart").symlink_to(private)
    subprocess.run(
        ["git", "add", "apps/kinaiya_flutter/test/escape.dart"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    target = tmp_path / "sdk-project"
    with pytest.raises(ValueError) as error:
        isolated_flutter_project(root, target)
    assert private.read_text() not in str(error.value)
    assert not target.exists()


def test_copy_requires_declared_manifest(declared_project, tmp_path):
    root, _ = declared_project
    subprocess.run(
        ["git", "rm", "--cached", "apps/kinaiya_flutter/pubspec.yaml"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    target = tmp_path / "sdk-project"
    with pytest.raises(ValueError, match="manifest"):
        isolated_flutter_project(root, target)
    assert not target.exists()
