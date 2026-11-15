"""Public-only native task metadata; real Agent, SQLite, auth and HTTP client."""

import asyncio
import base64
import copy
import json
import socket
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn

from core.policy import ToolPolicy
from packages.agent_adapters.task_context import TaskContextClient
from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    Evidence,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    TaskRequest,
    TaskState,
    WindowDecision,
)
from packages.contracts.task_context import (
    TaskContextRequest,
    TaskContextSource,
    TaskContextUnavailable,
)
from packages.providers.prompt_budget import PromptTokenCount, request_digest
from packages.providers.realtime_audio import NativeAudioInput, NativeAudioReply
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.common.auth import Auth
from services.converse.native_audio import NativeAudioOwner, NativeClipRequest
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store

TOKENS = {
    name: name + "-public-task-context-" + "x" * 32
    for name in ("owner", "decisions", "converse", "agent")
}


class HoldingProvider(ScriptedProvider):
    def __init__(self):
        super().__init__([])
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages):
        self.calls.append(messages)
        self.entered.set()
        await self.release.wait()
        raise AssertionError("The metadata fixture never completes a task")


@asynccontextmanager
async def loopback(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        task.result()
                        raise AssertionError("Temporary Agent server stopped")
                    await asyncio.sleep(0.01)
            yield base
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


def user_source(store, session, *, scope="public_synthetic", text="Public task status question."):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={"text": text, "revision": 1, "final": True, "content_scope": scope},
            idempotency_key=str(uuid4()),
        ),
        "owner",
    )


@asynccontextmanager
async def environment(path, *, socket_http=False):
    store = Store(path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    source = user_source(store, session)
    workspace = path / "workspace"
    workspace.mkdir()
    provider = HoldingProvider()
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace))
    task = await runtime.submit(
        TaskRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="Public synthetic task held without tools.",
            content_scope="public_synthetic",
            idempotency_key="public-context-task",
            source_event_ids=[source.event_id],
        )
    )
    await asyncio.wait_for(provider.entered.wait(), 3)
    app = create_app(runtime, Auth(dict(TOKENS)))
    env = SimpleNamespace(
        store=store,
        session=session,
        source=source,
        runtime=runtime,
        provider=provider,
        task=task,
        app=app,
    )
    try:
        if socket_http:
            async with loopback(app) as base:
                env.base, env.transport = base, None
                yield env
        else:
            env.base = "http://127.0.0.1:8761"
            env.transport = httpx.ASGITransport(app=app)
            yield env
    finally:
        await runtime.close()
        store.close()


def proposal(env, **updates):
    request = TaskContextRequest(source=TaskContextSource.of(env.source))
    return TaskContextRequest.model_validate({**request.model_dump(), **updates})


@pytest.mark.parametrize("socket_http", [False, True], ids=["asgi", "socket"])
async def test_actual_client_reads_running_metadata_without_effects_or_private_text(
    tmp_path, socket_http
):
    async with environment(tmp_path, socket_http=socket_http) as env:
        rec = env.runtime._tasks[env.task.task_id]
        private = "PUBLIC_FIXTURE_MUST_NOT_APPEAR_IN_METADATA"
        rec.task.objective = private
        rec.task.evidence.extend(
            [
                Evidence(
                    kind="file", summary=private, path="/synthetic/private/path", output=private
                ),
                Evidence(kind="external", summary=private, output=private),
                Evidence(kind="artifact", summary=private, path="artifact:public-fixture"),
            ]
        )
        before = env.store.db.total_changes
        client = TaskContextClient(env.base, TOKENS["converse"], transport=env.transport)
        try:
            result = await client.lookup(proposal(env))
        finally:
            await client.close()
        assert env.store.db.total_changes == before
        assert len(env.provider.calls) == 1
        assert len(result.tasks) == 1
        observed = result.tasks[0]
        assert observed.task_id == env.task.task_id and observed.state == "running"
        assert observed.submission_confirmed is True
        assert observed.evidence_counts["file"] == 1
        assert observed.evidence_counts["external"] == 1
        assert observed.evidence_counts["artifact"] == 1
        assert observed.program_execution_receipt == "not_observed"
        assert observed.semantic_verification == "not_assessed"
        assert private not in result.model_dump_json()
        assert "/synthetic/private/path" not in result.model_dump_json()


