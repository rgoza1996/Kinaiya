#!/bin/sh
# Standalone native Linux bootstrap, Apache-2.0. Review before execution.
# Trust: official GitHub HTTPS metadata + immutable release assets + SHA-256,
# not an independent publisher signature. Never fetch or execute main/latest code.
set -eu
if [ -z "${KINAIYA_INVOKING_PWD:-}" ]; then
  KINAIYA_INVOKING_PWD="$(pwd -P 2>/dev/null || pwd)"
  export KINAIYA_INVOKING_PWD
fi
if [ -z "${KINAIYA_PYTHON:-}" ]; then
  for candidate in python3 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; then
      KINAIYA_PYTHON="$candidate"
      break
    fi
  done
fi
exec "${KINAIYA_PYTHON:-python3}" -I - "$@" <<'KINAIYA_BOOTSTRAP_PY'
# BEGIN PYTHON BOOTSTRAP
import argparse
import ctypes.util
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.github.com/repos/rgoza1996/Kinaiya/releases"
ROOT = "https://github.com/rgoza1996/Kinaiya/releases"
MAX_BYTES = 512 * 1024**2
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
MIN_BUBBLEWRAP_VERSION = (0, 6, 1)
MIN_LIBSECCOMP_VERSION = (2, 5)
STAGES = (
    ("preflight", "Checking host & sandbox prerequisites"),
    ("dependencies", "Verifying system dependencies"),
    ("download", "Downloading & verifying release artifact"),
    ("staging", "Staging runtime & isolated virtual environment"),
    ("setup_handoff", "Handing off to Kinaiya setup"),
)
STAGE_LABELS = dict(STAGES)
STAGE_INDEX = {key: idx + 1 for idx, (key, _) in enumerate(STAGES)}
STAGE_REMEDIATION = {
    "preflight": "Verify Ubuntu 22.04/24.04 host prerequisites, Python 3.11-3.13, >=1 GiB free disk, and bubblewrap user namespaces.",
    "dependencies": "Install missing packages manually with `sudo apt-get install --no-upgrade -y <pkgs>` or refresh your local apt index.",
    "download": "Check HTTPS connectivity to github.com / release-assets.githubusercontent.com and retry the installation.",
    "staging": "Check Python venv/ensurepip availability, network access to pypi.org, and writable space in the install root.",
    "setup_handoff": "Run `kinaiya setup` explicitly to complete provider configuration.",
}
_ACTIVE_BOOTSTRAP_REPORTER = None
PROTECTED_INSTALL_ROOTS = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/libx32",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
    }
)
PROTECTED_INSTALL_TREES = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
)
DEFAULT_DPKG_STATUS_PATH = Path("/var/lib/dpkg/status")
APT_STRICT_OPTIONS = (
    "--no-upgrade",
    "--no-remove",
    "-o",
    "APT::Get::Upgrade=false",
    "-o",
    "APT::Get::Remove=false",
    "-o",
    "APT::Get::Allow-Downgrades=false",
)
_REAL_SUBPROCESS_RUN = subprocess.run
_APT_PKG_NAME_RE = re.compile(r"[a-z0-9][a-z0-9.+-]*(?::[a-z0-9]+)?")
_APT_VERSION_RE = re.compile(r"[0-9][A-Za-z0-9.+:~-]*")
_APT_SUMMARY_RE = re.compile(
    r"^(?P<upgraded>\d+)\s+upgraded,\s+"
    r"(?P<newly_installed>\d+)\s+newly installed,"
    r"(?:\s+(?P<downgraded>\d+)\s+downgraded,)?"
    r"\s+(?P<to_remove>\d+)\s+to remove\s+and\s+"
    r"(?P<not_upgraded>\d+)\s+not upgraded\."
)
_APT_INST_RE = re.compile(
    r"^Inst\s+"
    r"(?P<pkg>[a-z0-9][a-z0-9.+-]*(?::[a-z0-9]+)?)"
    r"(?:\s+\[(?P<old_ver>[^\]]*)\])?"
    r"\s+\((?P<new_ver>[0-9][A-Za-z0-9.+:~-]*)(?:\s+[^\)]*)?\)"
    r"(?:\s*(?P<trailing>\[.*\]))?\s*$"
)
_APT_CONF_RE = re.compile(
    r"^Conf\s+"
    r"(?P<pkg>[a-z0-9][a-z0-9.+-]*(?::[a-z0-9]+)?)"
    r"\s+\((?P<new_ver>[0-9][A-Za-z0-9.+:~-]*)(?:\s+[^\)]*)?\)"
    r"(?:\s*(?P<trailing>\[.*\]))?\s*$"
)
_APT_REMV_RE = re.compile(
    r"^(?:Remv|Purg)\s+"
    r"(?P<pkg>\S+)"
    r"(?:\s+\[(?P<old_ver>[^\]]*)\])?"
    r"(?:\s*(?P<trailing>\[.*\]))?\s*$"
)
_APT_SKIP_INSTALLED_RE = re.compile(
    r"^(?:Skipping\s+(?P<skip_pkg>[a-z0-9][a-z0-9.+-]*(?::[a-z0-9]+)?),\s+it is already installed|"
    r"(?P<newest_pkg>[a-z0-9][a-z0-9.+-]*(?::[a-z0-9]+)?)\s+is already the newest version)"
)


class BootstrapError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise BootstrapError(message)


