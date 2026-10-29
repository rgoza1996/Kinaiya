"""Optional count transport, with synthetic HTTP bodies and durable policy only."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import ProviderInfo
from packages.providers.gguf import ManagedGGUFProvider
from packages.providers.prompt_budget import (
    MAX_COUNT_BYTES,
    MAX_SAFE_COUNT,
    PromptCountUnavailable,
    PromptTokenCount,
    binding_digest,
    request_digest,
)
from packages.providers.qwen3_ipc import MAX_MESSAGES_BYTES, MAX_REQUEST_BYTES
from packages.providers.text import ProviderCleanupError, owned_http_stream_cleanup
from services.resources.providers import ResourceBoundProvider
from services.resources.state import ResourceState

MESSAGES = [
    {"role": "system", "content": "Public synthetic personality 🌙."},
    {"role": "user", "content": "Earlier public question."},
    {"role": "assistant", "content": "Earlier public answer."},
    {"role": "user", "content": "Current public request."},
]
TOKEN = "synthetic-count-converse-credential"
PIN = "a" * 64


class CountBody(httpx.AsyncByteStream):
    def __init__(self, raw, *, held=False, failure=False, stall=False):
        self.raw, self.failure, self.stall = raw, failure, stall
        self.closing, self.release, self.reading = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.closed = 0
        self.cancelled = False
        if not held:
            self.release.set()

    async def __aiter__(self):
        self.reading.set()
        if self.stall:
            await asyncio.Event().wait()
        for start in range(0, len(self.raw), 17):
            yield self.raw[start : start + 17]

    async def aclose(self):
        self.closed += 1
        self.closing.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.failure:
            raise OSError("Synthetic private backend path must not escape")


def provider_for(*, transform=None, raw=None, status=200, headers=None, **settings):
    requests, bodies = [], []
    provider = None

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        value = {
            "version": 1,
            "input_tokens": 1920,
            "context_tokens": 2048,
            "reserved_output_tokens": 128,
            "request_sha256": request_digest(body),
            "binding_sha256": binding_digest(provider.managed_binding),
        }
        if transform is not None:
            value = transform(value, request)
        output = raw if raw is not None else json.dumps(value).encode()
        stream = output if isinstance(output, CountBody) else CountBody(output)
        bodies.append(stream)
        return httpx.Response(status, headers=headers or {}, stream=stream)

    provider = ManagedGGUFProvider(
        "http://127.0.0.1:19990",
        TOKEN,
        model="Synthetic display model",
        alias="synthetic-alias",
        profile="synthetic-cpu",
        manifest_sha256=PIN,
        transport=httpx.MockTransport(respond),
        **settings,
    )
    return provider, requests, bodies


@pytest.mark.parametrize("count,fits", [(1920, True), (1921, False), (MAX_SAFE_COUNT, False)])
async def test_full_generation_body_and_template_inclusive_count_after_real_close(count, fits):
    def change(value, request):
        return {**value, "input_tokens": count}

    provider, calls, bodies = provider_for(transform=change)
    try:
        result = await provider.measure_prompt(MESSAGES)
        assert type(result) is PromptTokenCount and result.input_tokens == count
        assert result.fits is fits and result.reserved_output_tokens == 128
        assert len(calls) == 1 and bodies[0].closed == 1
        request = calls[0]
        assert str(request.url) == "http://127.0.0.1:19990/v1/chat/completions/input_tokens"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "model": "synthetic-alias",
            "messages": MESSAGES,
            "max_tokens": 128,
            "stream": True,
        }
        assert result.request_sha256 == request_digest(json.loads(request.content))
        assert result.binding_sha256 == binding_digest(provider.managed_binding)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers["accept-encoding"] == "identity"
        assert provider.http.follow_redirects is False and provider.http._trust_env is False
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "native_timeout,expected", [(1, 6), (9, 14), (10, 15), (120, 15), (900, 15)]
)
async def test_count_transport_uses_short_native_deadline_plus_grace(native_timeout, expected):
    provider, calls, _ = provider_for(request_timeout_s=native_timeout)
    try:
        await provider.measure_prompt(MESSAGES)
        assert set(calls[0].extensions["timeout"].values()) == {expected}
    finally:
        await provider.close()


@pytest.mark.parametrize("extra", [0, 1])
async def test_count_response_byte_bound_is_inclusive(extra):
    def padding(value, request):
        return value

    provider, calls, bodies = provider_for(transform=padding)
    original = provider.http._transport

    def respond(request):
        response = original.handler(request)
        encoded = response.stream.raw
        stream = CountBody(encoded + b" " * (MAX_COUNT_BYTES + extra - len(encoded)))
        bodies[-1] = stream
        return httpx.Response(200, stream=stream)

    provider.http._transport = httpx.MockTransport(respond)
    try:
        if extra:
            with pytest.raises(PromptCountUnavailable):
                await provider.measure_prompt(MESSAGES)
        else:
            assert (await provider.measure_prompt(MESSAGES)).input_tokens == 1920
        assert len(calls) == 1 and bodies[0].closed == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"input_tokens": 0},
        {"input_tokens": True},
        {"input_tokens": 1.0},
        {"input_tokens": MAX_SAFE_COUNT + 1},
        {"context_tokens": 4096},
        {"context_tokens": 2048.0},
        {"reserved_output_tokens": 129},
        {"reserved_output_tokens": True},
        {"request_sha256": "0" * 64},
        {"binding_sha256": "0" * 64},
        {"request_sha256": "\ud800"},
        {"extra": "private synthetic payload"},
    ],
)
async def test_wire_shape_types_limits_and_both_digests_are_authoritative(change):
    provider, calls, bodies = provider_for(transform=lambda value, _: {**value, **change})
    try:
        with pytest.raises(PromptCountUnavailable) as caught:
            await provider.measure_prompt(MESSAGES)
        assert "private synthetic" not in str(caught.value)
        assert len(calls) == 1 and bodies[0].closed == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"null",
        b"{",
        b"\xff",
        b'{"version":1,"version":1}',
        b'{"input_tokens":NaN}',
        b'{"input_tokens":Infinity}',
        b'{"input_tokens":1e999}',
        b'{"unused":{"x":1,"x":2}}',
        b"[" * 2000 + b"]" * 2000,
    ],
)
async def test_malformed_duplicate_nonfinite_and_non_utf8_wire_refuses(raw):
    provider, calls, bodies = provider_for(raw=raw)
    try:
        with pytest.raises(PromptCountUnavailable):
            await provider.measure_prompt(MESSAGES)
        assert len(calls) == 1 and bodies[0].closed == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("status", [201, 204, 301, 307, 400, 401, 404, 405, 501, 503])
async def test_non_count_status_is_fixed_failure_no_redirect_or_retry(status):
    provider, calls, bodies = provider_for(
        status=status,
        headers={"Location": "http://foreign.invalid/secret"},
        raw=b"private synthetic backend details",
    )
    try:
        with pytest.raises(PromptCountUnavailable) as caught:
            await provider.measure_prompt(MESSAGES)
        assert "private synthetic" not in str(caught.value) and "foreign" not in str(caught.value)
        assert len(calls) == 1 and bodies[0].closed == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "identity,gzip"])
async def test_count_refuses_content_encoding_without_decoding_it(encoding):
    provider, calls, bodies = provider_for(headers={"Content-Encoding": encoding})
    try:
        with pytest.raises(PromptCountUnavailable):
            await provider.measure_prompt(MESSAGES)
        assert len(calls) == 1 and bodies[0].closed == 1
        assert not bodies[0].reading.is_set()
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "tool", "content": "x"}],
        [{"role": "user", "content": "\ud800"}],
        [{"role": "user", "content": "x", "other": True}],
        [{"role": "user", "content": "x"}] * 33,
        [{"role": "user", "content": "é" * (MAX_MESSAGES_BYTES // 2) + "x"}],
    ],
)
async def test_invalid_or_oversize_input_refuses_without_http(messages):
    provider, calls, _ = provider_for()
    try:
        with pytest.raises(PromptCountUnavailable):
            await provider.measure_prompt(messages)
        assert calls == []
    finally:
        await provider.close()


@pytest.mark.parametrize("kind", ["messages", "content", "wire", "wire-plus-one"])
async def test_original_message_and_request_bounds_are_not_silently_truncated(kind):
    provider, calls, _ = provider_for()
    if kind == "messages":
        messages = [{"role": "user", "content": str(i)} for i in range(32)]
    elif kind == "content":
        messages = [{"role": "user", "content": "é" * (MAX_MESSAGES_BYTES // 2)}]
    else:
        empty = {
            "model": "synthetic-alias",
            "messages": [{"role": "user", "content": ""}],
            "max_tokens": 128,
            "stream": True,
        }
        overhead = len(json.dumps(empty, separators=(",", ":"), ensure_ascii=False).encode())
        encoded_content = MAX_REQUEST_BYTES + (kind == "wire-plus-one") - overhead
        controls, ascii_count = divmod(encoded_content, 6)
        messages = [{"role": "user", "content": "\0" * controls + "x" * ascii_count}]
    try:
        if kind == "wire-plus-one":
            with pytest.raises(PromptCountUnavailable):
                await provider.measure_prompt(messages)
            assert calls == []
        else:
            await provider.measure_prompt(messages)
            assert json.loads(calls[0].content)["messages"] == messages
            if kind == "wire":
                assert len(calls[0].content) == MAX_REQUEST_BYTES
    finally:
        await provider.close()


async def held_client(*, failure=False, stall=False):
    provider, calls, bodies = provider_for()
    original = provider.http._transport

    def respond(request):
        response = original.handler(request)
        stream = CountBody(response.stream.raw, held=True, failure=failure, stall=stall)
        bodies[-1] = stream
        return httpx.Response(200, stream=stream)

    provider.http._transport = httpx.MockTransport(respond)
    return provider, calls, bodies


async def first_body(bodies):
    async with asyncio.timeout(1):
        while not bodies:
            await asyncio.sleep(0)
    return bodies[0]


@pytest.mark.parametrize("cancel_phase", [None, "close", "read"])
async def test_success_waits_for_actual_close_and_cancel_keeps_it_owned(cancel_phase):
    provider, calls, bodies = await held_client(stall=cancel_phase == "read")

    async def run():
        with owned_http_stream_cleanup():
            return await provider.measure_prompt(MESSAGES)

    task = asyncio.create_task(run())
    try:
        body = await first_body(bodies)
        if cancel_phase == "read":
            await asyncio.wait_for(body.reading.wait(), 1)
            task.cancel()
        await asyncio.wait_for(body.closing.wait(), 1)
        assert not task.done(), "count escaped the actual response close"
        if cancel_phase is not None:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not body.cancelled
        body.release.set()
        if cancel_phase is None:
            assert (await asyncio.wait_for(task, 1)).input_tokens == 1920
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        assert len(calls) == 1 and body.closed == 1 and not body.cancelled
    finally:
        for body in bodies:
            body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


async def test_real_close_failure_is_not_measurement_success():
    provider, calls, bodies = await held_client(failure=True)

    async def run():
        with owned_http_stream_cleanup():
            return await provider.measure_prompt(MESSAGES)

    task = asyncio.create_task(run())
    try:
        body = await first_body(bodies)
        await asyncio.wait_for(body.closing.wait(), 1)
        body.release.set()
        with pytest.raises(ProviderCleanupError):
            await asyncio.wait_for(task, 1)
        assert len(calls) == 1 and bodies[0].closed == 1
    finally:
        for body in bodies:
            body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


async def test_overall_deadline_interrupts_stalled_body_but_retains_actual_close():
    body = CountBody(b"", held=True, stall=True)
    provider, calls, _ = provider_for(raw=body, request_timeout_s=1)

    async def run():
        with owned_http_stream_cleanup():
            return await provider.measure_prompt(MESSAGES)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(body.reading.wait(), 1)
        # One-second native budget plus five-second transport grace. HTTPX's
        # MockTransport does not implement its own deadline, so this verifies
        # the client's overall wait as well as the existing real-close owner.
        await asyncio.wait_for(body.closing.wait(), 7)
        assert not task.done() and not body.cancelled
        body.release.set()
        with pytest.raises(PromptCountUnavailable):
            await asyncio.wait_for(task, 1)
        assert len(calls) == 1 and body.closed == 1
    finally:
        body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.parametrize("status,raw", [(503, b"private synthetic error"), (200, b"malformed")])
async def test_error_also_waits_for_real_close(status, raw):
    body = CountBody(raw, held=True)
    provider, calls, _ = provider_for(raw=body, status=status)

    async def run():
        with owned_http_stream_cleanup():
            return await provider.measure_prompt(MESSAGES)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(body.closing.wait(), 1)
        assert not task.done()
        body.release.set()
        with pytest.raises(PromptCountUnavailable):
            await asyncio.wait_for(task, 1)
        assert len(calls) == 1 and body.closed == 1
    finally:
        body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.parametrize("mutation", ["input", "alias", "tokens", "context", "manifest"])
async def test_captured_request_or_configuration_change_during_close_refuses(mutation):
    provider, calls, bodies = await held_client()
    messages = [dict(row) for row in MESSAGES]
    task = asyncio.create_task(provider.measure_prompt(messages))
    try:
        body = await first_body(bodies)
        await asyncio.wait_for(body.closing.wait(), 1)
        if mutation == "input":
            messages[-1]["content"] = "Different public request."
        elif mutation == "alias":
            provider.model = "different-alias"
        elif mutation == "tokens":
            provider.max_tokens = 129
        elif mutation == "context":
            provider._binding["context_tokens"] = 4096
        else:
            provider._binding["manifest_sha256"] = "b" * 64
        bodies[0].release.set()
        with pytest.raises(PromptCountUnavailable):
            await asyncio.wait_for(task, 1)
        assert json.loads(calls[0].content)["messages"] == MESSAGES
        assert bodies[0].closed == 1
    finally:
        for body in bodies:
            body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.fixture
def policy(tmp_path):
    state = ResourceState(tmp_path / "resources.json")
    state.save(phase="ready")
    yield state
    state.close()


def optional_provider(*, local=True, result=None):
    calls = []
    result = result or PromptTokenCount(1920, 2048, 128, "a" * 64, "b" * 64)

    async def measure(messages):
        calls.append(messages)
        return result

    provider = SimpleNamespace(
        info=ProviderInfo(provider="synthetic", model="fixture", local=local),
        measure_prompt=measure,
    )
    return provider, calls, result


async def test_resource_wrapper_delegates_optional_local_converse_only(policy):
    provider, calls, expected = optional_provider()
    wrapped = ResourceBoundProvider(provider, policy.path, "converse")
    assert await wrapped.measure_prompt(MESSAGES) is expected
    assert calls == [MESSAGES]


@pytest.mark.parametrize("role,local", [("agent", True), ("decisions", True), ("converse", False)])
async def test_other_roles_or_cloud_cannot_use_count_capability(policy, role, local):
    provider, calls, _ = optional_provider(local=local)
    with pytest.raises(PromptCountUnavailable):
        await ResourceBoundProvider(provider, policy.path, role).measure_prompt(MESSAGES)
    assert calls == []


async def test_unsupported_stays_usable_for_ordinary_dialogue(policy):
    calls = []

    async def complete(messages):
        calls.append(messages)
        return "Public ordinary reply."

    provider = SimpleNamespace(
        info=ProviderInfo(provider="synthetic", model="fixture", local=True), complete=complete
    )
    wrapped = ResourceBoundProvider(provider, policy.path, "converse")
    with pytest.raises(PromptCountUnavailable):
        await wrapped.measure_prompt(MESSAGES)
    assert calls == [] and await wrapped.complete(MESSAGES) == "Public ordinary reply."


@pytest.mark.parametrize("failure", ["off", "revision", "provider", "role", "path", "locality"])
async def test_wrapper_fences_policy_and_selected_provider_after_await(policy, failure, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    provider, calls, expected = optional_provider()

    async def measure(messages):
        calls.append(messages)
        entered.set()
        await release.wait()
        return expected

    provider.measure_prompt = measure
    wrapped = ResourceBoundProvider(provider, policy.path, "converse")
    task = asyncio.create_task(wrapped.measure_prompt(MESSAGES))
    replacement = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if failure == "off":
            policy.save(phase="off", desired_converse=False)
        elif failure == "revision":
            policy.save(reason="Still ready, but a distinct resource revision.")
        elif failure == "provider":
            wrapped.provider = optional_provider()[0]
        elif failure == "role":
            wrapped.role = "agent"
        elif failure == "path":
            replacement = ResourceState(tmp_path / "other" / "resources.json")
            replacement.save(phase="ready")
            wrapped.state_path = replacement.path
        else:
            provider.info.local = False
        release.set()
        with pytest.raises(PromptCountUnavailable):
            await asyncio.wait_for(task, 1)
        assert calls == [MESSAGES]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if replacement is not None:
            replacement.close()


@pytest.mark.parametrize("failure", ["off", "missing", "invalid"])
async def test_unavailable_policy_refuses_before_provider(policy, failure):
    provider, calls, _ = optional_provider()
    if failure == "off":
        policy.save(desired_converse=False, phase="off")
    elif failure == "missing":
        policy.path.unlink()
    else:
        policy.path.write_text("private synthetic malformed state")
    with pytest.raises(PromptCountUnavailable) as caught:
        await ResourceBoundProvider(provider, policy.path, "converse").measure_prompt(MESSAGES)
    assert calls == [] and "private synthetic" not in str(caught.value)


@pytest.mark.parametrize("result", [None, {}, 7])
async def test_wrapper_refuses_non_result_capability_values(policy, result):
    provider, _, _ = optional_provider()

    async def measure(messages):
        return result

    provider.measure_prompt = measure
    with pytest.raises(PromptCountUnavailable):
        await ResourceBoundProvider(provider, policy.path, "converse").measure_prompt(MESSAGES)


@pytest.mark.parametrize("failure", [ProviderCleanupError, asyncio.CancelledError, ValueError])
async def test_wrapper_preserves_cancellation_and_cleanup_error(policy, failure):
    provider, _, _ = optional_provider()

    async def measure(messages):
        raise failure("private synthetic exception")

    provider.measure_prompt = measure
    expected = (
        failure
        if failure in {ProviderCleanupError, asyncio.CancelledError}
        else PromptCountUnavailable
    )
    with pytest.raises(expected) as caught:
        await ResourceBoundProvider(provider, policy.path, "converse").measure_prompt(MESSAGES)
    if expected is PromptCountUnavailable:
        assert "private synthetic" not in str(caught.value)


async def test_wrapper_policy_recheck_happens_after_actual_http_close(policy):
    provider, calls, bodies = await held_client()
    wrapped = ResourceBoundProvider(provider, policy.path, "converse")
    task = asyncio.create_task(wrapped.measure_prompt(MESSAGES))
    try:
        body = await first_body(bodies)
        await asyncio.wait_for(body.closing.wait(), 1)
        policy.save(reason="New ready revision during retained HTTP close.")
        assert not task.done()
        bodies[0].release.set()
        with pytest.raises(PromptCountUnavailable):
            await asyncio.wait_for(task, 1)
        assert len(calls) == 1 and bodies[0].closed == 1
    finally:
        for body in bodies:
            body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()