@pytest.mark.parametrize("actor", ["owner", "decisions", "agent", "invalid"])
async def test_context_privilege_does_not_grant_task_control_or_broad_task_reads(tmp_path, actor):
    async with environment(tmp_path) as env:
        async with httpx.AsyncClient(transport=env.transport, base_url=env.base) as client:
            response = await client.post(
                "/v1/agent/task-context",
                json=proposal(env).model_dump(),
                headers={"Authorization": "Bearer " + TOKENS.get(actor, "wrong")},
            )
            assert response.status_code == (401 if actor == "invalid" else 403)
            for path in ("/v1/agent/tasks", "/v1/agent/tasks/" + env.task.task_id):
                response = await client.get(
                    path, headers={"Authorization": "Bearer " + TOKENS["converse"]}
                )
                assert response.status_code == 403


async def test_source_revision_and_epoch_fence_observations(tmp_path):
    async with environment(tmp_path) as env:
        client = TaskContextClient(env.base, TOKENS["converse"], transport=env.transport)
        try:
            original = proposal(env)
            env.store.append(
                AppendRequest(
                    session_id=env.session.session_id,
                    thread_id=env.session.thread_id,
                    epoch=env.session.epoch,
                    kind="conversation.revision",
                    causes=[env.source.event_id],
                    payload={
                        "target_event_id": env.source.event_id,
                        "text": "Edited public status question.",
                        "revision": 2,
                        "content_scope": "public_synthetic",
                    },
                    idempotency_key="public-source-edit",
                ),
                "owner",
            )
            with pytest.raises(TaskContextUnavailable):
                await client.lookup(original)
            revised = TaskContextRequest(
                source=TaskContextSource.of(env.store.resolve(env.source.event_id))
            )
            assert (await client.lookup(revised)).tasks[0].state == "running"
            env.store.reset(env.session.session_id)
            with pytest.raises(TaskContextUnavailable):
                await client.lookup(revised)
        finally:
            await client.close()


async def test_task_source_restriction_folds_before_metadata_projection_without_mutation(tmp_path):
    async with environment(tmp_path) as env:
        question = user_source(env.store, env.session)
        request = TaskContextRequest(source=TaskContextSource.of(question))
        env.store.append(
            AppendRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                kind="conversation.revision",
                causes=[env.source.event_id],
                payload={
                    "target_event_id": env.source.event_id,
                    "text": "Public fixture now local only.",
                    "revision": 2,
                    "content_scope": "local_only",
                },
                idempotency_key="private-floor",
            ),
            "owner",
        )
        rec = env.runtime._tasks[env.task.task_id]
        assert rec.task.content_scope == "public_synthetic"
        before = env.store.db.total_changes
        client = TaskContextClient(env.base, TOKENS["converse"], transport=env.transport)
        try:
            observed = (await client.lookup(request)).tasks[0]
        finally:
            await client.close()
        assert observed.content_scope == "local_only"
        assert rec.task.content_scope == "public_synthetic"
        assert env.store.db.total_changes == before


@pytest.mark.parametrize("state", [TaskState.FAILED, TaskState.PAUSED, TaskState.SUCCEEDED])
async def test_runtime_state_does_not_claim_semantic_verification(tmp_path, state):
    async with environment(tmp_path) as env:
        rec = env.runtime._tasks[env.task.task_id]
        rec.task.state = state
        rec.task.evidence.append(Evidence(kind="inspection", summary="Public observation only."))
        client = TaskContextClient(env.base, TOKENS["converse"], transport=env.transport)
        try:
            result = await client.lookup(proposal(env))
        finally:
            await client.close()
        assert result.tasks[0].state == state.value
        assert result.tasks[0].semantic_verification == "not_assessed"
        assert result.tasks[0].program_execution_receipt == "not_observed"


async def test_malformed_body_and_ambiguous_json_are_not_metadata(tmp_path):
    async with environment(tmp_path) as env:
        async with httpx.AsyncClient(
            transport=env.transport,
            base_url=env.base,
            headers={"Authorization": "Bearer " + TOKENS["converse"]},
        ) as client:
            raw = json.dumps(proposal(env).model_dump())
            duplicate = raw.replace('"source":', '"source":{},"source":', 1)
            for wire, status in (
                (duplicate, 422),
                (raw[:-1] + ',"extra":true}', 422),
                ("x" * 8193, 413),
            ):
                response = await client.post("/v1/agent/task-context", content=wire)
                assert response.status_code == status


