"""Bounded media framing; a complete frame is never authority or cleanup proof."""

import asyncio
import hashlib
import struct
from collections.abc import AsyncIterator

from packages.contracts import media

_HEADER_BYTES = 65536
_BODY_BYTES = 16777216
_UPLOAD_BYTES = 4194304
_READ_BYTES = 65536
_INGRESS_SECONDS = 60
_ERROR_CODES = frozenset(
    {
        "bad_packet",
        "denied",
        "unknown_ref",
        "stale",
        "expired",
        "unsupported",
        "bound",
        "capacity",
        "unavailable",
        "conflict",
    }
)


class MediaWireError(ValueError):
    def __init__(self, code: str):
        if type(code) is not str or code not in _ERROR_CODES:
            raise ValueError("Invalid media error code")
        self.code = code
        super().__init__("Media frame was not verified")


def _allowed(kind):
    return any(
        kind is item for item in (media.MediaUploadPut, media.MediaCapturePut, media.MediaASRFeed)
    )


def _body_binding(header):
    if type(header) is media.MediaASRFeed:
        return header.packet.body_bytes, header.packet.body_sha256, _BODY_BYTES
    limit = _UPLOAD_BYTES if type(header) is media.MediaUploadPut else _BODY_BYTES
    return header.body_bytes, header.body_sha256, limit


def _checked_header(raw, kind):
    if not _allowed(kind):
        raise MediaWireError("unsupported")
    try:
        header = media.media_json(raw, kind)
        if media.media_bytes(header) != raw:
            raise MediaWireError("bad_packet")
        size, digest, limit = _body_binding(header)
        if not 0 < size <= limit:
            raise MediaWireError("bound")
        return header, size, digest
    except MediaWireError:
        raise
    except media.MediaContractError as exc:
        raise MediaWireError(exc.code) from None
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise MediaWireError("bad_packet") from None


class _FrameStream:
    def __init__(self, chunks):
        try:
            self._chunks = aiter(chunks)
        except TypeError:
            raise MediaWireError("bad_packet") from None
        except Exception:
            raise MediaWireError("unavailable") from None
        self._buffer = b""
        self._offset = 0
        self._eof = False
        self._fetch_mod = 0
        self._loop = asyncio.get_running_loop()
        self._deadline = self._loop.time() + _INGRESS_SECONDS

    async def _next(self):
        if self._eof:
            return False
        while True:
            if self._loop.time() >= self._deadline:
                raise MediaWireError("unavailable")
            self._fetch_mod = (self._fetch_mod + 1) % 64
            if self._fetch_mod == 0:
                await asyncio.sleep(0)
            try:
                async with asyncio.timeout_at(self._deadline):
                    chunk = await anext(self._chunks)
            except StopAsyncIteration:
                self._eof = True
                self._buffer = b""
                self._offset = 0
                return False
            except TimeoutError:
                raise MediaWireError("unavailable") from None
            except Exception:
                raise MediaWireError("unavailable") from None
            if type(chunk) is not bytes:
                raise MediaWireError("bad_packet")
            # Keep at most one bounded incoming chunk, never an unbounded read-all.
            if len(chunk) > _HEADER_BYTES + _BODY_BYTES + 4:
                raise MediaWireError("bound")
            if chunk:
                self._buffer = chunk
                self._offset = 0
                return True

    async def read_exact(self, size):
        remaining = size
        parts = []
        while remaining:
            if self._offset == len(self._buffer) and not await self._next():
                raise MediaWireError("bad_packet")
            take = min(remaining, len(self._buffer) - self._offset)
            parts.append(self._buffer[self._offset : self._offset + take])
            self._offset += take
            remaining -= take
        return b"".join(parts)

    async def require_end(self):
        if self._offset < len(self._buffer) or await self._next():
            raise MediaWireError("bad_packet")
        if self._loop.time() >= self._deadline:
            raise MediaWireError("unavailable")


class IngressBody:
    """Single-use exact body. Abandonment/cancellation leaves verified false."""

    def __init__(self, stream, raw_header, kind, size, digest):
        self._stream = stream
        self._raw_header = raw_header
        self._kind = kind
        self._size = size
        self._digest = digest
        self._started = False
        self._verified = False

    @property
    def header(self):
        # No mutable nested DTO alias can change the original byte/hash binding.
        return media.media_json(self._raw_header, self._kind)

    @property
    def verified(self):
        return self._verified

    def iter_chunks(self) -> AsyncIterator[bytes]:
        if self._started:
            raise MediaWireError("conflict")
        self._started = True
        return self._iter_chunks()

    async def _iter_chunks(self):
        remaining = self._size
        digest = hashlib.sha256()
        while remaining:
            chunk = await self._stream.read_exact(min(remaining, _READ_BYTES))
            remaining -= len(chunk)
            digest.update(chunk)
            yield chunk
        if digest.hexdigest() != self._digest:
            raise MediaWireError("bad_packet")
        await self._stream.require_end()
        self._verified = True


async def open_ingress(chunks, kind) -> IngressBody:
    """Parse only the closed header; the caller guards permission before body use.

    Authenticate before calling. Recheck current source/fence/reservation after
    this header and before any ACK. This helper issues no ACK or native receipt.
    """
    if not _allowed(kind):
        raise MediaWireError("unsupported")
    stream = _FrameStream(chunks)
    raw_length = await stream.read_exact(4)
    size = struct.unpack(">I", raw_length)[0]
    if size == 0:
        raise MediaWireError("bad_packet")
    if size > _HEADER_BYTES:
        raise MediaWireError("bound")
    raw_header = await stream.read_exact(size)
    _, body_size, digest = _checked_header(raw_header, kind)
    return IngressBody(stream, raw_header, kind, body_size, digest)


def encode_ingress(header, body: bytes) -> bytes:
    """Bounded client/fixture encoder. It grants no native admission or permission."""
    if not _allowed(type(header)):
        raise MediaWireError("unsupported")
    try:
        raw_header = media.media_bytes(header)
    except media.MediaContractError as exc:
        raise MediaWireError(exc.code) from None
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise MediaWireError("bad_packet") from None
    if not 0 < len(raw_header) <= _HEADER_BYTES:
        raise MediaWireError("bound")
    _, size, digest = _checked_header(raw_header, type(header))
    if type(body) is not bytes or len(body) != size or hashlib.sha256(body).hexdigest() != digest:
        raise MediaWireError("bad_packet")
    return struct.pack(">I", len(raw_header)) + raw_header + body
