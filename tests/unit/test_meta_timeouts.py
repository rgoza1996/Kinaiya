"""Long Meta generations: synthetic HTTP only, no model or credential access."""

import asyncio
import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from packages.contracts.models import CatalogModelBinding
from packages.providers.prompt_budget import PromptCountUnavailable
from packages.providers.text import (
    MetaResponsesProvider,
    MetaTransportError,
    ProviderCleanupError,
    ProviderError,
    owned_http_stream_cleanup,
)
from services.settings.provider_catalog import MetaProfile, ProviderCatalog
from services.settings.provider_factory import (
    CapturedCatalogRecipe,
    CatalogRecipe,
    open_captured_recipe,
)

MESSAGES = [{"role": "user", "content": "Public synthetic timeout observation."}]
MARKER = "SYNTHETIC_SECRET_URL_HEADER_BODY"
REPLY = {
    "status": "completed",
    "output": [
        {
            "type": "message",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Synthetic result."}],
        }
    ],
}


def adapter(**kwargs):
    return MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="SYNTHETIC_ONLY_CREDENTIAL",
        allow_cloud=True,
        context_tokens=8192,
        transport=kwargs.pop(
            "transport", httpx.MockTransport(lambda _: pytest.fail("No dispatch"))
        ),
        **kwargs,
    )


def profile(**changes):
    return {
        "kind": "meta",
        "base_url": "https://synthetic.invalid/v1",
        "model": "muse-spark-1.3-contributor",
        "allow_cloud": True,
        "credential": {"id": "synthetic", "generation": "a" * 32},
        "max_tokens": 2048,
        "context_tokens": 8192,
        "reasoning_effort": "xhigh",
        **changes,
    }


@pytest.mark.parametrize(
    "timeout", [True, False, None, "600", 0, 0.5, 1801, 10**400, float("inf"), float("nan")]
)
def test_adapter_timeout_is_finite_bounded_and_not_boolean(timeout):
    with pytest.raises(ProviderError, match="between 1 and 1800"):
        adapter(timeout=timeout)


@pytest.mark.parametrize("timeout", [True, False, None, "600", 0, 1801, 600.0])
def test_catalog_timeout_is_a_strict_bounded_integer(timeout):
    with pytest.raises(ValidationError):
        MetaProfile.model_validate(profile(timeout_s=timeout))


@pytest.mark.parametrize("timeout", [1, 600, 900, 1800])
async def test_long_timeout_reaches_http_transport_without_generation(timeout):
    provider = adapter(timeout=timeout)
    try:
        assert provider.http.timeout.read == timeout
        assert provider.http.timeout.connect == min(10, timeout)
        assert provider.http.timeout.pool == min(10, timeout)
        assert provider.http.timeout.write == min(60, timeout)
        assert provider._request_timeout_s == timeout
    finally:
        await provider.close()


async def test_default_is_600_seconds():
    provider = adapter()
    try:
        assert provider.http.timeout.read == provider._request_timeout_s == 600
    finally:
        await provider.close()


def test_historical_catalog_generation_retains_exact_shape_and_digest(tmp_path):
    old = profile()
    encoded = json.dumps(old, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    parsed = MetaProfile.model_validate(old)
    assert parsed.timeout_s == 600
    assert parsed.model_dump(mode="json") == old
    assert (
        MetaProfile.model_validate(profile(timeout_s=900)).model_dump(mode="json")["timeout_s"]
        == 900
    )
    catalog = ProviderCatalog(tmp_path / "protected/providers.sqlite", tmp_path / "workspace")
    try:
        with catalog.db:
            catalog.db.execute("INSERT INTO profiles VALUES ('legacy', 1, 0)")
            catalog.db.execute(
                "INSERT INTO profile_versions VALUES ('legacy', 1, 'Synthetic', ?, ?)",
                (digest, encoded),
            )
        observed = catalog.profile("legacy", 1, for_new=False)
        assert observed["configuration_sha256"] == digest and observed["config"] == old
    finally:
        catalog.db.close()


@pytest.mark.parametrize("timeout", [None, 900])
async def test_catalog_factory_passes_default_or_declared_timeout(timeout):
    config = profile(**({} if timeout is None else {"timeout_s": timeout}))
    binding = CatalogModelBinding(id="synthetic", generation=1, configuration_sha256="b" * 64)
    captured = CapturedCatalogRecipe(
        CatalogRecipe(binding, 1, json.dumps(config), "xhigh"), "synthetic"
    )
    provider = open_captured_recipe(captured)
    try:
        assert provider._request_timeout_s == (600 if timeout is None else timeout)
        assert captured.consumed and captured.key is None
    finally:
        await provider.close()


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize(
    "error,reason",
    [
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.WriteTimeout, "write_timeout"),
        (httpx.PoolTimeout, "pool_timeout"),
        (httpx.TimeoutException, "transport_timeout"),
        (httpx.ConnectError, "transport_error"),
    ],
)
async def test_blank_or_secret_transport_errors_have_closed_reason_and_no_retry(
    prepared, error, reason
):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 17})
        raise error("" if error is httpx.ReadTimeout else MARKER, request=request)

    provider = adapter(transport=httpx.MockTransport(respond))
    try:
        operation = await provider.prepare_complete(MESSAGES) if prepared else None
        with pytest.raises(MetaTransportError) as raised:
            await (operation.complete() if prepared else provider.complete(MESSAGES))
        assert raised.value.reason == reason
        assert raised.value.diagnostic == {
            "stage": "generation_transport",
            "reason": reason,
            "completion": "unknown",
        }
        assert "provider completion is unknown" in str(raised.value)
        assert "no automatic retry" in str(raised.value)
        assert MARKER not in str(raised.value) + json.dumps(raised.value.diagnostic)
        assert calls.count("/v1/responses") == 1
    finally:
        await provider.close()


