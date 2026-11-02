"""Issue #261: managed Linux distribution safety and setup contracts."""

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def linux_directory_links_on_windows(monkeypatch):
    # Windows lacks symlink privilege here. Junctions exercise real resolution
    # and rename, but do NOT qualify Linux symlink/flock/sandbox behavior.
    import os

    if os.name == "nt":
        import _winapi

        monkeypatch.setattr(
            Path,
            "symlink_to",
            lambda self, target, target_is_directory=False: _winapi.CreateJunction(
                str(target), str(self)
            ),
        )
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: original(self) or self.is_junction())


def distribution():
    # Keep missing implementation an assertion failure, not a collection error.
    try:
        return importlib.import_module("services.distribution.installation")
    except ModuleNotFoundError:
        pytest.fail("managed Linux installation module is not implemented")


def test_layout_separates_trusted_install_from_authorized_workspace(tmp_path):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    assert layout.install == tmp_path / ".local/share/kinaiya/install"
    assert layout.data == tmp_path / ".local/share/kinaiya/data"
    assert layout.config == tmp_path / ".config/kinaiya/config.json"
    assert layout.workspace == tmp_path / "Kinaiya Workspace"
    layout.validate()
    unsafe = module.Layout(
        layout.install,
        layout.data,
        layout.config,
        layout.install / "worker",
        layout.cache,
        layout.bin,
    )
    with pytest.raises(module.InstallError, match="disjoint"):
        unsafe.validate()
    assert not layout.install.exists()


def archive_release(tmp_path, *, build=1, channel="stable", state=1, members=None):
    import hashlib
    import io
    import tarfile

    archive = tmp_path / f"release-{build}.tar.gz"
    files = members or {
        "kinaiya/pyproject.toml": b"[project]\n",
        "kinaiya/requirements-runtime.lock": b"# locked\n",
        "kinaiya/services/launcher.py": b"# fixture\n",
    }
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
    return archive, {
        "schema_version": 1,
        "channel": channel,
        "version": f"0.2.{build}",
        "build_number": build,
        "source_sha": f"{build:040x}",
        "protocol_min": 1,
        "protocol_max": 1,
        "state_schema": state,
        "min_updater": 1,
        "artifacts": [
            {
                "name": "kinaiya-test-source.tar.gz",
                "platform": "linux",
                "architecture": "any",
                "url": f"https://github.com/rgoza1996/Kinaiya/releases/download/v0.2.{build}/kinaiya-test-source.tar.gz",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "size": archive.stat().st_size,
            }
        ],
    }


def test_verified_install_switches_only_after_staged_validation(tmp_path):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    seen = []

    def prepare(generation, source):
        assert not (layout.install / "current").exists()
        assert (source / "requirements-runtime.lock").is_file()
        seen.append(generation)

    result = module.install_release(layout, release, archive, prepare=prepare, busy=lambda: False)
    assert result["source_sha"] == release["source_sha"]
    assert (layout.install / "current").resolve() == seen[0]
    assert (layout.bin / "kinaiya").is_file()
    assert not (layout.install / "pending.json").exists()
    receipt = json.loads((seen[0] / "receipt.json").read_text())
    assert receipt["release"] == release
    assert "PYTHONPATH" in (seen[0] / "launch").read_text()


def test_installer_refuses_unrelated_install_and_path_launcher(tmp_path):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    layout.install.mkdir(parents=True)
    (layout.install / "unrelated.txt").write_text("keep")
    with pytest.raises(module.InstallError, match="unmanaged"):
        module.install_release(
            layout, release, archive, prepare=lambda *a: None, busy=lambda: False
        )
    assert (layout.install / "unrelated.txt").read_text() == "keep"
    (layout.install / "unrelated.txt").unlink()
    layout.bin.mkdir(parents=True)
    (layout.bin / "kinaiya").write_text("unrelated launcher")
    with pytest.raises(module.InstallError, match="launcher"):
        module.install_release(
            layout, release, archive, prepare=lambda *a: None, busy=lambda: False
        )
    assert (layout.bin / "kinaiya").read_text() == "unrelated launcher"