def redact_install_log(text):
    if not text:
        return ""
    redacted = re.sub(r"(?i)(\bAuthorization\s*:\s*)(?:Bearer\s+|Basic\s+|Token\s+)?[^\s\"'\\,;]+", r"\1[REDACTED]", str(text))
    redacted = re.sub(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{4,}", r"\1 [REDACTED]", redacted)
    redacted = re.sub(r"\b((?:https?|wss?)://)[^\s/@\"']+@(?=[^\s/\"']+)", r"\1[REDACTED]@", redacted)
    redacted = re.sub(r'(?i)("(?:[a-z0-9_]*?(?:api_key|secret|token|password|credential)(?!_env)[a-z0-9_]*)"\s*:\s*)"[^"]*"', r'\1"[REDACTED]"', redacted)
    redacted = re.sub(r"(?i)(--(?:[a-z0-9-]*?(?:key|token|secret|password|credential))(?!-env)\s+)([^\s\"']+|\"[^\"]*\"|'[^']*')", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)\b([A-Za-z0-9_]*?(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)(?!_ENV)[A-Za-z0-9_]*\s*[=:]\s*)([^\s,;\"']+|\"[^\"]*\"|'[^']*')", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})\b", "[REDACTED]", redacted)
    return redacted


def append_private_log(log_path, message):
    require(not log_path.is_symlink() and not log_path.parent.is_symlink(), "Diagnostic log path cannot be a symlink alias")
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(log_path.parent.resolve() == Path(os.path.abspath(log_path.parent)), "Diagnostic log directory cannot use symlink aliases")
    safe = redact_install_log(message)
    if not safe.endswith("\n"):
        safe += "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(log_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(safe)
            handle.flush()
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return log_path


class BootstrapStageReporter:
    def __init__(self, *, log_path=None, json_output=False, stream=None, err_stream=None, defer_log=False):
        self.stream = stream if stream is not None else sys.stdout
        self.err_stream = err_stream if err_stream is not None else sys.stderr
        self.log_path = log_path or (Path.home() / ".cache/kinaiya/install.log")
        is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.json_mode = bool(json_output or not is_tty)
        self.no_color = bool(self.json_mode or not is_tty or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb")
        self._defer_log = bool(defer_log)
        self._buffered = []
        self._sha256_verified = False

    def enable_log_persistence(self):
        self._defer_log = False
        if self._buffered:
            combined = "\n".join(self._buffered)
            self._buffered.clear()
            append_private_log(self.log_path, combined)

    def log(self, message, *, stage=None):
        entry = redact_install_log((f"[{stage}] " if stage else "") + str(message))
        if self._defer_log:
            self._buffered.append(entry)
            return
        append_private_log(self.log_path, entry)

    def _emit(self, text, *, error=False):
        if self.json_mode:
            return
        target = self.err_stream if error else self.stream
        target.write(redact_install_log(text) + "\n")
        if hasattr(target, "flush"):
            target.flush()

    def start_stage(self, stage):
        idx = STAGE_INDEX[stage]
        label = STAGE_LABELS[stage]
        self.log(f"[STAGE {idx}/{len(STAGES)} START] {label}", stage=stage)
        self._emit(f"[{idx}/{len(STAGES)}] {label} ...")

    def update_download(self, downloaded_bytes, total_bytes=None):
        self._sha256_verified = False
        idx = STAGE_INDEX["download"]
        if type(total_bytes) is int and total_bytes > 0:
            pct = min(100, (int(downloaded_bytes) * 100) // total_bytes)
            msg = f"[{idx}/{len(STAGES)}] Download progress: {downloaded_bytes}/{total_bytes} B ({pct}%) - awaiting SHA-256 verification"
        else:
            msg = f"[{idx}/{len(STAGES)}] Download progress: {downloaded_bytes} B (total size unknown) - awaiting SHA-256 verification"
        self.log(msg, stage="download")
        self._emit(msg)

    def complete_stage(self, stage, *, sha256_verified=False):
        if stage == "download":
            if sha256_verified:
                self._sha256_verified = True
            require(self._sha256_verified, "Download stage cannot complete before SHA-256 verification")
        idx = STAGE_INDEX[stage]
        label = STAGE_LABELS[stage]
        self.log(f"[STAGE {idx}/{len(STAGES)} PASS] {label}", stage=stage)
        badge = "PASS" if self.no_color else "\x1b[32mPASS\x1b[0m"
        self._emit(f"[{idx}/{len(STAGES)}] {label} ... {badge}")

    def fail_stage(self, stage, reason):
        idx = STAGE_INDEX[stage]
        label = STAGE_LABELS[stage]
        clean = redact_install_log(reason)
        remediation = STAGE_REMEDIATION.get(stage, "Inspect the diagnostic log and retry.")
        self.log(f"[STAGE {idx}/{len(STAGES)} FAIL] {label}: {clean} | Remediation: {remediation}", stage=stage)
        badge = "FAIL" if self.no_color else "\x1b[31mFAIL\x1b[0m"
        self._emit(
            f"[{idx}/{len(STAGES)}] {label} ({stage}) ... {badge}\n"
            f"  Reason:         {clean}\n"
            f"  Diagnostic log: {self.log_path} (mode 0600)\n"
            f"  Remediation:    {remediation}",
            error=True,
        )


def _atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def existing_install_root(home=None):
    home = home or Path.home()
    env_root = os.environ.get("KINAIYA_INSTALL_ROOT")
    if env_root:
        candidate = Path(env_root)
        require(
            not candidate.is_symlink() and candidate.resolve() == Path(os.path.abspath(candidate)),
            "Launcher install root cannot use symlink aliases",
        )
        return candidate
    marker = home / ".config/kinaiya/install-root.json"
    require(not marker.is_symlink(), "Managed install location marker cannot be a symlink alias")
    if marker.exists():
        require(marker.is_file() and marker.stat().st_size <= 4 * 1024**2, "Invalid install location marker")
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise BootstrapError("Malformed install location marker; preserve files for recovery") from None
        require(isinstance(data, dict) and isinstance(data.get("install"), str), "Invalid install location marker")
        recorded = Path(data["install"])
        require(
            not recorded.is_symlink() and recorded.resolve() == Path(os.path.abspath(recorded)),
            "Recorded install root cannot use symlink aliases",
        )
        require(
            (recorded / "layout.json").exists(),
            "Recorded install root is missing layout metadata; preserve files for recovery",
        )
        return recorded
    default_install = home / ".local/share/kinaiya/install"
    require(not (default_install / "layout.json").is_symlink(), "Managed path alias is unsafe; refusing operation")
    if (default_install / "layout.json").exists():
        return default_install
    return None


def validate_install_root(candidate, *, home=None, workspace=None, data_dir=None):
    home = home or Path.home()
    if not isinstance(candidate, Path):
        candidate = Path(candidate)
    require(
        candidate.is_absolute() and all(ord(c) >= 32 for c in str(candidate)),
        "Install location must be an absolute path without control characters",
    )
    normalized = Path(os.path.abspath(candidate))
    require(
        not candidate.is_symlink() and candidate.resolve() == normalized,
        "Install location cannot use symlink aliases",
    )
    posix_str = normalized.as_posix()
    require(
        posix_str not in PROTECTED_INSTALL_ROOTS
        and not any(posix_str == tree or posix_str.startswith(tree + "/") for tree in PROTECTED_INSTALL_TREES),
        f"Refusing protected system root as install location: {normalized}",
    )
    abs_home = Path(os.path.abspath(home))
    require(
        normalized != abs_home and normalized not in abs_home.parents,
        "Install location cannot be the home directory or its parent",
    )
    data_root = Path(os.path.abspath(data_dir or (home / ".local/share/kinaiya/data")))
    config_file = Path(os.path.abspath(home / ".config/kinaiya/config.json"))
    config_root = config_file.parent
    workspace_root = Path(os.path.abspath(workspace or (home / "Kinaiya Workspace")))
    cache_root = Path(os.path.abspath(home / ".cache/kinaiya"))
    bin_root = Path(os.path.abspath(home / ".local/bin"))
    roots = [normalized, data_root, config_root, workspace_root, cache_root, bin_root]
    for path in [*roots, config_file]:
        require(
            path.is_absolute() and all(ord(c) >= 32 for c in str(path)),
            "Locations must be absolute paths without control characters",
        )
        require(
            not path.is_symlink() and path.resolve() == Path(os.path.abspath(path)),
            "Locations cannot use symlink aliases",
        )
    for idx, path in enumerate(roots):
        for other in roots[idx + 1 :]:
            require(
                path != other and path not in other.parents and other not in path.parents,
                "Install, data, config, workspace and cache must be disjoint",
            )
    if normalized.exists():
        require(normalized.is_dir(), "Install location exists and is not a directory")
        require(
            os.access(normalized, os.R_OK | os.W_OK | os.X_OK),
            "Install location lacks read/write/execute permissions",
        )
        for name in ("layout.json", "generations", "runtime.lock", "pending.json", "recovery.json"):
            managed_item = normalized / name
            require(
                not managed_item.is_symlink()
                and (not managed_item.exists() or managed_item.resolve() == managed_item),
                "Managed path alias is unsafe; refusing operation",
            )
        marker = normalized / "layout.json"
        if marker.exists():
            try:
                saved = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise BootstrapError("Malformed managed metadata; preserve files for recovery") from None
            expected = {
                "install": str(normalized),
                "data": str(data_root),
                "config": str(config_file),
                "workspace": str(workspace_root),
                "cache": str(cache_root),
                "bin": str(bin_root),
            }
            require(saved == expected, "Saved layout differs; preserve existing data and configuration")
        else:
            require(not any(normalized.iterdir()), "Refusing non-empty or unmanaged installation directory")
    else:
        ancestor = next((p for p in normalized.parents if p.exists()), None)
        require(
            ancestor is not None and ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK),
            "Install location parent directory is not writable",
        )
    return normalized


def select_install_root(
    home,
    *,
    install_dir=None,
    install_location=None,
    invoking_dir=None,
    workspace=None,
    data_dir=None,
    interactive=False,
    tty=None,
):
    require(
        install_dir is None or install_location is None,
        "Specify either --install-dir or --install-location, not both",
    )
    default_install = home / ".local/share/kinaiya/install"
    existing = existing_install_root(home)
    explicit = install_dir if install_dir is not None else install_location
    if existing is not None:
        if explicit is not None:
            raw = str(explicit)
            if raw == "default":
                requested = default_install
            elif raw == "current":
                require(
                    invoking_dir is not None,
                    "Cannot infer current directory from a managed launcher; pass an explicit path",
                )
                requested = invoking_dir
            else:
                requested = Path(raw)
            require(
                requested.is_absolute() and Path(os.path.abspath(requested)) == existing,
                f"Existing installation is recorded at {existing}; refusing to relocate existing installation",
            )
        return validate_install_root(existing, home=home, workspace=workspace, data_dir=data_dir), False
    if explicit is not None:
        raw = str(explicit)
        if raw == "default":
            candidate = default_install
        elif raw == "current":
            require(
                invoking_dir is not None,
                "Cannot infer current directory from a managed launcher; pass an explicit path",
            )
            candidate = invoking_dir
        else:
            candidate = Path(raw)
        return validate_install_root(candidate, home=home, workspace=workspace, data_dir=data_dir), False
    if not interactive or invoking_dir is None:
        return validate_install_root(default_install, home=home, workspace=workspace, data_dir=data_dir), False
    canonical_default = default_install.resolve()
    canonical_invoking = invoking_dir.resolve()
    if canonical_invoking == canonical_default:
        return validate_install_root(default_install, home=home, workspace=workspace, data_dir=data_dir), False
    tty.write(
        "Select Kinaiya install location:\n"
        f"  1) Default (recommended): {canonical_default}\n"
        f"  2) Current directory:     {canonical_invoking}\n"
        "Choose install location [1]: "
    )
    choice = tty.readline().strip().lower()
    if choice in {"", "1", "default"}:
        return validate_install_root(default_install, home=home, workspace=workspace, data_dir=data_dir), False
    if choice == "yes":
        return validate_install_root(default_install, home=home, workspace=workspace, data_dir=data_dir), True
    if choice in {"2", "current"}:
        return validate_install_root(invoking_dir, home=home, workspace=workspace, data_dir=data_dir), False
    raise BootstrapError(
        "Installation cancelled or invalid install location choice; select 1 (Default) or 2 (Current directory)"
    )


def _parse_version_tuple(raw):
    if not raw or not isinstance(raw, str):
        return None
    match = re.search(r"(\d+(?:\.\d+)+)", raw)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _deb_char_order(ch):
    if ch == "~":
        return -1
    if not ch:
        return 0
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _compare_deb_part(left, right):
    i, j = 0, 0
    while i < len(left) or j < len(right):
        while (i < len(left) and not left[i].isdigit()) or (j < len(right) and not right[j].isdigit()):
            lc = left[i] if i < len(left) and not left[i].isdigit() else ""
            rc = right[j] if j < len(right) and not right[j].isdigit() else ""
            if lc != rc:
                return -1 if _deb_char_order(lc) < _deb_char_order(rc) else 1
            if lc:
                i += 1
            if rc:
                j += 1
        i_start, j_start = i, j
        while i < len(left) and left[i].isdigit():
            i += 1
        while j < len(right) and right[j].isdigit():
            j += 1
        l_num = int(left[i_start:i] or "0")
        r_num = int(right[j_start:j] or "0")
        if l_num != r_num:
            return -1 if l_num < r_num else 1
    return 0


def _split_deb_version(version):
    rest = version.strip()
    if ":" in rest:
        epoch_str, rest = rest.split(":", 1)
        epoch = int(epoch_str) if epoch_str.isdigit() else 0
    else:
        epoch = 0
    if "-" in rest:
        upstream, revision = rest.rsplit("-", 1)
    else:
        upstream, revision = rest, ""
    return epoch, upstream, revision


def _compare_deb_versions(left, right):
    l_epoch, l_up, l_rev = _split_deb_version(left)
    r_epoch, r_up, r_rev = _split_deb_version(right)
    if l_epoch != r_epoch:
        return -1 if l_epoch < r_epoch else 1
    up_cmp = _compare_deb_part(l_up, r_up)
    return up_cmp if up_cmp != 0 else _compare_deb_part(l_rev, r_rev)


def _validate_apt_package_spec(spec, *, allow_version_pin=False):
    require(isinstance(spec, str) and bool(spec), f"Invalid dependency name: {spec!r}")
    if allow_version_pin and "=" in spec:
        pkg, ver = spec.split("=", 1)
        require(
            _APT_PKG_NAME_RE.fullmatch(pkg) is not None and _APT_VERSION_RE.fullmatch(ver) is not None,
            f"Invalid dependency pin: {spec!r}",
        )
        return pkg, ver
    require(_APT_PKG_NAME_RE.fullmatch(spec) is not None, f"Invalid dependency name: {spec!r}")
    return spec, None


def _resolve_dpkg_status_path(status_path=None, *, env=None):
    if status_path is not None:
        return Path(status_path)
    active_env = os.environ if env is None else env
    apt_config = active_env.get("APT_CONFIG")
    if apt_config:
        try:
            cfg_text = Path(apt_config).read_text(encoding="utf-8", errors="replace")
        except OSError:
            cfg_text = ""
        status_match = re.search(r'Dir::State::status\s+"([^"]+)"\s*;', cfg_text)
        if status_match:
            raw_status = Path(status_match.group(1))
            if raw_status.is_absolute():
                return raw_status
            dir_match = re.search(r'Dir\s+"([^"]+)"\s*;', cfg_text)
            base_dir = Path(dir_match.group(1)) if dir_match else Path(apt_config).parent
            return base_dir / raw_status
    return DEFAULT_DPKG_STATUS_PATH


def installed_packages_snapshot(status_path=None, *, env=None):
    resolved = _resolve_dpkg_status_path(status_path, env=env)
    if resolved is None:
        return {}
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    snapshot = {}
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if ":" in line and not line[:1].isspace():
                key, val = line.split(":", 1)
                fields[key.strip()] = val.strip()
        pkg = fields.get("Package")
        ver = fields.get("Version")
        status_tokens = fields.get("Status", "").split()
        is_present = "installed" in status_tokens or (
            len(status_tokens) >= 3 and status_tokens[2] not in {"not-installed", "config-files"}
        )
        if pkg and ver and is_present:
            snapshot[pkg] = ver
            arch = fields.get("Architecture")
            if arch:
                snapshot[f"{pkg}:{arch}"] = ver
    return snapshot


def installed_package_version(package, status_path=DEFAULT_DPKG_STATUS_PATH):
    return installed_packages_snapshot(status_path).get(package)


def _venv_available():
    try:
        import ensurepip  # noqa: F401
        import venv  # noqa: F401
    except ImportError:
        return False
    return True


def missing_dependencies():
    missing = []
    if shutil.which("bwrap") is None:
        missing.append("bubblewrap")
    if ctypes.util.find_library("seccomp") is None:
        missing.append("libseccomp2")
    if not _venv_available():
        missing.append(f"python{sys.version_info[0]}.{sys.version_info[1]}-venv")
    return missing


def apt_simulation_command(packages):
    require(bool(packages), "Refusing empty apt simulation package list")
    for pkg in packages:
        _validate_apt_package_spec(pkg, allow_version_pin=True)
    return ["apt-get", "-s", *APT_STRICT_OPTIONS, "-y", "install", "--", *packages]


def dependency_install_command(missing):
    for pkg in missing:
        _validate_apt_package_spec(pkg, allow_version_pin=True)
    return ["sudo", "apt-get", "install", *APT_STRICT_OPTIONS, "-y", "--", *missing]


def parse_apt_simulation_output(
    stdout,
    stderr="",
    *,
    returncode=0,
    requested_packages=(),
    installed_snapshot=None,
):
    snapshot = dict(installed_snapshot or {})
    upgrades, removals, downgrades, already_installed, broken = [], [], [], [], []
    new_installs = []
    configured = {}
    summary = None
    section = None
    section_upgrades, section_removals, section_downgrades = [], [], []
    section_headers = {
        "The following packages will be upgraded:": "upgraded",
        "The following packages will be REMOVED:": "removed",
        "The following packages will be DOWNGRADED:": "downgraded",
        "The following packages have unmet dependencies:": "unmet",
        "The following NEW packages will be installed:": "new",
        "The following additional packages will be installed:": "additional",
        "The following packages have been kept back:": "kept_back",
    }

    for raw_line in (stdout or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped in section_headers:
            section = section_headers[stripped]
            continue
        if raw_line[:1].isspace() and section in {"upgraded", "removed", "downgraded", "unmet"}:
            if section == "unmet":
                broken.append(stripped)
            else:
                tokens = [tok.rstrip("*") for tok in stripped.split() if tok.rstrip("*")]
                if section == "upgraded":
                    section_upgrades.extend(tokens)
                elif section == "removed":
                    section_removals.extend(tokens)
                elif section == "downgraded":
                    section_downgrades.extend(tokens)
            continue
        section = None

        skip_match = _APT_SKIP_INSTALLED_RE.match(stripped)
        if skip_match:
            pkg_hit = skip_match.group("skip_pkg") or skip_match.group("newest_pkg")
            if pkg_hit and pkg_hit not in already_installed:
                already_installed.append(pkg_hit)
            continue

        summary_match = _APT_SUMMARY_RE.match(stripped)
        if summary_match:
            summary = {
                "upgraded": int(summary_match.group("upgraded")),
                "newly_installed": int(summary_match.group("newly_installed")),
                "downgraded": int(summary_match.group("downgraded") or "0"),
                "to_remove": int(summary_match.group("to_remove")),
                "not_upgraded": int(summary_match.group("not_upgraded")),
            }
            continue

        if stripped.startswith(("Remv ", "Purg ")):
            remv_match = _APT_REMV_RE.match(stripped)
            if remv_match:
                pkg = remv_match.group("pkg")
                old_ver = remv_match.group("old_ver")
                label = f"{pkg} ({old_ver})" if old_ver else pkg
                if label not in removals:
                    removals.append(label)
            else:
                removals.append(stripped)
            continue

        if stripped.startswith("Inst "):
            inst_match = _APT_INST_RE.match(stripped)
            if not inst_match:
                broken.append(f"malformed Inst line: {stripped}")
                continue
            pkg = inst_match.group("pkg")
            base_pkg = pkg.split(":", 1)[0]
            old_ver = inst_match.group("old_ver")
            new_ver = inst_match.group("new_ver")
            trailing = inst_match.group("trailing")
            if trailing:
                broken.append(f"{pkg} has unresolved dependencies {trailing}")
            if old_ver is not None:
                is_downgrade = (
                    pkg in section_downgrades
                    or base_pkg in section_downgrades
                    or (bool(old_ver) and _compare_deb_versions(old_ver, new_ver) > 0)
                )
                label = f"{pkg} ({old_ver or 'installed'} -> {new_ver})"
                target_list = downgrades if is_downgrade else upgrades
                if label not in target_list:
                    target_list.append(label)
            else:
                snap_ver = snapshot.get(pkg) or snapshot.get(base_pkg)
                if snap_ver is not None:
                    label = f"{pkg} ({snap_ver} -> {new_ver})"
                    target_list = (
                        downgrades if _compare_deb_versions(snap_ver, new_ver) > 0 else upgrades
                    )
                    if label not in target_list:
                        target_list.append(label)
                else:
                    new_installs.append((pkg, new_ver))
            continue

        if stripped.startswith("Conf "):
            conf_match = _APT_CONF_RE.match(stripped)
            if not conf_match or conf_match.group("trailing"):
                broken.append(f"broken Conf line: {stripped}")
                continue
            configured[conf_match.group("pkg")] = conf_match.group("new_ver")
            continue

        if stripped.startswith("E:"):
            broken.append(stripped)

    for raw_err in (stderr or "").splitlines():
        err_stripped = raw_err.strip()
        if err_stripped:
            broken.append(err_stripped)

    def _already_recorded(pkg_name, items):
        base = pkg_name.split(":", 1)[0]
        return any(
            item == pkg_name
            or item == base
            or item.startswith(f"{pkg_name} ")
            or item.startswith(f"{base} ")
            for item in items
        )

    for pkg in section_upgrades:
        if not _already_recorded(pkg, upgrades):
            snap_ver = snapshot.get(pkg) or snapshot.get(pkg.split(":", 1)[0])
            upgrades.append(f"{pkg} ({snap_ver} -> upgraded)" if snap_ver else pkg)
    for pkg in section_downgrades:
        if not _already_recorded(pkg, downgrades):
            snap_ver = snapshot.get(pkg) or snapshot.get(pkg.split(":", 1)[0])
            downgrades.append(f"{pkg} ({snap_ver} -> downgraded)" if snap_ver else pkg)
    for pkg in section_removals:
        if not _already_recorded(pkg, removals):
            snap_ver = snapshot.get(pkg) or snapshot.get(pkg.split(":", 1)[0])
            removals.append(f"{pkg} ({snap_ver})" if snap_ver else pkg)

    req_label = ", ".join(requested_packages) if requested_packages else "requested packages"
    policy_tail = (
        "Kinaiya refuses to upgrade, downgrade, or remove existing system packages, "
        "and never runs `sudo apt-get update` or `sudo apt-get upgrade` for you."
    )

    mutation_reasons = []
    if upgrades or (summary is not None and summary["upgraded"] > 0):
        offending = ", ".join(upgrades) if upgrades else f"{summary['upgraded']} package(s)"
        mutation_reasons.append(f"upgrade existing package(s) [{offending}]")
    if downgrades or (summary is not None and summary["downgraded"] > 0):
        offending = ", ".join(downgrades) if downgrades else f"{summary['downgraded']} package(s)"
        mutation_reasons.append(f"downgrade existing package(s) [{offending}]")
    if removals or (summary is not None and summary["to_remove"] > 0):
        offending = ", ".join(removals) if removals else f"{summary['to_remove']} package(s)"
        mutation_reasons.append(f"remove existing package(s) [{offending}]")
    if mutation_reasons:
        raise BootstrapError(
            f"Out-of-date system blocker: cannot install missing dependencies ({req_label}): "
            f"apt solver transaction would {'; '.join(mutation_reasons)}. "
            f"Please update or reconcile those packages manually first; {policy_tail}"
        )

    if already_installed:
        raise BootstrapError(
            f"Out-of-date system blocker: cannot install ({req_label}): "
            f"package(s) [{', '.join(already_installed)}] are already installed. {policy_tail}"
        )

    if returncode != 0 or broken:
        detail = broken[-1] if broken else f"apt-get exited {returncode}"
        raise BootstrapError(
            f"Out-of-date system blocker: could not install missing dependencies ({req_label}) "
            f"with `--no-upgrade` ({detail}). An existing installed package or your local apt package index "
            f"is out of date. Please update that specific package or refresh your package index manually "
            f"first; {policy_tail}"
        )

    if summary is None:
        raise BootstrapError(
            f"Out-of-date system blocker: apt-get simulation for ({req_label}) did not produce a "
            f"verifiable solver summary. {policy_tail}"
        )

    if summary["newly_installed"] <= 0 or summary["newly_installed"] != len(new_installs):
        raise BootstrapError(
            f"Out-of-date system blocker: apt-get simulation for ({req_label}) reported "
            f"{summary['newly_installed']} newly installed package(s), but {len(new_installs)} "
            f"verified Inst entry/entries were parsed. {policy_tail}"
        )

    seen_pkgs = set()
    by_name = {}
    for pkg, ver in new_installs:
        base_pkg = pkg.split(":", 1)[0]
        if pkg in seen_pkgs or base_pkg in seen_pkgs:
            raise BootstrapError(
                f"Out-of-date system blocker: duplicate Inst entry for {pkg} in apt simulation. {policy_tail}"
            )
        if configured.get(pkg) != ver:
            raise BootstrapError(
                f"Out-of-date system blocker: missing or mismatched Conf entry for {pkg}={ver} in apt simulation. {policy_tail}"
            )
        seen_pkgs.add(pkg)
        seen_pkgs.add(base_pkg)
        by_name[pkg] = ver
        by_name[base_pkg] = ver

    for req in requested_packages:
        req_name, req_ver = _validate_apt_package_spec(req, allow_version_pin=True)
        req_base = req_name.split(":", 1)[0]
        resolved_ver = by_name.get(req_name) or by_name.get(req_base)
        if resolved_ver is None:
            raise BootstrapError(
                f"Out-of-date system blocker: requested package {req_name} was not present in "
                f"verified new-install set for ({req_label}). {policy_tail}"
            )
        if req_ver is not None and resolved_ver != req_ver:
            raise BootstrapError(
                f"Out-of-date system blocker: requested package {req} resolved to {resolved_ver}. {policy_tail}"
            )

    return {
        "requested_packages": list(requested_packages),
        "new_installs": list(new_installs),
        "pinned_packages": [f"{pkg}={ver}" for pkg, ver in new_installs],
        "summary": summary,
    }


def _active_dpkg_snapshot(*, status_path, env, runner):
    active_env = os.environ if env is None else env
    has_explicit_status = (
        status_path is not None
        or "APT_CONFIG" in active_env
        or DEFAULT_DPKG_STATUS_PATH != Path("/var/lib/dpkg/status")
    )
    is_real_runner = runner is _REAL_SUBPROCESS_RUN and subprocess.run is _REAL_SUBPROCESS_RUN
    if not has_explicit_status and not is_real_runner:
        return {}
    return installed_packages_snapshot(status_path, env=env)


def verify_apt_no_existing_package_changes(
    packages,
    *,
    env=None,
    runner=None,
    status_path=None,
    expected_snapshot=None,
    expected_pinned=None,
):
    require(bool(packages), "Refusing to verify an empty dependency package list")
    for pkg in packages:
        _validate_apt_package_spec(pkg, allow_version_pin=True)

    active_runner = subprocess.run if runner is None else runner
    policy_tail = (
        "Kinaiya refuses to upgrade, downgrade, or remove existing system packages, "
        "and never runs `sudo apt-get update` or `sudo apt-get upgrade` for you."
    )

    pre_snapshot = _active_dpkg_snapshot(
        status_path=status_path, env=env, runner=active_runner
    )
    if expected_snapshot is not None and pre_snapshot != expected_snapshot:
        raise BootstrapError(
            "Out-of-date system blocker: system package state changed between preflight inspection "
            f"and dependency installation. {policy_tail}"
        )

    already_present = []
    for spec in packages:
        pkg_name, _ = _validate_apt_package_spec(spec, allow_version_pin=True)
        base_name = pkg_name.split(":", 1)[0]
        existing_ver = pre_snapshot.get(pkg_name) or pre_snapshot.get(base_name)
        if existing_ver is not None:
            already_present.append(f"{pkg_name} ({existing_ver})")
    if already_present:
        raise BootstrapError(
            f"Out-of-date system blocker: refusing to install ({', '.join(packages)}) because "
            f"requested package(s) [{', '.join(already_present)}] are already installed. {policy_tail}"
        )

    sim_cmd = apt_simulation_command(packages)
    sim_env = {
        **(os.environ if env is None else env),
        "DEBIAN_FRONTEND": "noninteractive",
        "LC_ALL": "C",
        "LANG": "C",
    }
    try:
        sim_result = active_runner(
            sim_cmd, capture_output=True, text=True, timeout=120, env=sim_env
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError(
            f"Failed to simulate dependency installation ({', '.join(packages)}) via `{' '.join(sim_cmd)}`: {exc}"
        ) from None

    parsed = parse_apt_simulation_output(
        getattr(sim_result, "stdout", "") or "",
        getattr(sim_result, "stderr", "") or "",
        returncode=getattr(sim_result, "returncode", 1),
        requested_packages=packages,
        installed_snapshot=pre_snapshot,
    )

    post_snapshot = _active_dpkg_snapshot(
        status_path=status_path, env=env, runner=active_runner
    )
    if post_snapshot != pre_snapshot:
        raise BootstrapError(
            "Out-of-date system blocker: system package state changed during apt-get solver verification. "
            f"{policy_tail}"
        )
    for pkg, _ver in parsed["new_installs"]:
        base_pkg = pkg.split(":", 1)[0]
        if pkg in post_snapshot or base_pkg in post_snapshot:
            existing_ver = post_snapshot.get(pkg) or post_snapshot.get(base_pkg)
            raise BootstrapError(
                f"Out-of-date system blocker: package {pkg} ({existing_ver}) is already installed "
                f"in dpkg status. {policy_tail}"
            )
    if expected_pinned is not None and parsed["pinned_packages"] != list(expected_pinned):
        raise BootstrapError(
            "Out-of-date system blocker: apt-get solver transaction changed between preflight inspection "
            f"and execution ({parsed['pinned_packages']} != {list(expected_pinned)}). {policy_tail}"
        )

    return {
        **parsed,
        "installed_snapshot": pre_snapshot,
        "simulation_command": sim_cmd,
        "install_command": dependency_install_command(parsed["pinned_packages"]),
    }


def install_missing_dependencies(
    missing,
    *,
    env=None,
    runner=None,
    status_path=None,
    expected_snapshot=None,
    expected_pinned=None,
):
    if not missing:
        return []
    for pkg in missing:
        require(
            isinstance(pkg, str) and re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", pkg),
            f"Invalid dependency name: {pkg!r}",
        )
    active_runner = subprocess.run if runner is None else runner
    verified = verify_apt_no_existing_package_changes(
        missing,
        env=env,
        runner=active_runner,
        status_path=status_path,
        expected_snapshot=expected_snapshot,
        expected_pinned=expected_pinned,
    )
    current_snapshot = _active_dpkg_snapshot(
        status_path=status_path, env=env, runner=active_runner
    )
    if current_snapshot != verified["installed_snapshot"]:
        raise BootstrapError(
            "Out-of-date system blocker: system package state changed before executing apt-get install. "
            "Kinaiya refuses to upgrade, downgrade, or remove existing system packages, "
            "and never runs `sudo apt-get update` or `sudo apt-get upgrade` for you."
        )
    cmd = verified["install_command"]
    exec_env = {
        **(os.environ if env is None else env),
        "DEBIAN_FRONTEND": "noninteractive",
        "LC_ALL": "C",
        "LANG": "C",
    }
    result = active_runner(cmd, capture_output=True, text=True, timeout=300, env=exec_env)
    if _ACTIVE_BOOTSTRAP_REPORTER is not None:
        _ACTIVE_BOOTSTRAP_REPORTER.log(
            f"command={' '.join(cmd)} exit_code={result.returncode}\n"
            f"stdout={(getattr(result, 'stdout', '') or '').strip()}\n"
            f"stderr={(getattr(result, 'stderr', '') or '').strip()}",
            stage="dependencies",
        )
    if result.returncode != 0:
        detail = redact_install_log((getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip())
        suffix = f" ({detail.splitlines()[-1]})" if detail else ""
        raise BootstrapError(
            f"Out-of-date system blocker: could not install missing dependencies ({', '.join(missing)}) "
            f"with pinned `--no-upgrade --no-remove`{suffix}. An existing installed package or your local apt "
            "package index is out of date. Please update that specific package or refresh your package index "
            "manually first; Kinaiya refuses to upgrade, downgrade, or remove existing system packages, and "
            "never runs `sudo apt-get update` or `sudo apt-get upgrade` for you."
        )
    return list(missing)


def check_outdated_system_blockers():
    if sys.version_info[:2] < (3, 11):
        raise BootstrapError(
            f"Out-of-date system blocker: Python {sys.version_info[0]}.{sys.version_info[1]} is out of date "
            "(Kinaiya requires Python 3.11-3.13). Please update Python first; "
            "the installer will not update existing system packages for you."
        )
    if platform.system() != "Linux":
        return
    try:
        distro = platform.freedesktop_os_release()
    except OSError:
        distro = {}
    distro_id = distro.get("ID")
    version_id = distro.get("VERSION_ID", "")
    if distro_id == "ubuntu" and version_id not in {"22.04", "24.04"}:
        parsed_ubuntu = _parse_version_tuple(version_id)
        if parsed_ubuntu is not None and parsed_ubuntu < (22, 4):
            raise BootstrapError(
                f"Out-of-date system blocker: Ubuntu {version_id} is out of date "
                "(Kinaiya requires Ubuntu 22.04 or 24.04). Please update Ubuntu first; "
                "the installer will not update your system for you."
            )
    bwrap_bin = shutil.which("bwrap")
    if bwrap_bin is not None:
        bwrap_ver = installed_package_version("bubblewrap")
        if bwrap_ver is None:
            try:
                ver_probe = subprocess.run([bwrap_bin, "--version"], capture_output=True, text=True, timeout=5)
                bwrap_ver = getattr(ver_probe, "stdout", None)
            except (OSError, subprocess.SubprocessError):
                bwrap_ver = None
        parsed_bwrap = _parse_version_tuple(bwrap_ver)
        if parsed_bwrap is not None and parsed_bwrap < MIN_BUBBLEWRAP_VERSION:
            raise BootstrapError(
                f"Out-of-date system blocker: bubblewrap {bwrap_ver.strip()} is out of date "
                "(Kinaiya requires bubblewrap >= 0.6.1). Please update bubblewrap first; "
                "the installer will not update existing system packages for you."
            )
    if ctypes.util.find_library("seccomp") is not None:
        seccomp_ver = installed_package_version("libseccomp2")
        parsed_seccomp = _parse_version_tuple(seccomp_ver)
        if parsed_seccomp is not None and parsed_seccomp < MIN_LIBSECCOMP_VERSION:
            raise BootstrapError(
                f"Out-of-date system blocker: libseccomp2 {seccomp_ver.strip()} is out of date "
                "(Kinaiya requires libseccomp2 >= 2.5). Please update libseccomp2 first; "
                "the installer will not update existing system packages for you."
            )


def preflight(*, allow_missing_dependencies=False):
    require(platform.system() == "Linux" and "microsoft" not in platform.release().lower(), "Native Linux required; WSL/Windows/macOS are not qualified")
    require(os.geteuid() != 0, "Never run the installer as root; the CLI invokes sudo only for missing dependencies")
    check_outdated_system_blockers()
    distro = platform.freedesktop_os_release()
    require(distro.get("ID") == "ubuntu" and distro.get("VERSION_ID") in {"22.04", "24.04"}, "Candidate baseline: Ubuntu 22.04/24.04 only")
    require(platform.machine() in {"x86_64", "aarch64"} and (3, 11) <= sys.version_info[:2] <= (3, 13), "Require x86_64/aarch64 Python 3.11-3.13 with venv/ensurepip")
    require(shutil.disk_usage(Path.home()).free >= 1024**3, "At least 1 GiB free disk is required")
    missing = missing_dependencies()
    if missing and not allow_missing_dependencies:
        sim = " ".join(apt_simulation_command(missing))
        cmd = " ".join(dependency_install_command(missing))
        raise BootstrapError(
            f"Missing required system dependencies: {', '.join(missing)}. "
            f"Verify with `{sim}` before `{cmd}` "
            "(pins exact package=version; refuses any upgrade, downgrade, or removal; never runs apt-get update/upgrade)"
        )
    binary = shutil.which("bwrap")
    if binary is not None:
        result = subprocess.run([binary, "--unshare-all", "--die-with-parent", "--new-session", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--clearenv", "/usr/bin/true"], capture_output=True, timeout=15)
        require(result.returncode == 0, "bubblewrap namespace probe denied; check kernel/user-namespace policy. Never disable isolation to install")


def open_url(url):
    class Redirects(HTTPRedirectHandler):
        max_redirections = 3
        max_repeats = 1

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            parsed = urlsplit(newurl)
            require(url.startswith(ROOT + "/download/") and parsed.scheme == "https" and parsed.netloc in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"} and not parsed.fragment, "Unsafe bootstrap redirect")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    allowed = url.startswith(API + "/") or re.fullmatch(re.escape(API) + r"\?per_page=100&page=[1-5]", url) or url.startswith(ROOT + "/download/")
    require(allowed and urlsplit(url).scheme == "https", "Unofficial bootstrap URL")
    return build_opener(Redirects()).open(Request(url, headers={"User-Agent": "Kinaiya-bootstrap/1"}), timeout=20)


def read_json(url):
    with open_url(url) as response:
        raw = response.read(1024**2 + 1)
    require(len(raw) <= 1024**2, "Bootstrap metadata exceeds size bound")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "Duplicate bootstrap metadata key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def resolve(channel):
    try:
        if channel == "stable":
            metadata = read_json(API + "/latest")
        else:
            candidates, builds = [], set()
            for page in range(1, 6):
                entries = read_json(API + f"?per_page=100&page={page}")
                require(isinstance(entries, list) and len(entries) <= 100, "Invalid nightly release page")
                for entry in entries:
                    require(isinstance(entry, dict), "Invalid release entry")
                    tag = entry.get("tag_name", "")
                    if entry.get("draft") is False and entry.get("prerelease") is True and isinstance(tag, str) and tag.startswith("nightly-"):
                        match = re.fullmatch(r"nightly-([1-9][0-9]{0,18})-([0-9a-f]{12})", tag)
                        require(match is not None, "Malformed nightly tag")
                        build = int(match[1])
                        require(build not in builds, "Ambiguous nightly build")
                        builds.add(build)
                        candidates.append((build, entry))
                if len(entries) < 100:
                    break
            else:
                raise BootstrapError("Nightly pagination bound reached; selection refused")
            require(candidates, "No qualified nightly release is published")
            metadata = max(candidates, key=lambda item: item[0])[1]
    except HTTPError as exc:
        if channel == "stable" and exc.code == 404:
            raise BootstrapError("No approved stable release is published; no fallback to main or nightly") from None
        raise BootstrapError("Release feed unavailable (HTTP error); retry explicitly") from None
    require(isinstance(metadata, dict) and metadata.get("draft") is False and metadata.get("prerelease") is (channel == "nightly"), "Release channel/draft mismatch")
    tag = metadata.get("tag_name")
    pattern = rf"v{VERSION}" if channel == "stable" else r"nightly-[1-9][0-9]{0,18}-[0-9a-f]{12}"
    require(isinstance(tag, str) and re.fullmatch(pattern, tag) and metadata.get("html_url") == ROOT + "/tag/" + tag, "Invalid immutable release origin/tag")
    assets = metadata.get("assets")
    require(isinstance(assets, list) and 1 <= len(assets) <= 33, "Invalid release assets")
    indexed = {}
    for asset in assets:
        require(isinstance(asset, dict) and isinstance(asset.get("name"), str) and asset["name"] not in indexed and asset.get("state") == "uploaded", "Ambiguous/incomplete release assets")
        indexed[asset["name"]] = asset
    manifest_asset = indexed.get("release-manifest.json", {})
    require(manifest_asset.get("browser_download_url") == ROOT + f"/download/{tag}/release-manifest.json" and type(manifest_asset.get("size")) is int and 0 < manifest_asset["size"] <= 1024**2, "Invalid release manifest asset")
    manifest = read_json(manifest_asset["browser_download_url"])
    require(isinstance(manifest, dict) and manifest.get("channel") == channel and type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1, "Invalid release manifest")
    sha = manifest.get("source_sha")
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) and metadata.get("target_commitish") == sha, "Release must pin an exact immutable source SHA")
    expected_tag = "v" + str(manifest.get("version")) if channel == "stable" else f"nightly-{manifest.get('build_number')}-{sha[:12]}"
    require(expected_tag == tag, "Manifest/tag identity mismatch")
    artifacts = manifest.get("artifacts")
    require(isinstance(artifacts, list) and 1 <= len(artifacts) <= 32 and all(isinstance(a, dict) for a in artifacts), "Invalid artifacts")
    sources = [a for a in artifacts if a.get("platform") == "linux" and a.get("architecture") == "any"]
    require(len(sources) == 1, "Exactly one Linux source artifact is required")
    source = sources[0]
    name = source.get("name")
    require(isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._+-]*-source\.tar\.gz", name), "Invalid source name")
    require(source.get("url") == ROOT + f"/download/{tag}/{name}" and indexed.get(name, {}).get("browser_download_url") == source["url"] and indexed.get(name, {}).get("size") == source.get("size"), "Invalid official source artifact URL/size")
    require(type(source.get("size")) is int and 0 < source["size"] <= MAX_BYTES and isinstance(source.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", source["sha256"]), "Invalid source digest/size bound")
    return manifest, source


def download(artifact, target):
    digest, total, started = hashlib.sha256(), 0, time.monotonic()
    size = artifact.get("size")
    with open_url(artifact["url"]) as response, target.open("xb") as output:
        while True:
            require(time.monotonic() - started <= 600, "Bootstrap download time bound exceeded")
            chunk = response.read(min(1024**2, size - total + 1 if type(size) is int and size > 0 else 1024**2))
            if not chunk:
                break
            total += len(chunk)
            if type(size) is int and size > 0:
                require(total <= size, "Bootstrap artifact exceeds size bound")
            digest.update(chunk)
            output.write(chunk)
            if _ACTIVE_BOOTSTRAP_REPORTER is not None:
                _ACTIVE_BOOTSTRAP_REPORTER.update_download(total, size)
    require(total == size and digest.hexdigest() == artifact["sha256"], "Bootstrap artifact size/digest mismatch")
    if _ACTIVE_BOOTSTRAP_REPORTER is not None:
        _ACTIVE_BOOTSTRAP_REPORTER._sha256_verified = True


class _BootstrapTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # Reject hidden extension headers before tarfile allocates their metadata.
        require(self.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE), "Unsafe archive: only regular files/directories; extension headers refused")
        return super()._proc_member(archive)


def unpack(archive, destination):
    # Validate the complete gzip envelope before creating or importing any files.
    maximum, total = MAX_BYTES + 20000 * 1024 + 10240, 0
    try:
        with gzip.open(archive, "rb") as stream:
            while chunk := stream.read(min(1024**2, maximum - total + 1)):
                total += len(chunk)
                require(total <= maximum, "Bootstrap gzip decompression bound exceeded")
    except (OSError, EOFError, zlib.error):
        raise BootstrapError("Invalid or truncated gzip stream") from None
    destination.mkdir(mode=0o700)
    names, total = set(), 0
    with tarfile.open(archive, "r|gz", tarinfo=_BootstrapTarInfo) as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            total += member.size
            require(len(names) < 20000 and total <= MAX_BYTES, "Bootstrap extraction bound exceeded")
            require(path.parts and not path.is_absolute() and ".." not in path.parts and "\\" not in member.name and ":" not in member.name and all(ord(c) >= 32 for c in member.name) and (member.isfile() or member.isdir()) and not member.sparse and member.name.rstrip("/") == str(path) and str(path) not in names, "Unsafe bootstrap archive member")
            names.add(str(path))
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(member) as incoming, target.open("xb") as output:
                    shutil.copyfileobj(incoming, output, length=1024**2)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
    require((destination / "pyproject.toml").is_file() and (destination / "requirements-runtime.lock").is_file(), "Bootstrap requires a flat release source bundle and runtime lock")
    return destination


@contextmanager
def open_terminal():
    # Buffered r+ requires seeking, which controlling terminals do not support.
    with open("/dev/tty", "r+b", buffering=0) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", line_buffering=True, write_through=True) as terminal:
            yield terminal


def run(argv=None, *, invoking_dir=None):
    global _ACTIVE_BOOTSTRAP_REPORTER
    if invoking_dir is None:
        env_pwd = os.environ.get("KINAIYA_INVOKING_PWD", "").strip()
        if env_pwd:
            invoking_dir = Path(env_pwd)
        elif not os.environ.get("KINAIYA_INSTALL_ROOT"):
            invoking_dir = Path.cwd()
    parser = argparse.ArgumentParser(description="Verified native Linux Kinaiya bootstrap; installs missing dependencies without upgrading existing packages")
    parser.add_argument("--channel", choices=("stable", "nightly"), default="stable")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-setup", action="store_true")
    parser.add_argument("--json", action="store_true", help="Emit deterministic machine-readable JSON output")
    parser.add_argument("--install-dir", type=Path, help="Explicit target directory for managed Kinaiya install root")
    parser.add_argument("--install-location", help="Install root selection: 'default', 'current', or an absolute directory path")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--model")
    parser.add_argument("--credential-env")
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--converse", action="store_true")
    args = parser.parse_args(argv)
    home = Path.home()
    cache = home / ".cache/kinaiya"
    reporter = BootstrapStageReporter(log_path=cache / "install.log", json_output=args.json, defer_log=True)
    _ACTIVE_BOOTSTRAP_REPORTER = reporter
    try:
        reporter.start_stage("preflight")
        try:
            try:
                preflight(allow_missing_dependencies=True)
                missing = missing_dependencies()
            except TypeError:
                preflight()
                missing = []
        except BootstrapError as exc:
            reporter.fail_stage("preflight", str(exc))
            raise
        reporter.complete_stage("preflight")
        selected_install = None
        if args.yes or args.install_dir is not None or args.install_location is not None or existing_install_root(home) is not None:
            selected_install, _ = select_install_root(
                home,
                install_dir=args.install_dir,
                install_location=args.install_location,
                invoking_dir=invoking_dir,
                workspace=args.workspace,
                data_dir=args.data_dir,
                interactive=False,
            )
        manifest, artifact = resolve(args.channel)
        if not args.yes:
            with open_terminal() as tty:
                pre_consented = False
                if selected_install is None:
                    selected_install, pre_consented = select_install_root(
                        home,
                        install_dir=args.install_dir,
                        install_location=args.install_location,
                        invoking_dir=invoking_dir,
                        workspace=args.workspace,
                        data_dir=args.data_dir,
                        interactive=True,
                        tty=tty,
                    )
                plan_summary = {"channel": args.channel, "source_sha": manifest["source_sha"], "download_bytes": artifact["size"], "install": str(selected_install), "dependencies": "hash-locked isolated venv; no voice/model downloads", "trust": "official GitHub HTTPS plus artifact SHA-256, not publisher signing"}
                if missing:
                    plan_summary["missing_system_dependencies"] = missing
                    plan_summary["system_dependency_check"] = " ".join(apt_simulation_command(missing))
                    plan_summary["system_dependency_command"] = " ".join(dependency_install_command(missing))
                    plan_summary["system_dependency_policy"] = (
                        "solver-verified exact package=version install; refuses any upgrade, downgrade, or removal of existing packages"
                    )
                print(json.dumps(plan_summary), flush=True)
                tty.write("Review the plan. Type yes to download and install: ")
                confirm_line = tty.readline()
                require(
                    confirm_line.strip() == "yes" or (pre_consented and confirm_line == ""),
                    "Installation cancelled; no files changed",
                )
        else:
            plan_summary = {"channel": args.channel, "source_sha": manifest["source_sha"], "download_bytes": artifact["size"], "install": str(selected_install), "dependencies": "hash-locked isolated venv; no voice/model downloads", "trust": "official GitHub HTTPS plus artifact SHA-256, not publisher signing"}
            if missing:
                plan_summary["missing_system_dependencies"] = missing
                plan_summary["system_dependency_check"] = " ".join(apt_simulation_command(missing))
                plan_summary["system_dependency_command"] = " ".join(dependency_install_command(missing))
                plan_summary["system_dependency_policy"] = (
                    "solver-verified exact package=version install; refuses any upgrade, downgrade, or removal of existing packages"
                )
            print(json.dumps(plan_summary), flush=True)
        require(cache.resolve() == cache.absolute(), "Bootstrap cache cannot use symlink aliases")
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        reporter.enable_log_persistence()
        reporter.start_stage("dependencies")
        try:
            if missing:
                install_missing_dependencies(missing)
                preflight()
            reporter.complete_stage("dependencies")
        except BootstrapError as exc:
            reporter.fail_stage("dependencies", str(exc))
            raise
        with tempfile.TemporaryDirectory(prefix="bootstrap-", dir=cache) as temporary:
            temporary = Path(temporary)
            archive = temporary / "source.tar.gz"
            reporter.start_stage("download")
            try:
                download(artifact, archive)
                reporter.complete_stage("download", sha256_verified=True)
            except BootstrapError as exc:
                reporter.fail_stage("download", str(exc))
                raise
            reporter.start_stage("staging")
            try:
                source = unpack(archive, temporary / "source")
                sys.path.insert(0, str(source))
                # Only code from the official digest-verified immutable source is imported.
                # The shared full #262 contract validates all remaining compatibility fields.
                from dataclasses import replace
                from packages.distribution.releases import validate_manifest
                from services.distribution.installation import Layout, install_release, preflight as native_preflight
                validate_manifest(manifest, channel=args.channel)
                identity = json.loads((source / "release-identity.json").read_text())
                require(all(identity.get(key) == manifest.get(key) for key in identity) and identity.get("source_sha") == manifest["source_sha"], "Bundled source identity mismatch")
                try:
                    layout = Layout.for_home(home, install_root=selected_install)
                except TypeError:
                    layout = replace(Layout.for_home(home), install=selected_install)
                layout = replace(layout, workspace=args.workspace or layout.workspace, data=args.data_dir or layout.data)
                native_preflight(layout)
                install_release(layout, manifest, archive)
                _atomic_write_json(layout.config.parent / "install-root.json", {"install": str(layout.install)})
                reporter.complete_stage("staging")
            except BaseException as exc:
                if not isinstance(exc, SystemExit):
                    reporter.fail_stage("staging", str(exc))
                raise
            print(json.dumps({"installed": manifest["source_sha"], "install": str(layout.install), "launcher": str(layout.bin / "kinaiya"), "log": str(reporter.log_path), "PATH": 'Add "$HOME/.local/bin" to PATH explicitly', "ready": False}), flush=True)
        if not args.no_setup and not layout.config.exists():
            reporter.start_stage("setup_handoff")
            # The bootstrap interpreter has no runtime dependencies. Setup must execute
            # in the verified managed venv, not import the native launcher into system Python.
            setup_cmd = [str(layout.bin / "kinaiya"), "setup"]
            if args.endpoint or args.model:
                if args.yes:
                    setup_cmd.append("--yes")
                if args.endpoint:
                    setup_cmd.extend(["--endpoint", args.endpoint])
                if args.model:
                    setup_cmd.extend(["--model", args.model])
                if args.credential_env:
                    setup_cmd.extend(["--credential-env", args.credential_env])
                if args.allow_cloud:
                    setup_cmd.append("--allow-cloud")
                if args.converse:
                    setup_cmd.append("--converse")
            result = subprocess.run(setup_cmd, check=False)
            if result.returncode != 0:
                reporter.fail_stage("setup_handoff", "Code installed; setup incomplete. Run kinaiya setup explicitly")
                raise BootstrapError("Code installed; setup incomplete. Run kinaiya setup explicitly")
            reporter.complete_stage("setup_handoff")
    finally:
        _ACTIVE_BOOTSTRAP_REPORTER = None


if __name__ == "__main__":
    try:
        run()
    except (RuntimeError, OSError, ValueError, RecursionError, tarfile.TarError, subprocess.SubprocessError) as exc:
        if isinstance(exc, BootstrapError):
            print(f"kinaiya: {exc}", file=sys.stderr)
        else:
            print("kinaiya: bootstrap failed; check network, Python/venv, permissions and kernel isolation; preserve existing installation", file=sys.stderr)
        raise SystemExit(1)
# END PYTHON BOOTSTRAP
KINAIYA_BOOTSTRAP_PY
