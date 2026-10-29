"""Native prompt measurement with synthetic leases and owned HTTPX transport.

No socket is opened: the injected AsyncBaseTransport supplies bounded JSON.
"""

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import httpx
import pytest

from packages.providers import owned_unix_http
from packages.providers.prompt_budget import binding_digest, request_digest
from packages.providers.text import ProviderCleanupError, ProviderError
from packages.resources.processes import WorkerIdentity
from services.resources import gguf


class Lease:
    """Synthetic observer: extinction is controlled independently of manager proof."""

    def __init__(self, identity, path):
        self.identity = identity
        self.outer_pid, self.start_time = identity.pid, identity.start_time
        self.socket_path = path
        self.native_pid, self.init_pid = 102, 101
        self.handles = {102: 1102, 101: 1101}
        self.extinction = True
        self.forgotten = False

    def current(self, identity):
        assert identity is self.identity and not self.forgotten

    async def extinct(self):
        return self.extinction

    def forget(self):
        self.forgotten = True


@pytest.fixture
def endpoint(tmp_path):
    directory = tmp_path / "native"
    directory.mkdir(mode=0o700)
    (directory / "ipc").mkdir(mode=0o700)
    value = gguf.GGUFEndpoint(
        directory,
        {
            "alias": "synthetic",
            "model": "synthetic-model",
            "profile": "synthetic-profile",
            "assets": [
                {
                    "guest": "/runtime/llama-server",
                    "bytes": 12,
                    "sha256": "a" * 64,
                }
            ],
        },
    )
    return value


@pytest.fixture
def protocol(endpoint):
    return gguf.GGUFProtocol(endpoint, "b" * 64)


def retain(endpoint, *, identity=None, phase="exec_pending"):
    identity = identity or WorkerIdentity("gguf", 100, 10)
    lease = Lease(identity, endpoint.ipc / "model.sock")
    endpoint.leases[identity.pid] = lease
    endpoint.ready = lease
    path = endpoint.directory / "phase.json"
    path.write_text(json.dumps({"version": 1, "pid": identity.pid, "phase": phase}))
    path.chmod(0o600)
    return lease


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, *, hold_close=False, hold_eof=False, close_error=False):
        if isinstance(chunks, (bytes, bytearray)):
            chunks = [bytes(chunks)]
        self.chunks = chunks
        self.closing, self.release_close = asyncio.Event(), asyncio.Event()
        self.at_eof, self.release_eof = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.close_error = close_error
        if not hold_close:
            self.release_close.set()
        if not hold_eof:
            self.release_eof.set()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        self.at_eof.set()
        await self.release_eof.wait()

    async def aclose(self):
        self.closing.set()
        await self.release_close.wait()
        if self.close_error:
            raise OSError("synthetic HTTP close failure")
        self.closed = True


def install_http(monkeypatch, body, *, status=200, headers=None):
    transports, requests = [], []

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self, path, verify):
            self.cleanup_pending = False
            self.closed = False
            transports.append(self)

        async def handle_async_request(self, request):
            requests.append(request)
            return httpx.Response(
                status,
                headers=headers or {"content-type": "application/json"},
                stream=body,
            )

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(gguf, "OwnedUnixTransport", Transport)
    return transports, requests


def count_body(value):
    return json.dumps({"input_tokens": value, "object": "response.input_tokens"}).encode()


async def test_exact_count_body_equals_generation_body(endpoint, monkeypatch):
    lease = retain(endpoint)
    messages = [{"role": "user", "content": "public"}]
    body = Body(count_body(7))
    _, requests = install_http(monkeypatch, body)
    assert await endpoint.measure_prompt(messages, lease) == 7
    assert len(requests) == 1
    sent = json.loads(requests[0].content)
    assert sent == {
        "model": "synthetic",
        "messages": [{"role": "user", "content": "public"}],
        "max_tokens": 128,
        "stream": True,
    }
    assert sent == endpoint._body(messages)
    # The shared helper is the single source for both routes.
    assert endpoint._body(messages) == {
        "model": endpoint.manifest["alias"],
        "messages": messages,
        "max_tokens": endpoint.limits["max_new_tokens"],
        "stream": True,
    }


async def test_no_generation_request_on_measurement(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(3))
    _, requests = install_http(monkeypatch, body)
    assert await endpoint.measure_prompt([{"role": "user", "content": "hi"}], lease) == 3
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions/input_tokens"
    assert request.url.path != "/v1/chat/completions"


async def test_positive_count_returns_after_real_close(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(42))
    transports, _ = install_http(monkeypatch, body)
    assert await endpoint.measure_prompt([{"role": "user", "content": "hi"}], lease) == 42
    assert body.closed and transports[0].closed and not endpoint.exchanges


