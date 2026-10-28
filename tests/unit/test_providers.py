import asyncio
import json

import httpx
import pytest

from packages.providers.text import (
    CompatibleTextProvider,
    MetaResponsesProvider,
    ProviderError,
    owned_http_stream_cleanup,
)


async def test_meta_actions_are_constrained_and_commentary_cannot_break_json():
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        action = {
            "tool": "write_file",
            "arguments": {"path": "result.txt", "content": "7", "command": None},
        }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "I will inspect it."}],
                    },
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": json.dumps(action)}],
                    },
                ],
            },
        )

    provider = MetaResponsesProvider(
        base_url="https://provider.invalid/v1",
        api_key="synthetic-key",
        allow_cloud=True,
        structured_actions=True,
        transport=httpx.MockTransport(respond),
    )
    try:
        text = await provider.complete([{"role": "user", "content": "Synthetic fixture"}])
        action = json.loads(text)
        assert action["arguments"] == {"path": "result.txt", "content": "7"}
        format = observed[0]["text"]["format"]
        assert format["strict"] is True and format["type"] == "json_schema"
        assert format["schema"]["additionalProperties"] is False
        assert "synthetic-key" not in json.dumps(observed)
    finally:
        await provider.close()


async def test_incomplete_provider_response_fails_honestly():
    provider = MetaResponsesProvider(
        api_key="synthetic-key",
        allow_cloud=True,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "incomplete", "output": []})
        ),
    )
    try:
        with pytest.raises(ProviderError, match="did not complete"):
            await provider.complete([{"role": "user", "content": "Synthetic fixture"}])
    finally:
        await provider.close()


@pytest.mark.parametrize("encoded", ['{"values":[1,2,3]}', "[]", "{bad"])
async def test_meta_plugin_adapter_decodes_arguments_and_serializes_history(encoded):
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "tool": "plugin_call",
                                        "arguments": {
                                            "plugin_id": "public_stats",
                                            "plugin_arguments_json": encoded,
                                        },
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    provider = MetaResponsesProvider(
        api_key="synthetic-key",
        allow_cloud=True,
        structured_actions=True,
        transport=httpx.MockTransport(respond),
    )
    history = {
        "tool": "plugin_call",
        "arguments": {
            "plugin_id": "public_stats",
            "arguments": {"values": [1, 2]},
        },
    }
    try:
        messages = [{"role": "assistant", "content": json.dumps(history)}]
        if encoded.startswith('{"values"'):
            result = json.loads(await provider.complete(messages))
            assert result["arguments"] == {
                "plugin_id": "public_stats",
                "arguments": {"values": [1, 2, 3]},
            }
        else:
            with pytest.raises(ProviderError):
                await provider.complete(messages)
        prior = json.loads(
            next(m["content"] for m in observed[0]["input"] if m["role"] == "assistant")
        )
        assert json.loads(prior["arguments"]["plugin_arguments_json"]) == {"values": [1, 2]}
        assert "arguments" not in prior["arguments"]
    finally:
        await provider.close()


def test_local_only_never_silently_discloses_to_cloud():
    with pytest.raises(ProviderError, match="Cloud disclosure"):
        CompatibleTextProvider("https://cloud.invalid/v1", "synthetic")
    with pytest.raises(ProviderError, match="explicit cloud"):
        MetaResponsesProvider(api_key="synthetic-key", allow_cloud=False)
    with pytest.raises(ProviderError, match="HTTPS"):
        CompatibleTextProvider("http://cloud.invalid/v1", "synthetic", allow_cloud=True)


@pytest.mark.parametrize("encoded", ['{"path":"proof","content":"public"}', "[]", "{bad"])
async def test_meta_mcp_actions_decode_from_strict_schema_fields(encoded):
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "tool": "mcp_call",
                                        "arguments": {
                                            "server_id": "synth",
                                            "mcp_tool": "write_text",
                                            "mcp_arguments_json": encoded,
                                            "path": None,
                                        },
                                    }
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    provider = MetaResponsesProvider(
        api_key="synthetic-key",
        allow_cloud=True,
        structured_actions=True,
        transport=httpx.MockTransport(respond),
    )
    try:
        if encoded.startswith('{"path"'):
            result = json.loads(
                await provider.complete([{"role": "user", "content": "public synthetic fixture"}])
            )
            assert result["arguments"] == {
                "server_id": "synth",
                "tool": "write_text",
                "arguments": {"path": "proof", "content": "public"},
            }
            fields = observed[0]["text"]["format"]["schema"]["properties"]["arguments"][
                "properties"
            ]
            assert {"server_id", "mcp_tool", "mcp_arguments_json"} <= fields.keys()
            assert "wire serialization" in observed[0]["input"][0]["content"]
        else:
            with pytest.raises(ProviderError, match="MCP arguments"):
                await provider.complete([{"role": "user", "content": "public synthetic fixture"}])
    finally:
        await provider.close()


@pytest.mark.parametrize("adapter", ["compatible", "meta"])
async def test_http_cleanup_ownership_is_opt_in_and_task_scoped_on_reused_provider(adapter):
    class Bytes(httpx.AsyncByteStream):
        def __init__(self):
            self.closing, self.release = asyncio.Event(), asyncio.Event()
            self.closed = self.cancelled = False

        async def __aiter__(self):
            if adapter == "compatible":
                yield b'data: {"choices":[{"delta":{"content":"Public synthetic reply."}}]}\n\n'
            else:
                yield b'data: {"type":"response.output_text.delta","delta":"Public synthetic reply."}\n\n'
                yield b'data: {"type":"response.completed"}\n\n'

        async def aclose(self):
            self.closing.set()
            try:
                await self.release.wait()
                self.closed = True
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    owned, ordinary = Bytes(), Bytes()
    streams = iter([owned, ordinary])
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=next(streams)))
    provider = (
        CompatibleTextProvider("http://localhost/v1", "synthetic", transport=transport)
        if adapter == "compatible"
        else MetaResponsesProvider(
            base_url="https://synthetic.invalid/v1",
            api_key="synthetic-fixture-key",
            allow_cloud=True,
            transport=transport,
        )
    )

    async def consume():
        return [
            delta async for delta in provider.stream([{"role": "user", "content": "Synthetic"}])
        ]

    async def consume_owned():
        with owned_http_stream_cleanup():
            return await consume()

    tasks = []
    try:
        tasks.append(asyncio.create_task(consume_owned()))
        await asyncio.wait_for(owned.closing.wait(), 1)
        # The same instance serves an unrelated call in an unrelated task.
        # A completed scope in this task must also restore the ordinary default.
        with owned_http_stream_cleanup():
            pass
        tasks.append(asyncio.create_task(consume()))
        await asyncio.wait_for(ordinary.closing.wait(), 1)
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(tasks[1], return_exceptions=True), 0.5)
        assert ordinary.cancelled and not ordinary.closed
        assert not tasks[0].done() and not owned.cancelled
        owned.release.set()
        await asyncio.wait_for(asyncio.gather(tasks[0], return_exceptions=True), 1)
        assert owned.closed and not owned.cancelled
    finally:
        owned.release.set()
        ordinary.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await provider.close()
