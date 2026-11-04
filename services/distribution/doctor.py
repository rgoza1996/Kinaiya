"""Read-only local installation, configuration, and connection diagnostics (#392).

Never mutates files, directories, locks, or tokens; never reads or emits secret
values; never performs external network requests or provider model inference.
"""

from __future__ import annotations

import ctypes.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from services.distribution.installation import (
    MIN_BUBBLEWRAP_VERSION,
    MIN_LIBSECCOMP_VERSION,
    SUPPORTED_ROLE_ADAPTERS,
    InstallError,
    Layout,
    _parse_version_tuple,
    _venv_available,
    dependency_install_command,
    generation_receipt,
    installed_package_version,
)

VALID_CHECK_STATUSES = frozenset({"pass", "warning", "fail", "skipped", "unknown"})
MAX_JSON_METADATA_BYTES = 4 * 1024 * 1024
MAX_STRING_LEN = 240
ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
HEX_40_RE = re.compile(r"[0-9a-f]{40}")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

CHECK_ORDER: tuple[str, ...] = (
    "managed_install",
    "sandbox_isolation",
    "layout_and_config",
    "permissions_and_tokens",
    "provider_references",
    "service_reachability",
)


def _bound_str(value: Any, *, max_len: int = MAX_STRING_LEN) -> str:
    if not isinstance(value, str):
        value = str(value)
    cleaned = "".join(ch if (ord(ch) >= 32 and ord(ch) != 127) else " " for ch in value).strip()
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3] + "..."
    return cleaned


def _redact_url(raw_url: Any) -> tuple[str | None, bool, bool, bool]:
    """Return (redacted_url, had_secret_or_query, is_loopback, is_valid_url).

    Strips any embedded username, password, query string, or fragment so secret
    material in malformed URLs can never appear in diagnostic output.
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None, False, False, False
    candidate = raw_url.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in candidate):
        return "<invalid-endpoint-redacted>", True, False, False
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "<invalid-endpoint-redacted>", True, False, False

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in {"http", "https", "ws", "wss"} or not hostname:
        return "<invalid-endpoint-redacted>", ("@" in candidate or "?" in candidate), False, False

    had_secret = bool(
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "@" in (parsed.netloc or "")
        or "?" in candidate
        or "#" in candidate
    )
    try:
        port = parsed.port
    except ValueError:
        return "<invalid-endpoint-redacted>", True, False, False

    host_Part = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host_Part}:{port}" if port is not None else host_Part
    path = parsed.path or ""
    redacted = _bound_str(f"{scheme}://{netloc}{path}", max_len=200)
    is_loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    return redacted, had_secret, is_loopback, True


def _read_json_object_readonly(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read a non-secret JSON metadata file without following symlinks."""
    try:
        if not os.path.lexists(path):
            return None, "missing"
        if path.is_symlink():
            return None, "symlink_refused"
        if not path.is_file():
            return None, "not_regular_file"
        size = path.stat().st_size
        if size > MAX_JSON_METADATA_BYTES:
            return None, "oversized_file"
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, f"read_error: {type(exc).__name__}"

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            return None, f"JSONDecodeError at line {exc.lineno} column {exc.colno}"
        return None, f"parse_error: {type(exc).__name__}"

    if not isinstance(parsed, dict):
        return None, "root_not_object"
    return parsed, None


