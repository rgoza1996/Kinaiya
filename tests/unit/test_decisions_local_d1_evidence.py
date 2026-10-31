"""Public local-D1 artifact consistency; no inference or resource calls."""

import hashlib
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evaluation/evidence/decisions-local-d1-2026-10-31.json"
DOC = ROOT / "docs/decisions-model-screen.md"


def record():
    return json.loads(EVIDENCE.read_text())


def test_phase_one_and_frozen_48_case_corpus_remain_byte_exact():
    data = record()
    pins = data["immutable_prior_files_sha256"]
    assert len(pins) == 5
    for name, expected in pins.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
    phase_one = json.loads(
        (ROOT / "evaluation/evidence/decisions-models-2026-10-31.json").read_text()
    )
    first = data["attempts"][0]
    prior = phase_one["downloaded_LFM3_D1_Q8_startup_trial"]
    assert (
        first["telemetry"]["maximum_sampled_owned_vram_bytes"]
        == prior["guard_observation"]["maximum_sampled_owned_vram_bytes"]
    )
    assert (
        first["configuration"]["profile"]["owned_observer_cap_bytes"]
        == prior["guard_observation"]["projected_cap_bytes"]
    )
    assert not data["publication"]["prior_phase1_record_changed"]
    assert not data["publication"]["frozen_48_case_files_changed"]


def test_all_five_startup_failures_have_no_inference_score_or_latency():
    data = record()
    attempts = data["attempts"][:5]
    assert [trial["attempt"] for trial in attempts] == [1, 2, 3, 4, 5]
    assert sum(trial["native_load_attempts"] for trial in attempts) == 5
    assert all(
        trial["systemone_protocol_posts"] == trial["fixed_fixture_posts"] == 0 for trial in attempts
    )
    assert (
        hashlib.sha256(canonical(attempts)).hexdigest()
        == (data["publication"]["attempts_01_to_05_canonical_sha256"])
        == "406407c72d36d11e9bfcba6e32d86189fe6ca210e46faeb94f3695cef320377c"
    )
    for trial in attempts:
        assert trial["status"] == "failed"
        assert trial["systemone_protocol_posts"] == trial["fixed_fixture_posts"] == 0
        assert trial["valid_fixed_cases"] == 0 and trial["literal_inference_records"] == []
        assert trial["quality_metrics"] is None
        assert all(value is None for value in trial["latency"].values())
        assert trial["startup_timing"]["ready_startup_ms"] is None
        assert not trial["startup_timing"]["telemetry_span_is_startup_to_ready_latency"]
        assert trial["fixed_cases_unattempted"] == trial["fixed_cases_planned"]
        assert not trial["fixed_denominator_placeholder_is_model_accuracy"]
    assert [trial["fixed_cases_planned"] for trial in attempts] == [0, 48, 48, 0, 48]


def test_sampled_vram_extrema_recompute_without_inventing_peak_or_missing_zero():
    attempts = record()["attempts"]
    assert sum(trial["telemetry"]["samples"] for trial in attempts) == 2602
    for trial in attempts:
        values = trial["telemetry"]
        times, owned, global_used = (
            values["relative_times"],
            values["owned_vram_bytes"],
            values["global_used_bytes"],
        )
        assert len(times) == len(owned) == len(global_used) == values["samples"]
        assert times[0] == 0 and all(type(value) is int and value >= 0 for value in times)
        assert values["unavailable_owned_readings_retained"] == sum(
            value is None for value in owned
        )
        assert (
            max(value for value in owned if value is not None)
            == values["maximum_sampled_owned_vram_bytes"]
        )
        assert max(global_used) == values["maximum_sampled_global_used_bytes"]
        assert (
            values["global_total_bytes"] - max(global_used)
            == values["minimum_sampled_global_free_bytes"]
        )
        assert max(times) / 1_000_000 == trial["startup_timing"]["telemetry_observation_span_ms"]
        assert not values["continuous_true_peak_proven"]
        assert not values["sample_maximum_is_model_weight_size"]
        assert not values["whole_stack_8_or_12_qualified"]


