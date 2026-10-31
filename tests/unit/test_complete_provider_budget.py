"""Exact complete-request admissions through actual Meta, offline HTTP only."""

import asyncio
import copy
import json

import httpx
import pytest

from packages.contracts.agent_tools import (
    agent_export_permitted,
    agent_export_scope,
    agent_tool_scope,
    capture_agent_export_check,
    native_tool_snapshot,
)
from packages.providers.meta_action_wire import encode_action
from packages.providers.prompt_budget import (
    PromptCountUnavailable,
    PromptTokenCount,
    binding_digest,
    request_digest,
)
from packages.providers.text import (
    CompatibleTextProvider,
    MetaResponsesProvider,
    ProviderCleanupError,
    ProviderError,
    ProviderRefusal,
    ScriptedProvider,
)

MESSAGES = [{"role": "user", "content": "Public synthetic example: complete 🌿."}]
LOGICAL = {
    "tool": "finish",
    "arguments": {"summary": "Synthetic proposal", "objective_revision": 1},
}
COUNT = b'{"object":"response.input_tokens","input_tokens":123}'


def generation(body):
    action = encode_action(LOGICAL) if body["text"]["format"]["name"].endswith("v2") else LOGICAL
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(action)}],
                }
            ],
        },
    )


def provider(handler=None, **kwargs):
    calls = []

    async def send(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body, request.content))
        if handler is not None:
            return await handler(request, body)
        return (
            httpx.Response(200, content=COUNT)
            if request.url.path.endswith("input_tokens")
            else generation(body)
        )

    options = dict(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-key",
        allow_cloud=True,
        context_tokens=1007997,
        max_tokens=8192,
        structured_actions=True,
        reasoning_effort="xhigh",
        transport=httpx.MockTransport(send),
    )
    options.update(kwargs)
    return MetaResponsesProvider(**options), calls


class Body(httpx.AsyncByteStream):
    def __init__(self, raw=COUNT, *, held=False, fail=False):
        self.raw, self.fail, self.closed = raw, fail, 0
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        if not held:
            self.release.set()

    async def __aiter__(self):
        for index in range(0, len(self.raw), 7):
            yield self.raw[index : index + 7]

    async def aclose(self):
        self.closed += 1
        self.closing.set()
        await self.release.wait()
        if self.fail:
            raise OSError("untrusted synthetic exception text")


@pytest.mark.parametrize("action_format", ["flat-v1", "typed-v2"])
async def test_exact_complete_body_8192_and_single_use(action_format):
    p, calls = provider(action_format=action_format)
    snapshot = native_tool_snapshot(artifacts_available=True, catalog_available=True)
    try:
        with agent_tool_scope(snapshot):
            admission = await p.prepare_complete(MESSAGES)
            assert not admission.fixture and len(calls) == 1
            assert admission.measurement.reserved_output_tokens == 8192
            assert admission.measurement.request_sha256 == request_digest(calls[0][1])
            assert json.loads(await admission.complete()) == LOGICAL
            with pytest.raises(PromptCountUnavailable):
                await admission.complete()
        assert [path for path, _, _ in calls] == ["/v1/responses/input_tokens", "/v1/responses"]
        assert calls[0][2] == calls[1][2]
        assert calls[0][1]["stream"] is False
        assert calls[0][1]["max_output_tokens"] == 8192
        assert calls[0][1]["reasoning"] == {"effort": "xhigh"}
        assert not p._measurement_required and p._measurement is None and p._count_generation == 0
    finally:
        await p.close()


