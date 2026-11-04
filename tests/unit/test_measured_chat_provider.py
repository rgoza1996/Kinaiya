"""Independent #447 phase-one transport/admission oracles; synthetic HTTP only.

Written from the frozen contract before inspecting the implementation. These
checks establish neither endpoint/model identity nor native media readiness.
"""

import asyncio
import copy
import json

import httpx
import pytest

from packages.contracts.agent_tools import (
    agent_export_scope,
    agent_tool_scope,
    native_tool_snapshot,
)
from packages.providers import decision_capture
from packages.providers.complete_http import RequestBoundAsyncClient
from packages.providers.measured_chat import MeasuredChatProvider
from packages.providers.prompt_budget import MAX_SAFE_COUNT, PromptCountUnavailable, request_digest
from packages.providers.text import ProviderCleanupError, ProviderError, ProviderRefusal

MARKER = "SYNTHETIC-UNTRUSTED-MEASURED-CHAT-MARKER"
MESSAGES = [
    {"role": "system", "content": "Public synthetic personality."},
    {"role": "user", "content": "Public synthetic question café 🧭."},
]
COUNT = b'{"object":"response.input_tokens","input_tokens":123}'
COMPLETE = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"Public reply."},"finish_reason":"stop"}]}'
SSE = (
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"Public reply."},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


