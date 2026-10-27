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


class Registry:
    """Trusted owner of versions, reports, and the activation pointer."""

    def __init__(
        self,
        root: Path | str,
        suite_path: Path | str,
        model_profile: dict | None = None,
    ) -> None:
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
        return self._store(manifest, raw, event="seed-baseline")

    def register(
        self, source_path: Path | str, manifest: Manifest | dict | None = None
    ) -> str:
        """Store immutable content-addressed bytes; return the version ID."""
        src = Path(source_path)
        if not src.is_file() or src.is_symlink():
            raise RegistrationError(f"not a regular file: {source_path}")
        raw = src.read_bytes()
        decode_source(raw)
        man = (
            manifest
            if isinstance(manifest, Manifest)
            else Manifest.model_validate(manifest or {"name": "candidate"})
        )
        man.check_supported()
        return self._store(man, raw, event="register")

    def _store(self, manifest: Manifest, raw: bytes, *, event: str) -> str:
        digest = _sha256(CONTRACT.encode() + b"\x00" + raw + b"\x00" + manifest.canonical().encode())
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
        tmpdir = Path(
            tempfile.mkdtemp(dir=str(self._versions), prefix=".tmp-")
        )
        try:
            (tmpdir / "source.py").write_bytes(raw)
            (tmpdir / "manifest.json").write_text(
                manifest.canonical(), encoding="utf-8"
            )
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
        if not vdir.is_dir():
            raise IntegrityError(f"unknown version: {version_id}")
        return vdir

    def _verified_source(self, version_id: str) -> tuple[Path, dict]:
        vdir = self._version_dir(version_id)
        try:
            meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
            raw = (vdir / "source.py").read_bytes()
        except OSError as exc:
            raise IntegrityError(f"version unreadable: {exc}") from exc
        if _sha256(raw) != meta.get("source_sha256"):
            raise IntegrityError(f"source tampering detected: {version_id}")
        manifest = Manifest.model_validate(meta["manifest"])
        manifest.check_supported()
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
                    source_file, case["payload"], case["expected_ids"],
                    limits=EVAL_LIMITS,
                )
            )
            base_cases.append(
                evaluator.run_case(
                    baseline_file, case["payload"], case["expected_ids"],
                    limits=EVAL_LIMITS,
                )
            )
        cand = evaluator.summarize(cand_cases, len(cases))
        base = evaluator.summarize(base_cases, len(cases))
        passed, reasons = evaluator.verdict(cand, base, policy)
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

    def _write_active(
        self, active: str, *, previous: str | None, event: str, **extra: Any
    ) -> None:
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
        report = self._load_report(report_id)
        if report.candidate_version_id != version_id:
            raise PromotionError("report is not bound to this candidate version")
        if report.passed is not True:
            raise PromotionError("stored report did not pass; cannot promote")
        _, meta = self._verified_source(version_id)
        if meta["source_sha256"] != report.candidate_source_sha256:
            raise PromotionError("candidate source changed since evaluation")
        from evaluation.harness.policy import DEFAULT_POLICY
        from evaluation.harness.suite import suite_hash_of

        if suite_hash_of(self.suite_path) != report.suite_hash:
            raise PromotionError("suite changed since evaluation")
        if report.policy != dict(DEFAULT_POLICY):
            raise PromotionError("report policy is not the predeclared policy")
        if report.model_profile != self.model_profile:
            raise PromotionError("report model profile does not match this registry")
        previous = self.active
        self._write_active(
            version_id, previous=previous, event="promote",
            report_id=report.report_id,
        )
        return version_id

    def rollback(self, *, actor: str = "owner") -> str:
        """Restore the previous known-good version; rejected versions stay out."""
        if actor != "owner":
            raise PromotionError("rollback requires actor='owner'")
        doc = self._read_active()
        history = doc.get("history", [])
        if not history:
            raise PromotionError("no previous version to roll back to")
        target = history[-1]
        self._verified_source(target)
        remaining = history[:-1]
        _atomic_write_json(
            self._active_file, {"active": target, "history": remaining}
        )
        self._audit({"event": "rollback", "active": target})
        return target

    def _load_report(self, report_id: str) -> EvaluationReport:
        if (
            not report_id.startswith("rep-")
            or "/" in report_id
            or len(report_id) > 64
        ):
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
        entry = {"ts": time.time(), **event}
        with open(self._audit_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
