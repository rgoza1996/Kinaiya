"""Native local/authenticated-HTTP journals and actual synthetic typed-provider prompts."""

import json
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import AppendRequest, ConverseRequest, ProviderInfo, SessionRequest
from services.common.auth import Auth
from services.converse.history import MAX_INTEGER
from services.converse.runtime import ConverseRuntime
from services.session.api import create_app
from services.session.store import LocalJournal, Store

PROVIDERS = {
    "local": ProviderInfo(provider="synthetic", model="local-fixture", local=True),
    "contributor": ProviderInfo(
        provider="meta", model="synthetic-contributor-fixture", local=False
    ),
    "cloud": ProviderInfo(
        provider="synthetic-cloud", model="authorized-cloud-fixture", local=False
    ),
}
SCOPES = ["public_synthetic", "local_only", "private_cloud_authorized"]


class RecordingProvider:
    def __init__(self):
        self.info = PROVIDERS["contributor"]
        self.prompts = []

    async def stream(self, messages):
        self.prompts.append(messages)
        yield "Public synthetic typed reply."


@pytest.fixture(params=["local", "http"])
async def env(tmp_path, request):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    auth = Auth(
        {"owner": "synthetic-owner-0000000000000000", "converse": "synthetic-converse-111111111111"}
    )
    if request.param == "local":
        owner, journal = LocalJournal(store, "owner"), LocalJournal(store, "converse")
    else:
        transport = httpx.ASGITransport(app=create_app(store, auth))
        owner = JournalClient("http://fixture", auth.tokens["owner"], transport=transport)
        journal = JournalClient("http://fixture", auth.tokens["converse"], transport=transport)
    provider = RecordingProvider()
    runtime = ConverseRuntime(journal, provider)
    result = SimpleNamespace(
        store=store,
        session=session,
        owner=owner,
        journal=journal,
        provider=provider,
        runtime=runtime,
    )
    try:
        yield result
    finally:
        await runtime.close()
        if request.param == "http":
            await owner.close()
            await journal.close()
        store.close()


async def write(env, kind, payload, *, target=None, writer="owner"):
    return await getattr(env, writer).append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind=kind,
            payload=payload,
            causes=[target.event_id] if target else [],
            idempotency_key=f"fixture-{len(env.store.read(limit=200))}",
        )
    )


async def base(env, **payload):
    return await write(
        env,
        "conversation.user",
        {"text": "Synthetic original draft", "final": True, "revision": 1, **payload},
    )


async def correct(env, target, **payload):
    return await write(
        env,
        "conversation.revision",
        {
            "target_event_id": target.event_id,
            "text": "Synthetic corrected text",
            "revision": 2,
            **payload,
        },
        target=target,
    )


def request_for(env, key="current"):
    return ConverseRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        text="Public synthetic current input.",
        idempotency_key=key,
    )


