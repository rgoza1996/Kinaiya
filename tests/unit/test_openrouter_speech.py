"""Unit tests for the OpenRouter Kokoro TTS adapter (issue #146).

No credentials, no network, no live budget file: synthetic ``api_key``
values, scripted in-memory transports and a faked ``CharacterBudget``.
Scripted transports keep these checks offline.
"""

import asyncio
import base64

import httpx
import pytest

import packages.providers.openrouter_speech as ors
from packages.providers.speech import SpeechProviderError
from packages.providers.text import ProviderCleanupError


class FakeBudget:
    last = None

    def __init__(self, path, limit_usd=1.0):
        self.path = path
        self.limit_usd = limit_usd
        self.limit = limit_usd
        self.admits = []
        self.finishes = []
        self._n = 0
        FakeBudget.last = self

    def admit(self, text):
        self.admits.append(text)
        self._n += 1
        return f"res-{self._n}"

    def finish(self, rid, generation_id=None):
        self.finishes.append((rid, generation_id))


class ScriptStream(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None, close_error=None):
        self._chunks = [bytes(c) for c in chunks]
        self._error = error
        self.close_error = close_error
        self.close_calls = 0

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def aclose(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class ScriptTransport(httpx.AsyncBaseTransport):
    def __init__(self, status=200, headers=None, chunks=(), error=None, close_error=None):
        self.status = status
        self.headers = headers if headers is not None else [(b"content-type", b"audio/pcm")]
        self.chunks = chunks
        self.error = error
        self.close_error = close_error
        self.requests = []
        self.bodies = []
        self.streams = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        self.bodies.append(await request.aread())
        stream = ScriptStream(self.chunks, error=self.error, close_error=self.close_error)
        self.streams.append(stream)
        return httpx.Response(self.status, headers=self.headers, stream=stream)


def make_client(monkeypatch, transport, **over):
    monkeypatch.setattr(ors, "CharacterBudget", FakeBudget)
    FakeBudget.last = None
    kwargs = dict(
        allow_cloud=True,
        secret_ref="openrouter",
        model=ors.MODEL,
        voice="af_heart",
        base_url="https://openrouter.ai/api/v1",
        timeout_s=120,
        budget_path="/tmp/or-budget-test.json",
        budget_limit_usd=1.0,
    )
    kwargs.update(over)
    return ors.OpenRouterTTSClient(**kwargs, api_key="sk-test-synthetic", transport=transport)


async def _allow():
    return True


def _drain(client, text, **kw):
    kw.setdefault("content_scope", "public_synthetic")
    kw.setdefault("current", _allow)
    return asyncio.run(_collect(client.synthesize_scoped(text, **kw)))


async def _collect(gen):
    return [frame async for frame in gen]


def test_settings_minimal_defaults_and_no_provider_leak():
    out = ors.openrouter_tts_settings(
        {
            "provider": "openrouter",
            "allow_cloud": True,
            "secret_ref": "openrouter",
            "budget_path": "/tmp/b.json",
        }
    )
    assert "provider" not in out
    assert out["model"] == ors.MODEL == "hexgrad/kokoro-82m"
    assert out["voice"] == "af_heart"
    assert out["base_url"] == "https://openrouter.ai/api/v1"
    assert out["timeout_s"] == 120
    assert out["budget_path"] == "/tmp/b.json"
    assert out["budget_limit_usd"] == 1.0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(provider="other"),
        lambda c: c.update(allow_cloud=False),
        lambda c: c.update(secret_ref="meta"),
        lambda c: c.update(model="other/model"),
        lambda c: c.update(voice="af_sky"),
        lambda c: c.update(api_key="sk-leak"),
        lambda c: c.update(transport=None),
        lambda c: c.pop("budget_path"),
        lambda c: c.update(budget_path=""),
        lambda c: c.update(timeout_s=121),
        lambda c: c.update(timeout_s=0),
        lambda c: c.update(base_url="https://example.com/api/v1"),
        lambda c: c.update(base_url="http://openrouter.ai/api/v1"),
        lambda c: c.update(base_url="https://openrouter.ai/api/v1?x=1"),
        lambda c: c.update(base_url="https://openrouter.ai/api/v1#frag"),
        lambda c: c.update(base_url="https://user@openrouter.ai/api/v1"),
        lambda c: c.update(base_url="https://openrouter.ai/other"),
    ],
)
def test_settings_rejects_bad_choice(mutate):
    choice = {
        "provider": "openrouter",
        "allow_cloud": True,
        "secret_ref": "openrouter",
        "budget_path": "/tmp/b.json",
    }
    mutate(choice)
    with pytest.raises(ValueError):
        ors.openrouter_tts_settings(choice)