class DialogueProvider:
    """Controlled outputs verify the framework, never a model's task accuracy."""

    def __init__(self, *, local=True, native=False):
        self.info = ProviderInfo(provider="fixture", model="public-context", local=local)
        self.counts, self.prompts, self.audio = [], [], []
        self.native = native
        self.before_count = self.before_reply = None
        self.model, self.base_url = self.info.model, "http://127.0.0.1:8817"
        self.reply = "Public observation only."

    async def measure_prompt(self, messages):
        assert not self.native, "Native PCM must not use a text-token oracle"
        self.counts.append(copy.deepcopy(messages))
        if self.before_count:
            await self.before_count()
        return PromptTokenCount(
            13 + sum(len(m["content"].encode()) + 8 for m in messages),
            1_000_000,
            32,
            request_digest(messages),
            "a" * 64,
        )

    async def stream(self, messages):
        assert not self.native, "Native PCM must not fall back to text generation"
        self.prompts.append(copy.deepcopy(messages))
        if self.before_reply:
            await self.before_reply()
        yield self.reply

    async def transcribe(self, audio, *, current):
        assert await current()
        return "Public current question."

    async def respond(self, audio, messages, *, current):
        assert await current()
        self.audio.append(audio)
        self.prompts.append(copy.deepcopy(messages))
        if self.before_reply:
            await self.before_reply()
        return NativeAudioReply(self.reply, b"\x11\x00\x12\x00", 24000)

    async def close(self):
        pass


@asynccontextmanager
async def dialogue(
    env,
    path,
    *,
    local=True,
    native=False,
    budgeted=True,
    transport=None,
    personality="Public owner persona.",
):
    provider = DialogueProvider(local=local, native=native)
    client = TaskContextClient(env.base, TOKENS["converse"], transport=transport or env.transport)
    runtime = ConverseRuntime(
        LocalJournal(env.store, "converse"),
        provider,
        personality=personality,
        personality_scope="public_synthetic",
        context_budgeted=budgeted,
        task_context_client=client,
    )
    controller = WindowConverseController(runtime)
    owner = None
    if native:
        owner = NativeAudioOwner(
            runtime, provider, path=path / "audio.sqlite", workspace=path / "workspace"
        )
        runtime.native_audio = owner
    result = SimpleNamespace(
        runtime=runtime, controller=controller, provider=provider, client=client, owner=owner
    )
    try:
        yield result
    finally:
        await controller.close()
        if owner:
            await owner.close()
        await runtime.close()
        assert client._transport._submit_closed


async def respond(env, chat, mode, text="Public current question."):
    s = env.session
    if mode == "typed":
        return await chat.runtime.turn(
            ConverseRequest(
                session_id=s.session_id,
                thread_id=s.thread_id,
                epoch=s.epoch,
                text=text,
                content_scope="public_synthetic",
                idempotency_key=str(uuid4()),
            )
        )
    if mode == "native":
        pcm = struct.pack("<5h", 17, -33, 54, -82, 0)
        wave = (
            struct.pack(
                "<4sI4s4sIHHIIHH4sI",
                b"RIFF",
                36 + len(pcm),
                b"WAVE",
                b"fmt ",
                16,
                1,
                1,
                16000,
                32000,
                2,
                16,
                b"data",
                len(pcm),
            )
            + pcm
        )
        receipt = await chat.owner.admit(
            NativeClipRequest(
                session_id=s.session_id,
                thread_id=s.thread_id,
                epoch=s.epoch,
                utterance_id=str(uuid4()),
                request_id=str(uuid4()),
                audio_base64=base64.b64encode(wave).decode(),
                content_scope="public_synthetic",
            ),
            chat.controller,
        )
        source = env.store.entry(receipt["entry"]["event_id"])
    else:
        source = await chat.controller.ingest(
            SpeechWindow(
                session_id=s.session_id,
                thread_id=s.thread_id,
                epoch=s.epoch,
                utterance_id=str(uuid4()),
                window_index=0,
                text=text,
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                capture_ended=True,
                content_scope="public_synthetic",
            )
        )
    decision = env.store.append(
        AppendRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            kind="routing.window_decided",
            causes=[source.event_id],
            payload=WindowDecision(
                source_event_id=source.event_id,
                source_sequence=source.sequence,
                source_revision=1,
                conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
                agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
            ).model_dump(mode="json"),
            idempotency_key=str(uuid4()),
        ),
        "decisions",
    )
    await chat.controller.process(decision)
    jobs = [p.task for p in chat.controller.pending.values()]
    if jobs:
        await asyncio.wait_for(asyncio.gather(*jobs), 8)
    await chat.controller.tick()


