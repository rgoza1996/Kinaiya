"""Consistency of the public single-request native refusal; no model or GPU calls."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAPSULE = ROOT / "evaluation/evidence/decisions-d1-overflow-2026-10-31.json"


def record():
    return json.loads(CAPSULE.read_text())


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def test_prior_six_and_phase_one_are_unchanged_and_model_profile_is_bound():
    data = record()
    pins = data["unchanged_public_files_sha256"]
    assert len(pins) == 7
    for name, expected in pins.items():
        assert sha((ROOT / name).read_bytes()) == expected
    name = "evaluation/evidence/decisions-local-d1-2026-10-31.json"
    assert pins[name] == "4483ec452f5d6260636ae9db87fac0e36d05784960bd839a09f754399fbb51f8"
    prior = json.loads((ROOT / name).read_text())
    assert data["model"] == {
        key: prior["model"][key]
        for key in ("repository", "revision", "quantization", "bytes", "sha256")
    }
    assert data["backend"] == prior["backend"]
    source = data["source"]
    previous_source = prior["attempts"][5]["source"]
    assert source["head"] == previous_source["head"]
    assert source["native_file_sha256"] == previous_source["native_file_sha256"]
    assert source["actual_result_sha256"] == (
        "48a709dc77a0dd6b9c4e9f881228159c17a777f897c80041cb60141950996817"
    )
    assert source["prior_06_files_preserved"] == 229
    assert all(
        re.fullmatch("[0-9a-f]{64}", value)
        for key, value in source.items()
        if key.endswith("sha256") and isinstance(value, str)
    )
    profile = data["profile"]
    previous_profile = prior["attempts"][5]["configuration"]
    for key in ("context", "batch", "ubatch", "parallel", "all_tensor_buffer_override"):
        assert profile[key] == previous_profile[key]
    assert (profile["context"], profile["batch"], profile["ubatch"]) == (8192, 1024, 1024)
    assert profile["device"] == "ROCm0" and profile["all_tensor_buffer_override"] == ".*=ROCm0"
    assert profile["native_empty_warmup_allowed"] and not profile["cpu_fallback_allowed"]
    assert profile["owned_observer_cap_bytes"] == 4 * 1024**3
    assert profile["candidate_profile_bytes"] == 8 * 1024**3


def test_exact_one_request_and_native_error_hashes_refuse_without_becoming_success():
    data = record()
    request, response = data["request"], data["response"]
    raw = request["utf8"].encode()
    expected = {
        "model": "d1-q8-controlled-public-02",
        "questions": {
            "marker": {
                "criteria": {
                    "BEGIN_PUBLIC_OVERFLOW_STATE": "The declared start sentinel is present.",
                    "NO_PUBLIC_SENTINEL": "The declared start sentinel is absent.",
                },
                "instructions": "Which literal start sentinel is present in the state?",
                "type": "choice",
            }
        },
        "state": "Public synthetic physical-head overflow check.\nBEGIN_PUBLIC_OVERFLOW_STATE\n"
        + "x " * 2048
        + "\nEND_PUBLIC_OVERFLOW_STATE",
    }
    assert json.loads(raw) == expected
    assert raw == canonical(expected)
    assert len(raw) == request["bytes"] == 4508 <= data["profile"]["request_bytes_limit"]
    assert (
        sha(raw)
        == request["original_body_sha256"]
        == request["canonical_sha256"]
        == ("f07a008ea246d1a4bfd27f2e087fe9aa0e78d89b55c0acfe321f80f1a56e572e")
    )
    raw = response["utf8"].encode()
    native = json.loads(raw)
    assert native == response["body"]
    assert set(native) == {"error"} and set(native["error"]) == {"code", "message", "type"}
    assert native["error"]["code"] == 500 and native["error"]["type"] == "server_error"
    match = re.fullmatch(
        r"input \(([1-9][0-9]*) tokens\) is too large to process\. "
        r"increase the physical batch size \(current batch size: ([1-9][0-9]*)\)",
        native["error"]["message"],
    )
    assert match is not None
    tokens, batch = map(int, match.groups())
    boundary = data["observed_boundary"]
    assert tokens == boundary["reported_input_tokens"] == 2130
    assert batch == boundary["reported_physical_batch_limit"] == data["profile"]["batch"] == 1024
    assert tokens > batch
    assert (
        not boundary["valid_decision_returned"]
        and not boundary["token_count_independently_verified"]
    )
    assert len(raw) == response["bytes"] == 161 <= data["profile"]["response_bytes_limit"]
    assert (
        sha(raw)
        == response["original_body_sha256"]
        == response["published_utf8_sha256"]
        == ("6503f6e424b632f51e6d836eb71bec0541889abf9708dccdd73cdffdb288a0d4")
    )
    assert sha(canonical(native)) == response["canonical_sha256"]
    assert response["canonicalization_changes_serialized_bytes"] == (raw != canonical(native))
    assert not request["published_fields_or_bytes_changed"]
    assert not response["published_fields_or_bytes_changed"]
    assert data["calls"] == {
        "native_loads": 1,
        "systemone_POSTs": 1,
        "protocol_POSTs": 0,
        "fixture_POSTs": 0,
        "health_GETs": 3,
        "models_GETs": 1,
        "automatic_retries": 0,
        "other_resource_control_calls": 0,
        "model_unloads": 0,
        "qwen_reload": False,
    }
    wire = data["wire"]
    assert wire["method"] == "POST" and wire["path"] == "/v1/systemone"
    assert wire["http_status"] == 500 and wire["capture_error"] == "model_http_status_refused"
    assert all(
        wire[key]
        for key in (
            "sent",
            "eof_observed",
            "close_confirmed",
            "hash_covers_full_observed_response",
            "raw_response_saved",
        )
    )
    gets = data["other_GET_capture_receipts"]
    assert [(row["path"], row["http_status"]) for row in gets] == [
        ("/health", 503),
        ("/health", 503),
        ("/health", 200),
        ("/v1/models", 200),
    ]
    assert all(row["sent"] and row["eof_observed"] and row["close_confirmed"] for row in gets)


def test_relative_error_intervals_and_sampled_memory_recompute_without_capacity_claim():
    data = record()
    time = data["wire"]["relative_ns"]
    assert time["request_start"] == 0
    assert (
        0
        <= time["response_headers_received"]
        <= time["first_byte"]
        <= time["eof"]
        <= time["close"]
        <= time["decode_start"]
        <= time["decode_complete"]
        <= time["settled"]
    )
    timing = data["timing"]
    assert timing["error_validation_complete_after_request_ns"] >= time["settled"]
    assert (
        timing["error_validation_complete_after_request_ns"] / 1e6
        == timing["http_request_through_error_validation_ms"]
        == 38.416791
    )
    assert timing["error_wrapper_through_validation_ms"] == 56.313016
    assert not timing["wrapper_start_endpoint_retained"]
    assert timing["startup_including_empty_warmup_ms"] == 2306.539096
    assert not timing["error_path_is_decision_latency"]
    assert timing["decision_latency_ms"] is timing["decision_quality"] is None
    memory, ready = data["telemetry"], data["readiness"]
    times, owned, used, gaps = (
        memory[key]
        for key in ("relative_ns", "owned_vram_bytes", "global_used_bytes", "startup_gap")
    )
    assert len(times) == len(owned) == len(used) == len(gaps) == memory["samples"] == 91
    assert times[0] == 0 and all(type(t) is int and t >= 0 for t in times)
    assert all(a <= b for a, b in zip(times[:-1], times[1:], strict=True))
    assert sum(x is None for x in owned) == memory["all_null_owned_samples"] == 5
    assert sum(gaps) == memory["startup_gaps"] == 4
    assert owned[0] is None and gaps[0] is False
    for i, gap in enumerate(gaps):
        if owned[i] is None:
            assert times[i] < ready["observed_after_telemetry_origin_ns"]
        if gap:
            assert owned[i] is None
    assert (
        max(x for x in owned if x is not None)
        == memory["maximum_sampled_owned_vram_bytes"]
        == 2132496384
    )
    assert max(used) == memory["maximum_sampled_global_used_bytes"] == 8474005504
    assert memory["global_total_bytes"] - max(used) == memory["minimum_sampled_global_free_bytes"]
    assert (
        memory["unknown_pre_ready_reservation_bytes"] == data["profile"]["owned_observer_cap_bytes"]
    )
    assert (
        memory["post_ready_unknown_measurement_is_fatal"] and not memory["unknown_is_observed_zero"]
    )
    assert (
        not memory["continuous_true_peak_proven"]
        and not memory["whole_stack_8_or_12_GiB_qualified"]
    )
    assert (
        ready["fresh_owned_vram_bytes"]
        == owned[times.index(ready["fresh_sample_after_telemetry_origin_ns"])]
    )
    assert (
        ready["fresh_sample_after_telemetry_origin_ns"]
        <= ready["observed_after_telemetry_origin_ns"]
    )
    assert ready["offloaded_layers"] == ready["total_layers"] == 25
    assert {row["field"]: row["value"] for row in ready["context"]} == {
        "n_ctx": 8192,
        "n_batch": 1024,
        "n_ubatch": 1024,
    }
    assert ready["model_buffers"] == [
        {
            "buffer": "ROCm0",
            "decimal_places": 2,
            "exact_bytes_known": False,
            "positive": True,
            "reported_mib_decimal": "1752.84",
        }
    ]
    assert ready["native_model_weight_placement_readiness_passed"]
    assert (
        not ready["all_runtime_GPU_kernels_attested"]
        and not ready["selected_log_projection_is_full_raw_log"]
    )


def test_cleanup_and_public_projection_retain_limits_without_private_identity():
    data = record()
    cleanup = data["cleanup"]
    assert all(
        cleanup[key]
        for key in (
            "confirmed",
            "http_cleanup_confirmed",
            "owned_child_reaped",
            "group_empty",
            "ownership_verified",
            "temporary_owned_api_key_removed",
            "preserved_workers_and_owner_policy_unchanged",
        )
    )
    assert (
        cleanup["native_returncode_after_cleanup"]
        == cleanup["residual_process_count"]
        == cleanup["capture_persistence_error_count"]
        == 0
    )
    assert not cleanup["stop_receipt_is_complete_signal_history"]
    audit = data["independent_review"]
    assert audit["all_checks_passed"] and not audit["source_runner_or_scorer_imports"]
    assert audit["new_live_actions"] == 0
    assert (
        audit["receipt_sha256"]
        == "258080a2acb4266cf082eafcc353148a1aed0bcc3b55e556167b90ccb035c727"
    )
    text = CAPSULE.read_text()
    assert not any(
        s in text
        for s in ("/home/", "/tmp/", ".dev-run/", "Bearer ", "Authorization:", "sk-or-v1-")
    )
    assert not re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", text)
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            assert not set(value) & {
                "pid",
                "pgid",
                "start_ticks",
                "headers",
                "source_root",
                "auth_file",
                "raw_text",
                "argv",
                "child_env",
            }
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