def test_settings_accepts_loopback_origin():
    out = ors.openrouter_tts_settings(
        {
            "provider": "openrouter",
            "allow_cloud": True,
            "secret_ref": "openrouter",
            "budget_path": "/tmp/b.json",
            "base_url": "http://127.0.0.1:8765/api/v1",
        }
    )
    assert out["base_url"] == "http://127.0.0.1:8765/api/v1"


def test_credential_unavailable_without_key_file(monkeypatch):
    monkeypatch.setattr(ors, "CharacterBudget", FakeBudget)
    monkeypatch.setenv("KINAIYA_OPENROUTER_KEY_FILE", "/nonexistent-or-key-146")
    with pytest.raises(ValueError):
        ors.OpenRouterTTSClient(
            allow_cloud=True,
            secret_ref="openrouter",
            budget_path="/tmp/b.json",
            transport=ScriptTransport(),
        )


def test_client_info_and_profile(monkeypatch):
    client = make_client(monkeypatch, ScriptTransport())
    assert (client.info.provider, client.info.model) == ("openrouter", ors.MODEL)
    assert client.info.local is False and client.info.streaming is True
    assert client.profile == "Kokoro 82M (OpenRouter)"
    asyncio.run(client.close())


def test_request_body_headers_and_frames_are_exact(monkeypatch):
    pcm = bytes([i % 251 for i in range(4800)])  # 2400 samples, even length
    transport = ScriptTransport(chunks=[pcm])
    client = make_client(monkeypatch, transport)
    frames = _drain(client, "hello")
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://openrouter.ai/api/v1/audio/speech"
    import json as _json

    assert _json.loads(transport.bodies[0].decode("utf-8")) == {
        "model": "hexgrad/kokoro-82m",
        "input": "hello",
        "voice": "af_heart",
        "response_format": "pcm",
    }
    assert request.headers["authorization"] == "Bearer sk-test-synthetic"
    assert request.headers["accept-encoding"] == "identity"
    assert b"sk-test-synthetic" not in transport.bodies[0]
    assert frames == [
        {
            "type": "audio",
            "audio": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 24000,
            "samples": 2400,
            "chunk_index": 0,
        }
    ]
    assert set(frames[0]) == {"type", "audio", "sample_rate", "samples", "chunk_index"}
    assert FakeBudget.last.admits == ["hello"]
    assert FakeBudget.last.finishes == [("res-1", None)]
    asyncio.run(client.close())


@pytest.mark.parametrize("scope", ["local_only", None, "bogus", ""])
def test_denied_scope_never_touches_http_or_budget(monkeypatch, scope):
    transport = ScriptTransport(chunks=[b"\x00\x01"])
    client = make_client(monkeypatch, transport)
    with pytest.raises(SpeechProviderError):
        _drain(client, "hi", content_scope=scope)
    assert transport.requests == []
    assert FakeBudget.last.admits == [] and FakeBudget.last.finishes == []
    asyncio.run(client.close())


def test_private_scope_allowed_and_generation_id_settles(monkeypatch):
    transport = ScriptTransport(
        headers=[(b"content-type", b"audio/pcm"), (b"x-generation-id", b"gen_123")],
        chunks=[b"\x01\x02\x03\x04"],
    )
    client = make_client(monkeypatch, transport)
    frames = _drain(client, "hi", content_scope="private_cloud_authorized")
    assert [f["samples"] for f in frames] == [2]
    assert FakeBudget.last.finishes == [("res-1", "gen_123")]
    asyncio.run(client.close())


