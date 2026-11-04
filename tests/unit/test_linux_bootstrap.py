"""Exercise the standalone downloadable shell bootstrap without remote requests."""

import io
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError

import pytest


@pytest.fixture
def bootstrap():
    script = Path(__file__).resolve().parents[2] / "install.sh"
    assert script.is_file(), "standalone Linux bootstrap is missing"
    text = script.read_text()
    assert "# BEGIN PYTHON BOOTSTRAP\n" in text
    source = text.split("# BEGIN PYTHON BOOTSTRAP\n", 1)[1].split("\n# END PYTHON BOOTSTRAP", 1)[0]
    module = ModuleType("bootstrap_test")
    exec(compile(source, str(script), "exec"), module.__dict__)
    return module


def test_stable_absent_is_explicit_and_does_not_fallback(bootstrap, monkeypatch):
    calls = []

    def absent(url, **kwargs):
        calls.append(url)
        raise HTTPError(url, 404, "not published", {}, None)

    monkeypatch.setattr(bootstrap, "read_json", absent)
    with pytest.raises(bootstrap.BootstrapError, match="stable.*published"):
        bootstrap.resolve("stable")
    assert calls == [bootstrap.API + "/latest"]


def test_bootstrap_cannot_extract_links_or_traversal(bootstrap, tmp_path):
    import tarfile

    for name in ("../../outside", "/absolute", "valid/link"):
        archive = tmp_path / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo(name)
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside"
            tar.addfile(member)
        with pytest.raises(bootstrap.BootstrapError, match="unsafe|Unsafe"):
            bootstrap.unpack(archive, tmp_path / name.replace("/", "_"))
    assert not (tmp_path / "outside").exists()


def test_bootstrap_refuses_bad_digest_before_loading_release_code(bootstrap, tmp_path, monkeypatch):
    artifact = {
        "url": "https://github.com/rgoza1996/Kinaiya/releases/download/v0.2.1/kinaiya-v0.2.1-source.tar.gz",
        "size": 3,
        "sha256": "0" * 64,
    }
    monkeypatch.setattr(bootstrap, "open_url", lambda url: io.BytesIO(b"abc"))
    with pytest.raises(bootstrap.BootstrapError, match="digest"):
        bootstrap.download(artifact, tmp_path / "archive")


def test_bootstrap_preflight_failure_happens_before_feed_or_cache(bootstrap, tmp_path, monkeypatch):
    def denied():
        raise bootstrap.BootstrapError("bubblewrap namespace probe denied; never disable isolation")

    monkeypatch.setattr(bootstrap, "preflight", denied)
    monkeypatch.setattr(bootstrap, "resolve", lambda *a: pytest.fail("must not fetch release"))
    with pytest.raises(bootstrap.BootstrapError, match="namespace"):
        bootstrap.run(["--yes", "--no-setup"])


@pytest.mark.parametrize("header_type", [b"x", b"g", b"L"])
@pytest.mark.parametrize("extractor", ["bootstrap", "managed"])
def test_extractors_reject_hidden_extension_metadata(bootstrap, tmp_path, header_type, extractor):
    import hashlib
    import tarfile

    from services.distribution.installation import InstallError, unpack_source

    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
        member = tarfile.TarInfo("hidden-extension")
        member.type = header_type
        payload = b"131072 comment=" + b"x" * (131072 - 16) + b"\n"
        if header_type == b"L":
            payload = b"pyproject.toml\0" + b"\0" * 131057
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
        for name in ("pyproject.toml", "requirements-runtime.lock"):
            member = tarfile.TarInfo(name)
            member.size = 1
            tar.addfile(member, io.BytesIO(b"#"))
    destination = tmp_path / "output"
    with pytest.raises((bootstrap.BootstrapError, InstallError), match="regular|extension"):
        if extractor == "bootstrap":
            bootstrap.unpack(archive, destination)
        else:
            unpack_source(
                archive,
                destination,
                {
                    "size": archive.stat().st_size,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                },
            )
    assert not any(destination.iterdir())