async def test_overbudget_count_still_returns(endpoint, monkeypatch):
    lease = retain(endpoint)
    # 5000 exceeds context_tokens 2048; endpoint still returns the native count.
    body = Body(count_body(5000))
    _, _ = install_http(monkeypatch, body)
    assert await endpoint.measure_prompt([{"role": "user", "content": "hi"}], lease) == 5000


@pytest.mark.parametrize("status", [404, 405, 501])
async def test_unsupported_returns_none_only_after_full_close(endpoint, monkeypatch, status):
    lease = retain(endpoint)
    body = Body(b'{"error":"unsupported"}')
    transports, requests = install_http(monkeypatch, body, status=status)
    assert await endpoint.measure_prompt([{"role": "user", "content": "hi"}], lease) is None
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions/input_tokens"
    assert body.closed and transports[0].closed and not endpoint.exchanges


async def test_unsupported_empty_body_returns_none(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body([b""])
    transports, _ = install_http(monkeypatch, body, status=404)
    assert await endpoint.measure_prompt([], lease) is None
    assert body.closed and transports[0].closed


@pytest.mark.parametrize("status", [400, 429, 500, 502, 201, 302])
async def test_unexpected_status_is_refused(endpoint, monkeypatch, status):
    lease = retain(endpoint)
    body = Body(b'{"input_tokens":1,"object":"response.input_tokens"}')
    transports, _ = install_http(monkeypatch, body, status=status, headers={"location": "/else"})
    with pytest.raises(ValueError, match="refused"):
        await endpoint.measure_prompt([{"role": "user", "content": "hi"}], lease)
    assert body.closed and transports[0].closed and not endpoint.exchanges


async def test_redirect_is_not_followed(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(b"")
    transports, requests = install_http(
        monkeypatch, body, status=302, headers={"location": "/v1/chat/completions"}
    )
    with pytest.raises(ValueError, match="refused"):
        await endpoint.measure_prompt([], lease)
    assert requests[0].url.path == "/v1/chat/completions/input_tokens"
    assert body.closed and transports[0].closed


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"null",
        b"",
        b"not json",
        b'{"input_tokens":1}',
        b'{"object":"response.input_tokens"}',
        b'{"input_tokens":1,"object":"response.input_tokens","extra":1}',
        b'{"input_tokens":"1","object":"response.input_tokens"}',
        b'{"input_tokens":1.0,"object":"response.input_tokens"}',
        b'{"input_tokens":null,"object":"response.input_tokens"}',
        b'{"input_tokens":true,"object":"response.input_tokens"}',
        b'{"input_tokens":0,"object":"response.input_tokens"}',
        b'{"input_tokens":-1,"object":"response.input_tokens"}',
        b'{"input_tokens":9007199254740992,"object":"response.input_tokens"}',
        b'{"input_tokens":1,"object":"wrong"}',
        b'{"input_tokens":1,"object":1}',
        b'{"INPUT_TOKENS":1,"object":"response.input_tokens"}',
    ],
)
async def test_malformed_shape_or_type_is_rejected(endpoint, monkeypatch, raw):
    lease = retain(endpoint)
    secret = "SECRET_INPUT_9f3c"
    body = Body(raw)
    transports, _ = install_http(monkeypatch, body)
    with pytest.raises((ValueError, ProviderError)) as excinfo:
        await endpoint.measure_prompt([{"role": "user", "content": secret}], lease)
    assert secret not in str(excinfo.value)
    assert body.closed and transports[0].closed and not endpoint.exchanges


async def test_duplicate_keys_are_rejected(endpoint, monkeypatch):
    lease = retain(endpoint)
    raw = b'{"input_tokens":1,"input_tokens":2,"object":"response.input_tokens"}'
    body = Body(raw)
    transports, _ = install_http(monkeypatch, body)
    with pytest.raises((ValueError, ProviderError)):
        await endpoint.measure_prompt([], lease)
    assert body.closed and transports[0].closed


@pytest.mark.parametrize(
    "raw",
    [
        b'{"input_tokens":NaN,"object":"response.input_tokens"}',
        b'{"input_tokens":Infinity,"object":"response.input_tokens"}',
        b'{"input_tokens":-Infinity,"object":"response.input_tokens"}',
    ],
)
async def test_nonfinite_json_is_rejected(endpoint, monkeypatch, raw):
    lease = retain(endpoint)
    body = Body(raw)
    transports, _ = install_http(monkeypatch, body)
    with pytest.raises((ValueError, ProviderError)):
        await endpoint.measure_prompt([], lease)
    assert body.closed and transports[0].closed


