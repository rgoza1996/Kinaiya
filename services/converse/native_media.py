"""Converse's request/reply projection for the native bounded PCM lane.

Session supplies already admitted original media and authenticated owner text.
This module confers no source, publication, model or Agent authority.
"""

import re

from packages.providers.llama_media import (
    LlamaMediaChatResult,
    LlamaMediaPart,
    LlamaMediaRequestSpec,
    build_request,
)
from packages.providers.text import ProviderRefusal
from services.converse.state_guard import UNVERIFIED_STATE_REPLY, negative_runtime_state_claim


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
    # Preserve the ordinary Converse publication guards. These bounded English
    # predicates do not qualify the raw model or prove all paraphrases safe.
    text = result.text
    if re.search(r"\b(done|completed?|finished|sent|saved|created|deleted|verified)\b", text, re.I):
        text = "The Agent has not provided a verified result for that claim."
    elif negative_runtime_state_claim(text):
        text = UNVERIFIED_STATE_REPLY
    return {
        "text": text,
        "final": True,
        "content_scope": "local_only",
        "native_media_v2": provenance,
    }
