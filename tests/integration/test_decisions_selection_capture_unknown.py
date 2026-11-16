"""Actual Session capture uncertainty must fail the positive selection oracle."""

import asyncio
from contextlib import asynccontextmanager

import pytest
import test_openrouter_decisions_selection as selection


def hold_first_finish(monkeypatch, phase):
    original_environment = selection.environment
    report = {"held": 0, "cancelled": 0, "records": []}

    @asynccontextmanager
    async def controlled(tmp_path):
        async with original_environment(tmp_path) as env:
            app = env.journal.http._transport.app

            @app.middleware("http")
            async def hold(request, call_next):
                target = (
                    request.url.path.startswith("/v1/decisions/calls/")
                    and request.url.path.endswith("/finish")
                    and report["held"] == 0
                )
                if not target:
                    return await call_next(request)
                report["held"] += 1
                if phase == "after_commit":
                    response = await call_next(request)
                    assert response.status_code == 200
                try:
                    await asyncio.Event().wait()
                finally:
                    report["cancelled"] += 1

            try:
                yield env
            finally:
                report["records"] = selection.capture_records(env)
                report["native_reservations"] = env.store.db.execute(
                    "SELECT count(*) FROM decisions_attempts"
                ).fetchone()[0]
                report["dispatch_events"] = env.store.read(
                    kinds=["routing.decided", "routing.window_decided"]
                )

    monkeypatch.setattr(selection, "environment", controlled)
    return report


def assert_unknown_original(reads, finishes):
    assert reads["held"] == reads["cancelled_reads"] == 1
    assert finishes["held"] == finishes["cancelled"] == 1
    assert len(reads["wires"]) == 1 and reads["wires"][0].requests == []
    records = finishes["records"]
    assert len(records) == 1
    assert records[0]["status"] == "started" and records[0]["error_type"] is None
    assert records[0]["raw_response"] is None and records[0]["validated_output"] is None
    assert finishes["native_reservations"] == 0 and finishes["dispatch_events"] == []


async def test_unconfirmed_finish_is_a_specific_hard_failure_without_fresh_observation(
    tmp_path, monkeypatch
):
    reads = selection.hold_latest_response(monkeypatch, 1)
    finishes = hold_first_finish(monkeypatch, "before_commit")
    with pytest.raises(AssertionError, match="Selected-provider capture finish is unconfirmed"):
        await selection.test_selected_openrouter_decisions_routes_and_captures_inspection(
            tmp_path, monkeypatch, "window"
        )
    assert_unknown_original(reads, finishes)


async def test_exhaustion_negative_control_proves_conservative_unconfirmed_failure(
    tmp_path, monkeypatch
):
    # The existing negative control installs its own held latest-window reads.
    # Wrap that helper to retain its original, independently counted report.
    original_hold = selection.hold_latest_response
    reports = []

    def observe_reads(monkeypatch, count):
        report = original_hold(monkeypatch, count)
        reports.append(report)
        return report

    monkeypatch.setattr(selection, "hold_latest_response", observe_reads)
    finishes = hold_first_finish(monkeypatch, "before_commit")
    await selection.test_selected_window_oracle_exhaustion_is_a_failure_not_success(
        tmp_path, monkeypatch
    )
    assert len(reports) == 1
    assert_unknown_original(reports[0], finishes)


async def test_lost_finish_ack_keeps_native_error_proof_before_one_actual_provider_request(
    tmp_path, monkeypatch
):
    reads = selection.hold_latest_response(monkeypatch, 1)
    finishes = hold_first_finish(monkeypatch, "after_commit")
    await selection.test_selected_openrouter_decisions_routes_and_captures_inspection(
        tmp_path, monkeypatch, "window"
    )
    assert reads["held"] == reads["cancelled_reads"] == 1
    assert finishes["held"] == finishes["cancelled"] == 1
    assert len(reads["wires"]) == 1 and len(reads["wires"][0].requests) == 1
    assert [row["status"] for row in finishes["records"]] == ["error", "completed"]
    assert finishes["records"][0]["error_type"] == "CaptureNativeUnavailable"
    assert finishes["native_reservations"] == 0 and finishes["dispatch_events"] == []


async def test_negative_control_does_not_accept_an_unrelated_assertion(tmp_path, monkeypatch):
    async def unrelated(*args, **kwargs):
        raise AssertionError("PUBLIC-SYNTHETIC-UNRELATED-FAILURE")

    monkeypatch.setattr(
        selection, "test_selected_openrouter_decisions_routes_and_captures_inspection", unrelated
    )
    with pytest.raises(AssertionError, match="PUBLIC-SYNTHETIC-UNRELATED-FAILURE"):
        await selection.test_selected_window_oracle_exhaustion_is_a_failure_not_success(
            tmp_path, monkeypatch
        )
