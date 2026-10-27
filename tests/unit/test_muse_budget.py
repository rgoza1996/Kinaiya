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
            payload = {"model": module.MODEL, "usage": result.usage}
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
