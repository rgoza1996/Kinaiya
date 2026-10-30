"""Optional prompt measurement; importing this module loads no inference runtime."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from packages.providers.text import ProviderError

MAX_COUNT_BYTES = 4096
MAX_SAFE_COUNT = 2**53 - 1
_BINDING_FIELDS = (
    "provider",
    "model",
    "profile",
    "manifest_sha256",
    "context_tokens",
    "max_new_tokens",
)


class PromptCountUnavailable(ProviderError):
    """No trustworthy optional measurement; never substitute a guessed count."""


def request_digest(body):
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def binding_digest(binding):
    # The endpoint address is client-side configuration, not native tokenizer
    # identity. The model alias is covered by the exact request digest.
    return request_digest({key: binding[key] for key in _BINDING_FIELDS})


@dataclass(frozen=True)
class PromptTokenCount:
    input_tokens: int
    context_tokens: int
    reserved_output_tokens: int
    request_sha256: str
    binding_sha256: str

    def __post_init__(self):
        if (
            type(self.input_tokens) is not int
            or not 1 <= self.input_tokens <= MAX_SAFE_COUNT
            or type(self.context_tokens) is not int
            or not 64 <= self.context_tokens <= MAX_SAFE_COUNT
            or type(self.reserved_output_tokens) is not int
            or not 16 <= self.reserved_output_tokens < self.context_tokens
            or self.reserved_output_tokens > 2048
            or any(
                type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in (self.request_sha256, self.binding_sha256)
            )
        ):
            raise PromptCountUnavailable("Prompt measurement is invalid")

    @property
    def fits(self):
        return self.input_tokens + self.reserved_output_tokens <= self.context_tokens

    def to_wire(self):
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_wire(cls, value, *, body, binding):
        if (
            type(value) is not dict
            or set(value) != {"version", *cls.__dataclass_fields__}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise PromptCountUnavailable("Prompt measurement is invalid")
        result = cls(**{key: value[key] for key in cls.__dataclass_fields__})
        if (
            result.request_sha256 != request_digest(body)
            or result.binding_sha256 != binding_digest(binding)
            or result.context_tokens != binding["context_tokens"]
            or result.reserved_output_tokens != binding["max_new_tokens"]
        ):
            raise PromptCountUnavailable("Prompt measurement binding changed")
        return result