def test_current_denial_blocks_before_http(monkeypatch):
    transport = ScriptTransport(chunks=[b"\x00\x01"])

    async def deny():
        return False

    client = make_client(monkeypatch, transport)
    with pytest.raises(SpeechProviderError) as exc:
        _drain(client, "hi", current=deny)
    assert exc.value.status_code == 409
    assert transport.requests == [] and FakeBudget.last.admits == []
    asyncio.run(client.close())


def test_current_mutation_of_headers_blocks_midstream(monkeypatch):
    pcm = bytes([7 % 251 for _ in range(96000)])  # two full frames
    transport = ScriptTransport(chunks=[pcm[:48000], pcm[48000:]])
    client = make_client(monkeypatch, transport)
    calls = 0

    async def current():
        nonlocal calls
        calls += 1
        if calls == 6:  # after the first frame was accepted for export
            client.http.headers["Authorization"] = "Bearer tampered"
        return True

    with pytest.raises(SpeechProviderError) as exc:
        _drain(client, "hi", current=current)
    assert exc.value.status_code == 409
    assert FakeBudget.last.admits == ["hi"] and FakeBudget.last.finishes == []
    asyncio.run(client.close())


def test_split_tail_pcm_reassembles_valid_frames(monkeypatch):
    pcm = bytes([i % 251 for i in range(6)])  # 3 samples split across chunks
    transport = ScriptTransport(chunks=[pcm[:1], pcm[1:3], pcm[3:]])
    client = make_client(monkeypatch, transport)
    frames = _drain(client, "hi")
    assert len(frames) == 1 and frames[0]["samples"] == 3
    assert frames[0]["chunk_index"] == 0
    assert base64.b64decode(frames[0]["audio"]) == pcm
    assert FakeBudget.last.finishes == [("res-1", None)]
    asyncio.run(client.close())


def test_large_stream_splits_into_bounded_frames(monkeypatch):
    pcm = bytes([i % 251 for i in range(48002)])  # 24001 samples
    transport = ScriptTransport(chunks=[pcm])
    client = make_client(monkeypatch, transport)
    frames = _drain(client, "hi")
    assert [f["samples"] for f in frames] == [2400] * 10 + [1]
    assert [f["chunk_index"] for f in frames] == list(range(11))
    assert b"".join(base64.b64decode(f["audio"]) for f in frames) == pcm
    asyncio.run(client.close())


@pytest.mark.parametrize(
    "headers,chunks",
    [
        ([(b"content-type", b"application/json")], [b'{"error": "x"}']),
        ([(b"content-type", b"audio/pcm; rate=16000")], [b"\x00\x01"]),
        ([(b"content-type", b"audio/pcm; channels=2")], [b"\x00\x01"]),
        ([(b"content-type", b"audio/pcm; foo=1")], [b"\x00\x01"]),
        ([(b"content-type", b"text/plain")], [b"\x00\x01"]),
        ([(b"content-type", b"audio/pcm"), (b"content-encoding", b"gzip")], [b"\x00\x01"]),
        ([(b"content-type", b"audio/pcm")], []),
        ([(b"content-type", b"audio/pcm")], [b"\x01\x02\x03"]),
        ([(b"content-type", b"audio/pcm"), (b"content-length", b"4")], [b"\x00\x01"]),
        ([(b"content-type", b"audio/pcm"), (b"content-length", b"abc")], [b"\x00\x01"]),
        ([(b"content-type", b"audio/pcm"), (b"content-length", b"99999999")], [b"\x00\x01"]),
    ],
)
def test_malformed_audio_rejected_without_settle(monkeypatch, headers, chunks):
    transport = ScriptTransport(headers=headers, chunks=chunks)
    client = make_client(monkeypatch, transport)
    with pytest.raises(SpeechProviderError):
        _drain(client, "hi")
    assert FakeBudget.last.admits == ["hi"] and FakeBudget.last.finishes == []
    asyncio.run(client.close())


def test_oversized_stream_rejected(monkeypatch):
    transport = ScriptTransport(chunks=[b"\x00" * 60000] * 100)
    client = make_client(monkeypatch, transport)
    with pytest.raises(SpeechProviderError) as exc:
        _drain(client, "hi")
    assert exc.value.status_code == 413
    assert FakeBudget.last.finishes == []
    asyncio.run(client.close())