@pytest.mark.parametrize("case", ["downgrade", "migration", "dirty", "interrupted"])
def test_update_refuses_unsafe_state_before_staging(tmp_path, case):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path, build=2)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    previous = (layout.install / "current").resolve()
    next_archive, target = archive_release(
        tmp_path, build=1 if case == "downgrade" else 3, state=2 if case == "migration" else 1
    )
    if case == "dirty":
        (previous / "source/kinaiya/services/launcher.py").write_text("user changes")
    if case == "interrupted":
        (layout.install / "pending.json").write_text('{"generation":"orphan"}')
    with pytest.raises(
        module.InstallError,
        match={
            "downgrade": "older",
            "migration": "schema",
            "dirty": "modified",
            "interrupted": "interrupted",
        }[case],
    ):
        module.install_release(
            layout,
            target,
            next_archive,
            prepare=lambda *a: pytest.fail("must not stage"),
            busy=lambda: False,
        )
    assert (layout.install / "current").resolve() == previous


def test_managed_operation_lock_serializes_and_releases(tmp_path):
    module = distribution()
    lock = tmp_path / "runtime.lock"
    with module.operation_lock(lock):
        with pytest.raises(module.InstallError, match="busy"):
            with module.operation_lock(lock):
                pytest.fail("lock must exclude the second updater")
    with module.operation_lock(lock):
        pass


def test_failed_stage_keeps_old_code_and_user_data(tmp_path):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    previous = (layout.install / "current").resolve()
    layout.data.mkdir(parents=True)
    (layout.data / "journal.sqlite").write_bytes(b"owner data")
    archive, target = archive_release(tmp_path, build=2)

    def fail(*args):
        raise module.InstallError("health check failed")

    with pytest.raises(module.InstallError, match="health"):
        module.install_release(layout, target, archive, prepare=fail, busy=lambda: False)
    assert (layout.install / "current").resolve() == previous
    assert (layout.data / "journal.sqlite").read_bytes() == b"owner data"
    assert not (layout.install / "pending.json").exists()
    assert len(list((layout.install / "generations").iterdir())) == 1


def test_setup_reuses_init_with_agent_only_defaults_and_preserves_existing(tmp_path, monkeypatch):
    from services import launcher

    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    # Linux permission enforcement is tested on Linux; Windows cannot represent 0600.
    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    result = module.setup(layout, endpoint="http://127.0.0.1:8080/v1", model="local-model")
    config = json.loads(layout.config.read_text())
    assert "voice" not in config and "resources" not in config
    assert config["providers"]["agent"]["model"] == "local-model"
    assert config["providers"]["agent"]["allow_cloud"] is False
    assert config["policy"] == {
        "allow_write": False,
        "allow_shell": False,
        "network": False,
        "yolo": False,
    }
    assert result["provider_connection"] == "not_checked"
    assert result["ready"] is False
    before = layout.config.read_bytes()
    with pytest.raises(module.InstallError, match="existing"):
        module.setup(layout, endpoint="http://127.0.0.1:8080/v1", model="another")
    assert layout.config.read_bytes() == before


@pytest.mark.parametrize(
    "endpoint,model,key",
    [
        ("https://provider.example/v1", "model", None),
        ("http://user:secret@127.0.0.1:8080/v1", "model", None),
        ("http://127.0.0.1:8080/v1?key=secret", "model", None),
        ("http://127.0.0.1:8080/v1", "", None),
        ("http://127.0.0.1:8080/v1", "model", "raw-api-secret"),
    ],
)
def test_invalid_setup_is_rejected_without_writes(tmp_path, endpoint, model, key):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    with pytest.raises(module.InstallError):
        module.setup(layout, endpoint=endpoint, model=model, credential_env=key)
    assert not layout.config.exists() and not layout.data.exists()


def test_cloud_setup_requires_explicit_disclosure_and_uses_env_reference(tmp_path, monkeypatch):
    from services import launcher

    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setenv("TEST_PROVIDER_KEY", "synthetic-do-not-persist")
    result = module.setup(
        layout,
        endpoint="https://provider.example/v1",
        model="explicit-model",
        allow_cloud=True,
        credential_env="TEST_PROVIDER_KEY",
        converse=True,
    )
    config = json.loads(layout.config.read_text())
    assert config["providers"]["agent"]["api_key_env"] == "TEST_PROVIDER_KEY"
    assert config["providers"]["agent"]["allow_cloud"] is True
    assert config["distribution"]["converse"] is True
    assert "voice" not in config
    assert "synthetic-do-not-persist" not in layout.config.read_text()
    assert result["ready"] is False


