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


class BootstrapError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise BootstrapError(message)


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


def installed_package_version(package, status_path=Path("/var/lib/dpkg/status")):
    try:
        text = status_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if ":" in line and not line[:1].isspace():
                key, val = line.split(":", 1)
                fields[key.strip()] = val.strip()
        if fields.get("Package") == package and "installed" in fields.get("Status", "").split():
            return fields.get("Version")
    return None


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


def dependency_install_command(missing):
    return ["sudo", "apt-get", "install", "--no-upgrade", "-y", *missing]


def install_missing_dependencies(missing):
    if not missing:
        return []
    for pkg in missing:
        require(isinstance(pkg, str) and re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", pkg), f"Invalid dependency name: {pkg!r}")
    cmd = dependency_install_command(missing)
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    if result.returncode != 0:
        detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        suffix = f" ({detail.splitlines()[-1]})" if detail else ""
        raise BootstrapError(
            f"Out-of-date system blocker: could not install missing dependencies ({', '.join(missing)}) "
            f"with `--no-upgrade`{suffix}. An existing installed package or your local apt package index "
            "is out of date. Please update that specific package or refresh your package index manually "
            "first; Kinaiya never runs `sudo apt-get update` or `sudo apt-get upgrade` for you."
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
        cmd = " ".join(dependency_install_command(missing))
        raise BootstrapError(
            f"Missing required system dependencies: {', '.join(missing)}. "
            f"Run `{cmd}` (never runs apt-get update/upgrade)"
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
    with open_url(artifact["url"]) as response, target.open("xb") as output:
        while True:
            require(time.monotonic() - started <= 600, "Bootstrap download time bound exceeded")
            chunk = response.read(min(1024**2, artifact["size"] - total + 1))
            if not chunk:
                break
            total += len(chunk)
            require(total <= artifact["size"], "Bootstrap artifact exceeds size bound")
            digest.update(chunk)
            output.write(chunk)
    require(total == artifact["size"] and digest.hexdigest() == artifact["sha256"], "Bootstrap artifact size/digest mismatch")


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
    try:
        preflight(allow_missing_dependencies=True)
        missing = missing_dependencies()
    except TypeError:
        preflight()
        missing = []
    home = Path.home()
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
                plan_summary["system_dependency_command"] = " ".join(dependency_install_command(missing))
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
            plan_summary["system_dependency_command"] = " ".join(dependency_install_command(missing))
        print(json.dumps(plan_summary), flush=True)
    if missing:
        install_missing_dependencies(missing)
        preflight()
    cache = home / ".cache/kinaiya"
    require(cache.resolve() == cache.absolute(), "Bootstrap cache cannot use symlink aliases")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="bootstrap-", dir=cache) as temporary:
        temporary = Path(temporary)
        archive = temporary / "source.tar.gz"
        download(artifact, archive)
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
        print(json.dumps({"installed": manifest["source_sha"], "install": str(layout.install), "launcher": str(layout.bin / "kinaiya"), "PATH": 'Add "$HOME/.local/bin" to PATH explicitly', "ready": False}), flush=True)
    if not args.no_setup and not layout.config.exists():
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
        require(result.returncode == 0, "Code installed; setup incomplete. Run kinaiya setup explicitly")


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