@pytest.mark.parametrize("action_format", ["flat-v1", "typed-v2"])
async def test_concurrent_different_snapshots_do_not_invalidate_each_other(action_format):
    entered = asyncio.Event()
    counted = set()

    async def send(request, body):
        if request.url.path.endswith("input_tokens"):
            counted.add(body["input"][-1]["content"])
            if len(counted) == 2:
                entered.set()
            await entered.wait()
            return httpx.Response(200, content=COUNT)
        return generation(body)

    p, calls = provider(send, action_format=action_format)
    snapshots = [native_tool_snapshot(), native_tool_snapshot(artifacts_available=True)]

    async def run(index):
        with agent_tool_scope(snapshots[index]):
            admission = await p.prepare_complete([{"role": "user", "content": f"Public {index}"}])
            return await admission.complete()

    try:
        async with asyncio.timeout(2):
            assert all(
                json.loads(value) == LOGICAL for value in await asyncio.gather(run(0), run(1))
            )
        assert len(calls) == 4
        for index in range(2):
            selected = [
                body for _, body, _ in calls if body["input"][-1]["content"] == f"Public {index}"
            ]
            assert len(selected) == 2 and selected[0] == selected[1]
            schema = selected[0]["text"]["format"]["schema"]
            assert ("publish_ui" in json.dumps(schema)) == bool(index)
    finally:
        await p.close()


async def test_inherited_scope_child_cannot_consume_parent_admission():
    p, calls = provider()
    try:
        with agent_tool_scope(native_tool_snapshot()):
            admission = await p.prepare_complete(MESSAGES)
            with pytest.raises(PromptCountUnavailable):
                await asyncio.create_task(admission.complete())
            with pytest.raises(PromptCountUnavailable):
                await admission.complete()
        assert len(calls) == 1
    finally:
        await p.close()


@pytest.mark.parametrize(
    "change",
    [
        "messages",
        "model",
        "output",
        "context",
        "context_type",
        "effort",
        "format",
        "structured",
        "headers",
        "origin",
        "client",
        "transport",
        "redirects",
        "timeout",
        "cookies",
        "hooks",
        "params",
        "auth",
        "mounts",
        "info",
        "snapshot",
        "closed",
    ],
)
async def test_stale_prepared_request_never_generates_and_stays_consumed(change):
    p, calls = provider()
    old_client = p.http
    messages = copy.deepcopy(MESSAGES)
    snapshot = native_tool_snapshot()
    try:
        with agent_tool_scope(snapshot):
            admission = await p.prepare_complete(messages)
            if change == "messages":
                messages[0]["content"] = "Changed public input"
            elif change == "model":
                p.model = "replacement"
            elif change == "output":
                p.max_tokens = 128
            elif change == "context":
                p.context_tokens = 999999
            elif change == "context_type":
                p.context_tokens = float(p.context_tokens)
            elif change == "effort":
                p.effort = "minimal"
            elif change == "format":
                p.action_format = "typed-v2"
            elif change == "structured":
                p.structured_actions = False
            elif change == "headers":
                p.http.headers["Authorization"] = "Bearer another-synthetic-key"
            elif change == "origin":
                p.http.base_url = "https://changed.invalid/v1/"
            elif change == "client":
                p.http = httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _: pytest.fail("replacement client exported")
                    )
                )
            elif change == "transport":
                p.http._transport = httpx.MockTransport(
                    lambda _: pytest.fail("replacement transport exported")
                )
            elif change == "redirects":
                p.http.follow_redirects = True
            elif change == "timeout":
                p.http.timeout = 1
            elif change == "cookies":
                p.http.cookies.set("synthetic-cookie", "changed")
            elif change == "hooks":

                async def hook(_request):
                    pytest.fail("new hook must not run")

                p.http.event_hooks["request"].append(hook)
            elif change == "params":
                p.http.params = {"changed": "true"}
            elif change == "auth":
                p.http.auth = httpx.BasicAuth("synthetic", "synthetic")
            elif change == "mounts":
                p.http._mounts = {
                    object(): httpx.MockTransport(lambda _: pytest.fail("new mount exported"))
                }
            elif change == "info":
                p.info = p.info.model_copy(update={"model": "replacement"})
            elif change == "closed":
                await p.close()
            if change == "snapshot":
                with (
                    agent_tool_scope(native_tool_snapshot(artifacts_available=True)),
                    pytest.raises(PromptCountUnavailable),
                ):
                    await admission.complete()
            else:
                with pytest.raises(PromptCountUnavailable):
                    await admission.complete()
            with pytest.raises(PromptCountUnavailable):
                await admission.complete()
        assert len(calls) == 1
    finally:
        await p.close()
        if old_client is not p.http:
            await old_client.aclose()


