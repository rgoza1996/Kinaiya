"""Actual caller/Session/spool/model-lease/exchange/publication composition.

Only HTTPX response delivery/close is a failure-injection transport. Native
source/consumer/reader/worker/receipt ownership and journal commits are real.
The ordinary authenticated ASGI owner-channel fixture grants no model readiness,
decoder availability or command permission. No device/model/network is used.
"""

import asyncio
import hashlib
import io
import json
import sys
import wave
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from test_session_media_owner import _headers, _stack

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts import media as m
from packages.contracts.models import AppendRequest, TaskRequest
from packages.providers.llama_media import NativeLlamaMediaProvider
from packages.providers.text import CompatibleTextProvider, ProviderCleanupError
from services.agent.runtime import AgentRuntime
from services.converse.model_settings import ConverseModelSettings
from services.converse.runtime import ConverseRuntime
from services.decisions.model_settings import DecisionsModelSettings
from services.decisions.runtime import DecisionsWorker
from services.session.native_media import NativeBufferedMediaOwner
from services.session.store import LocalJournal

_WAIT = 10
_MODES = ("converse_chat", "decisions_systemone")


def _wav():
    target = io.BytesIO()
    with wave.open(target, "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(b"\x00\x00" * 1600)
    return target.getvalue()


def _response(request):
    body = json.loads(request.content)
    if "questions" not in body:
        return json.dumps(
            {
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "The synthetic packet is silent.",
                        },
                    }
                ],
            }
        ).encode()
    answers = {}
    for name, question in body["questions"].items():
        labels = list(question["criteria"])
        # A model's request to create work must still be evidence-only.
        choice = {"agent_route": "new_task", "attention": "attach_ranges"}.get(name, labels[-1])
        answers[name] = {
            "type": "choice",
            "choice": choice,
            "probabilities": {label: int(label == choice) for label in labels},
            "confidence": 1,
        }
    return json.dumps(
        {
            "model": body["model"],
            "answers": answers,
            "usage": {"input_tokens": 50, "output_tokens": 0},
        }
    ).encode()


class _Wire(httpx.AsyncByteStream):
    def __init__(self, phase=None, *, fail_close=False):
        self.phase, self.fail_close = phase, fail_close
        self.entered, self.release, self.cancel_observed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.close_done = asyncio.Event()
        self.requests = []
        self.close_calls = 0
        self.close_task = None
        self.body = None

    async def _hold(self, phase):
        if self.phase != phase:
            return
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_observed.set()
            await self.release.wait()  # Suppress once in the original work.

    async def handle(self, request):
        self.requests.append(request)
        self.body = _response(request)
        await self._hold("before_response")
        return httpx.Response(200, stream=self)

    async def __aiter__(self):
        await self._hold("body")
        yield self.body

    async def aclose(self):
        self.close_calls += 1
        self.close_task = asyncio.current_task()
        await self._hold("close")
        self.close_done.set()
        if self.fail_close:
            raise OSError("Synthetic original close could not be confirmed")


async def _reconciled(native, *, key="native-a"):
    a = native._attempts[key]
    await asyncio.wait_for(asyncio.gather(a.worker, return_exceptions=True), _WAIT)
    await asyncio.sleep(0)
    assert a.reconciliation is not None
    await asyncio.wait_for(asyncio.shield(a.reconciliation), _WAIT)
    return a


async def _entered(call, event):
    observer = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            {call, observer}, timeout=_WAIT, return_when=asyncio.FIRST_COMPLETED
        )
        if call in done:
            await call  # Preserve an actual early refusal instead of a wait timeout.
            pytest.fail("Native caller returned before the held operation")
        assert observer in done and observer.result() is True
    finally:
        if not observer.done():
            observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)


