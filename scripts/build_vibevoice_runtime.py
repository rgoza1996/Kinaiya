"""Build a checked Transformers 5 derivative of the pinned VibeVoice runtime."""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path

DISTRIBUTION = "kinaiya-vibevoice-runtime"
VERSION = "1.0.0+kinaiya.2"
WHEEL_FILENAME = "kinaiya_vibevoice_runtime-1.0.0+kinaiya.2-py3-none-any.whl"
DIST_INFO = "kinaiya_vibevoice_runtime-1.0.0+kinaiya.2.dist-info"
PROVENANCE_FILENAME = WHEEL_FILENAME + ".provenance.json"
REQUIRES_PYTHON = ">=3.11"
REQUIRES_DIST = (
    "torch>=2.14,<2.15",
    "transformers>=5.21,<5.22",
    "huggingface-hub>=1.10,<2",
    "accelerate>=1.15,<2",
    "diffusers>=0.39,<1",
    "numpy>=2.3.3,<3",
    "scipy>=1.17,<2",
    "librosa>=0.11,<1",
    "tqdm>=4.67,<5",
    "ml-collections>=1.1,<2",
    "absl-py>=2.5,<3",
)
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
MANIFEST_RELATIVE = Path("packages/providers/vibevoice_manifest.json")
LICENSE_MANIFEST_KEY = "licenses/runtime-MIT.txt"
LICENSE_ARCNAME = f"{DIST_INFO}/licenses/LICENSE"
RUNTIME_REVISION = "16fb2cb1217c9934a886e1948ffb06120caa2df5"


class BuildError(Exception):
    """Build validation or output failure without leaking input paths."""


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_checked_manifest() -> dict:
    path = _repository_root() / MANIFEST_RELATIVE
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if (
        not isinstance(data, dict)
        or data.get("runtime_revision") != RUNTIME_REVISION
        or data.get("runtime_versions")
        != {
            DISTRIBUTION: VERSION,
            "transformers": "5.21.0",
            "torch": "2.14.1+cpu",
        }
        or not isinstance(data.get("runtime_files"), dict)
        or len(data["runtime_files"]) != 22
        or not isinstance(data.get("upstream_runtime_files"), dict)
        or len(data["upstream_runtime_files"]) != 22
        or not isinstance(data.get("runtime_patch"), dict)
    ):
        raise BuildError("invalid checked manifest")
    return data


def _runtime_files(manifest: dict) -> dict:
    records = manifest.get("runtime_files")
    if not isinstance(records, dict) or not records:
        raise BuildError("invalid runtime file list")
    return records


def _license_record(manifest: dict) -> dict:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BuildError("invalid license record")
    record = files.get(LICENSE_MANIFEST_KEY)
    if not isinstance(record, dict):
        raise BuildError("invalid license record")
    return record


def _check_arcname(name: str) -> None:
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
        raise BuildError("invalid archive name")
    for part in name.split("/"):
        if part in ("", ".", ".."):
            raise BuildError("invalid archive name")


def _open_dir_at(parent_fd: int, name: str) -> int:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        raise BuildError("missing source file") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise BuildError("symlink not allowed") from None
        raise BuildError("unreadable source file") from None
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise BuildError("nonregular source file")
    return fd


