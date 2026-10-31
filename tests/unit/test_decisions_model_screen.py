"""Offline integrity, native projection and scoring of the consumed public screen."""

import hashlib
import json
import math
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.contracts.models import JournalEntry, ProviderInfo
from packages.providers.laya import (
    AGENT_CHOICES,
    CONVERSATION_CHOICES,
    FINAL_CHOICES,
    LayaRoutingProvider,
    _finalized_questions,
    _window_questions,
)
from services.decisions.window_routing import WindowClassifier

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "evaluation/decisions/2026-10-31"
EVIDENCE = ROOT / "evaluation/evidence/decisions-models-2026-10-31.json"
DOC = ROOT / "docs/decisions-model-screen.md"
FILE_PINS = {
    "fixture-inputs.json": "6155e428c3ac53a7a3048c3b8d4156d896923f5e19f98f3376a5b2d31bf8bf98",
    "fixture-references.json": "cab6a619481ae8315348621cae783c075b66bcf2b56da231f1869a3ce682c9d9",
    "manifest.json": "2f19b4957b1cfd0bf90505a99a98ec3e2fa8afe909e6eae69f32b3bac557c76b",
    "README.md": "b62756530b5f89f5e8fe568b5c97fe5c01841efeb7fa447c507e0c29fce03437",
}
CASE_IDS = json.loads((FIXTURE / "manifest.json").read_text())["ordering"]["case_ids"]
WINDOW_IDS = [case_id for case_id in CASE_IDS if case_id.startswith("W")]


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_load(path):
    def pairs(items):
        result = {}
        for key, value in items:
            assert key not in result, "Duplicate publication field"
            result[key] = value
        return result

    def constant(_):
        raise AssertionError("Nonfinite publication literal")

    value = json.loads(path.read_bytes(), object_pairs_hook=pairs, parse_constant=constant)
    canonical(value)
    return value


@pytest.fixture(scope="module")
def corpus():
    inputs = strict_load(FIXTURE / "fixture-inputs.json")
    references = strict_load(FIXTURE / "fixture-references.json")
    return SimpleNamespace(
        inputs=inputs,
        cases=inputs["cases"],
        by_id={case["case_id"]: case for case in inputs["cases"]},
        references=references["cases"],
        refs={ref["case_id"]: ref for ref in references["cases"]},
        manifest=strict_load(FIXTURE / "manifest.json"),
        evidence=strict_load(EVIDENCE),
    )


def native_adapter():
    # Only the actual pure parse/map functions are used; no client is created.
    adapter = LayaRoutingProvider.__new__(LayaRoutingProvider)
    adapter.model = "kinaiya-v4"
    return adapter


def projected(adapter, case):
    state, mode = adapter._parse_messages(
        [
            {"role": "system", "content": "Native offline projection check"},
            {"role": "user", "content": json.dumps(case["native_state"])},
        ]
    )
    questions = (
        _window_questions(state["tasks"])
        if mode == "window"
        else _finalized_questions(state["tasks"])
    )
    return state, mode, {"state": adapter._render_state(state), "questions": questions}


def one_hot_reference(case, reference):
    answers = {}
    for name, question in case["model_input"]["questions"].items():
        choice = reference["acceptable_choices"][name][0]
        answers[name] = {
            "type": "choice",
            "choice": choice,
            "probabilities": {label: int(label == choice) for label in question["criteria"]},
            "answer_confidence": 1,
        }
    return {
        "answers": answers,
        "usage": {"truncated": False, "state_tokens_dropped": 0, "truncated_questions": []},
        "routing": {"model": "kinaiya-v4"},
    }


@pytest.mark.parametrize("name", FILE_PINS)
def test_frozen_public_files_remain_exact_original_bytes(name):
    assert sha((FIXTURE / name).read_bytes()) == FILE_PINS[name]


