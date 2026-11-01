"""Independent oracle supplements; the original client81 stays byte-exact."""

import asyncio
import copy
import json

import pytest
from test_agent_submit_client import (
    TASK,
    WINNER,
    digest,
    done,
    native_entry,
    proposal,
    response,
    safe_error,
    scenario,
    wire,
)


def floor_proposal(*, winner=False):
    body = proposal(task_id=WINNER if winner else TASK, winner=winner)
    payload = dict(body.payload)
    payload["source_event_ids"] = ["synthetic-local-source"]
    payload["requested_content_scope"] = "public_synthetic"
    logical = {
        key: payload[key]
        for key in (
            "session_id",
            "thread_id",
            "epoch",
            "objective",
            "source_event_ids",
            "source_revision",
        )
    }
    logical["content_scope"] = "public_synthetic"
    payload["payload_digest"] = digest(logical)
    assert payload["content_scope"] == "local_only"
    return body.model_copy(update={"payload": payload}, deep=True)


async def test_retained_original_client_key_is_bound_independently_of_logical_digest():
    valid = response("admit", "retained")
    changed = copy.deepcopy(valid)
    changed["entry"]["payload"]["idempotency_key"] = "other-original-client-key"
    assert (
        changed["entry"]["payload"]["payload_digest"] == valid["entry"]["payload"]["payload_digest"]
    )
    assert {
        key
        for key in valid["entry"]["payload"]
        if valid["entry"]["payload"][key] != changed["entry"]["payload"][key]
    } == {"idempotency_key"}
    async with scenario("admit", payload=changed) as env:
        with pytest.raises(ValueError) as caught:
            await env.method(env.request)
        safe_error(caught.value)


@pytest.mark.parametrize("widened", [False, True], ids=["valid_local_floor", "invalid_widening"])
async def test_retained_effective_scope_is_bound_without_changing_requested_digest(widened):
    original, winning = floor_proposal(), floor_proposal(winner=True)
    payload = {"schema_version": "1.0", "disposition": "retained", "entry": native_entry(winning)}
    expected_digest = original.payload["payload_digest"]
    if widened:
        payload["entry"]["payload"]["content_scope"] = "public_synthetic"
    assert payload["entry"]["payload"]["requested_content_scope"] == "public_synthetic"
    assert payload["entry"]["payload"]["payload_digest"] == expected_digest
    async with scenario("admit", payload=payload) as env:
        if widened:
            with pytest.raises(ValueError) as caught:
                await env.method(original)
            safe_error(caught.value)
        else:
            retained = await env.method(original)
            assert retained.disposition == "retained"
            assert retained.entry.payload["content_scope"] == "local_only"
            assert retained.entry.payload["payload_digest"] == expected_digest
            assert retained.entry.payload["model_pin"] == winning.payload["model_pin"]


@pytest.mark.parametrize("operation,budget", [("admit", 15), ("lookup", 5)])
async def test_public_deadline_finishes_while_resistant_owned_close_remains_held(
    operation, budget, monkeypatch
):
    payload = response(operation, "unobserved" if operation == "lookup" else "created")
    async with scenario(operation, payload=payload) as env:
        env.stream.hold_close = True
        env.stream.defer_close_cancellation = True
        loop = asyncio.get_running_loop()
        monotonic, offset = loop.time, [0.0]
        try:
            with monkeypatch.context() as clock:
                clock.setattr(loop, "time", lambda: monotonic() + offset[0])
                task = env.operation_task = asyncio.create_task(env.method(env.request))
                await asyncio.wait_for(env.stream.close_entered.wait(), timeout=1)
                offset[0] = budget + 1
                # Keep close held until AFTER the public call must have failed.
                # No dependence on whether the implementation shields or
                # cancels its independently owned cleanup operation.
                with pytest.raises((TimeoutError, ValueError)) as caught:
                    await done(task)
                safe_error(caught.value)
                assert not env.stream.close_release.is_set()
                assert not env.stream.close_completed.is_set()
        finally:
            env.stream.close_release.set()
        await asyncio.wait_for(env.stream.close_completed.wait(), timeout=1)


@pytest.mark.parametrize("operation", ["admit", "lookup"])
@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-16-le", b""),
        ("utf-16-be", b""),
        ("utf-16-le", b"\xff\xfe"),
        ("utf-16-be", b"\xfe\xff"),
        ("utf-32-le", b""),
        ("utf-32-be", b""),
        ("utf-32-le", b"\xff\xfe\x00\x00"),
        ("utf-32-be", b"\x00\x00\xfe\xff"),
    ],
    ids=["16le", "16be", "16le_bom", "16be_bom", "32le", "32be", "32le_bom", "32be_bom"],
)
async def test_otherwise_valid_utf16_utf32_receipts_violate_utf8_only_wire(
    operation, encoding, bom
):
    payload = response(operation)
    raw = bom + wire(payload).decode("utf-8").encode(encoding)
    # stdlib's bytes auto-detection would accept this exact valid receipt.
    assert json.loads(raw) == payload
    async with scenario(operation, raw=raw) as env:
        with pytest.raises(ValueError) as caught:
            await env.method(env.request)
        safe_error(caught.value)
        assert env.stream.close_calls == 1
