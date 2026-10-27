import json

import httpx
import pytest

from packages.providers.text import CompatibleTextProvider, MetaResponsesProvider, ProviderError


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


def test_local_only_never_silently_discloses_to_cloud():
    with pytest.raises(ProviderError, match="Cloud disclosure"):
        CompatibleTextProvider("https://cloud.invalid/v1", "synthetic")
    with pytest.raises(ProviderError, match="explicit cloud"):
        MetaResponsesProvider(api_key="synthetic-key", allow_cloud=False)
    with pytest.raises(ProviderError, match="HTTPS"):
        CompatibleTextProvider("http://cloud.invalid/v1", "synthetic", allow_cloud=True)