def test_fixture_order_reference_pins_and_fixed_denominators(corpus):
    assert corpus.inputs["content_scope"] == "public_synthetic"
    assert [c["case_id"] for c in corpus.cases] == CASE_IDS
    assert [r["case_id"] for r in corpus.references] == CASE_IDS
    assert len(CASE_IDS) == len(set(CASE_IDS)) == 48
    assert Counter(c["mode"] for c in corpus.cases) == {"window": 32, "finalized": 16}
    assert sum(ref["target_scored"] for ref in corpus.references) == 36
    for case, ref, pin in zip(
        corpus.cases, corpus.references, corpus.manifest["case_pins"], strict=True
    ):
        body = {key: value for key, value in ref.items() if key != "reference_sha256"}
        assert case["case_id"] == ref["case_id"] == pin["case_id"]
        assert sha(canonical(case)) == pin["input_case_sha256"]
        assert sha(canonical(case["model_input"])) == case["model_input_sha256"]
        assert sha(canonical(case["native_state"])) == pin["native_state_sha256"]
        assert sha(canonical(body)) == ref["reference_sha256"] == pin["reference_sha256"]
    assert corpus.evidence["fixture"]["fixed_denominators"] == {
        "agent_intent": 48,
        "window_timing": 32,
        "target_choice": 36,
        "joint": 48,
        "false_work_negatives": 12,
        "false_interrupt_negatives": 36,
    }
    for entry in corpus.evidence["fixture"]["files"]:
        assert sha((ROOT / entry["path"]).read_bytes()) == entry["sha256"]


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_exact_native_projection_and_reference_roundtrip(corpus, case_id):
    adapter = native_adapter()
    case, reference = corpus.by_id[case_id], corpus.refs[case_id]
    state, mode, model_input = projected(adapter, case)
    assert mode == case["mode"] and state == case["adapter_state"]
    assert model_input == case["model_input"]
    assert list(model_input["questions"]) == list(case["model_input"]["questions"])
    for name, question in model_input["questions"].items():
        assert list(question["criteria"]) == list(
            case["model_input"]["questions"][name]["criteria"]
        )
    mapped = adapter._map_answers(
        one_hot_reference(case, reference), state, mode, model_input["questions"]
    )
    assert all(mapped[key] == value for key, value in reference["normalized_reference"].items())


def journal_entry(kind, payload, sequence):
    return JournalEntry(
        session_id="public-screen",
        thread_id="public-screen-thread",
        epoch=1,
        kind=kind,
        payload=payload,
        idempotency_key=f"public-screen-{sequence}",
        sequence=sequence,
        origin="stt" if kind == "conversation.window" else "converse",
    )


@pytest.mark.parametrize("case_id", WINDOW_IDS)
def test_window_native_request_bridge_preserves_every_frozen_state(corpus, case_id):
    case = corpus.by_id[case_id]
    state = case["native_state"]
    source = journal_entry(
        "conversation.window",
        {
            **state["latest"],
            "session_id": "public-screen",
            "thread_id": "public-screen-thread",
            "epoch": 1,
            "start_ms": 0,
            "end_ms": 100,
            "content_scope": "public_synthetic",
        },
        100,
    )
    context = [
        journal_entry(
            item["kind"], {"text": item["text"], "content_scope": "public_synthetic"}, index
        )
        for index, item in enumerate(state["context"], 1)
    ]
    provider = SimpleNamespace(info=ProviderInfo(provider="laya", model="kinaiya-v4", local=True))
    body = WindowClassifier(provider)._request_body(
        source, context, [SimpleNamespace(**task) for task in state["tasks"]]
    )
    assert json.loads(body) == state


def assert_answers(response, questions):
    assert set(response["answers"]) == set(questions)
    for name, question in questions.items():
        answer = response["answers"][name]
        assert answer["type"] == "choice" and answer["choice"] in question["criteria"]
        probabilities = answer["probabilities"]
        assert set(probabilities) == set(question["criteria"])
        assert all(
            type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
            for value in probabilities.values()
        )
        assert abs(sum(probabilities.values()) - 1) <= len(probabilities) * 0.0001 + 1e-6
        assert probabilities[answer["choice"]] + 0.0001 >= max(probabilities.values())
        if "confidence" in answer:
            confidence = answer["confidence"]
            assert type(confidence) in (int, float)
            assert type(confidence) is int or math.isfinite(confidence)


