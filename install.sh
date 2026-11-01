#!/bin/sh
# Standalone native Linux bootstrap, Apache-2.0. Review before execution.
# Trust: official GitHub HTTPS metadata + immutable release assets + SHA-256,
# not an independent publisher signature. Never fetch or execute main/latest code.
set -eu
exec "${KINAIYA_PYTHON:-python3}" -I - "$@" <<'KINAIYA_BOOTSTRAP_PY'
# BEGIN PYTHON BOOTSTRAP
import argparse
import ctypes.util
import hashlib
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
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.github.com/repos/rgoza1996/Kinaiya/releases"
ROOT = "https://github.com/rgoza1996/Kinaiya/releases"
MAX_BYTES = 512 * 1024**2
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


class BootstrapError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise BootstrapError(message)


def preflight():
    import ensurepip  # noqa: F401
    import venv  # noqa: F401
    require(platform.system() == "Linux" and "microsoft" not in platform.release().lower(), "Native Linux required; WSL/Windows/macOS are not qualified")
    require(os.geteuid() != 0, "Never run the installer as root; install system prerequisites separately")
    distro = platform.freedesktop_os_release()
    require(distro.get("ID") == "ubuntu" and distro.get("VERSION_ID") in {"22.04", "24.04"}, "Candidate baseline: Ubuntu 22.04/24.04 only")
    require(platform.machine() in {"x86_64", "aarch64"} and (3, 11) <= sys.version_info[:2] <= (3, 13), "Require x86_64/aarch64 Python 3.11-3.13 with venv/ensurepip")
    require(shutil.disk_usage(Path.home()).free >= 1024**3, "At least 1 GiB free disk is required")
    binary = shutil.which("bwrap")
    require(binary and ctypes.util.find_library("seccomp"), "Install bubblewrap and libseccomp2 separately; no automatic sudo/package installation")
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


def unpack(archive, destination):
    destination.mkdir(mode=0o700)
    names, total = set(), 0
    with tarfile.open(archive, "r|gz") as tar:
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


def run(argv=None):
    parser = argparse.ArgumentParser(description="Verified native Linux Kinaiya bootstrap; never installs system packages")
    parser.add_argument("--channel", choices=("stable", "nightly"), default="stable")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-setup", action="store_true")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    preflight()
    manifest, artifact = resolve(args.channel)
    print(json.dumps({"channel": args.channel, "source_sha": manifest["source_sha"], "download_bytes": artifact["size"], "install": str(Path.home() / ".local/share/kinaiya/install"), "dependencies": "hash-locked isolated venv; no voice/model downloads", "trust": "official GitHub HTTPS plus artifact SHA-256, not publisher signing"}), flush=True)
    if not args.yes:
        with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as tty:
            tty.write("Review the plan. Type yes to download and install: ")
            require(tty.readline().strip() == "yes", "Installation cancelled; no files changed")
    cache = Path.home() / ".cache/kinaiya"
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
        layout = Layout.for_home(Path.home())
        layout = replace(layout, workspace=args.workspace or layout.workspace, data=args.data_dir or layout.data)
        native_preflight(layout)
        install_release(layout, manifest, archive)
        print(json.dumps({"installed": manifest["source_sha"], "launcher": str(layout.bin / "kinaiya"), "PATH": 'Add "$HOME/.local/bin" to PATH explicitly', "ready": False}), flush=True)
    if not args.no_setup and not layout.config.exists():
        # The bootstrap interpreter has no runtime dependencies. Setup must execute
        # in the verified managed venv, not import the native launcher into system Python.
        result = subprocess.run([str(layout.bin / "kinaiya"), "setup"], check=False)
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