@asynccontextmanager
async def _case(tmp_path, mode, phase=None, *, provider_timeout=5, fail_close=False):
    wire = _Wire(phase, fail_close=fail_close)
    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        "synthetic-local",
        mode=mode,
        timeout=provider_timeout,
        transport=httpx.MockTransport(wire.handle),
    )
    async with _stack(tmp_path, allow_unknown_shutdown=fail_close) as env:
        raw = _wav()
        source, grant = await env.admit(size=len(raw))
        async with env.connected(source, grant):
            assert (await env.put(env.put_header(source, body=raw), raw, grant)).status_code == 200
            sealed, _ = await env.seal(source, grant, raw)
            assert sealed.status_code == 200
            request = Request(
                {
                    "type": "http",
                    "headers": [
                        (key.lower().encode(), value.encode())
                        for key, value in _headers(grant=grant).items()
                    ],
                }
            )
            principal = await env.owner.authenticate(request, {"owner"}, capability=True)
            native = NativeBufferedMediaOwner(env.owner)
            if mode == "converse_chat":
                caller = ConverseRuntime(
                    LocalJournal(env.store, "converse"), provider, native_media_owner=native
                )
                settings = caller.model_settings = ConverseModelSettings(caller, None)
            else:
                settings = DecisionsModelSettings(provider, None)
                caller = DecisionsWorker(
                    LocalJournal(env.store, "decisions"),
                    None,
                    None,
                    model_settings=settings,
                    native_media_owner=native,
                )
            ref = m.media_json(json.dumps(env.ref(source)).encode(), m.MediaNativeRef)
            fence = m.media_json(json.dumps(source["body"]["fence"]).encode(), m.MediaFence)
            case = SimpleNamespace(
                env=env,
                native=native,
                caller=caller,
                provider=provider,
                wire=wire,
                source=source,
                grant=grant,
                principal=principal,
                ref=ref,
                fence=fence,
                settings=settings,
            )
            case.start = lambda **kwargs: asyncio.create_task(
                caller.complete_native_media(ref, fence, principal, key="native-a", **kwargs)
            )
            try:
                yield case
            finally:
                primary = sys.exception()
                wire.release.set()
                try:
                    for a in native._attempts.values():
                        if not a.worker.done():
                            native._fence(a)
                    for key in native._attempts:
                        await _reconciled(native, key=key)
                    await settings.close()
                    await provider.close()
                except BaseException as error:
                    if primary is None:
                        raise
                    primary.add_note("Additional native fixture teardown: " + type(error).__name__)


def _publications(case):
    return case.env.store.read(kinds=["conversation.converse", "routing.decided"])


def _assert_original(case, a, *, settled=True):
    assert len(case.wire.requests) == 1 and case.wire.close_calls == 1
    assert a.worker.get_coro() is a.coroutine and a.worker.done()
    assert a.reader_closed and a.reader._closed and a.reader._fd is None
    assert len(a.snapshots) == 2
    before, after = a.snapshots
    assert before.cleanup.state == "unknown" and not before.cleanup.local_task_joined
    assert not before.cleanup.publications_settled
    assert after.cleanup.local_task_joined and after.cleanup.publications_settled
    assert (after.cleanup.state == "settled") is settled
    assert a.settled is settled
    assert after.cleanup.original_owner == a.registration.original_owner
    assert after.cleanup.exchange_owner == a.registration.exchange_owner
    assert after.cleanup.body_owner == a.registration.body_owner_ref


@pytest.mark.parametrize("mode", _MODES)
async def test_actual_native_callers_publish_after_original_return_and_join(tmp_path, mode):
    async with _case(tmp_path, mode) as case:
        event = await asyncio.wait_for(case.start(), _WAIT)
        a = await _reconciled(case.native)
        _assert_original(case, a)
        assert _publications(case) == [event]
        assert event.payload["native_media_v2"]["source"] == case.env.ref(case.source)
        assert (
            event.payload["native_media_v2"]["request_sha256"]
            == hashlib.sha256(case.wire.requests[0].content).hexdigest()
        )
        if mode == "decisions_systemone":
            assert event.payload["route"] == "ignore"
            assert event.payload["allowed_effect"] == "evidence_only"
        # The real native submit rows contain only the original owner task.
        assert len(case.env.store.read(kinds=["agent.command"])) == 1
        replay = await case.caller.complete_native_media(
            case.ref, case.fence, case.principal, key="native-a"
        )
        assert replay.event_id == event.event_id
        assert _publications(case) == [event] and len(case.wire.requests) == 1


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("phase", ["before_response", "body", "close"])
@pytest.mark.parametrize("fault", ["cancel", "provider_deadline"])
async def test_native_callers_refuse_resistant_late_transport_before_publication(
    tmp_path, mode, phase, fault
):
    async with _case(
        tmp_path, mode, phase, provider_timeout=0.3 if fault == "provider_deadline" else 5
    ) as case:
        call = case.start()
        await _entered(call, case.wire.entered)
        if fault == "cancel":
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        if phase == "close":
            a = case.native._attempts["native-a"]
            async with asyncio.timeout(_WAIT):
                while not a.worker.cancelling():
                    await asyncio.sleep(0.01)
            assert case.wire.close_task is not a.worker
            assert not case.wire.close_task.done() and not case.wire.close_task.cancelling()
            assert not case.wire.cancel_observed.is_set()
        else:
            await asyncio.wait_for(case.wire.cancel_observed.wait(), _WAIT)
        assert not _publications(case)
        case.wire.release.set()
        if fault == "provider_deadline":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(call, _WAIT)
        a = await _reconciled(case.native)
        _assert_original(case, a)
        assert a.outcome != "completed" and not _publications(case)
        assert len(case.env.store.read(kinds=["agent.command"])) == 1


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("fault", ["cancel", "deadline", "model_close", "source_stop"])
async def test_real_publication_guard_rechecks_after_waiting_for_its_original_slot(
    tmp_path, mode, fault
):
    async with _case(tmp_path, mode) as case:
        async with case.native._publication_lock:
            call = case.start(timeout=5.0 if fault == "deadline" else 30.0)
            await _entered(call, case.wire.close_done)
            # The real caller has returned from HTTP and is queued at its
            # actual native publication lock, not a synthetic settle callback.
            for _ in range(1000):
                a = case.native._attempts["native-a"]
                if a.staged is not None:
                    break
                if call.done():
                    await call
                await asyncio.sleep(0.001)
            assert a.staged is not None and not _publications(case)
            if fault == "cancel":
                call.cancel()
            elif fault == "model_close":
                await case.settings.close()
            elif fault == "source_stop":
                response = await case.env.post(
                    "/v1/media/control",
                    case.env.control_request(case.source, "stop"),
                    grant=case.grant,
                )
                assert response.status_code == 200
            else:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(call, _WAIT)
        if fault != "deadline":
            with pytest.raises((asyncio.CancelledError, ValueError)):
                await asyncio.wait_for(call, _WAIT)
        a = await _reconciled(case.native)
        _assert_original(case, a)
        assert not _publications(case)