async def prompt_history(env, *, key="current"):
    result = await env.runtime.turn(request_for(env, key))
    assert result["text"] == "Public synthetic typed reply."
    prompt = env.provider.prompts[-1]
    assert prompt[-1] == {"role": "user", "content": "Public synthetic current input."}
    return [message for message in prompt[1:-1] if message["role"] != "system"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("base_scope", SCOPES)
@pytest.mark.parametrize("revision_scope", SCOPES)
async def test_actual_prompt_uses_corrected_text_only_when_entire_scope_chain_is_allowed(
    env, provider, base_scope, revision_scope
):
    env.provider.info = PROVIDERS[provider]
    original = await base(env, content_scope=base_scope)
    await correct(env, original, content_scope=revision_scope)
    allowed = (
        set(SCOPES)
        if provider == "local"
        else {"public_synthetic", "private_cloud_authorized"}
        if provider == "cloud"
        else {"public_synthetic"}
    )
    history = await prompt_history(env)
    assert history == (
        [{"role": "user", "content": "Synthetic corrected text"}]
        if base_scope in allowed and revision_scope in allowed
        else []
    )
    assert "Synthetic original draft" not in json.dumps(env.provider.prompts[-1])


@pytest.mark.parametrize("provider", ["contributor", "cloud"])
async def test_denied_intervening_scope_cannot_be_widened_by_future_public_correction(
    env, provider
):
    env.provider.info = PROVIDERS[provider]
    original = await base(env, content_scope="public_synthetic")
    await correct(env, original, content_scope="local_only")
    await correct(
        env,
        original,
        revision=3,
        content_scope="public_synthetic",
        text="Later public label must not restore permission",
    )
    assert await prompt_history(env) == []


@pytest.mark.parametrize(
    "base_revision,next_revision",
    [(True, 2), (1.0, 2.0), (0, 1), (1, 2.0), (MAX_INTEGER, MAX_INTEGER + 1)],
)
@pytest.mark.parametrize("later_public", [False, True])
async def test_store_accepted_unprocessable_chain_never_exports_stale_or_later_text(
    env, base_revision, next_revision, later_public
):
    original = await base(env, revision=base_revision, content_scope="public_synthetic")
    await correct(env, original, revision=next_revision, content_scope="local_only")
    resolved = await env.journal.resolve(original.event_id)
    assert resolved.payload["text"] == "Synthetic corrected text"
    assert resolved.payload["content_scope"] == "local_only"
    if later_public:
        await correct(
            env,
            original,
            revision=next_revision + 1,
            content_scope="public_synthetic",
            text="Public label after malformed revision",
        )
    assert await prompt_history(env) == []


@pytest.mark.parametrize("scope", [None, {}, [], 42, "unrecognized"])
@pytest.mark.parametrize("where", ["base", "revision"])
@pytest.mark.parametrize("provider", ["local", "contributor"])
async def test_present_malformed_scope_is_sticky_denied_for_actual_provider_prompt(
    env, scope, where, provider
):
    env.provider.info = PROVIDERS[provider]
    original = await base(env, content_scope=scope if where == "base" else "public_synthetic")
    await correct(env, original, content_scope=scope if where == "revision" else "public_synthetic")
    await correct(
        env,
        original,
        revision=3,
        content_scope="public_synthetic",
        text="Later public cannot untaint malformed scope",
    )
    assert await prompt_history(env) == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_legacy_unscoped_typed_history_and_missing_revision_scope_keep_consent_path(
    env, provider
):
    env.provider.info = PROVIDERS[provider]
    original = await base(env)
    await correct(env, original)
    await write(
        env,
        "conversation.converse",
        {"text": "Legacy unscoped assistant", "final": True},
        writer="journal",
    )
    assert await prompt_history(env) == [
        {"role": "user", "content": "Synthetic corrected text"},
        {"role": "assistant", "content": "Legacy unscoped assistant"},
    ]


async def test_latest_consecutive_revision_keeps_original_order_full_text_and_context_turns(env):
    env.runtime.context_turns = 10
    original = await base(env, content_scope="public_synthetic")
    await write(
        env,
        "conversation.converse",
        {"text": "Prior assistant text", "final": True, "content_scope": "public_synthetic"},
        writer="journal",
    )
    for i in range(8):
        await base(env, text=f"Intervening user {i}", content_scope="public_synthetic")
    await correct(env, original, content_scope="public_synthetic")
    latest_text = "Latest corrected text: " + "x" * 3000
    await correct(env, original, revision=3, content_scope="public_synthetic", text=latest_text)
    history = await prompt_history(env)
    assert len(history) == 10
    assert history[0] == {"role": "user", "content": latest_text}
    assert history[1] == {"role": "assistant", "content": "Prior assistant text"}
    assert history[-1] == {"role": "user", "content": "Intervening user 7"}


async def test_projection_uses_one_existing_history_snapshot_without_resolve_or_extra_reads(env):
    original = await base(env, content_scope="public_synthetic")
    await correct(env, original, content_scope="public_synthetic")
    # Warm the unrelated generation-discovery read before measuring the one
    # existing typed-history suffix. The projection itself adds no journal I/O.
    await env.runtime._epoch(env.session.session_id)
    recent = env.journal.recent
    calls = []

    async def counted_recent(**kwargs):
        calls.append(kwargs)
        return await recent(**kwargs)

    async def forbidden_read(*args, **kwargs):
        pytest.fail("Typed history must not resolve messages or page history")

    env.journal.recent = counted_recent
    env.journal.resolve = env.journal.read = forbidden_read
    assert await prompt_history(env) == [{"role": "user", "content": "Synthetic corrected text"}]
    assert calls == [{"session_id": env.session.session_id, "limit": 100}]


async def test_history_suffix_is_bounded_without_filling_an_orphan_revision_from_old_history(env):
    original = await base(env, content_scope="public_synthetic")
    for i in range(100):
        await write(env, "delivery.updated", {"fixture": i}, writer="journal")
    await correct(env, original, content_scope="public_synthetic")
    assert await prompt_history(env) == []


async def test_reset_excludes_old_native_history_even_if_recent_contains_it(env):
    original = await base(env, content_scope="public_synthetic")
    await correct(env, original, content_scope="public_synthetic")
    env.session = env.store.reset(env.session.session_id)
    assert await prompt_history(env) == []


async def test_replay_retains_raw_idempotency_identity_and_does_not_repeat_provider_or_input(env):
    original = await base(env, content_scope="public_synthetic")
    await correct(env, original, content_scope="public_synthetic")
    await prompt_history(env)
    before = env.store.read(limit=200)
    result = await env.runtime.turn(request_for(env))
    assert result["text"] == "Public synthetic typed reply."
    assert all(event.get("replay") is True for event in result["events"])
    assert len(env.provider.prompts) == 1
    assert env.store.read(limit=200) == before


async def test_existing_typed_delivery_notes_are_preserved_separately_from_revision_projection(env):
    original = await base(env, content_scope="public_synthetic")
    await correct(env, original, content_scope="public_synthetic")
    await write(
        env,
        "delivery.updated",
        {
            "event_id": "synthetic-prior-assistant",
            "status": "playing",
            "alignment": "conservative_estimate",
            "estimated_text_fraction": 0.25,
            "fixture_extra": "not forwarded",
        },
        writer="journal",
    )
    await prompt_history(env)
    system = [
        message["content"] for message in env.provider.prompts[-1] if message["role"] == "system"
    ]
    assert len(system) == 2
    assert (
        system[-1]
        == 'Playback delivery notes (estimates, not word alignment): [{"event_id": "synthetic-prior-assistant", "status": "playing", "alignment": "conservative_estimate", "estimated_text_fraction": 0.25}]'
    )
