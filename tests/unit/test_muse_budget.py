"""Budget boundaries use synthetic upstreams; these tests make no Meta calls."""

import copy
import importlib.util
import io
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def budget(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/muse_budget_proxy.py"
    spec = importlib.util.spec_from_file_location("tested_budget", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LEDGER = tmp_path / "ledger.json"
    module.STATE = {
        "limit_usd": 25,
        "admission_limit_usd": 22,
        "billing_reported_usd": None,
        "estimated_usd": 0.0,
        "contributor_estimated_usd": 0.0,
        "admission_estimated_usd": 0.0,
        "reserved_usd": 0.0,
        "reported_input_tokens": 0,
        "reported_output_tokens": 0,
        "reported_cached_tokens": 0,
        "calls": [],
    }
    calls = []
    result = SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 20}, stream=False)

    class Upstream:
        def open(self, request, timeout):
            calls.append(request)
            if hasattr(result, "opened"):
                result.opened.set()
                assert result.release.wait(timeout=5)
            payload = {"model": getattr(result, "model", module.MODEL), "usage": result.usage}
            if result.stream:
                raw = b"data: " + json.dumps({"response": payload}).encode() + b"\n\n"
            else:
                raw = json.dumps(payload).encode()
            response = io.BytesIO(raw)
            response.status = 200
            response.headers = {
                "Content-Type": "text/event-stream" if result.stream else "application/json"
            }
            return response

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *a: Upstream())
    yield module, calls, result


@pytest.fixture
def gateway(budget):
    module, calls, result = budget
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
        yield module, calls, result, client
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def test_cache_discount_never_reduces_admission(budget):
    module, _, _ = budget
    normalized, costs = module.measured_usage(
        {"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 800}}
    )
    assert normalized["input_tokens_details"]["cached_tokens"] == 800
    assert costs["contributor"] == pytest.approx(0.0000416)
    assert costs["admission"] == pytest.approx(0.00024)
    assert costs["comparison"] == pytest.approx(0.001675)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"total_tokens": 10},
        {"input_tokens": -1, "output_tokens": 2},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": "100", "output_tokens": 2},
        {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 11}},
        {"input_tokens": 10, "prompt_tokens": 11, "output_tokens": 2},
        {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": []},
        {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": None},
        {"input_tokens": 10, "output_tokens": 2, "prompt_tokens_details": {"cached_tokens": False}},
        {
            "input_tokens": 10,
            "output_tokens": 2,
            "input_tokens_details": {"cached_tokens": 1},
            "prompt_tokens_details": {"cached_tokens": 2},
        },
    ],
)
def test_incomplete_or_invalid_usage_is_uncertain(budget, usage):
    module, _, _ = budget
    with pytest.raises(ValueError):
        module.measured_usage(usage)