def test_literal_health_replies_recompute_hashes_without_becoming_model_readiness():
    attempts = record()["attempts"][:5]
    assert [trial["health"]["get_requests"] for trial in attempts] == [33, 1, 59, 1, 1]
    for trial in attempts:
        health = trial["health"]
        assert (
            sum(row["observations"] for row in health["response_catalog"]) == health["get_requests"]
        )
        for row in health["response_catalog"]:
            raw = row["response_utf8"].encode()
            assert hashlib.sha256(raw).hexdigest() == row["response_sha256"]
            assert len(raw) == row["response_bytes"] <= 131072
            assert json.loads(raw) == row["response"]
            assert row["response"] in (
                {"status": "ok"},
                {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}},
            )
            if trial["attempt"] != 1:
                assert row["eof_observed"] is row["close_confirmed"] is True
        assert not health["native_model_readiness_completed"]
        assert not health["systemone_protocol_validated"]
    assert attempts[2]["health"]["http_200_observed"]
    assert (
        sum(
            row["observations"]
            for row in attempts[2]["health"]["response_catalog"]
            if row["http_status"] == 200
        )
        == 58
    )


def test_cleanup_and_partial_placement_do_not_invent_a_cause_or_cpu_amount():
    attempts = record()["attempts"][:5]
    for trial in attempts:
        cleanup = trial["cleanup"]
        assert cleanup["confirmed"] is cleanup["http_cleanup_confirmed"] is True
        assert cleanup["owned_child_reaped"] is cleanup["group_empty"] is True
        assert cleanup["ownership_verified"] and cleanup["residual_process_count"] == 0
        assert cleanup["other_resource_control_calls"] == cleanup["model_unloads"] == 0
        assert not cleanup["qwen_reload"]
        assert not cleanup["stop_receipt_is_complete_signal_history"]
        assert not trial["failure"]["proven_self_exit"]
        assert not trial["failure"]["term_sent_false_proves_self_exit"]
        assert not trial["failure"]["descriptor_read_race_proven"]
        if trial["attempt"] != 1:
            assert cleanup["temporary_owned_api_key_removed"]
            assert cleanup["preserved_workers_and_policy_unchanged"]
    fourth = attempts[3]["native_numeric_log_projection"]
    assert fourth["offloaded_layers"] == fourth["total_layers"] == 25
    assert fourth["cpu_model_weight_refusal"] and fourth["cpu_model_buffer_amount_mib"] is None
    assert not fourth["all_gpu_placement_qualified"]
    fifth = attempts[4]["native_numeric_log_projection"]
    assert fifth["offloaded_layers"] == fifth["total_layers"] == 25
    assert fifth["model_buffers"] == [
        {
            "buffer": "ROCm0",
            "reported_mib_decimal": "1752.84",
            "decimal_places": 2,
            "exact_bytes_known": False,
            "positive": True,
        }
    ]
    assert not fifth["full_logs_or_eof_proven"]


def test_laya_reference_keeps_distinct_boundaries_without_promoting_defaults():
    data = record()
    original = json.loads(
        (ROOT / "evaluation/evidence/decisions-models-2026-10-31.json").read_text()
    )
    reference = data["laya_reference"]
    laya = original["local_Laya"]
    assert reference["adapter_and_identity_p50_ms"] == round(
        laya["metrics"]["latency_seconds"]["p50"] * 1000, 3
    )
    assert reference["adapter_and_identity_p95_ms"] == round(
        laya["metrics"]["latency_seconds"]["p95"] * 1000, 3
    )
    assert reference["http_forward_through_close_p50_ms"] == round(
        laya["transport_latency_seconds"]["p50"] * 1000, 3
    )
    assert reference["http_forward_through_close_p95_ms"] == round(
        laya["transport_latency_seconds"]["p95"] * 1000, 3
    )
    assert not reference["controlled_speed_comparison_with_D1"]
    assert reference["raw_typed_quality"] == {
        "agent_intent": {
            "correct": laya["metrics"]["groups"]["pooled"]["agent_correct"],
            "denominator": 48,
        },
        "conversation_timing": {
            "correct": laya["metrics"]["window_conversation"]["correct"],
            "denominator": 32,
        },
        "target": {
            "correct": laya["metrics"]["groups"]["pooled"]["target_correct"],
            "denominator": 36,
        },
        "joint": {
            "correct": laya["metrics"]["groups"]["pooled"]["joint_correct"],
            "denominator": 48,
        },
        "false_work": {
            "observed": laya["metrics"]["harms"]["false_work"]["observed"],
            "denominator": 12,
        },
        "false_interrupt": {
            "observed": laya["metrics"]["harms"]["false_interrupt"]["observed"],
            "denominator": 36,
        },
    }
    assert "pending_attempt_06" not in data
    qualified = data["qualification"]
    assert qualified["local_D1_protocol_qualified"] is True
    assert qualified["observed_ROCm0_model_weight_placement_readiness_passed"] is True
    for key in (
        "native_systemone_adapter_integrated",
        "native_M3_admission_dispatch_qualified",
        "default_or_settings_promoted",
        "end_to_end_GPU_execution_qualified",
        "whole_stack_8GB",
        "whole_stack_12GB",
        "16GB_profile_tests",
        "32GB_profile_tests",
        "actual_STT_TTS_audio_overlap_qualified",
    ):
        assert qualified[key] is False


