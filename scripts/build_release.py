"""Build immutable release assets from Git objects, never the developer worktree.

Run from the repository root: ``python -m scripts.build_release --help``.
No credentials or network access are needed by the bundler.
"""

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

from packages.distribution.releases import (
    ROOT,
    ReleaseError,
    release_tag,
    validate_identity,
    validate_manifest,
    verify_artifact,
    verify_gzip_archive,
)

ROOT_FILES = {"pyproject.toml", "requirements-runtime.lock", "LICENSE", "README.md", "install.sh"}
SOURCE_ROOTS = {"core", "services", "packages", "harnesses"}
SOURCE_SUFFIXES = {".py", ".json", ".txt", ".patch"}
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 10000


def validate_archive_name(name):
    """Only portable relative archive paths, never traversal or Windows aliases."""
    parts = name.split("/")
    if len(name) > 240 or any(
        part in ("", ".", "..")
        or len(part) > 100
        or not re.fullmatch(r"[a-zA-Z0-9_@+.-]+", part)
        or part.endswith((".", " "))
        or part.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *[f"COM{i}" for i in range(1, 10)],
            *[f"LPT{i}" for i in range(1, 10)],
        }
        for part in parts
    ):
        raise ReleaseError("Unsafe archive member name")
    return name


def _check_files(files):
    if (
        len(files) > MAX_FILES
        or sum(len(content) for content, _ in files.values()) > MAX_TOTAL_BYTES
    ):
        raise ReleaseError("Archive resource bound exceeded")
    folded = set()
    for name, (content, _) in files.items():
        validate_archive_name(name)
        if name.casefold() in folded:
            raise ReleaseError("Archive member case collision")
        folded.add(name.casefold())
        if len(content) > MAX_FILE_BYTES:
            raise ReleaseError("Archive file size bound exceeded")


def _source_allowed(name):
    parts = PurePosixPath(name).parts
    if name in ROOT_FILES:
        return True
    if any(part.startswith(".") or part == "__pycache__" for part in parts):
        return False
    if PurePosixPath(name).suffix != ".py" and (
        PurePosixPath(name).name.casefold() == "config.json"
        or re.match(r"(?:credentials?|secrets?|tokens?)(?:[._-]|$)", PurePosixPath(name).name, re.I)
    ):
        return False
    return (
        parts[0] in SOURCE_ROOTS
        or name.startswith("evaluation/harness/")
        or name == "evaluation/__init__.py"
    ) and PurePosixPath(name).suffix in SOURCE_SUFFIXES


def _json(data):
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def _archive(path, files):
    _check_files(files)
    with path.open("xb") as raw:
        with gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, (content, mode) in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mode = mode
                    archive.addfile(info, io.BytesIO(content))


