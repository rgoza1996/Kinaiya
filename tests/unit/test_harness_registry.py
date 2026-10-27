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
