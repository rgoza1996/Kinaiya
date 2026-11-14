"""Native-owned opaque video receipts; no paths, URLs, or upload endpoint.

Only the trusted source/projector owner calls register. MP4 signatures and
exact byte digests are checked here; real decoding and SAM provenance remain
the native projector's responsibility. A receipt proves publication only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
from pathlib import Path

MAX_VIDEO_BYTES = 32 * 1024 * 1024
MAX_ASSETS = 16
MAX_RETAINED_BYTES = 128 * 1024 * 1024
MAX_SAFE_INTEGER = 9_007_199_254_740_991
ASSET_PATTERN = r"^media_[0-9a-f]{32}$"
_SHA = re.compile(r"[0-9a-f]{64}")
_SCOPES = {"local_only", "public_synthetic", "private_cloud_authorized"}
_BINDING_KEYS = {"task_id", "objective_revision", "session_id", "thread_id", "epoch"}


class MediaAssetError(ValueError):
    pass


class MediaAssetNotFound(MediaAssetError):
    pass


def _refuse():
    raise MediaAssetError("Native media asset is unavailable")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _binding(value):
    if type(value) is not dict or set(value) != _BINDING_KEYS:
        _refuse()
    for key in ("task_id", "session_id", "thread_id"):
        item = value[key]
        if (
            type(item) is not str
            or not 1 <= len(item) <= 128
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in item)
        ):
            _refuse()
    for key in ("objective_revision", "epoch"):
        if type(value[key]) is not int or not 1 <= value[key] <= MAX_SAFE_INTEGER:
            _refuse()
    return copy.deepcopy(value)


def _sha(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        _refuse()
    return value


def _video(value):
    if type(value) is not bytes or not 16 <= len(value) <= MAX_VIDEO_BYTES or value[4:8] != b"ftyp":
        _refuse()
    return {
        "sha256": hashlib.sha256(value).hexdigest(),
        "bytes": len(value),
        "mime_type": "video/mp4",
    }


class NativeMediaAssets:
    """Bounded immutable storage outside the generated Agent workspace."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.absolute() != path.resolve() or path.parent.is_symlink():
            _refuse()
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            _refuse()
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                _refuse()
        finally:
            os.close(descriptor)
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS media_assets(
            asset_id TEXT PRIMARY KEY, request_sha256 TEXT UNIQUE NOT NULL,
            receipt_json TEXT NOT NULL, source BLOB NOT NULL, highlighted BLOB NOT NULL)""")

    def _open(self):
        if self._closed:
            _refuse()

    def register(
        self,
        *,
        binding,
        content_scope,
        source_bytes,
        source_sha256,
        rendered_mp4,
        overlay_sha256,
        width,
        height,
        duration_seconds,
        fps_num,
        fps_den,
        frame_count,
    ):
        native_binding = _binding(binding)
        if type(content_scope) is not str or content_scope not in _SCOPES:
            _refuse()
        source, highlighted = _video(source_bytes), _video(rendered_mp4)
        if source["sha256"] != _sha(source_sha256):
            _refuse()
        _sha(overlay_sha256)
        for value, maximum in (
            (width, 8192),
            (height, 8192),
            (fps_num, 1_000_000),
            (fps_den, 1_000_000),
            (frame_count, 100_000),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                _refuse()
        if (
            type(duration_seconds) not in {int, float}
            or not 0 < duration_seconds <= 600
            or not math.isfinite(duration_seconds)
            or not 0 < fps_num / fps_den <= 240
        ):
            _refuse()
        body = {
            "schema_version": "artifact_media.v1",
            "binding": native_binding,
            "content_scope": content_scope,
            "source": source,
            "highlighted": highlighted,
            "overlay_sha256": overlay_sha256,
            "width": width,
            "height": height,
            "duration_seconds": duration_seconds,
            "fps_num": fps_num,
            "fps_den": fps_den,
            "frame_count": frame_count,
            "publication_only": True,
        }
        request_sha = hashlib.sha256(_canonical(body)).hexdigest()
        with self._lock:
            self._open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                old = self._db.execute(
                    "SELECT receipt_json FROM media_assets WHERE request_sha256=?", (request_sha,)
                ).fetchone()
                if old is not None:
                    receipt = json.loads(old[0])
                else:
                    count, retained = self._db.execute(
                        "SELECT COUNT(*),COALESCE(SUM(LENGTH(source)+LENGTH(highlighted)),0) FROM media_assets"
                    ).fetchone()
                    if (
                        count >= MAX_ASSETS
                        or retained + len(source_bytes) + len(rendered_mp4) > MAX_RETAINED_BYTES
                    ):
                        _refuse()
                    receipt = {"asset_id": "media_" + secrets.token_hex(16), **body}
                    self._db.execute(
                        "INSERT INTO media_assets VALUES(?,?,?,?,?)",
                        (
                            receipt["asset_id"],
                            request_sha,
                            _canonical(receipt).decode(),
                            source_bytes,
                            rendered_mp4,
                        ),
                    )
                self._db.execute("COMMIT")
                return copy.deepcopy(receipt)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _row(self, asset_id, binding, content_scope, variant=None):
        native_binding = _binding(binding)
        if (
            type(asset_id) is not str
            or re.fullmatch(ASSET_PATTERN, asset_id) is None
            or type(content_scope) is not str
            or content_scope not in _SCOPES
        ):
            _refuse()
        self._open()
        columns = "receipt_json" if variant is None else "receipt_json," + variant
        row = self._db.execute(
            "SELECT " + columns + " FROM media_assets WHERE asset_id=?", (asset_id,)
        ).fetchone()
        if row is None:
            raise MediaAssetNotFound("Native media asset not found in this scope")
        receipt = json.loads(row["receipt_json"])
        if receipt["binding"] != native_binding or receipt["content_scope"] != content_scope:
            raise MediaAssetNotFound("Native media asset not found in this scope")
        return row, receipt

    def get(self, asset_id, *, binding, content_scope):
        with self._lock:
            _, receipt = self._row(asset_id, binding, content_scope)
            return copy.deepcopy(receipt)

    def content(self, asset_id, *, binding, content_scope, variant):
        if type(variant) is not str or variant not in {"source", "highlighted"}:
            _refuse()
        with self._lock:
            row, receipt = self._row(asset_id, binding, content_scope, variant)
            data = bytes(row[variant])
            if _video(data) != receipt[variant]:
                _refuse()
            return data

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._db.close()
