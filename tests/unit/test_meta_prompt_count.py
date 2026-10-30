"""Exact count requests and real transport ownership, with no provider calls."""

import asyncio
import json

import httpx
import pytest

from packages.providers.prompt_budget import PromptCountUnavailable, request_digest
from packages.providers.text import MetaResponsesProvider, ProviderCleanupError
from services.resources.providers import ResourceBoundProvider
from services.resources.state import ResourceState

MESSAGES = [
    {"role": "system", "content": "Public personality."},
    {"role": "user", "content": "Public synthetic question 🌙."},
]


class Body(httpx.AsyncByteStream):
    def __init__(self, raw, held=False, fail=False):
        self.raw, self.fail, self.closed = raw, fail, 0
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        if not held:
            self.release.set()

    async def __aiter__(self):
        for part in range(0, len(self.raw), 3):
            yield self.raw[part : part + 3]

    async def aclose(self):
        self.closed += 1
        self.closing.set()
        await self.release.wait()
        if self.fail:
            raise OSError("private synthetic error")


def provider(body=None, status=200, headers=None, **kwargs):
    calls = []
    body = body or Body(b'{"object":"response.input_tokens","input_tokens":123}')

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers=headers, stream=body)

    return (
        MetaResponsesProvider(
            base_url="https://synthetic.invalid/v1",
            api_key="synthetic-api-key",
            allow_cloud=True,
            context_tokens=8192,
            max_tokens=128,
            reasoning_effort="minimal",
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
        calls,
        body,
    )


@pytest.mark.parametrize("structured", [False, True])
async def test_exact_detached_generation_body_and_no_generation(structured):
    p, calls, body = provider(structured_actions=structured)
    try:
        expected = json.loads(json.dumps(p.body(MESSAGES, True)))
        measured = await p.measure_prompt(MESSAGES)
        assert len(calls) == 1 and calls[0].url.path == "/v1/responses/input_tokens"
        assert json.loads(calls[0].content) == expected
        assert measured.request_sha256 == request_digest(expected)
        assert (
            measured.input_tokens,
            measured.context_tokens,
            measured.reserved_output_tokens,
        ) == (123, 8192, 128)
        assert body.closed == 1
    finally:
        await p.close()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"object":"response.input_tokens","input_tokens":0}',
        b'{"object":"response.input_tokens","input_tokens":true}',
        b'{"object":"response.input_tokens","input_tokens":9007199254740992}',
        b'{"object":"response.input_tokens","input_tokens":1,"input_tokens":2}',
        b'{"object":"response.input_tokens","input_tokens":NaN}',
        b'{"object":"other","input_tokens":1}',
        b"{}",
        b"\xff",
        b" " * 4097,
    ],
)
async def test_invalid_count_is_refused_after_close(raw):
    p, calls, body = provider(Body(raw))
    try:
        with pytest.raises(PromptCountUnavailable):
            await p.measure_prompt(MESSAGES)
        assert body.closed == 1 and len(calls) == 1
    finally:
        await p.close()


@pytest.mark.parametrize(
    "status,headers",
    [
        (302, {"location": "https://foreign.invalid"}),
        (404, {}),
        (200, {"content-encoding": "gzip"}),
    ],
)
async def test_status_compression_never_retries(status, headers):
    p, calls, body = provider(status=status, headers=headers)
    try:
        with pytest.raises(PromptCountUnavailable):
            await p.measure_prompt(MESSAGES)
        assert body.closed == 1 and len(calls) == 1
    finally:
        await p.close()


async def test_cancel_retains_actual_close_and_never_returns_count():
    p, _, body = provider(Body(b'{"object":"response.input_tokens","input_tokens":1}', held=True))
    task = asyncio.create_task(p.measure_prompt(MESSAGES))
    try:
        await asyncio.wait_for(body.closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and body.closed == 1
        body.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.close()


async def test_failed_close_is_not_a_valid_count():
    p, calls, _ = provider(Body(b'{"object":"response.input_tokens","input_tokens":1}', fail=True))
    try:
        with pytest.raises(ProviderCleanupError):
            await p.measure_prompt(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await p.measure_prompt(MESSAGES)
        assert len(calls) == 1
    finally:
        await p.close()


@pytest.mark.parametrize("change", ["messages", "model", "context", "headers", "resource"])
async def test_mutation_during_real_close_invalidates_count(tmp_path, change):
    p, _, body = provider(Body(b'{"object":"response.input_tokens","input_tokens":1}', held=True))
    policy = ResourceState(tmp_path / "policy.json")
    policy.save(phase="ready", profile="configured_hybrid")
    wrapped = ResourceBoundProvider(p, policy.path, "converse")
    messages = [dict(m) for m in MESSAGES]
    task = asyncio.create_task(wrapped.measure_prompt(messages))
    try:
        await asyncio.wait_for(body.closing.wait(), 1)
        if change == "messages":
            messages[-1]["content"] = "replacement"
        elif change == "model":
            p.model = "replacement"
        elif change == "context":
            p.context_tokens = 4096
        elif change == "headers":
            p.http.headers["authorization"] = "Bearer replacement-synthetic-key"
        else:
            policy.save(phase="off", desired_converse=False)
        body.release.set()
        with pytest.raises(PromptCountUnavailable):
            await task
    finally:
        body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.close()
        policy.close()


async def test_local_only_policy_blocks_cloud_before_count(tmp_path):
    p, calls, _ = provider()
    policy = ResourceState(tmp_path / "policy.json", profile="local_only")
    policy.save(phase="ready")
    try:
        with pytest.raises(PromptCountUnavailable):
            await ResourceBoundProvider(p, policy.path, "converse").measure_prompt(MESSAGES)
        assert calls == []
    finally:
        await p.close()
        policy.close()


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "oversize", "duplicate"])
def test_scoped_secret_reference_is_bounded_and_nofollow(tmp_path, monkeypatch, unsafe):
    from packages.providers.text import ProviderError, _meta_credential

    root = tmp_path / "config"
    folder = root / "muse"
    folder.mkdir(parents=True)
    path = folder / "auth.json"
    path.write_text('{"providers":{"meta":{"api_key":"public-synthetic-secret"}}}')
    path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    assert _meta_credential() == "public-synthetic-secret"
    if unsafe == "symlink":
        target = path.with_name("target.json")
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "oversize":
        path.write_bytes(b" " * 65537)
    else:
        path.write_text('{"providers":{},"providers":{"meta":{"api_key":"synthetic"}}}')
    with pytest.raises(ProviderError, match="credential is unavailable"):
        _meta_credential()