def _check_managed_install(layout: Layout) -> dict[str, Any]:
    install_root = layout.install
    if os.path.lexists(install_root) and install_root.is_symlink():
        return {
            "status": "fail",
            "summary": _bound_str(f"Managed install root is a symlink alias: {install_root}"),
            "remediation": "Remove the symlink alias and reinstall with `kinaiya install`.",
            "next_step": "kinaiya install",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": None,
        }

    state_path = install_root / "state.json"
    layout_path = install_root / "layout.json"
    pending_path = install_root / "pending.json"
    current_link = install_root / "current"
    generations_dir = install_root / "generations"
    root_manifest_path = install_root / "release-manifest.json"
    root_identity_path = install_root / "release-identity.json"

    has_any_marker = any(
        os.path.lexists(p)
        for p in (
            state_path,
            layout_path,
            pending_path,
            current_link,
            generations_dir,
            root_manifest_path,
            root_identity_path,
        )
    )

    if not os.path.lexists(install_root) or not has_any_marker:
        if install_root.exists() and install_root.is_dir():
            try:
                if any(install_root.iterdir()):
                    return {
                        "status": "fail",
                        "summary": _bound_str(
                            f"Install directory contains unmanaged files without receipt or state.json: {install_root}"
                        ),
                        "remediation": (
                            "Clean up unrelated files in the install root or run `kinaiya install` "
                            "after verifying the directory."
                        ),
                        "next_step": "kinaiya install",
                        "managed": False,
                        "channel": None,
                        "version": None,
                        "source_sha": None,
                        "build_number": None,
                        "current_target": None,
                    }
            except OSError:
                pass
        return {
            "status": "warning",
            "summary": "Unmanaged source checkout (no managed installation receipt found).",
            "remediation": (
                "Run `kinaiya install` for a managed release installation, or continue using "
                "this source checkout."
            ),
            "next_step": "kinaiya install",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": None,
        }

    if os.path.lexists(pending_path):
        return {
            "status": "fail",
            "summary": "Interrupted managed installation or update transaction (pending.json present).",
            "remediation": "Run `kinaiya recover` to reconcile the interrupted transaction.",
            "next_step": "kinaiya recover",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": None,
        }

    state_data: dict[str, Any] | None = None
    if os.path.lexists(state_path):
        state_data, state_err = _read_json_object_readonly(state_path)
        if state_err is not None or not state_data:
            return {
                "status": "fail",
                "summary": _bound_str(f"Corrupted or invalid managed state.json ({state_err})."),
                "remediation": (
                    "Restore valid installation metadata with `kinaiya recover` or reinstall "
                    "via `kinaiya install`."
                ),
                "next_step": "kinaiya recover",
                "managed": False,
                "channel": None,
                "version": None,
                "source_sha": None,
                "build_number": None,
                "current_target": None,
            }
        state_channel = state_data.get("channel")
        state_version = state_data.get("version")
        if state_channel is not None and state_channel not in {"stable", "nightly"}:
            return {
                "status": "fail",
                "summary": "Invalid release channel in managed state.json.",
                "remediation": "Run `kinaiya recover` or reinstall via `kinaiya install`.",
                "next_step": "kinaiya recover",
                "managed": False,
                "channel": None,
                "version": None,
                "source_sha": None,
                "build_number": None,
                "current_target": None,
            }
        if state_version is not None and (
            not isinstance(state_version, str) or not state_version.strip()
        ):
            return {
                "status": "fail",
                "summary": "Invalid version in managed state.json.",
                "remediation": "Run `kinaiya recover` or reinstall via `kinaiya install`.",
                "next_step": "kinaiya recover",
                "managed": False,
                "channel": None,
                "version": None,
                "source_sha": None,
                "build_number": None,
                "current_target": None,
            }

    if os.path.lexists(layout_path):
        layout_data, layout_err = _read_json_object_readonly(layout_path)
        expected_layout = {key: str(value) for key, value in asdict(layout).items()}
        if layout_err is not None or layout_data != expected_layout:
            return {
                "status": "fail",
                "summary": _bound_str(
                    f"Managed layout.json is corrupted or mismatches active layout ({layout_err or 'path_mismatch'})."
                ),
                "remediation": "Preserve existing data and reconcile layout.json or run `kinaiya recover`.",
                "next_step": "kinaiya recover",
                "managed": False,
                "channel": None,
                "version": None,
                "source_sha": None,
                "build_number": None,
                "current_target": None,
            }

    if not os.path.lexists(current_link) or not current_link.is_symlink():
        return {
            "status": "fail",
            "summary": "Missing or non-symlink `current` pointer in managed installation.",
            "remediation": "Run `kinaiya recover` or `kinaiya rollback` to restore the active generation.",
            "next_step": "kinaiya recover",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": None,
        }

    try:
        resolved_current = current_link.resolve(strict=True)
        resolved_install = install_root.resolve(strict=True)
    except OSError:
        return {
            "status": "fail",
            "summary": "Broken `current` symlink in managed installation.",
            "remediation": "Run `kinaiya recover` or `kinaiya rollback` to switch to an intact generation.",
            "next_step": "kinaiya recover",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": None,
        }

    if not resolved_current.is_dir() or not resolved_current.is_relative_to(resolved_install):
        return {
            "status": "fail",
            "summary": "Active `current` symlink points outside managed installation directory.",
            "remediation": "Run `kinaiya recover` or `kinaiya install` to restore a valid generation.",
            "next_step": "kinaiya recover",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": str(resolved_current),
        }

    receipt_path = resolved_current / "receipt.json"
    receipt_release: dict[str, Any] | None = None
    if os.path.lexists(receipt_path):
        try:
            receipt = generation_receipt(layout, resolved_current)
            receipt_release = receipt.get("release") if isinstance(receipt, dict) else None
        except InstallError as exc:
            return {
                "status": "fail",
                "summary": _bound_str(f"Managed generation receipt validation failed: {exc}"),
                "remediation": "Run `kinaiya recover` or `kinaiya rollback` to restore an unmodified generation.",
                "next_step": "kinaiya recover",
                "managed": False,
                "channel": None,
                "version": None,
                "source_sha": None,
                "build_number": None,
                "current_target": str(resolved_current),
            }

    manifest_candidates = [
        resolved_current / "release-manifest.json",
        resolved_current / "source" / "release-manifest.json",
        root_manifest_path,
    ]
    identity_candidates = [
        resolved_current / "release-identity.json",
        resolved_current / "source" / "release-identity.json",
        root_identity_path,
    ]
    if receipt_release is not None and os.path.lexists(resolved_current / "receipt.json"):
        raw_receipt, _ = _read_json_object_readonly(receipt_path)
        if isinstance(raw_receipt, dict) and isinstance(raw_receipt.get("source"), str):
            src_rel = raw_receipt["source"]
            manifest_candidates.insert(1, resolved_current / src_rel / "release-manifest.json")
            identity_candidates.insert(1, resolved_current / src_rel / "release-identity.json")

    manifest_path = next((p for p in manifest_candidates if os.path.lexists(p)), None)
    identity_path = next((p for p in identity_candidates if os.path.lexists(p)), None)

    manifest_data: dict[str, Any] | None = None
    if manifest_path is not None:
        manifest_data, manifest_err = _read_json_object_readonly(manifest_path)
        if manifest_err is not None or not manifest_data:
            return {
                "status": "fail",
                "summary": _bound_str(
                    f"Corrupted release-manifest.json in managed installation ({manifest_err})."
                ),
                "remediation": "Run `kinaiya recover` or reinstall via `kinaiya install`.",
                "next_step": "kinaiya recover",
                "managed": False,
                "channel": None,
                "version": None,
                "source_sha": None,
                "build_number": None,
                "current_target": str(resolved_current),
            }
    elif receipt_release is None or state_data is not None:
        return {
            "status": "fail",
            "summary": "Missing release-manifest.json in managed installation.",
            "remediation": "Restore release-manifest.json or reinstall via `kinaiya install`.",
            "next_step": "kinaiya install",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": str(resolved_current),
        }

    identity_data: dict[str, Any] | None = None
    if identity_path is not None:
        identity_data, identity_err = _read_json_object_readonly(identity_path)
        if identity_err is not None or not identity_data:
            return {
                "status": "fail",
                "summary": _bound_str(
                    f"Corrupted release-identity.json in managed installation ({identity_err})."
                ),
                "remediation": "Run `kinaiya recover` or reinstall via `kinaiya install`.",
                "next_step": "kinaiya recover",
                "managed": False,
                "channel": None,
                "version": None,
                "source_sha": None,
                "build_number": None,
                "current_target": str(resolved_current),
            }
    elif receipt_release is None:
        return {
            "status": "fail",
            "summary": "Missing release-identity.json in managed installation.",
            "remediation": "Restore release-identity.json or reinstall via `kinaiya install`.",
            "next_step": "kinaiya install",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": str(resolved_current),
        }

    primary = manifest_data or receipt_release or state_data or {}
    channel = primary.get("channel")
    version = primary.get("version")
    source_sha = primary.get("source_sha")
    build_number = primary.get("build_number")

    if (
        channel not in {"stable", "nightly"}
        or not isinstance(version, str)
        or not (1 <= len(version) <= 128)
        or (
            source_sha is not None
            and (not isinstance(source_sha, str) or not HEX_40_RE.fullmatch(source_sha))
        )
    ):
        return {
            "status": "fail",
            "summary": "Invalid release metadata fields in managed installation manifest.",
            "remediation": "Run `kinaiya recover` or reinstall via `kinaiya install`.",
            "next_step": "kinaiya install",
            "managed": False,
            "channel": None,
            "version": None,
            "source_sha": None,
            "build_number": None,
            "current_target": str(resolved_current),
        }

    for label, other in (
        ("state.json", state_data),
        ("release-manifest.json", manifest_data),
        ("release-identity.json", identity_data),
        ("receipt.json", receipt_release),
    ):
        if other is None:
            continue
        for key in ("channel", "version", "source_sha", "build_number"):
            if key in other and key in primary and other[key] != primary[key]:
                return {
                    "status": "fail",
                    "summary": _bound_str(f"Managed metadata mismatch for {key!r} in {label}."),
                    "remediation": "Run `kinaiya recover` or reinstall via `kinaiya install`.",
                    "next_step": "kinaiya recover",
                    "managed": False,
                    "channel": None,
                    "version": None,
                    "source_sha": None,
                    "build_number": None,
                    "current_target": str(resolved_current),
                }

    return {
        "status": "pass",
        "summary": _bound_str(f"Managed installation verified ({channel} {version})."),
        "remediation": None,
        "next_step": None,
        "managed": True,
        "channel": _bound_str(channel, max_len=32),
        "version": _bound_str(version, max_len=128),
        "source_sha": _bound_str(source_sha, max_len=40) if isinstance(source_sha, str) else None,
        "build_number": build_number if type(build_number) is int else None,
        "current_target": str(resolved_current),
    }