def test_migration_preserves_unknown_reserve_prior_charges_and_legacy_aliases(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state.update(reserved_usd=1.33, admission_estimated_usd=7.0, estimated_usd=90.0)
    state["calls"] = [
        {
            "state": "http_404_reservation_retained",
            "reservation_usd": 1.33,
        },
        {
            "state": "usage_reported",
            "reservation_usd": 1.3,
            "reported_usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1000},
        },
    ]
    module.reconcile(state)
    assert state["reserved_usd"] == 1.33
    assert state["admission_estimated_usd"] == 7.0
    assert state["ordinary_spark_comparison_usd"] == 90.0
    assert state["calls"][0]["state"] == "http_404_reservation_retained"
    assert state["calls"][1]["admission_estimated_usd"] == pytest.approx(0.2004)
    assert module.reconcile(copy.deepcopy(state)) == state


def test_bad_settled_usage_restores_reservation_once(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state["reserved_usd"] = 0.75
    state["calls"] = [
        {
            "state": "usage_reported",
            "reservation_usd": 0.25,
            "reported_usage": {"total_tokens": 100},
        }
    ]
    module.reconcile(state)
    assert state["reserved_usd"] == 1.0
    module.reconcile(state)
    assert state["reserved_usd"] == 1.0


def test_second_writer_and_symlink_lock_refuse_before_ledger_creation(budget, tmp_path):
    module, _, _ = budget
    with module.ledger_owner(module.LEDGER):
        with pytest.raises(BlockingIOError):
            with module.ledger_owner(module.LEDGER):
                pytest.fail("Two budget owners")
    assert not module.LEDGER.exists()
    other = tmp_path / "other"
    (tmp_path / "other.lock").symlink_to(tmp_path / "ledger.json.lock")
    with pytest.raises(OSError):
        with module.ledger_owner(other):
            pytest.fail("Symlink lock accepted")


@pytest.mark.parametrize("stream", [False, True])
def test_real_gateway_settles_valid_usage_without_persisting_prompt(gateway, stream):
    module, calls, result, client = gateway
    result.stream = stream
    result.usage.update(provider_debug={"prompt": "synthetic provider debug marker"})
    response = client.post(
        "/v1/responses",
        json={
            "model": module.MODEL,
            "input": "synthetic request prompt marker",
            "max_output_tokens": 999999,
        },
    )
    assert response.status_code == 200 and len(calls) == 1
    assert json.loads(calls[0].data)["max_output_tokens"] == module.OUTPUT_CAP
    assert module.STATE["calls"][0]["state"] == "usage_reported"
    assert module.STATE["reserved_usd"] == pytest.approx(0)
    assert module.STATE["reported_input_tokens"] == 100
    saved = module.LEDGER.read_text()
    assert "synthetic request prompt marker" not in saved
    assert "synthetic provider debug marker" not in saved
    assert module.LEDGER.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "usage", [None, {"total_tokens": 100}, {"input_tokens": -1, "output_tokens": 0}]
)
def test_gateway_keeps_full_reservation_for_uncertain_usage(gateway, usage):
    module, _, result, client = gateway
    result.usage = usage
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 200
    assert module.STATE["reserved_usd"] > 0.2
    assert module.STATE["admission_estimated_usd"] == 0
    assert module.STATE["calls"][0]["state"] == "usage_missing_reservation_retained"


@pytest.mark.parametrize("reported", [False, True])
def test_admission_refuses_before_any_upstream_call(gateway, reported):
    module, calls, _, client = gateway
    if reported:
        module.STATE["billing_reported_usd"] = 22.0
    else:
        module.STATE["admission_estimated_usd"] = 21.9
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 402
    assert calls == [] and module.STATE["calls"] == []


def test_unauthorized_model_and_invalid_output_never_reach_upstream(gateway):
    module, calls, _, client = gateway
    assert client.post("/v1/responses", json={"model": "muse-spark-1.3"}).status_code == 403
    assert (
        client.post(
            "/v1/responses", json={"model": module.MODEL, "max_output_tokens": -1}
        ).status_code
        == 400
    )
    assert calls == []


def test_unmetered_routes_never_reach_upstream(gateway):
    _, calls, _, client = gateway
    assert client.get("/v1/responses").status_code == 405
    assert (
        client.post("/v1/batches", json={"model": "muse-spark-1.3-contributor"}).status_code == 405
    )
    assert calls == []


def settled_contributor(module, *, cached=950_000, state="usage_reported"):
    return {
        "model": module.MODEL,
        "resolved_model": module.MODEL,
        "state": state,
        "reservation_usd": 0.2278138,
        "reported_usage": {
            "input_tokens": 1_000_000,
            "output_tokens": 20_000,
            "input_tokens_details": {"cached_tokens": cached},
        },
    }


def test_verified_settled_cache_has_separate_enforcement_basis(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state["calls"] = [settled_contributor(module)]
    module.reconcile(state)
    assert state["admission_estimated_usd"] == pytest.approx(0.208)
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(0.0218)
    assert state["calls"][0]["reported_usage"]["input_tokens_details"]["cached_tokens"] == 950_000
    assert module.reconcile(copy.deepcopy(state)) == state


@pytest.mark.parametrize("cached", [0, 1_000_000])
def test_cache_misses_and_full_hits_use_reported_counts(budget, cached):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    row = settled_contributor(module, cached=cached)
    state["calls"] = [row]
    module.reconcile(state)
    expected = 2 * ((1_000_000 - cached) * 0.10 + cached * 0.002 + 20_000 * 0.20) / 1_000_000
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(expected)
    assert state["admission_estimated_usd"] == pytest.approx(0.208)


@pytest.mark.parametrize("key", ["model", "resolved_model"])
@pytest.mark.parametrize("identity", [None, "muse-spark-1.3", "unverified-alias"])
def test_cache_credit_requires_both_contributor_identities(budget, key, identity):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    row = settled_contributor(module)
    if identity is None:
        row.pop(key)
    else:
        row[key] = identity
    state["calls"] = [row]
    module.reconcile(state)
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(0.208)


def test_cache_credit_preserves_legacy_premiums_unknown_floor_and_reservations(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    row = settled_contributor(module)
    row["admission_estimated_usd"] = 0.508  # unexplained per-record premium0.30
    state.update(admission_estimated_usd=0.758, reserved_usd=0.75, billing_reported_usd=9.0)
    unknown = {"state": "http_401_reservation_retained", "reservation_usd": 0.75}
    state["calls"] = [row, unknown]
    module.reconcile(state)
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(0.0218 + 0.30 + 0.25)
    assert state["admission_estimated_usd"] == 0.758
    assert state["reserved_usd"] == 0.75
    assert state["billing_reported_usd"] == 9.0
    assert unknown == {"state": "http_401_reservation_retained", "reservation_usd": 0.75}
    assert module.reconcile(copy.deepcopy(state)) == state


@pytest.mark.parametrize("phase", ["in_flight", "usage_missing_reservation_retained"])
def test_unsettled_usage_never_receives_cache_credit(budget, phase):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state["calls"] = [settled_contributor(module, state=phase)]
    module.reconcile(state)
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(0.208)
    assert state["reserved_usd"] == pytest.approx(0.2278138)


def test_cache_basis_never_refunds_existing_new_policy_charge(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state["calls"] = [settled_contributor(module)]
    state["cache_aware_admission_estimated_usd"] = 3.0
    module.reconcile(state)
    assert state["cache_aware_admission_estimated_usd"] == 3.0


def test_gateway_cache_settlement_and_billing_floor(gateway):
    module, calls, result, client = gateway
    result.usage = settled_contributor(module)["reported_usage"]
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 200
    assert module.STATE["admission_estimated_usd"] == pytest.approx(0.208)
    assert module.STATE["cache_aware_admission_estimated_usd"] == pytest.approx(0.0218)
    assert module.STATE["reserved_usd"] == pytest.approx(0)
    module.STATE["billing_reported_usd"] = 22.0
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 402
    assert len(calls) == 1


def test_gateway_refuses_cache_discount_for_unexpected_resolved_model(gateway):
    module, _, result, client = gateway
    result.usage = settled_contributor(module)["reported_usage"]
    result.model = "muse-spark-1.3"
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 200
    assert module.STATE["cache_aware_admission_estimated_usd"] == pytest.approx(0.208)


@pytest.mark.parametrize("tools", [[{"type": "web_search"}], [{"type": "mcp"}], {}, [None]])
def test_hosted_or_ambiguous_tools_are_not_silently_unmetered(gateway, tools):
    module, calls, _, client = gateway
    assert (
        client.post("/v1/responses", json={"model": module.MODEL, "tools": tools}).status_code
        == 403
    )
    assert calls == [] and module.STATE["calls"] == []


def test_local_function_tool_schema_remains_usable(gateway):
    module, calls, _, client = gateway
    response = client.post(
        "/v1/responses",
        json={"model": module.MODEL, "tools": [{"type": "function", "name": "public_fixture"}]},
    )
    assert response.status_code == 200 and len(calls) == 1


def test_simultaneous_requests_reserve_before_dispatch(gateway):
    from concurrent.futures import ThreadPoolExecutor

    module, calls, result, client = gateway
    module.STATE["cache_aware_admission_estimated_usd"] = 21.65
    module.STATE["admission_estimated_usd"] = 21.65
    result.opened, result.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/v1/responses", json={"model": module.MODEL})
        try:
            assert result.opened.wait(timeout=3)
            assert module.STATE["reserved_usd"] > 0.22
            second = client.post("/v1/responses", json={"model": module.MODEL})
            assert second.status_code == 402 and len(calls) == 1
        finally:
            result.release.set()
        assert first.result(timeout=3).status_code == 200
