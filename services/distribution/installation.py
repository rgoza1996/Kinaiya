"""Conservative per-user Linux installation. No runtime dependencies at import time."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import uuid
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

MIN_BUBBLEWRAP_VERSION = (0, 6, 1)
MIN_LIBSECCOMP_VERSION = (2, 5)


def _parse_version_tuple(raw: str | None) -> tuple[int, ...] | None:
    if not raw or not isinstance(raw, str):
        return None
    match = re.search(r"(\d+(?:\.\d+)+)", raw)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def installed_package_version(
    package: str, status_path: Path = Path("/var/lib/dpkg/status")
) -> str | None:
    try:
        text = status_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for stanza in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            if ":" in line and not line[:1].isspace():
                key, val = line.split(":", 1)
                fields[key.strip()] = val.strip()
        if fields.get("Package") == package and "installed" in fields.get("Status", "").split():
            return fields.get("Version")
    return None


def _venv_available() -> bool:
    try:
        import ensurepip  # noqa: F401
        import venv  # noqa: F401
    except ImportError:
        return False
    return True


def missing_system_dependencies() -> list[str]:
    import ctypes.util

    missing: list[str] = []
    if shutil.which("bwrap") is None:
        missing.append("bubblewrap")
    if ctypes.util.find_library("seccomp") is None:
        missing.append("libseccomp2")
    if not _venv_available():
        missing.append(f"python{sys.version_info[0]}.{sys.version_info[1]}-venv")
    return missing


def dependency_install_command(missing: list[str] | tuple[str, ...]) -> list[str]:
    return ["sudo", "apt-get", "install", "--no-upgrade", "-y", *missing]


def install_missing_dependencies(missing: list[str] | tuple[str, ...]) -> list[str]:
    """Install missing system packages without updating or upgrading existing packages."""
    from services.distribution.stage_progress import current_reporter, redact_install_log

    if not missing:
        return []
    for pkg in missing:
        if not isinstance(pkg, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", pkg):
            raise InstallError(f"Refusing invalid system dependency name: {pkg!r}")
    cmd = dependency_install_command(missing)
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    reporter = current_reporter()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        if reporter is not None:
            reporter.log_subprocess("dependencies", cmd, 1, None, str(exc))
        raise InstallError(
            f"Failed to install missing dependencies ({', '.join(missing)}) via `{' '.join(cmd)}`: {redact_install_log(str(exc))}"
        ) from None
    if reporter is not None:
        reporter.log_subprocess(
            "dependencies",
            cmd,
            result.returncode,
            getattr(result, "stdout", None),
            getattr(result, "stderr", None),
        )
    if result.returncode != 0:
        detail = redact_install_log(
            (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        )
        suffix = f" ({detail.splitlines()[-1]})" if detail else ""
        raise InstallError(
            f"Out-of-date system blocker: could not install missing dependencies ({', '.join(missing)}) "
            f"with `--no-upgrade`{suffix}. An existing installed package or your local apt package index "
            "is out of date. Please update that specific package or refresh your package index manually "
            "first; Kinaiya never runs `sudo apt-get update` or `sudo apt-get upgrade` for you."
        )
    return list(missing)


def check_outdated_system_blockers(*, probe_subprocesses: bool = False) -> None:
    """Warn the user if an existing host component is out of date and blocks installation."""
    import ctypes.util
    import platform

    py_major, py_minor = sys.version_info[:2]
    if (py_major, py_minor) < (3, 11):
        raise InstallError(
            f"Out-of-date system blocker: Python {py_major}.{py_minor} is out of date "
            "(Kinaiya requires Python 3.11-3.13). Please update Python first; "
            "the installer will not update existing system packages for you."
        )
    if platform.system() != "Linux":
        return
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    distro_id = release.get("ID")
    version_id = release.get("VERSION_ID", "")
    if distro_id == "ubuntu" and version_id not in {"22.04", "24.04"}:
        parsed_ubuntu = _parse_version_tuple(version_id)
        if parsed_ubuntu is not None and parsed_ubuntu < (22, 4):
            raise InstallError(
                f"Out-of-date system blocker: Ubuntu {version_id} is out of date "
                "(Kinaiya requires Ubuntu 22.04 or 24.04). Please update Ubuntu first; "
                "the installer will not update your system for you."
            )
    bwrap_bin = shutil.which("bwrap")
    if bwrap_bin is not None:
        bwrap_ver = installed_package_version("bubblewrap")
        if bwrap_ver is None and probe_subprocesses:
            try:
                ver_probe = subprocess.run(
                    [bwrap_bin, "--version"], capture_output=True, text=True, timeout=5
                )
                bwrap_ver = getattr(ver_probe, "stdout", None)
            except (OSError, subprocess.SubprocessError):
                bwrap_ver = None
        parsed_bwrap = _parse_version_tuple(bwrap_ver)
        if parsed_bwrap is not None and parsed_bwrap < MIN_BUBBLEWRAP_VERSION:
            raise InstallError(
                f"Out-of-date system blocker: bubblewrap {bwrap_ver.strip()} is out of date "
                "(Kinaiya requires bubblewrap >= 0.6.1). Please update bubblewrap first; "
                "the installer will not update existing system packages for you."
            )
    if ctypes.util.find_library("seccomp") is not None:
        seccomp_ver = installed_package_version("libseccomp2")
        parsed_seccomp = _parse_version_tuple(seccomp_ver)
        if parsed_seccomp is not None and parsed_seccomp < MIN_LIBSECCOMP_VERSION:
            raise InstallError(
                f"Out-of-date system blocker: libseccomp2 {seccomp_ver.strip()} is out of date "
                "(Kinaiya requires libseccomp2 >= 2.5). Please update libseccomp2 first; "
                "the installer will not update existing system packages for you."
            )


def preflight(layout, *, allow_missing_dependencies: bool = False):
    import platform

    layout.validate()
    if platform.system() != "Linux":
        raise InstallError("Native Linux required; Windows/macOS/WSL are not qualified")
    if os.geteuid() == 0:
        raise InstallError(
            "Do not run the installer as root; the CLI invokes sudo only for missing dependencies"
        )
    check_outdated_system_blockers(probe_subprocesses=True)
    release = platform.freedesktop_os_release()
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") not in {"22.04", "24.04"}:
        raise InstallError(
            "Initial candidate baseline is Ubuntu 22.04/24.04 only; see docs/linux-installation.md"
        )
    if platform.machine() not in {"x86_64", "aarch64"} or not (3, 11) <= sys.version_info[:2] <= (
        3,
        13,
    ):
        raise InstallError("Require x86_64/aarch64 and Python 3.11-3.13 with venv/ensurepip")
    missing = missing_system_dependencies()
    if missing and not allow_missing_dependencies:
        cmd = " ".join(dependency_install_command(missing))
        raise InstallError(
            f"Missing required system dependencies: {', '.join(missing)}. "
            f"Run `kinaiya install` or `{cmd}` (never runs apt-get update/upgrade)"
        )
    ancestor = next(p for p in [layout.install, *layout.install.parents] if p.exists())
    if shutil.disk_usage(ancestor).free < 1024**3:
        raise InstallError(
            "Insufficient disk: at least 1 GiB free is required for Agent-only staging"
        )
    binary = shutil.which("bwrap")
    if binary is not None:
        try:
            probe = subprocess.run(
                [
                    binary,
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
                timeout=15,
            )
            if probe.returncode:
                raise InstallError(
                    "bubblewrap namespace probe denied; configure host isolation, never disable it"
                )
        except (OSError, subprocess.SubprocessError):
            raise InstallError(
                "bubblewrap namespace probe failed; verify kernel/user namespace policy"
            ) from None
    result: dict[str, Any] = {
        "platform": "linux",
        "architecture": platform.machine(),
        "distro": release.get("VERSION_ID"),
        "python": platform.python_version(),
        "sandbox_probe": "passed" if binary is not None else "pending_dependencies",
    }
    if missing:
        result["missing_dependencies"] = missing
    return result


SUPPORTED_ROLE_ADAPTERS: dict[str, tuple[str, ...]] = {
    "agent": ("lmstudio", "meta"),
    "converse": ("lmstudio", "compatible", "meta"),
    "decisions": ("compatible", "laya", "native_d1", "meta"),
}


def _build_role_profile(
    role: str,
    *,
    endpoint: Any,
    model: Any,
    kind: Any = None,
    allow_cloud: Any = False,
    credential_env: Any = None,
    context_tokens: Any = None,
    max_tokens: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from services.launcher import validate_setup_provider

    if role not in SUPPORTED_ROLE_ADAPTERS:
        raise InstallError(f"Unsupported provider role {role!r}")
    if extra and "api_key" in extra:
        raise InstallError("Credentials accept an environment variable NAME only, never an API key")
    if not isinstance(endpoint, str) or not endpoint or any(c.isspace() for c in endpoint):
        raise InstallError(
            "Use a credential-free loopback endpoint, or HTTPS with explicit cloud disclosure"
        )
    if type(allow_cloud) is not bool:
        raise InstallError("Cloud disclosure must be an explicit boolean")
    url = urlsplit(endpoint)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        url.scheme not in {"http", "https", "ws", "wss"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or (not local and (not allow_cloud or url.scheme not in {"https", "wss"}))
    ):
        raise InstallError(
            "Use a credential-free loopback endpoint, or HTTPS with explicit cloud disclosure"
        )
    if not isinstance(model, str) or not 1 <= len(model) <= 200 or any(ord(c) < 32 for c in model):
        raise InstallError("Select an explicit bounded model name")
    if credential_env is not None and (
        not isinstance(credential_env, str)
        or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", credential_env)
    ):
        raise InstallError("Credentials accept an environment variable NAME only, never an API key")

    # Never infer provider kind from the model name; adapter selection is explicit
    # or derived strictly from the role and local/cloud endpoint mode.
    if kind is None:
        selected_kind = ("meta" if allow_cloud else "lmstudio") if role == "agent" else "compatible"
    else:
        if not isinstance(kind, str):
            raise InstallError(f"Adapter kind for {role} must be a string")
        selected_kind = kind

    if selected_kind not in SUPPORTED_ROLE_ADAPTERS[role]:
        if role == "agent" and selected_kind == "compatible":
            raise InstallError(
                "Adapter 'compatible' does not implement exact complete-request measurement "
                "(prepare_complete) required by Agent; select 'lmstudio' for local native "
                "loopback or 'meta' with explicit cloud disclosure"
            )
        raise InstallError(
            f"Unsupported {role} adapter kind {selected_kind!r}; supported {role} adapters: "
            f"{', '.join(SUPPORTED_ROLE_ADAPTERS[role])}"
        )

    if selected_kind != "lmstudio" and url.scheme not in {"http", "https"}:
        raise InstallError(
            "Use a credential-free loopback endpoint, or HTTPS with explicit cloud disclosure"
        )

    if selected_kind == "lmstudio":
        if allow_cloud or not local:
            raise InstallError(
                "Native LM Studio adapter supports local loopback only (allow_cloud=False); "
                "select 'meta' for cloud endpoints"
            )
        if credential_env is not None:
            raise InstallError(
                "Native LM Studio loopback adapter does not use API key credentials; "
                "omit credential_env or select 'meta' with allow_cloud"
            )
        profile: dict[str, Any] = {
            "kind": "lmstudio",
            "base_url": endpoint,
            "model": model,
            "allow_cloud": False,
            "max_tokens": 128 if max_tokens is None else max_tokens,
            "reasoning_effort": "none",
        }
    elif selected_kind == "meta":
        if not allow_cloud:
            raise InstallError(
                "Adapter 'meta' requires explicit cloud disclosure (allow_cloud=True)"
            )
        secret_ref = extra.get("secret_ref") if extra else None
        if not credential_env and secret_ref != "muse":
            raise InstallError(
                "Adapter 'meta' requires an explicit credential_env environment variable name"
            )
        profile = {
            "kind": "meta",
            "base_url": endpoint,
            "model": model,
            "allow_cloud": True,
            "max_tokens": 2048 if max_tokens is None else max_tokens,
            "context_tokens": 32768 if context_tokens is None else context_tokens,
        }
        if role == "agent":
            profile["structured_actions"] = True
            profile["action_format"] = extra.get("action_format", "flat-v1") if extra else "flat-v1"
        if credential_env:
            profile["api_key_env"] = credential_env
        if secret_ref is not None:
            profile["secret_ref"] = secret_ref
    elif selected_kind in {"laya", "native_d1"}:
        if allow_cloud or not local:
            raise InstallError(
                f"Adapter {selected_kind!r} supports local loopback Decisions endpoints only"
            )
        profile = {
            "kind": selected_kind,
            "base_url": endpoint,
            "model": model,
        }
        if credential_env:
            profile["api_key_env"] = credential_env
        if selected_kind == "native_d1":
            profile["min_confidence"] = (
                extra.get("min_confidence", 0.6)
                if extra and extra.get("min_confidence") is not None
                else 0.6
            )
        if extra and "expected_artifact_sha256" in extra:
            profile["expected_artifact_sha256"] = extra["expected_artifact_sha256"]
    else:
        profile = {
            "kind": selected_kind,
            "base_url": endpoint,
            "model": model,
            "allow_cloud": allow_cloud,
            "max_tokens": 2048 if max_tokens is None else max_tokens,
        }
        if credential_env:
            profile["api_key_env"] = credential_env
        if extra and "reasoning_effort" in extra:
            profile["reasoning_effort"] = extra["reasoning_effort"]

    try:
        validate_setup_provider(role, profile)
    except (ValueError, RuntimeError, TypeError, KeyError) as exc:
        raise InstallError(str(exc)) from None
    return profile


def _build_role_from_mapping(role: str, spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise InstallError(f"Role configuration for {role} must be a mapping")
    spec_kind = spec.get("adapter") if "adapter" in spec else spec.get("kind")
    if "adapter" in spec and "kind" in spec and spec["adapter"] != spec["kind"]:
        raise InstallError(f"Conflicting adapter/kind in {role} role configuration")
    spec_endpoint = spec.get("endpoint") if "endpoint" in spec else spec.get("base_url")
    spec_cred = spec.get("credential_env") if "credential_env" in spec else spec.get("api_key_env")
    return _build_role_profile(
        role,
        endpoint=spec_endpoint,
        model=spec.get("model"),
        kind=spec_kind,
        allow_cloud=spec.get("allow_cloud", False),
        credential_env=spec_cred,
        context_tokens=spec.get("context_tokens"),
        max_tokens=spec.get("max_tokens"),
        extra=spec,
    )


def setup(
    layout,
    *,
    endpoint=None,
    model=None,
    kind=None,
    adapter=None,
    allow_cloud=False,
    credential_env=None,
    converse=False,
    context_tokens=None,
    roles=None,
    converse_profile=None,
    decisions_profile=None,
):
    """Reuse native init; configuration is not evidence of provider/task readiness."""
    from services.launcher import initialize

    check_outdated_system_blockers(probe_subprocesses=False)
    layout.validate()
    if (layout.install / "layout.json").exists():
        verify_layout(layout)
    if os.path.lexists(layout.config):
        raise InstallError("Preserving existing configuration; edit it explicitly instead")
    if kind is not None and adapter is not None and kind != adapter:
        raise InstallError("Conflicting Agent adapter kind arguments")
    selected_kind = adapter if adapter is not None else kind

    if roles is not None:
        if not isinstance(roles, dict) or not set(roles).issubset(SUPPORTED_ROLE_ADAPTERS):
            raise InstallError("Roles must map only 'agent', 'converse', and 'decisions'")
    role_map = dict(roles) if roles else {}
    if converse_profile is not None:
        if "converse" in role_map:
            raise InstallError("Conflicting converse role configuration")
        role_map["converse"] = converse_profile
    if decisions_profile is not None:
        if "decisions" in role_map:
            raise InstallError("Conflicting decisions role configuration")
        role_map["decisions"] = decisions_profile

    if "agent" in role_map:
        agent_built = _build_role_from_mapping("agent", role_map["agent"])
    else:
        agent_built = _build_role_profile(
            "agent",
            endpoint=endpoint,
            model=model,
            kind=selected_kind,
            allow_cloud=allow_cloud,
            credential_env=credential_env,
            context_tokens=context_tokens,
        )

    converse_built = (
        _build_role_from_mapping("converse", role_map["converse"])
        if "converse" in role_map
        else None
    )
    decisions_built = (
        _build_role_from_mapping("decisions", role_map["decisions"])
        if "decisions" in role_map
        else None
    )

    for directory in (layout.config.parent, layout.data, layout.workspace):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with redirect_stdout(io.StringIO()):
        initialize(SimpleNamespace(data_dir=layout.data, config=layout.config))
    config = json.loads(layout.config.read_text())
    config.pop("voice", None)

    config["providers"]["agent"] = dict(agent_built)
    if converse_built is not None:
        config["providers"]["converse"] = dict(converse_built)
    elif converse:
        converse_default_kind = "lmstudio" if agent_built["kind"] == "lmstudio" else "compatible"
        config["providers"]["converse"] = _build_role_profile(
            "converse",
            endpoint=agent_built["base_url"],
            model=agent_built["model"],
            kind=converse_default_kind,
            allow_cloud=agent_built["allow_cloud"],
            credential_env=agent_built.get("api_key_env"),
        )
    if decisions_built is not None:
        config["providers"]["decisions"] = dict(decisions_built)
    elif converse:
        config["providers"]["decisions"] = _build_role_profile(
            "decisions",
            endpoint=agent_built["base_url"],
            model=agent_built["model"],
            kind="compatible",
            allow_cloud=agent_built["allow_cloud"],
            credential_env=agent_built.get("api_key_env"),
        )

    config["distribution"] = {"converse": bool(converse or converse_built is not None)}
    atomic_json(layout.config, config)
    return {"ready": False, "provider_connection": "not_checked", "next": "kinaiya serve host"}


class InstallError(RuntimeError):
    """Actionable refusal; never includes downloaded bodies or secret configuration."""


@dataclass(frozen=True)
class Layout:
    install: Path
    data: Path
    config: Path
    workspace: Path
    cache: Path
    bin: Path

    @classmethod
    def for_home(cls, home: Path):
        return cls(
            home / ".local/share/kinaiya/install",
            home / ".local/share/kinaiya/data",
            home / ".config/kinaiya/config.json",
            home / "Kinaiya Workspace",
            home / ".cache/kinaiya",
            home / ".local/bin",
        )

    def validate(self):
        roots = [self.install, self.data, self.config.parent, self.workspace, self.cache, self.bin]
        for path in [*roots, self.config]:
            if not path.is_absolute() or any(ord(c) < 32 for c in str(path)):
                raise InstallError("Locations must be absolute paths without control characters")
            if path.resolve() != Path(os.path.abspath(path)):
                raise InstallError("Locations cannot use symlink aliases")
        for i, path in enumerate(roots):
            for other in roots[i + 1 :]:
                if path == other or path in other.parents or other in path.parents:
                    raise InstallError(
                        "Install, data, config, workspace and cache must be disjoint"
                    )


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_artifact(release):
    artifacts = [
        a
        for a in release["artifacts"]
        if a["platform"] == "linux"
        and a["architecture"] == "any"
        and a["name"].endswith("-source.tar.gz")
    ]
    if len(artifacts) != 1:
        raise InstallError("Release must contain exactly one Linux host source artifact")
    return artifacts[0]


class _SourceTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # This hook precedes stdlib PAX/GNU payload parsing and allocation.
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
            raise InstallError(
                "Unsafe archive: only regular files/directories; extension headers refused"
            )
        # The private stdlib hook is intentionally absent from typeshed.
        return cast(Any, tarfile.TarInfo)._proc_member(self, archive)


def unpack_source(archive, destination, artifact):
    from packages.distribution.releases import ReleaseError, verify_artifact, verify_gzip_archive

    try:
        verify_artifact(archive, artifact)
        verify_gzip_archive(archive, 512 * 1024**2 + 20000 * 1024 + 10240)
    except ReleaseError:
        raise InstallError(
            "Source artifact integrity mismatch (size, SHA-256 or gzip stream)"
        ) from None
    destination.mkdir(mode=0o700)
    seen, total = set(), 0
    try:
        # Stream and bound members before extracting; never delegate path/link handling
        # to tar.extractall, and never allow links, devices, sparse or duplicate entries.
        with tarfile.open(archive, "r|gz", tarinfo=_SourceTarInfo) as tar:
            for member in tar:
                path = PurePosixPath(member.name)
                total += member.size
                if len(seen) >= 20000 or total > 512 * 1024**2:
                    raise InstallError("Source archive exceeds extraction limits")
                if (
                    not path.parts
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or ":" in member.name
                    or any(ord(c) < 32 for c in member.name)
                    or not (member.isfile() or member.isdir())
                    or member.sparse
                    or member.name.rstrip("/") != str(path)
                ):
                    raise InstallError("Unsafe source archive member")
                if str(path) in seen:
                    raise InstallError("Duplicate source archive member")
                seen.add(str(path))
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with tar.extractfile(member) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
    except (tarfile.TarError, OSError, EOFError):
        raise InstallError("Unsafe or malformed source archive; stage discarded") from None
    roots = (
        [destination] if (destination / "pyproject.toml").is_file() else list(destination.iterdir())
    )
    if (
        len(roots) != 1
        or not (roots[0] / "pyproject.toml").is_file()
        or not (roots[0] / "requirements-runtime.lock").is_file()
    ):
        raise InstallError("Source archive lacks project/runtime lock")
    return roots[0]


def prepare_runtime(generation, source):
    from services.distribution.stage_progress import current_reporter

    python = generation / "runtime/bin/python"
    commands = [
        [sys.executable, "-m", "venv", str(generation / "runtime")],
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--index-url",
            "https://pypi.org/simple",
            "--no-input",
            "--require-hashes",
            "--only-binary=:all:",
            "--disable-pip-version-check",
            "-r",
            str(source / "requirements-runtime.lock"),
        ],
        [
            str(python),
            "-c",
            "import services.launcher; import services.agent; import services.session.api",
        ],
    ]
    reporter = current_reporter()
    for index, command in enumerate(commands):
        try:
            proc = subprocess.run(command, cwd=source, check=True, capture_output=True, timeout=600)
            if reporter is not None and proc is not None:
                reporter.log_subprocess(
                    "staging",
                    command,
                    getattr(proc, "returncode", 0),
                    getattr(proc, "stdout", None),
                    getattr(proc, "stderr", None),
                )
        except (OSError, subprocess.SubprocessError) as exc:
            if reporter is not None:
                reporter.log_subprocess(
                    "staging",
                    command,
                    getattr(exc, "returncode", 1),
                    getattr(exc, "stdout", None),
                    getattr(exc, "stderr", str(exc)),
                )
            raise InstallError(
                f"Runtime stage {index + 1} failed; current install unchanged. Check Python venv, network and locked wheel availability"
            ) from None


def ports_busy():
    for port in range(8760, 8766):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
    return False


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024**2:
        raise InstallError(
            "Missing, oversized or aliased managed metadata; preserve files for recovery"
        )
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (ValueError, UnicodeError, RecursionError):
        raise InstallError("Malformed managed metadata; preserve files for recovery") from None


def verify_layout(layout, *, required=True):
    layout.validate()
    for name in ("layout.json", "generations", "runtime.lock", "pending.json", "recovery.json"):
        path = layout.install / name
        if path.is_symlink() or (path.exists() and path.resolve() != path):
            raise InstallError("Managed path alias is unsafe; refusing operation")
    marker = layout.install / "layout.json"
    expected = {key: str(value) for key, value in asdict(layout).items()}
    if marker.exists():
        if read_json(marker) != expected:
            raise InstallError("Saved layout differs; preserve existing data and configuration")
    elif required or (layout.install.exists() and any(layout.install.iterdir())):
        raise InstallError("Refusing unmanaged installation directory")


def inventory(source):
    result = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise InstallError("Installed source was modified with an unsafe alias")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            with path.open("rb") as stream:
                result[path.relative_to(source).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return result


def generation_receipt(layout, generation):
    if (
        not re.fullmatch(r"[0-9a-f]{32}", generation.name)
        or generation.parent != layout.install / "generations"
        or generation.is_symlink()
        or generation.resolve() != generation
    ):
        raise InstallError("Generation is unmanaged or an unsafe alias")
    receipt = read_json(generation / "receipt.json")
    try:
        validate_release(receipt["release"])
        source = generation / receipt["source"]
        if (
            not source.is_dir()
            or not source.resolve().is_relative_to(generation)
            or source.resolve() != source
            or inventory(source) != receipt["inventory"]
            or (generation / "launch").is_symlink()
            or hashlib.sha256((generation / "launch").read_bytes()).hexdigest()
            != receipt["launch_sha256"]
        ):
            raise InstallError("Installed source/launcher was locally modified; refusing update")
    except (KeyError, TypeError, ValueError):
        raise InstallError("Invalid generation metadata; preserve files for recovery") from None
    return receipt


def current_receipt(layout):
    if not layout.install.exists():
        return None
    verify_layout(layout)
    current = layout.install / "current"
    if not os.path.lexists(current):
        return None
    generation = current.resolve()
    if not current.is_symlink() or generation.parent != layout.install / "generations":
        raise InstallError("Current installation pointer is unmanaged")
    return generation_receipt(layout, generation)


def update_plan(current, target):
    validate_release(target)
    if target["min_updater"] > 1 or not target["protocol_min"] <= 1 <= target["protocol_max"]:
        raise InstallError("Target needs a newer updater or unsupported protocol")
    if current is None:
        if target["state_schema"] != 1:
            raise InstallError("Unsupported initial state schema")
        return "install"
    old = current["release"]
    if target["state_schema"] != old["state_schema"]:
        raise InstallError(
            "State schema change requires a validated migration/backup restore; refused"
        )
    if target["build_number"] < old["build_number"]:
        raise InstallError("Target is older; downgrade refused without proven schema-safe rollback")
    if target["build_number"] == old["build_number"]:
        if target != old:
            raise InstallError("Conflicting immutable build identity")
        return "current"
    return "update"


@contextmanager
def operation_lock(path, *, shared=False):
    if path.is_symlink():
        raise InstallError("Operation lock cannot be an alias")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                # Test-host equivalent only. Native installs are Linux-only.
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
                )
        except OSError:
            raise InstallError(
                "Managed services or another update are busy; stop/retry explicitly"
            ) from None
        yield
    finally:
        os.close(descriptor)


def validate_release(release):
    # Shared #262 contract: no independent permissive installer metadata parser.
    from packages.distribution.releases import ReleaseError, validate_manifest

    try:
        return validate_manifest(release)
    except ReleaseError as exc:
        raise InstallError(f"Invalid release metadata: {exc}") from None


def install_release(layout, release, archive, *, prepare=prepare_runtime, busy=ports_busy):
    validate_release(release)
    verify_layout(layout, required=False)
    marker = layout.install / "layout.json"
    layout.install.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not marker.exists():
        atomic_json(marker, {key: str(value) for key, value in asdict(layout).items()})
    with operation_lock(layout.install / "runtime.lock"):
        return _install_release(layout, release, archive, prepare=prepare, busy=busy)


def launcher_text(layout):
    return "#!/bin/sh\nexec " + shlex.quote(str(layout.install / "current/launch")) + ' "$@"\n'


def _install_release(layout, release, archive, *, prepare, busy):
    verify_layout(layout)
    artifact = source_artifact(release)
    wrapper = layout.bin / "kinaiya"
    wrapper_text = launcher_text(layout)
    if wrapper.is_symlink() or (wrapper.exists() and wrapper.read_text() != wrapper_text):
        raise InstallError("Refusing to overwrite an unrelated launcher")
    if (layout.install / "pending.json").exists():
        raise InstallError("An interrupted operation needs explicit recovery before update")
    if update_plan(current_receipt(layout), release) == "current":
        return release
    if busy():
        raise InstallError("Services are busy; stop them explicitly. No process will be killed")
    layout.install.mkdir(parents=True, exist_ok=True, mode=0o700)
    generations = layout.install / "generations"
    generations.mkdir(exist_ok=True)
    generation = generations / uuid.uuid4().hex
    generation.mkdir()
    pending = layout.install / "pending.json"
    atomic_json(pending, {"generation": generation.name, "phase": "staging"})
    switched = False
    try:
        source = unpack_source(archive, generation / "source", artifact)
        prepare(generation, source)
        launch = generation / "launch"
        text = (
            "#!/bin/sh\nset -eu\n"
            + f"cd {shlex.quote(str(source))}\n"
            + f"export PYTHONPATH={shlex.quote(str(source))}\n"
            + f"export KINAIYA_INSTALL_ROOT={shlex.quote(str(layout.install))}\n"
            + "exec "
            + shlex.quote(str(generation / "runtime/bin/python"))
            + " -m services.launcher --data-dir "
            + shlex.quote(str(layout.data))
            + " --config "
            + shlex.quote(str(layout.config))
            + " --workspace "
            + shlex.quote(str(layout.workspace))
            + ' "$@"\n'
        )
        launch.write_text(text, encoding="utf-8", newline="\n")
        launch.chmod(0o700)
        atomic_json(
            generation / "receipt.json",
            {
                "release": release,
                "source": str(source.relative_to(generation)).replace("\\", "/"),
                "inventory": inventory(source),
                "launch_sha256": hashlib.sha256(launch.read_bytes()).hexdigest(),
            },
        )
        atomic_json(
            layout.install / "layout.json",
            {key: str(value) for key, value in asdict(layout).items()},
        )
        layout.bin.mkdir(parents=True, exist_ok=True)
        wrapper = layout.bin / "kinaiya"
        wrapper.write_text(wrapper_text, encoding="utf-8", newline="\n")
        wrapper.chmod(0o700)
        link = layout.install / (".current-" + uuid.uuid4().hex)
        link.symlink_to(generation, target_is_directory=True)
        os.replace(link, layout.install / "current")
        switched = True
        pending.unlink()
        return release
    except BaseException:
        if switched:
            # A cleanup failure is not proof the switch failed. Retain the active
            # generation and journal so explicit recovery can reconcile it.
            raise InstallError(
                "Runtime switched; explicit recovery required to finish receipt cleanup"
            ) from None
        shutil.rmtree(generation)
        pending.unlink(missing_ok=True)
        raise


def remove_link(path):
    if os.name == "nt" and path.is_junction():
        path.rmdir()
    else:
        path.unlink()


def recover(layout, *, busy=ports_busy):
    verify_layout(layout)
    with operation_lock(layout.install / "runtime.lock"):
        verify_layout(layout)
        if busy():
            raise InstallError("Services are busy; stop them explicitly before recovery")
        pending = layout.install / "pending.json"
        if not pending.exists():
            return {"action": "nothing_to_recover"}
        state = read_json(pending)
        name = state.get("generation") if isinstance(state, dict) else None
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[0-9a-f]{32}", name)
            or state.get("phase") != "staging"
        ):
            raise InstallError(
                "Unrecognized interrupted operation; preserve metadata for manual recovery"
            )
        generation = layout.install / "generations" / name
        if generation.is_symlink() or generation.resolve() != generation:
            raise InstallError("Interrupted generation alias is unsafe")
        current_receipt(layout)  # Refuse any dirty or invalid active code.
        active = layout.install / "current"
        if os.path.lexists(active) and active.resolve() == generation:
            generation_receipt(layout, generation)
            action = "completed_switch"
        else:
            if generation.exists():
                shutil.rmtree(generation)
            action = "discarded_stage"
        result = {
            "action": action,
            "generation": name,
            "database_restored": False,
            "tasks_replayed": False,
            "services_restarted": False,
        }
        atomic_json(layout.install / "recovery.json", result)
        pending.unlink()
        return result


def rollback(layout, name, *, busy=ports_busy):
    verify_layout(layout)
    if not re.fullmatch(r"[0-9a-f]{32}", name):
        raise InstallError("Select an exact retained generation ID from the install directory")
    with operation_lock(layout.install / "runtime.lock"):
        verify_layout(layout)
        if (layout.install / "pending.json").exists():
            raise InstallError("Resolve interrupted operation before rollback")
        if busy():
            raise InstallError("Services are busy; stop them explicitly before rollback")
        current = current_receipt(layout)
        generation = layout.install / "generations" / name
        target = generation_receipt(layout, generation)
        if (
            current is None
            or current["release"]["state_schema"] != target["release"]["state_schema"]
        ):
            raise InstallError(
                "State schema mismatch; no validated database backup/restore is implemented"
            )
        update_plan(None, target["release"])
        pending = layout.install / "pending.json"
        atomic_json(pending, {"generation": name, "phase": "staging"})
        link = layout.install / (".current-" + uuid.uuid4().hex)
        link.symlink_to(generation, target_is_directory=True)
        os.replace(link, layout.install / "current")
        result = {
            "action": "code_rollback",
            "generation": name,
            "database_restored": False,
            "services_restarted": False,
            "tasks_replayed": False,
        }
        atomic_json(layout.install / "recovery.json", result)
        pending.unlink()
        return result


def uninstall(layout, *, delete_data=False, confirmation=None, busy=ports_busy):
    verify_layout(layout)
    if delete_data and confirmation != "DELETE":
        raise InstallError("Data deletion requires a separate --confirm-data-deletion DELETE")
    with operation_lock(layout.install / "runtime.lock"):
        verify_layout(layout)
        if busy():
            raise InstallError("Services are busy; stop them explicitly before uninstall")
        if (layout.install / "pending.json").exists():
            raise InstallError("Resolve interrupted operation before uninstall")
        current_receipt(layout)
        wrapper = layout.bin / "kinaiya"
        if wrapper.is_symlink() or (
            wrapper.exists() and wrapper.read_text() != launcher_text(layout)
        ):
            raise InstallError("Preserving unrelated or modified launcher")
        current = layout.install / "current"
        if os.path.lexists(current):
            remove_link(current)
        generations = layout.install / "generations"
        if generations.exists():
            shutil.rmtree(generations)
        wrapper.unlink(missing_ok=True)
        if delete_data:
            for directory in (layout.data, layout.cache):
                if directory.exists():
                    shutil.rmtree(directory)
            layout.config.unlink(missing_ok=True)
        result = {"action": "uninstalled", "data_deleted": delete_data, "workspace_deleted": False}
        # Keep the marker and same lock inode so competing installers cannot bypass
        # serialization by recreating a removed lock while it is still held.
        atomic_json(layout.install / "recovery.json", result)
        return result