def test_projection_has_only_public_identity_and_no_private_logs_or_paths():
    data = record()
    assert (
        data["model"]["sha256"]
        == "aedf1a7c341b4281979423dbc39e9c3d3ad1da8834156c8e75af5399ca3b0861"
    )
    assert data["backend"]["reported_commit"] == "0893ec9"
    assert not data["backend"]["pins_are_complete_runtime_attestation"]
    for trial in data["attempts"]:
        for key in (
            "declaration_sha256",
            "runner_sha256",
            "actual_result_sha256",
            "gpu_observations_sha256",
        ):
            assert re.fullmatch(r"[a-f0-9]{64}", trial["source"][key])
    for path in (EVIDENCE, DOC):
        text = path.read_text()
        assert not any(
            secret in text
            for secret in ("/home/", "/tmp/", ".dev-run/", "Bearer ", "Authorization:", "sk-or-v1-")
        )
        assert not re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", text)
    pending = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            assert not set(item) & {
                "pid",
                "pgid",
                "start_ticks",
                "headers",
                "argv",
                "child_env",
                "source_root",
                "auth_file",
                "raw_text",
                "user_message",
            }
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def frozen_cases():
    folder = ROOT / "evaluation/decisions/2026-10-31"
    cases = json.loads((folder / "fixture-inputs.json").read_text())["cases"]
    refs = json.loads((folder / "fixture-references.json").read_text())["cases"]
    return cases, {row["case_id"]: row for row in refs}


def test_completed_six_preserves_exact_request_and_literal_response_bindings():
    data = record()
    trial = data["attempts"][5]
    cases, _ = frozen_cases()
    assert [row["attempt"] for row in data["attempts"]] == list(range(1, 7))
    assert (
        data["total_native_load_attempts"]
        == sum(row["native_load_attempts"] for row in data["attempts"])
        == 6
    )
    assert data["total_systemone_protocol_posts"] == trial["systemone_protocol_posts"] == 1
    assert data["total_fixed_fixture_posts"] == trial["fixed_fixture_posts"] == 48
    assert (
        data["total_systemone_posts"]
        == data["total_systemone_protocol_posts"] + data["total_fixed_fixture_posts"]
        == 49
    )
    assert trial["fixed_cases_unattempted"] == 0 and trial["valid_fixed_cases"] == 48
    assert trial["status"] == "completed_public_synthetic_comparison" and trial["failure"] is None
    records = trial["literal_inference_records"]
    assert [row["case_id"] for row in records] == [case["case_id"] for case in cases]
    assert [row["position"] for row in records] == list(range(1, 49))
    alias = "d1-q8-controlled-public-02"
    for case, row in zip(cases, records, strict=True):
        assert row["request"] == {"model": alias, **case["model_input"]}
        assert row["mode"] == case["mode"]
    responses = [trial["first_protocol_inference"], *records]
    assert len(responses) == data["completed_inference_response_count"] == 49
    for row in responses:
        assert set(row["request"]) == {"model", "questions", "state"}
        assert row["request"]["model"] == alias
        assert len(canonical(row["request"])) == row["request_bytes"] <= 2032
        assert (
            hashlib.sha256(canonical(row["request"])).hexdigest() == row["request_canonical_sha256"]
        )
        raw = row["response_utf8"].encode("utf-8")
        response = json.loads(raw)
        assert response == row["response"]
        assert len(raw) == row["response_bytes"] <= 131072
        assert (
            hashlib.sha256(raw).hexdigest()
            == row["original_response_body_sha256"]
            == row["published_response_utf8_sha256"]
        )
        assert (
            hashlib.sha256(canonical(response)).hexdigest()
            == row["published_response_canonical_sha256"]
        )
        assert not row["response_content_fields_changed"]
        assert row["response_canonicalization_changes_serialized_bytes"] == (
            raw != canonical(response)
        )
        assert set(response) == {"model", "answers", "usage"} and response["model"] == alias
        assert set(response["usage"]) == {"input_tokens", "output_tokens"}
        assert (
            type(response["usage"]["input_tokens"]) is int and response["usage"]["input_tokens"] > 0
        )
        assert response["usage"]["output_tokens"] == 0
        assert set(response["answers"]) == set(row["request"]["questions"])
        for key, answer in response["answers"].items():
            assert set(answer) == {"type", "choice", "probabilities", "confidence"}
            assert answer["type"] == "choice"
            assert set(answer["probabilities"]) == set(row["request"]["questions"][key]["criteria"])
            assert answer["choice"] in answer["probabilities"]
            assert all(
                type(v) is float and math.isfinite(v) and 0 <= v <= 1
                for v in [answer["confidence"], *answer["probabilities"].values()]
            )
            assert math.isclose(sum(answer["probabilities"].values()), 1, abs_tol=1e-12)
        wire = row["wire"]
        assert (
            wire["method"] == "POST"
            and wire["path"] == "/v1/systemone"
            and wire["http_status"] == 200
        )
        assert (
            wire["sent"]
            is wire["eof_observed"]
            is wire["close_confirmed"]
            is wire["full_observed_response_hash"]
            is True
        )
        assert wire["error"] is None
    assert trial["first_protocol_inference"]["response"]["answers"]["marker"]["choice"] == "CEDAR"


