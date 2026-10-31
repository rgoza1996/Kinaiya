"""Consistency of public Mica evidence data; never execute embedded fixture code."""

import ast
import difflib
import hashlib
import json
import re
from pathlib import Path

from core.policy import action_digest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evaluation/evidence/agent-history-mica-2026-10-31.json"


def capsule():
    return json.loads(EVIDENCE.read_text())


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_embedded_frozen_texts_match_original_manifests():
    data = capsule()
    fixture = data["fixture"]
    manifest = json.loads(fixture["original_manifest_utf8"])
    assert (
        sha(fixture["original_manifest_utf8"])
        == data["provenance_sha256"]["frozen_fixture_manifest"]
    )
    assert set(fixture["files"]) == set(manifest["files"])
    for name, item in fixture["files"].items():
        assert sha(item["utf8_text"]) == item["sha256"] == manifest["files"][name]["sha256"]
        assert (
            len(item["utf8_text"].encode())
            == item["utf8_bytes"]
            == manifest["files"][name]["size_bytes"]
        )
    assert fixture["public_test_utf8_bytes"] == 4084
    public_tests = fixture["files"]["fixture/workspace/tests/test_ledger.py"]["utf8_text"]
    names = [
        node.name
        for node in ast.walk(ast.parse(public_tests))
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    assert len(names) == fixture["public_test_count"] == 14
    assert fixture["meaningful_public_test_tail"] in names
    assert (
        sha(fixture["files"]["fixture/grader/test_ledger_heldout.py"]["utf8_text"])
        == fixture["original_author_heldout_sha256"]
    )


def test_candidate_text_and_two_line_diff_are_consistent():
    data = capsule()
    candidate = data["candidate"]
    before = data["fixture"]["files"]["fixture/workspace/mica/ledger.py"]["utf8_text"]
    after = candidate["utf8_text"]
    assert sha(after) == candidate["sha256"] == data["observed"]["write"]["sha256"]
    assert len(after.encode()) == candidate["utf8_bytes"] == 902
    expected_diff = "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile="a/mica/ledger.py",
            tofile="b/mica/ledger.py",
        )
    )
    assert expected_diff == candidate["unified_diff_utf8"]
    assert sha(expected_diff) == data["provenance_sha256"]["candidate_diff"]
    removed = [line for line in expected_diff.splitlines() if line.startswith("-")][1:]
    added = [line for line in expected_diff.splitlines() if line.startswith("+")][1:]
    assert len(removed) == len(added) == candidate["changed_statement_lines"] == 2
    module = ast.parse(after)
    assert len(module.body) == 2 and isinstance(module.body[1], ast.FunctionDef)
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(module))


def test_six_count_generation_pairs_preserve_identity_and_usage_difference():
    data = capsule()
    wire = data["wire"]
    assert wire["count_requests"] == wire["generation_requests"] == len(wire["pairs"]) == 6
    assert wire["capture_errors"] == 0
    assert [pair["tool"] for pair in wire["pairs"]] == [
        "read_file",
        "read_file",
        "read_file",
        "write_file",
        "shell",
        "finish",
    ]
    for index, pair in enumerate(wire["pairs"]):
        assert pair["count_request"] == index * 2 + 1
        assert pair["generation_request"] == index * 2 + 2
        assert pair["count_request_sha256"] == pair["generation_request_sha256"]
        for key, value in pair.items():
            if key.endswith("sha256"):
                assert re.fullmatch("[0-9a-f]{64}", value)
        assert pair["count_status"] == pair["generation_status"] == 200
        assert pair["count_eof_complete"] and pair["generation_eof_complete"]
        assert pair["generation_result"] == "completed"
        assert pair["count_endpoint_input_tokens"] - pair["generation_usage_input_tokens"] == 143
        assert pair["count_minus_generation_usage"] == 143
    assert data["profile"]["model"] == "muse-spark-1.3-contributor"
    assert data["profile"]["reasoning_effort"] == "xhigh"
    assert data["profile"]["max_output_tokens"] == 8192