def _check_sandbox_isolation(*, runner: Any = subprocess.run) -> dict[str, Any]:
    py_major, py_minor = sys.version_info[:2]
    py_version = f"{py_major}.{py_minor}.{sys.version_info[2]}"
    py_bin = shutil.which("python3") or sys.executable
    py_supported = (3, 11) <= (py_major, py_minor) <= (3, 13) and bool(py_bin)
    venv_ok = _venv_available()

    bwrap_bin = shutil.which("bwrap")
    curl_bin = shutil.which("curl")
    sha256sum_bin = shutil.which("sha256sum")
    seccomp_lib = ctypes.util.find_library("seccomp")

    missing_prereqs: list[str] = []
    apt_packages: list[str] = []
    blockers: list[str] = []
    remediations: list[str] = []

    if not py_supported:
        missing_prereqs.append("python3 (3.11-3.13)")
        blockers.append(f"Python {py_major}.{py_minor} is unsupported (require Python 3.11-3.13).")
        remediations.append("Install Python 3.11, 3.12, or 3.13 and set KINAIYA_PYTHON.")
    if not venv_ok:
        venv_pkg = f"python{py_major}.{py_minor}-venv"
        missing_prereqs.append(venv_pkg)
        apt_packages.append(venv_pkg)
        blockers.append(f"Python venv/ensurepip module ({venv_pkg}) is missing.")

    if bwrap_bin is None:
        missing_prereqs.append("bwrap")
        apt_packages.append("bubblewrap")
        blockers.append("Required sandbox binary `bwrap` (bubblewrap) is not installed.")
    else:
        bwrap_ver = installed_package_version("bubblewrap")
        parsed_bwrap = _parse_version_tuple(bwrap_ver)
        if parsed_bwrap is not None and parsed_bwrap < MIN_BUBBLEWRAP_VERSION:
            blockers.append(f"bubblewrap {bwrap_ver} is out of date (require >= 0.6.1).")
            remediations.append("Update the host `bubblewrap` package to >= 0.6.1.")

    if curl_bin is None:
        missing_prereqs.append("curl")
        apt_packages.append("curl")
        blockers.append("Required utility `curl` is not installed.")

    if sha256sum_bin is None:
        missing_prereqs.append("sha256sum")
        apt_packages.append("coreutils")
        blockers.append("Required utility `sha256sum` is not installed.")

    if seccomp_lib is not None:
        seccomp_ver = installed_package_version("libseccomp2")
        parsed_seccomp = _parse_version_tuple(seccomp_ver)
        if parsed_seccomp is not None and parsed_seccomp < MIN_LIBSECCOMP_VERSION:
            blockers.append(f"libseccomp2 {seccomp_ver} is out of date (require >= 2.5).")
            remediations.append("Update the host `libseccomp2` package to >= 2.5.")

    bwrap_probe_status = "skipped"
    if bwrap_bin is not None:
        try:
            probe = runner(
                [
                    bwrap_bin,
                    "--unshare-all",
                    "--die-with-parent",
                    "--new-session",
                    "--ro-bind",
                    "/",
                    "/",
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "--tmpfs",
                    "/tmp",
                    "--clearenv",
                    "/usr/bin/true",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if getattr(probe, "returncode", 1) == 0:
                bwrap_probe_status = "pass"
            else:
                bwrap_probe_status = "fail"
                blockers.append(
                    "bubblewrap unprivileged user namespace probe was denied by the host kernel/AppArmor."
                )
                remediations.append(
                    "Configure host user namespaces (on Ubuntu 24.04 load "
                    "`sudo install -m 0644 docs/examples/apparmor-bwrap /etc/apparmor.d/kinaiya-bwrap && "
                    "sudo apparmor_parser -r /etc/apparmor.d/kinaiya-bwrap`); never disable isolation."
                )
        except (OSError, subprocess.SubprocessError) as exc:
            bwrap_probe_status = "fail"
            blockers.append(f"bubblewrap namespace probe failed ({type(exc).__name__}).")
            remediations.append(
                "Verify `/usr/bin/bwrap` installation and kernel user namespace policy; never disable isolation."
            )

    if apt_packages:
        cmd_str = " ".join(dependency_install_command(apt_packages))
        remediations.insert(0, f"Install missing host packages: `{cmd_str}`")

    if blockers:
        return {
            "status": "fail",
            "summary": _bound_str(blockers[0]),
            "remediation": _bound_str(" ".join(remediations)),
            "next_step": (
                " ".join(dependency_install_command(apt_packages))
                if apt_packages
                else "kinaiya doctor"
            ),
            "python_version": py_version,
            "python_supported": py_supported,
            "bwrap_available": bwrap_bin is not None,
            "bwrap_probe": bwrap_probe_status,
            "curl_available": curl_bin is not None,
            "sha256sum_available": sha256sum_bin is not None,
            "missing_prerequisites": missing_prereqs,
            "blockers": [_bound_str(item) for item in blockers],
        }

    return {
        "status": "pass",
        "summary": "Host prerequisites (python3 3.11-3.13, bwrap namespace probe, curl, sha256sum) passed.",
        "remediation": None,
        "next_step": None,
        "python_version": py_version,
        "python_supported": True,
        "bwrap_available": True,
        "bwrap_probe": "pass",
        "curl_available": True,
        "sha256sum_available": True,
        "missing_prerequisites": [],
        "blockers": [],
    }


def _check_layout_and_config(layout: Layout) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        layout.validate()
    except InstallError as exc:
        return (
            {
                "status": "fail",
                "summary": _bound_str(f"Invalid directory layout: {exc}"),
                "remediation": (
                    "Ensure install, data, config, workspace, cache, and bin directories are "
                    "disjoint absolute paths without symlink aliases."
                ),
                "next_step": "kinaiya setup",
                "config_state": "invalid_layout",
                "config_exists": os.path.lexists(layout.config),
                "layout_valid": False,
            },
            None,
        )

    config_path = layout.config
    if not os.path.lexists(config_path):
        return (
            {
                "status": "warning",
                "summary": _bound_str(f"Configuration file is missing ({config_path})."),
                "remediation": "Run `kinaiya setup` to configure providers and initialize credentials.",
                "next_step": "kinaiya setup",
                "config_state": "not_configured",
                "config_exists": False,
                "layout_valid": True,
            },
            None,
        )

    config_data, config_err = _read_json_object_readonly(config_path)
    if config_err is not None or config_data is None:
        return (
            {
                "status": "fail",
                "summary": _bound_str(f"Corrupted or unreadable config.json ({config_err})."),
                "remediation": (
                    f"Repair the JSON syntax in {config_path} or move the broken file aside "
                    "and rerun `kinaiya setup`."
                ),
                "next_step": "kinaiya setup",
                "config_state": "corrupted",
                "config_exists": True,
                "layout_valid": True,
            },
            None,
        )

    providers = config_data.get("providers")
    if not isinstance(providers, dict) or not isinstance(providers.get("agent"), dict):
        return (
            {
                "status": "fail",
                "summary": "Invalid config.json schema: missing `providers.agent` object.",
                "remediation": "Repair `providers.agent` in config.json or rerun `kinaiya setup`.",
                "next_step": "kinaiya setup",
                "config_state": "invalid_schema",
                "config_exists": True,
                "layout_valid": True,
            },
            None,
        )

    agent_prof = providers["agent"]
    agent_kind = agent_prof.get("kind") or agent_prof.get("adapter")
    agent_url = agent_prof.get("base_url") or agent_prof.get("endpoint")
    agent_model = agent_prof.get("model")
    if (
        not isinstance(agent_kind, str)
        or not agent_kind.strip()
        or not isinstance(agent_url, str)
        or not agent_url.strip()
        or not isinstance(agent_model, str)
        or not agent_model.strip()
    ):
        return (
            {
                "status": "fail",
                "summary": (
                    "Invalid config.json schema: `providers.agent` requires string `kind`, "
                    "`base_url`/`endpoint`, and `model`."
                ),
                "remediation": "Repair `providers.agent` fields in config.json or rerun `kinaiya setup`.",
                "next_step": "kinaiya setup",
                "config_state": "invalid_schema",
                "config_exists": True,
                "layout_valid": True,
            },
            None,
        )

    policy = config_data.get("policy")
    if policy is not None:
        if not isinstance(policy, dict) or any(
            key in policy and type(policy[key]) is not bool
            for key in ("allow_write", "allow_shell", "network", "yolo")
        ):
            return (
                {
                    "status": "fail",
                    "summary": "Invalid config.json schema: `policy` flags must be explicit booleans.",
                    "remediation": "Repair `policy` in config.json or rerun `kinaiya setup`.",
                    "next_step": "kinaiya setup",
                    "config_state": "invalid_schema",
                    "config_exists": True,
                    "layout_valid": True,
                },
                None,
            )

    return (
        {
            "status": "pass",
            "summary": "Layout directories are disjoint and config.json schema is valid.",
            "remediation": None,
            "next_step": None,
            "config_state": "valid",
            "config_exists": True,
            "layout_valid": True,
        },
        config_data,
    )


def _inspect_private_file(path: Path) -> dict[str, Any]:
    """Inspect only file metadata (lstat/stat); never open or read secret contents."""
    if not os.path.lexists(path):
        return {
            "path": str(path),
            "exists": False,
            "is_symlink": False,
            "mode": None,
            "status": "missing",
        }
    is_link = path.is_symlink()
    if is_link:
        return {
            "path": str(path),
            "exists": True,
            "is_symlink": True,
            "mode": None,
            "status": "fail",
        }
    try:
        st = path.stat()
    except OSError:
        return {
            "path": str(path),
            "exists": True,
            "is_symlink": False,
            "mode": None,
            "status": "fail",
        }
    if not stat.S_ISREG(st.st_mode):
        return {
            "path": str(path),
            "exists": True,
            "is_symlink": False,
            "mode": None,
            "status": "fail",
        }
    imode = stat.S_IMODE(st.st_mode)
    mode_str = f"{imode:04o}"
    if imode != 0o600:
        return {
            "path": str(path),
            "exists": True,
            "is_symlink": False,
            "mode": mode_str,
            "status": "fail",
        }
    return {
        "path": str(path),
        "exists": True,
        "is_symlink": False,
        "mode": mode_str,
        "status": "pass",
    }


def _check_permissions_and_tokens(layout: Layout, *, config_exists: bool) -> dict[str, Any]:
    tokens_path = layout.data / "service-tokens.json"
    tokens_info = _inspect_private_file(tokens_path)

    dotenv_candidates = [
        layout.config.parent / ".env",
        layout.data / ".env",
        layout.workspace / ".env",
    ]
    dotenv_reports: list[dict[str, Any]] = []
    seen_dotenv: set[str] = set()
    for candidate in dotenv_candidates:
        key = str(candidate)
        if key in seen_dotenv:
            continue
        seen_dotenv.add(key)
        if os.path.lexists(candidate):
            dotenv_reports.append(_inspect_private_file(candidate))

    failures: list[str] = []
    remediations: list[str] = []

    if tokens_info["status"] == "fail":
        if tokens_info["is_symlink"]:
            failures.append(f"service-tokens.json is a symlink alias ({tokens_path}).")
        else:
            failures.append(
                f"service-tokens.json has insecure permissions {tokens_info['mode']} "
                f"(require 0600): {tokens_path}."
            )
        remediations.append(
            f"Run `chmod 600 {tokens_path}` and rotate credentials via `kinaiya auth rotate-owner`."
        )

    for dotenv in dotenv_reports:
        if dotenv["status"] == "fail":
            if dotenv["is_symlink"]:
                failures.append(f".env file is a symlink alias ({dotenv['path']}).")
            else:
                failures.append(
                    f".env file has insecure permissions {dotenv['mode']} "
                    f"(require 0600): {dotenv['path']}."
                )
            remediations.append(
                f"Run `chmod 600 {dotenv['path']}` and rotate exposed credentials via `kinaiya auth rotate-owner`."
            )

    if failures:
        return {
            "status": "fail",
            "summary": _bound_str(failures[0]),
            "remediation": _bound_str(" ".join(remediations)),
            "next_step": f"chmod 600 {tokens_path}; kinaiya auth rotate-owner",
            "service_tokens": tokens_info,
            "dotenv_files": dotenv_reports,
        }

    if not tokens_info["exists"]:
        return {
            "status": "warning",
            "summary": _bound_str(
                f"Private service credential file is missing ({tokens_path})."
                if config_exists
                else "service-tokens.json not yet initialized; run `kinaiya setup`."
            ),
            "remediation": ("Run `kinaiya setup` to generate mode-0600 service tokens."),
            "next_step": "kinaiya setup",
            "service_tokens": tokens_info,
            "dotenv_files": dotenv_reports,
        }

    return {
        "status": "pass",
        "summary": "Private credential files have strict 0600 permissions.",
        "remediation": None,
        "next_step": None,
        "service_tokens": tokens_info,
        "dotenv_files": dotenv_reports,
    }


def _check_provider_references(
    config: dict[str, Any] | None,
    *,
    env: Mapping[str, str],
) -> dict[str, Any]:
    if config is None:
        return {
            "status": "skipped",
            "summary": "Provider inspection skipped because config.json is not available.",
            "remediation": "Run `kinaiya setup` to configure provider adapters.",
            "next_step": "kinaiya setup",
            "agent_supports_complete_measurement": None,
            "roles": {},
        }

    providers = config.get("providers")
    if not isinstance(providers, dict):
        return {
            "status": "fail",
            "summary": "Invalid `providers` mapping in config.json.",
            "remediation": "Repair config.json or run `kinaiya setup`.",
            "next_step": "kinaiya setup",
            "agent_supports_complete_measurement": False,
            "roles": {},
        }

    dist = config.get("distribution")
    converse_enabled = (
        bool(dist.get("converse"))
        if isinstance(dist, dict) and "converse" in dist
        else ("converse" in providers)
    )

    roles_report: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    warnings: list[str] = []
    remediations: list[str] = []
    agent_measurement: bool | None = None

    for role in ("agent", "converse", "decisions"):
        if role not in providers and role != "agent":
            continue
        raw_profile = providers.get(role)
        role_enabled = True if role == "agent" else converse_enabled
        if not isinstance(raw_profile, dict):
            if role_enabled:
                failures.append(f"Provider role {role!r} is missing or not an object.")
                remediations.append(f"Configure `providers.{role}` via `kinaiya setup`.")
            continue

        raw_kind = raw_profile.get("kind") or raw_profile.get("adapter")
        kind = _bound_str(raw_kind, max_len=40) if isinstance(raw_kind, str) else None
        raw_endpoint = raw_profile.get("base_url") or raw_profile.get("endpoint")
        redacted_url, had_url_secret, is_loopback, url_valid = _redact_url(raw_endpoint)
        raw_model = raw_profile.get("model")
        model = _bound_str(raw_model, max_len=200) if isinstance(raw_model, str) else None
        allow_cloud = raw_profile.get("allow_cloud") is True

        raw_cred_env = (
            raw_profile.get("credential_env")
            if "credential_env" in raw_profile
            else raw_profile.get("api_key_env")
        )
        cred_env: str | None = None
        cred_present: bool | None = None
        role_status = "pass"

        if any(k in raw_profile for k in ("api_key", "authorization", "headers", "bearer_token")):
            role_status = "fail"
            failures.append(
                f"Role {role!r} contains a raw secret/header field in config.json (forbidden)."
            )
            remediations.append(
                f"Remove raw secret fields from `providers.{role}` in config.json, rotate the key, "
                "and reference an environment variable name via `credential_env` / `api_key_env`."
            )

        if raw_cred_env is not None:
            if not isinstance(raw_cred_env, str) or not ENV_NAME_RE.fullmatch(raw_cred_env):
                cred_env = "<invalid-env-name-redacted>"
                cred_present = False
                role_status = "fail"
                failures.append(
                    f"Role {role!r} has an invalid `credential_env` name (must be an uppercase env var name, never a secret)."
                )
                remediations.append(
                    f"Replace `credential_env` in `providers.{role}` with an environment variable NAME (e.g. `OPENAI_API_KEY`)."
                )
            else:
                cred_env = raw_cred_env
                cred_present = bool(env.get(cred_env))
                if not cred_present:
                    if role_status == "pass":
                        role_status = "warning"
                    warnings.append(
                        f"Environment variable {cred_env} for role {role!r} is not set."
                    )
                    remediations.append(
                        f"Export `{cred_env}` in the environment before starting Kinaiya services."
                    )

        supports_measurement = kind in {"lmstudio", "meta"} if role == "agent" else None
        if role == "agent":
            agent_measurement = bool(supports_measurement)
            if kind == "compatible" or not supports_measurement:
                role_status = "fail"
                failures.append(
                    f"Agent adapter {kind!r} does not support complete-request measurement (`prepare_complete`)."
                )
                remediations.append(
                    "Select `'lmstudio'` for local native loopback or `'meta'` with `allow_cloud=True` for Agent."
                )
        elif role_enabled and kind not in SUPPORTED_ROLE_ADAPTERS.get(role, ()):
            role_status = "fail"
            failures.append(f"Unsupported {role} adapter kind {kind!r}.")
            remediations.append(
                f"Select a supported {role} adapter ({', '.join(SUPPORTED_ROLE_ADAPTERS.get(role, ()))})."
            )

        if had_url_secret:
            if role_status == "pass":
                role_status = "warning"
            warnings.append(
                f"Redacted embedded userinfo/query parameters from {role!r} endpoint URL."
            )
            remediations.append(
                f"Remove embedded credentials or query strings from `providers.{role}.base_url`."
            )

        if not url_valid:
            if role_enabled:
                role_status = "fail"
                failures.append(f"Role {role!r} has an invalid endpoint URL.")
                remediations.append(
                    f"Configure a valid HTTP/HTTPS endpoint for `providers.{role}`."
                )
        elif role_enabled:
            if not is_loopback and not allow_cloud:
                role_status = "fail"
                failures.append(
                    f"Role {role!r} targets non-loopback endpoint {redacted_url} without `allow_cloud=True`."
                )
                remediations.append(
                    f"Use a loopback endpoint for {role!r} or explicitly enable `allow_cloud=True` over HTTPS."
                )
            if kind == "lmstudio" and (allow_cloud or not is_loopback):
                role_status = "fail"
                failures.append(
                    f"Role {role!r} uses `'lmstudio'` adapter with cloud/non-loopback settings."
                )
                remediations.append(
                    "Use `'lmstudio'` only with local loopback (`allow_cloud=False`), or switch to `'meta'`."
                )
            if kind == "meta" and not allow_cloud:
                role_status = "fail"
                failures.append(f"Role {role!r} uses `'meta'` adapter without `allow_cloud=True`.")
                remediations.append(
                    f"Set `allow_cloud=True` for `providers.{role}` or use `'lmstudio'`."
                )
            if kind == "meta" and cred_env is None and raw_profile.get("secret_ref") != "muse":
                role_status = "fail"
                failures.append(
                    f"Role {role!r} uses `'meta'` adapter without a `credential_env` reference."
                )
                remediations.append(
                    f"Configure `api_key_env` / `credential_env` for `providers.{role}`."
                )

        if role_enabled and (not model or model == "configure-your-local-model"):
            if role_status == "pass":
                role_status = "warning"
            warnings.append(f"Role {role!r} still uses placeholder model {model!r}.")
            remediations.append("Run `kinaiya setup` to select an explicit model name.")

        roles_report[role] = {
            "enabled": role_enabled,
            "kind": kind,
            "endpoint": redacted_url,
            "redacted_credentials": had_url_secret,
            "model": model,
            "allow_cloud": allow_cloud,
            "credential_env": cred_env,
            "credential_present": cred_present,
            "credential": {
                "env": cred_env,
                "present": cred_present,
            },
            "supports_complete_measurement": supports_measurement,
            "status": role_status,
        }

    if failures:
        return {
            "status": "fail",
            "summary": _bound_str(failures[0]),
            "remediation": _bound_str(" ".join(dict.fromkeys(remediations))),
            "next_step": "kinaiya setup",
            "agent_supports_complete_measurement": agent_measurement,
            "roles": roles_report,
        }
    if warnings:
        return {
            "status": "warning",
            "summary": _bound_str(warnings[0]),
            "remediation": _bound_str(" ".join(dict.fromkeys(remediations))),
            "next_step": "kinaiya setup" if "placeholder" in warnings[0] else None,
            "agent_supports_complete_measurement": agent_measurement,
            "roles": roles_report,
        }

    return {
        "status": "pass",
        "summary": "Provider adapters, endpoints, and credential references are valid.",
        "remediation": None,
        "next_step": None,
        "agent_supports_complete_measurement": agent_measurement,
        "roles": roles_report,
    }


def _extract_port(config: dict[str, Any] | None, port_key: str, url_key: str, default: int) -> int:
    if isinstance(config, dict):
        raw_port = config.get(port_key)
        if type(raw_port) is int and 1 <= raw_port <= 65535:
            return raw_port
        raw_url = config.get(url_key)
        if isinstance(raw_url, str):
            try:
                parsed = urlsplit(raw_url)
                if parsed.port is not None and 1 <= parsed.port <= 65535:
                    return parsed.port
            except ValueError:
                pass
    return default


def _probe_loopback_port(port: int) -> bool:
    if not (1 <= port <= 65535):
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _check_service_reachability(
    config: dict[str, Any] | None,
    *,
    probe_services: bool,
) -> dict[str, Any]:
    session_port = _extract_port(config, "session_port", "session_url", 8760)
    agent_port = _extract_port(config, "agent_port", "agent_url", 8761)

    converse_enabled = False
    if isinstance(config, dict):
        dist = config.get("distribution")
        if isinstance(dist, dict) and dist.get("converse") is True:
            converse_enabled = True
        elif (
            config.get("converse_enabled") is True
            or "converse_port" in config
            or "converse_url" in config
        ):
            converse_enabled = True

    converse_port: int | None = (
        _extract_port(config, "converse_port", "converse_url", 8762) if converse_enabled else None
    )

    target_ports: list[tuple[str, int]] = [
        ("session", session_port),
        ("agent", agent_port),
    ]
    if converse_port is not None:
        target_ports.append(("converse", converse_port))

    if not probe_services:
        services_skipped = {
            name: {
                "host": "127.0.0.1",
                "port": port,
                "listening": None,
                "status": "skipped",
            }
            for name, port in target_ports
        }
        return {
            "status": "skipped",
            "state": "skipped",
            "summary": "Local loopback TCP listener probe skipped (--no-service-probe).",
            "remediation": "Run `kinaiya doctor` without `--no-service-probe` or start services with `kinaiya serve host`.",
            "next_step": "kinaiya serve host",
            "probe_services": False,
            "session_port": session_port,
            "agent_port": agent_port,
            "converse_port": converse_port,
            "services": services_skipped,
            "provider_connection": "unknown",
            "provider_connection_status": "skipped",
        }

    services_report: dict[str, dict[str, Any]] = {}
    offline: list[str] = []
    for name, port in target_ports:
        listening = _probe_loopback_port(port)
        services_report[name] = {
            "host": "127.0.0.1",
            "port": port,
            "listening": listening,
            "status": "pass" if listening else "offline",
        }
        if not listening:
            offline.append(f"{name}:{port}")

    if offline:
        return {
            "status": "warning",
            "state": "offline",
            "summary": _bound_str(f"Local loopback services are offline ({', '.join(offline)})."),
            "remediation": "Start local services with `kinaiya serve host`.",
            "next_step": "kinaiya serve host",
            "probe_services": True,
            "session_port": session_port,
            "agent_port": agent_port,
            "converse_port": converse_port,
            "services": services_report,
            "provider_connection": "unknown",
            "provider_connection_status": "skipped",
        }

    return {
        "status": "pass",
        "state": "listening",
        "summary": "Configured local loopback services are listening.",
        "remediation": None,
        "next_step": None,
        "probe_services": True,
        "session_port": session_port,
        "agent_port": agent_port,
        "converse_port": converse_port,
        "services": services_report,
        "provider_connection": "unknown",
        "provider_connection_status": "skipped",
    }


def run_doctor(
    layout: Layout,
    *,
    probe_services: bool = True,
    env: Mapping[str, str] | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Run read-only local installation, configuration, and loopback diagnostics."""
    effective_env: Mapping[str, str] = os.environ if env is None else env

    managed_install = _check_managed_install(layout)
    sandbox_isolation = _check_sandbox_isolation(runner=runner)
    layout_and_config, parsed_config = _check_layout_and_config(layout)
    permissions_and_tokens = _check_permissions_and_tokens(
        layout, config_exists=layout_and_config["config_exists"]
    )
    provider_references = _check_provider_references(parsed_config, env=effective_env)
    service_reachability = _check_service_reachability(parsed_config, probe_services=probe_services)

    checks: dict[str, dict[str, Any]] = {
        "managed_install": managed_install,
        "sandbox_isolation": sandbox_isolation,
        "layout_and_config": layout_and_config,
        "permissions_and_tokens": permissions_and_tokens,
        "provider_references": provider_references,
        "service_reachability": service_reachability,
    }

    for check in checks.values():
        assert check["status"] in VALID_CHECK_STATUSES

    statuses = [checks[name]["status"] for name in CHECK_ORDER]
    if "fail" in statuses:
        overall_status = "fail"
    elif "warning" in statuses or layout_and_config["config_state"] != "valid":
        overall_status = "warning"
    else:
        overall_status = "pass"

    ready = bool(
        overall_status != "fail"
        and layout_and_config["status"] == "pass"
        and sandbox_isolation["status"] == "pass"
        and permissions_and_tokens["status"] == "pass"
        and provider_references["status"] == "pass"
        and service_reachability["status"] == "pass"
    )

    overall_next_step: str | None = None
    overall_remediation: str | None = None
    overall_summary = "All local installation, configuration, and loopback service checks passed."

    for priority in ("fail", "warning", "skipped"):
        for name in CHECK_ORDER:
            chk = checks[name]
            if chk["status"] == priority:
                if overall_summary.startswith("All local"):
                    overall_summary = _bound_str(f"{name}: {chk['summary']}")
                if overall_remediation is None and chk.get("remediation"):
                    overall_remediation = _bound_str(chk["remediation"])
                if overall_next_step is None and chk.get("next_step"):
                    overall_next_step = _bound_str(chk["next_step"], max_len=120)

    return {
        "schema_version": 1,
        "command": "doctor",
        "read_only": True,
        "status": overall_status,
        "ready": ready,
        "provider_connection": "unknown",
        "summary": overall_summary,
        "remediation": overall_remediation,
        "next_step": overall_next_step,
        "layout": {
            "install": str(layout.install),
            "data": str(layout.data),
            "config": str(layout.config),
            "workspace": str(layout.workspace),
            "cache": str(layout.cache),
            "bin": str(layout.bin),
        },
        "checks": checks,
    }


def format_doctor_report(
    report: Mapping[str, Any],
    *,
    no_color: bool = False,
    is_tty: bool = False,
    width: int = 80,
) -> str:
    """Format a doctor report as a human-readable summary or plain text."""
    env_no_color = bool(os.environ.get("NO_COLOR"))
    use_color = bool(is_tty and not no_color and not env_no_color)
    bounded_width = max(40, min(int(width) if isinstance(width, int) else 80, 120))

    palette = {
        "pass": "\x1b[32m",
        "warning": "\x1b[33m",
        "fail": "\x1b[31m",
        "skipped": "\x1b[36m",
        "unknown": "\x1b[35m",
        "bold": "\x1b[1m",
        "reset": "\x1b[0m",
    }

    def badge(status: str) -> str:
        label = {
            "pass": "PASS",
            "warning": "WARN",
            "fail": "FAIL",
            "skipped": "SKIP",
            "unknown": "UNKN",
        }.get(status, "UNKN")
        raw = f"[{label}]"
        if not use_color:
            return raw
        color = palette.get(status, "")
        return f"{color}{raw}{palette['reset']}"

    lines: list[str] = []
    title = "Kinaiya Doctor (read-only local diagnostics)"
    if use_color:
        lines.append(f"{palette['bold']}{title}{palette['reset']}")
    else:
        lines.append(title)
    lines.append("=" * min(len(title), bounded_width))

    overall_status = str(report.get("status", "unknown"))
    ready = bool(report.get("ready", False))
    provider_conn = _bound_str(report.get("provider_connection", "unknown"), max_len=40)
    lines.append(
        f"Overall: {badge(overall_status)} status={overall_status} "
        f"ready={str(ready).lower()} provider_connection={provider_conn}"
    )

    checks = report.get("checks")
    if isinstance(checks, Mapping):
        lines.append("")
        lines.append("Checks:")
        for name in CHECK_ORDER:
            chk = checks.get(name)
            if not isinstance(chk, Mapping):
                continue
            st = str(chk.get("status", "unknown"))
            summary = _bound_str(chk.get("summary", ""), max_len=bounded_width + 120)
            lines.append(f"  {badge(st)} {name}: {summary}")

            if name == "provider_references":
                roles = chk.get("roles")
                if isinstance(roles, Mapping):
                    for role_name, role_info in roles.items():
                        if not isinstance(role_info, Mapping):
                            continue
                        r_kind = _bound_str(role_info.get("kind") or "unset", max_len=32)
                        r_end = _bound_str(role_info.get("endpoint") or "unset", max_len=120)
                        r_mod = _bound_str(role_info.get("model") or "unset", max_len=80)
                        r_cloud = bool(role_info.get("allow_cloud", False))
                        r_env = role_info.get("credential_env")
                        r_pres = role_info.get("credential_present")
                        cred_desc = (
                            f"credential_env={_bound_str(r_env, max_len=64)} "
                            f"(present={str(bool(r_pres)).lower()})"
                            if r_env
                            else "credential_env=none"
                        )
                        lines.append(
                            f"         role={role_name} kind={r_kind} endpoint={r_end} "
                            f"model={r_mod} allow_cloud={str(r_cloud).lower()} {cred_desc}"
                        )

            remediation = chk.get("remediation")
            if isinstance(remediation, str) and remediation.strip():
                lines.append(f"         remediation: {_bound_str(remediation, max_len=200)}")
            next_step = chk.get("next_step")
            if isinstance(next_step, str) and next_step.strip():
                lines.append(f"         next_step: {_bound_str(next_step, max_len=120)}")

    top_next = report.get("next_step")
    if isinstance(top_next, str) and top_next.strip():
        lines.append("")
        lines.append(f"Next step: {_bound_str(top_next, max_len=120)}")

    output = "\n".join(lines) + "\n"
    if not use_color:
        output = ANSI_ESCAPE_RE.sub("", output)
    return output
