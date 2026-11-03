"""Closed FD3 frame codec for decoder requests and responses.

Frames use a big-endian uint32 header byte length followed by canonical UTF-8
JSON and, for packet responses only, the exact declared binary payload.
Encoding or decoding a frame establishes no process, reservation, or cleanup
authority.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Literal

from packages.contracts import media

_HEADER_BYTES = media.MAX_MEDIA_CONTROL_BYTES
_BODY_BYTES = media.MAX_MEDIA_BODY_BYTES
_ERROR = "Invalid media decoder frame"
_ERROR_CODES = frozenset({"bad_packet", "bound"})


class MediaDecoderWireError(ValueError):
    """Fixed decoder frame boundary classification; never includes untrusted text."""

    def __init__(self, code: Literal["bad_packet", "bound"] = "bad_packet") -> None:
        if type(code) is not str or code not in _ERROR_CODES:
            raise ValueError("Invalid media decoder error code")
        self.code = code
        super().__init__(_ERROR)


def _encode_header(value, expected_type) -> tuple[bytes, object]:
    if type(value) is not expected_type:
        raise MediaDecoderWireError("bad_packet")
    try:
        raw_header = media.media_bytes(value)
        if not 0 < len(raw_header) <= _HEADER_BYTES:
            raise MediaDecoderWireError("bound")
        rebuilt = media.media_json(raw_header, expected_type)
        if media.media_bytes(rebuilt) != raw_header:
            raise MediaDecoderWireError("bad_packet")
        return raw_header, rebuilt
    except MediaDecoderWireError:
        raise
    except media.MediaContractError as exc:
        raise MediaDecoderWireError(exc.code) from None
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise MediaDecoderWireError("bad_packet") from None


def _decode_header(frame: bytes, expected_type) -> tuple[object, int]:
    """Unpack the uint32be prefix and validate canonical JSON before inspecting body/trailing bytes.

    Edge precedence choice: zero/oversize uint32be header lengths (> 65,536) fail
    immediately as ``bound`` before slicing. Once ``1 <= header_size <= 65,536``
    and at least ``4 + header_size`` bytes are present, the bounded header slice
    is validated through ``media.media_json`` before trailing/body length checks so
    nested DTO ``bound`` violations preserve their contract classification.
    """
    if type(frame) is not bytes or len(frame) < 4:
        raise MediaDecoderWireError("bad_packet")
    header_size = struct.unpack(">I", frame[:4])[0]
    if header_size == 0 or header_size > _HEADER_BYTES:
        raise MediaDecoderWireError("bound")
    body_offset = 4 + header_size
    if len(frame) < body_offset:
        raise MediaDecoderWireError("bad_packet")
    raw_header = frame[4:body_offset]
    try:
        parsed = media.media_json(raw_header, expected_type)
        if media.media_bytes(parsed) != raw_header:
            raise MediaDecoderWireError("bad_packet")
        return parsed, body_offset
    except MediaDecoderWireError:
        raise
    except media.MediaContractError as exc:
        raise MediaDecoderWireError(exc.code) from None
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise MediaDecoderWireError("bad_packet") from None


def decoder_request_bytes(value: media.MediaDecodeRequest) -> bytes:
    """Encode a revalidated ``MediaDecodeRequest`` as a closed FD3 frame."""
    raw_header, _ = _encode_header(value, media.MediaDecodeRequest)
    return struct.pack(">I", len(raw_header)) + raw_header


def decoder_request_json(frame: bytes) -> media.MediaDecodeRequest:
    """Decode a complete closed FD3 request frame with no body or trailing bytes."""
    request, body_offset = _decode_header(frame, media.MediaDecodeRequest)
    if len(frame) != body_offset:
        raise MediaDecoderWireError("bad_packet")
    return request


def decoder_response_bytes(value: media.MediaDecodeResponse, body: bytes) -> bytes:
    """Encode a revalidated ``MediaDecodeResponse`` and its exact declared body."""
    if type(body) is not bytes:
        raise MediaDecoderWireError("bad_packet")
    raw_header, response = _encode_header(value, media.MediaDecodeResponse)
    if response.result == "packet":
        packet = response.packet
        if not 0 < packet.body_bytes <= _BODY_BYTES:
            raise MediaDecoderWireError("bound")
        if len(body) != packet.body_bytes or hashlib.sha256(body).hexdigest() != packet.body_sha256:
            raise MediaDecoderWireError("bad_packet")
    elif body != b"":
        raise MediaDecoderWireError("bad_packet")
    return struct.pack(">I", len(raw_header)) + raw_header + body


def decoder_response_json(frame: bytes) -> tuple[media.MediaDecodeResponse, bytes]:
    """Decode a complete closed FD3 response frame and return ``(response, body)``."""
    response, body_offset = _decode_header(frame, media.MediaDecodeResponse)
    if response.result == "packet":
        packet = response.packet
        if not 0 < packet.body_bytes <= _BODY_BYTES:
            raise MediaDecoderWireError("bound")
        expected_end = body_offset + packet.body_bytes
        if len(frame) != expected_end:
            raise MediaDecoderWireError("bad_packet")
        body = frame[body_offset:expected_end]
        if hashlib.sha256(body).hexdigest() != packet.body_sha256:
            raise MediaDecoderWireError("bad_packet")
        return response, body
    if len(frame) != body_offset:
        raise MediaDecoderWireError("bad_packet")
    return response, b""