def test_complete_reads_approvals_and_durable_evidence_order():
    data = capsule()
    observed = data["observed"]
    assert observed["changed_files"] == [data["candidate"]["path"]] == ["mica/ledger.py"]
    assert observed["elapsed_task_seconds"] == 50.43152827298036
    assert not observed["deadline_reached"]
    assert observed["native_state"] == observed["durable_outcome"]["state"] == "succeeded"
    assert (
        observed["native_journal_entries"] == observed["durable_outcome"]["journal_sequence"] == 11
    )
    for read in observed["whole_prewrite_reads"]:
        original = data["fixture"]["files"]["fixture/workspace/" + read["path"]]
        assert (read["sha256"], read["utf8_bytes"]) == (original["sha256"], original["utf8_bytes"])
        assert read["native_journal_sequence"] < observed["write"]["journal_sequence"]
        assert read["complete_observation_in_generation_request"] == 8
        assert read["exact_entire_file_after_ok_prefix"] and read["before_first_write"]
    approvals = observed["approvals"]
    assert len(approvals) == 2
    assert approvals[0]["arguments"]["content"] == data["candidate"]["utf8_text"]
    for approval, effect in zip(
        approvals, (observed["write"], observed["public_command"]), strict=True
    ):
        assert approval["approved"] and approval["owner_resolution"]
        assert approval["action_digest"] == action_digest(
            approval["tool"], approval["arguments"], approval["objective_revision"]
        )
        assert (
            approval["requested_sequence"]
            < approval["resolved_sequence"]
            < effect["journal_sequence"]
        )
    assert observed["write"]["journal_sequence"] < observed["public_command"]["journal_sequence"]
    assert observed["public_command"]["command"] == data["fixture"]["public_test_command"]
    assert observed["public_command"]["exit_code"] == 0
    assert observed["public_command"]["tests_reported"] == 14
    assert (
        observed["public_command"]["journal_sequence"]
        < observed["durable_outcome"]["journal_sequence"]
    )


def test_frozen_host_values_match_recorded_response_with_child_property_limits():
    host = capsule()["independent_host_grade"]
    manifest = json.loads(host["frozen_manifest_utf8"])
    assert sha(host["frozen_manifest_utf8"]) == host["frozen_manifest_sha256"]
    for name, item in host["files"].items():
        assert sha(item["utf8_text"]) == item["sha256"] == manifest["files"][name]["sha256"]
        assert (
            len(item["utf8_text"].encode())
            == item["utf8_bytes"]
            == manifest["files"][name]["size_bytes"]
        )
    expected = json.loads(host["files"]["expected.json"]["utf8_text"])
    response = host["observed_response"]
    assert response["protocol"] == "mica-ledger-result-v1"
    assert len(expected["cases"]) == len(response["cases"]) == host["cases"] == 8
    calls = 0
    for wanted_case, actual_case in zip(expected["cases"], response["cases"], strict=True):
        assert wanted_case["id"] == actual_case["id"]
        for wanted, actual in zip(wanted_case["calls"], actual_case["calls"], strict=True):
            calls += 1
            assert wanted["id"] == actual["id"] and wanted["status"] == actual["status"]
            if wanted["status"] == "value":
                assert wanted["value"] == actual["value"]
            else:
                assert actual["error_type"] in wanted["error_types"]
            assert wanted["reported_properties"] == actual["reported_properties"]
    assert calls == host["calls"] == 10
    assert not host["properties_independently_verified"]
    assert not host["expected_values_and_author_grader_mounted"]
    assert not host["exit_zero_or_unittest_footer_is_a_pass_verdict"]
    assert set(host["input_only_grading_file_set"]) == {
        "candidate/mica/__init__.py",
        "candidate/mica/ledger.py",
        "driver.py",
        "requests.json",
    }


def test_scope_privacy_ci_and_preserved_failed_baseline():
    data = capsule()
    scope = data["scope"]
    assert scope["attempts"] == 1
    assert not any(
        scope[key]
        for key in (
            "automatic_retry",
            "converse_present",
            "decisions_present",
            "general_reliability_claim",
            "all_M1_gates_complete_claim",
            "paired_causal_comparison_claim",
        )
    )
    review = data["independent_review"]
    assert review["predicate_count"] == 208 and not review["predicate_count_is_test_count"]
    assert data["budget"]["actual_reported_billing_usd"] is None
    assert not data["budget"]["estimates_are_invoices"]
    ci = data["source"]["exact_head_ci"]
    assert ci["head"] == data["source"]["head"]
    assert ci["tree"] == data["source"]["tree"]
    assert ci["run_id"] == 41220660884 and ci["conclusion"] == "success"
    assert len(ci["jobs"]) == 5 and all(job["conclusion"] == "success" for job in ci["jobs"])
    previous = data["prior_failed_baseline"]
    previous_bytes = (ROOT / previous["path"]).read_bytes()
    assert hashlib.sha256(previous_bytes).hexdigest() == previous["public_record_sha256"]
    baseline = json.loads(previous_bytes)
    assert baseline["result"] == previous["result"] == "failed/unqualified"
    assert baseline["receipt_sha256"] == previous["private_receipt_sha256"]
    assert not previous["retried"] and not previous["same_fixture_paired_comparison"]
    public_text = EVIDENCE.read_text()
    assert not re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", public_text)
    assert not any(
        value in public_text for value in ("/home/", "/tmp/", ".dev-run/", "auth.json", "Bearer ")
    )
    pending = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            assert not set(item) & {
                "headers",
                "authorization",
                "auth_file",
                "guard",
                "pid",
                "task_id",
                "session_id",
                "thread_id",
                "event_id",
                "approval_id",
            }
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
