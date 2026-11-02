"""Official stable/nightly feed contracts (#262)."""

import gzip
import hashlib
import json
import re
import zlib
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.github.com/repos/rgoza1996/Kinaiya/releases"
ROOT = "https://github.com/rgoza1996/Kinaiya/releases"
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_PAGES = 5
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


class ReleaseError(ValueError):
    """An actionable release refusal, never an implicit source checkout fallback."""

    def __init__(self, message, code="invalid_release"):
        super().__init__(message)
        self.code = code


def _require(condition, message):
    if not condition:
        raise ReleaseError(message)


def _integer(value, name, maximum=2**31 - 1):
    _require(type(value) is int and 1 <= value <= maximum, f"Invalid {name}")
    return value


def release_tag(data):
    """Derive the immutable tag from an already validated identity."""
    if data["channel"] == "stable":
        return f"v{data['version']}"
    return f"nightly-{data['build_number']}-{data['source_sha'][:12]}"


def _asset_url(url, tag, name):
    _require(isinstance(url, str), "Invalid artifact URL")
    _require(
        url == f"{ROOT}/download/{tag}/{name}",
        "Artifact must use the official repository and immutable release tag",
    )


def validate_identity(data, *, channel=None):
    """Validate v1 immutable identity and compatibility without artifact fields."""
    _require(isinstance(data, dict), "Manifest must be an object")
    _require(
        type(data.get("schema_version")) is int and data["schema_version"] == 1,
        "Unsupported manifest schema",
    )
    selected = data.get("channel")
    _require(selected in ("stable", "nightly"), "Unsupported channel")
    _require(channel is None or selected == channel, "Channel mismatch")
    build = _integer(data.get("build_number"), "build_number", 2**63 - 1)
    sha = data.get("source_sha")
    _require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid source_sha")
    version = data.get("version")
    pattern = VERSION if selected == "stable" else rf"{VERSION}-nightly\.{build}\+{sha[:12]}"
    _require(
        isinstance(version, str) and len(version) <= 128 and re.fullmatch(pattern, version),
        "Invalid channel version",
    )
    for field in ("protocol_min", "protocol_max", "state_schema", "min_updater"):
        _integer(data.get(field), field)
    _require(data["protocol_min"] <= data["protocol_max"], "Inverted protocol range")
    return data


def validate_manifest(data, *, channel=None):
    """Validate v1 identity, compatibility and bounded artifacts; return the input."""
    validate_identity(data, channel=channel)
    artifacts = data.get("artifacts")
    _require(isinstance(artifacts, list) and 1 <= len(artifacts) <= 32, "Invalid artifacts")
    names, targets = set(), set()
    tag = release_tag(data)
    for artifact in artifacts:
        _require(isinstance(artifact, dict), "Invalid artifact")
        name = artifact.get("name")
        _require(
            isinstance(name, str)
            and len(name) <= 200
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._+-]*", name),
            "Invalid artifact name",
        )
        platform, arch = artifact.get("platform"), artifact.get("architecture")
        _require(platform in ("linux", "web", "android", "ios"), "Unsupported artifact platform")
        _require(arch in ("any", "x86_64", "aarch64", "arm64"), "Unsupported architecture")
        _require(name not in names and (platform, arch) not in targets, "Ambiguous artifacts")
        names.add(name)
        targets.add((platform, arch))
        digest = artifact.get("sha256")
        _require(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest), "Invalid sha256"
        )
        _integer(artifact.get("size"), "artifact size", MAX_ASSET_BYTES)
        _asset_url(artifact.get("url"), tag, name)
    _require(("linux", "any") in targets, "Missing required Linux source artifact")
    source = next(a for a in artifacts if (a["platform"], a["architecture"]) == ("linux", "any"))
    _require(source["name"].endswith("-source.tar.gz"), "Invalid source artifact format")
    return data


