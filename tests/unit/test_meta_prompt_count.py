"""Exact count requests and real transport ownership, with no provider calls."""

import asyncio
import json

import httpx
import pytest

from packages.providers.prompt_budget import PromptCountUnavailable, request_digest
from packages.providers.text import (
    MetaResponsesProvider,
    ProviderCleanupError,
    owned_http_stream_cleanup,
)
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


def generation_provider(*, generation_body=None, count_bodies=None):
    calls = []
    count_bodies = iter(count_bodies) if count_bodies is not None else None
    generation_body = generation_body or Body(
        b'data: {"type":"response.output_text.delta","delta":"Public reply."}\n\n'
        b'data: {"type":"response.completed"}\n\n'
    )

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("input_tokens"):
            body = (
                next(count_bodies)
                if count_bodies is not None
                else Body(b'{"object":"response.input_tokens","input_tokens":123}')
            )
            return httpx.Response(200, stream=body)
        return httpx.Response(200, stream=generation_body)

    return (
        MetaResponsesProvider(
            base_url="https://synthetic.invalid/v1",
            api_key="synthetic-api-key",
            allow_cloud=True,
            context_tokens=4096,
            max_tokens=128,
            reasoning_effort="minimal",
            transport=httpx.MockTransport(respond),
        ),
        calls,
    )


async def collect(provider, messages=MESSAGES):
    return [delta async for delta in provider.stream(messages)]


@pytest.mark.parametrize("structured", [False, True])
async def test_latest_exact_count_authorizes_only_one_generation(structured):
    p, calls = generation_provider()
    p.structured_actions = structured
    try:
        await p.measure_prompt([{"role": "user", "content": "Earlier fitting candidate."}])
        await p.measure_prompt(MESSAGES)
        assert await collect(p) == ["Public reply."]
        assert json.loads(calls[-2].content) == json.loads(calls[-1].content)
        assert json.loads(calls[-1].content)["stream"] is True
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        assert [request.url.path for request in calls] == [
            "/v1/responses/input_tokens",
            "/v1/responses/input_tokens",
            "/v1/responses",
        ]
    finally:
        await p.close()


async def test_fresh_count_is_required_after_stale_generation_refusal():
    p, calls = generation_provider()
    try:
        await p.measure_prompt(MESSAGES)
        p.max_tokens = 2048
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        p.max_tokens = 128
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        assert len(calls) == 1
        await p.measure_prompt(MESSAGES)
        assert await collect(p) == ["Public reply."]
        assert len(calls) == 3
    finally:
        await p.close()


async def test_overflowing_measurement_never_permits_generation():
    p, calls = generation_provider(
        count_bodies=[Body(b'{"object":"response.input_tokens","input_tokens":4096}')]
    )
    try:
        measured = await p.measure_prompt(MESSAGES)
        assert not measured.fits
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        assert len(calls) == 1
    finally:
        await p.close()


@pytest.mark.parametrize(
    "change",
    ["messages", "model", "output", "effort", "structured", "context", "headers", "origin", "info"],
)
async def test_changed_measured_request_refuses_before_generation(change):
    p, calls = generation_provider()
    messages = [dict(message) for message in MESSAGES]
    try:
        await p.measure_prompt(messages)
        if change == "messages":
            messages[-1]["content"] = "Different public request."
        elif change == "model":
            p.model = "replacement"
        elif change == "output":
            p.max_tokens = 2048
        elif change == "effort":
            p.effort = "low"
        elif change == "structured":
            p.structured_actions = True
        elif change == "context":
            p.context_tokens = 8192
        elif change == "headers":
            p.http.headers["authorization"] = "Bearer different-synthetic-key"
        elif change == "origin":
            p.http.base_url = "https://replacement.invalid/v1/"
        else:
            p.info.model = "replacement"
        with pytest.raises(PromptCountUnavailable):
            await collect(p, messages)
        assert len(calls) == 1
        # Restoring a field cannot revive the consumed measurement.
        p.model, p.max_tokens, p.effort = "muse-spark-1.3-contributor", 128, "minimal"
        with pytest.raises(PromptCountUnavailable):
            await collect(p, messages)
        assert len(calls) == 1
    finally:
        await p.close()


async def test_nonstream_generation_cannot_use_stream_count_or_leave_reusable_ticket():
    p, calls = generation_provider()
    try:
        await p.measure_prompt(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await p.complete(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        assert len(calls) == 1
    finally:
        await p.close()


async def test_failed_recount_cannot_reuse_previous_successful_count():
    p, calls = generation_provider(
        count_bodies=[
            Body(b'{"object":"response.input_tokens","input_tokens":123}'),
            Body(b'{"object":"response.input_tokens","input_tokens":false}'),
        ]
    )
    try:
        await p.measure_prompt(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await p.measure_prompt(MESSAGES)
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        assert len(calls) == 2
    finally:
        await p.close()


async def test_older_count_closing_late_cannot_replace_current_ticket():
    older = Body(b'{"object":"response.input_tokens","input_tokens":123}', held=True)
    p, calls = generation_provider(
        count_bodies=[older, Body(b'{"object":"response.input_tokens","input_tokens":124}')]
    )
    task = asyncio.create_task(
        p.measure_prompt([{"role": "user", "content": "Earlier public candidate."}])
    )
    try:
        await asyncio.wait_for(older.closing.wait(), 1)
        await p.measure_prompt(MESSAGES)
        older.release.set()
        with pytest.raises(PromptCountUnavailable):
            await task
        assert await collect(p) == ["Public reply."]
        assert json.loads(calls[-2].content) == json.loads(calls[-1].content)
    finally:
        older.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await p.close()


async def test_generation_ticket_stays_consumed_through_repeated_cancel_and_real_close():
    body = Body(b'data: {"type":"response.completed"}\n\n', held=True)
    p, calls = generation_provider(generation_body=body)

    async def owned():
        with owned_http_stream_cleanup():
            return await collect(p)

    task = None
    try:
        await p.measure_prompt(MESSAGES)
        task = asyncio.create_task(owned())
        await asyncio.wait_for(body.closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and body.closed == 1
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        assert len(calls) == 2
        body.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        assert len(calls) == 2
    finally:
        body.release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await p.close()


async def test_generation_close_failure_cannot_reuse_count_ticket():
    p, calls = generation_provider(
        generation_body=Body(b'data: {"type":"response.completed"}\n\n', fail=True)
    )
    try:
        await p.measure_prompt(MESSAGES)
        with owned_http_stream_cleanup(), pytest.raises(ProviderCleanupError):
            await collect(p)
        with pytest.raises(PromptCountUnavailable):
            await collect(p)
        assert len(calls) == 2
    finally:
        await p.close()


@pytest.mark.parametrize("stream", [False, True])
async def test_uncounted_agent_generation_with_explicit_context_is_unchanged(stream):
    raw = (
        b'data: {"type":"response.output_text.delta","delta":"Public reply."}\n\n'
        b'data: {"type":"response.completed"}\n\n'
        if stream
        else b'{"status":"completed","output":[{"type":"message","content":'
        b'[{"type":"output_text","text":"Public reply."}]}]}'
    )
    p, calls = generation_provider(generation_body=Body(raw))
    try:
        for _ in range(2):
            assert ("".join(await collect(p)) if stream else await p.complete(MESSAGES)) == (
                "Public reply."
            )
        assert len(calls) == 2
        assert all(request.url.path == "/v1/responses" for request in calls)
        assert all(json.loads(request.content)["stream"] is stream for request in calls)
    finally:
        await p.close()


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