@pytest.mark.parametrize("which", ["captured", "active"])
async def test_captured_and_fresh_disclosure_guards_both_apply(which):
    allowed = True
    p, calls = provider()
    try:
        with agent_export_scope(lambda: allowed):
            admission = await p.prepare_complete(MESSAGES)
        if which == "captured":
            allowed = False
        with agent_export_scope(lambda: which != "active"), pytest.raises(ProviderRefusal):
            await admission.complete()
        assert len(calls) == 1 and agent_export_permitted()
    finally:
        await p.close()


async def test_disclosure_denial_before_count_makes_no_transport_call():
    p, calls = provider()
    try:
        with agent_export_scope(lambda: False), pytest.raises(ProviderRefusal):
            await p.prepare_complete(MESSAGES)
        assert not calls
    finally:
        await p.close()


@pytest.mark.parametrize("change", ["body", "disclosure", "headers"])
async def test_actual_capture_await_cannot_change_counted_request(monkeypatch, change):
    from packages.providers import decision_capture

    p, calls = provider()
    allowed = True

    async def capture(_provider, body, _format):
        nonlocal allowed
        if change == "body":
            body["input"][-1]["content"] = "changed after count"
        elif change == "disclosure":
            allowed = False
        else:
            p.http.headers["authorization"] = "Bearer replacement"

    monkeypatch.setattr(decision_capture, "capture_request", capture)
    try:
        with agent_export_scope(lambda: allowed):
            admission = await p.prepare_complete(MESSAGES)
            with pytest.raises((PromptCountUnavailable, ProviderRefusal)):
                await admission.complete()
        assert len(calls) == 1
    finally:
        await p.close()


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'"primitive"',
        b'{"object":"response.input_tokens","input_tokens":true}',
        b'{"object":"response.input_tokens","input_tokens":1,"input_tokens":2}',
        b'{"object":"response.input_tokens","input_tokens":NaN}',
        b"\xff",
        b" " * 4097,
    ],
)
async def test_invalid_complete_count_is_closed_and_never_admitted(raw):
    stream = Body(raw)

    async def send(_request, _body):
        return httpx.Response(200, stream=stream)

    p, calls = provider(send)
    try:
        with pytest.raises(PromptCountUnavailable):
            await p.prepare_complete(MESSAGES)
        assert len(calls) == 1 and stream.closed == 1
    finally:
        await p.close()


async def test_count_cancel_retains_real_close_and_returns_no_admission():
    stream = Body(held=True)

    async def send(_request, _body):
        return httpx.Response(200, stream=stream)

    p, calls = provider(send)
    task = asyncio.create_task(p.prepare_complete(MESSAGES))
    try:
        async with asyncio.timeout(2):
            await stream.closing.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and stream.closed == 1
            stream.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == 1 and p._count_generation == 0
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.close()


async def test_count_cleanup_failure_does_not_invalidate_another_local_admission():
    failing = Body(fail=True)
    attempts = 0

    async def send(request, body):
        nonlocal attempts
        if request.url.path.endswith("input_tokens"):
            attempts += 1
            return (
                httpx.Response(200, stream=failing)
                if attempts == 2
                else httpx.Response(200, content=COUNT)
            )
        return generation(body)

    p, calls = provider(send)
    try:
        first = await p.prepare_complete(MESSAGES)
        with pytest.raises(ProviderCleanupError):
            await p.prepare_complete(MESSAGES)
        assert json.loads(await first.complete()) == LOGICAL
        assert len(calls) == 3 and failing.closed == 1
    finally:
        await p.close()


