"""Content-addressed immutable registry with owner-reviewed promotion.

Layout under ``root`` (all writes atomic, audit append-only)::

    versions/<version_id>/source.py      immutable candidate bytes
    versions/<version_id>/manifest.json  validated manifest
    versions/<version_id>/meta.json      sha256, size, registered_at
    reports/<report_id>.json             authoritative evaluation reports
    active.json                          per-plugin {active, history, disabled}
    pins.json                            versions held for running tasks
    audit.jsonl                          one JSON object per state change
    .lock                                single-writer OS file lock (held open)

Threading: an RLock guards short state changes. The lock is never held
during sandbox execution/evaluation so owner disable/stop stays
responsive; run/evaluate snapshot verified bytes under the lock, release
it for isolation, then recheck enabled/integrity before returning.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.plugins.errors import (
    PluginCandidateFailure,
    PluginIntegrityError,
    PluginPromotionError,
    PluginRegistrationError,
    PluginSandboxUnavailable,
    PluginStateError,
)
from core.plugins.manifest import PluginManifest
from core.plugins.sandbox import EVAL_LIMITS, RUN_LIMITS, run_plugin
from core.plugins.schemas import check_schema_profile, validate_against
from core.plugins.sourcecheck import decode_source
from core.plugins.storage import read_bytes
from core.plugins.suite import load_suite, suite_hash_of

MAX_VERSIONS = 128
MAX_REPORTS = 256
MAX_AUDIT_BYTES = 1_048_576

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bounded_owner_entries(directory: Path, prefix: str, limit: int) -> list[Path]:
    """Bound enumeration before opening any metadata, including corrupt debris."""
    result = []
    for count, path in enumerate(directory.iterdir(), 1):
        if count > limit + 64:
            raise PluginStateError("owner metadata directory bound exceeded")
        if path.name.startswith(prefix):
            result.append(path)
            if len(result) > limit:
                raise PluginStateError("owner metadata record bound exceeded")
    return sorted(result)


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _check_plugin_id(value: Any) -> str:
    if not isinstance(value, str) or not _PLUGIN_ID_RE.fullmatch(value):
        raise PluginIntegrityError(f"malformed plugin_id: {value!r}")
    return value


class PluginRegistry:
    """Trusted owner of versions, reports, and activation pointers."""

    def __init__(self, root: Path | str, suites: dict[str, Path | str]) -> None:
        self._thread = threading.RLock()
        self._closed = False
        self._lock_fd: int | None = None
        raw_root = Path(root)
        if str(raw_root) in ("", "/"):
            raise PluginStateError(f"unsafe registry root: {raw_root!r}")
        if raw_root.is_symlink():
            raise PluginStateError(f"registry root is a symlink: {raw_root}")
        if any(part.is_symlink() for part in [raw_root.absolute(), *raw_root.absolute().parents]):
            raise PluginStateError(f"registry root resolves via symlink: {raw_root}")
        try:
            raw_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise PluginStateError(f"registry root unusable: {exc}") from exc
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise PluginStateError(f"registry root unsafe: {raw_root}")
        try:
            if any(
                part.is_symlink() for part in [raw_root.absolute(), *raw_root.absolute().parents]
            ):
                # Symlink component in the root path; fail closed.
                raise PluginStateError(f"registry root resolves via symlink: {raw_root}")
        except OSError as exc:
            raise PluginStateError(f"registry root unusable: {exc}") from exc
        self.root = raw_root
        self._versions = self.root / "versions"
        self._reports = self.root / "reports"
        self._active_file = self.root / "active.json"
        self._pins_file = self.root / "pins.json"
        self._audit_file = self.root / "audit.jsonl"
        self._lock_file = self.root / ".lock"
        if not isinstance(suites, dict):
            raise PluginStateError("suites must be a dict plugin_id->Path")
        cleaned: dict[str, Path] = {}
        for pid, spath in suites.items():
            _check_plugin_id(pid)
            sp = Path(spath)
            if sp.is_symlink():
                raise PluginStateError(f"suite path is a symlink: {pid}")
            if not sp.is_file():
                raise PluginStateError(f"suite path not a regular file: {pid}")
            try:
                if os.path.realpath(sp).startswith(os.path.realpath(self.root) + os.sep):
                    raise PluginStateError(f"suite path under registry root: {pid}")
            except OSError as exc:
                raise PluginStateError(f"suite path unusable: {exc}") from exc
            cleaned[pid] = sp
        self.suites = cleaned
        # Lock before inspecting or mutating durable state, including startup.
        fd = os.open(self._lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise PluginStateError("unsafe registry lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PluginStateError) as exc:
            os.close(fd)
            raise PluginStateError("registry lock unavailable or already owned") from exc
        self._lock_fd = fd
        try:
            self._versions.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._reports.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._versions.is_symlink() or self._reports.is_symlink():
                raise PluginStateError("registry subdirectory is a symlink")
            if self._active_file.is_symlink() or self._pins_file.is_symlink():
                raise PluginStateError("registry state file is a symlink")
            retained = (
                any(self._versions.iterdir())
                or any(self._reports.iterdir())
                or (self.root / "audit.jsonl").exists()
                or self._active_file.exists()
                or self._pins_file.exists()
            )
            if retained and (not self._active_file.exists() or not self._pins_file.exists()):
                raise PluginStateError("Retained plugin activation or pin state is missing")
            if not self._active_file.exists():
                _atomic_write_json(self._active_file, {"plugins": {}})
            if not self._pins_file.exists():
                _atomic_write_json(self._pins_file, {"pinned": {}})
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Release the lifecycle file lock. Idempotent."""
        with self._thread:
            if self._closed:
                return
            self._closed = True
            fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise PluginStateError("registry is closed")

    def _audit(self, event: dict[str, Any]) -> None:
        with self._thread:
            self._ensure_open()
            try:
                if self._audit_file.exists() and not self._audit_file.is_symlink():
                    if self._audit_file.stat().st_size > MAX_AUDIT_BYTES:
                        raise PluginStateError("audit bound exceeded; refusing state change")
                elif self._audit_file.is_symlink():
                    raise PluginIntegrityError("audit file is a symlink")
            except OSError as exc:
                raise PluginStateError(f"audit unavailable: {exc}") from exc
            entry = {"ts": time.time(), **event}
            with open(self._audit_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")

    # -- registration -------------------------------------------------

    def register_bytes(self, source: bytes, manifest: dict) -> str:
        """Store immutable content-addressed bytes; return the version ID."""
        self._ensure_open()
        if not isinstance(source, (bytes, bytearray)):
            raise PluginRegistrationError("source must be bytes")
        if not isinstance(manifest, dict):
            raise PluginRegistrationError("manifest must be a dict")
        raw = bytes(source)
        decode_source(raw)
        try:
            man = PluginManifest.model_validate(manifest)
        except ValueError as exc:
            raise PluginRegistrationError(f"invalid manifest: {exc}") from exc
        man.check_supported()
        check_schema_profile(man.input_schema)
        check_schema_profile(man.output_schema)
        with self._thread:
            self._ensure_open()
            existing = [d for d in self._versions.iterdir() if d.name.startswith("v-")]
            if len(existing) >= MAX_VERSIONS:
                # Deduplication still allowed: identical bytes return same ID.
                digest = _sha256(b"python_json.v1\x00" + raw + b"\x00" + man.canonical().encode())
                probe = "v-" + digest[:24]
                if not (self._versions / probe).exists():
                    raise PluginStateError(
                        f"version bound exceeded ({MAX_VERSIONS}); refusing registration"
                    )
            return self._store(man, raw, event="register")

    def _store(self, manifest: PluginManifest, raw: bytes, *, event: str) -> str:
        digest = _sha256(b"python_json.v1\x00" + raw + b"\x00" + manifest.canonical().encode())
        version_id = "v-" + digest[:24]
        vdir = self._versions / version_id
        meta = {
            "version_id": version_id,
            "plugin_id": manifest.plugin_id,
            "source_sha256": _sha256(raw),
            "source_bytes": len(raw),
            "manifest": json.loads(manifest.canonical()),
            "registered_at": time.time(),
        }
        if vdir.exists():
            if vdir.is_symlink():
                raise PluginIntegrityError("version directory is a symlink")
            _, stored = self._verified_source(version_id)
            if stored.get("source_sha256") != meta["source_sha256"]:
                raise PluginIntegrityError("version directory collision with other bytes")
            return version_id
        tmpdir = Path(tempfile.mkdtemp(dir=str(self._versions), prefix=".tmp-"))
        try:
            (tmpdir / "source.py").write_bytes(raw)
            (tmpdir / "manifest.json").write_text(manifest.canonical(), encoding="utf-8")
            _atomic_write_json(tmpdir / "meta.json", meta)
            os.replace(tmpdir, vdir)
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        self._audit({"event": event, "version_id": version_id})
        return version_id

    def inspect(self, version_id: str) -> dict:
        """Return JSON primitives describing one stored version."""
        self._ensure_open()
        with self._thread:
            _, meta = self._verified_source(version_id)
            return {
                "version_id": meta["version_id"],
                "plugin_id": meta["plugin_id"],
                "source_sha256": meta["source_sha256"],
                "source_bytes": meta["source_bytes"],
                "manifest": meta["manifest"],
                "registered_at": meta["registered_at"],
            }

    def versions(self) -> list[dict]:
        """List stored versions (bounded to MAX_VERSIONS), oldest first."""
        self._ensure_open()
        with self._thread:
            result = []
            for directory in _bounded_owner_entries(self._versions, "v-", MAX_VERSIONS):
                if directory.name.startswith("v-"):
                    _, meta = self._verified_source(directory.name)
                    result.append(
                        {
                            "version_id": meta["version_id"],
                            "plugin_id": meta["plugin_id"],
                            "source_sha256": meta["source_sha256"],
                            "source_bytes": meta["source_bytes"],
                            "manifest": meta["manifest"],
                            "registered_at": meta["registered_at"],
                        }
                    )
            if len(result) > MAX_VERSIONS:
                raise PluginStateError("version bound exceeded")
            return result

    def owner_inventory(self) -> list[dict]:
        """Complete registered-version state without executing a candidate.

        Owner acceptance inputs and paths never leave this projection. Report
        summaries describe stored evidence, not general execution readiness.
        """
        with self._thread:
            rows = self.versions()
            document = self._read_active_doc()
            reports = []
            paths = _bounded_owner_entries(self._reports, "rep-", MAX_REPORTS)
            for path in paths:
                report = self._load_report(path.stem)
                patterns = {
                    "report_id": r"rep-[A-Za-z0-9-]{1,60}",
                    "version_id": r"v-[0-9a-f]{24}",
                    "plugin_id": r"[a-z][a-z0-9_-]{0,63}",
                    "source_sha256": r"[0-9a-f]{64}",
                    "manifest_sha256": r"[0-9a-f]{64}",
                    "suite_sha256": r"[0-9a-f]{64}",
                }
                if (
                    any(
                        type(report.get(key)) is not str
                        or re.fullmatch(pattern, report[key]) is None
                        for key, pattern in patterns.items()
                    )
                    or type(report.get("passed")) is not bool
                    or type(report.get("created_at")) not in {int, float}
                    or not math.isfinite(report["created_at"])
                    or report["created_at"] < 0
                ):
                    raise PluginStateError("Report metadata is malformed")
                # Private acceptance cases/results are never retained by the
                # owner metadata inventory or returned in an Options page.
                reports.append({key: report[key] for key in (*patterns, "passed", "created_at")})
                del report
            reports.sort(key=lambda report: (report["created_at"], report["report_id"]))
            pins = self._read_pins()
            if any(
                key.startswith("task:")
                and (
                    re.fullmatch(r"task:[A-Za-z0-9_-]{1,128}:v-[0-9a-f]{24}", key) is None
                    or key.rsplit(":", 1)[-1] != version
                )
                for key, version in pins.items()
            ):
                raise PluginStateError("Plugin task pin metadata is malformed")
            result = []
            for row in rows:
                identity, version = row["plugin_id"], row["version_id"]
                entry = document["plugins"].get(identity, {})
                if not isinstance(entry, dict):
                    raise PluginStateError("Invalid plugin activation state")
                approved = False
                try:
                    self._approval(row, entry)
                    approved = True
                except PluginPromotionError:
                    pass
                except PluginStateError:
                    pass
                summaries = []
                for report in reports:
                    if report.get("version_id") != version:
                        continue
                    valid = (
                        report.get("passed") is True
                        and report.get("plugin_id") == identity
                        and report.get("source_sha256") == row["source_sha256"]
                        and report.get("manifest_sha256")
                        == _sha256(
                            PluginManifest.model_validate(row["manifest"]).canonical().encode()
                        )
                        and identity in self.suites
                        and report.get("suite_sha256") == suite_hash_of(self.suites[identity])
                    )
                    summaries.append(
                        {
                            "report_id": report["report_id"],
                            "passed": report.get("passed") is True,
                            "current_binding": valid,
                        }
                    )
                history = entry.get("history", [])
                if not isinstance(history, list) or len(history) > MAX_VERSIONS:
                    raise PluginStateError("Invalid plugin activation history")
                rollback = bool(history and history[-1] == version)
                rollback_approved = False
                if rollback:
                    try:
                        self._approval(row, {**entry, "disabled": False})
                        rollback_approved = True
                    except (PluginPromotionError, PluginStateError):
                        pass
                result.append(
                    {
                        **row,
                        "manifest_sha256": _sha256(
                            PluginManifest.model_validate(row["manifest"]).canonical().encode()
                        ),
                        "active_version": entry.get("active"),
                        "enabled": entry.get("disabled") is not True,
                        "approved": approved,
                        "rollback_eligible": rollback_approved,
                        "suite_configured": identity in self.suites,
                        "reports": summaries[-8:],
                        "reports_total": len(summaries),
                        "dependent_task_ids": sorted(
                            key.split(":", 2)[1]
                            for key, pinned in pins.items()
                            if pinned == version and key.startswith("task:")
                        ),
                    }
                )
            return result

    # -- integrity ------------------------------------------------------

    def _version_dir(self, version_id: str) -> Path:
        if (
            not isinstance(version_id, str)
            or not version_id.startswith("v-")
            or len(version_id) != 26
            or "/" in version_id
            or version_id.startswith("v-.")
        ):
            raise PluginIntegrityError(f"malformed version ID: {version_id!r}")
        vdir = self._versions / version_id
        if vdir.is_symlink():
            raise PluginIntegrityError(f"version directory is a symlink: {version_id}")
        if not vdir.is_dir():
            raise PluginIntegrityError(f"unknown version: {version_id}")
        return vdir

    def _verified_source(self, version_id: str) -> tuple[bytes, dict]:
        vdir = self._version_dir(version_id)
        for name in ("source.py", "manifest.json", "meta.json"):
            entry = vdir / name
            if entry.is_symlink():
                raise PluginIntegrityError(f"version file is a symlink: {name}")
            if not entry.is_file():
                raise PluginIntegrityError(f"version file missing: {name}")
        try:
            meta = json.loads(read_bytes(vdir / "meta.json", 65_536))
            if not isinstance(meta, dict):
                raise PluginIntegrityError("version metadata must be an object")
            raw = read_bytes(vdir / "source.py", 65_536)
        except OSError as exc:
            raise PluginIntegrityError(f"version unreadable: {exc}") from exc
        except (ValueError, RecursionError) as exc:
            raise PluginIntegrityError(f"version metadata invalid: {exc}") from exc
        if _sha256(raw) != meta.get("source_sha256"):
            raise PluginIntegrityError(f"source tampering detected: {version_id}")
        try:
            manifest = PluginManifest.model_validate(meta.get("manifest"))
        except ValueError as exc:
            raise PluginIntegrityError(f"stored manifest invalid: {exc}") from exc
        manifest.check_supported()
        try:
            stored_manifest = PluginManifest.model_validate_json(
                read_bytes(vdir / "manifest.json", 65_536)
            )
        except ValueError as exc:
            raise PluginIntegrityError(f"stored manifest invalid: {exc}") from exc
        if manifest.canonical() != stored_manifest.canonical():
            raise PluginIntegrityError(f"manifest tampering detected: {version_id}")
        digest = _sha256(b"python_json.v1\x00" + raw + b"\x00" + manifest.canonical().encode())
        if meta.get("version_id") != version_id or "v-" + digest[:24] != version_id:
            raise PluginIntegrityError(f"content address mismatch: {version_id}")
        if meta.get("plugin_id") != manifest.plugin_id:
            raise PluginIntegrityError(f"plugin_id mismatch: {version_id}")
        return raw, meta

    def _read_active_doc(self) -> dict:
        if self._active_file.is_symlink():
            raise PluginIntegrityError("activation file is a symlink")
        try:
            doc = json.loads(read_bytes(self._active_file, 262_144))
        except (OSError, ValueError, RecursionError) as exc:
            raise PluginIntegrityError(f"activation pointer unreadable: {exc}") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("plugins"), dict):
            raise PluginIntegrityError("activation pointer malformed")
        return doc

    def _write_active_doc(self, doc: dict) -> None:
        _atomic_write_json(self._active_file, doc)

    def _plugin_entry(self, doc: dict, plugin_id: str) -> dict:
        entry = doc["plugins"].get(plugin_id)
        if not isinstance(entry, dict):
            entry = {"active": None, "history": [], "disabled": False}
            doc["plugins"][plugin_id] = entry
        entry.setdefault("active", None)
        entry.setdefault("history", [])
        entry.setdefault("disabled", False)
        if not isinstance(entry["history"], list):
            raise PluginIntegrityError("activation history malformed")
        return entry

    def _read_pins(self) -> dict[str, str]:
        if self._pins_file.is_symlink():
            raise PluginIntegrityError("pins file is a symlink")
        try:
            doc = json.loads(read_bytes(self._pins_file, 65_536))
            pins = doc["pinned"]
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            raise PluginIntegrityError("pins unreadable") from exc
        if isinstance(pins, list):
            pins = {"legacy:" + str(i): value for i, value in enumerate(pins)}
        if (
            not isinstance(pins, dict)
            or len(pins) > 256
            or any(
                not isinstance(key, str) or len(key) > 200 or not isinstance(value, str)
                for key, value in pins.items()
            )
        ):
            raise PluginIntegrityError("pins malformed")
        return pins

    def _write_pins(self, pins: dict[str, str]) -> None:
        _atomic_write_json(self._pins_file, {"pinned": pins})

    def _approval(self, meta: dict, entry: dict | None) -> None:
        if not isinstance(entry, dict) or entry.get("disabled") is True:
            raise PluginStateError("Plugin is inactive or disabled")
        report_id = entry.get("approved", {}).get(meta["version_id"])
        if not isinstance(report_id, str):
            raise PluginPromotionError("Plugin version has not been owner-approved")
        report = self._load_report(report_id)
        manifest = PluginManifest.model_validate(meta["manifest"])
        suite = self.suites.get(meta["plugin_id"])
        if (
            suite is None
            or report.get("passed") is not True
            or any(
                (
                    report.get("version_id") != meta["version_id"],
                    report.get("source_sha256") != meta["source_sha256"],
                    report.get("manifest_sha256") != _sha256(manifest.canonical().encode()),
                    report.get("suite_sha256") != suite_hash_of(suite),
                    report.get("plugin_id") != meta["plugin_id"],
                )
            )
        ):
            raise PluginPromotionError("Approved plugin no longer matches its acceptance report")

    def _load_report(self, report_id: str) -> dict:
        if (
            not isinstance(report_id, str)
            or not report_id.startswith("rep-")
            or "/" in report_id
            or len(report_id) > 64
        ):
            raise PluginPromotionError(f"malformed report ID: {report_id!r}")
        path = self._reports / f"{report_id}.json"
        if path.is_symlink():
            raise PluginPromotionError("report is a symlink")
        try:
            doc = json.loads(read_bytes(path, 1_048_576))
        except (OSError, ValueError, RecursionError) as exc:
            raise PluginPromotionError(f"unknown report: {exc}") from exc
        if not isinstance(doc, dict):
            raise PluginPromotionError("stored report invalid")
        if doc.get("report_id") != report_id:
            raise PluginPromotionError("stored report identity differs")
        return doc

    # -- evaluation -----------------------------------------------------

    def evaluate(self, version_id: str, *, cancel_event=None) -> dict:
        """Evaluate a version on its owner suite; return JSON primitives.

        The mutex is never held during isolated execution so owner
        disable/stop stays responsive. Each case invokes the same isolated
        runner used by :meth:`run` and compares outputs exactly; candidate
        self-reported pass/score fields never count (they are dropped by
        the runner framing).
        """
        import threading as _threading

        self._ensure_open()
        if cancel_event is not None and cancel_event.is_set():
            raise PluginCandidateFailure("evaluation cancelled before start")
        with self._thread:
            self._ensure_open()
            source_bytes, meta = self._verified_source(version_id)
            manifest = PluginManifest.model_validate(meta["manifest"])
            plugin_id = manifest.plugin_id
            source_sha256 = meta["source_sha256"]
            suite_path = self.suites.get(plugin_id)
            if suite_path is None:
                raise PluginIntegrityError(f"no owner suite configured for plugin {plugin_id!r}")
            input_schema = json.loads(json.dumps(manifest.input_schema))
            output_schema = json.loads(json.dumps(manifest.output_schema))
        cases, suite_sha256 = load_suite(suite_path, expected_plugin_id=plugin_id)
        # Owner suite inputs must themselves satisfy the declared input schema.
        for index, case in enumerate(cases):
            try:
                validate_against(input_schema, case["input"], what=f"suite case {index}")
            except PluginCandidateFailure as exc:
                raise PluginIntegrityError(
                    f"suite case {index} violates input_schema: {exc}"
                ) from exc
        if cancel_event is not None and not isinstance(cancel_event, _threading.Event):
            raise PluginStateError("cancel_event must be a threading.Event or None")
        case_results: list[dict] = []
        reasons: list[str] = []
        all_passed = True
        for index, case in enumerate(cases):
            if cancel_event is not None and cancel_event.is_set():
                raise PluginCandidateFailure("evaluation cancelled; no report stored")
            try:
                actual = run_plugin(
                    source_bytes, case["input"], limits=EVAL_LIMITS, cancel_event=cancel_event
                )
            except PluginSandboxUnavailable:
                raise
            except PluginCandidateFailure as exc:
                all_passed = False
                case_results.append(
                    {
                        "input": case["input"],
                        "expected": case["expected"],
                        "actual": None,
                        "passed": False,
                        "failure": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                reasons.append(f"FAIL case {index}: {exc}"[:300])
                continue
            except Exception as exc:  # noqa: BLE001 - failures are data
                all_passed = False
                case_results.append(
                    {
                        "input": case["input"],
                        "expected": case["expected"],
                        "actual": None,
                        "passed": False,
                        "failure": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                reasons.append(f"FAIL case {index}: {type(exc).__name__}"[:300])
                continue
            try:
                validate_against(output_schema, actual, what=f"case {index} output")
            except PluginCandidateFailure as exc:
                all_passed = False
                case_results.append(
                    {
                        "input": case["input"],
                        "expected": case["expected"],
                        "actual": actual,
                        "passed": False,
                        "failure": str(exc)[:300],
                    }
                )
                reasons.append(f"FAIL case {index}: output schema: {exc}"[:300])
                continue
            if _canonical(actual) == _canonical(case["expected"]):
                case_results.append(
                    {
                        "input": case["input"],
                        "expected": case["expected"],
                        "actual": actual,
                        "passed": True,
                        "failure": None,
                    }
                )
                reasons.append(f"PASS case {index}")
            else:
                all_passed = False
                case_results.append(
                    {
                        "input": case["input"],
                        "expected": case["expected"],
                        "actual": actual,
                        "passed": False,
                        "failure": "output mismatch",
                    }
                )
                reasons.append(f"FAIL case {index}: output mismatch")
        if cancel_event is not None and cancel_event.is_set():
            raise PluginCandidateFailure("evaluation cancelled; no report stored")
        report_id = "rep-" + uuid.uuid4().hex[:16]
        report = {
            "report_id": report_id,
            "version_id": version_id,
            "plugin_id": plugin_id,
            "source_sha256": source_sha256,
            "manifest_sha256": _sha256(manifest.canonical().encode()),
            "suite_sha256": suite_sha256,
            "suite_cases": len(cases),
            "passed": bool(all_passed),
            "cases": len(cases),
            "case_results": case_results,
            "reasons": reasons,
            "created_at": time.time(),
        }
        with self._thread:
            self._ensure_open()
            _, fresh_meta = self._verified_source(version_id)
            if fresh_meta["source_sha256"] != source_sha256:
                raise PluginIntegrityError("candidate source changed during evaluation")
            existing_reports = [p for p in self._reports.iterdir() if p.name.startswith("rep-")]
            if len(existing_reports) >= MAX_REPORTS:
                raise PluginStateError(
                    f"report bound exceeded ({MAX_REPORTS}); refusing evaluation"
                )
            _atomic_write_json(self._reports / f"{report_id}.json", report)
        self._audit(
            {
                "event": "evaluate",
                "version_id": version_id,
                "report_id": report_id,
                "passed": bool(all_passed),
            }
        )
        return {
            "report_id": report_id,
            "version_id": version_id,
            "source_sha256": source_sha256,
            "suite_sha256": suite_sha256,
            "passed": bool(all_passed),
            "cases": len(cases),
            "reasons": list(reasons),
        }

    # -- promotion / activation ------------------------------------------

    def promote(
        self,
        version_id: str,
        report_id: str,
        *,
        actor: str = "owner",
        reviewed: bool = False,
    ) -> str:
        """Activate only on the stored authoritative passing report.

        Never trusts a caller-supplied score: the report is reloaded from
        disk, its binding re-verified (same source, manifest, and suite),
        and automatic (unreviewed) promotion is always refused. The
        candidate cannot activate itself.
        """
        self._ensure_open()
        if actor != "owner":
            raise PluginPromotionError("promotion requires actor='owner'")
        if reviewed is not True:
            raise PluginPromotionError("promotion requires explicit reviewed=True")
        with self._thread:
            self._ensure_open()
            _, meta = self._verified_source(version_id)
            manifest = PluginManifest.model_validate(meta["manifest"])
            plugin_id = manifest.plugin_id
            report = self._load_report(report_id)
            if report.get("version_id") != version_id:
                raise PluginPromotionError("report is not bound to this version")
            if report.get("passed") is not True:
                raise PluginPromotionError("stored report did not pass; cannot promote")
            if report.get("source_sha256") != meta["source_sha256"]:
                raise PluginPromotionError("candidate source changed since evaluation")
            if report.get("plugin_id") != plugin_id:
                raise PluginPromotionError("report plugin binding mismatch")
            if report.get("manifest_sha256") != _sha256(manifest.canonical().encode()):
                raise PluginPromotionError("candidate manifest changed since evaluation")
            suite_path = self.suites.get(plugin_id)
            if suite_path is None:
                raise PluginIntegrityError(f"no owner suite for plugin {plugin_id!r}")
            try:
                current_suite_hash = suite_hash_of(suite_path)
            except PluginIntegrityError as exc:
                raise PluginPromotionError(f"owner suite unreadable: {exc}") from exc
            if report.get("suite_sha256") != current_suite_hash:
                raise PluginPromotionError("suite changed since evaluation")
            doc = self._read_active_doc()
            entry = self._plugin_entry(doc, plugin_id)
            previous = entry.get("active")
            if previous is not None:
                self._verified_source(previous)
                history = entry.get("history", [])
                if previous not in history:
                    history.append(previous)
            entry.setdefault("approved", {})[version_id] = report_id
            entry["active"] = version_id
            entry["disabled"] = False
            self._write_active_doc(doc)
        self._audit(
            {
                "event": "promote",
                "plugin_id": plugin_id,
                "version_id": version_id,
                "report_id": report_id,
            }
        )
        return version_id

    def active(self, plugin_id: str) -> str | None:
        """Current active version for a plugin, or None when inactive."""
        self._ensure_open()
        _check_plugin_id(plugin_id)
        with self._thread:
            doc = self._read_active_doc()
            entry = doc["plugins"].get(plugin_id)
            if not isinstance(entry, dict):
                return None
            if entry.get("disabled") is True:
                return None
            current = entry.get("active")
            if current is None:
                return None
            _, meta = self._verified_source(current)
            self._approval(meta, entry)
            return current

    def rollback(self, plugin_id: str, *, actor: str = "owner", reviewed: bool = False) -> str:
        """Restore the previous approved intact version.

        Only rolls back to a previously approved version in history whose
        bytes still verify. The current owner suite binding is rechecked;
        failures stay inactive.
        """
        self._ensure_open()
        _check_plugin_id(plugin_id)
        if actor != "owner":
            raise PluginPromotionError("rollback requires actor='owner'")
        if reviewed is not True:
            raise PluginPromotionError("rollback requires explicit reviewed=True")
        with self._thread:
            self._ensure_open()
            doc = self._read_active_doc()
            entry = self._plugin_entry(doc, plugin_id)
            history = entry.get("history", [])
            if not history:
                raise PluginPromotionError("no previous version to roll back to")
            target = history[-1]
            _, meta = self._verified_source(target)
            # Disabled state revokes runs, but an explicit reviewed rollback may restore it.
            self._approval(meta, {**entry, "disabled": False})
            suite_path = self.suites.get(plugin_id)
            if suite_path is not None:
                try:
                    suite_hash_of(suite_path)
                except PluginIntegrityError as exc:
                    raise PluginPromotionError(
                        f"owner suite unreadable; refusing rollback: {exc}"
                    ) from exc
            entry["active"] = target
            entry["history"] = history[:-1]
            entry["disabled"] = False
            self._write_active_doc(doc)
        self._audit({"event": "rollback", "plugin_id": plugin_id, "version_id": target})
        return target

    def disable(self, plugin_id: str, *, actor: str = "owner") -> None:
        """Revoke future runs for a plugin, including pinned versions."""
        self._ensure_open()
        _check_plugin_id(plugin_id)
        if actor != "owner":
            raise PluginPromotionError("disable requires actor='owner'")
        with self._thread:
            self._ensure_open()
            doc = self._read_active_doc()
            entry = self._plugin_entry(doc, plugin_id)
            entry["disabled"] = True
            self._write_active_doc(doc)
        self._audit({"event": "disable", "plugin_id": plugin_id})

    def remove(self, version_id: str, *, actor: str = "owner") -> None:
        """Remove a version. Refuses active/history-required/pinned versions.

        Once its plugin is disabled and no pins hold it, removal is allowed
        (with audit) even for previously active/history versions.
        """
        self._ensure_open()
        if actor != "owner":
            raise PluginPromotionError("remove requires actor='owner'")
        with self._thread:
            self._ensure_open()
            _, meta = self._verified_source(version_id)
            plugin_id = meta["plugin_id"]
            pins = self._read_pins()
            if version_id in pins.values():
                raise PluginStateError(f"version is pinned for running tasks: {version_id}")
            doc = self._read_active_doc()
            entry = doc["plugins"].get(plugin_id)
            disabled = bool(isinstance(entry, dict) and entry.get("disabled") is True)
            if isinstance(entry, dict) and not disabled:
                if entry.get("active") == version_id:
                    raise PluginStateError(f"version is active: {version_id}")
                if version_id in (entry.get("history") or []):
                    raise PluginStateError(
                        f"version is history-required for rollback: {version_id}"
                    )
            vdir = self._version_dir(version_id)
            if vdir.is_symlink():
                raise PluginIntegrityError("version directory is a symlink")
            shutil.rmtree(vdir)
            if isinstance(entry, dict):
                if entry.get("active") == version_id:
                    entry["active"] = None
                entry["history"] = [h for h in (entry.get("history") or []) if h != version_id]
                self._write_active_doc(doc)
        self._audit({"event": "remove", "version_id": version_id})

    def pin(self, version_id: str, *, task_id: str | None = None) -> None:
        self._ensure_open()
        if task_id is not None and (
            not isinstance(task_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id) is None
        ):
            raise PluginStateError("Invalid task pin identity")
        with self._thread:
            self._verified_source(version_id)
            pins = self._read_pins()
            key = (
                "task:" + task_id + ":" + version_id
                if task_id is not None
                else "anonymous:" + uuid.uuid4().hex
            )
            if key not in pins and len(pins) >= 256:
                raise PluginStateError("Plugin task pin bound exceeded")
            pins[key] = version_id
            self._write_pins(pins)

    def unpin(self, version_id: str, *, task_id: str | None = None) -> None:
        self._ensure_open()
        with self._thread:
            pins = self._read_pins()
            if task_id is not None:
                pins.pop("task:" + task_id + ":" + version_id, None)
            else:
                key = next(
                    (
                        key
                        for key, value in pins.items()
                        if value == version_id and not key.startswith("task:")
                    ),
                    None,
                )
                if key is not None:
                    pins.pop(key)
            self._write_pins(pins)

    def reconcile_task_pins(self, tasks: dict[str, dict[str, str]]) -> None:
        """Startup only: replace task references from the native task journal."""
        with self._thread:
            self._ensure_open()
            pins = {k: v for k, v in self._read_pins().items() if not k.startswith("task:")}
            for task_id, versions in tasks.items():
                if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id) is None or len(versions) > 16:
                    raise PluginStateError("Invalid recovered task pin identity")
                for plugin_id, version_id in versions.items():
                    _, meta = self._verified_source(version_id)
                    if meta["plugin_id"] != plugin_id:
                        raise PluginStateError("Recovered plugin identity does not match version")
                    pins["task:" + task_id + ":" + version_id] = version_id
            if len(pins) > 256:
                raise PluginStateError("Plugin task pin bound exceeded")
            self._write_pins(pins)

    # -- invocation -------------------------------------------------------

    def run(self, version_id: str, arguments: dict, *, cancel_event=None) -> dict:
        """Run an exact version; return ``{"output": ..., "receipt": ...}``.

        The mutex is never held during execution. After the run, enabled
        state and content integrity are rechecked before the receipt is
        returned, so an owner disable that lands mid-run revokes the
        result. The receipt proves which bytes computed which output; it
        proves nothing about external side effects or domain correctness.
        A result may contain ``success`` fields as untrusted DATA only.
        """
        import threading as _threading

        self._ensure_open()
        if not isinstance(arguments, dict):
            raise PluginCandidateFailure("arguments must be an object")
        if cancel_event is not None and not isinstance(cancel_event, _threading.Event):
            raise PluginStateError("cancel_event must be a threading.Event or None")
        if cancel_event is not None and cancel_event.is_set():
            raise PluginCandidateFailure("run cancelled before start")
        with self._thread:
            self._ensure_open()
            source_bytes, meta = self._verified_source(version_id)
            manifest = PluginManifest.model_validate(meta["manifest"])
            plugin_id = manifest.plugin_id
            source_sha256 = meta["source_sha256"]
            doc = self._read_active_doc()
            entry = doc["plugins"].get(plugin_id)
            self._approval(meta, entry)
            input_schema = json.loads(json.dumps(manifest.input_schema))
            output_schema = json.loads(json.dumps(manifest.output_schema))
        validate_against(input_schema, arguments, what="arguments")
        arguments_sha256 = _sha256(_canonical(arguments))
        started = time.monotonic()
        output = run_plugin(source_bytes, arguments, limits=RUN_LIMITS, cancel_event=cancel_event)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        validate_against(output_schema, output, what="output")
        output_sha256 = _sha256(_canonical(output))
        with self._thread:
            self._ensure_open()
            _, fresh_meta = self._verified_source(version_id)
            if fresh_meta["source_sha256"] != source_sha256:
                raise PluginIntegrityError("candidate source changed during run")
            fresh_doc = self._read_active_doc()
            fresh_entry = fresh_doc["plugins"].get(plugin_id)
            self._approval(fresh_meta, fresh_entry)
        receipt = {
            "version_id": version_id,
            "source_sha256": source_sha256,
            "arguments_sha256": arguments_sha256,
            "output_sha256": output_sha256,
            "elapsed_ms": elapsed_ms,
        }
        return {"output": output, "receipt": receipt}