def normalize(case, choices):
    key = "agent_route" if case["mode"] == "window" else "route"
    route = choices[key]
    route_map = dict(
        zip(
            ("conversation", "correction", "cancellation", "new_task"),
            (
                (
                    "IGNORE_FOR_IMMEDIATE_ROUTING",
                    "STEER_CURRENT_TASK",
                    "INTERRUPT_CURRENT_TASK",
                    "QUEUE_UNTIL_TASK_DONE",
                )
                if case["mode"] == "window"
                else ("ignore", "steer", "interrupt", "queue")
            ),
            strict=True,
        )
    )
    native = {key: route_map[route], "target_task_id": None, "expected_objective_revision": None}
    if case["mode"] == "window":
        native["conversation"] = {
            "backchannel": "IGNORE",
            "unfinished": "WAIT_FOR_NEXT_WINDOW",
            "urgent": "RESPOND_INTERRUPT_USER",
            "ready": "RESPOND_WITHOUT_INTERRUPTING_USER",
        }[choices["conversation"]]
    target = choices.get("target", "NONE")
    if route != "conversation" and target != "NONE":
        task = case["adapter_state"]["tasks"][int(target[1:])]
        native.update(
            target_task_id=task["task_id"], expected_objective_revision=task["objective_revision"]
        )
    return native


def confusion(rows, expected_key, observed_key):
    result = {}
    for row in rows:
        counts = result.setdefault(row[expected_key], {})
        counts[row[observed_key]] = counts.get(row[observed_key], 0) + 1
    return result


