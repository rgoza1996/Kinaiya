"""Bounded local Qwen dialogue protocol; importing it loads no model runtime."""

import hashlib
import json
from pathlib import Path

from packages.providers.whistle_ipc import (
    decode_json,
    encode_json,
    peer_pid,
    private_directory,
    read_frame,
    receive_frame,
    send_frame,
    socket_path,
    token,
    write_frame,
)

# Reuse only the established framing/peer primitives. Whistle's wire shapes
# and limits remain unchanged; every Qwen caller passes its explicit bound.
VERSION = 1
MANIFEST_PATH = Path(__file__).with_name("qwen3_manifest.json")
_manifest_bytes = MANIFEST_PATH.read_bytes()
MANIFEST = json.loads(_manifest_bytes)
MANIFEST_SHA256 = hashlib.sha256(_manifest_bytes).hexdigest()
PROFILE = MANIFEST["profile"]
MODEL = MANIFEST["model"]
REVISION = MANIFEST["revision"]
MAX_REQUEST_BYTES = 512 * 1024
MAX_FRAME_BYTES = 16 * 1024
MAX_MESSAGES = 32
MAX_MESSAGES_BYTES = 128 * 1024
MAX_DELTA_BYTES = 8192
MAX_TEXT_BYTES = 32 * 1024
MAX_EVENTS = 4096
ERROR_CODES = {"input_limit", "output_limit", "generation_failed", "invalid_request"}

__all__ = [
    "decode_json",
    "encode_json",
    "peer_pid",
    "private_directory",
    "read_frame",
    "receive_frame",
    "send_frame",
    "socket_path",
    "token",
    "write_frame",
]


def snapshot_path(directory, pid):
    """Fixed child snapshot name; this path is never process/cleanup authority."""
    if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
        raise ValueError("Invalid Qwen snapshot PID")
    return Path(directory) / f"model-{pid}"


def validate_limits(max_input_tokens, max_new_tokens):
    if (
        type(max_input_tokens) is not int
        or not 64 <= max_input_tokens <= 4096
        or type(max_new_tokens) is not int
        or not 16 <= max_new_tokens <= 512
    ):
        raise ValueError("Invalid bounded Qwen token limits")
    return {"max_input_tokens": max_input_tokens, "max_new_tokens": max_new_tokens}


def validate_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("Qwen requires bounded dialogue messages")
    total, result = 0, []
    for row in messages:
        if (
            not isinstance(row, dict)
            or set(row) != {"role", "content"}
            or not isinstance(row["role"], str)
            or row["role"] not in {"system", "user", "assistant"}
            or not isinstance(row["content"], str)
        ):
            raise ValueError("Invalid Qwen dialogue message")
        total += len(row["content"].encode("utf-8"))
        if total > MAX_MESSAGES_BYTES:
            raise ValueError("Qwen dialogue exceeds its byte bound")
        result.append(dict(row))
    return result


def _identity(value, pid, incarnation, nonce=None):
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != VERSION
        or type(value.get("pid")) is not int
        or value["pid"] != pid
        or not token(incarnation)
        or value.get("incarnation") != incarnation
        or (nonce is not None and (not token(nonce) or value.get("nonce") != nonce))
    ):
        raise ValueError("Qwen response identity mismatch")


