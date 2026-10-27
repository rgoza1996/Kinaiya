"""Compute-plugin host tests: real isolated execution, no skipping.

Requires Linux bubblewrap + system Python + libseccomp on this host. These
tests never skip when isolation is missing; they fail closed (the host
under test promises real isolation). Synthetic sources/fixtures only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.plugins import PluginRegistry
from core.plugins import errors as PE
from core.plugins.sandbox import check_available

ADD_SRC = "def run(arguments):\n    return {'sum': arguments['a'] + arguments['b']}\n"

FAIL_SRC = "def run(arguments):\n    return {'sum': arguments['a'] - arguments['b']}\n"


def _manifest(plugin_id="adder", **over):
    base = {
        "schema_version": "1.0",
        "plugin_id": plugin_id,
        "name": "Adder",
        "runtime": "python_json.v1",
        "entrypoint": "run",
        "capabilities": ["compute"],
        "dependencies": [],
        "input_schema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"sum": {"type": "number"}},
            "required": ["sum"],
            "additionalProperties": False,
        },
        "author": "test",
        "license": "MIT",
    }
    base.update(over)
    return base


def _suite_doc(plugin_id="adder", cases=None):
    if cases is None:
        cases = [
            {"input": {"a": 1, "b": 2}, "expected": {"sum": 3}},
            {"input": {"a": -5, "b": 10}, "expected": {"sum": 5}},
        ]
    return {"schema_version": "1.0", "plugin_id": plugin_id, "cases": cases}


@pytest.fixture()
def ctx(tmp_path):
    check_available()  # fail closed, never skip
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps(_suite_doc()), encoding="utf-8")
    reg = PluginRegistry(tmp_path / "root", {"adder": suite})
    yield reg, tmp_path, suite
    reg.close()


def test_valid_registration_evaluation_activation_invocation(ctx):
    reg, _, _ = ctx
    vid = reg.register_bytes(ADD_SRC.encode(), _manifest())
    assert vid.startswith("v-")
    info = reg.inspect(vid)
    assert info["plugin_id"] == "adder" and info["manifest"]["name"] == "Adder"
    assert reg.active("adder") is None
    rep = reg.evaluate(vid)
    assert rep["passed"] is True and rep["cases"] == 2
    assert rep["version_id"] == vid and rep["report_id"].startswith("rep-")
    assert all(r.startswith("PASS") for r in rep["reasons"])
    assert reg.promote(vid, rep["report_id"], actor="owner", reviewed=True) == vid
    assert reg.active("adder") == vid
    got = reg.run(vid, {"a": 2, "b": 3})
    assert got["output"] == {"sum": 5}
    receipt = got["receipt"]
    assert receipt["version_id"] == vid and receipt["source_sha256"] == info["source_sha256"]
    assert (
        receipt["arguments_sha256"]
        == hashlib.sha256(
            json.dumps({"a": 2, "b": 3}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert (
        receipt["output_sha256"]
        == hashlib.sha256(
            json.dumps({"sum": 5}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert isinstance(receipt["elapsed_ms"], int)
    assert len(reg.versions()) == 1


def test_boolean_does_not_match_numeric_owner_expectation(ctx):
    reg, _, suite = ctx
    suite.write_text(
        json.dumps(
            _suite_doc(
                cases=[
                    {
                        "input": {"a": 1, "b": 0},
                        "expected": {"sum": 1},
                    }
                ]
            )
        )
    )
    manifest = _manifest(
        output_schema={
            "type": "object",
            "properties": {"sum": {"type": ["integer", "boolean"]}},
            "required": ["sum"],
            "additionalProperties": False,
        }
    )
    version = reg.register_bytes(b"def run(a):\n    return {'sum': True}\n", manifest)
    assert reg.evaluate(version)["passed"] is False


def test_storage_byte_bounds_and_durable_distinct_task_pins(ctx):
    reg, path, suite = ctx
    version = reg.register_bytes(ADD_SRC.encode(), _manifest())
    reg.pin(version, task_id="first")
    reg.pin(version, task_id="first")  # recovery is idempotent
    reg.pin(version, task_id="second")
    reg.close()
    reopened = PluginRegistry(path / "root", {"adder": suite})
    try:
        reopened.unpin(version, task_id="first")
        with pytest.raises(PE.PluginError, match="pinned"):
            reopened.remove(version)
        reopened.unpin(version, task_id="second")
        source = reopened.root / "versions" / version / "source.py"
        source.write_bytes(b"x" * 65_537)
        with pytest.raises(PE.PluginError, match="byte bound"):
            reopened.inspect(version)
    finally:
        reopened.close()


def test_registry_ancestor_symlink_rejected_before_directory_creation(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PE.PluginError, match="symlink"):
        PluginRegistry(link / "new-registry", {})
    assert not (target / "new-registry").exists()


def test_retained_real_agent_candidate_replays_without_model_inference(tmp_path):
    root = Path(__file__).resolve().parents[2]
    artifacts = root / "evaluation/plugins/csv_stats"
    evidence = json.loads(
        (root / "evaluation/evidence/compute-plugins-2026-10-27.json").read_text()
    )
    source = (artifacts / "candidate.py.txt").read_bytes()
    assert hashlib.sha256(source).hexdigest() == evidence["source_sha256"]
    suite = artifacts / "owner-acceptance.json"
    registry = PluginRegistry(tmp_path / "registry", {"public_csv_stats": suite})
    try:
        version = registry.register_bytes(
            source, json.loads((artifacts / "manifest.json").read_text())
        )
        assert version == evidence["version_id"]
        report = registry.evaluate(version)
        assert report["passed"] and report["cases"] == 4
        assert report["suite_sha256"] == evidence["acceptance"]["suite_sha256"]
        registry.promote(version, report["report_id"], reviewed=True)
        result = registry.run(version, {"csv": "value,label\n2,a\n8,b\n5,c\n", "column": "value"})
        assert result["output"] == evidence["direct_without_llm"]["output"]
        assert result["receipt"]["output_sha256"] == evidence["result_sha256"]
    finally:
        registry.close()


def test_content_addressed_dedup_and_immutable(ctx):
    reg, _, _ = ctx
    v1 = reg.register_bytes(ADD_SRC.encode(), _manifest())
    v2 = reg.register_bytes(ADD_SRC.encode(), _manifest())
    assert v1 == v2
    v3 = reg.register_bytes(FAIL_SRC.encode(), _manifest())
    assert v3 != v1
    stored = reg.root / "versions" / v1 / "source.py"
    stored.write_bytes(b"def run(arguments):\n    return {'sum': 999}\n")
    with pytest.raises(PE.PluginError):
        reg.run(v1, {"a": 1, "b": 2})
    with pytest.raises(PE.PluginError):
        reg.evaluate(v1)


def test_manifest_strictness(ctx):
    reg, _, _ = ctx
    good = _manifest()
    bad_extra = dict(good, extra_field="nope")
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), bad_extra)
    bad_missing = dict(good)
    del bad_missing["author"]
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), bad_missing)
    for pid in ("Adder", "1abc", "a" * 65, "has space", ""):
        with pytest.raises(PE.PluginError):
            reg.register_bytes(ADD_SRC.encode(), _manifest(plugin_id=pid))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(capabilities=["network"]))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(capabilities=["compute", "extra"]))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(dependencies=["numpy"]))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(runtime="other.v1"))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(entrypoint="main"))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(schema_version="2.0"))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(name="x" * 81))
    # Description is the only optional field: base has none and registers fine.
    vid = reg.register_bytes(ADD_SRC.encode(), dict(good))
    assert vid.startswith("v-")
    with_desc = dict(good, description="optional blurb")
    vid2 = reg.register_bytes(ADD_SRC.encode(), with_desc)
    assert vid2.startswith("v-") and vid2 != vid


def test_schema_profile_rejections(ctx):
    reg, _, _ = ctx
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(input_schema={"$ref": "#/$defs/x"}))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(
            ADD_SRC.encode(),
            _manifest(input_schema={"type": "string", "pattern": "^a"}),
        )
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(input_schema={"weirdKeyword": True}))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(input_schema={"$schema": "https://x/y"}))
    deep = cur = {}
    for _ in range(14):
        nxt: dict = {}
        cur["properties"] = {"x": nxt}
        cur = nxt
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(input_schema=deep))
    big = {"type": "string", "description": "z" * (17 * 1024)}
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(input_schema=big))
    with pytest.raises(PE.PluginError):
        reg.register_bytes(ADD_SRC.encode(), _manifest(input_schema=["not-a-dict"]))


def test_source_static_rejections(ctx):
    reg, _, _ = ctx
    man = _manifest()
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"import os\ndef run(a):\n    return {}\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"import socket\ndef run(a):\n    return {}\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"import subprocess\ndef run(a):\n    return {}\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"def run(a):\n    open('x').read()\n    return {}\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"def run(a):\n    return eval('1')\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"X = 1\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"async def run(a):\n    return {}\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"def run(a, b):\n    return {}\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"def run(*a):\n    return {}\n", man)
    with pytest.raises(PE.PluginError):
        reg.register_bytes(b"def run(a):\n    return {}\n" * 4000, man)
    # Declared compute stdlib is accepted.
    ok_src = (
        "import csv, io, statistics, decimal, datetime, hashlib\n"
        "def run(arguments):\n"
        "    return {'sum': float(arguments['a'] + arguments['b'])}\n"
    )
    vid = reg.register_bytes(ok_src.encode(), man)
    assert vid.startswith("v-")


def test_evaluation_failure_stays_inactive(ctx):
    reg, _, _ = ctx
    vid = reg.register_bytes(FAIL_SRC.encode(), _manifest())
    rep = reg.evaluate(vid)
    assert rep["passed"] is False
    assert any(r.startswith("FAIL") for r in rep["reasons"])
    assert reg.active("adder") is None
    with pytest.raises(PE.PluginError):
        reg.promote(vid, rep["report_id"], actor="owner", reviewed=True)
    assert reg.active("adder") is None


def test_owner_suite_binding_and_tamper(ctx):
    reg, tmp_path, suite = ctx
    vid = reg.register_bytes(ADD_SRC.encode(), _manifest())
    rep = reg.evaluate(vid)
    assert rep["passed"] is True
    # Suite changed after evaluation blocks promotion.
    suite.write_text(
        json.dumps(_suite_doc(cases=[{"input": {"a": 9, "b": 9}, "expected": {"sum": 18}}])),
        encoding="utf-8",
    )
    with pytest.raises(PE.PluginError):
        reg.promote(vid, rep["report_id"], actor="owner", reviewed=True)
    # Suite for another plugin cannot qualify this version.
    other = tmp_path / "other.json"
    other.write_text(json.dumps(_suite_doc(plugin_id="other")), encoding="utf-8")
    reg2 = PluginRegistry(tmp_path / "root2", {"adder": other})
    try:
        vid2 = reg2.register_bytes(ADD_SRC.encode(), _manifest())
        # Registration is fine; evaluation must refuse the suite binding.
        with pytest.raises(PE.PluginError):
            reg2.evaluate(vid2)
    finally:
        reg2.close()
    # Forged report / actor / review fail closed.
    suite.write_text(json.dumps(_suite_doc()), encoding="utf-8")
    rep2 = reg.evaluate(vid)
    with pytest.raises(PE.PluginError):
        reg.promote(vid, "rep-forged", actor="owner", reviewed=True)
    with pytest.raises(PE.PluginError):
        reg.promote(vid, rep2["report_id"], actor="attacker", reviewed=True)
    with pytest.raises(PE.PluginError):
        reg.promote(vid, rep2["report_id"], actor="owner", reviewed=False)
    # Candidate cannot activate itself: no path promotes without owner review.
    with pytest.raises(PE.PluginError):
        reg.promote(vid, rep2["report_id"], actor="owner", reviewed=False)


def test_promotion_report_binding_mismatch(ctx):
    reg, _, _ = ctx
    v1 = reg.register_bytes(ADD_SRC.encode(), _manifest())
    v2 = reg.register_bytes(FAIL_SRC.encode(), _manifest())
    rep1 = reg.evaluate(v1)
    assert rep1["passed"] is True
    with pytest.raises(PE.PluginError):
        reg.promote(v2, rep1["report_id"], actor="owner", reviewed=True)


def test_rollback_requires_history_and_review(ctx):
    reg, _, _ = ctx
    v1 = reg.register_bytes(ADD_SRC.encode(), _manifest())
    r1 = reg.evaluate(v1)
    reg.promote(v1, r1["report_id"], actor="owner", reviewed=True)
    with pytest.raises(PE.PluginError):
        reg.rollback("adder", actor="owner", reviewed=True)
    v2src = "def run(arguments):\n    return {'sum': arguments['a'] + arguments['b'] + 0}\n"
    v2 = reg.register_bytes(v2src.encode(), _manifest())
    r2 = reg.evaluate(v2)
    assert r2["passed"] is True
    reg.promote(v2, r2["report_id"], actor="owner", reviewed=True)
    assert reg.active("adder") == v2
    with pytest.raises(PE.PluginError):
        reg.rollback("adder", actor="owner", reviewed=False)
    with pytest.raises(PE.PluginError):
        reg.rollback("adder", actor="attacker", reviewed=True)
    back = reg.rollback("adder", actor="owner", reviewed=True)
    assert back == v1
    assert reg.active("adder") == v1


def test_disable_revokes_runs_including_pinned(ctx):
    reg, _, _ = ctx
    vid = reg.register_bytes(ADD_SRC.encode(), _manifest())
    rep = reg.evaluate(vid)
    reg.promote(vid, rep["report_id"], actor="owner", reviewed=True)
    reg.pin(vid)
    assert reg.run(vid, {"a": 1, "b": 1})["output"] == {"sum": 2}
    reg.disable("adder", actor="owner")
    assert reg.active("adder") is None
    with pytest.raises(PE.PluginError):
        reg.run(vid, {"a": 1, "b": 1})
    with pytest.raises(PE.PluginError):
        reg.disable("adder", actor="attacker")


def test_remove_and_pins(ctx):
    reg, _, _ = ctx
    v1 = reg.register_bytes(ADD_SRC.encode(), _manifest())
    r1 = reg.evaluate(v1)
    reg.promote(v1, r1["report_id"], actor="owner", reviewed=True)
    v2 = reg.register_bytes(
        b"def run(arguments):\n    return {'sum': arguments['a'] + arguments['b'] + 0}\n",
        _manifest(),
    )
    # Active version cannot be removed while enabled.
    with pytest.raises(PE.PluginError):
        reg.remove(v1, actor="owner")
    reg.pin(v2)
    with pytest.raises(PE.PluginError):
        reg.remove(v2, actor="owner")
    reg.unpin(v2)
    reg.remove(v2, actor="owner")
    assert v2 not in [v["version_id"] for v in reg.versions()]
    # History-required version cannot be removed while enabled: promote v2b then check.
    v2b = reg.register_bytes(
        b"def run(arguments):\n    return {'sum': arguments['a'] + arguments['b'] + 0}\n",
        _manifest(),
    )
    r2 = reg.evaluate(v2b)
    reg.promote(v2b, r2["report_id"], actor="owner", reviewed=True)
    with pytest.raises(PE.PluginError):
        reg.remove(v1, actor="owner")
    # After disable with no pins, removal of history is allowed with audit.
    reg.disable("adder", actor="owner")
    reg.remove(v1, actor="owner")
    assert v1 not in [v["version_id"] for v in reg.versions()]


def test_restart_and_single_writer(ctx):
    reg, tmp_path, suite = ctx
    vid = reg.register_bytes(ADD_SRC.encode(), _manifest())
    with pytest.raises(PE.PluginStateError):
        PluginRegistry(tmp_path / "root", {"adder": suite})
    reg.close()
    reg2 = PluginRegistry(tmp_path / "root", {"adder": suite})
    try:
        assert reg2.inspect(vid)["version_id"] == vid
        assert [v["version_id"] for v in reg2.versions()] == [vid]
    finally:
        reg2.close()
    with pytest.raises(PE.PluginError):
        reg.run(vid, {"a": 1, "b": 2})


def test_unsafe_roots_and_symlinks(tmp_path):
    check_available()
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps(_suite_doc()), encoding="utf-8")
    link_root = tmp_path / "linkroot"
    try:
        link_root.symlink_to(tmp_path)
    except OSError:
        pass
    else:
        with pytest.raises(PE.PluginError):
            PluginRegistry(link_root, {"adder": suite})
    link_suite = tmp_path / "linksuite.json"
    try:
        link_suite.symlink_to(suite)
    except OSError:
        pass
    else:
        with pytest.raises(PE.PluginError):
            PluginRegistry(tmp_path / "rootx", {"adder": link_suite})


def test_oversize_and_malformed_input_output(ctx):
    reg, _, suite = ctx
    vid = reg.register_bytes(ADD_SRC.encode(), _manifest())
    report = reg.evaluate(vid)
    reg.promote(vid, report["report_id"], reviewed=True)
    with pytest.raises(PE.PluginError):
        reg.run(vid, {"a": "x" * 70000, "b": 1})
    with pytest.raises(PE.PluginError):
        reg.run(vid, {"a": 1})  # missing required field
    with pytest.raises(PE.PluginError):
        reg.run(vid, {"a": 1, "b": 2, "extra": 3})  # additionalProperties False
    with pytest.raises(PE.PluginError):
        reg.run(vid, ["not-a-dict"])  # type: ignore[arg-type]
    deep: dict = {}
    cur = deep
    for _ in range(40):
        cur["x"] = {}
        cur = cur["x"]
    with pytest.raises(PE.PluginError):
        reg.run(vid, {"a": 1, "b": deep})  # type: ignore[dict-item]
    with pytest.raises(PE.PluginError):
        reg.run(vid, {"a": float("nan"), "b": 1})
    # Output schema violations are native failures, never success evidence.
    bad_out = b"def run(arguments):\n    return {'wrong': 1} if arguments['a'] == 99 else {'sum': arguments['a'] + arguments['b']}\n"
    bv = reg.register_bytes(bad_out, _manifest())
    report = reg.evaluate(bv)
    reg.promote(bv, report["report_id"], reviewed=True)
    with pytest.raises(PE.PluginError, match="output failed schema"):
        reg.run(bv, {"a": 99, "b": 2})
    # A result may carry 'success' DATA only when the schema allows it.
    data_src = b"def run(arguments):\n    return {'sum': arguments['a'] + arguments['b'], 'success': True}\n"
    man2 = _manifest(
        output_schema={
            "type": "object",
            "properties": {"sum": {"type": "number"}, "success": {"type": "boolean"}},
            "required": ["sum", "success"],
            "additionalProperties": False,
        }
    )
    suite_doc = _suite_doc()
    for case in suite_doc["cases"]:
        case["expected"]["success"] = True
    suite.write_text(json.dumps(suite_doc))
    dv = reg.register_bytes(data_src, man2)
    report = reg.evaluate(dv)
    reg.promote(dv, report["report_id"], reviewed=True)
    got = reg.run(dv, {"a": 1, "b": 2})
    assert got["output"] == {"sum": 3, "success": True}


def test_success_field_is_data_not_evidence(ctx):
    reg, _, _ = ctx
    # Candidate claims success but computes the wrong answer; evaluation fails.
    liar = b"def run(arguments):\n    return {'sum': 999, 'success': True}\n"
    man = _manifest(
        output_schema={
            "type": "object",
            "properties": {"sum": {"type": "number"}, "success": {"type": "boolean"}},
            "required": ["sum", "success"],
            "additionalProperties": False,
        }
    )
    suite_cases = [
        {"input": {"a": 1, "b": 2}, "expected": {"sum": 3, "success": True}},
    ]
    import json as _json

    suite2 = reg.root.parent / "suite2.json"
    suite2.write_text(
        _json.dumps({"schema_version": "1.0", "plugin_id": "adder", "cases": suite_cases}),
        encoding="utf-8",
    )
    reg.suites["adder"] = suite2
    lv = reg.register_bytes(liar, man)
    rep = reg.evaluate(lv)
    assert rep["passed"] is False
