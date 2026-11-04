"""Issue #336 (KIN-02): enforce no upgrades, downgrades, or removals of existing packages."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from services.distribution import installation


@pytest.fixture
def bootstrap() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "install.sh"
    text = script.read_text(encoding="utf-8")
    source = text.split("# BEGIN PYTHON BOOTSTRAP\n", 1)[1].split("\n# END PYTHON BOOTSTRAP", 1)[0]
    module = ModuleType("bootstrap_apt_test")
    exec(compile(source, str(script), "exec"), module.__dict__)
    return module


def _target_modules(bootstrap: ModuleType):
    return (
        ("installation", installation, installation.InstallError),
        ("bootstrap", bootstrap, bootstrap.BootstrapError),
    )


def _write_dpkg_status(path: Path, packages: dict[str, str]) -> Path:
    stanzas = []
    for pkg, ver in packages.items():
        stanzas.append(
            f"Package: {pkg}\n"
            "Status: install ok installed\n"
            "Priority: optional\n"
            "Section: misc\n"
            "Installed-Size: 10\n"
            "Maintainer: Test <test@example.com>\n"
            "Architecture: amd64\n"
            f"Version: {ver}\n"
            f"Description: Synthetic {pkg}\n"
        )
    path.write_text("\n".join(stanzas) + ("\n" if stanzas else ""), encoding="utf-8")
    return path


def test_verifier_refuses_transitive_dependency_upgrade_before_sudo(bootstrap, tmp_path):
    status = _write_dpkg_status(tmp_path / "status", {"demo-base": "1.0"})
    sim_stdout = (
        "Reading package lists...\n"
        "Building dependency tree...\n"
        "The following additional packages will be installed:\n"
        "  demo-base\n"
        "The following NEW packages will be installed:\n"
        "  demo-addon\n"
        "The following packages will be upgraded:\n"
        "  demo-base\n"
        "1 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\n"
        "Inst demo-base [1.0] (2.0 localhost [amd64])\n"
        "Inst demo-addon (1.0 localhost [amd64])\n"
        "Conf demo-base (2.0 localhost [amd64])\n"
        "Conf demo-addon (1.0 localhost [amd64])\n"
    )
    for _name, mod, err_cls in _target_modules(bootstrap):
        calls = []

        def runner(argv, _calls=calls, **kwargs):
            _calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout=sim_stdout, stderr="")

        with pytest.raises(
            err_cls,
            match=r"upgrade existing package\(s\) \[demo-base \(1\.0 -> 2\.0\)\].*refuses to upgrade, downgrade, or remove",
        ):
            mod.install_missing_dependencies(["demo-addon"], runner=runner, status_path=status)
        assert len(calls) == 1
        assert calls[0][0] == "apt-get" and "-s" in calls[0]
        assert "sudo" not in calls[0]


@pytest.mark.parametrize(
    "case,sim_rc,sim_stdout,sim_stderr,expected_pattern",
    [
        (
            "remv_line",
            0,
            (
                "The following packages will be REMOVED:\n"
                "  demo-conflict\n"
                "The following NEW packages will be installed:\n"
                "  demo-addon\n"
                "0 upgraded, 1 newly installed, 1 to remove and 0 not upgraded.\n"
                "Remv demo-conflict [1.0]\n"
                "Inst demo-addon (1.0 localhost [amd64])\n"
                "Conf demo-addon (1.0 localhost [amd64])\n"
            ),
            "",
            r"remove existing package\(s\) \[demo-conflict \(1\.0\)\]",
        ),
        (
            "remove_disabled_abort",
            100,
            (
                "The following packages will be REMOVED:\n"
                "  demo-base\n"
                "The following NEW packages will be installed:\n"
                "  demo-addon\n"
                "0 upgraded, 1 newly installed, 1 to remove and 0 not upgraded.\n"
            ),
            "E: Packages need to be removed but remove is disabled.\n",
            r"remove existing package\(s\) \[demo-base \(1\.0\)\]",
        ),
        (
            "downgrade_inst",
            0,
            (
                "The following packages will be DOWNGRADED:\n"
                "  demo-base\n"
                "The following NEW packages will be installed:\n"
                "  demo-addon\n"
                "0 upgraded, 1 newly installed, 1 downgraded, 0 to remove and 0 not upgraded.\n"
                "Inst demo-base [2.0] (1.0 localhost [amd64])\n"
                "Inst demo-addon (1.0 localhost [amd64])\n"
                "Conf demo-base (1.0 localhost [amd64])\n"
                "Conf demo-addon (1.0 localhost [amd64])\n"
            ),
            "",
            r"downgrade existing package\(s\) \[demo-base \(2\.0 -> 1\.0\)\]",
        ),
        (
            "downgrade_version_compare_without_header",
            0,
            (
                "0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\n"
                "Inst demo-base [2.0-1ubuntu2] (1.9-1ubuntu1 localhost [amd64])\n"
                "Inst demo-addon (1.0 localhost [amd64])\n"
                "Conf demo-base (1.9-1ubuntu1 localhost [amd64])\n"
                "Conf demo-addon (1.0 localhost [amd64])\n"
            ),
            "",
            r"downgrade existing package\(s\) \[demo-base \(2\.0-1ubuntu2 -> 1\.9-1ubuntu1\)\]",
        ),
        (
            "conflict_unmet_dependencies",
            100,
            (
                "The following packages have unmet dependencies:\n"
                " demo-addon : Depends: demo-base (>= 2.0) but 1.0 is to be installed\n"
            ),
            "E: Unable to correct problems, you have held broken packages.\n",
            r"Out-of-date system blocker.*demo-addon.*refuses to upgrade, downgrade, or remove",
        ),
        (
            "broken_trailing_brackets",
            0,
            (
                "0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\n"
                "Inst demo-addon (1.0 localhost [amd64]) [demo-base:amd64 ]\n"
                "Conf demo-addon (1.0 localhost [amd64]) [demo-base:amd64 ]\n"
            ),
            "",
            r"Out-of-date system blocker.*demo-addon.*refuses to upgrade, downgrade, or remove",
        ),
        (
            "hidden_existing_package_without_inst_bracket",
            0,
            (
                "0 upgraded, 2 newly installed, 0 to remove and 0 not upgraded.\n"
                "Inst demo-base (2.0 localhost [amd64])\n"
                "Inst demo-addon (1.0 localhost [amd64])\n"
                "Conf demo-base (2.0 localhost [amd64])\n"
                "Conf demo-addon (1.0 localhost [amd64])\n"
            ),
            "",
            r"upgrade existing package\(s\) \[demo-base \(1\.0 -> 2\.0\)\]",
        ),
    ],
)
def test_verifier_refuses_removals_downgrades_and_conflicts(
    bootstrap, tmp_path, case, sim_rc, sim_stdout, sim_stderr, expected_pattern
):
    status = _write_dpkg_status(
        tmp_path / f"status-{case}",
        {"demo-base": "2.0" if "downgrade" in case else "1.0", "demo-conflict": "1.0"},
    )
    for _name, mod, err_cls in _target_modules(bootstrap):
        calls = []

        def runner(argv, _calls=calls, **kwargs):
            _calls.append(list(argv))
            return SimpleNamespace(returncode=sim_rc, stdout=sim_stdout, stderr=sim_stderr)

        with pytest.raises(err_cls, match=expected_pattern):
            mod.install_missing_dependencies(["demo-addon"], runner=runner, status_path=status)
        assert len(calls) == 1
        assert "sudo" not in calls[0]


def test_verifier_refuses_state_changes_between_preflight_and_execution(bootstrap, tmp_path):
    for name, mod, err_cls in _target_modules(bootstrap):
        # 1. Requested package became installed before execution starts.
        status_already = _write_dpkg_status(
            tmp_path / f"status-already-{name}", {"demo-base": "1.0", "demo-addon": "1.0"}
        )
        calls = []
        with pytest.raises(
            err_cls, match=r"requested package\(s\) \[demo-addon \(1\.0\)\] are already installed"
        ):
            mod.install_missing_dependencies(
                ["demo-addon"],
                runner=lambda argv, _calls=calls, **kw: _calls.append(argv),
                status_path=status_already,
            )
        assert calls == []

        # 2. Dpkg status changes between preflight snapshot and install execution.
        status_drift = _write_dpkg_status(tmp_path / f"status-drift-{name}", {"demo-base": "1.0"})
        preflight_snapshot = mod.installed_packages_snapshot(status_drift)
        _write_dpkg_status(status_drift, {"demo-base": "1.0", "demo-concurrent": "1.0"})
        with pytest.raises(
            err_cls,
            match=r"system package state changed between preflight inspection and dependency installation",
        ):
            mod.install_missing_dependencies(
                ["demo-addon"],
                runner=lambda argv, _calls=calls, **kw: _calls.append(argv),
                status_path=status_drift,
                expected_snapshot=preflight_snapshot,
            )
        assert calls == []

        # 3. Dpkg status mutates during simulation before sudo apt-get runs.
        status_race = _write_dpkg_status(tmp_path / f"status-race-{name}", {"demo-base": "1.0"})

        def racing_runner(argv, _calls=calls, _status_race=status_race, **kwargs):
            _calls.append(list(argv))
            _write_dpkg_status(_status_race, {"demo-base": "1.0", "demo-addon": "0.9"})
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\n"
                    "Inst demo-addon (1.0 localhost [amd64])\n"
                    "Conf demo-addon (1.0 localhost [amd64])\n"
                ),
                stderr="",
            )

        with pytest.raises(
            err_cls,
            match=r"system package state changed during apt-get solver verification",
        ):
            mod.install_missing_dependencies(
                ["demo-addon"], runner=racing_runner, status_path=status_race
            )
        assert len(calls) == 1
        assert "sudo" not in calls[0]


def test_clean_missing_package_only_install_pins_exact_package_versions(bootstrap, tmp_path):
    status = _write_dpkg_status(tmp_path / "status-clean", {"demo-base": "1.0"})
    sim_stdout = (
        "Reading package lists...\n"
        "Building dependency tree...\n"
        "The following additional packages will be installed:\n"
        "  demo-helper\n"
        "The following NEW packages will be installed:\n"
        "  demo-addon demo-helper\n"
        "0 upgraded, 2 newly installed, 0 to remove and 1 not upgraded.\n"
        "Inst demo-helper (1.2-1ubuntu1 localhost [amd64])\n"
        "Inst demo-addon (1.0-1ubuntu1 localhost [amd64])\n"
        "Conf demo-helper (1.2-1ubuntu1 localhost [amd64])\n"
        "Conf demo-addon (1.0-1ubuntu1 localhost [amd64])\n"
    )
    for _name, mod, _err_cls in _target_modules(bootstrap):
        calls = []

        def runner(argv, _calls=calls, **kwargs):
            _calls.append(list(argv))
            if "-s" in argv:
                return SimpleNamespace(returncode=0, stdout=sim_stdout, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        verified = mod.verify_apt_no_existing_package_changes(
            ["demo-addon"], runner=runner, status_path=status
        )
        assert verified["pinned_packages"] == [
            "demo-helper=1.2-1ubuntu1",
            "demo-addon=1.0-1ubuntu1",
        ]
        calls.clear()
        installed = mod.install_missing_dependencies(
            ["demo-addon"],
            runner=runner,
            status_path=status,
            expected_snapshot=verified["installed_snapshot"],
            expected_pinned=verified["pinned_packages"],
        )
        assert installed == ["demo-addon"]
        assert len(calls) == 2
        assert calls[0] == [
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
            "demo-addon",
        ]
        assert calls[1] == [
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
            "demo-helper=1.2-1ubuntu1",
            "demo-addon=1.0-1ubuntu1",
        ]


def _setup_isolated_apt_sandbox(
    root: Path, *, installed_stanzas: str, repo_packages_stanzas: str
) -> dict[str, str]:
    for rel in (
        "var/lib/dpkg",
        "var/lib/apt/lists/partial",
        "var/cache/apt/archives/partial",
        "var/log/apt",
        "etc/apt/apt.conf.d",
        "etc/apt/sources.list.d",
        "etc/apt/preferences.d",
        "repo",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)

    status = root / "var/lib/dpkg/status"
    status.write_text(installed_stanzas, encoding="utf-8")

    packages_index = root / "repo/Packages"
    packages_index.write_text(repo_packages_stanzas, encoding="utf-8")

    sources_list = root / "etc/apt/sources.list"
    sources_list.write_text(f"deb [trusted=yes] file:{root / 'repo'} ./\n", encoding="utf-8")

    apt_conf = root / "apt.conf"
    apt_conf.write_text(
        f'APT::Architecture "amd64";\n'
        f'Dir "{root}";\n'
        f'Dir::State "var/lib/apt";\n'
        f'Dir::State::status "{status}";\n'
        f'Dir::Cache "var/cache/apt";\n'
        f'Dir::Etc "etc/apt";\n'
        f'Dir::Etc::sourcelist "{sources_list}";\n'
        f'Dir::Etc::sourceparts "{root / "etc/apt/sources.list.d"}";\n'
        f'Dir::Etc::main "{apt_conf}";\n'
        f'Dir::Etc::parts "{root / "etc/apt/apt.conf.d"}";\n'
        f'Dir::Etc::preferences "{root / "etc/apt/preferences"}";\n'
        f'Dir::Etc::preferencesparts "{root / "etc/apt/preferences.d"}";\n'
        f'Dir::Log "var/log/apt";\n'
        f'Acquire::AllowInsecureRepositories "true";\n',
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "APT_CONFIG": str(apt_conf),
        "DEBIAN_FRONTEND": "noninteractive",
        "LC_ALL": "C",
        "LANG": "C",
    }
    update_res = subprocess.run(
        ["apt-get", "update"], env=env, capture_output=True, text=True, timeout=30
    )
    assert update_res.returncode == 0, update_res.stderr
    return env


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("apt-get") is None,
    reason="Requires Linux host with apt-get binary for isolated APT_CONFIG simulation",
)
def test_real_isolated_apt_simulation_reproduces_and_blocks_transitive_upgrade(bootstrap, tmp_path):
    """Prove `apt-get -s --no-upgrade` upgrades transitive `demo-base` and our verifier blocks it."""
    installed_stanzas = (
        "Package: demo-base\n"
        "Status: install ok installed\n"
        "Priority: optional\n"
        "Section: misc\n"
        "Installed-Size: 10\n"
        "Maintainer: Test <test@example.com>\n"
        "Architecture: amd64\n"
        "Version: 1.0\n"
        "Description: Demo base package\n\n"
    )
    repo_stanzas = (
        "Package: demo-base\n"
        "Priority: optional\n"
        "Section: misc\n"
        "Installed-Size: 10\n"
        "Maintainer: Test <test@example.com>\n"
        "Architecture: amd64\n"
        "Version: 2.0\n"
        "Filename: pool/demo-base_2.0_amd64.deb\n"
        "Size: 1024\n"
        "SHA256: " + ("a" * 64) + "\n"
        "Description: Demo base package\n\n"
        "Package: demo-addon\n"
        "Priority: optional\n"
        "Section: misc\n"
        "Installed-Size: 10\n"
        "Maintainer: Test <test@example.com>\n"
        "Architecture: amd64\n"
        "Version: 1.0\n"
        "Depends: demo-base (>= 2.0)\n"
        "Filename: pool/demo-addon_1.0_amd64.deb\n"
        "Size: 1024\n"
        "SHA256: " + ("b" * 64) + "\n"
        "Description: Demo addon package\n\n"
    )
    env = _setup_isolated_apt_sandbox(
        tmp_path / "upgrade-case",
        installed_stanzas=installed_stanzas,
        repo_packages_stanzas=repo_stanzas,
    )

    # 1. Confirm raw `apt-get -s --no-upgrade -y install demo-addon` selects `demo-base` for upgrade.
    raw_sim = subprocess.run(
        ["apt-get", "-s", "--no-upgrade", "-y", "install", "demo-addon"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert raw_sim.returncode == 0
    assert "Inst demo-base [1.0] (2.0" in raw_sim.stdout
    assert "1 upgraded, 1 newly installed" in raw_sim.stdout

    # 2. Prove both `installation` and `install.sh` refuse before any mutating command.
    for _name, mod, err_cls in _target_modules(bootstrap):
        executed_commands = []

        def safe_runner(argv, _executed=executed_commands, **kwargs):
            _executed.append(list(argv))
            assert argv[0] == "apt-get" and "-s" in argv, (
                f"Mutating command must never be invoked when upgrade is required: {argv}"
            )
            return subprocess.run(argv, **kwargs)

        with pytest.raises(
            err_cls,
            match=r"upgrade existing package\(s\) \[demo-base \(1\.0 -> 2\.0\)\].*refuses to upgrade, downgrade, or remove",
        ):
            mod.install_missing_dependencies(["demo-addon"], env=env, runner=safe_runner)
        assert len(executed_commands) == 1
        assert "sudo" not in executed_commands[0]

    # 3. Prove clean missing-package-only resolution in the same isolated APT sandbox succeeds
    # when `demo-base (>= 1.0)` is already satisfied by installed `demo-base 1.0`, even though
    # `demo-base 2.0` is available in the repository.
    clean_repo_stanzas = repo_stanzas.replace(
        "Depends: demo-base (>= 2.0)", "Depends: demo-base (>= 1.0)"
    )
    clean_env = _setup_isolated_apt_sandbox(
        tmp_path / "clean-case",
        installed_stanzas=installed_stanzas,
        repo_packages_stanzas=clean_repo_stanzas,
    )
    for _name, mod, _err_cls in _target_modules(bootstrap):
        verified = mod.verify_apt_no_existing_package_changes(["demo-addon"], env=clean_env)
        assert verified["pinned_packages"] == ["demo-addon=1.0"]
        assert verified["summary"]["upgraded"] == 0
        assert verified["summary"]["newly_installed"] == 1
        assert verified["summary"]["to_remove"] == 0