def test_all_six_metrics_and_confusions_recompute_from_literal_answers():
    data = record()
    trial = data["attempts"][5]
    cases, refs = frozen_cases()
    rows = []
    for case, observed in zip(cases, trial["literal_inference_records"], strict=True):
        ref = refs[case["case_id"]]
        choices = {key: a["choice"] for key, a in observed["response"]["answers"].items()}
        assert choices == observed["choices"]
        native = normalize(case, choices)
        assert native == observed["normalized"]
        key = "agent_route" if case["mode"] == "window" else "route"
        intent = choices[key] in ref["acceptable_choices"][key]
        timing = (
            case["mode"] != "window"
            or choices["conversation"] in ref["acceptable_choices"]["conversation"]
        )
        target_native = all(
            native[k] == ref["normalized_reference"][k]
            for k in ("target_task_id", "expected_objective_revision")
        )
        target_label = (
            "target" not in choices or choices["target"] in ref["acceptable_choices"]["target"]
        )
        rows.append(
            {
                "mode": case["mode"],
                "truth": ref["acceptable_choices"][key][0],
                "prediction": choices[key],
                "intent": intent,
                "timing": timing,
                "target_scored": ref["target_scored"],
                "target": target_native and target_label,
                "joint": intent
                and timing
                and target_native
                and (target_label or not ref["target_scored"]),
                "timing_truth": ref["acceptable_choices"].get("conversation", [None])[0],
                "timing_prediction": choices.get("conversation"),
            }
        )
    metric = trial["quality_metrics"]
    assert metric == data["local_D1_quality_score"]
    for mode in ("pooled", "window", "finalized"):
        group = [r for r in rows if mode == "pooled" or r["mode"] == mode]
        assert metric["groups"][mode] == {
            "cases": len(group),
            "valid": len(group),
            "errors": 0,
            "agent_correct": sum(r["intent"] for r in group),
            "target_denominator": sum(r["target_scored"] for r in group),
            "target_correct": sum(r["target_scored"] and r["target"] for r in group),
            "joint_correct": sum(r["joint"] for r in group),
            "agent_confusion": confusion(group, "truth", "prediction"),
        }
    windows = [r for r in rows if r["mode"] == "window"]
    assert metric["window_conversation"] == {
        "correct": sum(r["timing"] for r in windows),
        "denominator": len(windows),
        "confusion": confusion(windows, "timing_truth", "timing_prediction"),
    }
    for name, negatives, harmful in (
        (
            "false_work",
            [r for r in rows if r["truth"] == "conversation"],
            lambda r: r["prediction"] != "conversation",
        ),
        (
            "false_interrupt",
            [r for r in rows if r["truth"] != "cancellation"],
            lambda r: r["prediction"] == "cancellation",
        ),
    ):
        count = sum(harmful(r) for r in negatives)
        assert metric["harms"][name] == {
            "denominator": len(negatives),
            "observed": count,
            "errors": 0,
            "pessimistic_numerator": count,
        }
    assert metric["native_dispatch"] == "not_assessed"
    assert metric["reference_kind"] == "synthetic_author_judgments_not_human_ground_truth"
    assert metric["unbound_intent_policy_mismatch_case_ids"] == [
        "W21",
        "W23",
        "W28",
        "W29",
        "F11",
        "F15",
        "F16",
    ]


def nearest_rank(values):
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50_ms": ordered[math.ceil(len(ordered) * 0.5) - 1] / 1_000_000,
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1] / 1_000_000,
        "max_ms": max(ordered) / 1_000_000,
    }


def test_per_case_relative_intervals_recompute_all_published_latency_summaries():
    data = record()
    trial = data["attempts"][5]
    rows = trial["literal_inference_records"]
    for row in rows:
        interval, endpoints, wire = (
            row["interval_ns"],
            row["relative_endpoint_ns"],
            row["wire"]["relative_time_ns"],
        )
        assert wire["request_start"] == 0
        assert (
            0
            <= wire["response_headers_received"]
            <= wire["first_byte"]
            <= wire["eof"]
            <= wire["close"]
            <= wire["decode_start"]
            <= wire["decode_complete"]
            <= wire["settled"]
        )
        assert interval["raw_http_through_owned_close"] == wire["close"]
        assert (
            interval["json_and_privacy_validation"]
            == wire["decode_complete"] - wire["decode_start"]
        )
        assert interval["decision_latency"] == endpoints["validation_complete"]
        assert interval["attempt_latency"] == endpoints["attempt_settled"]
        assert (
            interval["projection_inclusive_latency"]
            == endpoints["validation_complete"] - endpoints["projection_start"]
        )
        assert (
            interval["validation"]
            == endpoints["validation_complete"] - endpoints["validation_start"]
        )
        assert (
            endpoints["projection_start"]
            < 0
            < endpoints["validation_start"]
            <= endpoints["validation_complete"]
            <= endpoints["validation_settled"]
            <= endpoints["attempt_settled"]
        )
    decisions = [r["interval_ns"]["decision_latency"] for r in rows]
    attempts = [r["interval_ns"]["attempt_latency"] for r in rows]
    summary = trial["latency"]
    assert nearest_rank(decisions) == summary["fixed_48_nearest_rank_including_first"]
    assert nearest_rank(attempts) == summary["all_48_attempt_nearest_rank_including_first"]
    assert nearest_rank(decisions[1:]) == summary["remaining_47_valid_decisions"]
    assert decisions[0] / 1e6 == summary["first_case_valid_decision_ms"] == 99.837706
    assert attempts[0] / 1e6 == summary["first_case_attempt_ms"]
    assert data["local_D1_p50_ms"] == nearest_rank(decisions)["p50_ms"] == 100.447251
    assert data["local_D1_p95_ms"] == nearest_rank(decisions)["p95_ms"] == 121.687
    probe = trial["first_protocol_inference"]
    assert probe["validated_after_origin_ns"] / 1e6 == probe["latency_ms"] == 73.668757
    assert (
        0 < probe["wire"]["relative_time_ns"]["request_start"] < probe["validated_after_origin_ns"]
    )
    assert trial["startup_timing"]["ready_startup_ms"] == 1223.723169
    assert trial["startup_timing"]["manager_start_duration_s"] == 1.2236996289575472