def parse_hello(value, pid, *, max_input_tokens, max_new_tokens):
    limits = validate_limits(max_input_tokens, max_new_tokens)
    if not isinstance(value, dict):
        raise ValueError("Invalid Qwen hello")
    _identity(value, pid, value.get("incarnation"))
    runtime = value.get("runtime")
    valid_runtime = (
        isinstance(runtime, dict)
        and set(runtime) == set(MANIFEST["runtime"])
        and all(
            type(runtime[key]) is type(expected) and runtime[key] == expected
            for key, expected in MANIFEST["runtime"].items()
        )
    )
    expected = {
        "version",
        "kind",
        "pid",
        "incarnation",
        "profile",
        "model",
        "revision",
        "manifest_sha256",
        "runtime",
        "isolation",
        "input_mode",
        *limits,
    }
    if (
        set(value) != expected
        or value["kind"] != "hello"
        or value["profile"] != PROFILE
        or value["model"] != MODEL
        or value["revision"] != REVISION
        or value["manifest_sha256"] != MANIFEST_SHA256
        or not valid_runtime
        or value["isolation"] != "seccomp-network-v1"
        or value["input_mode"] != "sealed-safetensors-v1"
        or any(type(value[key]) is not int or value[key] != count for key, count in limits.items())
    ):
        raise ValueError("Qwen readiness profile mismatch")
    return value["incarnation"]


def request_message(kind, incarnation, nonce, messages=None):
    value = {"version": VERSION, "kind": kind, "incarnation": incarnation, "nonce": nonce}
    if messages is not None:
        value["messages"] = messages
    return parse_request(value, incarnation)


def parse_request(value, incarnation):
    if not isinstance(value, dict):
        raise ValueError("Invalid Qwen request")
    kind = value.get("kind")
    if (
        not isinstance(kind, str)
        or kind not in {"probe", "generate"}
        or set(value)
        != {"version", "kind", "incarnation", "nonce"}
        | ({"messages"} if kind == "generate" else set())
        or type(value["version"]) is not int
        or value["version"] != VERSION
        or not token(incarnation)
        or value["incarnation"] != incarnation
        or not token(value["nonce"])
    ):
        raise ValueError("Qwen request identity mismatch")
    result = dict(value)
    if kind == "generate":
        result["messages"] = validate_messages(value["messages"])
    return result


def event_message(kind, pid, incarnation, nonce, *, seq=None, **payload):
    value = {
        "version": VERSION,
        "kind": kind,
        "pid": pid,
        "incarnation": incarnation,
        "nonce": nonce,
        **payload,
    }
    if seq is not None:
        value["seq"] = seq
    return value


def parse_event(
    value, pid, incarnation, nonce, *, seq=None, max_input_tokens=4096, max_new_tokens=512
):
    _identity(value, pid, incarnation, nonce)
    base = {"version", "kind", "pid", "incarnation", "nonce"}
    kind = value.get("kind")
    if seq is None:
        if set(value) != base or kind != "ready":
            raise ValueError("Invalid Qwen probe response")
        return value
    validate_limits(max_input_tokens, max_new_tokens)
    if (
        type(seq) is not int
        or not 0 <= seq <= MAX_EVENTS
        or type(value.get("seq")) is not int
        or value["seq"] != seq
        or not isinstance(kind, str)
        or kind not in {"delta", "done", "error"}
    ):
        raise ValueError("Invalid Qwen stream sequence")
    base.add("seq")
    if kind == "delta":
        if (
            set(value) != base | {"text"}
            or not isinstance(value["text"], str)
            or not 1 <= len(value["text"].encode("utf-8")) <= MAX_DELTA_BYTES
            or seq == MAX_EVENTS
        ):
            raise ValueError("Invalid bounded Qwen text delta")
    elif kind == "done":
        if (
            set(value) != base | {"finish_reason", "input_tokens", "output_tokens"}
            or not isinstance(value["finish_reason"], str)
            or value["finish_reason"] not in {"eos", "length"}
            or type(value["input_tokens"]) is not int
            or not 1 <= value["input_tokens"] <= max_input_tokens
            or type(value["output_tokens"]) is not int
            or not 1 <= value["output_tokens"] <= max_new_tokens
            or (value["finish_reason"] == "length" and value["output_tokens"] != max_new_tokens)
        ):
            raise ValueError("Invalid Qwen terminal response")
    elif (
        set(value) != base | {"code"}
        or not isinstance(value["code"], str)
        or value["code"] not in ERROR_CODES
    ):
        raise ValueError("Invalid Qwen error response")
    return value