@pytest.mark.parametrize("status", [307, 401, 429])
def test_redirect_and_error_statuses_rejected(monkeypatch, status):
    transport = ScriptTransport(
        status=status,
        headers=[(b"content-type", b"audio/pcm"), (b"location", b"https://x/")],
        chunks=[b"\x00\x01"],
    )
    client = make_client(monkeypatch, transport)
    with pytest.raises(SpeechProviderError):
        _drain(client, "hi")
    assert FakeBudget.last.admits == ["hi"] and FakeBudget.last.finishes == []
    asyncio.run(client.close())


def test_incomplete_http_stream_rejected(monkeypatch):
    transport = ScriptTransport(chunks=[b"\x00\x01"], error=httpx.StreamError("cut"))
    client = make_client(monkeypatch, transport)
    with pytest.raises(SpeechProviderError):
        _drain(client, "hi")
    assert FakeBudget.last.finishes == []
    asyncio.run(client.close())


def test_cancellation_cleanup_failure_remains_owned(monkeypatch):
    gate = asyncio.Event()

    class HangStream(httpx.AsyncByteStream):
        def __init__(self):
            self.close_calls = 0

        async def __aiter__(self):
            yield b"\x00\x01" * 2400
            await gate.wait()

        async def aclose(self):
            self.close_calls += 1
            raise RuntimeError("close failed")

    class HangTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.requests = []
            self.stream = HangStream()

        async def handle_async_request(self, request):
            self.requests.append(request)
            return httpx.Response(
                200, headers=[(b"content-type", b"audio/pcm")], stream=self.stream
            )

    transport = HangTransport()
    client = make_client(monkeypatch, transport)

    async def main():
        gen = client.synthesize_scoped("hi", content_scope="public_synthetic", current=_allow)
        first = await gen.__anext__()
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.call_soon(task.cancel)
        try:
            async for _ in gen:
                pass
        except BaseException:
            raise
        return first

    with pytest.raises((SpeechProviderError, ProviderCleanupError, asyncio.CancelledError)):
        asyncio.run(main())
    assert client._cleanup_failed is True
    assert FakeBudget.last.finishes == []
    with pytest.raises(SpeechProviderError):
        _drain(client, "hi")
    asyncio.run(client.close())


def test_synthesize_fails_closed_without_context(monkeypatch):
    transport = ScriptTransport(chunks=[b"\x00\x01"])
    client = make_client(monkeypatch, transport)
    with pytest.raises(SpeechProviderError) as exc:
        asyncio.run(_collect(client.synthesize("hi")))
    assert exc.value.status_code == 409
    assert transport.requests == [] and FakeBudget.last.admits == []
    asyncio.run(client.close())


def test_input_bounds_checked_before_http(monkeypatch):
    transport = ScriptTransport(chunks=[b"\x00\x01"])
    client = make_client(monkeypatch, transport)
    with pytest.raises(SpeechProviderError):
        _drain(client, "")
    with pytest.raises(SpeechProviderError) as exc:
        _drain(client, "x" * 4097)
    assert exc.value.status_code == 413
    assert transport.requests == [] and FakeBudget.last.admits == []
    frames = _drain(client, "x" * 4096)
    assert frames[0]["samples"] == 1
    asyncio.run(client.close())


def test_budget_admit_failure_propagates_without_http(monkeypatch):
    transport = ScriptTransport(chunks=[b"\x00\x01"])
    client = make_client(monkeypatch, transport)

    def boom(text):
        raise RuntimeError("ledger down")

    FakeBudget.last.admit = boom
    with pytest.raises(RuntimeError):
        _drain(client, "hi")
    assert transport.requests == []
    asyncio.run(client.close())