def _record_values(record: dict) -> tuple[int, str]:
    if (
        not isinstance(record, dict)
        or type(record.get("size_bytes")) is not int
        or not 0 <= record["size_bytes"] <= 2_000_000
        or not isinstance(record.get("sha256"), str)
        or len(record["sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in record["sha256"])
    ):
        raise BuildError("invalid manifest record")
    return record["size_bytes"], record["sha256"]


def _read_relative(root_fd: int, arcname: str, record: dict) -> bytes:
    _check_arcname(arcname)
    size, _ = _record_values(record)
    parts = arcname.split("/")
    opened: list[int] = []
    try:
        current = root_fd
        for component in parts[:-1]:
            nxt = _open_dir_at(current, component)
            opened.append(nxt)
            current = nxt
        leaf = parts[-1]
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        except FileNotFoundError:
            raise BuildError("missing source file") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise BuildError("symlink not allowed") from None
            raise BuildError("unreadable source file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise BuildError("nonregular source file")
            if info.st_size != size:
                raise BuildError("size mismatch")
            chunks: list[bytes] = []
            count = 0
            while True:
                chunk = os.read(fd, min(65536, size + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > size:
                    raise BuildError("size mismatch")
            data = b"".join(chunks)
            _verify_bytes(data, record)
            return data
        finally:
            os.close(fd)
    finally:
        for item in reversed(opened):
            try:
                os.close(item)
            except OSError:
                pass


def _directory_fd(path: Path, *, create=False) -> int:
    """Walk absolute parents using owned no-follow directory descriptors."""
    parts = Path(os.path.abspath(path)).parts[1:]
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
            nxt = _open_dir_at(current, component)
            os.close(current)
            current = nxt
        return current
    except BaseException:
        os.close(current)
        raise


def _verify_bytes(data: bytes, record: dict) -> str:
    size, expected = _record_values(record)
    if len(data) != size:
        raise BuildError("size mismatch")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise BuildError("hash mismatch")
    return digest


def _apply_unified_patch(payloads: dict[str, bytes], patch: bytes) -> dict[str, bytes]:
    """Apply only exact existing-file hunks; never execute patch or source code.

    This deliberately excludes fuzzy offsets, renames, file creation, binary
    patches and missing final newlines. Both input and final file hashes are
    checked separately by the builder.
    """
    try:
        lines = patch.decode("utf-8").splitlines(keepends=True)
    except UnicodeError:
        raise BuildError("invalid source patch") from None
    if not lines or any(not line.endswith("\n") for line in lines):
        raise BuildError("invalid source patch")
    result = dict(payloads)
    changed: set[str] = set()
    pos = 0
    while pos < len(lines):
        if not lines[pos].startswith("--- a/") or pos + 1 >= len(lines):
            raise BuildError("invalid source patch")
        name = lines[pos][6:-1]
        _check_arcname(name)
        if name not in payloads or name in changed or lines[pos + 1] != f"+++ b/{name}\n":
            raise BuildError("invalid source patch")
        changed.add(name)
        pos += 2
        try:
            original = payloads[name].decode("utf-8").splitlines(keepends=True)
        except UnicodeError:
            raise BuildError("invalid source patch") from None
        output: list[str] = []
        cursor = 0
        hunks = 0
        while pos < len(lines) and lines[pos].startswith("@@"):
            header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n", lines[pos])
            if header is None:
                raise BuildError("invalid source patch")
            old_start, old_count, new_start, new_count = (
                int(part) if part is not None else 1 for part in header.groups()
            )
            # A zero-length range starts after the preceding line (unified-diff
            # convention); nonempty ranges use one-based line positions.
            start = old_start if old_count == 0 else old_start - 1
            if start < cursor or start > len(original):
                raise BuildError("invalid source patch")
            output.extend(original[cursor:start])
            new_index = new_start if new_count == 0 else new_start - 1
            if new_index != len(output):
                raise BuildError("invalid source patch")
            pos += 1
            before: list[str] = []
            after: list[str] = []
            while pos < len(lines) and not lines[pos].startswith(("@@", "--- a/")):
                line = lines[pos]
                if line[0] not in " +-":
                    raise BuildError("invalid source patch")
                if line[0] in " -":
                    before.append(line[1:])
                if line[0] in " +":
                    after.append(line[1:])
                pos += 1
            if (
                len(before) != old_count
                or len(after) != new_count
                or original[start : start + old_count] != before
            ):
                raise BuildError("invalid source patch")
            output.extend(after)
            cursor = start + old_count
            hunks += 1
        if hunks == 0:
            raise BuildError("invalid source patch")
        output.extend(original[cursor:])
        result[name] = "".join(output).encode()
    return result


def _patch_payloads(
    payloads: dict[str, bytes], manifest: dict
) -> tuple[dict[str, bytes], str | None]:
    record = manifest.get("runtime_patch")
    if record is None:
        return payloads, None
    if not isinstance(record, dict) or set(record) != {"path", "size_bytes", "sha256"}:
        raise BuildError("invalid source patch record")
    # The checked repository owns this one patch. Caller paths cannot select
    # arbitrary patches or source code.
    if record["path"] != "packages/providers/vibevoice_transformers5.patch":
        raise BuildError("invalid source patch record")
    root_fd = _directory_fd(_repository_root())
    try:
        patch = _read_relative(root_fd, record["path"], record)
    finally:
        os.close(root_fd)
    return _apply_unified_patch(payloads, patch), hashlib.sha256(patch).hexdigest()


def _metadata_bytes(revision: str) -> bytes:
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {DISTRIBUTION}",
        f"Version: {VERSION}",
        "Summary: Checked Transformers 5 derivative runtime for Microsoft VibeVoice",
        "License-Expression: MIT",
        "License-File: LICENSE",
        f"Requires-Python: {REQUIRES_PYTHON}",
    ]
    for req in REQUIRES_DIST:
        lines.append(f"Requires-Dist: {req}")
    lines.extend(
        [
            "",
            f"Derived from microsoft/VibeVoice at revision {revision}.",
            "MIT-licensed source with an explicit checked compatibility patch.",
            "Upstream and derived source digests are retained in provenance.",
            "Source-only derivative: no weights,",
            "tokenizer, presets, demos or web requirements.",
            "Installs no dependencies here; owner applies a hashed CPU lock.",
            "Declared ranges are not claimed security-clean.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _wheel_marker_bytes() -> bytes:
    return (
        b"Wheel-Version: 1.0\n"
        b"Generator: kinaiya-vibevoice-runtime build\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n"
    )


def _record_hash(data: bytes) -> str:
    raw = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _zip_entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=ZIP_DATE_TIME)
    info.create_system = 0
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def build_runtime_wheel(source_dir: Path, license_file: Path, output_dir: Path) -> dict:
    manifest = _load_checked_manifest()
    revision = str(manifest.get("runtime_revision", ""))
    if not revision:
        raise BuildError("invalid runtime revision")
    records = _runtime_files(manifest)
    upstream_records = manifest.get("upstream_runtime_files", records)
    if not isinstance(upstream_records, dict) or set(upstream_records) != set(records):
        raise BuildError("invalid upstream runtime file list")
    license_record = _license_record(manifest)
    for name in list(records) + [LICENSE_ARCNAME]:
        _check_arcname(name)
    for name in records:
        if not name.startswith("vibevoice/") or not name.endswith(".py"):
            raise BuildError("invalid runtime file list")
    try:
        source_fd = _directory_fd(source_dir)
    except FileNotFoundError:
        raise BuildError("missing source directory") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise BuildError("symlink not allowed") from None
        raise BuildError("unreadable source directory") from None
    payloads: dict[str, bytes] = {}
    source_digests: dict[str, str] = {}
    try:
        for arcname in sorted(records):
            data = _read_relative(source_fd, arcname, upstream_records[arcname])
            digest = _verify_bytes(data, upstream_records[arcname])
            payloads[arcname] = data
            source_digests[arcname] = digest
    finally:
        try:
            os.close(source_fd)
        except OSError:
            pass
    upstream_digests = dict(source_digests)
    payloads, patch_digest = _patch_payloads(payloads, manifest)
    source_digests = {
        name: _verify_bytes(payloads[name], records[name]) for name in sorted(records)
    }
    absolute_license = Path(os.path.abspath(license_file))
    license_parent = _directory_fd(absolute_license.parent)
    try:
        license_bytes = _read_relative(license_parent, absolute_license.name, license_record)
    finally:
        os.close(license_parent)
    license_digest = _verify_bytes(license_bytes, license_record)
    metadata = _metadata_bytes(revision)
    marker = _wheel_marker_bytes()
    dist_metadata = f"{DIST_INFO}/METADATA"
    dist_wheel = f"{DIST_INFO}/WHEEL"
    dist_record = f"{DIST_INFO}/RECORD"
    payloads[dist_metadata] = metadata
    payloads[dist_wheel] = marker
    payloads[LICENSE_ARCNAME] = license_bytes
    record_lines: list[str] = []
    for name in sorted(payloads):
        data = payloads[name]
        record_lines.append(f"{name},sha256={_record_hash(data)},{len(data)}")
    record_lines.append(f"{dist_record},,")
    payloads[dist_record] = ("\n".join(record_lines) + "\n").encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name in sorted(payloads):
            handle.writestr(_zip_entry(name), payloads[name], compresslevel=9)
    wheel_bytes = buffer.getvalue()
    wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
    attestation = {
        "distribution": DISTRIBUTION,
        "version": VERSION,
        "wheel_filename": WHEEL_FILENAME,
        "source_revision": revision,
        "sources": dict(sorted(source_digests.items())),
        "upstream_sources": dict(sorted(upstream_digests.items())),
        "source_patch_sha256": patch_digest,
        "license_sha256": license_digest,
        "license_size": len(license_bytes),
        "wheel_sha256": wheel_digest,
        "wheel_size": len(wheel_bytes),
        "requires_python": REQUIRES_PYTHON,
        "requires_dist": list(REQUIRES_DIST),
    }
    try:
        out_fd = _directory_fd(output_dir, create=True)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise BuildError("symlink not allowed") from None
        raise BuildError("invalid output directory") from None
    created: list[str] = []
    try:
        try:
            wheel_fd = os.open(
                WHEEL_FILENAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=out_fd,
            )
        except FileNotFoundError:
            raise BuildError("invalid output directory") from None
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ELOOP):
                raise BuildError("output exists") from None
            raise BuildError("unwritable output") from None
        created.append(WHEEL_FILENAME)
        try:
            view = wheel_bytes
            while view:
                written = os.write(wheel_fd, view)
                if written <= 0:
                    raise BuildError("incomplete wheel write")
                view = view[written:]
        finally:
            os.close(wheel_fd)
        provenance = (json.dumps(attestation, sort_keys=True, indent=2) + "\n").encode()
        try:
            prov_fd = os.open(
                PROVENANCE_FILENAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=out_fd,
            )
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ELOOP):
                raise BuildError("output exists") from None
            raise BuildError("unwritable output") from None
        created.append(PROVENANCE_FILENAME)
        try:
            rest = provenance
            while rest:
                done = os.write(prov_fd, rest)
                if done <= 0:
                    raise BuildError("incomplete provenance write")
                rest = rest[done:]
        finally:
            os.close(prov_fd)
    except BaseException:
        for name in created:
            try:
                os.unlink(name, dir_fd=out_fd)
            except OSError:
                pass
        raise
    finally:
        try:
            os.close(out_fd)
        except OSError:
            pass
    return {
        "wheel_path": Path(output_dir) / WHEEL_FILENAME,
        "provenance_path": Path(output_dir) / PROVENANCE_FILENAME,
        "attestation": attestation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build derivative runtime wheel")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--license-file", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_runtime_wheel(
            Path(args.source_dir), Path(args.license_file), Path(args.output_dir)
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc.strerror or 'output failure'}", file=sys.stderr)
        return 1
    attestation = result["attestation"]
    public = {
        "wheel": attestation["wheel_filename"],
        "sha256": attestation["wheel_sha256"],
        "revision": attestation["source_revision"],
        "files": len(attestation["sources"]),
    }
    print(json.dumps(public, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