def restrict_task_source(env):
    env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.revision",
            causes=[env.source.event_id],
            payload={
                "target_event_id": env.source.event_id,
                "text": "Public privacy fixture.",
                "revision": 2,
                "content_scope": "local_only",
            },
            idempotency_key="task-now-local",
        ),
        "owner",
    )


@pytest.mark.parametrize("socket_http", [False, True], ids=["asgi", "socket"])
@pytest.mark.parametrize(
    "mode,budgeted", [("typed", False), ("typed", True), ("window", True), ("native", True)]
)
async def test_actual_dialogue_paths_consume_bounded_native_facts_in_first_system_message(
    tmp_path, socket_http, mode, budgeted
):
    async with environment(tmp_path, socket_http=socket_http) as env:
        rec = env.runtime._tasks[env.task.task_id]
        rec.task.objective = "NONEXPORTED_OBJECTIVE_FIXTURE"
        rec.task.evidence.append(Evidence(kind="external", summary="NONEXPORTED_OUTPUT_FIXTURE"))
        for n in range(15):
            user_source(env.store, env.session, text=f"Public older dialogue {n}.")
        async with dialogue(env, tmp_path, native=mode == "native", budgeted=budgeted) as chat:
            await respond(env, chat, mode)
            prompt = chat.provider.prompts[-1]
            first = prompt[0]
            assert first["role"] == "system"
            assert first["content"].startswith(
                "Public owner persona.\n\nNative task observations v1"
            )
            facts = json.loads(first["content"].split("\n")[-1])
            observed = facts["tasks"][0]
            assert observed["task_id"] == env.task.task_id
            assert observed["state"] == "running"
            assert observed["evidence_counts"]["external"] == 1
            assert observed["semantic_verification"] == "not_assessed"
            assert facts["coverage"] == "bounded_observations_not_inventory"
            assert "NONEXPORTED_" not in json.dumps(prompt)
            assert not any("Native task observations v1" in m["content"] for m in prompt[1:])
            older = [m["content"] for m in prompt[1:] if m["role"] == "user"][:-1]
            # Preserve the explicit legacy constructor's 12-message limit;
            # measured and native paths must retain the full fitting history.
            expected = [f"Public older dialogue {n}." for n in range(15)]
            assert older[-15:] == (expected if budgeted else expected[-12:])
            if mode == "native":
                assert chat.provider.counts == []
                assert chat.provider.audio == [
                    NativeAudioInput(struct.pack("<5h", 17, -33, 54, -82, 0), 16000)
                ]
            elif budgeted:
                assert chat.provider.counts[-1] == prompt
            assert rec.task.state == TaskState.RUNNING
            assert len(env.provider.calls) == 1
            assert env.store.read(kinds=["agent.outcome"]) == []
            assert (
                env.store.read(kinds=["conversation.converse"])[-1].payload["text"]
                == chat.provider.reply
            )


@pytest.mark.parametrize("mode", ["typed", "window", "native"])
async def test_consumed_task_scope_narrowing_during_generation_narrows_reply(tmp_path, mode):
    async with environment(tmp_path) as env:
        async with dialogue(env, tmp_path, native=mode == "native") as chat:

            async def narrow():
                restrict_task_source(env)

            chat.provider.before_reply = narrow
            await respond(env, chat, mode)
            reply = env.store.read(kinds=["conversation.converse"])[-1]
            assert reply.payload["content_scope"] == "local_only"


@pytest.mark.parametrize("mode", ["typed", "window"])
async def test_task_scope_narrowed_during_measurement_prevents_remote_generation(tmp_path, mode):
    async with environment(tmp_path) as env:
        async with dialogue(env, tmp_path, local=False) as chat:

            async def narrow():
                if env.store.resolve(env.source.event_id).payload["revision"] == 1:
                    restrict_task_source(env)

            chat.provider.before_count = narrow
            await respond(env, chat, mode)
            assert len(chat.provider.counts) == 1
            assert chat.provider.prompts == []
            assert env.store.read(kinds=["conversation.converse"]) == []


