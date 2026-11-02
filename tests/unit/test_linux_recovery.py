"""Installer recovery and untrusted artifact regression tests (#261)."""

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from test_linux_distribution import archive_release, distribution, linux_directory_links_on_windows  # noqa: F401


@pytest.mark.parametrize("case", ["traversal", "absolute", "symlink", "hardlink", "duplicate"])
def test_unsafe_archives_never_escape_staging(tmp_path, case):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    with tarfile.open(archive, "w:gz") as tar:
        item = tarfile.TarInfo(
            {"traversal": "../../escape", "absolute": "/escape"}.get(case, "kinaiya/file")
        )
        if case in {"symlink", "hardlink"}:
            item.type = tarfile.SYMTYPE if case == "symlink" else tarfile.LNKTYPE
            item.linkname = "../../escape"
        else:
            item.size = 1
        tar.addfile(item, io.BytesIO(b"x"))
        if case == "duplicate":
            tar.addfile(item, io.BytesIO(b"y"))
    release["artifacts"][0].update(
        size=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    with pytest.raises(module.InstallError, match="[Uu]nsafe|[Dd]uplicate"):
        module.install_release(
            layout,
            release,
            archive,
            prepare=lambda *a: pytest.fail("must not prepare"),
            busy=lambda: False,
        )
    assert not (tmp_path / "escape").exists()
    assert not (layout.install / "current").exists()


def test_failed_cleanup_after_switch_never_deletes_active_runtime(tmp_path, monkeypatch):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    original = Path.unlink

    def fail_pending(self, *args, **kwargs):
        if self.name == "pending.json":
            raise PermissionError("synthetic pending cleanup failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_pending)
    with pytest.raises(module.InstallError, match="recovery"):
        module.install_release(
            layout, release, archive, prepare=lambda *a: None, busy=lambda: False
        )
    assert (layout.install / "current/receipt.json").is_file()
    assert (layout.install / "pending.json").is_file()


def test_recover_discards_only_abandoned_stage_and_retains_data(tmp_path):
    import json

    module = distribution()
    assert hasattr(module, "recover"), "interrupted transaction recovery missing"
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    current = (layout.install / "current").resolve()
    abandoned = layout.install / "generations" / ("a" * 32)
    abandoned.mkdir()
    (abandoned / "partial").write_text("incomplete")
    (layout.install / "pending.json").write_text(
        json.dumps({"generation": abandoned.name, "phase": "staging"})
    )
    layout.data.mkdir()
    (layout.data / "journal.sqlite").write_bytes(b"private owner state")
    assert module.recover(layout, busy=lambda: False)["action"] == "discarded_stage"
    assert not abandoned.exists()
    assert (layout.install / "current").resolve() == current
    assert (layout.data / "journal.sqlite").read_bytes() == b"private owner state"


@pytest.mark.skipif(
    __import__("sys").platform != "linux",
    reason="requires Linux atomic symlink replacement; Windows junctions are not equivalent",
)
def test_compatible_rollback_restores_code_only_and_preserves_database(tmp_path):
    module = distribution()
    assert hasattr(module, "rollback"), "schema-compatible code rollback missing"
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    previous = (layout.install / "current").resolve()
    archive, release = archive_release(tmp_path, build=2)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    layout.data.mkdir()
    (layout.data / "journal.sqlite").write_bytes(b"latest state, never rolled back")
    result = module.rollback(layout, previous.name, busy=lambda: False)
    assert result["database_restored"] is False
    assert (layout.install / "current").resolve() == previous
    assert (layout.data / "journal.sqlite").read_bytes() == b"latest state, never rolled back"


def test_uninstall_retains_workspace_data_and_config_unless_separately_confirmed(tmp_path):
    module = distribution()
    assert hasattr(module, "uninstall"), "managed uninstall missing"
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    for path in (layout.data, layout.workspace, layout.config.parent):
        path.mkdir(parents=True, exist_ok=True)
    layout.config.write_text("owner configuration")
    (layout.data / "journal.sqlite").write_text("keep data")
    (layout.workspace / "owner-file").write_text("keep work")
    with pytest.raises(module.InstallError, match="DELETE"):
        module.uninstall(layout, delete_data=True, busy=lambda: False)
    assert (layout.install / "current").exists()
    result = module.uninstall(layout, busy=lambda: False)
    assert result["data_deleted"] is False
    assert not (layout.install / "current").exists()
    assert not (layout.bin / "kinaiya").exists()
    assert layout.config.read_text() == "owner configuration"
    assert (layout.data / "journal.sqlite").read_text() == "keep data"
    assert (layout.workspace / "owner-file").read_text() == "keep work"


def test_generation_directory_alias_cannot_write_outside_install(tmp_path):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    outside = tmp_path / "outside"
    outside.mkdir()
    generations = layout.install / "generations"
    generations.rename(layout.install / "saved-generations")
    generations.symlink_to(outside, target_is_directory=True)
    archive, release = archive_release(tmp_path, build=2)
    with pytest.raises(module.InstallError, match="alias|unmanaged"):
        module.install_release(
            layout, release, archive, prepare=lambda *a: None, busy=lambda: False
        )
    assert list(outside.iterdir()) == []