@pytest.mark.parametrize(
    "change", ["messages", "context", "client", "headers", "structured", "disclosure"]
)
async def test_final_count_close_await_revalidates_native_request(change):
    stream = Body(held=True)
    messages = copy.deepcopy(MESSAGES)
    allowed = True
    snapshot = native_tool_snapshot()

    async def send(_request, _body):
        return httpx.Response(200, stream=stream)

    p, calls = provider(send)
    old_client = p.http

    async def prepare():
        with agent_tool_scope(snapshot, export_guard=lambda: allowed):
            return await p.prepare_complete(messages)

    task = asyncio.create_task(prepare())
    try:
        async with asyncio.timeout(2):
            await stream.closing.wait()
            if change == "messages":
                messages[0]["content"] = "Changed during real close"
            elif change == "context":
                p.context_tokens = 999999
            elif change == "client":
                p.http = httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda _: pytest.fail("replacement exported"))
                )
            elif change == "headers":
                p.http.headers["Authorization"] = "Bearer changed"
            elif change == "structured":
                p.structured_actions = False
            else:
                allowed = False
            stream.release.set()
            with pytest.raises((PromptCountUnavailable, ProviderRefusal)):
                await task
        assert len(calls) == 1 and stream.closed == 1
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.close()
        if old_client is not p.http:
            await old_client.aclose()


async def test_agent_preparation_and_generation_leave_converse_ticket_intact():
    async def send(request, body):
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, content=COUNT)
        if body["stream"]:
            return httpx.Response(
                200,
                text='data: {"type":"response.output_text.delta","delta":"Legacy reply"}\n\ndata: {"type":"response.completed"}\n\n',
            )
        return generation(body)

    p, calls = provider(send)
    try:
        measured = await p.measure_prompt(MESSAGES)
        legacy_ticket, generation_number = p._measurement, p._count_generation
        complete = await p.prepare_complete(MESSAGES)
        assert json.loads(await complete.complete()) == LOGICAL
        assert p._measurement is legacy_ticket and p._count_generation == generation_number
        assert [part async for part in p.stream(MESSAGES)] == ["Legacy reply"]
        assert measured.reserved_output_tokens == 8192
        assert [body["stream"] for _, body, _ in calls] == [True, False, False, True]
    finally:
        await p.close()


async def test_generation_failure_is_consumed_without_retry():
    async def send(request, _body):
        return (
            httpx.Response(200, content=COUNT)
            if request.url.path.endswith("input_tokens")
            else httpx.Response(503, json={"error": "synthetic unavailable"})
        )

    p, calls = provider(send)
    try:
        admitted = await p.prepare_complete(MESSAGES)
        with pytest.raises(ProviderError, match="Meta HTTP 503"):
            await admitted.complete()
        with pytest.raises(PromptCountUnavailable):
            await admitted.complete()
        assert len(calls) == 2
    finally:
        await p.close()


@pytest.mark.parametrize("fail_close", [False, True])
async def test_generation_cancel_or_close_failure_consumes_callback(fail_close):
    stream = Body(b'{"status":"completed","output":[]}', held=not fail_close, fail=fail_close)
    holder = []

    async def send(request, _body):
        return (
            httpx.Response(200, content=COUNT)
            if request.url.path.endswith("input_tokens")
            else httpx.Response(200, stream=stream)
        )

    p, calls = provider(send)

    async def run():
        admitted = await p.prepare_complete(MESSAGES)
        holder.append(admitted)
        return await admitted.complete()

    task = asyncio.create_task(run())
    try:
        async with asyncio.timeout(2):
            await stream.closing.wait()
            if fail_close:
                with pytest.raises(ProviderCleanupError):
                    await task
            else:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                stream.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        with pytest.raises(PromptCountUnavailable):
            await holder[0].complete()
        assert stream.closed == 1 and len(calls) == 2
    finally:
        stream.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.close()