@pytest.mark.parametrize(
    "field", ["transport", "mounts", "redirects", "hooks", "budget_path", "budget_limit"]
)
def test_awaited_currentness_cannot_replace_export_configuration(monkeypatch, field):
    original, replacement = ScriptTransport(chunks=[b"\0\0"]), ScriptTransport(chunks=[b"\0\0"])
    client = make_client(monkeypatch, original)

    async def current():
        await asyncio.sleep(0)
        if field == "transport":
            client.http._transport = replacement
        elif field == "mounts":
            client.http._mounts.clear()
            client.http._mounts[httpx._utils.URLPattern("all://")] = replacement
        elif field == "redirects":
            client.http.follow_redirects = True
        elif field == "hooks":
            client.http.event_hooks["request"].append(lambda request: None)
        elif field == "budget_path":
            client._budget.path = "/tmp/replaced-budget"
        else:
            client._budget.limit = 25
        return True

    with pytest.raises(SpeechProviderError):
        _drain(client, "hello", current=current)
    assert original.requests == replacement.requests == []
    assert FakeBudget.last.admits == []
    asyncio.run(client.close())


@pytest.mark.parametrize("change", ["native_admission", "transport", "headers"])
def test_ledger_io_is_followed_by_native_revalidation_before_export(monkeypatch, change):
    transport, replacement = ScriptTransport(chunks=[b"\0\0"]), ScriptTransport(chunks=[b"\0\0"])
    client = make_client(monkeypatch, transport)
    allowed = True
    admit = client._budget.admit

    def changed(text):
        nonlocal allowed
        reservation = admit(text)
        if change == "native_admission":
            allowed = False
        elif change == "transport":
            client.http._transport = replacement
        else:
            client.http.headers["authorization"] = "Bearer changed-synthetic"
        return reservation

    client._budget.admit = changed
    with pytest.raises(SpeechProviderError):
        _drain(client, "hello", current=lambda: allowed)
    assert FakeBudget.last.admits == ["hello"]
    assert FakeBudget.last.finishes == []
    assert transport.requests == replacement.requests == []
    asyncio.run(client.close())


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-type", b"audio/pcm"), (b"content-length", b"2"), (b"content-length", b"2")],
        [
            (b"content-type", b"audio/pcm"),
            (b"content-length", b"2"),
            (b"transfer-encoding", b"chunked"),
        ],
        [(b"content-type", b"audio/pcm"), (b"transfer-encoding", b"gzip, chunked")],
        [(b"content-type", b"audio/pcm;rate=24000;rate=24000")],
        [(b"content-type", b' audio/pcm;rate="24000')],
        [(b"content-type", b"audio/pcm"), (b"content-type", b"audio/pcm")],
    ],
)
def test_ambiguous_or_malformed_wire_headers_never_settle(monkeypatch, headers):
    client = make_client(monkeypatch, ScriptTransport(headers=headers, chunks=[b"\0\0"]))
    with pytest.raises(SpeechProviderError):
        _drain(client, "hello")
    assert FakeBudget.last.finishes == []
    asyncio.run(client.close())


@pytest.mark.parametrize("prefix", [b"RIFF", b"ID3", b"OggS", b"fLaC", b'{"error":'])
def test_common_containers_and_errors_cannot_masquerade_as_pcm(monkeypatch, prefix):
    pcm = prefix + b"\0" * (4800 - len(prefix))
    client = make_client(monkeypatch, ScriptTransport(chunks=[pcm[:1], pcm[1:]]))
    with pytest.raises(SpeechProviderError):
        _drain(client, "hello")
    assert FakeBudget.last.finishes == []
    asyncio.run(client.close())


def test_successful_response_is_really_closed_once(monkeypatch):
    transport = ScriptTransport(chunks=[b"\0\0"])
    client = make_client(monkeypatch, transport)
    assert _drain(client, "hello")[0]["samples"] == 1
    assert transport.streams[0].close_calls == 1
    asyncio.run(client.close())


def test_repeated_cancellation_retains_real_http_cleanup(monkeypatch):
    class BlockedClose(ScriptStream):
        def __init__(self):
            super().__init__([b"\0\0"])
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def aclose(self):
            self.close_calls += 1
            self.entered.set()
            await self.release.wait()

    stream = BlockedClose()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "audio/pcm"}, stream=stream)
    )
    client = make_client(monkeypatch, transport)

    async def run():
        task = asyncio.create_task(
            _collect(
                client.synthesize_scoped("hello", content_scope="public_synthetic", current=_allow)
            )
        )
        await asyncio.wait_for(stream.entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not FakeBudget.last.finishes
        stream.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert stream.close_calls == 1
        await client.close()

    asyncio.run(run())