def compact(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


class Body(httpx.AsyncByteStream):
    def __init__(self, raw=COUNT, *, held=False, fail=False):
        self.raw, self.fail = raw, fail
        self.closing, self.release, self.exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.closed = 0
        self.close_cancelled = False
        if not held:
            self.release.set()

    async def __aiter__(self):
        for index in range(0, len(self.raw), 7):
            yield self.raw[index : index + 7]

    async def aclose(self):
        self.closed += 1
        self.closing.set()
        try:
            await self.release.wait()
            if self.fail:
                raise OSError(MARKER)
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        finally:
            self.exited.set()


class Wire:
    def __init__(self, *, counts=None, generation=None, count_status=200, count_headers=None):
        self.counts = iter(counts) if counts is not None else None
        self.generation = generation
        self.count_status, self.count_headers = count_status, count_headers or {}
        self.calls, self.bodies = [], []

    async def __call__(self, request):
        self.calls.append(request)
        if request.url.path.endswith("/input_tokens"):
            body = next(self.counts) if self.counts is not None else Body()
            status, headers = self.count_status, self.count_headers
        else:
            body = self.generation or Body(
                SSE if json.loads(request.content)["stream"] else COMPLETE
            )
            status, headers = 200, {}
        self.bodies.append(body)
        return httpx.Response(status, headers=headers, stream=body)

    @property
    def generation_calls(self):
        return [request for request in self.calls if not request.url.path.endswith("/input_tokens")]


def provider(wire=None, **changes):
    wire = wire or Wire()
    options = dict(
        base_url="http://127.0.0.1:19447/v1",
        model="synthetic-model",
        context_tokens=4096,
        api_key="synthetic-key",
        max_tokens=128,
        reasoning_effort="none",
        timeout=30,
        transport=httpx.MockTransport(wire),
    )
    options.update(changes)
    return MeasuredChatProvider(**options), wire


async def collect(adapter, messages):
    return [part async for part in adapter.stream(messages)]


async def close(adapter):
    try:
        await adapter.close()
    except ProviderCleanupError:
        # A retained actual failure is not normalized into a successful close.
        pass


@pytest.mark.parametrize("mode", ["stream", "prepared", "complete"])
async def test_exact_count_and_generation_wire_body_and_single_use(mode):
    adapter, wire = provider()
    messages = copy.deepcopy(MESSAGES)
    try:
        assert isinstance(adapter.http, RequestBoundAsyncClient)
        assert adapter.info.provider == "openai-compatible" and adapter.info.local is True
        if mode == "stream":
            measured = await adapter.measure_prompt(messages)
            assert len(wire.calls) == 1 and not wire.generation_calls
            assert wire.bodies[0].exited.is_set()
            assert await collect(adapter, messages) == ["Public reply."]
            with pytest.raises(PromptCountUnavailable):
                await collect(adapter, messages)
        elif mode == "prepared":
            prepared = await adapter.prepare_complete(messages)
            measured = prepared.measurement
            assert not prepared.fixture and len(wire.calls) == 1
            assert await prepared.complete() == "Public reply."
            with pytest.raises(PromptCountUnavailable):
                await prepared.complete()
        else:
            assert await adapter.complete(messages) == "Public reply."
            measured = None
        expected = {
            "model": "synthetic-model",
            "messages": messages,
            "max_tokens": 128,
            "stream": mode == "stream",
            "reasoning_effort": "none",
        }
        assert [request.url.path for request in wire.calls] == [
            "/v1/chat/completions/input_tokens",
            "/v1/chat/completions",
        ]
        assert wire.calls[0].content == wire.calls[1].content
        assert json.loads(wire.calls[0].content) == expected
        assert wire.calls[0].headers["authorization"] == "Bearer synthetic-key"
        assert all(request.headers["accept-encoding"] == "identity" for request in wire.calls)
        assert adapter.http.follow_redirects is False and adapter.http.trust_env is False
        assert all(body.closed == 1 and body.exited.is_set() for body in wire.bodies)
        if measured is not None:
            assert (
                measured.input_tokens,
                measured.context_tokens,
                measured.reserved_output_tokens,
            ) == (123, 4096, 128)
            assert measured.fits and measured.request_sha256 == request_digest(expected)
            assert len(measured.binding_sha256) == 64
    finally:
        await close(adapter)


@pytest.mark.parametrize("count,fits", [(3968, True), (3969, False), (MAX_SAFE_COUNT, False)])
async def test_exact_fit_and_overflow_count_never_estimate_or_generate_early(count, fits):
    adapter, wire = provider(
        Wire(counts=[Body(compact({"object": "response.input_tokens", "input_tokens": count}))])
    )
    try:
        measured = await adapter.measure_prompt(MESSAGES)
        assert measured.fits is fits and measured.input_tokens == count
        if fits:
            assert await collect(adapter, MESSAGES) == ["Public reply."]
        else:
            with pytest.raises(PromptCountUnavailable):
                await collect(adapter, MESSAGES)
            assert not wire.generation_calls
        assert len(wire.calls) == 1 + int(fits)
    finally:
        await close(adapter)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"\xff",
        b'{"object":"wrong","input_tokens":1}',
        b'{"object":"response.input_tokens","input_tokens":true}',
        b'{"object":"response.input_tokens","input_tokens":1.0}',
        b'{"object":"response.input_tokens","input_tokens":0}',
        b'{"object":"response.input_tokens","input_tokens":9007199254740992}',
        b'{"object":"response.input_tokens","input_tokens":1,"input_tokens":2}',
        b'{"object":"response.input_tokens","input_tokens":NaN}',
        compact({"object": "response.input_tokens", "input_tokens": 1, "extra": MARKER}),
        b'{"object":"response.input_tokens","input_tokens":' + b"[" * 1500 + b"]" * 1500 + b"}",
        b" " * 4097,
    ],
)
async def test_invalid_count_closes_original_and_never_yields_generation_ticket(raw):
    body = Body(raw)
    adapter, wire = provider(Wire(counts=[body]))
    try:
        with pytest.raises(PromptCountUnavailable) as error:
            await adapter.measure_prompt(MESSAGES)
        assert MARKER not in str(error.value) and "synthetic-key" not in str(error.value)
        assert body.closed == 1 and body.exited.is_set()
        with pytest.raises(PromptCountUnavailable):
            await collect(adapter, MESSAGES)
        assert len(wire.calls) == 1 and not wire.generation_calls
    finally:
        await close(adapter)


@pytest.mark.parametrize(
    "status,headers",
    [
        (302, {"location": "http://127.0.0.1:19448/v1"}),
        (503, {}),
        (200, {"content-encoding": "gzip"}),
    ],
)
async def test_count_status_or_encoding_refuses_without_redirect_or_retry(status, headers):
    body = Body()
    adapter, wire = provider(Wire(counts=[body], count_status=status, count_headers=headers))
    try:
        with pytest.raises(PromptCountUnavailable):
            await adapter.prepare_complete(MESSAGES)
        assert len(wire.calls) == body.closed == 1
        assert not wire.generation_calls and body.exited.is_set()
    finally:
        await close(adapter)