def _validate_start_url(url):
    asset_pattern = rf"{re.escape(ROOT)}/download/(?:v{VERSION}|nightly-[1-9][0-9]{{0,18}}-[0-9a-f]{{12}})/[a-zA-Z0-9][a-zA-Z0-9._+-]*"
    _require(
        isinstance(url, str)
        and (
            url == f"{API}/latest"
            or re.fullmatch(rf"{re.escape(API)}\?per_page=100&page=[1-{MAX_PAGES}]", url)
            or re.fullmatch(asset_pattern, url)
        ),
        "Unofficial or mutable release URL",
    )


def validate_redirect(source, target):
    """Only immutable official assets may redirect to GitHub's asset CDN."""
    _validate_start_url(source)
    parsed = urlsplit(target)
    _require(
        source.startswith(f"{ROOT}/download/")
        and parsed.scheme == "https"
        and parsed.netloc
        in ("release-assets.githubusercontent.com", "objects.githubusercontent.com")
        and not parsed.fragment,
        "Unsafe release redirect",
    )
    return True


def open_release_url(url):
    """Open an allowlisted public URL with bounded redirects and no credentials."""
    _validate_start_url(url)

    class Redirects(HTTPRedirectHandler):
        max_redirections = 3
        max_repeats = 1

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            validate_redirect(url, newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    response = build_opener(Redirects()).open(
        Request(url, headers={"User-Agent": "Kinaiya-release/1", "Accept": "application/json"}),
        timeout=20,
    )
    try:
        if response.geturl() != url:
            validate_redirect(url, response.geturl())
    except Exception:
        response.close()
        raise
    return response


def fetch_release_json(url):
    """Read at most 1 MiB; reject duplicate keys and malformed/deep JSON."""

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    with open_release_url(url) as response:
        raw = response.read(MAX_JSON_BYTES + 1)
    _require(len(raw) <= MAX_JSON_BYTES, "Release JSON exceeds size bound")
    try:
        return json.loads(raw, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReleaseError("Malformed release JSON") from exc


def select_artifact(manifest, platform, architecture):
    """Select an exact architecture or platform's architecture-independent asset."""
    validate_manifest(manifest)
    for arch in (architecture, "any"):
        matches = [
            a
            for a in manifest["artifacts"]
            if a["platform"] == platform and a["architecture"] == arch
        ]
        if matches:
            return matches[0]
    raise ReleaseError(f"No release artifact for {platform}/{architecture}", "unsupported_platform")


def verify_artifact(path, artifact):
    """Bound and stream the digest check before opening an archive or executing."""
    path = Path(path)
    _require(not path.is_symlink() and path.is_file(), "Invalid artifact file")
    expected = _integer(artifact.get("size"), "artifact size", MAX_ASSET_BYTES)
    _require(path.stat().st_size == expected, "Artifact size mismatch")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(min(1024 * 1024, expected - total + 1)):
            total += len(chunk)
            _require(total <= expected, "Artifact size changed during verification")
            digest.update(chunk)
    _require(total == expected, "Artifact size changed during verification")
    _require(digest.hexdigest() == artifact.get("sha256"), "Artifact digest mismatch")


def verify_gzip_archive(path, max_uncompressed):
    """Consume the full bounded envelope so gzip verifies CRC, size and trailer.

    Tar iteration stops at end-of-archive padding without necessarily checking
    the enclosing gzip trailer. Count headers/padding too, not just tar members.
    """
    total = 0
    try:
        with gzip.open(path, "rb") as stream:
            while chunk := stream.read(min(1024 * 1024, max_uncompressed - total + 1)):
                total += len(chunk)
                _require(total <= max_uncompressed, "Gzip decompression bound exceeded")
    except (OSError, EOFError, zlib.error) as exc:
        raise ReleaseError("Invalid or truncated gzip stream") from exc


def update_status(manifest, installed_build):
    """Compare within a channel; a rollback requires explicit recovery, never auto-downgrade."""
    validate_manifest(manifest)
    _integer(installed_build, "installed build", 2**63 - 1)
    if manifest["build_number"] < installed_build:
        raise ReleaseError("Release is older than the installed build", "stale_release")
    return "current" if manifest["build_number"] == installed_build else "available"


def resolve_release(channel, fetch_json=None):
    """Resolve a qualified release; errors distinguish absent, unavailable and invalid.

    ``fetch_json(url)`` may supply a deterministic offline transport. It must not
    manufacture an empty response on HTTP/network errors.
    """
    from urllib.error import HTTPError

    _require(channel in ("stable", "nightly"), "Unsupported channel")
    fetch_json = fetch_json or fetch_release_json

    def fetch(url):
        try:
            return fetch_json(url)
        except HTTPError as exc:
            if exc.code == 404 and url == f"{API}/latest":
                raise ReleaseError(
                    "No approved stable release is published", "release_absent"
                ) from exc
            raise ReleaseError("Release feed unavailable; retry later", "feed_unavailable") from exc
        except (OSError, TimeoutError) as exc:
            raise ReleaseError(
                "Release feed unavailable; check network and retry", "feed_unavailable"
            ) from exc

    if channel == "stable":
        metadata = fetch(f"{API}/latest")
    else:
        candidates = []
        identities = set()
        for page in range(1, MAX_PAGES + 1):
            entries = fetch(f"{API}?per_page=100&page={page}")
            _require(isinstance(entries, list) and len(entries) <= 100, "Invalid release page")
            for entry in entries:
                _require(isinstance(entry, dict), "Invalid release entry")
                tag = entry.get("tag_name", "")
                if entry.get("draft") is not False or entry.get("prerelease") is not True:
                    continue
                if not isinstance(tag, str) or not tag.startswith("nightly-"):
                    continue
                match = re.fullmatch(r"nightly-([1-9][0-9]{0,18})-([0-9a-f]{12})", tag)
                _require(match is not None, "Invalid immutable nightly tag")
                build = int(match[1])
                _require(build not in identities, "Ambiguous nightly build identity")
                identities.add(build)
                candidates.append((build, entry))
            if len(entries) < 100:
                break
        else:
            raise ReleaseError("Release pagination bound reached; cannot safely select nightly")
        if not candidates:
            raise ReleaseError("No qualified nightly release is published", "release_absent")
        metadata = max(candidates, key=lambda item: item[0])[1]
    _require(isinstance(metadata, dict), "Invalid release metadata")
    _require(metadata.get("draft") is False, "Draft release is not installable")
    _require(metadata.get("prerelease") is (channel == "nightly"), "Release channel mismatch")
    tag = metadata.get("tag_name")
    pattern = rf"v{VERSION}" if channel == "stable" else r"nightly-[1-9][0-9]{0,18}-[0-9a-f]{12}"
    _require(isinstance(tag, str) and re.fullmatch(pattern, tag), "Invalid release tag")
    _require(metadata.get("html_url") == f"{ROOT}/tag/{tag}", "Unofficial release origin")
    assets = metadata.get("assets")
    _require(
        isinstance(assets, list) and 1 <= len(assets) <= 33, "Missing or oversized release assets"
    )
    indexed = {}
    for asset in assets:
        _require(
            isinstance(asset, dict) and isinstance(asset.get("name"), str), "Invalid release asset"
        )
        name = asset["name"]
        _require(name not in indexed, "Ambiguous release assets")
        _require(asset.get("state") == "uploaded", "Incomplete release upload")
        indexed[name] = asset
    _require("release-manifest.json" in indexed, "Missing release manifest")
    manifest_asset = indexed["release-manifest.json"]
    _integer(manifest_asset.get("size"), "manifest size", MAX_JSON_BYTES)
    _asset_url(manifest_asset.get("browser_download_url"), tag, "release-manifest.json")
    data = validate_manifest(fetch(manifest_asset["browser_download_url"]), channel=channel)
    _require(release_tag(data) == tag, "Manifest/tag identity mismatch")
    _require(
        metadata.get("target_commitish") == data["source_sha"], "Release/source identity mismatch"
    )
    for artifact in data["artifacts"]:
        actual = indexed.get(artifact["name"], {})
        _require(
            actual.get("browser_download_url") == artifact["url"]
            and actual.get("size") == artifact["size"],
            "Missing or inconsistent release artifact",
        )
    return data
