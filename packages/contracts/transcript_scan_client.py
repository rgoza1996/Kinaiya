"""Transcript scans share the JournalClient's retained exchange ownership."""

import asyncio

from packages.contracts.client import _SubmitExchange
from packages.contracts.transcript_scan import (
    TranscriptScanConflict,
    TranscriptScanPage,
    TranscriptScanRequest,
    TranscriptScanUnavailable,
    checked_scan,
)


class _TranscriptScanExchange(_SubmitExchange):
    def _timely(self, deadline):
        if self.abandoned or asyncio.get_running_loop().time() >= deadline:
            raise TranscriptScanUnavailable

    async def _run(self, request, proposal, result_type, deadline):
        try:
            self._timely(deadline)
            self.response = await self.owner.http.send(request, stream=True, follow_redirects=False)
            self._timely(deadline)
            response = self.response
            if response.status_code == 409:
                raise TranscriptScanConflict
            if response.status_code != 200:
                raise TranscriptScanUnavailable
            media = response.headers.get("content-type", "").lower().split(";")
            if (
                response.headers.get("content-encoding", "identity").strip().lower() != "identity"
                or media[0].strip() != "application/json"
                or any(
                    part.strip() not in {"charset=utf-8", 'charset="utf-8"'} for part in media[1:]
                )
            ):
                raise TranscriptScanUnavailable
            declared = response.headers.get("content-length")
            length = None
            if declared is not None:
                if (
                    "transfer-encoding" in response.headers
                    or not declared.isascii()
                    or not declared.isdigit()
                    or len(declared) > 8
                    or int(declared) > result_type.wire_bytes
                ):
                    raise TranscriptScanUnavailable
                length = int(declared)
            raw = bytearray()
            # Raw stream iteration leaves the response's sole close with the
            # independently retained close task, including after cancellation.
            async for chunk in response.stream:
                self._timely(deadline)
                if type(chunk) is not bytes or len(raw) + len(chunk) > result_type.wire_bytes:
                    raise TranscriptScanUnavailable
                raw.extend(chunk)
            if length is not None and len(raw) != length:
                raise TranscriptScanUnavailable
            self._timely(deadline)
            result = result_type.from_json(bytes(raw)).validate_request(proposal)
        finally:
            if self.response is not None:
                self.close_task = asyncio.create_task(self._close())
                self.close_task.add_done_callback(self._settled)
                await asyncio.shield(self.close_task)
        self._timely(deadline)
        return result


async def transcript_scan(owner, original):
    exchange = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 25

    def available():
        if (
            owner._submit_closed
            or owner._submit_cleanup_failed
            or len(owner._submit_exchanges) >= 64
            or loop.time() >= deadline
        ):
            raise TranscriptScanUnavailable

    try:
        available()
        frozen = checked_scan(original, TranscriptScanRequest)
        body = frozen.canonical_bytes()
        frozen = TranscriptScanRequest.from_json(body)
        available()
        request = owner.http.build_request(
            "POST",
            "/v1/transcript/scan",
            content=body,
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            timeout=None,
        )
        if any(name.lower().startswith("x-kinaiya-") for name in request.headers):
            raise TranscriptScanUnavailable
        available()
        exchange = _TranscriptScanExchange(owner, request, frozen, TranscriptScanPage, deadline)
        done, _ = await asyncio.wait({exchange.task}, timeout=max(0, deadline - loop.time()))
        if not done or exchange.abandoned or loop.time() >= deadline:
            raise TranscriptScanUnavailable
        return exchange.task.result()
    except asyncio.CancelledError:
        if exchange is not None:
            exchange.abandon()
        if asyncio.current_task().cancelling():
            raise
        raise TranscriptScanUnavailable from None
    except TranscriptScanConflict:
        raise TranscriptScanConflict from None
    except Exception:
        if exchange is not None:
            exchange.abandon()
        raise TranscriptScanUnavailable from None