@pytest.mark.parametrize("mode", _MODES)
async def test_unconfirmed_original_close_retains_native_source_and_capacity(tmp_path, mode):
    async with _case(tmp_path, mode, fail_close=True) as case:
        with pytest.raises(ProviderCleanupError):
            await asyncio.wait_for(case.start(), _WAIT)
        a = await _reconciled(case.native)
        _assert_original(case, a, settled=False)
        assert not a.snapshots[-1].cleanup.transport_sole_closed
        assert not _publications(case)
        assert case.env.spool.reserved_bytes == len(_wav())
        assert case.native.source_settled(a.source) is False
        response = await case.env.post(
            "/v1/media/delete",
            {
                "v": 1,
                "key": "delete-unknown",
                "source": case.env.ref(case.source),
                "fence": case.source["body"]["fence"],
            },
            grant=case.grant,
        )
        assert response.status_code in (200, 503)
        assert case.env.spool.reserved_bytes == len(_wav())


@pytest.mark.parametrize("mode", _MODES)
async def test_native_evidence_cannot_admit_commands_at_the_actual_agent_boundary(tmp_path, mode):
    async with _case(tmp_path, mode) as case:
        workspace = tmp_path / "agent-workspace"
        workspace.mkdir()

        def controlled_agent_response(request):
            return httpx.Response(
                200,
                json={
                    "model": "synthetic-agent",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "tool": "finish",
                                        "arguments": {
                                            "summary": "Controlled fixture observed; no external work.",
                                            "objective_revision": 1,
                                        },
                                    }
                                ),
                            },
                        }
                    ],
                },
            )

        provider = CompatibleTextProvider(
            "http://127.0.0.1:8891/v1",
            "synthetic-agent",
            transport=httpx.MockTransport(controlled_agent_response),
        )
        agent = AgentRuntime(
            LocalJournal(case.env.store, "agent"), provider, workspace, ToolPolicy(workspace)
        )
        if mode == "decisions_systemone":
            case.caller.agent = agent
        try:
            owner = case.env.store.append(
                AppendRequest(
                    **case.env.session,
                    kind="conversation.user",
                    payload={
                        "text": "Observe this public synthetic fixture.",
                        "final": True,
                        "revision": 1,
                    },
                    idempotency_key="native-agent-positive-owner",
                ),
                "owner",
            )
            accepted = await agent.submit(
                TaskRequest(
                    **case.env.session,
                    objective=owner.payload["text"],
                    source_event_ids=[owner.event_id],
                    source_revision=1,
                    content_scope="local_only",
                    idempotency_key="native-agent-positive",
                ),
                actor="owner",
            )
            assert accepted.task_id in agent._tasks
            assert agent._tasks[accepted.task_id].submit_confirmed
            commands = case.env.store.read(kinds=["agent.command"])
            assert len(commands) == 2  # Original media anchor and real Agent positive control.
            published = await asyncio.wait_for(case.start(), _WAIT)
            await _reconciled(case.native)
            assert case.env.store.read(kinds=["agent.command"]) == commands
            assert set(agent._tasks) == {accepted.task_id}
            # A classifier/caller cannot turn its new media observation into
            # authenticated owner text by sending it to the real ingress.
            with pytest.raises((PolicyDenied, ValueError)):
                await agent.submit(
                    TaskRequest(
                        **case.env.session,
                        objective="Untrusted imported command",
                        source_event_ids=[published.event_id],
                        source_revision=1,
                        content_scope="local_only",
                        idempotency_key="native-agent-forbidden",
                    ),
                    actor="decisions",
                )
            assert case.env.store.read(kinds=["agent.command"]) == commands
        finally:
            await agent.close()
            await provider.close()
