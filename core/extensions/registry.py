"""Content-addressed immutable registry with owner-reviewed promotion.

Layout under ``root`` (all writes atomic, audit append-only)::

    versions/<version_id>/source.py      immutable candidate bytes
    versions/<version_id>/manifest.json  validated manifest
    versions/<version_id>/meta.json      sha256, size, registered_at
    reports/<report_id>.json             authoritative evaluation reports
    active.json                          {"active": <id>, "history": [...]}
    audit.jsonl                          one JSON object per state change
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from core.extensions import evaluator
from core.extensions.errors import (
    IntegrityError,
    PromotionError,
    RegistrationError,
)
from core.extensions.manifest import CONTRACT, Manifest
from core.extensions.payloads import check_output_ids, validate_payload
from core.extensions.sandbox import SandboxLimits, run_selector
from core.extensions.sourcecheck import decode_source

EVAL_LIMITS = SandboxLimits(wall_timeout_s=5.0, cpu_s=10, memory_mb=256)
RUN_LIMITS = SandboxLimits(wall_timeout_s=10.0, cpu_s=15, memory_mb=256)

BASELINE_NAME = "baseline-keyword"
MAX_SELECTION_AUDIT_BYTES = 4_000_000
MAX_SELECTION_VERSIONS = 4096


class EvaluationReport(BaseModel):
    """Authoritative, serializable record of one evaluation."""

    model_config = ConfigDict(extra="forbid")

    report_id: str
    candidate_version_id: str
    candidate_source_sha256: str
    baseline_version_id: str
    baseline_source_sha256: str
    suite_hash: str
    suite_cases: int
    policy: dict[str, Any]
    policy_version: str
    model_profile: dict[str, Any] | None
    candidate: dict[str, Any]
    baseline: dict[str, Any]
    passed: bool
    reasons: list[str]
    created_at: float


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _gate_baseline_health(
    base: dict[str, Any], passed: bool, reasons: list[str]
) -> tuple[bool, list[str]]:
    """A green candidate requires a valid, comparable baseline.

    The non-inferiority comparison in :mod:`core.extensions.evaluator` is
    unchanged, but a baseline that failed any case cannot anchor
    promotion: force fail with an explicit reason.
    """
    reasons = list(reasons)
    if base.get("n_fail", 0) > 0:
        reasons.append(f"FAIL baseline failed {base['n_fail']} case(s); no comparable promotion")
        return False, reasons
    return passed, reasons


class Registry:
    """Trusted owner of versions, reports, and the activation pointer."""

    def __init__(
        self,
        root: Path | str,
        suite_path: Path | str,
        model_profile: dict | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.root = Path(root)
        self.suite_path = Path(suite_path)
        self.model_profile = dict(model_profile) if model_profile else None
        self._versions = self.root / "versions"
        self._reports = self.root / "reports"
        self._active_file = self.root / "active.json"
        self._audit_file = self.root / "audit.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)
        self._versions.mkdir(parents=True, exist_ok=True)
        self._reports.mkdir(parents=True, exist_ok=True)
        baseline_id = self._seed_baseline()
        if not self._active_file.exists():
            self._write_active(baseline_id, previous=None, event="init")

    # -- registration -------------------------------------------------

    def _seed_baseline(self) -> str:
        from core.extensions import baseline_selector  # noqa: PLC0415

        raw = Path(baseline_selector.__file__).read_bytes()
        manifest = Manifest(name=BASELINE_NAME, description="Pinned keyword baseline")
        version_id = self._store(manifest, raw, event="seed-baseline")
        self._reject_stale_baseline(version_id)
        return version_id

    def _reject_stale_baseline(self, current_id: str) -> None:
        """Fail closed if a different baseline-named version already exists.

        The pinned baseline is content-addressed; a second baseline-named
        version ID means the baseline source changed under an existing
        registry, so seeding must refuse rather than silently re-pin.
        """
        for meta_path in self._versions.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            manifest = meta.get("manifest", {})
            if (
                isinstance(manifest, dict)
                and manifest.get("name") == BASELINE_NAME
                and meta.get("version_id") != current_id
            ):
                raise IntegrityError(
                    "pinned baseline changed under existing registry; refusing to re-seed"
                )

    def register(self, source_path: Path | str, manifest: Manifest | dict | None = None) -> str:
        """Store immutable content-addressed bytes; return the version ID."""
        src = Path(source_path)
        if not src.is_file() or src.is_symlink():
            raise RegistrationError(f"not a regular file: {source_path}")
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            import stat

            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RegistrationError("source must be a regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(65_537)
        finally:
            os.close(fd)
        return self.register_bytes(raw, manifest)

    def register_bytes(self, raw: bytes, manifest: Manifest | dict | None = None) -> str:
        """The trusted broker supplies bounded, descriptor-checked source bytes."""
        decode_source(raw)
        man = (
            manifest
            if isinstance(manifest, Manifest)
            else Manifest.model_validate(manifest or {"name": "candidate"})
        )
        man.check_supported()
        with self._lock:
            return self._store(man, raw, event="register")

    def versions(self) -> list[dict]:
        result = []
        for directory in sorted(self._versions.iterdir()):
            if directory.name.startswith("v-"):
                _, meta = self._verified_source(directory.name)
                result.append(meta)
        return result

    def _selection_promotions(self) -> dict[str, list[str]]:
        """Only protected native promotion receipts can establish past approval."""
        if not self._audit_file.exists():
            return {}
        with self._audit_file.open("rb") as stream:
            raw = stream.read(MAX_SELECTION_AUDIT_BYTES + 1)
        if len(raw) > MAX_SELECTION_AUDIT_BYTES:
            raise IntegrityError("Harness selection audit exceeds its inspection bound")
        result: dict[str, list[str]] = {}
        try:
            for line in raw.splitlines():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Invalid promotion receipt")
                if event.get("event") == "promote":
                    version, report = event.get("active"), event.get("report_id")
                    if not isinstance(version, str) or not isinstance(report, str):
                        raise ValueError("Invalid promotion receipt")
                    result.setdefault(version, []).append(report)
        except (ValueError, UnicodeError) as exc:
            raise IntegrityError("Harness promotion history is unavailable") from exc
        return result

    def _selection_eligibility(self, version_id, active, promotions) -> str:
        self._verified_source(version_id)
        if version_id == active:
            return "active"
        if version_id == self._baseline_id():
            return "approved_baseline"
        if promotions is None:
            return "unavailable"
        for report_id in reversed(promotions.get(version_id, [])):
            try:
                self._check_promotion_report(version_id, report_id)
            except (OSError, ValueError, IntegrityError, PromotionError):
                continue
            return "owner_promoted"
        return "unapproved"

    def selection_eligibility(self, version_id: str) -> str:
        with self._lock:
            try:
                promotions = self._selection_promotions()
            except IntegrityError:
                promotions = None
            return self._selection_eligibility(version_id, self.active, promotions)

    def selection_page(self, *, after: str | None = None, limit: int = 20) -> dict:
        """Bounded owner metadata; this method neither evaluates nor activates."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid harness page limit")
        with self._lock:
            names = []
            for count, directory in enumerate(self._versions.iterdir()):
                if count >= MAX_SELECTION_VERSIONS:
                    raise IntegrityError("Harness catalog exceeds its inspection bound")
                if directory.name.startswith("v-") and (after is None or directory.name > after):
                    names.append(directory.name)
            names.sort()
            active = self.active
            try:
                promotions = self._selection_promotions()
            except IntegrityError:
                promotions = None
            items = []
            for version_id in names[:limit]:
                _, meta = self._verified_source(version_id)
                eligibility = self._selection_eligibility(version_id, active, promotions)
                items.append({**meta, "eligibility": eligibility, "active": version_id == active})
            return {"items": items, "next_cursor": names[limit - 1] if len(names) > limit else None}

    def _store(self, manifest: Manifest, raw: bytes, *, event: str) -> str:
        digest = _sha256(
            CONTRACT.encode() + b"\x00" + raw + b"\x00" + manifest.canonical().encode()
        )
        version_id = "v-" + digest[:24]
        vdir = self._versions / version_id
        meta = {
            "version_id": version_id,
            "source_sha256": _sha256(raw),
            "source_bytes": len(raw),
            "manifest": json.loads(manifest.canonical()),
            "registered_at": time.time(),
        }
        if vdir.exists():
            stored = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
            if stored["source_sha256"] != meta["source_sha256"]:
                raise IntegrityError("version directory collision with other bytes")
            return version_id
        tmpdir = Path(tempfile.mkdtemp(dir=str(self._versions), prefix=".tmp-"))
        try:
            (tmpdir / "source.py").write_bytes(raw)
            (tmpdir / "manifest.json").write_text(manifest.canonical(), encoding="utf-8")
            _atomic_write_json(tmpdir / "meta.json", meta)
            os.replace(tmpdir, vdir)
        except BaseException:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        self._audit({"event": event, "version_id": version_id})
        return version_id

    # -- integrity ------------------------------------------------------

    def _version_dir(self, version_id: str) -> Path:
        if (
            not version_id.startswith("v-")
            or len(version_id) != 26
            or "/" in version_id
            or version_id.startswith("v-.")
        ):
            raise IntegrityError(f"malformed version ID: {version_id!r}")
        vdir = self._versions / version_id
        if vdir.is_symlink():
            raise IntegrityError(f"version directory is a symlink: {version_id}")
        if not vdir.is_dir():
            raise IntegrityError(f"unknown version: {version_id}")
        return vdir

    def _verified_source(self, version_id: str) -> tuple[Path, dict]:
        vdir = self._version_dir(version_id)
        for name in ("source.py", "manifest.json", "meta.json"):
            entry = vdir / name
            if entry.is_symlink():
                raise IntegrityError(f"version file is a symlink: {name}")
            if not entry.is_file():
                raise IntegrityError(f"version file missing: {name}")
        try:
            meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
            raw = (vdir / "source.py").read_bytes()
        except OSError as exc:
            raise IntegrityError(f"version unreadable: {exc}") from exc
        if _sha256(raw) != meta.get("source_sha256"):
            raise IntegrityError(f"source tampering detected: {version_id}")
        manifest = Manifest.model_validate(meta["manifest"])
        manifest.check_supported()
        stored_manifest = Manifest.model_validate_json((vdir / "manifest.json").read_bytes())
        if manifest.canonical() != stored_manifest.canonical():
            raise IntegrityError(f"manifest tampering detected: {version_id}")
        digest = _sha256(
            CONTRACT.encode() + b"\x00" + raw + b"\x00" + manifest.canonical().encode()
        )
        if meta.get("version_id") != version_id or "v-" + digest[:24] != version_id:
            raise IntegrityError(f"content address mismatch: {version_id}")
        return vdir / "source.py", meta

    # -- evaluation -----------------------------------------------------

    def evaluate(self, version_id: str) -> EvaluationReport:
        """Evaluate a candidate vs the pinned baseline on the heldout suite."""
        from evaluation.harness.policy import DEFAULT_POLICY, POLICY_VERSION
        from evaluation.harness.suite import load_suite

        source_file, meta = self._verified_source(version_id)
        baseline_id = self._baseline_id()
        baseline_file, baseline_meta = self._verified_source(baseline_id)
        cases, suite_hash = load_suite(self.suite_path)
        policy = dict(DEFAULT_POLICY)
        cand_cases, base_cases = [], []
        for case in cases:
            cand_cases.append(
                evaluator.run_case(
                    source_file,
                    case["payload"],
                    case["expected_ids"],
                    limits=EVAL_LIMITS,
                )
            )
            base_cases.append(
                evaluator.run_case(
                    baseline_file,
                    case["payload"],
                    case["expected_ids"],
                    limits=EVAL_LIMITS,
                )
            )
        cand = evaluator.summarize(cand_cases, len(cases))
        base = evaluator.summarize(base_cases, len(cases))
        passed, reasons = evaluator.verdict(cand, base, policy)
        passed, reasons = _gate_baseline_health(base, passed, reasons)
        report = EvaluationReport(
            report_id="rep-" + uuid.uuid4().hex[:16],
            candidate_version_id=version_id,
            candidate_source_sha256=meta["source_sha256"],
            baseline_version_id=baseline_id,
            baseline_source_sha256=baseline_meta["source_sha256"],
            suite_hash=suite_hash,
            suite_cases=len(cases),
            policy=policy,
            policy_version=POLICY_VERSION,
            model_profile=self.model_profile,
            candidate=cand,
            baseline=base,
            passed=passed,
            reasons=reasons,
            created_at=time.time(),
        )
        with self._lock:
            _atomic_write_json(
                self._reports / f"{report.report_id}.json",
                json.loads(report.model_dump_json()),
            )
        self._audit(
            {
                "event": "evaluate",
                "version_id": version_id,
                "report_id": report.report_id,
                "passed": passed,
            }
        )
        return report

    # -- promotion / rollback / activation -------------------------------

    def _read_active(self) -> dict:
        try:
            return json.loads(self._active_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IntegrityError(f"activation pointer unreadable: {exc}") from exc

    def _write_active(self, active: str, *, previous: str | None, event: str, **extra: Any) -> None:
        with self._lock:
            try:
                cur = self._read_active() if self._active_file.exists() else None
            except IntegrityError:
                cur = None
            history = list((cur or {}).get("history", []))
            if previous is not None:
                history.append(previous)
            _atomic_write_json(self._active_file, {"active": active, "history": history})
            self._audit({"event": event, "active": active, **extra})

    @property
    def active(self) -> str:
        """Pinned active version ID (never edited by candidates)."""
        doc = self._read_active()
        active = doc.get("active")
        if not isinstance(active, str):
            raise IntegrityError("activation pointer malformed")
        self._verified_source(active)
        return active

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
        disk, its binding re-verified, and automatic (unreviewed) promotion
        is always refused.
        """
        if actor != "owner":
            raise PromotionError("promotion requires actor='owner'")
        if reviewed is not True:
            raise PromotionError("promotion requires explicit reviewed=True")
        report = self._check_promotion_report(version_id, report_id)
        previous = self.active
        self._write_active(
            version_id,
            previous=previous,
            event="promote",
            report_id=report.report_id,
        )
        return version_id

    def _check_promotion_report(self, version_id: str, report_id: str) -> EvaluationReport:
        report = self._load_report(report_id)
        if report.candidate_version_id != version_id:
            raise PromotionError("report is not bound to this candidate version")
        if report.passed is not True:
            raise PromotionError("stored report did not pass; cannot promote")
        if report.baseline.get("n_fail", 0) > 0:
            raise PromotionError("baseline failed at evaluation; no comparable promotion")
        _, meta = self._verified_source(version_id)
        if meta["source_sha256"] != report.candidate_source_sha256:
            raise PromotionError("candidate source changed since evaluation")
        from evaluation.harness.policy import DEFAULT_POLICY, POLICY_VERSION
        from evaluation.harness.suite import suite_hash_of

        if suite_hash_of(self.suite_path) != report.suite_hash:
            raise PromotionError("suite changed since evaluation")
        if report.policy != dict(DEFAULT_POLICY):
            raise PromotionError("report policy is not the predeclared policy")
        if report.policy_version != POLICY_VERSION:
            raise PromotionError("report policy version is not current")
        if report.model_profile != self.model_profile:
            raise PromotionError("report model profile does not match this registry")
        return report

    def rollback(self, *, actor: str = "owner") -> str:
        """Restore the previous known-good version; rejected versions stay out."""
        if actor != "owner":
            raise PromotionError("rollback requires actor='owner'")
        with self._lock:
            doc = self._read_active()
            history = doc.get("history", [])
            if not history:
                raise PromotionError("no previous version to roll back to")
            target = history[-1]
            self._verified_source(target)
            remaining = history[:-1]
            _atomic_write_json(self._active_file, {"active": target, "history": remaining})
            self._audit({"event": "rollback", "active": target})
            return target

    def _load_report(self, report_id: str) -> EvaluationReport:
        if not report_id.startswith("rep-") or "/" in report_id or len(report_id) > 64:
            raise PromotionError(f"malformed report ID: {report_id!r}")
        path = self._reports / f"{report_id}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PromotionError(f"unknown report: {exc}") from exc
        try:
            return EvaluationReport.model_validate(doc)
        except ValueError as exc:
            raise PromotionError(f"stored report invalid: {exc}") from exc

    # -- execution --------------------------------------------------------

    def run(self, version_id: str, payload: dict) -> list[str]:
        """Run any registered version (incl. older pins) on a bounded payload."""
        clean = validate_payload(payload)
        source_file, _ = self._verified_source(version_id)
        ids = run_selector(source_file, clean, limits=RUN_LIMITS)
        return check_output_ids(ids, clean)

    # -- internals --------------------------------------------------------

    def _baseline_id(self) -> str:
        raw = Path(self._baseline_file()).read_bytes()
        from core.extensions.manifest import Manifest as _M

        manifest = _M(name=BASELINE_NAME, description="Pinned keyword baseline")
        digest = _sha256(
            CONTRACT.encode() + b"\x00" + raw + b"\x00" + manifest.canonical().encode()
        )
        return "v-" + digest[:24]

    def _baseline_file(self) -> str:
        from core.extensions import baseline_selector  # noqa: PLC0415

        return baseline_selector.__file__

    def _audit(self, event: dict[str, Any]) -> None:
        with self._lock:
            entry = {"ts": time.time(), **event}
            with open(self._audit_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
