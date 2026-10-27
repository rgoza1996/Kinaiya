"""Registry unit tests: registration, evaluation, promotion gates, pinning."""

from __future__ import annotations

import json
import shutil

import pytest

from core.extensions import errors as E
from core.extensions import sandbox as S
from core.extensions.manifest import Manifest
from core.extensions.registry import Registry

GOOD_SRC = (
    "import re\n"
    "def select_context(payload):\n"
    "    q = set(re.findall(r'[a-z0-9]+', payload['query'].lower()))\n"
    "    out, used = [], 0\n"
    "    files = sorted(payload['files'], key=lambda f: f['id'])\n"
    "    ranked = sorted(files, key=lambda f: -len(q & set(f['text'].lower().split())))\n"
    "    for f in ranked:\n"
    "        if used + len(f['text']) <= payload['max_chars']:\n"
    "            out.append(f['id'])\n"
    "            used += len(f['text'])\n"
    "    return out\n"
)

CASES = {
    "suite_version": "1",
    "cases": [
        {
            "case_id": "c1",
            "payload": {
                "query": "login password",
                "files": [
                    {"id": "a", "path": "a.py", "text": "login password check"},
                    {"id": "b", "path": "b.py", "text": "unrelated zebra snow"},
                ],
                "max_chars": 60,
            },
            "expected_ids": ["a"],
        },
        {
            "case_id": "c2",
            "payload": {
                "query": "retry backoff",
                "files": [
                    {"id": "c", "path": "c.py", "text": "retry backoff loop"},
                    {"id": "d", "path": "d.py", "text": "paint fence green"},
                ],
                "max_chars": 60,
            },
            "expected_ids": ["c"],
        },
    ],
}


@pytest.fixture()
def reg(tmp_path):
    if shutil.which("bwrap") is None or S.find_system_python() is None:
        pytest.skip("truthful skip: bubblewrap/system-python unavailable")
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps(CASES), encoding="utf-8")
    return Registry(tmp_path / "root", suite)


