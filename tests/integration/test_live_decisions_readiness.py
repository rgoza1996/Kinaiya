"""Actual live lifecycle/readiness is distinct from legacy and model probing."""

import asyncio

import httpx
import pytest

from packages.contracts.live_readiness import LiveDecisionsReadiness
from packages.providers.decision_capture import CaptureNativeUnavailable
from packages.providers.text import CompatibleTextProvider
from services.common.auth import Auth
from services.decisions.api import create_app
from services.decisions.live_worker import LiveDecisionsWorker
from services.decisions.model_settings import DecisionsModelSettings
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import fence
from tests.integration.test_live_completion_worker import Agent
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_required_decisions_readiness import TOKENS, ControlledWorker

PATH = "/v2/decisions/live-readiness"


async def test_actual_live_worker_lifespan_and_metadata_readiness_keep_legacy_independent(
    live, monkeypatch
):
    live.store.live_captures.fence(fence(live.source.capture.original))
    calls, probes = [], []

    def model(request):
        calls.append(request.content)
        pytest.fail("Routing readiness entered model transport")

    async def probe():
        probes.append(True)
        pytest.fail("Routing readiness probed a model")

    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1", "public-model", transport=httpx.MockTransport(model)
    )
    monkeypatch.setattr(provider, "health", probe, raising=False)
    settings = DecisionsModelSettings(provider, None)
    native = LiveDecisionsWorker(LocalJournal(live.store, "decisions"), Agent(), settings)
    legacy = ControlledWorker()
    app = create_app(legacy, Auth(TOKENS), model_settings=settings, live_worker=native)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://decisions.local",
        headers={"Authorization": "Bearer " + TOKENS["converse"]},
    ) as client:
        before = await client.get(PATH)
        assert LiveDecisionsReadiness.model_validate(before.json()).phase == "starting"
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(3):
                while not native.ready or not legacy.ready:
                    await asyncio.sleep(0)
            result = await client.get(PATH)
            ready = LiveDecisionsReadiness.model_validate(result.json())
            assert (
                ready.ready
                and ready.phase == "ready"
                and result.headers["cache-control"] == "no-store"
            )
            assert (await client.get("/v1/decisions/readiness")).json()["ready"]
            native.ready = False
            assert (await client.get(PATH)).json()["phase"] == "unavailable"
            assert (await client.get("/v1/decisions/readiness")).json()["ready"]
            native.ready = True
            actual_stamp = settings.cache_stamp

            def unavailable():
                raise CaptureNativeUnavailable("Public synthetic model metadata outage")

            monkeypatch.setattr(settings, "cache_stamp", unavailable)
            assert (await client.get(PATH)).json()["phase"] == "unavailable"
            monkeypatch.setattr(settings, "cache_stamp", actual_stamp)
            assert calls == probes == [] and not settings.leases
        stopped = LiveDecisionsReadiness.model_validate((await client.get(PATH)).json())
        assert (
            stopped.instance_id == ready.instance_id
            and stopped.phase == "stopped"
            and not stopped.ready
        )
    assert not native.running and not legacy.running and settings.closed
    assert native.cleanup.done() and native.cleanup.result() and not settings.leases
    assert calls == probes == []


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", None])
async def test_disabled_live_endpoint_roles_do_not_activate_legacy_or_model(role):
    worker = ControlledWorker()
    app = create_app(worker, Auth(TOKENS))
    headers = {} if role is None else {"Authorization": "Bearer " + TOKENS[role]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://decisions.local"
    ) as client:
        result = await client.get(PATH, headers=headers)
    if role in {"owner", "converse"}:
        state = LiveDecisionsReadiness.model_validate(result.json())
        assert state.phase == "disabled" and not state.ready
        assert result.headers["cache-control"] == "no-store"
    else:
        assert result.status_code == (401 if role is None else 403)
    assert not worker.running and not worker.ready


async def test_failed_live_task_cannot_report_ready_from_stale_flags(live):
    class FailedLive(LiveDecisionsWorker):
        async def run(self):
            self.running = self.ready = True
            raise RuntimeError("Public synthetic live loop failure")

    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1",
        "public-model",
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected model call")),
    )
    settings = DecisionsModelSettings(provider, None)
    native = FailedLive(LocalJournal(live.store, "decisions"), Agent(), settings)
    legacy = ControlledWorker()
    app = create_app(legacy, Auth(TOKENS), model_settings=settings, live_worker=native)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://decisions.local",
            headers={"Authorization": "Bearer " + TOKENS["owner"]},
        ) as client:
            assert (await client.get(PATH)).json()["phase"] == "unavailable"
            assert (await client.get("/v1/decisions/readiness")).json()["ready"]
    assert settings.closed and not settings.leases


def test_live_hook_requires_native_version_and_exact_model_owner(live):
    models = object()
    native = LiveDecisionsWorker(LocalJournal(live.store, "decisions"), Agent(), models)
    for candidate, owner in [(ControlledWorker(), models), (native, None), (native, object())]:
        with pytest.raises(ValueError):
            create_app(
                ControlledWorker(), Auth(TOKENS), model_settings=owner, live_worker=candidate
            )
    assert not native.running


@pytest.mark.parametrize(
    "change",
    [
        {"contract_version": True},
        {"contract_version": 2.0},
        {"protocol": "v1"},
        {"ready": True},
        {"model_quality": "qualified"},
        {"all_audio_stopped": True},
    ],
)
def test_live_readiness_cannot_coerce_versions_or_claim_quality_permission_and_stop(change):
    body = dict(
        contract_version=2,
        protocol="native_live_decisions_v2",
        service="decisions",
        consumer="converse",
        instance_id="a" * 32,
        phase="disabled",
        ready=False,
    )
    with pytest.raises(ValueError):
        LiveDecisionsReadiness.model_validate(body | change)