async def test_invalid_utf8_is_rejected(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body([b"\xff\xfe\x00bad"])
    transports, _ = install_http(monkeypatch, body)
    with pytest.raises((ValueError, ProviderError)):
        await endpoint.measure_prompt([], lease)
    assert body.closed and transports[0].closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "compress", "GZIP"])
async def test_compression_is_rejected(endpoint, monkeypatch, encoding):
    lease = retain(endpoint)
    body = Body(count_body(1))
    transports, _ = install_http(monkeypatch, body, headers={"content-encoding": encoding})
    with pytest.raises((ValueError, ProviderError)):
        await endpoint.measure_prompt([], lease)
    assert transports[0].closed and not endpoint.exchanges


async def test_success_body_cap_is_enforced(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body([b"x" * 4097])
    transports, _ = install_http(monkeypatch, body)
    with pytest.raises(ValueError, match="bound"):
        await endpoint.measure_prompt([], lease)
    assert transports[0].closed and not endpoint.exchanges


async def test_unsupported_body_cap_is_enforced(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body([b"x" * 5000])
    transports, _ = install_http(monkeypatch, body, status=404)
    with pytest.raises(ValueError, match="bound"):
        await endpoint.measure_prompt([], lease)
    assert transports[0].closed and not endpoint.exchanges


async def test_exact_4096_body_is_allowed(endpoint, monkeypatch):
    lease = retain(endpoint)
    payload = count_body(9)
    padding = b" " * (4096 - len(payload))
    # Trailing whitespace keeps the JSON valid at exactly the bound.
    body = Body([payload + padding])
    assert len(payload + padding) == 4096
    transports, _ = install_http(monkeypatch, body)
    assert await endpoint.measure_prompt([], lease) == 9
    assert body.closed and transports[0].closed


async def test_result_withheld_until_native_eof_and_real_close(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(11), hold_eof=True, hold_close=True)
    transports, _ = install_http(monkeypatch, body)
    task = asyncio.create_task(endpoint.measure_prompt([{"role": "user", "content": "hi"}], lease))
    await body.at_eof.wait()
    assert not task.done() and endpoint.exchanges
    body.release_eof.set()
    await body.closing.wait()
    assert not task.done() and endpoint.exchanges
    body.release_close.set()
    assert await task == 11
    assert body.closed and transports[0].closed and not endpoint.exchanges


async def test_unsupported_withheld_until_eof_and_close(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body([b"nope"], hold_eof=True, hold_close=True)
    transports, _ = install_http(monkeypatch, body, status=501)
    task = asyncio.create_task(endpoint.measure_prompt([], lease))
    await body.at_eof.wait()
    assert not task.done()
    body.release_eof.set()
    await body.closing.wait()
    assert not task.done()
    body.release_close.set()
    assert await task is None
    assert body.closed and transports[0].closed and not endpoint.exchanges


async def test_failed_close_raises_cleanup(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(5), close_error=True)
    transports, _ = install_http(monkeypatch, body)
    with pytest.raises((ProviderCleanupError, owned_unix_http.UnixCleanupError)):
        await endpoint.measure_prompt([], lease)
    # Synthetic response cleanup fails before the owned client close.
    assert not body.closed and transports[0].closed and not endpoint.exchanges


async def test_cancellation_waits_for_http_close(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(6), hold_close=True)
    transports, _ = install_http(monkeypatch, body)
    task = asyncio.create_task(endpoint.measure_prompt([], lease))
    await body.closing.wait()
    assert not task.done() and endpoint.exchanges
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and endpoint.exchanges
    body.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed and transports[0].closed


async def test_stale_lease_is_refused_before_request(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(1))
    _, requests = install_http(monkeypatch, body)
    endpoint.ready = None
    with pytest.raises(ValueError, match="ready"):
        await endpoint.measure_prompt([], lease)
    assert not requests and not body.closed


async def test_post_close_currentness_is_required(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(8), hold_close=True)
    transports, _ = install_http(monkeypatch, body)
    task = asyncio.create_task(endpoint.measure_prompt([], lease))
    await body.closing.wait()
    endpoint.ready = None
    body.release_close.set()
    with pytest.raises(ValueError, match="ready"):
        await task
    assert body.closed and transports[0].closed


async def test_errors_do_not_leak_input_or_paths(endpoint, monkeypatch, tmp_path):
    lease = retain(endpoint)
    secret = "SECRET_INPUT_ab12cd34"
    raw = b'{"backend":"BACKEND_SECRET_xyz","input_tokens":"bad","object":"x"}'
    body = Body(raw)
    install_http(monkeypatch, body)
    with pytest.raises((ValueError, ProviderError)) as excinfo:
        await endpoint.measure_prompt([{"role": "user", "content": secret}], lease)
    text = str(excinfo.value)
    assert secret not in text
    assert "BACKEND_SECRET_xyz" not in text
    assert str(tmp_path) not in text
    assert str(endpoint.directory) not in text


async def test_protocol_wraps_count_with_exact_digests(protocol, endpoint, monkeypatch):
    lease = retain(endpoint)
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hello"},
    ]
    body = Body(count_body(17))
    _, requests = install_http(monkeypatch, body)
    result = await protocol.measure_prompt(messages, lease.identity, lease)
    assert result.input_tokens == 17
    assert result.context_tokens == 2048
    assert result.reserved_output_tokens == 128
    assert result.fits is True
    expected_body = {
        "model": "synthetic",
        "messages": messages,
        "max_tokens": 128,
        "stream": True,
    }
    assert json.loads(requests[0].content) == expected_body
    assert result.request_sha256 == request_digest(expected_body)
    expected_binding = {
        "provider": "managed-gguf",
        "model": "synthetic-model",
        "profile": "synthetic-profile",
        "manifest_sha256": "b" * 64,
        "context_tokens": 2048,
        "max_new_tokens": 128,
    }
    assert result.binding_sha256 == binding_digest(expected_binding)
    wire = result.to_wire()
    assert wire["version"] == 1 and wire["input_tokens"] == 17


async def test_protocol_overbudget_count_reports_unfit(protocol, endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(2040))
    install_http(monkeypatch, body)
    result = await protocol.measure_prompt(
        [{"role": "user", "content": "hi"}], lease.identity, lease
    )
    # 2040 + 128 > 2048, so the snapshot does not fit.
    assert result.input_tokens == 2040
    assert result.fits is False


@pytest.mark.parametrize("mutation", ["messages", "limits", "alias", "manifest_pin"])
async def test_protocol_refuses_changed_snapshot_after_native_close(
    protocol, endpoint, monkeypatch, mutation
):
    lease = retain(endpoint)
    messages = [{"role": "user", "content": "Original public request"}]
    body = Body(count_body(17), hold_close=True)
    transports, requests = install_http(monkeypatch, body)
    task = asyncio.create_task(protocol.measure_prompt(messages, lease.identity, lease))
    try:
        await body.closing.wait()
        sent = json.loads(requests[0].content)
        assert sent["messages"] == messages
        if mutation == "messages":
            messages[0]["content"] = "Different public request, not measured"
        elif mutation == "limits":
            endpoint.limits.update(context_tokens=4096, max_new_tokens=256)
        elif mutation == "alias":
            endpoint.manifest["alias"] = "replacement"
        else:
            protocol.manifest_sha256 = "c" * 64
        body.release_close.set()
        with pytest.raises(ValueError, match="binding changed"):
            await task
        assert body.closed and transports[0].closed and not endpoint.exchanges
        assert len(requests) == 1
    finally:
        body.release_close.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_protocol_unsupported_stays_none(protocol, endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(b"gone", hold_close=False)
    transports, _ = install_http(monkeypatch, body, status=404)
    assert await protocol.measure_prompt([], lease.identity, lease) is None
    assert body.closed and transports[0].closed


async def test_protocol_does_not_generate_retry_or_guess(protocol, endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body(count_body(4))
    _, requests = install_http(monkeypatch, body)
    original_stream = endpoint.stream
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        async with aclosing(original_stream(*args, **kwargs)) as source:
            async for item in source:
                yield item

    monkeypatch.setattr(endpoint, "stream", forbidden)
    result = await protocol.measure_prompt(
        [{"role": "user", "content": "hi"}], lease.identity, lease
    )
    assert result.input_tokens == 4
    assert calls == []
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions/input_tokens"


def test_protocol_resident_delegates_without_admit_start_load(protocol):
    seen = []

    class Coordinator:
        def resident_text(self, policy):
            seen.append(policy)
            return SimpleNamespace(lease="resident-lease")

        def admit_text(self, policy):  # pragma: no cover - must not be called
            raise AssertionError("admit must not be called")

        def text_policy(self):  # pragma: no cover - must not be called
            raise AssertionError("policy must not be called")

    policy = object()
    result = protocol.resident(Coordinator(), policy)
    assert result.lease == "resident-lease"
    assert seen == [policy]


async def test_stream_still_uses_shared_body(endpoint, monkeypatch):
    lease = retain(endpoint)
    chunks = [
        b'data: {"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    body = Body(chunks)
    _, requests = install_http(monkeypatch, body, headers={"content-type": "text/event-stream"})
    seen = []
    async with aclosing(endpoint.stream([{"role": "user", "content": "hi"}], lease)) as source:
        async for chunk in source:
            seen.append(chunk)
    assert b"[DONE]" in seen[-1]
    assert json.loads(requests[0].content) == endpoint._body([{"role": "user", "content": "hi"}])
