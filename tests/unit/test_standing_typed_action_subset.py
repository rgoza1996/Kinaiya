"""Independent typed standing-subset oracles; real adapter, offline transport."""

import asyncio
import copy
import hashlib
import json

import httpx
import pytest
from jsonschema import Draft202012Validator
from test_standing_export_contract import initial_vector, request_vector

from packages.contracts.agent_tools import (
    NativeToolSnapshot,
    agent_tool_scope,
    current_agent_tools,
    native_tool_snapshot,
)
from packages.contracts.standing_export import StandingExportRequest
from packages.providers.meta_action_wire import TypedWireError, action_schema, decode_action
from packages.providers.text import MetaResponsesProvider

FILES = ("read_file", "write_file", "finish")
MESSAGES = [{"role": "user", "content": "Public synthetic native-files fixture."}]


def snapshot(*, masked=False):
    return NativeToolSnapshot(
        ("read_file", "finish") if masked else FILES,
        task_subset="native-files-v1",
        owner_disabled=("write_file",) if masked else None,
        owner_revision=7 if masked else None,
    )


def envelope(tool):
    arguments = {
        "read_file": {"path": "public.txt", "max_bytes": None},
        "write_file": {
            "path": "public.txt",
            "content": "Public synthetic é",
            "objective_revision": 1,
        },
        "finish": {"summary": "Public synthetic inspection complete.", "objective_revision": 1},
        # This must remain opaque when the known operation is unavailable.
        "shell": {"malformed": ["never interpret or execute"], "objective_revision": True},
    }
    return {"action": {"tool": tool, "arguments": copy.deepcopy(arguments[tool])}}


def advertised_tools(schema):
    """Inspect the emitted JSON Schema independently of its ref/union layout."""
    pending, names = [schema], set()
    while pending:
        value = pending.pop()
        if type(value) is dict:
            tool = value.get("properties", {}).get("tool")
            if type(tool) is dict:
                if "const" in tool:
                    names.add(tool["const"])
                names.update(tool.get("enum", []))
            pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)
    return names


@pytest.mark.parametrize("tool", ("write_file", "finish"))
def test_bound_files_schema_and_decoder_accept_exact_typed_actions(tool):
    selected = snapshot()
    wire = envelope(tool)
    saved = copy.deepcopy(wire)
    with agent_tool_scope(selected):
        schema = action_schema(selected.available_tools)
        Draft202012Validator.check_schema(schema)
        assert advertised_tools(schema) == set(FILES)
        Draft202012Validator(schema).validate(wire)
        assert decode_action(wire, selected.available_tools) == saved["action"]
    assert wire == saved and current_agent_tools() is None


def test_owner_write_mask_stays_distinct_and_cannot_be_restored_by_formatter():
    selected = snapshot(masked=True)
    with agent_tool_scope(selected):
        schema = action_schema(selected.available_tools)
        assert advertised_tools(schema) == {"read_file", "finish"}
        assert not Draft202012Validator(schema).is_valid(envelope("write_file"))
        assert decode_action(envelope("write_file"), selected.available_tools) == {
            "tool": "write_file",
            "arguments": {},
        }
    assert selected.owner_disabled == ("write_file",) and selected.owner_revision == 7
    assert selected.task_subset == "native-files-v1"


def test_known_unavailable_shell_is_only_a_membership_refusal_envelope():
    selected = snapshot()
    wire = envelope("shell")
    saved = copy.deepcopy(wire)
    with agent_tool_scope(selected):
        assert decode_action(wire, selected.available_tools) == {"tool": "shell", "arguments": {}}
        assert not Draft202012Validator(action_schema(selected.available_tools)).is_valid(wire)
    assert wire == saved


@pytest.mark.parametrize("surface", ("schema", "decode"))
def test_bound_context_refuses_a_different_otherwise_valid_legacy_tuple(surface):
    legacy = native_tool_snapshot().available_tools
    with agent_tool_scope(snapshot()), pytest.raises(TypedWireError):
        if surface == "schema":
            action_schema(legacy)
        else:
            decode_action(envelope("finish"), legacy)


@pytest.mark.parametrize("surface", ("schema", "decode"))
def test_unbound_three_names_do_not_invent_a_native_task_restriction(surface):
    assert current_agent_tools() is None
    with pytest.raises(TypedWireError):
        if surface == "schema":
            action_schema(FILES)
        else:
            decode_action(envelope("finish"), FILES)


@pytest.mark.parametrize(
    "field,value", (("task_subset", "unknown-subset"), ("owner_revision", True))
)
def test_actual_frozen_snapshot_fields_are_revalidated_instead_of_trusted(field, value):
    forged = snapshot(masked=True)
    object.__setattr__(forged, field, value)
    with agent_tool_scope(forged):
        with pytest.raises(TypedWireError):
            action_schema(forged.available_tools)
        with pytest.raises(TypedWireError):
            decode_action(envelope("finish"), forged.available_tools)


