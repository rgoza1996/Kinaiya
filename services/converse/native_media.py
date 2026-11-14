"""Converse's request/reply projection for the native bounded PCM lane.

Session supplies already admitted original media and authenticated owner text.
This module confers no source, publication, model or Agent authority.
"""

from packages.providers.llama_media import (
    LlamaMediaChatResult,
    LlamaMediaPart,
    LlamaMediaRequestSpec,
    build_request,
)
from packages.providers.text import ProviderRefusal


def prepare_request(model, part, owner_text, system_prompt):
    return build_request(
        LlamaMediaRequestSpec(
            mode="converse_chat",
            model=model,
            parts=(LlamaMediaPart(kind="text", text=owner_text), part),
            system_prompt=system_prompt,
            max_tokens=128,
        )
    )


def reply_payload(result, provenance):
    if type(result) is not LlamaMediaChatResult:
        raise ProviderRefusal("Native Converse result is unavailable")
    return {
        "text": result.text,
        "final": True,
        "content_scope": "local_only",
        "native_media_v2": provenance,
    }