def _src(tmp_path, body: str, name: str = "cand.py"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_baseline_seeded_and_runnable(reg):
    base = reg.active
    assert base.startswith("v-")
    out = reg.run(base, CASES["cases"][0]["payload"])
    assert out == ["a", "b"]


def test_register_rejects_escapes(reg, tmp_path):
    with pytest.raises(E.RegistrationError):
        reg.register(_src(tmp_path, "import os\ndef select_context(p):\n    return []\n"))
    with pytest.raises(E.RegistrationError):
        reg.register(
            _src(tmp_path, GOOD_SRC),
            {"name": "net", "capabilities": ["network"]},
        )
    with pytest.raises(E.RegistrationError):
        reg.register(
            _src(tmp_path, GOOD_SRC),
            {"name": "dep", "dependencies": ["numpy"]},
        )
    with pytest.raises(E.RegistrationError):
        reg.register(_src(tmp_path, "X = 1\n"))
    assert reg.active.startswith("v-")


def test_version_immutable_and_tamper_detected(reg, tmp_path):
    vid = reg.register(_src(tmp_path, GOOD_SRC), {"name": "cand"})
    assert reg.register(_src(tmp_path, GOOD_SRC, "c2.py"), {"name": "cand"}) == vid
    stored = reg.root / "versions" / vid / "source.py"
    stored.write_bytes(b"def select_context(p):\n    return ['zz']\n")
    with pytest.raises(E.IntegrityError):
        reg.run(vid, CASES["cases"][0]["payload"])
    with pytest.raises(E.IntegrityError):
        reg.evaluate(vid)


def test_evaluate_report_shape(reg, tmp_path):
    vid = reg.register(_src(tmp_path, GOOD_SRC), {"name": "cand"})
    rep = reg.evaluate(vid)
    assert rep.passed is True
    assert rep.candidate_version_id == vid
    assert rep.suite_cases == 2 and rep.suite_hash
    assert rep.candidate["budget_unit"].startswith("chars:")
    assert rep.baseline["mean_recall"] == pytest.approx(1.0)
    assert (reg.root / "reports" / f"{rep.report_id}.json").exists()


def test_promotion_gates_and_rollback(reg, tmp_path):
    vid = reg.register(_src(tmp_path, GOOD_SRC), {"name": "cand"})
    rep = reg.evaluate(vid)
    before = reg.active
    with pytest.raises(E.PromotionError):
        reg.promote(vid, rep.report_id, actor="intruder", reviewed=True)
    with pytest.raises(E.PromotionError):
        reg.promote(vid, rep.report_id, actor="owner", reviewed=False)
    with pytest.raises(E.PromotionError):
        reg.promote(vid, "rep-nope", actor="owner", reviewed=True)
    assert reg.active == before
    assert reg.promote(vid, rep.report_id, actor="owner", reviewed=True) == vid
    assert reg.active == vid
    # Older pin still runnable after pointer moved.
    assert reg.run(before, CASES["cases"][1]["payload"]) == ["c", "d"]
    assert reg.rollback(actor="owner") == before
    assert reg.active == before


def test_rejected_candidate_cannot_self_promote(reg, tmp_path):
    bad = _src(tmp_path, "def select_context(p):\n    return []\n")
    vid = reg.register(bad, {"name": "empty"})
    rep = reg.evaluate(vid)
    assert rep.passed is False
    before = reg.active
    with pytest.raises(E.PromotionError):
        reg.promote(vid, rep.report_id, actor="owner", reviewed=True)
    assert reg.active == before


def test_manifest_model_rejects_unknown_contract():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Manifest.model_validate({"name": "x", "contract": "other.v9"})


@pytest.fixture()
def reg_plain(tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps(CASES), encoding="utf-8")
    return Registry(tmp_path / "root", suite)


def test_version_files_reject_symlinks(reg_plain, tmp_path):
    vid = reg_plain.register(_src(tmp_path, GOOD_SRC), {"name": "cand"})
    stored = reg_plain.root / "versions" / vid / "source.py"
    stored.unlink()
    target = tmp_path / "evil-target.py"
    target.write_text("def select_context(p):\n    return []\n", encoding="utf-8")
    stored.symlink_to(target)
    with pytest.raises(E.IntegrityError):
        reg_plain.run(vid, CASES["cases"][0]["payload"])


def test_manifest_edit_breaks_content_address(reg_plain, tmp_path):
    vid = reg_plain.register(_src(tmp_path, GOOD_SRC), {"name": "cand"})
    meta_path = reg_plain.root / "versions" / vid / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["manifest"]["description"] = "forged"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(E.IntegrityError):
        reg_plain.run(vid, CASES["cases"][0]["payload"])


def test_seed_refuses_changed_baseline(tmp_path):
    root = tmp_path / "root"
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps(CASES), encoding="utf-8")
    Registry(root, suite)
    clash = root / "versions" / ("v-" + "0" * 24)
    clash.mkdir(parents=True)
    clash.joinpath("meta.json").write_text(
        json.dumps(
            {
                "version_id": "v-" + "0" * 24,
                "source_sha256": "x",
                "manifest": {"name": "baseline-keyword"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(E.IntegrityError):
        Registry(root, suite)


def test_concurrent_registration_stable(reg_plain, tmp_path):
    import threading

    src = _src(tmp_path, GOOD_SRC)
    ids: dict[int, str] = {}
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            ids[i] = reg_plain.register(src, {"name": "same"})
        except BaseException as exc:  # noqa: BLE001 - collected below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(set(ids.values())) == 1


def test_baseline_health_gate_pure():
    from core.extensions.registry import _gate_baseline_health

    ok, reasons = _gate_baseline_health({"n_fail": 0}, True, ["PASS x"])
    assert ok is True and reasons == ["PASS x"]
    blocked, reasons2 = _gate_baseline_health({"n_fail": 1}, True, ["PASS x"])
    assert blocked is False
    assert any("baseline" in reason for reason in reasons2)


def test_promote_refuses_failed_baseline(reg_plain, tmp_path):
    import time as _t

    from evaluation.harness.policy import DEFAULT_POLICY, POLICY_VERSION
    from evaluation.harness.suite import suite_hash_of

    vid = reg_plain.register(_src(tmp_path, GOOD_SRC), {"name": "cand"})
    meta = json.loads((reg_plain.root / "versions" / vid / "meta.json").read_text(encoding="utf-8"))
    report = {
        "report_id": "rep-" + "a" * 16,
        "candidate_version_id": vid,
        "candidate_source_sha256": meta["source_sha256"],
        "baseline_version_id": reg_plain.active,
        "baseline_source_sha256": "x",
        "suite_hash": suite_hash_of(reg_plain.suite_path),
        "suite_cases": 2,
        "policy": dict(DEFAULT_POLICY),
        "policy_version": POLICY_VERSION,
        "model_profile": None,
        "candidate": {"n_fail": 0},
        "baseline": {"n_fail": 1},
        "passed": True,
        "reasons": [],
        "created_at": _t.time(),
    }
    (reg_plain.root / "reports" / f"{report['report_id']}.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    with pytest.raises(E.PromotionError):
        reg_plain.promote(vid, report["report_id"], actor="owner", reviewed=True)


def test_suite_max_cases_bound(tmp_path):
    from evaluation.harness import suite as Smod

    cases = [
        {
            "case_id": f"c{i}",
            "payload": CASES["cases"][0]["payload"],
            "expected_ids": ["a"],
        }
        for i in range(Smod.MAX_SUITE_CASES + 1)
    ]
    path = tmp_path / "big.json"
    path.write_text(json.dumps({"suite_version": "1", "cases": cases}), encoding="utf-8")
    with pytest.raises(E.IntegrityError):
        Smod.load_suite(path)
