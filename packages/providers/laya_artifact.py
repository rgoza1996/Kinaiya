"""Copy an explicitly pinned local Laya checkpoint into a private verified snapshot.

No model framework imports belong here. The source is never loaded after verification:
the worker receives only the private copy whose bytes matched the owner's manifest pin.
"""

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

ARTIFACT_HEADER = "X-Kinaiya-Laya-Artifact-SHA256"
WORKER_PID_HEADER = "X-Kinaiya-Laya-Worker-Pid"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILES = {
    "model.safetensors": 2 * 1024**3,
    "rl_agent_config.json": 16 * 1024**2,
    "encoder/config.json": 16 * 1024**2,
    "tokenizer/tokenizer.json": 16 * 1024**2,
    "tokenizer/tokenizer_config.json": 16 * 1024**2,
}


class ArtifactError(ValueError):
    """Artifact verification failed before model loading."""


def valid_artifact_sha256(value):
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


@dataclass(frozen=True)
class VerifiedCheckpoint:
    sha256: str
    model_alias: str
    directory: Path
    digests: Mapping[str, str]


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ArtifactError("Duplicate manifest JSON key")
        obj[key] = value
    return obj


def _open(path, *, directory=False, parent=None):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    return os.open(path, flags, dir_fd=parent)


@contextmanager
def _directory(path):
    """Walk every directory component without following a symlink."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ArtifactError("Artifact paths cannot contain parent traversal")
    fd = _open(path.anchor, directory=True)
    try:
        for part in path.parts[1:]:
            next_fd = _open(part, directory=True, parent=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _file(root_fd, name):
    parts = name.split("/")
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = _open(part, directory=True, parent=parent)
            os.close(parent)
            parent = next_fd
        fd = _open(parts[-1], parent=parent)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ArtifactError("Artifact entries must be regular files")
            yield stream
    finally:
        os.close(parent)


def _manifest(path, expected_sha256):
    path = Path(path)
    with _directory(path.parent) as parent, _file(parent, path.name) as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ArtifactError("Artifact manifest exceeds 64 KiB")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ArtifactError("Artifact manifest digest mismatch")
    try:
        manifest = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ArtifactError("Invalid artifact manifest JSON") from exc
    if not isinstance(manifest, dict):
        raise ArtifactError("Artifact manifest must be an object")
    alias = manifest.get("model_alias")
    if not isinstance(alias, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", alias):
        raise ArtifactError("Invalid manifest model alias")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(_FILES):
        raise ArtifactError("Artifact manifest must name exactly five inference files")
    entries = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "size_bytes", "sha256"}:
            raise ArtifactError("Invalid artifact file entry")
        name, size, digest = entry["path"], entry["size_bytes"], entry["sha256"]
        if not isinstance(name, str) or name not in _FILES or name in entries:
            raise ArtifactError("Unexpected or duplicate artifact file")
        if type(size) is not int or not 0 < size <= _FILES[name]:
            raise ArtifactError("Invalid artifact file size")
        if not valid_artifact_sha256(digest):
            raise ArtifactError("Invalid artifact file digest")
        entries[name] = entry
    return alias, entries


@contextmanager
def verified_laya_checkpoint(model_dir, manifest_path, expected_sha256):
    """Yield an owned snapshot; refuse unexpected paths, bytes and nonregular files.

    The manifest pin comes from trusted configuration, never from the manifest itself.
    Source files may be replaced after copying without changing what the worker loads.
    Linux descriptor-relative no-follow opens are required; there is no weaker fallback.
    """
    if not valid_artifact_sha256(expected_sha256):
        raise ArtifactError("Expected artifact SHA-256 must be 64 lowercase hex characters")
    try:
        alias, entries = _manifest(manifest_path, expected_sha256)
        with TemporaryDirectory(prefix="kinaiya-laya-") as owned, _directory(model_dir) as source:
            snapshot = Path(owned)
            for name, entry in entries.items():
                destination = snapshot / name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                with _file(source, name) as original, destination.open("xb") as copied:
                    before = os.fstat(original.fileno())
                    if before.st_size != entry["size_bytes"]:
                        raise ArtifactError("Artifact file size mismatch")
                    total = 0
                    while block := original.read(1024 * 1024):
                        total += len(block)
                        if total > entry["size_bytes"]:
                            raise ArtifactError("Artifact file grew during verification")
                        digest.update(block)
                        copied.write(block)
                    after = os.fstat(original.fileno())
                if (
                    total != entry["size_bytes"]
                    or digest.hexdigest() != entry["sha256"]
                    or (before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_mtime_ns, after.st_ctime_ns)
                ):
                    raise ArtifactError("Artifact bytes changed or digest mismatch")
                destination.chmod(0o400)
            yield VerifiedCheckpoint(
                expected_sha256,
                alias,
                snapshot,
                MappingProxyType({name: entry["sha256"] for name, entry in entries.items()}),
            )
    except OSError as exc:
        raise ArtifactError("Artifact path or file could not be verified") from exc