async def test_count_raw_bound_is_inclusive_and_failed_recount_invalidates_prior_ticket():
    exact = COUNT + b" " * (4096 - len(COUNT))
    adapter, wire = provider(Wire(counts=[Body(exact), Body(exact + b" ")]))
    try:
        assert (await adapter.measure_prompt(MESSAGES)).input_tokens == 123
        with pytest.raises(PromptCountUnavailable):
            await adapter.measure_prompt(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await collect(adapter, MESSAGES)
        assert len(wire.calls) == 2 and not wire.generation_calls
        assert all(body.closed == 1 for body in wire.bodies)
    finally:
        await close(adapter)


async def test_missing_ticket_and_invalid_new_input_cannot_reuse_previous_count():
    adapter, wire = provider()
    try:
        with pytest.raises(PromptCountUnavailable):
            await collect(adapter, MESSAGES)
        assert not wire.calls
        await adapter.measure_prompt(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await adapter.measure_prompt([{"role": "user", "content": object()}])
        with pytest.raises(PromptCountUnavailable):
            await collect(adapter, MESSAGES)
        assert len(wire.calls) == 1
    finally:
        await close(adapter)


async def test_stream_ticket_is_consumed_before_an_awaited_generation_hook():
    adapter, wire = provider()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(request):
        if not request.url.path.endswith("input_tokens"):
            entered.set()
            await release.wait()

    adapter.http.event_hooks["request"].append(hook)
    await adapter.measure_prompt(MESSAGES)
    first = asyncio.create_task(collect(adapter, MESSAGES))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            with pytest.raises(PromptCountUnavailable):
                await collect(adapter, MESSAGES)
            assert len(wire.calls) == 1 and not wire.generation_calls
            release.set()
            assert await first == ["Public reply."]
        assert len(wire.calls) == 2 and len(wire.generation_calls) == 1
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await close(adapter)


async def test_older_slow_count_cannot_replace_or_erase_newer_original_ticket():
    older_body = Body(held=True)
    adapter, wire = provider(Wire(counts=[older_body, Body()]))
    older = asyncio.create_task(adapter.measure_prompt(copy.deepcopy(MESSAGES)))
    newer_messages = [{"role": "user", "content": "Newer public synthetic request"}]
    try:
        async with asyncio.timeout(2):
            await older_body.closing.wait()
            await adapter.measure_prompt(newer_messages)
            older_body.release.set()
            with pytest.raises(PromptCountUnavailable):
                await older
        assert await collect(adapter, newer_messages) == ["Public reply."]
        assert len(wire.calls) == 3 and len(wire.generation_calls) == 1
        assert json.loads(wire.generation_calls[0].content)["messages"] == newer_messages
    finally:
        older_body.release.set()
        await asyncio.gather(older, return_exceptions=True)
        await close(adapter)


@pytest.mark.parametrize("phase", ["count", "generation"])
@pytest.mark.parametrize("change", ["url", "stream", "headers", "transport"])
async def test_actual_request_hook_mutations_never_reach_real_transport(phase, change):
    adapter, wire = provider()
    original_transport = adapter.http._transport
    replacement = httpx.MockTransport(lambda _: pytest.fail("Replacement transport dispatched"))

    async def hook(request):
        if request.url.path.endswith("input_tokens") != (phase == "count"):
            return
        await asyncio.sleep(0)
        if change == "url":
            request.url = httpx.URL("http://127.0.0.1:19448/v1/chat/completions")
        elif change == "stream":
            request.stream = httpx.ByteStream(b"{}")
        elif change == "headers":
            request.headers["authorization"] = "Bearer synthetic-replacement"
        else:
            adapter.http._transport = replacement

    adapter.http.event_hooks["request"].append(hook)
    try:
        if phase == "count":
            with pytest.raises(PromptCountUnavailable):
                await adapter.prepare_complete(MESSAGES)
        else:
            prepared = await adapter.prepare_complete(MESSAGES)
            with pytest.raises(PromptCountUnavailable):
                await prepared.complete()
            with pytest.raises(PromptCountUnavailable):
                await prepared.complete()
        assert len(wire.calls) == int(phase == "generation")
        assert not wire.generation_calls
    finally:
        adapter.http._transport = original_transport
        await replacement.aclose()
        await close(adapter)


@pytest.mark.parametrize(
    "change", ["messages", "context", "headers", "auth", "cookies", "hooks", "origin", "mounts"]
)
async def test_awaited_original_count_close_rechecks_input_and_full_client_configuration(change):
    body = Body(held=True)
    adapter, wire = provider(Wire(counts=[body]))
    messages = copy.deepcopy(MESSAGES)
    task = asyncio.create_task(adapter.measure_prompt(messages))
    saved_mounts = adapter.http._mounts
    try:
        async with asyncio.timeout(2):
            await body.closing.wait()
            if change == "messages":
                messages[-1]["content"] = MARKER
            elif change == "context":
                adapter.context_tokens = 8192
            elif change == "headers":
                adapter.http.headers["authorization"] = "Bearer synthetic-replacement"
            elif change == "auth":
                adapter.http.auth = httpx.BasicAuth("synthetic", "replacement")
            elif change == "cookies":
                adapter.http.cookies.set("synthetic", "replacement")
            elif change == "hooks":

                async def added_hook(_request):
                    pytest.fail("New hook must not dispatch")

                adapter.http.event_hooks["request"].append(added_hook)
            elif change == "origin":
                adapter.http.base_url = "http://127.0.0.1:19448/v1/"
            else:
                adapter.http._mounts = {
                    httpx._utils.URLPattern("all://"): httpx.MockTransport(
                        lambda _: pytest.fail("New mount dispatched")
                    )
                }
            body.release.set()
            with pytest.raises(PromptCountUnavailable) as error:
                await task
        assert MARKER not in str(error.value)
        assert len(wire.calls) == 1 and body.closed == 1
        with pytest.raises(PromptCountUnavailable):
            await collect(adapter, messages)
        assert not wire.generation_calls
    finally:
        body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        adapter.http._mounts = saved_mounts
        await close(adapter)


@pytest.mark.parametrize("phase", ["count", "generation"])
async def test_suppressed_request_hook_cancellation_cannot_dispatch(phase):
    adapter, wire = provider()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(request):
        if request.url.path.endswith("input_tokens") != (phase == "count"):
            return
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # Deliberately hostile hook; the original task is still cancelling.

    adapter.http.event_hooks["request"].append(hook)

    async def run():
        prepared = await adapter.prepare_complete(MESSAGES)
        return await prepared.complete()

    task = asyncio.create_task(run())
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            task.cancel()
            with pytest.raises((PromptCountUnavailable, asyncio.CancelledError)):
                await task
        assert len(wire.calls) == int(phase == "generation") and not wire.generation_calls
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await close(adapter)


@pytest.mark.parametrize("change", ["source", "headers", "disclosure"])
async def test_capture_await_cannot_change_a_counted_complete_request(change):
    adapter, wire = provider()
    messages = copy.deepcopy(MESSAGES)
    allowed = True

    class Capture:
        enabled = False

        async def start(self, _provider, body, _format):
            nonlocal allowed
            if not self.enabled:
                return
            await asyncio.sleep(0)
            if change == "source":
                messages[-1]["content"] = MARKER
            elif change == "headers":
                adapter.http.headers["authorization"] = "Bearer synthetic-replacement"
            else:
                allowed = False

        async def check_current(self, _provider):
            pass

    capture = Capture()
    token = decision_capture._capture.set(capture)
    try:
        with agent_export_scope(lambda: allowed):
            prepared = await adapter.prepare_complete(messages)
            capture.enabled = True
            with pytest.raises((PromptCountUnavailable, ProviderRefusal)):
                await prepared.complete()
        assert len(wire.calls) == 1 and not wire.generation_calls
    finally:
        decision_capture._capture.reset(token)
        await close(adapter)


async def test_awaited_native_export_admission_rechecks_exact_dispatch_identity():
    adapter, wire = provider()
    admissions = []

    async def admit(purpose, digest):
        admissions.append((purpose, digest))
        await asyncio.sleep(0)
        if len(wire.calls) == 1:
            adapter.http.headers["authorization"] = "Bearer synthetic-replacement"

    try:
        with agent_tool_scope(native_tool_snapshot(), export_admission=admit):
            prepared = await adapter.prepare_complete(MESSAGES)
            with pytest.raises(PromptCountUnavailable):
                await prepared.complete()
        assert len(admissions) == 2
        assert all(len(digest) == 64 for _, digest in admissions)
        assert len(wire.calls) == 1 and not wire.generation_calls
    finally:
        await close(adapter)


@pytest.mark.parametrize(
    "change", ["source", "task", "tool_snapshot", "captured_grant", "active_grant"]
)
async def test_prepared_callback_is_owner_bound_consumed_and_keeps_both_disclosure_guards(change):
    adapter, wire = provider()
    messages, allowed = copy.deepcopy(MESSAGES), True
    snapshot = native_tool_snapshot()
    try:
        with agent_tool_scope(snapshot), agent_export_scope(lambda: allowed):
            prepared = await adapter.prepare_complete(messages)
        if change == "source":
            messages[-1]["content"] = MARKER
        elif change == "captured_grant":
            allowed = False
        other = (
            native_tool_snapshot(artifacts_available=True)
            if change == "tool_snapshot"
            else snapshot
        )
        with agent_tool_scope(other), agent_export_scope(lambda: change != "active_grant"):
            if change == "task":
                task = asyncio.create_task(prepared.complete())
                with pytest.raises(PromptCountUnavailable):
                    await task
            else:
                with pytest.raises((PromptCountUnavailable, ProviderRefusal)):
                    await prepared.complete()
            with pytest.raises(PromptCountUnavailable):
                await prepared.complete()
        assert len(wire.calls) == 1 and not wire.generation_calls
    finally:
        await close(adapter)


@pytest.mark.parametrize("phase", ["count", "generation", "stream_generation"])
async def test_repeated_cancellation_retains_same_actual_body_close(phase):
    raw = COUNT if phase == "count" else SSE if phase == "stream_generation" else COMPLETE
    held = Body(raw, held=True)
    adapter, wire = provider(
        Wire(
            counts=[held] if phase == "count" else None,
            generation=held if phase != "count" else None,
        )
    )

    async def run():
        if phase == "stream_generation":
            await adapter.measure_prompt(MESSAGES)
            return await collect(adapter, MESSAGES)
        return await adapter.complete(MESSAGES)

    task = asyncio.create_task(run())
    try:
        async with asyncio.timeout(2):
            await held.closing.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and held.closed == 1 and not held.exited.is_set()
            assert not held.close_cancelled
            held.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert held.exited.is_set() and held.closed == 1
        assert len(wire.calls) == (1 if phase == "count" else 2)
    finally:
        held.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await close(adapter)


async def test_cancelled_held_headers_still_own_and_close_that_late_original_response():
    entered, headers_release = asyncio.Event(), asyncio.Event()
    body, requests = Body(held=True), []

    async def send(request):
        requests.append(request)
        entered.set()
        try:
            await headers_release.wait()
        except asyncio.CancelledError:
            await headers_release.wait()  # Original send deliberately resists cancellation.
        return httpx.Response(200, stream=body)

    adapter, _ = provider(transport=httpx.MockTransport(send))
    task = asyncio.create_task(adapter.complete(MESSAGES))
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not body.closing.is_set()
            headers_release.set()
            await body.closing.wait()
            assert not task.done() and body.closed == 1 and not body.exited.is_set()
            body.release.set()
            with pytest.raises((PromptCountUnavailable, asyncio.CancelledError)):
                await task
        assert len(requests) == 1 and requests[0].url.path.endswith("/input_tokens")
        assert body.closed == 1 and body.exited.is_set()
    finally:
        headers_release.set()
        body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await close(adapter)


@pytest.mark.parametrize("phase", ["count", "generation", "stream_generation"])
async def test_actual_cleanup_failure_latches_adapter_unavailable_without_retry(phase):
    raw = COUNT if phase == "count" else SSE if phase == "stream_generation" else COMPLETE
    failed = Body(raw, fail=True)
    adapter, wire = provider(
        Wire(
            counts=[failed] if phase == "count" else None,
            generation=failed if phase != "count" else None,
        )
    )
    try:
        with pytest.raises(ProviderCleanupError) as error:
            if phase == "stream_generation":
                await adapter.measure_prompt(MESSAGES)
                await collect(adapter, MESSAGES)
            else:
                await adapter.complete(MESSAGES)
        assert MARKER not in str(error.value)
        before = len(wire.calls)
        with pytest.raises(PromptCountUnavailable):
            await adapter.measure_prompt(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await collect(adapter, MESSAGES)
        assert len(wire.calls) == before == (1 if phase == "count" else 2)
        assert failed.closed == 1 and failed.exited.is_set()
    finally:
        await close(adapter)


@pytest.mark.parametrize(
    "raw",
    [
        b"data: [DONE]\n\n",
        b'data: {"choices":[{"delta":{"content":"unqualified"},"finish_reason":"length"}]}\n\n',
        b'data: {"choices":[{"delta":{"tool_calls":[]},"finish_reason":"stop"}]}\n\n'
        + b"data: [DONE]\n\n",
        SSE.replace(b"data: [DONE]\n\n", b""),
    ],
)
async def test_stream_requires_compatible_terminals_and_does_not_repair_output(raw):
    body = Body(raw)
    adapter, wire = provider(Wire(generation=body))
    try:
        await adapter.measure_prompt(MESSAGES)
        with pytest.raises(ProviderError):
            await collect(adapter, MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await collect(adapter, MESSAGES)
        assert len(wire.calls) == 2 and body.closed == 1
    finally:
        await close(adapter)


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "http://localhost:19447/v1"},
        {"base_url": "https://example.invalid/v1"},
        {"base_url": "http://127.0.0.2:19447/v1"},
        {"base_url": "http://synthetic-key@127.0.0.1:19447/v1"},
        {"base_url": "http://127.0.0.1:19447/v1?secret=synthetic-key"},
        {"base_url": "http://127.0.0.1:19447/v1#fragment"},
        {"context_tokens": True},
        {"context_tokens": 63},
        {"context_tokens": MAX_SAFE_COUNT + 1},
        {"max_tokens": True},
        {"max_tokens": 15},
        {"max_tokens": 65537},
        {"context_tokens": 128, "max_tokens": 128},
        {"model": ""},
        {"model": "x" * 257},
        {"model": "bad\0model"},
        {"reasoning_effort": True},
        {"reasoning_effort": "unsupported"},
    ],
)
def test_strict_loopback_identity_and_context_limits_refuse_before_transport(change):
    wire = Wire()
    with pytest.raises(ProviderError) as error:
        provider(wire, **change)
    assert "synthetic-key" not in str(error.value)
    assert not wire.calls


async def test_explicit_ipv6_loopback_can_use_an_anonymous_owner_trusted_endpoint():
    adapter, wire = provider(base_url="https://[::1]:19447/v1", api_key="")
    try:
        assert (await adapter.measure_prompt(MESSAGES)).fits
        assert wire.calls[0].url.host == "::1"
        assert "authorization" not in wire.calls[0].headers
        assert adapter.info.local is True
    finally:
        await close(adapter)


async def test_request_wire_bound_and_nonjson_inputs_refuse_before_copy_or_send():
    class Foreign:
        def __deepcopy__(self, _memo):
            pytest.fail("Invalid foreign input must be rejected before deepcopy")

        def __repr__(self):
            pytest.fail("Foreign input must not be formatted")

    adapter, wire = provider(reasoning_effort=None)
    try:
        blank = [{"role": "user", "content": ""}]
        envelope = {
            "model": "synthetic-model",
            "messages": blank,
            "max_tokens": 128,
            "stream": True,
        }
        size = 512 * 1024 - len(compact(envelope))
        exact = [{"role": "user", "content": "a" * size}]
        await adapter.measure_prompt(exact)
        assert len(wire.calls[0].content) == 512 * 1024
        cycle = []
        cycle.append(cycle)
        for invalid in (
            [{"role": "user", "content": "a" * (size + 1)}],
            [{"role": "user", "content": float("nan")}],
            [{"role": "user", "content": Foreign()}],
            cycle,
        ):
            with pytest.raises(PromptCountUnavailable) as error:
                await adapter.measure_prompt(invalid)
            assert MARKER not in str(error.value)
        assert len(wire.calls) == 1 and not wire.generation_calls
    finally:
        await close(adapter)


async def test_close_fences_new_operations_synchronously_and_does_not_generate():
    class ClosingTransport(httpx.MockTransport):
        def __init__(self, handler):
            super().__init__(handler)
            self.closing, self.release = asyncio.Event(), asyncio.Event()

        async def aclose(self):
            self.closing.set()
            await self.release.wait()
            await super().aclose()

    wire = Wire()
    transport = ClosingTransport(wire)
    adapter, _ = provider(wire, transport=transport)
    await adapter.measure_prompt(MESSAGES)
    cleanup = asyncio.create_task(adapter.close())
    try:
        async with asyncio.timeout(2):
            await transport.closing.wait()
            assert not cleanup.done()
            for invoke in (
                lambda: adapter.measure_prompt(MESSAGES),
                lambda: adapter.prepare_complete(MESSAGES),
                lambda: adapter.complete(MESSAGES),
                lambda: collect(adapter, MESSAGES),
            ):
                with pytest.raises(PromptCountUnavailable):
                    await invoke()
            assert len(wire.calls) == 1 and not wire.generation_calls
            transport.release.set()
            await cleanup
    finally:
        transport.release.set()
        await asyncio.gather(cleanup, return_exceptions=True)
