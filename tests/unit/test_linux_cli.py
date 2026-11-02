"""Public installer/updater commands, including the native launcher parser."""

import importlib
from types import SimpleNamespace

import pytest
from test_linux_distribution import archive_release, distribution, linux_directory_links_on_windows  # noqa: F401


def cli_module():
    try:
        return importlib.import_module("services.distribution.cli")
    except ModuleNotFoundError:
        pytest.fail("distribution CLI is missing")


def test_native_parser_supports_managed_commands():
    from services import launcher

    assert hasattr(launcher, "build_parser"), "testable native command parser missing"
    parser = launcher.build_parser()
    args = parser.parse_args(["update", "--check", "--channel", "nightly"])
    assert args.check is True and args.channel == "nightly"
    assert parser.parse_args(["serve", "host"]).service == "host"
    assert parser.parse_args(
        ["setup", "--yes", "--endpoint", "http://127.0.0.1:8080/v1", "--model", "chosen"]
    ).yes
    assert parser.parse_args(["uninstall", "--yes"]).delete_data is False


def test_check_only_does_not_create_install_or_download(tmp_path, monkeypatch):
    cli = cli_module()
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    _, release = archive_release(tmp_path)
    monkeypatch.setattr(cli, "resolve_release", lambda channel: release)
    monkeypatch.setattr(cli, "download_artifact", lambda *a: pytest.fail("check must not download"))
    result = cli.check_update(layout, "stable")
    assert result["action"] == "install" and result["source_sha"] == release["source_sha"]
    assert not layout.install.exists() and not layout.cache.exists()


def test_feed_errors_are_not_reported_current(tmp_path, monkeypatch):
    cli = cli_module()
    from packages.distribution.releases import ReleaseError

    def absent(channel):
        raise ReleaseError("No approved stable release is published", "release_absent")

    monkeypatch.setattr(cli, "resolve_release", absent)
    with pytest.raises(ReleaseError, match="stable"):
        cli.check_update(distribution().Layout.for_home(tmp_path), "stable")


def test_bounded_download_removes_partial_and_rejects_digest(tmp_path, monkeypatch):
    cli = cli_module()
    import io

    from packages.distribution.releases import ReleaseError

    artifact = {
        "size": 3,
        "sha256": "0" * 64,
        "url": "https://github.com/rgoza1996/Kinaiya/releases/download/v0.2.1/fixture.tar.gz",
    }
    monkeypatch.setattr(cli, "open_release_url", lambda url: io.BytesIO(b"oversized"))
    target = tmp_path / "artifact"
    with pytest.raises((distribution().InstallError, ReleaseError), match="size|bound"):
        cli.download_artifact(artifact, target)
    assert not target.exists()
    monkeypatch.setattr(cli, "open_release_url", lambda url: io.BytesIO(b"abc"))
    with pytest.raises((distribution().InstallError, ReleaseError), match="digest"):
        cli.download_artifact(artifact, target)
    assert not target.exists()


def test_host_services_default_agent_only(monkeypatch, tmp_path):
    from services import launcher

    assert hasattr(launcher, "host_service_names"), "managed host selection missing"
    assert launcher.host_service_names({}) == ["session", "agent"]
    assert launcher.host_service_names({"distribution": {"converse": True}}) == [
        "session",
        "agent",
        "converse",
        "decisions",
    ]


def test_managed_guard_roots_cover_whole_install_and_release_trust(tmp_path, monkeypatch):
    from services import launcher

    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    assert hasattr(launcher, "managed_guard_roots"), "managed trust roots missing"
    monkeypatch.setenv("KINAIYA_INSTALL_ROOT", str(layout.install))
    roots = launcher.managed_guard_roots()
    assert set(roots) >= {
        layout.install,
        layout.data,
        layout.config.parent,
        layout.cache,
        layout.bin,
    }


def test_preflight_failure_prevents_resolution_download_and_publication(tmp_path, monkeypatch):
    cli = cli_module()
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)

    def denied(layout):
        raise module.InstallError("bubblewrap namespace probe denied; never disable it")

    monkeypatch.setattr(cli, "preflight", denied)
    monkeypatch.setattr(
        cli, "resolve_release", lambda *a: pytest.fail("must preflight before contacting feed")
    )
    with pytest.raises(module.InstallError, match="namespace"):
        cli.dispatch(SimpleNamespace(command="install", channel="stable", yes=True, no_setup=True))
    assert not layout.install.exists() and not layout.cache.exists()


def test_managed_service_holds_shared_lock_for_entire_serve(tmp_path, monkeypatch):
    from services import launcher

    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    monkeypatch.setenv("KINAIYA_INSTALL_ROOT", str(layout.install))
    seen = []

    def serving(args):
        with pytest.raises(module.InstallError, match="busy"):
            with module.operation_lock(layout.install / "runtime.lock"):
                pytest.fail("updater must not enter while services run")
        seen.append(args.service)

    monkeypatch.setattr(launcher, "serve", serving)
    launcher.main(["serve", "host"])
    assert seen == ["host"]


def test_install_hands_setup_to_new_managed_runtime(tmp_path, monkeypatch):
    import io
    import subprocess

    cli = cli_module()
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)
    monkeypatch.setattr(cli, "preflight", lambda chosen: {"sandbox_probe": "passed"})
    monkeypatch.setattr(cli, "resolve_release", lambda channel: release)
    monkeypatch.setattr(cli, "open_release_url", lambda url: io.BytesIO(archive.read_bytes()))
    monkeypatch.setattr(
        cli,
        "install_release",
        lambda chosen, target, path: module.install_release(
            chosen, target, path, prepare=lambda *a: None, busy=lambda: False
        ),
    )
    calls = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kw: calls.append(command) or SimpleNamespace(returncode=0),
    )
    result = cli.dispatch(
        SimpleNamespace(command="install", channel="stable", yes=True, no_setup=False)
    )
    assert result["ready"] is False
    assert calls == [[str(layout.bin / "kinaiya"), "setup"]]
    assert (layout.install / "current/receipt.json").is_file()


def test_runtime_dependencies_use_isolated_hash_locked_https_wheels(tmp_path, monkeypatch):
    import subprocess

    module = distribution()
    commands = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: commands.append(command))
    module.prepare_runtime(tmp_path / "generation", tmp_path)
    pip = commands[1]
    assert "--isolated" in pip and "--require-hashes" in pip
    assert "--only-binary=:all:" in pip and "--no-input" in pip
    assert pip[pip.index("--index-url") + 1] == "https://pypi.org/simple"