@pytest.mark.parametrize("model_index", range(7))
def test_hosted_records_exact_requests_sanitized_hashes_and_costs(corpus, model_index):
    evidence = corpus.evidence
    profile = evidence["hosted_protocol"]["provider_profiles"][model_index]
    model = evidence["cloud_models"][model_index]
    records = model["records"]
    assert model["requested_model"] == profile["requested_model"]
    assert len(records) == 49
    assert [record["case_id"] for record in records] == [None, *CASE_IDS]
    assert [record["attempt"] for record in records] == [1] * 49
    assert model["protocol_or_transport_errors"] == model["unattempted_fixed_cases"] == 0
    reservation = Decimal(profile["reservation_usd_per_attempt"])
    assert reservation == 2 * profile["context_tokens"] * Decimal(
        profile["snapshot_prompt_usd_per_token"]
    )
    total_cost = Decimal(0)
    for index, record in enumerate(records):
        datum = (
            evidence["hosted_protocol"]["protocol_probe"]
            if index == 0
            else corpus.by_id[record["case_id"]]["model_input"]
        )
        provider = profile["request_provider_preferences"]
        assert provider == {
            "only": [profile["provider_tag"]],
            "allow_fallbacks": False,
            "max_price": {
                "prompt": float(Decimal(profile["snapshot_prompt_usd_per_token"]) * 1_000_000),
                "completion": 0,
                "request": 0,
            },
        }
        request = {
            "model": profile["requested_model"],
            "state": datum["state"],
            "questions": datum["questions"],
            "provider": provider,
        }
        raw = json.dumps(
            request, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        binding = record["binding"]
        assert sha(raw) == binding["request_body_sha256"]
        assert len(raw) == binding["request_body_bytes"]
        assert (
            binding["declaration_canonical_sha256"]
            == evidence["hosted_protocol"]["declaration_canonical_sha256"]
        )
        response = record["response"]
        assert sha(canonical(response)) == binding["sanitized_response_canonical_sha256"]
        assert (
            binding["original_response_body_sha256"]
            != binding["sanitized_response_canonical_sha256"]
        )
        assert set(response) == {"model", "provider", "answers", "usage"}
        assert response["model"] in profile["allowed_response_identities"]
        assert response["provider"] == profile["provider_name"]
        assert_answers(response, datum["questions"])
        assert all(
            set(answer) == {"type", "choice", "probabilities", "confidence"}
            for answer in response["answers"].values()
        )
        assert record["http_status"] == 200 and record["status"] == "ok"
        assert record["error"] is record["fatal"] is None
        transport = record["transport"]
        assert all(
            transport[key] is True
            for key in [
                "send_started",
                "eof",
                "full_raw_capture",
                "response_closed",
                "client_closed",
                "cleanup_confirmed",
            ]
        )
        assert transport["cancelled"] is False and 0 <= transport["elapsed_seconds"] <= 20
        cost = Decimal(record["provider_reported_cost_usd"])
        assert cost == Decimal(str(response["usage"]["cost"]))
        assert cost == response["usage"]["input_tokens"] * Decimal(
            profile["snapshot_prompt_usd_per_token"]
        )
        assert cost <= reservation and record["confirmed_billing_usd"] is None
        total_cost += cost
        if index == 0:
            assert record["purpose"] == "protocol_probe"
            assert response["answers"]["probe"]["choice"] == "BLUE"
        else:
            assert record["purpose"] == "fixed_case"
    assert Decimal(model["costs_including_probe"]["provider_reported_cost_usd_sum"]) == total_cost
    assert Decimal(model["costs_including_probe"]["reserved_usd"]) == reservation * 49


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_published_laya_answers_pass_actual_native_mapper_without_inference(corpus, case_id):
    model = corpus.evidence["local_Laya"]
    record = next(row for row in model["records"] if row["case_id"] == case_id)
    case = corpus.by_id[case_id]
    adapter = native_adapter()
    state, mode, datum = projected(adapter, case)
    response = record["response"]
    assert_answers(response, datum["questions"])
    assert sha(canonical(response)) == record["binding"]["sanitized_response_canonical_sha256"]
    assert (
        adapter._map_answers(response, state, mode, datum["questions"])
        == record["native_normalized"]
    )
    request = {
        "state": datum["state"],
        "model": model["requested_model"],
        "questions": datum["questions"],
    }
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    assert sha(raw) == record["binding"]["request_body_sha256"]
    assert record["transport"]["eof_observed"] is True
    assert record["transport"]["close_confirmed"] is True
    assert record["transport"]["capture_truncated"] is False
    assert record["transport"]["error_category"] is None


def normalized(case, answers):
    key = "agent_route" if case["mode"] == "window" else "route"
    raw_route = answers[key]["choice"]
    result = {
        key: (AGENT_CHOICES if case["mode"] == "window" else FINAL_CHOICES)[raw_route],
        "target_task_id": None,
        "expected_objective_revision": None,
    }
    if case["mode"] == "window":
        result["conversation"] = CONVERSATION_CHOICES[answers["conversation"]["choice"]]
    target = answers.get("target", {}).get("choice", "NONE")
    if raw_route != "conversation" and target != "NONE":
        task = case["adapter_state"]["tasks"][int(target[1:])]
        result.update(
            target_task_id=task["task_id"], expected_objective_revision=task["objective_revision"]
        )
    return result


def confusion(rows, truth, prediction):
    result = {}
    for row in rows:
        expected, observed = truth(row), prediction(row)
        result.setdefault(expected, {})[observed] = (
            result.setdefault(expected, {}).get(observed, 0) + 1
        )
    return result


@pytest.mark.parametrize("model_index", range(8))
def test_all_published_metrics_recompute_from_literal_answers(corpus, model_index):
    model = [*corpus.evidence["cloud_models"], corpus.evidence["local_Laya"]][model_index]
    records = [row for row in model["records"] if row["purpose"] == "fixed_case"]
    assert [row["case_id"] for row in records] == CASE_IDS
    rows = []
    for record in records:
        case = corpus.by_id[record["case_id"]]
        ref = corpus.refs[record["case_id"]]
        answers = record["response"]["answers"]
        choices = {key: answer["choice"] for key, answer in answers.items()}
        native = normalized(case, answers)
        key = "agent_route" if case["mode"] == "window" else "route"
        intent_ok = choices[key] in ref["acceptable_choices"][key]
        timing_ok = (
            case["mode"] != "window"
            or choices["conversation"] in ref["acceptable_choices"]["conversation"]
        )
        target_native_ok = all(
            native[field] == ref["normalized_reference"][field]
            for field in ["target_task_id", "expected_objective_revision"]
        )
        target_label_ok = (
            "target" not in choices or choices["target"] in ref["acceptable_choices"]["target"]
        )
        elapsed = record["transport"].get(
            "elapsed_seconds", record["transport"].get("adapter_and_identity_elapsed_seconds")
        )
        rows.append(
            {
                "case_id": case["case_id"],
                "mode": case["mode"],
                "truth": ref["acceptable_choices"][key][0],
                "prediction": choices[key],
                "choices": choices,
                "intent_ok": intent_ok,
                "timing_ok": timing_ok,
                "target_scored": ref["target_scored"],
                "target_ok": target_native_ok and target_label_ok,
                "joint_ok": intent_ok
                and timing_ok
                and target_native_ok
                and (target_label_ok or not ref["target_scored"]),
                "elapsed": elapsed,
            }
        )
    metric = model["metrics"]
    for mode in ["pooled", "window", "finalized"]:
        group = [row for row in rows if mode == "pooled" or row["mode"] == mode]
        assert metric["groups"][mode] == {
            "cases": len(group),
            "valid": len(group),
            "errors": 0,
            "agent_correct": sum(row["intent_ok"] for row in group),
            "agent_confusion": confusion(
                group, lambda row: row["truth"], lambda row: row["prediction"]
            ),
            "target_denominator": sum(row["target_scored"] for row in group),
            "target_correct": sum(row["target_scored"] and row["target_ok"] for row in group),
            "joint_correct": sum(row["joint_ok"] for row in group),
        }
    windows = [row for row in rows if row["mode"] == "window"]
    assert metric["window_conversation"] == {
        "denominator": 32,
        "correct": sum(row["timing_ok"] for row in windows),
        "confusion": confusion(
            windows,
            lambda row: corpus.refs[row["case_id"]]["acceptable_choices"]["conversation"][0],
            lambda row: row["choices"]["conversation"],
        ),
    }
    for name, subset, harmful in [
        (
            "false_work",
            [row for row in rows if row["truth"] == "conversation"],
            lambda row: row["prediction"] != "conversation",
        ),
        (
            "false_interrupt",
            [row for row in rows if row["truth"] != "cancellation"],
            lambda row: row["prediction"] == "cancellation",
        ),
    ]:
        count = sum(harmful(row) for row in subset)
        assert metric["harms"][name] == {
            "denominator": len(subset),
            "observed": count,
            "errors": 0,
            "unattempted": 0,
            "pessimistic_numerator": count,
        }
    assert [row["case_id"] for row in metric["mismatches"]] == [
        row["case_id"] for row in rows if not row["joint_ok"]
    ]
    elapsed = sorted(row["elapsed"] for row in rows)
    assert metric["latency_seconds"] == {
        "count": 48,
        "p50": elapsed[23],
        "p95": elapsed[math.ceil(48 * 0.95) - 1],
        "method": "nearest rank",
    }


def test_cost_totals_and_literal_confidence_remain_distinct(corpus):
    cloud = corpus.evidence["cloud_models"]
    records = [record for model in cloud for record in model["records"]]
    assert len(records) == len({record["slot"] for record in records}) == 343
    assert len({record["binding"]["provider_response_id_sha256"] for record in records}) == 343
    assert sum(Decimal(record["provider_reported_cost_usd"]) for record in records) == Decimal(
        "0.008520861"
    )
    assert sum(Decimal(record["reservation_usd"]) for record in records) == Decimal("2.772142464")
    assert corpus.evidence["budget"]["confirmed_billing_usd"] is None
    assert corpus.evidence["budget"]["reservation_refunds_usd"] == "0"
    answers = [answer for record in records for answer in record["response"]["answers"].values()]
    assert any(
        answer["confidence"] != answer["probabilities"][answer["choice"]] for answer in answers
    )
    assert all("answer_confidence" not in answer for answer in answers)
    assert any(record["response"]["usage"]["output_tokens"] > 0 for record in records)


def test_native_ignore_target_head_does_not_create_a_task_binding(corpus):
    case = corpus.by_id["W06"]
    adapter = native_adapter()
    state, mode, datum = projected(adapter, case)
    reply = one_hot_reference(case, corpus.refs["W06"])
    reply["answers"]["target"].update(choice="t0", probabilities={"NONE": 0, "t0": 1})
    mapped = adapter._map_answers(reply, state, mode, datum["questions"])
    assert mapped["agent_route"] == "IGNORE_FOR_IMMEDIATE_ROUTING"
    assert mapped["target_task_id"] is mapped["expected_objective_revision"] is None


def test_local_d1_refusal_has_no_inference_score_or_capacity_claim(corpus):
    trial = corpus.evidence["downloaded_LFM3_D1_Q8_startup_trial"]
    assert trial["native_load_attempts"] == 1
    assert trial["protocol_posts"] == trial["fixed_fixture_posts"] == trial["retries"] == 0
    assert trial["comparison_quality_score"] is None
    assert trial["native_failure"] == "native_start_refused"
    assert trial["native_readiness_error"] == "readiness-failed"
    guard = trial["guard_observation"]
    assert guard["observer_predicate"] == "owned_allocation_exceeds_projected_cap"
    assert guard["maximum_sampled_owned_vram_bytes"] == 2_129_195_008
    assert guard["projected_cap_bytes"] == 2_046_820_352
    assert guard["excess_bytes"] == 82_374_656
    assert guard["last_sample_global_free_bytes"] > guard["global_free_floor_bytes"]
    assert guard["OOM_shown_in_retained_logs"] is False
    assert guard["general_model_incompatibility_or_cannot_fit_proven"] is False
    assert guard["per_PID_DRM_equals_backend_projected_model_allocation"] is False
    assert trial["whole_GPU_stack_8GB_or_12GB_qualified"] is False
    assert (
        trial["cleanup"]["confirmed"]
        is trial["cleanup"]["reaped"]
        is trial["cleanup"]["group_empty"]
        is True
    )
    assert (
        trial["cleanup"]["residual_process_count"]
        == trial["cleanup"]["other_model_resource_control_calls"]
        == 0
    )
    assert trial["cleanup"]["existing_models_stopped_unloaded_or_retuned"] is False


def test_review_privacy_and_qualification_are_truthfully_bounded(corpus):
    evidence = corpus.evidence
    review = evidence["review_and_verification"]
    assert len(review["initial_review_blockers"]) == 2
    assert review["offline_audit_regressions"]["before_repairs"]["failed"] == 18
    assert review["offline_audit_regressions"]["complete_repaired_suite"]["passed"] == 109
    assert review["offline_audit_regressions"]["lead_unchanged_repeat"]["passed"] == 109
    assert review["independent_completed_run_audit"]["material_failures"] == []
    assert review["independent_completed_run_audit"]["runner_summarize_imported_or_called"] is False
    assert evidence["fixture"]["now_consumed"] is True
    assert evidence["publication"]["original_full_capture_bodies_public"] is False
    for name in [
        "native_M3_admission_or_dispatch",
        "permissions",
        "Agent_task_success",
        "whole_GPU_stack_8GB",
        "whole_GPU_stack_12GB",
        "16GB_profile_tests",
        "32GB_profile_tests",
        "production_adapter_changed",
        "settings_or_default_changed",
        "quality_or_default_promotion",
        "downloaded_D1_is_cloud_liquid_d1",
    ]:
        assert evidence["qualification"][name] is False
    assert evidence["fixture"][
        "seven_typed_intent_vs_generic_ambiguity_policy_mismatch_case_ids"
    ] == ["W21", "W23", "W28", "W29", "F11", "F15", "F16"]
    assert (
        evidence["local_Laya"]["raw_report_sha256"]
        == "470598e828778634ecaebb331cd3a6bc6f58a4b8c71f0d41eb0ce73188da937c"
    )
    for path in [EVIDENCE, DOC, *(FIXTURE / name for name in FILE_PINS)]:
        raw = path.read_bytes()
        for forbidden in [
            b"/home/",
            b"/tmp/",
            b"Bearer ",
            b"Authorization:",
            b"sk-or-v1-",
            b"X-Amz-Signature",
        ]:
            assert forbidden not in raw
    assert "actionable target" not in DOC.read_text().lower()
