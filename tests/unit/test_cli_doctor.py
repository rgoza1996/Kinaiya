"""Issue #392: read-only CLI doctor installation, configuration, and connection diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services import launcher
from services.distribution import cli
from services.distribution.doctor import (
    VALID_CHECK_STATUSES,
    format_doctor_report,
    run_doctor,
)
from services.distribution.installation import Layout


@contextmanager
def loopback_listeners(count: int = 2):
    sockets: list[socket.socket] = []
    ports: list[int] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(5)
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        yield ports
    finally:
        for sock in sockets:
            sock.close()


def find_unused_ports(count: int = 2) -> list[int]:
    with loopback_listeners(count) as ports:
        return list(ports)


def snapshot_tree(root: Path) -> dict[str, tuple[str, int, int, str]]:
    """Capture (kind, mode, mtime_ns, sha256) for every path under root."""
    if not os.path.lexists(root):
        return {}
    result: dict[str, tuple[str, int, int, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current_dir = Path(dirpath)
        for entry in [
            current_dir,
            *(current_dir / d for d in dirnames),
            *(current_dir / f for f in filenames),
        ]:
            rel = entry.relative_to(root).as_posix() if entry != root else "."
            if rel in result:
                continue
            st = entry.lstat()
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                kind = "symlink"
                digest = os.readlink(entry)
            elif stat.S_ISREG(st.st_mode):
                kind = "file"
                digest = hashlib.sha256(entry.read_bytes()).hexdigest()
            elif stat.S_ISDIR(st.st_mode):
                kind = "dir"
                digest = ""
            else:
                kind = "other"
                digest = ""
            result[rel] = (kind, mode, st.st_mtime_ns, digest)
    return result


def build_managed_install_fixture(
    layout: Layout,
    *,
    version: str = "0.2.1",
    channel: str = "stable",
    build_number: int = 1,
    source_sha: str = "a" * 40,
) -> Path:
    layout.install.mkdir(parents=True, exist_ok=True, mode=0o700)
    release_dir = layout.install / "generations" / ("1" * 32)
    release_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    identity = {
        "schema_version": 1,
        "channel": channel,
        "version": version,
        "build_number": build_number,
        "source_sha": source_sha,
        "protocol_min": 1,
        "protocol_max": 1,
        "state_schema": 1,
        "min_updater": 1,
    }
    manifest = {
        **identity,
        "artifacts": [
            {
                "name": "kinaiya-linux-source.tar.gz",
                "platform": "linux",
                "architecture": "any",
                "url": f"https://github.com/rgoza1996/Kinaiya/releases/download/v{version}/kinaiya-linux-source.tar.gz",
                "sha256": "b" * 64,
                "size": 1024,
            }
        ],
    }
    state = {
        "schema_version": 1,
        "channel": channel,
        "version": version,
        "build_number": build_number,
        "source_sha": source_sha,
        "active_generation": release_dir.name,
    }
    (layout.install / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    (release_dir / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (release_dir / "release-identity.json").write_text(
        json.dumps(identity, indent=2) + "\n", encoding="utf-8"
    )
    current_link = layout.install / "current"
    if os.path.lexists(current_link):
        current_link.unlink()
    current_link.symlink_to(release_dir, target_is_directory=True)
    return release_dir


def write_private_tokens(layout: Layout, *, mode: int = 0o600) -> Path:
    layout.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    tokens_path = layout.data / "service-tokens.json"
    tokens_path.write_text(
        json.dumps(
            {
                "owner": "owner-secret-token-never-echo-in-doctor-123456",
                "agent": "agent-secret-token-never-echo-in-doctor-123456",
                "converse": "converse-secret-token-never-echo-in-doctor-1234",
                "decisions": "decisions-secret-token-never-echo-in-doctor-12",
                "resources": "resources-secret-token-never-echo-in-doctor-12",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(tokens_path, mode)
    return tokens_path


def write_valid_config(
    layout: Layout,
    *,
    session_port: int = 8760,
    agent_port: int = 8761,
    converse_port: int | None = None,
    converse_enabled: bool = False,
    agent_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    layout.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    default_agent = {
        "kind": "lmstudio",
        "base_url": "http://127.0.0.1:1234",
        "model": "qwen2.5-coder-7b",
        "allow_cloud": False,
        "max_tokens": 128,
        "reasoning_effort": "none",
    }
    config: dict[str, Any] = {
        "providers": {
            "agent": agent_profile or default_agent,
            "converse": {
                "kind": "compatible",
                "base_url": "http://127.0.0.1:8080/v1",
                "model": "converse-local-model"
                if converse_enabled
                else "configure-your-local-model",
                "allow_cloud": False,
                "max_tokens": 2048,
            },
            "decisions": {
                "kind": "compatible",
                "base_url": "http://127.0.0.1:8080/v1",
                "model": "decisions-local-model"
                if converse_enabled
                else "configure-your-local-model",
                "allow_cloud": False,
                "max_tokens": 2048,
            },
        },
        "session_url": f"http://127.0.0.1:{session_port}",
        "agent_url": f"http://127.0.0.1:{agent_port}",
        "session_port": session_port,
        "agent_port": agent_port,
        "distribution": {"converse": converse_enabled},
        "policy": {
            "allow_write": False,
            "allow_shell": False,
            "network": False,
            "yolo": False,
        },
    }
    if converse_port is not None:
        config["converse_port"] = converse_port
        config["converse_url"] = f"http://127.0.0.1:{converse_port}"
    layout.config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(layout.config, 0o600)
    return config


@pytest.fixture
def healthy_sandbox(monkeypatch):
    import ctypes.util
    import shutil

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: (
            f"/usr/bin/{name}" if name in {"python3", "bwrap", "curl", "sha256sum"} else None
        ),
    )
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libseccomp.so.2")
    return lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.mark.parametrize(
    "minor,supported", [(11, True), (12, True), (13, True), (14, True), (15, False)]
)
def test_doctor_python_candidate_range_matches_installer(
    monkeypatch, healthy_sandbox, minor, supported
):
    from services.distribution import doctor

    monkeypatch.setattr(
        doctor,
        "sys",
        SimpleNamespace(version_info=(3, minor, 0, "final", 0), executable=sys.executable),
    )
    check = doctor._check_sandbox_isolation(runner=healthy_sandbox)
    assert check["python_supported"] is supported
    assert check["status"] == ("pass" if supported else "fail")
    assert check["bwrap_probe"] == "pass"


def test_doctor_healthy_configured_and_listening_passes(tmp_path, healthy_sandbox):
    layout = Layout.for_home(tmp_path)
    build_managed_install_fixture(layout)
    write_private_tokens(layout, mode=0o600)

    with loopback_listeners(2) as (session_port, agent_port):
        write_valid_config(layout, session_port=session_port, agent_port=agent_port)
        before = snapshot_tree(tmp_path)

        report = run_doctor(layout, probe_services=True, env={}, runner=healthy_sandbox)
        after = snapshot_tree(tmp_path)

    assert before == after
    assert report["status"] == "pass"
    assert report["ready"] is True
    assert report["read_only"] is True
    assert report["provider_connection"] == "unknown"
    for name, check in report["checks"].items():
        assert check["status"] == "pass", f"Expected {name} to pass, got {check}"
        assert check["status"] in VALID_CHECK_STATUSES
    assert report["checks"]["managed_install"]["managed"] is True
    assert report["checks"]["managed_install"]["version"] == "0.2.1"
    assert report["checks"]["managed_install"]["channel"] == "stable"
    assert report["checks"]["provider_references"]["agent_supports_complete_measurement"] is True
    assert report["checks"]["service_reachability"]["provider_connection"] == "unknown"
    assert report["checks"]["service_reachability"]["provider_connection_status"] == "skipped"


def test_doctor_missing_config_warns_with_setup_next_step_and_zero_writes(
    tmp_path, healthy_sandbox
):
    layout = Layout.for_home(tmp_path)
    before = snapshot_tree(tmp_path)

    report = run_doctor(layout, probe_services=False, env={}, runner=healthy_sandbox)
    after = snapshot_tree(tmp_path)

    assert before == after
    assert set(after.keys()) == {"."}
    assert not layout.config.exists()
    assert not layout.data.exists()
    assert not layout.install.exists()
    assert report["status"] == "warning"
    assert report["ready"] is False
    cfg_check = report["checks"]["layout_and_config"]
    assert cfg_check["status"] == "warning"
    assert cfg_check["config_state"] == "not_configured"
    assert cfg_check["next_step"] == "kinaiya setup"
    assert "kinaiya setup" in (cfg_check["remediation"] or "")
    assert report["checks"]["managed_install"]["status"] == "warning"


def test_doctor_corrupted_config_and_corrupted_state_fail_gracefully(tmp_path, healthy_sandbox):
    layout = Layout.for_home(tmp_path)
    layout.install.mkdir(parents=True, exist_ok=True, mode=0o700)
    (layout.install / "state.json").write_text("{broken-state-json", encoding="utf-8")
    layout.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    layout.config.write_text("{corrupted-config-json", encoding="utf-8")
    write_private_tokens(layout, mode=0o600)

    before = snapshot_tree(tmp_path)
    report = run_doctor(layout, probe_services=False, env={}, runner=healthy_sandbox)
    after = snapshot_tree(tmp_path)

    assert before == after
    assert report["status"] == "fail"
    assert report["ready"] is False
    assert report["checks"]["managed_install"]["status"] == "fail"
    assert "JSONDecodeError" in report["checks"]["managed_install"]["summary"]
    assert report["checks"]["layout_and_config"]["status"] == "fail"
    assert report["checks"]["layout_and_config"]["config_state"] == "corrupted"
    assert "JSONDecodeError" in report["checks"]["layout_and_config"]["summary"]
    assert "corrupted-config-json" not in json.dumps(report)
    assert "broken-state-json" not in json.dumps(report)


def test_doctor_managed_install_broken_symlink_and_missing_manifest_fail(tmp_path, healthy_sandbox):
    layout = Layout.for_home(tmp_path)
    release_dir = build_managed_install_fixture(layout)
    write_private_tokens(layout, mode=0o600)
    write_valid_config(layout)

    # Remove release-manifest.json -> fail
    (release_dir / "release-manifest.json").unlink()
    report_missing_manifest = run_doctor(
        layout, probe_services=False, env={}, runner=healthy_sandbox
    )
    assert report_missing_manifest["checks"]["managed_install"]["status"] == "fail"
    assert (
        "release-manifest.json" in report_missing_manifest["checks"]["managed_install"]["summary"]
    )
    assert report_missing_manifest["status"] == "fail"
    assert report_missing_manifest["ready"] is False

    # Break `current` symlink -> fail
    current_link = layout.install / "current"
    current_link.unlink()
    current_link.symlink_to(layout.install / "generations" / ("dead" * 8))
    report_broken_link = run_doctor(layout, probe_services=False, env={}, runner=healthy_sandbox)
    assert report_broken_link["checks"]["managed_install"]["status"] == "fail"
    assert "current" in report_broken_link["checks"]["managed_install"]["summary"]
    assert report_broken_link["status"] == "fail"
    assert report_broken_link["ready"] is False


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX file permission bits")
def test_doctor_insecure_0644_token_permissions_fails_without_leaking_tokens(
    tmp_path, healthy_sandbox
):
    layout = Layout.for_home(tmp_path)
    build_managed_install_fixture(layout)
    tokens_path = write_private_tokens(layout, mode=0o644)
    dotenv_path = layout.config.parent / ".env"
    layout.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    dotenv_path.write_text("SECRET_TOKEN=top-secret-dotenv-value-98765\n", encoding="utf-8")
    os.chmod(dotenv_path, 0o644)

    with loopback_listeners(2) as (session_port, agent_port):
        write_valid_config(layout, session_port=session_port, agent_port=agent_port)
        report = run_doctor(layout, probe_services=True, env={}, runner=healthy_sandbox)

    assert report["status"] == "fail"
    assert report["ready"] is False
    perm_check = report["checks"]["permissions_and_tokens"]
    assert perm_check["status"] == "fail"
    assert perm_check["service_tokens"]["mode"] == "0644"
    assert "chmod 600" in (perm_check["remediation"] or "")
    assert "kinaiya auth rotate-owner" in (perm_check["remediation"] or "")

    serialized = json.dumps(report)
    formatted = format_doctor_report(report, no_color=True, is_tty=False)
    assert "owner-secret-token-never-echo-in-doctor-123456" not in serialized
    assert "top-secret-dotenv-value-98765" not in serialized
    assert "owner-secret-token-never-echo-in-doctor-123456" not in formatted
    assert "top-secret-dotenv-value-98765" not in formatted
    assert stat.S_IMODE(tokens_path.stat().st_mode) == 0o644


def test_doctor_credential_env_presence_and_never_leaks_env_value(tmp_path, healthy_sandbox):
    layout = Layout.for_home(tmp_path)
    build_managed_install_fixture(layout)
    write_private_tokens(layout, mode=0o600)

    cloud_agent = {
        "kind": "meta",
        "base_url": "https://api.example.com/v1",
        "model": "cloud-agent-model",
        "allow_cloud": True,
        "api_key_env": "KINAIYA_TEST_CLOUD_SECRET",
        "max_tokens": 2048,
        "context_tokens": 32768,
    }
    with loopback_listeners(2) as (session_port, agent_port):
        write_valid_config(
            layout,
            session_port=session_port,
            agent_port=agent_port,
            agent_profile=cloud_agent,
        )

        # 1. Missing credential_env in environment -> warning, ready=False
        missing_report = run_doctor(layout, probe_services=True, env={}, runner=healthy_sandbox)
        assert missing_report["status"] == "warning"
        assert missing_report["ready"] is False
        prov_missing = missing_report["checks"]["provider_references"]
        assert prov_missing["status"] == "warning"
        assert prov_missing["roles"]["agent"]["credential_env"] == "KINAIYA_TEST_CLOUD_SECRET"
        assert prov_missing["roles"]["agent"]["credential_present"] is False
        assert prov_missing["roles"]["agent"]["credential"] == {
            "env": "KINAIYA_TEST_CLOUD_SECRET",
            "present": False,
        }

        # 2. Present credential_env in environment -> pass, ready=True, value never leaked
        secret_value = "sk-live-super-secret-value-never-output-999999"
        present_report = run_doctor(
            layout,
            probe_services=True,
            env={"KINAIYA_TEST_CLOUD_SECRET": secret_value},
            runner=healthy_sandbox,
        )
        assert present_report["status"] == "pass"
        assert present_report["ready"] is True
        prov_present = present_report["checks"]["provider_references"]
        assert prov_present["status"] == "pass"
        assert prov_present["roles"]["agent"]["credential_present"] is True
        assert secret_value not in json.dumps(present_report)
        assert secret_value not in format_doctor_report(present_report, no_color=False, is_tty=True)


def test_doctor_rejects_unmeasured_compatible_agent_adapter_and_redacts_url_credentials(
    tmp_path, healthy_sandbox
):
    layout = Layout.for_home(tmp_path)
    build_managed_install_fixture(layout)
    write_private_tokens(layout, mode=0o600)

    bad_agent = {
        "kind": "compatible",
        "base_url": "http://user:secret@127.0.0.1:8080/v1?api_key=leak-query-token#frag",
        "model": "some-local-model",
        "allow_cloud": False,
    }
    write_valid_config(layout, agent_profile=bad_agent)

    report = run_doctor(layout, probe_services=False, env={}, runner=healthy_sandbox)
    assert report["status"] == "fail"
    assert report["ready"] is False

    prov = report["checks"]["provider_references"]
    assert prov["status"] == "fail"
    assert prov["agent_supports_complete_measurement"] is False
    agent_role = prov["roles"]["agent"]
    assert agent_role["supports_complete_measurement"] is False
    assert agent_role["endpoint"] == "http://127.0.0.1:8080/v1"
    assert agent_role["redacted_credentials"] is True

    serialized = json.dumps(report)
    rendered = format_doctor_report(report, no_color=True, is_tty=False)
    for forbidden in ("user:secret", "secret", "leak-query-token", "api_key="):
        assert forbidden not in serialized
        assert forbidden not in rendered


def test_doctor_offline_services_vs_listening_and_zero_external_network(
    tmp_path, monkeypatch, healthy_sandbox
):
    layout = Layout.for_home(tmp_path)
    build_managed_install_fixture(layout)
    write_private_tokens(layout, mode=0o600)

    # Guard socket.connect_ex to ensure only 127.0.0.1 is ever probed
    real_connect_ex = socket.socket.connect_ex
    probed_targets: list[Any] = []

    def guarded_connect_ex(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in {"127.0.0.1", "localhost", "::1"}:
            pytest.fail(f"External network connection attempted during doctor: {address!r}")
        probed_targets.append(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    session_port, agent_port = find_unused_ports(2)
    write_valid_config(layout, session_port=session_port, agent_port=agent_port)

    offline_report = run_doctor(layout, probe_services=True, env={}, runner=healthy_sandbox)
    assert offline_report["status"] == "warning"
    assert offline_report["ready"] is False
    reach_offline = offline_report["checks"]["service_reachability"]
    assert reach_offline["status"] == "warning"
    assert reach_offline["state"] == "offline"
    assert reach_offline["services"]["session"]["listening"] is False
    assert reach_offline["services"]["agent"]["listening"] is False
    assert reach_offline["provider_connection"] == "unknown"
    assert reach_offline["next_step"] == "kinaiya serve host"
    assert all(addr[0] == "127.0.0.1" for addr in probed_targets)


def test_doctor_sandbox_isolation_failures_report_actionable_remediation(tmp_path, monkeypatch):
    import shutil

    layout = Layout.for_home(tmp_path)
    write_private_tokens(layout, mode=0o600)
    write_valid_config(layout)

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: None if name in {"bwrap", "curl"} else f"/usr/bin/{name}",
    )
    report = run_doctor(layout, probe_services=False, env={})
    assert report["status"] == "fail"
    assert report["ready"] is False
    sandbox = report["checks"]["sandbox_isolation"]
    assert sandbox["status"] == "fail"
    assert "bwrap" in sandbox["missing_prerequisites"]
    assert "curl" in sandbox["missing_prerequisites"]
    assert "sudo apt-get install --no-upgrade" in (sandbox["remediation"] or "")


def test_cli_and_launcher_doctor_json_no_color_and_tty_modes(
    tmp_path, monkeypatch, capsys, healthy_sandbox
):
    layout = Layout.for_home(tmp_path)
    build_managed_install_fixture(layout)
    write_private_tokens(layout, mode=0o600)
    write_valid_config(layout)

    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)
    monkeypatch.setattr(
        "services.distribution.doctor._check_sandbox_isolation",
        lambda runner=None: {
            "status": "pass",
            "summary": "Sandbox probe passed.",
            "remediation": None,
            "next_step": None,
            "python_version": "3.12.0",
            "python_supported": True,
            "bwrap_available": True,
            "bwrap_probe": "pass",
            "curl_available": True,
            "sha256sum_available": True,
            "missing_prerequisites": [],
            "blockers": [],
        },
    )

    # 1. Explicit --json via launcher.main
    launcher.main(["doctor", "--json", "--no-service-probe"])
    out_json = capsys.readouterr().out
    assert "\x1b" not in out_json
    parsed = json.loads(out_json)
    assert parsed["command"] == "doctor"
    assert parsed["checks"]["service_reachability"]["status"] == "skipped"

    # 2. Non-TTY default emits JSON without ANSI codes
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    cli.main(["doctor", "--no-service-probe"])
    out_non_tty = capsys.readouterr().out
    assert "\x1b" not in out_non_tty
    parsed_non_tty = json.loads(out_non_tty)
    assert parsed_non_tty["command"] == "doctor"

    # 3. TTY mode emits human-readable summary with ANSI codes when NO_COLOR is unset
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    cli.main(["doctor", "--no-service-probe"])
    out_tty_color = capsys.readouterr().out
    assert "Kinaiya Doctor" in out_tty_color
    assert "\x1b[" in out_tty_color

    # 4. TTY mode with NO_COLOR=1 emits zero ANSI escape codes
    monkeypatch.setenv("NO_COLOR", "1")
    cli.main(["doctor", "--no-service-probe"])
    out_tty_no_color = capsys.readouterr().out
    assert "Kinaiya Doctor" in out_tty_no_color
    assert "\x1b" not in out_tty_no_color


def test_doctor_supports_install_release_receipt_and_converse_port_probe(tmp_path, healthy_sandbox):
    from test_linux_distribution import archive_release, distribution

    module = distribution()
    layout = Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path, build=4, channel="stable")
    module.install_release(layout, release, archive, prepare=lambda *a: None, busy=lambda: False)
    write_private_tokens(layout, mode=0o600)

    with loopback_listeners(3) as (session_port, agent_port, converse_port):
        write_valid_config(
            layout,
            session_port=session_port,
            agent_port=agent_port,
            converse_port=converse_port,
            converse_enabled=True,
        )
        report = run_doctor(layout, probe_services=True, env={}, runner=healthy_sandbox)

    assert report["status"] == "pass"
    assert report["ready"] is True
    assert report["checks"]["managed_install"]["status"] == "pass"
    assert report["checks"]["managed_install"]["channel"] == "stable"
    assert report["checks"]["service_reachability"]["converse_port"] == converse_port
    assert report["checks"]["service_reachability"]["services"]["converse"]["listening"] is True