@pytest.mark.parametrize("damage", ["crc", "trailer"])
@pytest.mark.parametrize("extractor", ["bootstrap", "managed"])
def test_extractors_reject_corrupt_gzip_before_writes(bootstrap, tmp_path, damage, extractor):
    import hashlib
    import tarfile

    from services.distribution.installation import InstallError, unpack_source

    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
        for name in ("pyproject.toml", "requirements-runtime.lock"):
            member = tarfile.TarInfo(name)
            member.size = 1
            tar.addfile(member, io.BytesIO(b"#"))
    raw = bytearray(archive.read_bytes())
    if damage == "crc":
        raw[-8] ^= 1
    else:
        del raw[-8:]
    archive.write_bytes(raw)
    destination = tmp_path / "output"
    with pytest.raises((bootstrap.BootstrapError, InstallError)):
        if extractor == "bootstrap":
            bootstrap.unpack(archive, destination)
        else:
            unpack_source(
                archive,
                destination,
                {
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
            )
    assert not destination.exists() or not any(destination.iterdir())


def test_bootstrap_warns_on_outdated_system_blockers_and_installs_missing_deps_with_no_upgrade(
    bootstrap, monkeypatch
):
    from types import SimpleNamespace

    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        bootstrap.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "20.04"},
    )
    with pytest.raises(
        bootstrap.BootstrapError,
        match="Out-of-date system blocker: Ubuntu 20.04 is out of date",
    ):
        bootstrap.check_outdated_system_blockers()

    monkeypatch.setattr(
        bootstrap.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"},
    )
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(bootstrap, "installed_package_version", lambda pkg: "0.5.0")
    with pytest.raises(
        bootstrap.BootstrapError,
        match="Out-of-date system blocker: bubblewrap 0.5.0 is out of date",
    ):
        bootstrap.check_outdated_system_blockers()

    commands = []

    def fake_run(argv, **kw):
        commands.append(argv)
        if "-s" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "0 upgraded, 3 newly installed, 0 to remove and 0 not upgraded.\n"
                    "Inst bubblewrap (0.9.0-1ubuntu0.3 Ubuntu:24.04/noble [amd64])\n"
                    "Inst libseccomp2 (2.5.5-1ubuntu3.1 Ubuntu:24.04/noble [amd64])\n"
                    "Inst python3.12-venv (3.12.3-1ubuntu0.17 Ubuntu:24.04/noble [amd64])\n"
                    "Conf bubblewrap (0.9.0-1ubuntu0.3 Ubuntu:24.04/noble [amd64])\n"
                    "Conf libseccomp2 (2.5.5-1ubuntu3.1 Ubuntu:24.04/noble [amd64])\n"
                    "Conf python3.12-venv (3.12.3-1ubuntu0.17 Ubuntu:24.04/noble [amd64])\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    bootstrap.install_missing_dependencies(["bubblewrap", "libseccomp2", "python3.12-venv"])
    assert commands == [
        [
            "apt-get",
            "-s",
            "--no-upgrade",
            "--no-remove",
            "-o",
            "APT::Get::Upgrade=false",
            "-o",
            "APT::Get::Remove=false",
            "-o",
            "APT::Get::Allow-Downgrades=false",
            "-y",
            "install",
            "--",
            "bubblewrap",
            "libseccomp2",
            "python3.12-venv",
        ],
        [
            "sudo",
            "apt-get",
            "install",
            "--no-upgrade",
            "--no-remove",
            "-o",
            "APT::Get::Upgrade=false",
            "-o",
            "APT::Get::Remove=false",
            "-o",
            "APT::Get::Allow-Downgrades=false",
            "-y",
            "--",
            "bubblewrap=0.9.0-1ubuntu0.3",
            "libseccomp2=2.5.5-1ubuntu3.1",
            "python3.12-venv=3.12.3-1ubuntu0.17",
        ],
    ]
    for cmd in commands:
        assert "update" not in cmd and "upgrade" not in cmd


