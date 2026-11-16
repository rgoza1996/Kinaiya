"""Exact completed-result recovery rechecks native model, capture and disclosure."""

from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_decisions_attempts import (
    LiveDecisionsAttemptLookup,
    LiveDecisionsSource,
)
from packages.contracts.models import AppendRequest
from packages.providers.decision_capture import CaptureNativeUnavailable
from packages.providers.text import CompatibleTextProvider, ProviderRefusal
from services.common.auth import Auth
from services.decisions.live_worker import LiveDecisionsWorker
from services.decisions.model_settings import DecisionsModelSettings
from services.session.api import create_app
from services.session.live_windows import publish
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import fence
from tests.integration.test_live_completion_worker import Agent
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_decisions_http import response
from tests.integration.test_live_window_publication import original


@pytest.mark.parametrize("fence_kind", ["model", "capture", "context-disclosure"])
async def test_completed_recovery_rechecks_original_authority(live, monkeypatch, fence_kind):
    context = live.store.append(
        AppendRequest(
            session_id=live.session.session_id,
            thread_id=live.session.thread_id,
            epoch=live.session.epoch,
            kind="conversation.user",
            payload={
                "text": "PUBLIC SYNTHETIC INCLUDED HISTORY",
                "final": True,
                "revision": 1,
                "content_scope": "public_synthetic",
            },
            idempotency_key="public-recovery-context",
        ),
        "owner",
    )
    pair = publish(
        live.store, original(live, 1, text="Public current input after included context")
    )
    live.pair = pair
    live.source = LiveDecisionsSource.of(pair, live.source.capture)
    auth = Auth({"decisions": "public-current-recovery-token-000000000"})
    journal = JournalClient(
        "http://session.local",
        auth.tokens["decisions"],
        transport=httpx.ASGITransport(app=create_app(live.store, auth)),
    )
    calls, proposed = [], []

    def model(request):
        calls.append(request.content)
        return httpx.Response(200, stream=httpx.ByteStream(response()))

    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1", "public-model", transport=httpx.MockTransport(model)
    )
    # The transport is synthetic; exercise the declared cloud disclosure floor.
    provider.info = provider.info.model_copy(update={"local": False})
    settings = DecisionsModelSettings(provider, None)
    replacement_settings = settings
    actual_publish = journal.publish_live_decision

    async def before_publication(proof):
        proposed.append(proof)
        raise CaptureNativeUnavailable("Public synthetic stop before original publication")

    monkeypatch.setattr(journal, "publish_live_decision", before_publication)
    try:
        worker = LiveDecisionsWorker(journal, Agent(), settings)
        with pytest.raises(CaptureNativeUnavailable):
            await worker._job(
                SimpleNamespace(source=live.source, stamp=settings.cache_stamp(), lease=None)
            )
        state = await journal.live_decisions_attempt_lookup(
            LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
        )
        assert state.state == "completed" and state.charged_attempts == 1
        assert context.event_id in [
            s.source_event_id for s in state.last_attempt.admission.disclosure_sources
        ]
        assert len(calls) == len(proposed) == 1
        proof = proposed[0]
        assert (await journal.lookup_live_decision(proof)).receipt is None
        if fence_kind == "model":
            replacement_provider = CompatibleTextProvider(
                "http://127.0.0.1:8897/v1",
                "public-different-model",
                transport=httpx.MockTransport(model),
            )
            replacement_settings = DecisionsModelSettings(replacement_provider, None)
        elif fence_kind == "capture":
            live.store.live_captures.fence(fence(live.source.capture.original))
        else:
            for revision, scope in [(2, "local_only"), (3, "public_synthetic")]:
                live.store.append(
                    AppendRequest(
                        session_id=context.session_id,
                        thread_id=context.thread_id,
                        epoch=context.epoch,
                        kind="conversation.revision",
                        payload={
                            "target_event_id": context.event_id,
                            "text": "PUBLIC SYNTHETIC RESTRICTED HISTORY",
                            "revision": revision,
                            "content_scope": scope,
                        },
                        idempotency_key=f"public-recovery-context-rev-{revision}",
                    ),
                    "owner",
                )
        new_publications = []

        async def observed_publish(original_proof):
            new_publications.append(original_proof)
            return await actual_publish(original_proof)

        monkeypatch.setattr(journal, "publish_live_decision", observed_publish)
        replacement = LiveDecisionsWorker(journal, Agent(), replacement_settings)
        slot = SimpleNamespace(
            source=live.source, stamp=replacement_settings.cache_stamp(), lease=None
        )
        if fence_kind == "model":
            await replacement._job(slot)
            assert replacement.last_error == "original_model_selection_changed"
        else:
            with pytest.raises((CaptureNativeUnavailable, ProviderRefusal)):
                await replacement._job(slot)
        assert new_publications == [] and len(calls) == 1
        assert (await journal.lookup_live_decision(proof)).receipt is None
        assert (
            await journal.live_decisions_attempt_lookup(
                LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
            )
            == state
        )
        assert not settings.leases and not replacement_settings.leases
        assert live.store.read(kinds=["routing.live_window_decided"]) == []
    finally:
        if replacement_settings is not settings:
            await replacement_settings.close()
        await settings.close()
        await journal.close()
