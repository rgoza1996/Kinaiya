"""Closed native proof for the narrowly retryable auxiliary count boundary.

This is never inferred from a generic provider exception, elapsed deadline or
missing output. Generation transport failures deliberately have no such proof.
"""

import re

from packages.providers.text import ProviderError


class PreGenerationUnavailable(ProviderError):
    def __init__(self, code, request_sha256=None):
        if code not in {
            "endpoint_refused",
            "model_not_loaded",
            "count_rate_limited",
            "count_unavailable",
        }:
            raise ValueError("Unsupported native pre-generation proof")
        if request_sha256 is not None and (
            type(request_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", request_sha256) is None
        ):
            raise ValueError("Invalid native request identity")
        super().__init__("Native count unavailable before generation; operation cleanup settled")
        self.code = code
        self.request_sha256 = request_sha256
        self.phase = "before_generation"
        self.generation_started = False
        self.output_started = False
        self.cleanup_state = "settled"
