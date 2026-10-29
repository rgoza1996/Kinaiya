"""Native local routing preserves disclosure; denied remote sources cannot starve later work."""

import json

import pytest

from packages.contracts.models import AppendRequest, ProviderInfo, SessionRequest, Task
from packages.providers.text import ScriptedProvider
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.session.store import LocalJournal, Store


class RecordingAgent:
    def __init__(self, task):
        self.task, self.requests = task, []

    async def status(self):
        return [self.task]

    async def submit(self, request, actor):
        assert actor == "decisions"
        self.requests.append(request)

    async def steer(self, task_id, request, actor):
        assert task_id == self.task.task_id and actor == "decisions"
        self.requests.append(request)


@pytest.mark.parametrize("route", ["queue", "steer"])
@pytest.mark.parametrize(
    "scope", [None, "local_only", "public_synthetic", "private_cloud_authorized"]
)
async def test_local_classifier_preserves_owner_scope_in_native_dispatch(tmp_path, route, scope):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Synthetic existing work",
        state="running",
        idempotency_key="task",
    )
    agent = RecordingAgent(task)
    result = {"route": route}
    if route == "steer":
        result.update(target_task_id=task.task_id, expected_objective_revision=1)
    provider = ScriptedProvider([json.dumps(result)])
    worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))
    payload = {"text": "Synthetic instruction", "final": True, "revision": 1}
    if scope is not None:
        payload["content_scope"] = scope
    source = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload=payload,
            idempotency_key="source",
        ),
        "owner",
    )
    try:
        await worker.tick()
        assert len(provider.calls) == 1
        assert len(agent.requests) == 1
        assert agent.requests[0].content_scope == (scope or "local_only")
        assert agent.requests[0].source_event_ids == [source.event_id]
    finally:
        store.close()


async def test_denied_remote_source_is_recorded_without_inference_and_later_public_turn_runs(
    tmp_path,
):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=1,
        objective="Synthetic task",
        state="running",
        idempotency_key="task",
    )
    agent = RecordingAgent(task)
    provider = ScriptedProvider([json.dumps({"route": "ignore"})])
    provider.info = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
    sources = []
    for scope, text in [
        ("local_only", "SYNTHETIC_WITHHELD_95"),
        ("public_synthetic", "Public input"),
    ]:
        sources.append(
            store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=1,
                    kind="conversation.user",
                    payload={"text": text, "final": True, "content_scope": scope},
                    idempotency_key=scope,
                ),
                "owner",
            )
        )
    worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))
    try:
        await worker.tick()
        await worker.tick()
        assert len(provider.calls) == 1
        assert "SYNTHETIC_WITHHELD_95" not in json.dumps(provider.calls)
        assert not agent.requests
        refused = store.read(kinds=["routing.classification_refused"])
        assert len(refused) == 1
        assert refused[0].payload["source_event_id"] == sources[0].event_id
        assert store.cursor("decisions", worker.consumer) >= sources[-1].sequence
    finally:
        store.close()


@pytest.mark.parametrize("restricted_scope", [None, "local_only", "private_cloud_authorized"])
async def test_remote_classifier_cannot_export_relabeled_revision_or_its_original_context(
    tmp_path, restricted_scope
):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    journal = LocalJournal(store, "owner")
    provider = ScriptedProvider([json.dumps({"route": "ignore"})])
    provider.info = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=1,
        objective="Synthetic task",
        idempotency_key="task",
    )
    worker = DecisionsWorker(
        LocalJournal(store, "decisions"), RecordingAgent(task), ModelClassifier(provider)
    )

    async def append(kind, payload, key):
        return await journal.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                kind=kind,
                payload=payload,
                idempotency_key=key,
            )
        )

    try:
        source = await append(
            "conversation.user",
            {
                "text": "SYNTHETIC_STICKY_CONTEXT_95",
                "revision": 1,
                "final": True,
                "content_scope": "public_synthetic",
            },
            "original",
        )
        restricted = {
            "target_event_id": source.event_id,
            "text": "SYNTHETIC_STICKY_REVISION_95",
            "revision": 2,
        }
        if restricted_scope is not None:
            restricted["content_scope"] = restricted_scope
        await append("conversation.revision", restricted, "restricted")
        relabeled = await append(
            "conversation.revision",
            {
                "target_event_id": source.event_id,
                "text": "SYNTHETIC_STICKY_REVISION_95",
                "revision": 3,
                "content_scope": "public_synthetic",
            },
            "relabeled",
        )
        await worker.process(relabeled)
        assert provider.calls == []
        current = await append(
            "conversation.user",
            {
                "text": "Allowed current public turn",
                "revision": 1,
                "final": True,
                "content_scope": "public_synthetic",
            },
            "current",
        )
        await worker.process(current)
        assert len(provider.calls) == 1
        assert "SYNTHETIC_STICKY_" not in json.dumps(provider.calls)
    finally:
        store.close()