def test_unbound_original_six_tool_grammar_and_typed_finish_remain_valid():
    legacy = native_tool_snapshot().available_tools
    assert legacy == ("read_file", "write_file", "search", "shell", "read_transcript", "finish")
    schema = action_schema(legacy)
    assert advertised_tools(schema) == set(legacy)
    Draft202012Validator(schema).validate(envelope("finish"))
    assert decode_action(envelope("finish"), legacy) == envelope("finish")["action"]


class ObservedBody(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw, self.close_calls = raw, 0

    async def __aiter__(self):
        for offset in range(0, len(self.raw), 19):
            yield self.raw[offset : offset + 19]

    async def aclose(self):
        self.close_calls += 1


@pytest.mark.parametrize(
    "tool,masked",
    (("write_file", False), ("finish", False), ("shell", False), ("write_file", True)),
)
async def test_actual_meta_measured_count_generation_and_decode_keep_the_native_subset(
    tool, masked
):
    selected, owner = snapshot(masked=masked), asyncio.current_task()
    calls, admissions, bodies = [], [], []
    initial = None

    async def admit(purpose, digest):
        assert asyncio.current_task() is owner and current_agent_tools() is selected
        original = StandingExportRequest.model_validate(
            request_vector(
                initial=initial,
                export_changes={
                    "purpose": purpose,
                    "request_sha256": digest,
                    "request_nonce": f"typed-subset-{len(admissions)}",
                    "provider": provider.info.model_dump(mode="json"),
                },
            )
        )
        # This spies on the existing native callback interface and full original
        # binding. A synthetic pure proof is not claimed as a native SQL ACK.
        admissions.append(original)

    async def send(request):
        assert asyncio.current_task() is owner and current_agent_tools() is selected
        assert admissions
        original = admissions[-1]
        expected_purpose = (
            "complete_count" if request.url.path.endswith("input_tokens") else "complete_generation"
        )
        assert original.export.purpose == expected_purpose
        assert original.export.request_sha256 == hashlib.sha256(request.content).hexdigest()
        assert (
            original.initial.canonical_bytes()
            == StandingExportRequest.model_validate(
                request_vector(initial=initial)
            ).initial.canonical_bytes()
        )
        body = json.loads(request.content)
        schema = body["text"]["format"]["schema"]
        assert body["text"]["format"]["name"] == "kinaiya_tool_action_v2"
        assert advertised_tools(schema) == set(selected.available_tools)
        assert body["stream"] is False
        assert Draft202012Validator(schema).is_valid(envelope(tool)) is (
            tool in selected.available_tools
        )
        calls.append((request.url.path, request.content, owner))
        payload = (
            {"object": "response.input_tokens", "input_tokens": 123}
            if expected_purpose == "complete_count"
            else {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(envelope(tool))}],
                    }
                ],
            }
        )
        stream = ObservedBody(json.dumps(payload).encode("utf-8"))
        bodies.append(stream)
        return httpx.Response(200, stream=stream)

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-placeholder",
        model="synthetic-typed-meta",
        allow_cloud=True,
        structured_actions=True,
        action_format="typed-v2",
        reasoning_effort="xhigh",
        context_tokens=32768,
        max_tokens=8192,
        transport=httpx.MockTransport(send),
    )
    pin = copy.deepcopy(initial_vector()["claim"]["request"]["task"]["model_pin"])
    pin.update(
        provider=provider.info.provider, model=provider.info.model, local=False, reasoning="xhigh"
    )
    initial = initial_vector(task_changes={"model_pin": pin, "content_scope": "public_synthetic"})
    try:
        with agent_tool_scope(selected, export_admission=admit):
            prepared = await provider.prepare_complete(MESSAGES)
            assert prepared.fixture is False and prepared.measurement.input_tokens == 123
            decoded = json.loads(await prepared.complete())
            expected = (
                envelope(tool)["action"]
                if tool in selected.available_tools
                else {"tool": tool, "arguments": {}}
            )
            assert decoded == expected
        assert [call[0] for call in calls] == ["/v1/responses/input_tokens", "/v1/responses"]
        assert calls[0][1] == calls[1][1] and all(call[2] is owner for call in calls)
        assert [original.export.purpose for original in admissions] == [
            "complete_count",
            "complete_generation",
        ]
        assert all(
            original.export.source_event_ids == [] and original.export.context_event_ids == []
            for original in admissions
        )
        assert len(bodies) == 2 and all(body.close_calls == 1 for body in bodies)
    finally:
        await provider.close()
    assert provider.http.is_closed and current_agent_tools() is None