@pytest.mark.parametrize(
    "case,message",
    [
        ("root", "root"),
        ("distro", "Ubuntu"),
        ("sandbox", "bubblewrap"),
        ("disk", "disk"),
        ("namespace", "namespace"),
    ],
)
def test_linux_preflight_refuses_unsafe_hosts_before_changes(tmp_path, monkeypatch, case, message):
    import ctypes.util
    import os
    import platform
    import shutil
    import subprocess
    from types import SimpleNamespace

    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "20.04" if case == "distro" else "24.04"},
    )
    monkeypatch.setattr(os, "geteuid", lambda: 0 if case == "root" else 1000, raising=False)
    monkeypatch.setattr(
        shutil, "which", lambda name: None if case == "sandbox" else "/usr/bin/bwrap"
    )
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libseccomp.so.2")
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 if case == "disk" else 2 * 1024**3),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1 if case == "namespace" else 0),
    )
    with pytest.raises(module.InstallError, match=message):
        module.preflight(layout)
    assert not layout.install.exists()


def test_tty_wizard_requires_confirmation_and_keeps_agent_only(tmp_path, monkeypatch):
    try:
        wizard = importlib.import_module("services.distribution.wizard")
    except ModuleNotFoundError:
        pytest.fail("interactive setup wizard is not implemented")
    module = distribution()
    layout = module.Layout.for_home(tmp_path)

    class Terminal:
        def __init__(self):
            self.answers = iter(["", "", "", "local-model", "", "", "", "yes"])
            self.output = ""

        def write(self, value):
            self.output += value

        def flush(self):
            pass

        def readline(self):
            return next(self.answers) + "\n"

    tty = Terminal()
    calls = []
    monkeypatch.setattr(
        wizard, "setup", lambda chosen, **kw: calls.append((chosen, kw)) or {"ready": False}
    )
    wizard.run_wizard(layout, tty=tty)
    assert calls[0][1]["converse"] is False
    assert calls[0][1]["allow_cloud"] is False
    assert calls[0][1]["model"] == "local-model"
    assert "No microphone" in tty.output and "No models" in tty.output
    assert not layout.config.exists()


def test_mascot_falls_back_for_narrow_and_noninteractive_output():
    try:
        wizard = importlib.import_module("services.distribution.wizard")
    except ModuleNotFoundError:
        pytest.fail("terminal mascot is not implemented")
    full = wizard.mascot(width=80, interactive=True)
    assert "(" in full and "~" in full and "*" in full
    assert "\x1b" not in full
    assert wizard.mascot(width=80, interactive=False) == "Kinaiya"
    assert max(map(len, wizard.mascot(width=12, interactive=True).splitlines())) <= 12


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha", "main"),
        ("state_schema", True),
        ("build_number", -1),
        ("artifacts", []),
        ("channel", "unknown"),
    ],
)
def test_invalid_release_metadata_refused_before_writes(tmp_path, field, value):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    release[field] = value
    with pytest.raises(module.InstallError, match="metadata"):
        module.install_release(
            layout, release, archive, prepare=lambda *a: None, busy=lambda: False
        )
    assert not layout.install.exists()


def test_layout_refuses_launcher_inside_agent_workspace(tmp_path):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    from dataclasses import replace

    with pytest.raises(module.InstallError, match="disjoint"):
        replace(layout, bin=layout.workspace / "bin").validate()


def test_managed_setup_refuses_path_change_instead_of_stale_launcher(tmp_path, monkeypatch):
    from dataclasses import replace

    from services import launcher

    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    monkeypatch.setattr(
        launcher, "load_or_create_tokens", lambda p: p.mkdir(parents=True, exist_ok=True)
    )
    with pytest.raises(module.InstallError, match="layout"):
        module.setup(
            replace(layout, data=tmp_path / "new-data"),
            endpoint="http://127.0.0.1:8080/v1",
            model="chosen",
        )
    assert not layout.config.exists()


def test_same_build_cannot_replace_immutable_manifest(tmp_path):
    import copy

    module = distribution()
    _, release = archive_release(tmp_path)
    target = copy.deepcopy(release)
    target["artifacts"][0]["sha256"] = "f" * 64
    with pytest.raises(module.InstallError, match="immutable"):
        module.update_plan({"release": release}, target)


@pytest.mark.skipif(
    __import__("sys").platform != "linux", reason="real POSIX protected credential permissions"
)
def test_real_linux_setup_private_tokens_and_agent_only_config(tmp_path):
    import stat

    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    result = module.setup(layout, endpoint="http://127.0.0.1:8080/v1", model="explicit-model")
    assert result["ready"] is False
    assert stat.S_IMODE(layout.config.stat().st_mode) == 0o600
    assert stat.S_IMODE((layout.data / "service-tokens.json").stat().st_mode) == 0o600
    config = json.loads(layout.config.read_text())
    assert "voice" not in config and "resources" not in config