def test_bootstrap_shell_captures_invoking_pwd_and_prompts_default_vs_current(
    bootstrap, tmp_path, monkeypatch, capsys
):
    from contextlib import contextmanager

    script_text = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    assert 'KINAIYA_INVOKING_PWD="$(pwd -P 2>/dev/null || pwd)"' in script_text

    home = tmp_path / "home"
    home.mkdir()
    invoking = tmp_path / "My Kinaiya App"
    invoking.mkdir()
    monkeypatch.setattr(bootstrap.Path, "home", lambda: home)
    monkeypatch.setenv("KINAIYA_INVOKING_PWD", str(invoking))
    monkeypatch.delenv("KINAIYA_INSTALL_ROOT", raising=False)
    monkeypatch.setattr(bootstrap, "preflight", lambda **kw: None)
    monkeypatch.setattr(bootstrap, "missing_dependencies", lambda: [])
    monkeypatch.setattr(
        bootstrap, "resolve", lambda channel: ({"source_sha": "b" * 40}, {"size": 42})
    )

    class FakeTTY:
        def __init__(self, lines):
            self.lines = iter(lines)
            self.written = ""

        def write(self, text):
            self.written += text

        def readline(self):
            return next(self.lines, "")

    # 1) Selecting choice 2 (Current directory) with spaces in path:
    tty = FakeTTY(["2\n", "yes\n"])

    @contextmanager
    def fake_open_terminal():
        yield tty

    monkeypatch.setattr(bootstrap, "open_terminal", fake_open_terminal)

    def stop_at_download(artifact, archive):
        raise SystemExit(0)

    monkeypatch.setattr(bootstrap, "download", stop_at_download)
    with pytest.raises(SystemExit):
        bootstrap.run(["--no-setup"])
    assert "1) Default (recommended):" in tty.written
    assert "2) Current directory:" in tty.written
    assert str(invoking.resolve()) in tty.written
    plan_out = capsys.readouterr().out.strip()
    assert f'"install": "{invoking}"' in plan_out

    # 2) Equal canonical paths: no redundant location prompt
    default_install = home / ".local/share/kinaiya/install"
    default_install.mkdir(parents=True)
    monkeypatch.setenv("KINAIYA_INVOKING_PWD", str(default_install))
    tty_equal = FakeTTY(["yes\n"])

    @contextmanager
    def fake_open_terminal_equal():
        yield tty_equal

    monkeypatch.setattr(bootstrap, "open_terminal", fake_open_terminal_equal)
    with pytest.raises(SystemExit):
        bootstrap.run(["--no-setup"])
    assert "Choose install location" not in tty_equal.written
    assert "Review the plan. Download and install? (y/n) [n]: " in tty_equal.written


def test_bootstrap_rejects_unsafe_or_populated_install_roots_without_deleting_files(
    bootstrap, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(bootstrap.Path, "home", lambda: home)
    monkeypatch.setattr(bootstrap, "preflight", lambda **kw: None)
    monkeypatch.setattr(bootstrap, "missing_dependencies", lambda: [])
    monkeypatch.setattr(
        bootstrap, "resolve", lambda *a: pytest.fail("must reject invalid install root before feed")
    )

    populated = tmp_path / "existing-project"
    populated.mkdir()
    keep = populated / "important.txt"
    keep.write_text("do not delete")

    with pytest.raises(bootstrap.BootstrapError, match="non-empty|unmanaged"):
        bootstrap.run(["--yes", "--no-setup", "--install-dir", str(populated)])
    assert keep.read_text() == "do not delete"

    alias = tmp_path / "symlink-install"
    target = tmp_path / "real-target"
    target.mkdir()
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(bootstrap.BootstrapError, match="symlink|alias"):
        bootstrap.run(["--yes", "--no-setup", "--install-dir", str(alias)])

    with pytest.raises(bootstrap.BootstrapError, match="protected"):
        bootstrap.run(["--yes", "--no-setup", "--install-dir", "/etc"])

    with pytest.raises(bootstrap.BootstrapError, match="home|disjoint"):
        bootstrap.run(["--yes", "--no-setup", "--install-dir", str(home)])

    with pytest.raises(bootstrap.BootstrapError, match="disjoint"):
        bootstrap.run(
            ["--yes", "--no-setup", "--install-dir", str(home / "Kinaiya Workspace" / "sub")]
        )


def test_bootstrap_confirm_yn_accepts_short_y_and_rejects_n(bootstrap):
    out_y = io.StringIO()
    tty_y = type(
        "TTY",
        (),
        {
            "write": out_y.write,
            "flush": lambda self: None,
            "readline": lambda self: "y\n",
        },
    )()
    assert bootstrap.confirm_yn(tty_y) is True
    assert "(y/n) [n]: " in out_y.getvalue()

    out_n = io.StringIO()
    tty_n = type(
        "TTY",
        (),
        {
            "write": out_n.write,
            "flush": lambda self: None,
            "readline": lambda self: "n\n",
        },
    )()
    assert bootstrap.confirm_yn(tty_n) is False


@pytest.mark.parametrize(
    "answer,pre_consented,expected",
    [
        ("Y\n", False, True),
        (" YeS \n", False, True),
        ("N\n", False, False),
        ("no\n", False, False),
        ("maybe\n", False, False),
        ("\n", False, False),
        ("", False, False),
        ("", True, True),
        ("\n", True, False),
        ("n\n", True, False),
        ("maybe\n", True, False),
    ],
)
def test_bootstrap_confirmation_preserves_only_existing_eof_consent(
    bootstrap, answer, pre_consented, expected
):
    class Terminal:
        def write(self, text):
            pass

        def flush(self):
            pass

        def readline(self):
            return answer

    assert bootstrap.confirm_yn(Terminal(), pre_consented=pre_consented) is expected
