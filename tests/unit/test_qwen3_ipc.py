"""Local dialogue wire authority and bounds; no optional model dependencies."""

import copy

import pytest

from packages.providers import qwen3_ipc as ipc

PID, INCARNATION, NONCE = 1234, "a" * 64, "b" * 64


def hello():
    return {
        "version": 1,
        "kind": "hello",
        "pid": PID,
        "incarnation": INCARNATION,
        "profile": ipc.PROFILE,
        "model": ipc.MODEL,
        "revision": ipc.REVISION,
        "manifest_sha256": ipc.MANIFEST_SHA256,
        "runtime": dict(ipc.MANIFEST["runtime"]),
        "isolation": "seccomp-network-v1",
        "input_mode": "sealed-safetensors-v1",
        "max_input_tokens": 2048,
        "max_new_tokens": 128,
    }


def parse_hello(value):
    return ipc.parse_hello(value, PID, max_input_tokens=2048, max_new_tokens=128)


def test_readiness_binds_profile_artifacts_runtime_limits_and_owned_pid():
    assert parse_hello(hello()) == INCARNATION
    for key in hello():
        value = hello()
        del value[key]
        with pytest.raises(ValueError):
            parse_hello(value)
    for key, bad in [
        ("version", True),
        ("pid", True),
        ("pid", PID + 1),
        ("incarnation", "A" * 64),
        ("revision", "f" * 40),
        ("manifest_sha256", "0" * 64),
        ("isolation", "env-only"),
        ("input_mode", "mutable-path"),
        ("max_input_tokens", 2048.0),
        ("max_new_tokens", 127),
        ("runtime", []),
    ]:
        value = hello()
        value[key] = bad
        with pytest.raises(ValueError):
            parse_hello(value)
    for key, bad in [("interop_threads", True), ("intraop_threads", 2.0), ("torch", "other")]:
        value = hello()
        value["runtime"][key] = bad
        with pytest.raises(ValueError):
            parse_hello(value)


def test_messages_are_preserved_and_detached_without_truncation():
    original = [
        {"role": "system", "content": "Public personality π"},
        {"role": "user", "content": "A public synthetic request 🌙"},
    ]
    saved = copy.deepcopy(original)
    request = ipc.request_message("generate", INCARNATION, NONCE, original)
    assert request["messages"] == original == saved
    assert request["messages"] is not original and request["messages"][0] is not original[0]
    wire = ipc.encode_json(request, ipc.MAX_REQUEST_BYTES)
    assert ipc.parse_request(ipc.decode_json(wire, ipc.MAX_REQUEST_BYTES), INCARNATION) == request


@pytest.mark.parametrize(
    "messages",
    [
        [],
        {},
        [{"role": "tool", "content": "untrusted"}],
        [{"role": "user", "content": 1}],
        [{"role": "user", "content": "x", "grant": True}],
        [{"role": "user", "content": "\ud800"}],
        [{"role": "user", "content": "x" * (ipc.MAX_MESSAGES_BYTES + 1)}],
        [{"role": "user", "content": "x"}] * (ipc.MAX_MESSAGES + 1),
    ],
)
def test_bad_messages_are_refused_without_a_request(messages):
    with pytest.raises(ValueError):
        ipc.request_message("generate", INCARNATION, NONCE, messages)


def test_probe_and_generate_have_distinct_exact_shapes():
    assert ipc.parse_request(ipc.request_message("probe", INCARNATION, NONCE), INCARNATION)
    for value in [
        {"version": True, "kind": "probe", "incarnation": INCARNATION, "nonce": NONCE},
        {"version": 1, "kind": [], "incarnation": INCARNATION, "nonce": NONCE},
        {"version": 1, "kind": "probe", "incarnation": INCARNATION, "nonce": "c"},
        {"version": 1, "kind": "probe", "incarnation": INCARNATION, "nonce": NONCE, "messages": []},
    ]:
        with pytest.raises(ValueError):
            ipc.parse_request(value, INCARNATION)


def event(kind, **fields):
    return ipc.event_message(kind, PID, INCARNATION, NONCE, seq=0, **fields)


def parse_event(value, **fields):
    return ipc.parse_event(
        value, PID, INCARNATION, NONCE, seq=0, max_input_tokens=2048, max_new_tokens=128, **fields
    )


def test_deltas_and_terminals_have_distinct_sequence_and_token_bounds():
    assert parse_event(event("delta", text="Public 🌙"))["kind"] == "delta"
    for reason, count in [("eos", 35), ("eos", 128), ("length", 128)]:
        assert (
            parse_event(event("done", finish_reason=reason, input_tokens=73, output_tokens=count))[
                "finish_reason"
            ]
            == reason
        )
    assert parse_event(event("error", code="input_limit"))["kind"] == "error"
    for value in [
        event("delta", text=""),
        event("delta", text="x" * (ipc.MAX_DELTA_BYTES + 1)),
        event("delta", text="\ud800"),
        event("delta", text="public", code="input_limit"),
        event("done", finish_reason="eof", input_tokens=73, output_tokens=35),
        event("done", finish_reason="length", input_tokens=73, output_tokens=35),
        event("done", finish_reason="eos", input_tokens=True, output_tokens=35),
        event("done", finish_reason="eos", input_tokens=2049, output_tokens=35),
        event("done", finish_reason="eos", input_tokens=73, output_tokens=129),
        event("error", code="raw private exception"),
        event("ready"),
        {**event("delta", text="public"), "pid": PID + 1},
        {**event("delta", text="public"), "nonce": "c" * 64},
        {**event("delta", text="public"), "seq": True},
        {**event("delta", text="public"), "seq": 1},
    ]:
        with pytest.raises(ValueError):
            parse_event(value)


def test_probe_is_not_a_generation_terminal_and_untrusted_json_is_refused():
    ready = ipc.event_message("ready", PID, INCARNATION, NONCE)
    assert ipc.parse_event(ready, PID, INCARNATION, NONCE)["kind"] == "ready"
    with pytest.raises(ValueError):
        parse_event(ready)
    for raw in [b'{"kind":"delta","kind":"done"}', b'{"token":NaN}', b"[]"]:
        with pytest.raises(ValueError):
            ipc.decode_json(raw, ipc.MAX_FRAME_BYTES)
