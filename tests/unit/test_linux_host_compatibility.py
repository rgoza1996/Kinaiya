"""Keep downloadable and managed host admission aligned with the candidate baseline."""

import ctypes.util
import os
import platform
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture(params=["bootstrap", "managed"])
def host_preflight(request, monkeypatch, tmp_path):
    if request.param == "bootstrap":
        script = Path(__file__).resolve().parents[2] / "install.sh"
        source = script.read_text().split("# BEGIN PYTHON BOOTSTRAP\n", 1)[1]
        source = source.split("\n# END PYTHON BOOTSTRAP", 1)[0]
        module = ModuleType("bootstrap_host_test")
        exec(compile(source, str(script), "exec"), module.__dict__)
        run = module.preflight
        error = module.BootstrapError
        monkeypatch.setattr(module, "missing_dependencies", lambda: [])
    else:
        from services.distribution import installation as module

        layout = module.Layout.for_home(tmp_path)

        def run():
            return module.preflight(layout)

        error = module.InstallError
        monkeypatch.setattr(module, "missing_system_dependencies", lambda: [])

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "release", lambda: "7.0.0-generic")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        platform, "freedesktop_os_release", lambda: {"ID": "ubuntu", "VERSION_ID": "26.04"}
    )
    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3))
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libseccomp.so.2")
    versions = {"bubblewrap": "0.11.1", "libseccomp2": "2.6.0"}
    monkeypatch.setattr(module, "installed_package_version", versions.get)
    probes = []

    def probe(argv, **kwargs):
        probes.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", probe)
    return SimpleNamespace(run=run, error=error, module=module, probes=probes)


@pytest.mark.parametrize("ubuntu", ["22.04", "24.04", "26.04"])
@pytest.mark.parametrize("minor", [11, 12, 13, 14])
@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
def test_supported_hosts_still_require_namespace_probe(
    host_preflight, monkeypatch, tmp_path, ubuntu, minor, architecture
):
    monkeypatch.setattr(
        platform, "freedesktop_os_release", lambda: {"ID": "ubuntu", "VERSION_ID": ubuntu}
    )
    monkeypatch.setattr(sys, "version_info", (3, minor, 0, "final", 0))
    monkeypatch.setattr(platform, "machine", lambda: architecture)
    host_preflight.run()
    assert len(host_preflight.probes) == 1
    assert "--unshare-all" in host_preflight.probes[0]
    assert not (tmp_path / ".local").exists()


@pytest.mark.parametrize(
    "distro,version", [("ubuntu", "25.10"), ("ubuntu", "26.10"), ("debian", "12")]
)
def test_other_distros_and_interim_releases_remain_refused(
    host_preflight, monkeypatch, tmp_path, distro, version
):
    monkeypatch.setattr(
        platform, "freedesktop_os_release", lambda: {"ID": distro, "VERSION_ID": version}
    )
    with pytest.raises(host_preflight.error, match="Ubuntu"):
        host_preflight.run()
    assert not host_preflight.probes
    assert not (tmp_path / ".local").exists()


@pytest.mark.parametrize("minor", [10, 15])
def test_python_versions_outside_candidate_range_remain_refused(host_preflight, monkeypatch, minor):
    monkeypatch.setattr(sys, "version_info", (3, minor, 0, "final", 0))
    with pytest.raises(host_preflight.error, match="Python"):
        host_preflight.run()
    assert not host_preflight.probes


@pytest.mark.parametrize("blocker", ["root", "system", "architecture", "disk", "namespace"])
def test_new_baseline_preserves_host_safety_gates(host_preflight, monkeypatch, tmp_path, blocker):
    module = host_preflight.module
    if blocker == "root":
        monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    elif blocker == "system":
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
    elif blocker == "architecture":
        monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    elif blocker == "disk":
        monkeypatch.setattr(
            module.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024**3 - 1)
        )
    else:
        monkeypatch.setattr(
            module.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1)
        )
    with pytest.raises(host_preflight.error):
        host_preflight.run()
    assert not (tmp_path / ".local").exists()


def test_shell_discovers_python314_when_default_python_is_unsupported(tmp_path):
    script = Path(__file__).resolve().parents[2] / "install.sh"
    for name, supported in [("python3", False), ("python3.14", True)]:
        binary = tmp_path / name
        binary.write_text(
            '#!/bin/sh\nif [ "$1" = "-c" ]; then\n'
            f"  exit {0 if supported else 1}\nfi\n"
            f"printf '%s\\n' '{name}'\n"
        )
        binary.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path)}
    env.pop("KINAIYA_PYTHON", None)
    result = subprocess.run(
        ["/bin/sh", str(script), "--help"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "python3.14"
