"""Bounded owner-selected GGUF metadata; importing this loads no native code.

Pins identify exact bytes, not publisher, build or license provenance. Only the
child copies/hashes large inputs, using ``whistle_assets.sealed_asset``.
"""

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

from packages.providers.whistle_assets import outside_workspace

MAX_MANIFEST_BYTES = 64 * 1024
MAX_ASSETS = 64
MAX_BYTES = 32 * 1024**3
MAX_EXECUTABLE_BYTES = 64 * 1024**2
ALIAS_RE = re.compile(r"[A-Za-z0-9._/\-]{1,128}\Z")
GUEST_RUNTIME_RE = re.compile(r"/(?:runtime|vendor)/[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
MODEL_GUEST = "/model.gguf"
RUNTIME_SERVER_GUEST = "/runtime/llama-server"
TOP_KEYS = {"version", "profile", "model", "alias", "assets", "sampling"}
ASSET_KEYS = {"path", "guest", "bytes", "sha256"}
SAMPLING_RANGES = {
    "temperature": (0, 2),
    "top_p": (0, 1),
    "top_k": (0, 1000),
    "min_p": (0, 1),
    "presence_penalty": (-2, 2),
    "repeat_penalty": (0.1, 2),
}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate GGUF manifest key")
        result[key] = value
    return result


def _invalid(value):
    raise ValueError("Non-finite GGUF manifest number")


def _text(value, limit):
    if not isinstance(value, str) or any(c in value for c in "\0\r\n"):
        raise ValueError("Invalid GGUF string")
    try:
        length = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError("Invalid GGUF UTF-8 string") from exc
    if not 1 <= length <= limit:
        raise ValueError("Invalid GGUF string length")
    return value


def validate_manifest(manifest):
    """Validate plain structure even for callers that bypass ``load_manifest``.

    This performs no large file reads or path metadata queries. The loader and
    child sealing step separately bind owner paths to the specified bytes.
    """
    if not isinstance(manifest, dict) or set(manifest) != TOP_KEYS:
        raise ValueError("Invalid GGUF manifest keys")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ValueError("Invalid GGUF manifest version")
    _text(manifest["profile"], 128)
    _text(manifest["model"], 128)
    if ALIAS_RE.fullmatch(_text(manifest["alias"], 128)) is None:
        raise ValueError("Invalid GGUF alias")
    assets = manifest["assets"]
    if not isinstance(assets, list) or not 1 <= len(assets) <= MAX_ASSETS:
        raise ValueError("Invalid GGUF assets")
    guests = set()
    for entry in assets:
        if not isinstance(entry, dict) or set(entry) != ASSET_KEYS:
            raise ValueError("Invalid GGUF asset keys")
        if not Path(_text(entry["path"], MAX_MANIFEST_BYTES)).is_absolute():
            raise ValueError("Invalid GGUF owner path")
        guest = _text(entry["guest"], 137)
        if guest != MODEL_GUEST and GUEST_RUNTIME_RE.fullmatch(guest) is None:
            raise ValueError("Invalid GGUF guest")
        if guest in guests:
            raise ValueError("Duplicate GGUF guest")
        guests.add(guest)
        limit = MAX_EXECUTABLE_BYTES if guest == RUNTIME_SERVER_GUEST else MAX_BYTES
        if type(entry["bytes"]) is not int or not 1 <= entry["bytes"] <= limit:
            raise ValueError("Invalid GGUF asset size")
        if not isinstance(entry["sha256"], str) or SHA_RE.fullmatch(entry["sha256"]) is None:
            raise ValueError("Invalid GGUF asset digest")
    if not {MODEL_GUEST, RUNTIME_SERVER_GUEST} <= guests:
        raise ValueError("GGUF model and executable pins are required")
    sampling = manifest["sampling"]
    if not isinstance(sampling, dict) or set(sampling) != set(SAMPLING_RANGES):
        raise ValueError("Invalid GGUF sampling keys")
    for key, (low, high) in SAMPLING_RANGES.items():
        value = sampling[key]
        # Check range before isfinite so arbitrary JSON integers cannot overflow.
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError("Invalid GGUF sampling value")
        if not math.isfinite(value) or (key == "top_k" and type(value) is not int):
            raise ValueError("Invalid GGUF sampling value")
    return manifest


def load_manifest(path, expected_sha256, workspace):
    """Validate exact small manifest bytes plus no-follow asset metadata only."""
    if not isinstance(expected_sha256, str) or SHA_RE.fullmatch(expected_sha256) is None:
        raise ValueError("Invalid GGUF manifest digest")
    candidate = outside_workspace(Path(_text(str(path), MAX_MANIFEST_BYTES)), workspace)
    fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_MANIFEST_BYTES:
            raise ValueError("Invalid GGUF manifest type/size")
        outside_workspace(os.readlink(f"/proc/self/fd/{fd}"), workspace)
        raw = bytearray()
        while chunk := os.read(fd, min(65536, MAX_MANIFEST_BYTES + 1 - len(raw))):
            raw.extend(chunk)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError("GGUF manifest exceeds its bound")
        if len(raw) != info.st_size:
            raise ValueError("GGUF manifest changed during read")
    finally:
        os.close(fd)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("GGUF manifest digest mismatch")
    try:
        manifest = validate_manifest(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid)
        )
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("Invalid GGUF manifest JSON") from exc
    for entry in manifest["assets"]:
        asset = outside_workspace(entry["path"], workspace)
        info = asset.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != entry["bytes"]
            or info.st_uid != os.geteuid()
        ):
            raise ValueError("Invalid GGUF asset metadata")
    return manifest
