"""Bounded HTTP-close ownership; separate from native process proof."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.converse import api


async def wait_empty():
    async with asyncio.timeout(3):
        while api._STT_OWNERS or api._STT_CLOSE_OWNERS:
            await asyncio.sleep(0.01)


async def test_simultaneous_completion_and_disconnect_discards_text():
    ready = asyncio.Event()

    async def receive():
        await ready.wait()
        return {"type": "http.disconnect"}

    async def work():
        await ready.wait()
        return {"text": "Public simultaneous fixture"}

    task = asyncio.create_task(api._transcribe_connected(SimpleNamespace(receive=receive), work))
    await asyncio.sleep(0)
    ready.set()
    with pytest.raises(HTTPException) as exc:
        await task
    assert exc.value.status_code == 499
    await wait_empty()


async def test_outer_repeated_cancellation_retains_actual_http_close_until_confirmed():
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def receive():
        await asyncio.Event().wait()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            await release.wait()

    task = asyncio.create_task(api._transcribe_connected(SimpleNamespace(receive=receive), work))
    await entered.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(api._STT_OWNERS) == len(api._STT_CLOSE_OWNERS) == 1
    release.set()
    await wait_empty()


async def test_saturation_reserves_before_factory_and_retains_all_pending_closes():
    release = asyncio.Event()
    entered = 0

    async def receive():
        await asyncio.Event().wait()

    async def work():
        nonlocal entered
        entered += 1
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()

    request = SimpleNamespace(receive=receive)
    tasks = [asyncio.create_task(api._transcribe_connected(request, work)) for _ in range(32)]
    async with asyncio.timeout(3):
        while entered != 32:
            await asyncio.sleep(0)
    try:
        with pytest.raises(HTTPException) as exc:
            await api._transcribe_connected(request, lambda: pytest.fail("Factory admitted"))
        assert exc.value.status_code == 503
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert len(api._STT_OWNERS) == len(api._STT_CLOSE_OWNERS) == 32
    finally:
        release.set()
        await wait_empty()
