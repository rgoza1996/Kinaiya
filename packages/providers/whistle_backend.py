"""Offline batch derivative of pinned Apache-2.0 needle/agent/whistle.py.

See whistle_NOTICE.txt and whistle_LICENSE.txt. Wrapper3.2.1 intentionally
selects engine3.3.3; pinned opaque binary identity is not source-build proof.
Only the isolated direct child constructs this native process-global engine.

Streaming seam: single-thread/process-global ownership. The child caller
serializes all native calls. No lock, no thread support, no handle/start/abort.
Native start is implicit on first process; stop flushes once. Any failure after
a process/stop dispatch permanently taints the engine; the trusted owner must
kill/reap the process. No needle_reset, no stop-as-cancel, no retry/replay.
"""

import array
import ctypes
import os
import struct

from packages.providers.whistle_assets import MANIFEST, sealed_asset
from packages.providers.whistle_audio import decode_wav
from packages.providers.whistle_ipc import decode_json
from packages.providers.whistle_stream import (
    MAX_CHUNK_SAMPLES,
    MAX_TOTAL_SAMPLES,
    parse_native_result,
)

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
    def __init__(self, weights, library, workspace, *, enable_streaming=False):
        if type(enable_streaming) is not bool:
            raise TypeError("Whistle enable_streaming requires a literal bool")
        import resource

        self._stream_enabled = enable_streaming
        self._stream_active = False
        self._tainted = False
        self._received_samples = 0
        self._committed_text = ""
        self._stream_started = False
        with (
            sealed_asset(weights, MANIFEST["weights"], workspace) as weight_fd,
            sealed_asset(library, MANIFEST["library"], workspace) as library_fd,
        ):
            # File-size limits cover memfd writes too. Prepare the exact sealed
            # inputs first, then prohibit growth before any native constructor
            # or needle_load can execute. Failure closes both inputs/refuses load.
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
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
            if self._stream_enabled:
                try:
                    proc = lib.needle_stream_transcribe_process
                    stop = lib.needle_stream_transcribe_stop
                except AttributeError:
                    raise RuntimeError("Whistle stream binding failed") from None
                proc.argtypes = [
                    ctypes.POINTER(ctypes.c_float),
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_int,
                ]
                proc.restype = ctypes.c_int
                stop.argtypes = [ctypes.c_char_p, ctypes.c_int]
                stop.restype = ctypes.c_int
            weights_bytes = os.read(weight_fd, MANIFEST["weights"]["bytes"])
            if (
                len(weights_bytes) != MANIFEST["weights"]["bytes"]
                or lib.needle_load(weights_bytes, len(weights_bytes)) < 0
            ):
                raise RuntimeError("Whistle native load failed")
        self.lib = lib

    def transcribe(self, data):
        if self._tainted:
            raise RuntimeError("Whistle engine tainted")
        if self._stream_active:
            raise RuntimeError("Whistle batch refused during active stream")
        samples = decode_wav(data)
        pcm = (ctypes.c_float * len(samples)).from_buffer(samples)
        output = ctypes.create_string_buffer(MAX_RESPONSE_BYTES)
        if self.lib.needle_transcribe(pcm, len(samples), None, None, 0, output, len(output)) < 0:
            raise RuntimeError("Whistle native transcription failed")
        return transcript(output.value)

    def begin_stream(self):
        if self._tainted:
            raise RuntimeError("Whistle engine tainted")
        if not self._stream_enabled:
            raise RuntimeError("Whistle streaming is disabled")
        if self._stream_active:
            raise RuntimeError("Whistle stream already active")
        self._stream_active = True
        self._received_samples = 0
        self._committed_text = ""
        self._stream_started = False
        return None

    def stream_pcm(self, pcm16: bytes):
        if self._tainted:
            raise RuntimeError("Whistle engine tainted")
        if not self._stream_enabled or not self._stream_active:
            raise RuntimeError("Whistle stream not active")
        if type(pcm16) is not bytes:
            raise TypeError("Whistle stream requires bytes")
        if len(pcm16) == 0 or len(pcm16) % 2 == 1:
            raise ValueError("Whistle stream requires even nonempty PCM16")
        count = len(pcm16) // 2
        if not 1 <= count <= MAX_CHUNK_SAMPLES:
            raise ValueError("Whistle stream chunk out of bounds")
        if self._received_samples + count > MAX_TOTAL_SAMPLES:
            raise ValueError("Whistle stream exceeds its bound")
        try:
            ints = struct.unpack("<" + "h" * count, pcm16)
        except struct.error:
            raise ValueError("Whistle stream requires PCM16") from None
        floats = array.array("f", (v / 32768.0 for v in ints))
        pcm = (ctypes.c_float * count).from_buffer(floats)
        total = self._received_samples + count
        prev = self._committed_text
        output = ctypes.create_string_buffer(MAX_RESPONSE_BYTES)
        try:
            code = self.lib.needle_stream_transcribe_process(
                pcm, count, None, None, output, len(output)
            )
            parsed = parse_native_result(
                bytes(output.value),
                total_samples=total,
                prev_committed=prev,
                return_code=code,
                is_final=False,
            )
        except BaseException:
            self._tainted = True
            raise RuntimeError("Whistle stream failed") from None
        self._received_samples = total
        self._committed_text = parsed["candidate"]
        self._stream_started = True
        return {
            "delta_text": parsed["delta_text"],
            "committed_text": parsed["candidate"],
            "pending_text": parsed["pending_text"],
            "received_samples": total,
            "final": False,
            "native_pass_ms": parsed["pass_ms"],
            "language": parsed["language"],
        }

    def finish_stream(self):
        if self._tainted:
            raise RuntimeError("Whistle engine tainted")
        if not self._stream_enabled or not self._stream_active:
            raise RuntimeError("Whistle stream not active")
        if not self._stream_started:
            self._stream_active = False
            self._received_samples = 0
            self._committed_text = ""
            return {
                "delta_text": "",
                "committed_text": "",
                "pending_text": "",
                "received_samples": 0,
                "final": True,
                "native_pass_ms": 0,
                "language": None,
            }
        total = self._received_samples
        prev = self._committed_text
        output = ctypes.create_string_buffer(MAX_RESPONSE_BYTES)
        try:
            code = self.lib.needle_stream_transcribe_stop(output, len(output))
            parsed = parse_native_result(
                bytes(output.value),
                total_samples=total,
                prev_committed=prev,
                return_code=code,
                is_final=True,
            )
        except BaseException:
            self._tainted = True
            raise RuntimeError("Whistle stream failed") from None
        self._committed_text = parsed["candidate"]
        committed = parsed["candidate"]
        result = {
            "delta_text": parsed["delta_text"],
            "committed_text": committed,
            "pending_text": parsed["pending_text"],
            "received_samples": total,
            "final": True,
            "native_pass_ms": parsed["pass_ms"],
            "language": parsed["language"],
        }
        self._stream_active = False
        self._received_samples = 0
        self._committed_text = ""
        self._stream_started = False
        return result