async def test_overflow_discard_and_unconfigured_context_never_generate():
    p, calls = provider(context_tokens=8193)
    try:
        admitted = await p.prepare_complete(MESSAGES)
        assert not admitted.measurement.fits
        with pytest.raises(PromptCountUnavailable):
            await admitted.complete()
        p.context_tokens = 1007997
        admitted = await p.prepare_complete(MESSAGES)
        admitted.discard()
        with pytest.raises(PromptCountUnavailable):
            await admitted.complete()
        p.context_tokens = None
        with pytest.raises(PromptCountUnavailable):
            await p.prepare_complete(MESSAGES)
        assert len(calls) == 2
    finally:
        await p.close()


async def test_scripted_prepare_does_not_consume_fixture_or_fake_measurement():
    p = ScriptedProvider(["fixture response"])
    admitted = await p.prepare_complete(MESSAGES)
    assert admitted.fixture and admitted.measurement is None
    assert not p.calls and len(p.responses) == 1
    assert await admitted.complete() == "fixture response"
    assert p.calls == [MESSAGES] and not p.responses
    with pytest.raises(PromptCountUnavailable):
        await admitted.complete()


async def test_scripted_bad_mutation_has_a_constant_consumed_failure():
    p = ScriptedProvider(["must remain unused"])
    messages = copy.deepcopy(MESSAGES)
    admitted = await p.prepare_complete(messages)
    messages[0]["content"] = object()
    with pytest.raises(PromptCountUnavailable, match="^Fixture complete admission changed$"):
        await admitted.complete()
    assert not p.calls and len(p.responses) == 1
    with pytest.raises(PromptCountUnavailable):
        await admitted.complete()


def test_native_export_scope_composes_and_resets_without_disclosing_exceptions():
    assert agent_export_permitted()
    with agent_export_scope(lambda: False):
        retained = capture_agent_export_check()
        with agent_export_scope(lambda: True):
            assert not agent_export_permitted()
    assert agent_export_permitted() and not retained()
    with pytest.raises(ValueError):
        with agent_export_scope(True):
            pass


def test_shared_reservation_8192_keeps_existing_wire_identity_and_local_boundaries():
    body = {"messages": MESSAGES, "stream": False, "max_tokens": 8192}
    binding = {
        "provider": "synthetic",
        "model": "fixture",
        "profile": "fixture",
        "manifest_sha256": "a" * 64,
        "context_tokens": 1007997,
        "max_new_tokens": 8192,
    }
    count = PromptTokenCount(123, 1007997, 8192, request_digest(body), binding_digest(binding))
    assert (
        count.fits
        and PromptTokenCount.from_wire(count.to_wire(), body=body, binding=binding) == count
    )
    assert set(count.to_wire()) == {
        "version",
        "input_tokens",
        "context_tokens",
        "reserved_output_tokens",
        "request_sha256",
        "binding_sha256",
    }
    from packages.providers.gguf import ManagedGGUFProvider
    from packages.providers.lmstudio import LMStudioProvider

    with pytest.raises(ValueError, match="token limits"):
        ManagedGGUFProvider(
            "http://127.0.0.1:1",
            "synthetic",
            model="fixture",
            alias="fixture",
            profile="fixture",
            manifest_sha256="a" * 64,
            context_tokens=32768,
            max_new_tokens=8192,
        )
    with pytest.raises(ProviderError, match="Unsupported LM Studio"):
        LMStudioProvider("http://127.0.0.1:1/v1", "synthetic", max_tokens=8192)


@pytest.mark.parametrize("value", [True, 15, 65537, 8192.0])
def test_explicit_meta_output_bound_remains_strict(value):
    with pytest.raises(ProviderError):
        provider(max_tokens=value)


async def test_generic_compatible_has_no_guessed_complete_count_capability():
    p = CompatibleTextProvider(
        "http://127.0.0.1:1/v1",
        "synthetic",
        transport=httpx.MockTransport(lambda _: pytest.fail("unsupported provider exported")),
    )
    try:
        assert not callable(getattr(p, "prepare_complete", None))
    finally:
        await p.close()