class HeldStream(httpx.AsyncByteStream):
    def __init__(self):
        self.entered, self.close_entered = asyncio.Event(), asyncio.Event()
        self.release_read, self.release_close = asyncio.Event(), asyncio.Event()
        self.closes = 0

    async def __aiter__(self):
        self.entered.set()
        await self.release_read.wait()
        yield json.dumps(REPLY).encode()

    async def aclose(self):
        self.closes += 1
        self.close_entered.set()
        await self.release_close.wait()


def held_provider(stream, timeout=1):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 17})
        assert request.extensions["timeout"]["read"] == timeout
        return httpx.Response(200, stream=stream)

    return adapter(timeout=timeout, transport=httpx.MockTransport(respond)), calls


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("phase", ["read", "close"])
async def test_total_deadline_retains_owned_close_and_reports_no_false_completion(prepared, phase):
    stream = HeldStream()
    if phase == "close":
        stream.release_read.set()
    provider, calls = held_provider(stream)

    async def complete():
        if prepared:
            operation = await provider.prepare_complete(MESSAGES)
            return await operation.complete()
        return await provider.complete(MESSAGES)

    task = asyncio.create_task(complete())
    try:
        await asyncio.wait_for(stream.entered.wait(), 1)
        await asyncio.wait_for(stream.close_entered.wait(), 2)
        if phase == "close":
            await asyncio.sleep(1.1)
        assert not task.done(), "A deadline must not release an unsettled actual close"
        stream.release_close.set()
        with pytest.raises(MetaTransportError) as raised:
            await task
        assert raised.value.reason == "total_timeout"
        assert calls.count("/v1/responses") == 1 and stream.closes == 1
    finally:
        stream.release_close.set()
        stream.release_read.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.parametrize("prepared", [False, True])
async def test_external_cancel_stays_cancelled_and_retains_original_close(prepared):
    stream = HeldStream()
    provider, calls = held_provider(stream, timeout=600)

    async def complete():
        if prepared:
            operation = await provider.prepare_complete(MESSAGES)
            return await operation.complete()
        return await provider.complete(MESSAGES)

    task = asyncio.create_task(complete())
    try:
        await asyncio.wait_for(stream.entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(stream.close_entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        stream.release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls.count("/v1/responses") == 1 and stream.closes == 1
    finally:
        stream.release_close.set()
        stream.release_read.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


@pytest.mark.parametrize("prepared", [False, True])
async def test_timeout_does_not_hide_unconfirmed_cleanup(prepared):
    class FailedClose(HeldStream):
        async def aclose(self):
            raise RuntimeError(MARKER)

    stream = FailedClose()
    provider, _ = held_provider(stream)
    try:
        with pytest.raises(ProviderCleanupError, match="cleanup is unconfirmed"):
            if prepared:
                operation = await provider.prepare_complete(MESSAGES)
                await operation.complete()
            else:
                await provider.complete(MESSAGES)
    finally:
        await provider.close()


class CancelResistantStream(HeldStream):
    def __init__(self, *, translate_error=False):
        super().__init__()
        self.translate_error = translate_error

    async def __aiter__(self):
        self.entered.set()
        try:
            await self.release_read.wait()
        except asyncio.CancelledError:
            if self.translate_error:
                raise httpx.ReadTimeout(MARKER) from None
        yield json.dumps(REPLY).encode()


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("control", ["deadline", "cancel", "cancel_as_read_error"])
async def test_suppressed_cancellation_cannot_become_a_completed_generation(prepared, control):
    stream = CancelResistantStream(translate_error=control == "cancel_as_read_error")
    provider, calls = held_provider(stream, timeout=1 if control == "deadline" else 600)

    async def complete():
        if prepared:
            operation = await provider.prepare_complete(MESSAGES)
            return await operation.complete()
        return await provider.complete(MESSAGES)

    task = asyncio.create_task(complete())
    try:
        await asyncio.wait_for(stream.entered.wait(), 1)
        if control != "deadline":
            task.cancel()
        await asyncio.wait_for(stream.close_entered.wait(), 2)
        assert not task.done()
        stream.release_close.set()
        if control == "deadline":
            with pytest.raises(MetaTransportError) as raised:
                await task
            assert raised.value.reason == "total_timeout"
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert calls.count("/v1/responses") == 1 and stream.closes == 1
    finally:
        stream.release_read.set()
        stream.release_close.set()
        await asyncio.gather(task, return_exceptions=True)
        await provider.close()


async def test_timeout_change_after_count_cannot_dispatch_measured_generation():
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 17})

    provider = adapter(transport=httpx.MockTransport(respond))
    try:
        operation = await provider.prepare_complete(MESSAGES)
        provider._request_timeout_s = 900
        with pytest.raises(PromptCountUnavailable, match="admission changed"):
            await operation.complete()
        assert calls == ["/v1/responses/input_tokens"]
    finally:
        await provider.close()


async def test_stream_first_delta_and_cross_task_close_keep_existing_protocol():
    class SSEStream(httpx.AsyncByteStream):
        closes = 0

        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"Synthetic"}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            self.closes += 1

    wire = SSEStream()
    provider = adapter(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=wire)))
    stream = provider.stream(MESSAGES)

    async def produce_first():
        with owned_http_stream_cleanup():
            return await anext(stream)

    try:
        assert await asyncio.create_task(produce_first()) == "Synthetic"
        # Converse deliberately closes the generator from a separate task.
        await asyncio.create_task(stream.aclose())
        assert wire.closes == 1
    finally:
        await stream.aclose()
        await provider.close()