async def test_private_task_row_is_not_disclosed_as_an_identity_or_hidden_count(tmp_path):
    async with environment(tmp_path) as env:
        restrict_task_source(env)
        async with dialogue(env, tmp_path, local=False) as chat:
            await respond(env, chat, "typed")
            prompt = chat.provider.prompts[-1]
            facts = json.loads(prompt[0]["content"].split("\n")[-1])
            assert facts["tasks"] == []
            assert "truncated" not in facts
            assert env.task.task_id not in json.dumps(prompt)


@pytest.mark.parametrize("failure", ["offline", "restarted"])
async def test_lost_original_observation_during_generation_never_restores_public_scope(
    tmp_path, failure
):
    async with environment(tmp_path) as env:
        current = {"app": env.app}

        async def switchable(scope, receive, send):
            await current["app"](scope, receive, send)

        transport = httpx.ASGITransport(app=switchable)
        async with dialogue(env, tmp_path, transport=transport) as chat:

            async def lose():
                if failure == "restarted":
                    current["app"] = create_app(env.runtime, Auth(dict(TOKENS)))
                else:

                    async def unavailable(scope, receive, send):
                        from starlette.responses import Response

                        await Response(status_code=503)(scope, receive, send)

                    current["app"] = unavailable

            chat.provider.before_reply = lose
            await respond(env, chat, "typed")
            reply = env.store.read(kinds=["conversation.converse"])[-1]
            assert reply.payload["content_scope"] == "local_only"


async def test_agent_metadata_offline_at_admission_keeps_converse_independently_available(tmp_path):
    async with environment(tmp_path) as env:

        async def unavailable(request):
            return httpx.Response(503)

        transport = httpx.MockTransport(unavailable)
        async with dialogue(env, tmp_path, transport=transport) as chat:
            await respond(env, chat, "typed")
            prompt = chat.provider.prompts[-1]
            assert "Native task observations v1: unavailable." in prompt[0]["content"]
            assert env.task.task_id not in json.dumps(prompt)
            assert (
                env.store.read(kinds=["conversation.converse"])[-1].payload["text"]
                == chat.provider.reply
            )


@pytest.mark.parametrize(
    "state,text",
    [
        (TaskState.RUNNING, "Ask Agent to prepare a fictional garden checklist."),
        (TaskState.RUNNING, "Add a rain plan to that request."),
        (TaskState.RUNNING, "While Agent works, tell me about triangles."),
        (TaskState.FAILED, "Agent could not finish; what do the observations say?"),
        (TaskState.RUNNING, "Unrelated question: what color is a clear daytime sky?"),
    ],
    ids=["delegated", "followup", "long-running", "failure", "unrelated"],
)
@pytest.mark.parametrize("mode", ["typed", "window"])
async def test_editable_default_and_native_state_remain_independent_for_illustrative_dialogue(
    tmp_path, state, text, mode
):
    async with environment(tmp_path) as env:
        rec = env.runtime._tasks[env.task.task_id]
        rec.task.state = state  # Controlled status fixture, not an actual failed execution.
        async with dialogue(env, tmp_path, personality=None) as chat:
            # Deliberately evade the old English verb guard. This utterance
            # cannot manufacture an Agent receipt or change native task state.
            chat.provider.reply = "Here is the translated prose and the highlighted workflow."
            await respond(env, chat, mode, text)
            prompt = chat.provider.prompts[-1]
            assert prompt[-1]["content"] == text
            assert "prefer a brief acknowledgment" in prompt[0]["content"]
            assert "speak naturally about unrelated topics" in prompt[0]["content"]
            facts = json.loads(prompt[0]["content"].split("\n")[-1])
            assert facts["tasks"][0]["state"] == state.value
            assert facts["tasks"][0]["semantic_verification"] == "not_assessed"
            assert rec.task.state == state and not rec.done.is_set()
            assert env.store.read(kinds=["agent.outcome"]) == []
            assert len(env.provider.calls) == 1


async def test_loopback_metadata_transport_ignores_environment_proxies(tmp_path, monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    async with environment(tmp_path, socket_http=True) as env:
        client = TaskContextClient(env.base, TOKENS["converse"])
        try:
            assert (await client.lookup(proposal(env))).tasks[0].state == "running"
        finally:
            await client.close()