def test_ready_numeric_proof_startup_gaps_and_final_cleanup_have_distinct_boundaries():
    trial = record()["attempts"][5]
    values, ready = trial["telemetry"], trial["native_readiness_numeric_projection"]
    assert values["samples"] == 273 and values["unavailable_owned_readings_retained"] == 4
    gaps = values["startup_telemetry_gap"]
    assert len(gaps) == len(values["owned_budget_basis"]) == 273
    assert sum(gaps) == values["startup_gap_samples"] == 3
    for i, gap in enumerate(gaps):
        if gap:
            assert values["owned_vram_bytes"][i] is None
            assert (
                values["relative_times"][i] < ready["readiness_observed_after_telemetry_origin_ns"]
            )
        assert values["owned_budget_basis"][i] == (
            "projected_cap_during_unknown_measurement"
            if values["owned_vram_bytes"][i] is None
            else "observed_bytes"
        )
    assert (
        values["unknown_startup_reservation_bytes"]
        == trial["configuration"]["profile"]["owned_observer_cap_bytes"]
        == 4294967296
    )
    assert values["maximum_sampled_owned_vram_bytes"] == 2171473920
    assert ready["offloaded_layers"] == ready["total_layers"] == 25
    assert ready["fresh_owned_vram_bytes"] == 2132508672
    ready_index = values["relative_times"].index(ready["fresh_telemetry_after_origin_ns"])
    assert values["owned_vram_bytes"][ready_index] == ready["fresh_owned_vram_bytes"]
    assert (
        ready["fresh_telemetry_after_origin_ns"]
        <= ready["readiness_observed_after_telemetry_origin_ns"]
    )
    assert {r["field"]: r["value"] for r in ready["context"]} == {
        "n_ctx": 8192,
        "n_batch": 1024,
        "n_ubatch": 1024,
    }
    assert ready["model_buffers"] == [
        {
            "buffer": "ROCm0",
            "reported_mib_decimal": "1752.84",
            "decimal_places": 2,
            "exact_bytes_known": False,
            "positive": True,
        }
    ]
    assert ready["native_placement_readiness_passed"] is ready["execution_granted"] is True
    assert ready["returncode_at_readiness"] is None and not ready["cancellation_requested"]
    assert (
        not ready["original_native_log_bytes_retained"]
        and not ready["all_runtime_kernels_attested_GPU_only"]
    )
    cleanup = trial["cleanup"]
    assert (
        cleanup["confirmed"]
        is cleanup["http_cleanup_confirmed"]
        is cleanup["owned_child_reaped"]
        is cleanup["group_empty"]
        is cleanup["ownership_verified"]
        is cleanup["temporary_owned_api_key_removed"]
        is cleanup["preserved_workers_and_policy_unchanged"]
        is True
    )
    assert (
        cleanup["native_returncode_after_cleanup"]
        == cleanup["residual_process_count"]
        == cleanup["other_resource_control_calls"]
        == cleanup["model_unloads"]
        == 0
    )
    assert cleanup["capture_persistence_errors"] == [] and not cleanup["qwen_reload"]
    assert trial["health"]["get_requests"] == 2 and trial["health"]["models_requests"] == 1
    assert (
        trial["health"]["native_model_readiness_completed"]
        is trial["health"]["systemone_protocol_validated"]
        is True
    )
    assert trial["independent_review"]["calls"] == {
        "cases": 48,
        "health_gets": 2,
        "models_get": 1,
        "posts": 49,
        "protocol": 1,
    }
    assert trial["independent_review"]["all_checks_passed"] is True


def test_confidence_literals_are_retained_without_claiming_a_general_probability_rule():
    trial = record()["attempts"][5]
    responses = [trial["first_protocol_inference"], *trial["literal_inference_records"]]
    answers = [a for r in responses for a in r["response"]["answers"].values()]
    observed = trial["observed_confidence"]
    assert len(answers) == observed["literal_fields"] == 120
    assert (
        sum(a["confidence"] == a["probabilities"][a["choice"]] for a in answers)
        == observed["confidence_equal_selected_probability_in_these_responses"]
        == 120
    )
    assert not observed["equality_is_general_protocol_rule"]
    assert not observed["confidence_is_calibrated"]
    assert not observed["laya_answer_confidence_semantics_reused"]
