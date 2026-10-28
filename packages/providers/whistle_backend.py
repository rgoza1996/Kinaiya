"""Offline batch derivative of pinned Apache-2.0 needle/agent/whistle.py.

See whistle_NOTICE.txt and whistle_LICENSE.txt. Wrapper3.2.1 intentionally
selects engine3.3.3; pinned opaque binary identity is not source-build proof.
Only the isolated direct child constructs this native process-global engine.
"""

import ctypes
import os

from packages.providers.whistle_assets import MANIFEST, sealed_asset
from packages.providers.whistle_audio import decode_wav
from packages.providers.whistle_ipc import decode_json

MAX_RESPONSE_BYTES = 128 * 1024
MAX_TEXT_CHARS = 16000


def transcript(raw):
    if not isinstance(raw, bytes) or len(raw) >= MAX_RESPONSE_BYTES:
        raise ValueError("Whistle response exceeds its bound")
    result = decode_json(raw, MAX_RESPONSE_BYTES)
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("text"), str)
        or len(result["text"]) > MAX_TEXT_CHARS
    ):
        raise ValueError("Invalid bounded Whistle transcript")
    result["text"].encode("utf-8")
    return result["text"]


class WhistleEngine:
    def __init__(self, weights, library, workspace):
        with (
            sealed_asset(weights, MANIFEST["weights"], workspace) as weight_fd,
            sealed_asset(library, MANIFEST["library"], workspace) as library_fd,
        ):
            lib = ctypes.CDLL(f"/proc/self/fd/{library_fd}")
            lib.needle_load.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
            lib.needle_load.restype = ctypes.c_int
            lib.needle_transcribe.argtypes = [
                ctypes.POINTER(ctypes.c_float),
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
            ]
            lib.needle_transcribe.restype = ctypes.c_int
            weights_bytes = os.read(weight_fd, MANIFEST["weights"]["bytes"])
            if (
                len(weights_bytes) != MANIFEST["weights"]["bytes"]
                or lib.needle_load(weights_bytes, len(weights_bytes)) < 0
            ):
                raise RuntimeError("Whistle native load failed")
        self.lib = lib

    def transcribe(self, data):
        samples = decode_wav(data)
        pcm = (ctypes.c_float * len(samples)).from_buffer(samples)
        output = ctypes.create_string_buffer(MAX_RESPONSE_BYTES)
        if self.lib.needle_transcribe(pcm, len(samples), None, None, 0, output, len(output)) < 0:
            raise RuntimeError("Whistle native transcription failed")
        return transcript(output.value)
