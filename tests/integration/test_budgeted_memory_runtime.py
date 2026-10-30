"""Selected recall stays whole in the measured production path; native Session DAG."""

import asyncio

import pytest
from test_converse_memory_runtime import host as native_memory_fixture
from test_converse_memory_runtime import replies, typed, use, window_reply

from packages.providers.prompt_budget import PromptTokenCount, request_digest

host = native_memory_fixture


@pytest.mark.parametrize("mode", ["fits", "too_large", "forget_during_measurement"])
async def test_budgeted_recall_is_complete_or_omitted_and_final_count_matches_generation(
    host, mode
):
    host.runtime.context_budgeted = True
    host.provider.counts.clear()
    forgot = False

    async def count(messages):
        nonlocal forgot
        host.provider.counts.append(messages)
        memory = any("Synthetic dependent memory." in m["content"] for m in messages)
        if memory and mode == "forget_during_measurement" and not forgot:
            forgot = True
            host.memory.forget(
                {
                    **host.binding.model_dump(),
                    "expected_revision": 3,
                    "idempotency_key": "forget-budgeted",
                    "node_id": host.parent.node_id,
                }
            )
        tokens = 9000 if memory and mode == "too_large" else 100 + len(messages)
        return PromptTokenCount(tokens, 8192, 128, request_digest(messages), "b" * 64)

    host.provider.measure_prompt = count
    await use(host)
    result = await typed(host, "budgeted")
    assert result["text"] == "Synthetic conversational reply 1."
    prompt = host.provider.prompts[-1]
    assert prompt == host.provider.counts[-1]
    encoded = str(prompt)
    if mode == "fits":
        assert "Synthetic parent memory." in encoded
        assert "Synthetic dependent memory." in encoded
        assert "memory_context" in replies(host)[-1].payload
        await use(host, False)
        await typed(host, "budgeted-after-off")
        assert not any(m["role"] == "assistant" for m in host.provider.prompts[-1])
        assert "Synthetic dependent memory." not in str(host.provider.prompts[-1])
    else:
        assert "Synthetic parent memory." not in encoded
        assert "Synthetic dependent memory." not in encoded
        assert "memory_context" not in replies(host)[-1].payload


async def test_live_final_read_failure_refuses_without_orphaning_successful_memory_lease(host):
    host.runtime.context_budgeted = True

    async def count(messages):
        host.provider.counts.append(messages)
        return PromptTokenCount(100, 8192, 128, request_digest(messages), "b" * 64)

    host.provider.measure_prompt = count
    await use(host)
    original = host.journal.session
    failed = False

    async def fail_after_recall(*args, **kwargs):
        nonlocal failed
        if not failed and any(lease.snapshot is not None for lease in host.runtime.memory.leases):
            failed = True
            raise RuntimeError("Synthetic native read failure")
        return await original(*args, **kwargs)

    host.journal.session = fail_after_recall
    await window_reply(host, "failed-final-read")
    await asyncio.sleep(0)
    assert failed and not host.provider.prompts
    assert not host.runtime.memory.pending(host.binding)
    assert not host.runtime.memory.leases
    await window_reply(host, "fresh-after-failure")
    assert "Synthetic dependent memory." in str(host.provider.prompts[-1])
    assert not host.runtime.memory.pending(host.binding)


async def test_repeated_cancel_retains_real_final_count_cleanup_under_memory_lease(host):
    host.runtime.context_budgeted = True
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def count(messages):
        host.provider.counts.append(messages)
        if any(lease.snapshot is not None for lease in host.runtime.memory.leases):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()
                closed.set()
        return PromptTokenCount(100, 8192, 128, request_digest(messages), "b" * 64)

    host.provider.measure_prompt = count
    await use(host)
    worker = asyncio.create_task(window_reply(host, "cancel-final-count"))
    await asyncio.wait_for(entered.wait(), 2)
    prepared = host.windows.pending[host.session.session_id]
    prepared.task.cancel()
    prepared.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert host.runtime.memory.pending(host.binding) and not closed.is_set()
    release.set()
    await asyncio.wait_for(closed.wait(), 2)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not host.runtime.memory.pending(host.binding)
    assert not host.provider.prompts