def build_release(
    repo, output, *, channel, version, build_number, source_sha, web_dir=None, web_source_sha=None
):
    """Return and write a v1 manifest plus reproducible Linux/source and web assets."""
    repo, output = Path(repo), Path(output)
    if output.exists():
        raise ReleaseError("Output already exists; immutable assets are never overwritten")
    if web_dir is not None and web_source_sha != source_sha:
        raise ReleaseError("Prebuilt web/source identity mismatch")
    identity = {
        "schema_version": 1,
        "channel": channel,
        "version": version
        if channel == "stable"
        else f"{version}-nightly.{build_number}+{source_sha[:12]}",
        "build_number": build_number,
        "source_sha": source_sha,
        "protocol_min": 1,
        "protocol_max": 1,
        "state_schema": 1,
        "min_updater": 1,
    }
    tag = release_tag(validate_identity(identity))
    if _git(repo, "cat-file", "-t", source_sha).strip() != b"commit":
        raise ReleaseError("source_sha must identify a commit")
    files = {}
    for entry in _git(repo, "ls-tree", "-rz", "--full-tree", source_sha).split(b"\0"):
        if not entry:
            continue
        meta, raw_name = entry.split(b"\t", 1)
        mode, _, oid = meta.split()
        name = raw_name.decode("utf-8")
        if not _source_allowed(name):
            continue
        if mode not in (b"100644", b"100755"):
            raise ReleaseError("Source symlink/submodule is not permitted")
        validate_archive_name(name)
        size = int(_git(repo, "cat-file", "-s", oid.decode()))
        if (
            size > MAX_FILE_BYTES
            or len(files) >= MAX_FILES
            or sum(len(c) for c, _ in files.values()) + size > MAX_TOTAL_BYTES
        ):
            raise ReleaseError("Source resource bound exceeded")
        files[name] = (
            _git(repo, "cat-file", "blob", oid.decode()),
            0o755 if mode == b"100755" else 0o644,
        )
    if "requirements-runtime.lock" not in files:
        raise ReleaseError("Missing committed runtime lock")
    web_files = {}
    if web_dir is not None:
        web_dir = Path(web_dir)
        if web_dir.is_symlink() or not web_dir.is_dir():
            raise ReleaseError("Invalid web build directory")
        for path in sorted(web_dir.rglob("*")):
            if path.is_symlink():
                raise ReleaseError("Web symlink is not permitted")
            if path.is_dir():
                continue
            name = validate_archive_name(path.relative_to(web_dir).as_posix())
            if any(p.startswith(".") for p in name.split("/")):
                raise ReleaseError("Hidden web build file is not permitted")
            if path.stat().st_size > MAX_FILE_BYTES or len(web_files) >= MAX_FILES:
                raise ReleaseError("Web resource bound exceeded")
            web_files[name] = (path.read_bytes(), 0o644)
            _check_files(web_files)
        if not {"index.html", "main.dart.js"} <= web_files.keys():
            raise ReleaseError("Missing required compiled web files")
        files.update(
            {f"apps/kinaiya_flutter/build/web/{name}": value for name, value in web_files.items()}
        )
        web_files["release-identity.json"] = (_json(identity), 0o644)
    _check_files(files)
    inventory = {
        "source_sha": source_sha,
        "runtime_lock": {
            "name": "requirements-runtime.lock",
            "sha256": hashlib.sha256(files["requirements-runtime.lock"][0]).hexdigest(),
        },
        "files": [
            {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "mode": mode}
            for name, (content, mode) in sorted(files.items())
        ],
    }
    files["release-inventory.json"] = (_json(inventory), 0o644)
    files["release-identity.json"] = (_json(identity), 0o644)
    output.mkdir(parents=True, exist_ok=False)
    name = f"kinaiya-{tag}-source.tar.gz"
    path = output / name
    _archive(path, files)
    data = {
        **identity,
        "artifacts": [
            {
                "name": name,
                "platform": "linux",
                "architecture": "any",
                "url": f"{ROOT}/download/{tag}/{name}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
        ],
    }
    if web_files:
        web_name = f"kinaiya-{tag}-web.tar.gz"
        web_path = output / web_name
        _archive(web_path, web_files)
        data["artifacts"].append(
            {
                "name": web_name,
                "platform": "web",
                "architecture": "any",
                "url": f"{ROOT}/download/{tag}/{web_name}",
                "sha256": hashlib.sha256(web_path.read_bytes()).hexdigest(),
                "size": web_path.stat().st_size,
            }
        )
    validate_manifest(data)
    (output / "release-manifest.json").write_bytes(_json(data))
    return data


class _ReleaseTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        # TarInfo normally consumes PAX/GNU metadata before yielding members.
        # Reject it here, before an attacker-controlled size reaches that parser.
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
            raise ReleaseError("Release archive must contain only regular USTAR files")
        # The stdlib's private extension-dispatch hook has no typeshed stub.
        return cast(Any, tarfile.TarInfo)._proc_member(self, archive)


def validate_bundle(directory):
    """Verify digests and stream bounded regular-file archives without extracting."""
    directory = Path(directory)
    manifest_path = directory / "release-manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 1024 * 1024:
        raise ReleaseError("Invalid local manifest size/type")
    data = validate_manifest(json.loads(manifest_path.read_bytes()))
    for artifact in data["artifacts"]:
        path = directory / artifact["name"]
        verify_artifact(path, artifact)
        if artifact["platform"] not in ("linux", "web"):
            continue
        verify_gzip_archive(path, MAX_TOTAL_BYTES + MAX_FILES * 1024 + 10240)
        seen, total, archive_identity = set(), 0, None
        try:
            with tarfile.open(path, "r|gz", tarinfo=_ReleaseTarInfo) as archive:
                for member in archive:
                    validate_archive_name(member.name)
                    folded = member.name.casefold()
                    if (
                        not member.isfile()
                        or member.issparse()
                        or folded in seen
                        or member.size > MAX_FILE_BYTES
                        or member.size < 0
                    ):
                        raise ReleaseError("Unsafe archive member type, duplicate or size")
                    seen.add(folded)
                    total += member.size
                    if len(seen) > MAX_FILES or total > MAX_TOTAL_BYTES:
                        raise ReleaseError("Archive resource bound exceeded")
                    if member.name == "release-identity.json":
                        archive_identity = json.load(archive.extractfile(member))
        except (tarfile.TarError, OSError, ValueError) as exc:
            raise ReleaseError("Invalid release archive") from exc
        required = (
            {"release-identity.json", "index.html", "main.dart.js"}
            if artifact["platform"] == "web"
            else {
                "release-identity.json",
                "release-inventory.json",
                "pyproject.toml",
                "requirements-runtime.lock",
                "license",
            }
        )
        if not required <= seen:
            raise ReleaseError("Archive missing required release files")
        if not isinstance(archive_identity, dict) or any(
            archive_identity.get(key) != value for key, value in data.items() if key != "artifacts"
        ):
            raise ReleaseError("Archive/manifest identity mismatch")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate", type=Path, help="Validate an existing bundle without network access"
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--channel", choices=("stable", "nightly"), default="nightly")
    parser.add_argument("--version", help="Base X.Y.Z; never an alpha/prerelease tag")
    parser.add_argument("--build-number", type=int)
    parser.add_argument("--source-sha")
    parser.add_argument("--web-dir", type=Path)
    parser.add_argument("--web-source-sha")
    args = parser.parse_args()
    try:
        if args.validate:
            data = validate_bundle(args.validate)
            print(f"validated {release_tag(data)}")
        else:
            if any(
                value is None
                for value in (args.output, args.version, args.build_number, args.source_sha)
            ):
                parser.error("build requires --output, --version, --build-number and --source-sha")
            data = build_release(
                args.repo,
                args.output,
                channel=args.channel,
                version=args.version,
                build_number=args.build_number,
                source_sha=args.source_sha,
                web_dir=args.web_dir,
                web_source_sha=args.web_source_sha,
            )
            print(f"built {release_tag(data)} at {args.output}")
    except (ReleaseError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        parser.exit(1, f"release refused: {exc}\n")


if __name__ == "__main__":
    main()
