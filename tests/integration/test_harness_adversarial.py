"""Adversarial integration: tampering, forgery, escalation, suite binding."""

from __future__ import annotations

import json
import shutil

import pytest

from core.extensions import errors as E
from core.extensions import sandbox as S
from core.extensions.registry import Registry

CASES = {
    "suite_version": "1",
    "cases": [
        {
            "case_id": "k1",
            "payload": {
                "query": "alpha beta",
                "files": [
                    {"id": "x", "path": "x.py", "text": "alpha beta gamma"},
                    {"id": "y", "path": "y.py", "text": "delta epsilon zeta"},
                ],
                "max_chars": 40,
            },
            "expected_ids": ["x"],
        }
    ],
}

FORGED = (
    "def select_context(payload):\n"
    "    return ['no-such-file']  # forged IDs must be rejected host-side\n"
)

OVER_BUDGET = (
    "def select_context(payload):\n"
    "    return [f['id'] for f in payload['files']]  # ignores max_chars\n"
)


@pytest.fixture()
def reg(tmp_path):
    if shutil.which("bwrap") is None or S.find_system_python() is None:
        pytest.skip("truthful skip: bubblewrap/system-python unavailable")
    suite = tmp_path / "heldout.json"
    suite.write_text(json.dumps(CASES), encoding="utf-8")
    return Registry(tmp_path / "root", suite)


def _src(tmp_path, body: str, name: str = "adv.py"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_forged_ids_rejected_and_fail_evaluation(reg, tmp_path):
    vid = reg.register(_src(tmp_path, FORGED), {"name": "forged"})
    with pytest.raises(E.CandidateFailure):
        reg.run(vid, CASES["cases"][0]["payload"])
    rep = reg.evaluate(vid)
    assert rep.passed is False
    assert rep.candidate["failure_rate"] == pytest.approx(1.0)


def test_over_budget_rejected(reg, tmp_path):
    payload = dict(CASES["cases"][0]["payload"])
    payload["max_chars"] = 5
    vid = reg.register(_src(tmp_path, OVER_BUDGET), {"name": "greedy"})
    with pytest.raises(E.CandidateFailure):
        reg.run(vid, payload)


def test_suite_change_breaks_promotion_binding(reg, tmp_path):
    good = "import re\ndef select_context(payload):\n    return [payload['files'][0]['id']]\n"
    vid = reg.register(_src(tmp_path, good), {"name": "first"})
    rep = reg.evaluate(vid)
    assert rep.passed is True
    reg.suite_path.write_text(json.dumps({"suite_version": "1", "cases": []}))
    with pytest.raises((E.PromotionError, E.IntegrityError)):
        reg.promote(vid, rep.report_id, actor="owner", reviewed=True)


def test_exfiltration_attempt_cannot_read_registry(reg, tmp_path):
    marker = tmp_path / "marker.txt"
    marker.write_text("registry-adjacent-secret", encoding="utf-8")
    payload = {
        "query": str(marker),
        "files": [{"id": "f1", "path": "a.py", "text": "hi"}],
        "max_chars": 10,
    }
    evil = (
        "def select_context(payload):\n    import os\n    return [open(payload['query']).read()]\n"
    )
    with pytest.raises(E.RegistrationError):
        reg.register(_src(tmp_path, evil), {"name": "evil"})
    # Even if such bytes reached the sandbox, the mount table denies them.
    direct = tmp_path / "direct.py"
    direct.write_text(evil, encoding="utf-8")
    with pytest.raises(E.CandidateFailure):
        S.run_selector(direct, payload)
    assert marker.read_text() == "registry-adjacent-secret"


def test_capability_escalation_rejected(reg, tmp_path):
    src = _src(tmp_path, "def select_context(p):\n    return []\n")
    with pytest.raises(E.RegistrationError):
        reg.register(src, {"name": "esc", "capabilities": ["context_selection", "fs_write"]})


def test_evaluate_rejects_unvalidated_suite_payload(reg, tmp_path):
    bad = {
        "suite_version": "1",
        "cases": [
            {
                "case_id": "bad",
                "payload": {"query": "", "files": [], "max_chars": 0},
                "expected_ids": [],
            }
        ],
    }
    reg.suite_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(E.IntegrityError):
        reg.evaluate(reg.active)
